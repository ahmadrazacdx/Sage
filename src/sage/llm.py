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

# --- GPU layer offload thresholds (VRAM in MB) ---
_VRAM_FULL_OFFLOAD_MB: int = 3_500  # ≥3.5 GB → all layers on GPU
_VRAM_PARTIAL_OFFLOAD_MB: int = 2_500  # 2.5–3.5 GB → partial offload
_VRAM_PARTIAL_LAYERS: int = 20
_GPU_ALL_LAYERS: int = -1  # llama.cpp sentinel: offload every layer

# --- CPU context-size thresholds ---
_AVAIL_10GB_MB: int = 10_000  # abundant: ctx=32K
_AVAIL_7GB_MB: int = 7_000  # comfortable: ctx=16K
_AVAIL_5GB_MB: int = 5_000  # adequate: ctx=8K
_AVAIL_3_5GB_MB: int = 3_500  # tight: ctx=4K
# Below 3.5 GB available: ctx=2K

_CTX_32K: int = 32_768
_CTX_16K: int = 16_384
_CTX_8K: int = 8_192
_CTX_4K: int = 4_096
_CTX_2K: int = 2_048

# --- GPU VRAM thresholds for context scaling (MB) ---
# Conservative 10% safety margin applied to each threshold:
_VRAM_8GB_MB: int = 8_000
_VRAM_6GB_MB: int = 5_500  # ≥5.5 GB detected = effectively a 6 GB card
_VRAM_4GB_MB: int = 3_500  # ≥3.5 GB = 4 GB card
_VRAM_3GB_MB: int = 2_800  # ≥2.8 GB = 3 GB card
_CTX_VRAM_8GB: int = 65_536  # 8+ GB: 64K ctx
_CTX_VRAM_6GB: int = 32_768  # 6 GB:  32K ctx
_CTX_VRAM_4GB: int = 16_384  # 4 GB:  16K ctx
_CTX_VRAM_3GB: int = 8_192  # 3 GB:   8K ctx
_CTX_VRAM_LOW: int = 4_096  # <2.8 GB: 4K ctx


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

    # CUDA: VRAM is known.
    # ≥3.5 GB → all 32 layers on GPU.
    # 2.5–3.5 GB → partial: 20/32 layers on GPU to stay under VRAM budget.
    # <2.5 GB → no offload possible without OOM.
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
            model_size_mb = cfg.active_model_path.stat().st_size // (1024 * 1024)
        except OSError:
            model_size_mb = 2_500

        size_diff = 2_500 - model_size_mb

        # Minimum RAM floors ensure OS + process overhead is always covered.
        t_32k = max(_AVAIL_10GB_MB - size_diff, 7_000)
        t_16k = max(_AVAIL_7GB_MB - size_diff, 4_500)
        t_8k = max(_AVAIL_5GB_MB - size_diff, 3_000)
        t_4k = max(_AVAIL_3_5GB_MB - size_diff, 2_000)

        log.info(
            "context_size_resolution",
            available_ram_mb=available_mb,
            model_size_mb=model_size_mb,
            backend="cpu",
        )

        if available_mb >= t_32k:
            return _CTX_32K
        if available_mb >= t_16k:
            return _CTX_16K
        if available_mb >= t_8k:
            return _CTX_8K
        if available_mb >= t_4k:
            return _CTX_4K
        return _CTX_2K

    # GPU path — graduated by VRAM bucket
    if vram >= _VRAM_8GB_MB:
        return _CTX_VRAM_8GB  # 64K
    if vram >= _VRAM_6GB_MB:
        return _CTX_VRAM_6GB  # 32K
    if vram >= _VRAM_4GB_MB:
        return _CTX_VRAM_4GB  # 16K
    if vram >= _VRAM_3GB_MB:
        return _CTX_VRAM_3GB  #  8K
    return _CTX_VRAM_LOW  #  4K


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
    _check_cuda_binary(binary, gpu_info)
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
        model=cfg.active_model_path.name,
        available_ram_mb=psutil.virtual_memory().available // (1024 * 1024),
    )

    proc = subprocess.Popen(
        _build_cmd(
            cfg,
            binary,
            port,
            gpu_layers,
            ctx_size,
            gen_threads,
            batch_threads,
            gpu_info["backend"],
        ),
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
    model: Path = cfg.active_model_path

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


def _check_cuda_binary(binary: Path, gpu_info: dict[str, Any]) -> None:
    """Warn loudly if the binary placed in the cuda/ folder is a CPU-only build.

    Root cause this guards against:
        The llama.cpp release page ships two separate .zip archives:

        - ``llama-bXXXX-bin-win-cpu-x64.zip``   — CPU only. Contains
          ``llama-server.exe`` with **no CUDA DLL imports**.
        - ``llama-bXXXX-bin-win-cuda-cu12.X-x64.zip`` — CUDA-enabled.
          Contains ``llama-server.exe`` **and** ``cublas64_12.dll``,
          ``cublasLt64_12.dll``, ``cudart64_12.dll`` as side-car DLLs.

        If a user copies the CPU ``llama-server.exe`` into the ``cuda/``
        directory (alongside the three CUDA DLLs), the binary will start
        successfully in CPU-only mode despite ``--n-gpu-layers -1``, giving
        0 % GPU utilization with no error message.

    Detection heuristic (Windows PE import table scan):
        Read the raw bytes of the EXE and check for the ASCII substring
        ``cublas`` (present in CUDA builds, absent in CPU builds).  This
        avoids loading the PE parser library ``pefile`` as a dependency.
        False-positive rate: essentially zero (CPU builds never link cuBLAS).

    Raises:
        RuntimeError: If CUDA is the detected backend, the binary lives in a
            ``cuda/`` directory, and no CUDA DLL import evidence is found.
    """
    if gpu_info["backend"] != "cuda":
        return  # Only relevant for CUDA path.

    # Only check binaries that are explicitly placed in a cuda/ subfolder.
    parts = binary.parts
    try:
        servers_idx = next(i for i, p in enumerate(parts) if p.lower() == "servers")
    except StopIteration:
        return  # Non-standard path; skip check.

    current_folder = parts[servers_idx + 1] if servers_idx + 1 < len(parts) else ""
    if current_folder.lower() != "cuda":
        return  # Binary is not in the cuda/ folder; skip check.

    # Scan first 512 KB of the binary for CUDA DLL import evidence.
    try:
        with binary.open("rb") as fh:
            header = fh.read(524_288)  # 512 KB is sufficient for the PE import table
        has_cuda = b"cublas" in header.lower() or b"cudart" in header.lower()
    except OSError as exc:
        log.warning("cuda_binary_check_failed", error=str(exc))
        return

    if not has_cuda:
        raise RuntimeError(
            f"GPU acceleration failure: the binary at\n"
            f"  {binary}\n"
            "appears to be a CPU-only build placed in the cuda/ directory.\n\n"
            "How to fix:\n"
            "  1. Go to https://github.com/ggml-org/llama.cpp/releases\n"
            "  2. Download the CUDA 12.x zip:\n"
            "       llama-bXXXX-bin-win-cuda-cu12.X-x64.zip\n"
            "     (NOT the cpu-only zip)\n"
            "  3. Extract llama-server.exe AND the three .dll files\n"
            "     (cublas64_12.dll, cublasLt64_12.dll, cudart64_12.dll)\n"
            "     into: artifacts/servers/cuda/\n"
            "  4. Restart Sage.\n\n"
            "The CPU-only exe ignores --n-gpu-layers; GPU utilization will be 0 %."
        )

    log.debug("cuda_binary_check_passed", binary=str(binary))


def _build_cmd(
    cfg: LLMSettings,
    binary: Path,
    port: int,
    gpu_layers: int,
    ctx_size: int,
    gen_threads: int,
    batch_threads: int,
    backend: str,
) -> list[str]:
    """Assemble the llama-server argv list from resolved hardware parameters.

    Flag rationale (llama-server b4000+, Jan 2026):

    ``--flash-attn auto``
        Enable Flash Attention when the model architecture supports it.
        ``auto`` = enable if supported, skip silently if not.  This is
        the safest value: Qwen3 supports FA on both CUDA and CPU (via
        llama.cpp's GGML FA kernels).

    ``--cache-type-k/v q4_0``
        Quantise the KV cache to 4-bit.  Reduces KV memory by 4× vs
        f16 with negligible perplexity loss on Qwen3 at ctx ≤ 32K.

    ``--no-mmap`` (GPU path)
        Disable memory-mapped file I/O for the model weights.  With
        mmap the OS may page weight pages out to disk during inference;
        without mmap all weights are loaded into CUDA VRAM upfront.
        On Windows this also avoids anti-virus scanner false-positives
        that can stall mmap reads mid-inference.

    ``--mlock`` (CPU path)
        Pin the model in physical RAM so the OS cannot swap it to the
        page file.  Critical on machines with 8–16 GB RAM where
        background processes compete for memory.  Fails silently if the
        process does not have the SeLockMemoryPrivilege (Windows);
        llama-server logs a warning and continues.

    ``--batch-size 512`` / ``--ubatch-size 256`` (CPU)
    ``--batch-size 2048`` / ``--ubatch-size 512`` (GPU)
        Prompt prefill ("context ingestion") is compute-bound on GPU;
        larger physical batch → higher CUDA tensor core utilisation.
        CPU is memory-bandwidth-bound; smaller ubatch avoids L3
        thrashing during the GEMM reduction.
    """
    is_gpu = backend in ("cuda", "vulkan") and gpu_layers != 0

    if is_gpu:
        # GPU path: large batches saturate CUDA tensor cores.
        batch_size = "2048"
        ubatch_size = "512"
    else:
        # CPU path: small ubatch avoids L3/L4 cache thrashing.
        batch_size = "512"
        ubatch_size = "256"

    cmd = [
        str(binary),
        "--model",
        str(cfg.active_model_path),
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
        batch_size,
        "--ubatch-size",
        ubatch_size,
        "--flash-attn",  # --flash-attn accepts: on | off | auto
        "auto",  # auto = enable when model architecture supports it
        "--cache-type-k",
        cfg.cache_type_k,
        "--cache-type-v",
        cfg.cache_type_v,
        "--cont-batching",  # interleave prompt ingestion with token generation
        "--jinja",  # required for Qwen3 Jinja2 chat template
        "--reasoning-format",
        "deepseek",  # strips <think> blocks from streamed output
    ]

    if is_gpu:
        # --no-mmap: load all weights into CUDA VRAM at startup; eliminates
        # mmap page-fault stalls and PCIe re-read penalties during inference.
        cmd.append("--no-mmap")
    else:
        # --mlock: pin model in physical RAM; prevents OS swap during inference.
        # Fails gracefully on Windows if SeLockMemoryPrivilege is absent.
        cmd.append("--mlock")

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
        model=cfg.active_model_name,
        temperature=cfg.temperature,
        max_tokens=max_tok,
        streaming=True,
        timeout=cfg_agent.llm_timeout,
    )
