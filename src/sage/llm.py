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
import json
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
_SERVER_STARTUP_TIMEOUT_S: float = 180.0

# Vulkan: VRAM is unknown via vulkaninfo; offload conservatively.
_VULKAN_DEFAULT_LAYERS: int = 0

# GPU layer offload thresholds (VRAM in MB).
_VRAM_FULL_OFFLOAD_MB: int = 4_000
_VRAM_PARTIAL_OFFLOAD_MB: int = 2_000
_VRAM_PARTIAL_LAYERS: int = 24
_GPU_ALL_LAYERS: int = -1  # llama.cpp sentinel: offload every layer to GPU

# ---- context-size thresholds ----

_AVAIL_7GB_MB: int = 7_000  # comfortable: ctx=16K
_AVAIL_5GB_MB: int = 5_000  # adequate: ctx=8K
_AVAIL_3_5GB_MB: int = 3_500  # tight: ctx=4K
# Below 3.5 GB available: ctx=2K, mmap paging is expected

_CTX_16K: int = 16_384
_CTX_8K: int = 8_192
_CTX_4K: int = 4_096
_CTX_2K: int = 2_048

# GPU VRAM thresholds for context scaling (MB).
_VRAM_8GB_MB: int = 8_000
_VRAM_4GB_MB: int = 4_000
_CTX_VRAM_8GB: int = 65_536
_CTX_VRAM_4GB: int = 32_768
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

    # CPU fallback
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
    is returned directly.

    For CPU paths, uses ``psutil.virtual_memory().available`` — the
    amount of RAM the OS can give to new allocations right now — not
    total RAM.
    """
    if cfg.context_window != "auto":
        return int(cfg.context_window)

    backend: str = gpu_info["backend"]
    vram: int = gpu_info["vram_mb"]

    if backend == "cpu":
        available_mb: int = psutil.virtual_memory().available // (1024 * 1024)

        try:
            model_size_mb = cfg.model_path.stat().st_size // (1024 * 1024)
        except OSError:
            model_size_mb = 2860

        size_diff = 2860 - model_size_mb

        # Absolute minimum floors to ensure OS and apps overhead is covered.
        t_16k = max(_AVAIL_7GB_MB - size_diff, 4_000)
        t_8k = max(_AVAIL_5GB_MB - size_diff, 3_000)
        t_4k = max(_AVAIL_3_5GB_MB - size_diff, 2_000)

        log.info(
            "context_size_resolution",
            available_ram_mb=available_mb,
            model_size_mb=model_size_mb,
            backend="cpu",
        )

        if available_mb >= t_16k:
            return _CTX_16K
        if available_mb >= t_8k:
            return _CTX_8K
        if available_mb >= t_4k:
            return _CTX_4K
        return _CTX_2K

    # GPU path
    if vram >= _VRAM_8GB_MB:
        return _CTX_VRAM_8GB
    if vram >= _VRAM_4GB_MB:
        return _CTX_VRAM_4GB
    return _CTX_VRAM_LOW


# --- Binary Resolution ---
def _resolve_binary(gpu_info: dict[str, Any], cfg: LLMSettings) -> Path:
    """Return the llama-server binary appropriate for the detected GPU backend.

    When ``cfg.llama_cpp_bin`` lives inside a ``servers/{backend}/``
    directory, this function tries to swap the backend subfolder
    to match the detected hardware::

        configured : artifacts/servers/cpu/llama-server.exe
        detected   : cuda
        candidate  : artifacts/servers/cuda/llama-server.exe

    If the candidate exists it is returned (GPU binary).  If it is absent
    a warning is logged and the original configured path is returned so the
    server still starts in CPU mode.

    Paths outside the ``servers/{x}/`` layout are returned unchanged.
    """
    backend: str = gpu_info["backend"]
    configured: Path = cfg.llama_cpp_bin

    # Find the "servers" segment in the path parts.
    parts = configured.parts
    try:
        servers_idx = next(i for i, p in enumerate(parts) if p.lower() == "servers")
    except StopIteration:
        # Custom path outside standard layout
        log.debug("binary_resolution_skipped", reason="non-standard path", path=str(configured))
        return configured

    # Already using the correct backend folder.
    current_backend = parts[servers_idx + 1] if servers_idx + 1 < len(parts) else ""
    if current_backend == backend:
        return configured

    # Swap the backend subfolder.
    candidate: Path = Path(*parts[: servers_idx + 1]) / backend / Path(*parts[servers_idx + 2 :])

    if candidate.exists():
        log.info(
            "binary_auto_selected",
            backend=backend,
            from_path=str(configured),
            to_path=str(candidate),
        )
        return candidate

    log.warning(
        "binary_upgrade_unavailable",
        detected_backend=backend,
        candidate=str(candidate),
        fallback=str(configured),
        hint=f"Place the {backend} llama.cpp release in: {candidate.parent}",
    )
    return configured


# --- Thread Count ---
def _resolve_thread_count() -> tuple[int, int]:
    """Return (generation_threads, batch_threads) tuned for the CPU.

    Two distinct thread counts are optimal for llama.cpp's dual-phase
    compute:

    - Token generation (decode): memory-bandwidth-bound.  More
      threads beyond the physical-core count causes cache thrashing.
      On Intel hybrid CPUs (Alder Lake / Raptor Lake), E-cores lack
      AVX-512 and stall the reduction tree, use P-cores only.
    - Prompt ingestion (encode / prefill): compute-bound.  All
      cores including E-cores are useful here.

    Heuristic for Intel 12th-gen hybrid (4P + 4E = 8 physical):
        generation_threads = physical // 2  (P-cores only)
        batch_threads      = physical       (all cores)

    For non-hybrid CPUs (AMD Ryzen, Intel 10th/11th gen, ARM):
        both = physical_cores
    """
    physical: int | None = psutil.cpu_count(logical=False)
    logical: int | None = psutil.cpu_count(logical=True)

    if physical is None:
        physical = 4
    if logical is None:
        logical = physical

    hyperthreading_ratio = logical / physical if physical > 0 else 1
    is_likely_hybrid = physical >= 8 and hyperthreading_ratio < 2.0

    # P-cores only for generation to avoid E-core stall in GEMM reduction.
    gen_threads = max(physical // 2, 1) if is_likely_hybrid else physical

    batch_threads = physical  # All cores for prompt prefill (compute-bound).

    log.info(
        "thread_resolution",
        physical_cores=physical,
        logical_cores=logical,
        is_likely_hybrid=is_likely_hybrid,
        generation_threads=gen_threads,
        batch_threads=batch_threads,
    )
    return gen_threads, batch_threads


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
    last_progress_log = time.monotonic()

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                if resp.status == 200:
                    log.info("llama_server_healthy", port=port)
                    return
        except (urllib.error.URLError, OSError):
            pass

        # Log progress every 15 seconds so the user sees the server is loading.
        now = time.monotonic()
        if now - last_progress_log >= 15.0:
            elapsed = now - (deadline - timeout_s)
            log.info(
                "llama_server_loading",
                elapsed_s=round(elapsed),
                timeout_s=round(timeout_s),
                port=port,
            )
            last_progress_log = now

        time.sleep(_HEALTH_POLL_INTERVAL_S)

    raise RuntimeError(
        f"llama-server did not become healthy within {timeout_s:.0f}s on port {port}. "
        "Verify that the binary path is correct and the model file is not corrupted. "
        "On slow storage (HDD/SATA SSD), increase llm.startup_timeout in config."
    )


def _warmup_server(port: int) -> None:
    """Send a single-token completion request to trigger GGML graph JIT.

    llama.cpp compiles its compute graph on the first real inference
    call.  Without a warm-up request, the first user message pays a
    2–8 second compilation penalty.  This call forces that compilation
    at startup instead.

    Failures are logged at WARNING level and swallowed, a failed
    warm-up is not fatal.
    """
    url = f"http://{_LLAMA_SERVER_HOST}:{port}/v1/completions"
    payload = json.dumps({"prompt": ".", "max_tokens": 1}).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            resp.read()
        log.info("llama_server_warmed_up", port=port)
    except Exception as exc:  # noqa: BLE001
        log.warning("llama_server_warmup_failed", error=str(exc))


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
    server passes its health check and a JIT warm-up request completes.

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
    binary = _resolve_binary(gpu_info, cfg)
    gpu_layers = _resolve_gpu_layers(gpu_info, cfg)
    ctx_size = _resolve_context_size(gpu_info, cfg)
    gen_threads, batch_threads = _resolve_thread_count()
    port = _find_free_port()

    log.info(
        "llama_server_starting",
        backend=gpu_info["backend"],
        gpu_name=gpu_info["gpu_name"],
        vram_mb=gpu_info["vram_mb"],
        gpu_layers=gpu_layers,
        ctx_size=ctx_size,
        gen_threads=gen_threads,
        batch_threads=batch_threads,
        port=port,
        binary=str(binary),
        model=cfg.model_path.name,
        available_ram_mb=psutil.virtual_memory().available // (1024 * 1024),
    )

    proc = subprocess.Popen(
        _build_cmd(cfg, binary, port, gpu_layers, ctx_size, gen_threads, batch_threads),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    atexit.register(_terminate_process, proc)

    def _sigterm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        """Ensure llama-server is terminated when the parent receives SIGTERM."""
        _terminate_process(proc)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    _wait_for_server(port, timeout_s=cfg.startup_timeout)
    _warmup_server(port)

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
    binary: Path,
    port: int,
    gpu_layers: int,
    ctx_size: int,
    gen_threads: int,
    batch_threads: int,
) -> list[str]:
    """Assemble the llama-server argv list from resolved hardware parameters."""
    try:
        model_size_mb = cfg.model_path.stat().st_size // (1024 * 1024)
    except OSError:
        model_size_mb = 2860

    ubatch = "64" if model_size_mb < 2000 else "128"

    cmd = [
        str(binary),
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
        str(gen_threads),
        "--threads-batch",
        str(batch_threads),
        "--batch-size",
        "512",
        "--ubatch-size",
        ubatch,
        "--flash-attn",
        "auto",
        "--cache-type-k",
        cfg.cache_type_k,
        "--cache-type-v",
        cfg.cache_type_v,
        "--cont-batching",  # interleave prompt ingestion with token generation
        "--jinja",  # required for Qwen3.5 Jinja2 chat template
        "--reasoning-format",
        "deepseek",  # strips <think> blocks from streamed output
    ]

    if cfg.thinking_mode:
        cmd += ["--reasoning-budget", str(cfg.reasoning_budget)]
    else:
        cmd += ["--reasoning-budget", "0"]

    return cmd


# --- LangChain Factory ---
def create_llm(port: int) -> ChatOpenAI:
    """Return a streaming ChatOpenAI client pointed at the local llama-server.

    Reads generation parameters from the application config.  When
    thinking mode is enabled and ``max_tokens`` is below 4096, the
    token budget is silently raised to 4096 to ensure the model has
    sufficient headroom for chain-of-thought tokens before producing
    the visible answer.

    The request timeout is wired to ``agent.llm_timeout`` to prevent
    indefinite hangs in multi-node agent loops when llama-server stalls.

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
    cfg_agent = get_settings().agent

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
        timeout=cfg_agent.llm_timeout,
    )
