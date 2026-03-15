"""
llama-server lifecycle manager for Sage.

Spawns the llama-server binary as a subprocess, waits for its HTTP
health endpoint, and exposes a LangChain ChatOpenAI factory for all
agent nodes.

Usage::

    proc, port, gpu_info = start_llm_server()
    llm = create_llm(port)
    response = await llm.ainvoke(messages)
"""

from __future__ import annotations

import atexit
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psutil
import structlog
from langchain_openai import ChatOpenAI

from sage.config import LLMSettings, get_settings

log = structlog.get_logger(__name__)

# --- Constants ---
_LLAMA_SERVER_HOST: str = "127.0.0.1"
_HEALTH_POLL_INTERVAL_S: float = 0.5
_SERVER_STARTUP_TIMEOUT_S: float = 60.0

# Vulkan: VRAM is unknown via vulkaninfo; disable offload to prevent iGPU bottlenecks.
_VULKAN_DEFAULT_LAYERS: int = 0

# GPU layer offload thresholds (VRAM in MB).
_VRAM_FULL_OFFLOAD_MB: int = 4_000
_VRAM_PARTIAL_OFFLOAD_MB: int = 2_000
_VRAM_PARTIAL_LAYERS: int = 24
_GPU_ALL_LAYERS: int = -1  # llama.cpp sentinel: offload every layer to GPU

# CPU RAM thresholds and corresponding context sizes (MB).
_RAM_16GB_MB: int = 16_000
_RAM_8GB_MB: int = 8_000
_RAM_6GB_MB: int = 6_000

_CTX_32K: int = 32_768
_CTX_16K: int = 16_384
_CTX_8K: int = 8_192
_CTX_4K: int = 4_096

# GPU VRAM thresholds for context scaling (MB).
_VRAM_8GB_MB: int = 8_000
_VRAM_4GB_MB: int = 4_000
_CTX_VRAM_8GB: int = 131_072
_CTX_VRAM_4GB: int = 65_536
_CTX_VRAM_LOW: int = 8_192


