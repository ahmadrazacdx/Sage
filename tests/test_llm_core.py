import sys
import threading
import subprocess
import urllib.request
import urllib.error
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import psutil
from sage import llm
from sage.config import LLMSettings

@pytest.fixture
def mock_cfg():
    cfg = LLMSettings()
    cfg.llama_cpp_cpu_bin = Path("/fake/cpu/llama-server.exe")
    cfg.llama_cpp_cuda_bin = Path("/fake/cuda/llama-server.exe")
    cfg.active_model_path = Path("/fake/model.gguf")
    cfg.model_path_cpu = Path("/fake/model_cpu.gguf")
    cfg.model_path_cuda = Path("/fake/model_cuda.gguf")
    return cfg

def test_detect_gpu_cuda():
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch("subprocess.run") as mock_run:
        
        mock_run.return_value = MagicMock(returncode=0, stdout="RTX 4090,24000\n", stderr="")
        res = llm.detect_gpu()
        assert res["backend"] == "cuda"
        assert res["gpu_name"] == "RTX 4090"
        assert res["vram_mb"] == 24000

def test_detect_gpu_cuda_fallback():
    with patch("shutil.which", return_value=None), \
         patch("sys.platform", "win32"), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("subprocess.run") as mock_run:
        
        mock_run.return_value = MagicMock(returncode=0, stdout="RTX 3080,8000\n", stderr="")
        res = llm.detect_gpu()
        assert res["backend"] == "cuda"
        assert res["gpu_name"] == "RTX 3080"
        assert res["vram_mb"] == 8000

def test_detect_gpu_cpu_only():
    with patch("shutil.which", return_value=None), \
         patch("sys.platform", "linux"):
        res = llm.detect_gpu()
        assert res["backend"] == "cpu"
        assert res["vram_mb"] == 0

def test_binary_installation_ok(tmp_path):
    bin_path = tmp_path / "llama-server.exe"
    
    assert not llm._binary_installation_ok(bin_path, "cpu")
    
    bin_path.touch()
    assert not llm._binary_installation_ok(bin_path, "cpu")
    
    for dll in llm._BASE_COMPANION_DLLS:
        (tmp_path / dll).touch()
    assert llm._binary_installation_ok(bin_path, "cpu")
    
    assert not llm._binary_installation_ok(bin_path, "cuda")
    
    for dll in llm._CUDA_COMPANION_DLLS:
        (tmp_path / dll).touch()
    assert llm._binary_installation_ok(bin_path, "cuda")

def test_resolve_binary(tmp_path, mock_cfg):
    cpu_bin = tmp_path / "cpu" / "llama-server.exe"
    cuda_bin = tmp_path / "cuda" / "llama-server.exe"
    mock_cfg.llama_cpp_cpu_bin = cpu_bin
    mock_cfg.llama_cpp_cuda_bin = cuda_bin
    
    cpu_bin.parent.mkdir(parents=True)
    cpu_bin.touch()
    for dll in llm._BASE_COMPANION_DLLS:
        (cpu_bin.parent / dll).touch()
    
    res_path, res_backend = llm._resolve_binary({"backend": "cpu"}, mock_cfg)
    assert res_path == cpu_bin
    assert res_backend == "cpu"
    
    cuda_bin.parent.mkdir(parents=True)
    cuda_bin.touch()
    for dll in llm._BASE_COMPANION_DLLS:
        (cuda_bin.parent / dll).touch()
    for dll in llm._CUDA_COMPANION_DLLS:
        (cuda_bin.parent / dll).touch()
        
    res_path, res_backend = llm._resolve_binary({"backend": "cuda"}, mock_cfg)
    assert res_path == cuda_bin
    assert res_backend == "cuda"

    (cuda_bin.parent / llm._CUDA_COMPANION_DLLS[0]).unlink()
    res_path, res_backend = llm._resolve_binary({"backend": "cuda"}, mock_cfg)
    assert res_path == cpu_bin
    assert res_backend == "cpu"

def test_resolve_gpu_layers(mock_cfg):
    assert llm._resolve_gpu_layers("cpu", 0, mock_cfg) == 0
    assert llm._resolve_gpu_layers("cuda", 24000, mock_cfg) == llm._GPU_ALL_LAYERS
    assert llm._resolve_gpu_layers("cuda", 2000, mock_cfg) == llm._VRAM_PARTIAL_LAYERS
    assert llm._resolve_gpu_layers("cuda", 1000, mock_cfg) == 0

def test_resolve_context_size(mock_cfg):
    with patch("psutil.virtual_memory") as mock_vmem:
        mock_vmem.return_value = MagicMock(available=8000 * 1024 * 1024)
        assert llm._resolve_context_size("cpu", 0, mock_cfg) == llm._CTX_32K
    
    assert llm._resolve_context_size("cuda", 24000, mock_cfg) == llm._CTX_64K
    assert llm._resolve_context_size("cuda", 8000, mock_cfg) == llm._CTX_32K
    assert llm._resolve_context_size("cuda", 1000, mock_cfg) == llm._CTX_VRAM_LOW

