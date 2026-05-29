from unittest.mock import MagicMock

from sage import llm


def test_resolve_gpu_layers():
    mock_cfg = MagicMock()
    mock_cfg.gpu_layers = "auto"
    assert llm._resolve_gpu_layers("cpu", 8000, mock_cfg) == 0
    assert llm._resolve_gpu_layers("cuda", 12000, mock_cfg) == llm._GPU_ALL_LAYERS
    assert llm._resolve_gpu_layers("cuda", 3000, mock_cfg) == llm._VRAM_PARTIAL_LAYERS
    mock_cfg.gpu_layers = "32"
    assert llm._resolve_gpu_layers("cuda", 12000, mock_cfg) == 32


def test_build_cmd():
    mock_bin = MagicMock()
    mock_bin.name = "llama-server"
    mock_cfg = MagicMock()
    mock_cfg.active_model_path = MagicMock()
    mock_cfg.active_parallel_slots = "auto"
    mock_cfg.flash_attention = True

    cmd, slots = llm._build_cmd(mock_bin, mock_cfg, 8000, 32, 4096, 4, 8, "cuda")
    assert "--port" in cmd
    assert "8000" in cmd
    assert "--n-gpu-layers" in cmd
    assert "32" in cmd
    assert "--flash-attn" in cmd

    # CPU specific
    cmd_cpu, slots_cpu = llm._build_cmd(mock_bin, mock_cfg, 8001, 0, 4096, 4, 8, "cpu")
    assert "--model" in cmd_cpu