# --- GPU Detection ---
def detect_gpu() -> dict[str, Any]:
    """Probe the host machine for an available GPU accelerator.

    Tries each backend in order of specificity: CUDA (NVIDIA) ->
    Vulkan (cross-vendor) -> CPU fallback.  The first successful
    detection is returned immediately; no backend is tried after a
    successful hit.

    Returns:
        A dict with the following keys:

        - ``backend`` (str): One of ``"cuda"``, ``"vulkan"``, or
          ``"cpu"``.
        - ``vram_mb`` (int): Available VRAM in megabytes.  ``0`` when
          VRAM cannot be determined (Vulkan) or no GPU is present.
        - ``gpu_name`` (str | None): Human-readable GPU name, or
          ``None`` for CPU-only machines.

    Note:
        This function never raises.  Subprocess failures are caught,
        logged at WARNING level, and execution falls through to the
        next backend.
    """
    # CUDA: nvidia-smi returns VRAM in MiB with machine-readable precision.
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(", ", 1)
                if len(parts) == 2:
                    gpu_name, vram_str = parts
                    return {
                        "backend": "cuda",
                        "vram_mb": int(float(vram_str)),
                        "gpu_name": gpu_name.strip(),
                    }
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            log.warning("gpu_detection_cuda_failed", error=str(exc))

    # Vulkan: covers AMD, Intel, and NVIDIA without the CUDA toolkit.
    if shutil.which("vulkaninfo"):
        try:
            result = subprocess.run(
                ["vulkaninfo", "--summary"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return {
                    "backend": "vulkan",
                    "vram_mb": 0,
                    "gpu_name": "Vulkan-compatible GPU",
                }
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning("gpu_detection_vulkan_failed", error=str(exc))

    # 3. CPU fallback
    return {"backend": "cpu", "vram_mb": 0, "gpu_name": None}


# --- Hardware Resolution ---
def _resolve_gpu_layers(gpu_info: dict[str, Any], cfg: LLMSettings) -> int:
    """Return the value for ``--n-gpu-layers`` based on hardware and config.

    When ``cfg.gpu_layers`` is not ``"auto"``, that explicit value is
    returned directly.  Otherwise the detected backend and VRAM
    determine the offload depth: CPU returns 0, Vulkan returns a
    conservative partial count, and CUDA offloads all layers when VRAM
    exceeds the model size.
    """
    if cfg.gpu_layers != "auto":
        return int(cfg.gpu_layers)

    backend: str = gpu_info["backend"]
    vram: int = gpu_info["vram_mb"]

    if backend == "cpu":
        return 0

    if backend == "vulkan":
        return _VULKAN_DEFAULT_LAYERS

    # CUDA: VRAM is known
    if vram >= _VRAM_FULL_OFFLOAD_MB:
        return _GPU_ALL_LAYERS
    if vram >= _VRAM_PARTIAL_OFFLOAD_MB:
        return _VRAM_PARTIAL_LAYERS
    return 0


def _resolve_context_size(gpu_info: dict[str, Any], cfg: LLMSettings) -> int:
    """Return the value for ``--ctx-size`` based on hardware and config.

    When ``cfg.context_window`` is not ``"auto"``, that explicit value
    is returned directly.  Otherwise available system RAM (CPU path) or
    VRAM (GPU path) is used to select the largest context that leaves
    sufficient headroom for the OS and application process.
    """
    if cfg.context_window != "auto":
        return int(cfg.context_window)

    backend: str = gpu_info["backend"]
    vram: int = gpu_info["vram_mb"]

    if backend == "cpu":
        total_ram_mb: int = psutil.virtual_memory().total // (1024 * 1024)
        if total_ram_mb >= _RAM_16GB_MB:
            return _CTX_32K
        if total_ram_mb >= _RAM_8GB_MB:
            return _CTX_8K
        if total_ram_mb >= _RAM_6GB_MB:
            # 6GB systems cannot handle 8K context with a 4B parameter model without paging to disk.
            # Limiting to 2K context provides a critical memory buffer.
            return 2048
        return _CTX_4K

    # GPU path
    if vram >= _VRAM_8GB_MB:
        return _CTX_VRAM_8GB
    if vram >= _VRAM_4GB_MB:
        return _CTX_VRAM_4GB
    return _CTX_VRAM_LOW


# --- Port Allocation ---
def _find_free_port() -> int:
    """Return an OS-assigned free TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_LLAMA_SERVER_HOST, 0))
        return int(sock.getsockname()[1])


# --- Health Check ---
def _wait_for_server(port: int, timeout_s: float = _SERVER_STARTUP_TIMEOUT_S) -> None:
    """Block until llama-server reports healthy or the timeout expires.

    Polls ``GET /health`` at a fixed interval.  The server returns HTTP
    200 once the model weights are memory-mapped and the KV cache is
    allocated.  Callers should not issue inference requests before this
    function returns.

    Args:
        port: The TCP port llama-server is listening on.
        timeout_s: Maximum number of seconds to wait before raising.

    Raises:
        RuntimeError: If the server does not become healthy within
            ``timeout_s`` seconds.
    """
    url = f"http://{_LLAMA_SERVER_HOST}:{port}/health"
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                if resp.status == 200:
                    log.info("llama_server_healthy", port=port)
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(_HEALTH_POLL_INTERVAL_S)

    raise RuntimeError(
        f"llama-server did not become healthy within {timeout_s:.0f}s on port {port}. "
        "Verify that the binary path is correct and the model file is not corrupted."
    )


# --- Teardown ---
def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    """Terminate a llama-server process gracefully with a forced kill fallback.

    Sends SIGTERM and waits up to 5 seconds for a clean exit.  If the
    process has not exited by then, SIGKILL is sent.  Calling this
    function on an already-exited process is a no-op.
    """
    if proc.poll() is not None:
        return
    log.info("llama_server_stopping", pid=proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning("llama_server_force_killed", pid=proc.pid)
        proc.kill()
        proc.wait()


# --- Server Lifecycle ---
def start_llm_server() -> tuple[subprocess.Popen[bytes], int, dict[str, Any]]:
    """Detect hardware, spawn llama-server, and wait for it to become ready.

    Performs hardware detection, resolves the appropriate GPU layer
    count and context size, and starts the llama-server subprocess on a
    dynamically allocated loopback port.  The call blocks until the
    server passes its health check.

    Teardown is registered on two channels so the subprocess is never
    orphaned:

    - ``atexit``: runs when the Python interpreter exits normally.
    - ``SIGTERM`` handler: runs when the process receives SIGTERM.

    Returns:
        A 3-tuple of:

        - ``proc`` (subprocess.Popen): The running server process.
          The caller must retain a reference to prevent garbage
          collection of the atexit registration.
        - ``port`` (int): The loopback port the server is listening on.
        - ``gpu_info`` (dict): Hardware detection result with keys
          ``backend``, ``vram_mb``, and ``gpu_name``.  Pass this to
          the ``/api/status`` endpoint for UI display.

    Raises:
        FileNotFoundError: If the llama-server binary or model file
            does not exist at the configured path.
        RuntimeError: If the server does not pass its health check
            within the startup timeout.
    """
    cfg = get_settings().llm
    _validate_paths(cfg)

    gpu_info = detect_gpu()
    gpu_layers = _resolve_gpu_layers(gpu_info, cfg)
    ctx_size = _resolve_context_size(gpu_info, cfg)
    port = _find_free_port()

    log.info(
        "llama_server_starting",
        backend=gpu_info["backend"],
        gpu_name=gpu_info["gpu_name"],
        vram_mb=gpu_info["vram_mb"],
        gpu_layers=gpu_layers,
        ctx_size=ctx_size,
        port=port,
        model=cfg.model_path.name,
    )

    proc = subprocess.Popen(
        _build_cmd(cfg, port, gpu_layers, ctx_size),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    atexit.register(_terminate_process, proc)

    def _sigterm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        """Ensure llama-server is terminated when the parent receives SIGTERM."""
        _terminate_process(proc)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    _wait_for_server(port)

    log.info(
        "llama_server_ready",
        backend=gpu_info["backend"],
        ctx_size=ctx_size,
        gpu_layers=gpu_layers,
        port=port,
    )
    return proc, port, gpu_info


def _validate_paths(cfg: LLMSettings) -> None:
    """Raise FileNotFoundError early if the binary or model path is missing."""
    binary: Path = cfg.llama_cpp_bin
    model: Path = cfg.model_path

    if not binary.exists():
        raise FileNotFoundError(
            f"llama-server binary not found at: {binary}\n"
            "Download a pre-built release from https://github.com/ggml-org/llama.cpp/releases "
            "and place it at the path configured under [llm] llama_cpp_bin."
        )
    if not model.exists():
        raise FileNotFoundError(
            f"GGUF model file not found at: {model}\n"
            "Ensure the quantized model is placed at the path configured under [llm] model_path."
        )


def _build_cmd(
    cfg: LLMSettings,
    port: int,
    gpu_layers: int,
    ctx_size: int,
) -> list[str]:
    """Assemble the llama-server argv list from resolved hardware parameters."""
    # Use physical cores to avoid hyperthreading cache thrashing
    physical_cores = psutil.cpu_count(logical=False)
    
    return [
        str(cfg.llama_cpp_bin),
        "--model",
        str(cfg.model_path),
        "--host",
        _LLAMA_SERVER_HOST,
        "--port",
        str(port),
        "--ctx-size",
        str(ctx_size),
        "--n-gpu-layers",
        str(gpu_layers),
        "--threads",
        str(physical_cores) if physical_cores else "4",
        "--batch-size",
        "128",  # Reduces prompt ingestion RAM spikes
        "--mlock",
        "--cache-type-k",
        cfg.cache_type_k,
        "--cache-type-v",
        cfg.cache_type_v,
        "--jinja",  # required for Qwen3.5 Jinja2 chat template
        "--reasoning-format",
        "deepseek",  # strips <think> blocks from streamed output
    ]


# --- LangChain Factory ---
def create_llm(port: int) -> ChatOpenAI:
    """Return a streaming ChatOpenAI client pointed at the local llama-server.

    Reads generation parameters from the application config.  When
    thinking mode is enabled and ``max_tokens`` is below 4096, the
    token budget is silently raised to 4096 to ensure the model has
    sufficient headroom for chain-of-thought tokens before producing
    the visible answer.

    This function is stateless and may be called multiple times.  Each
    call returns an independent ``ChatOpenAI`` instance sharing the
    same underlying server.

    Args:
        port: The TCP port returned by ``start_llm_server()``.

    Returns:
        A ``ChatOpenAI`` instance with ``streaming=True`` configured
        for use with ``astream_events`` v2.
    """
    cfg = get_settings().llm

    max_tok = cfg.max_tokens
    if cfg.thinking_mode and max_tok < 4096:
        max_tok = 4096
        log.debug(
            "thinking_mode_token_budget_raised",
            configured=cfg.max_tokens,
            effective=max_tok,
        )

    return ChatOpenAI(
        base_url=f"http://{_LLAMA_SERVER_HOST}:{port}/v1",
        api_key="local",
        model=cfg.model_name,
        temperature=cfg.temperature,
        max_tokens=max_tok,
        streaming=True,
    )