def test_resolve_thread_count():
    with patch("psutil.cpu_count", side_effect=[12, 16]):
        gen, batch = llm._resolve_thread_count()
        assert gen == 6
        assert batch == 12

    with patch("psutil.cpu_count", side_effect=[None, None]):
        gen, batch = llm._resolve_thread_count()
        assert gen == 4
        assert batch == 4

def test_kill_orphaned_servers():
    mock_proc1 = MagicMock()
    mock_proc1.info = {"name": "llama-server", "cmdline": ["llama-server", "--model", "model.gguf"]}
    mock_proc1.parent.side_effect = psutil.NoSuchProcess(123)
    
    mock_proc2 = MagicMock()
    mock_proc2.info = {"name": "python", "cmdline": ["python", "app.py"]}
    
    with patch("psutil.process_iter", return_value=[mock_proc1, mock_proc2]), \
         patch("time.sleep"):
        llm._kill_orphaned_servers(Path("/fake/model.gguf"))
        mock_proc1.kill.assert_called_once()
        mock_proc2.kill.assert_not_called()

def test_find_free_port():
    port = llm._find_free_port()
    assert isinstance(port, int)
    assert port > 0

def test_wait_for_server():
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    
    with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep"):
        mock_urlopen.return_value.__enter__.return_value.status = 200
        llm._wait_for_server(8000, mock_proc, [b"test"], 1.0)
    
    mock_proc.poll.return_value = 1
    with pytest.raises(RuntimeError, match="llama-server exited immediately"):
        llm._wait_for_server(8000, mock_proc, [b"crash"], 1.0)

def test_wait_for_server_timeout():
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("test")), \
         patch("time.monotonic", side_effect=[0, 0.5, 1.5, 2.0]), \
         patch("time.sleep"):
        with pytest.raises(RuntimeError, match="llama-server did not become healthy"):
            llm._wait_for_server(8000, mock_proc, [], 1.0)

def test_warmup_server():
    with patch("urllib.request.urlopen") as mock_urlopen:
        llm._warmup_server(8000)
        mock_urlopen.assert_called_once()
        
    with patch("urllib.request.urlopen", side_effect=Exception("warmup fail")):
        llm._warmup_server(8000)

def test_terminate_process():
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    llm._terminate_process(mock_proc)
    mock_proc.terminate.assert_called_once()
    mock_proc.wait.assert_called_once()

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="", timeout=5), None]
    llm._terminate_process(mock_proc)
    mock_proc.kill.assert_called_once()



def test_validate_paths(mock_cfg):
    with pytest.raises(FileNotFoundError, match="llama-server binary not found"):
        llm._validate_paths(mock_cfg, Path("/fake/not/exist.exe"))

    with patch("pathlib.Path.exists", side_effect=[True, False]):
        with pytest.raises(FileNotFoundError, match="GGUF model file not found"):
            llm._validate_paths(mock_cfg, Path("/fake/exist.exe"))

def test_create_llm():
    with patch("sage.llm.get_settings") as mock_get_settings:
        mock_cfg = MagicMock()
        mock_cfg.active_model_name = "test"
        mock_cfg.temperature = 0.5
        mock_cfg.max_tokens = 1000
        mock_cfg.thinking_mode = True
        mock_get_settings.return_value.llm = mock_cfg
        mock_get_settings.return_value.agent.llm_timeout = 600
        
        llm_instance = llm.create_llm(8000)
        assert llm_instance is not None

def test_with_thinking():
    mock_llm = MagicMock()
    llm._with_thinking(mock_llm, 1000)
    mock_llm.bind.assert_called_once()



def test_create_utility_llm():
    with patch("sage.llm.get_settings") as mock_get_settings:
        mock_cfg = MagicMock()
        mock_cfg.util_model_name = "util"
        mock_get_settings.return_value.llm = mock_cfg
        
        util_llm = llm.create_utility_llm(8001)
        assert util_llm is not None

def test_resolve_thread_count_logic():
    with patch("psutil.cpu_count", return_value=16):
        assert llm._resolve_thread_count() == (8, 16)
    with patch("psutil.cpu_count", return_value=2):
        assert llm._resolve_thread_count() == (2, 2)

def test_binary_installation_check(tmp_path):
    bin_path = tmp_path / "llama-server.exe"
    bin_path.touch()
    for d in llm._BASE_COMPANION_DLLS:
        (tmp_path / d).touch()
    assert llm._binary_installation_ok(bin_path, "cpu") is True
    for d in llm._CUDA_COMPANION_DLLS:
        (tmp_path / d).touch()
    assert llm._binary_installation_ok(bin_path, "cuda") is True

@patch("subprocess.run")
def test_detect_gpu_failure(mock_run):
    mock_run.side_effect = Exception("error")
    res = llm.detect_gpu()
    assert res["backend"] == "cpu"
