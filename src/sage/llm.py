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
import sys
import threading
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
_HEALTH_POLL_INTERVAL_S: float = 0.2
_SERVER_STARTUP_TIMEOUT_S: float = 180.0

_WIN_NVIDIA_SMI: Path = Path("C:/Windows/System32/nvidia-smi.exe")

# GPU layer offload thresholds (VRAM in MB).
_VRAM_FULL_OFFLOAD_MB: int = 4_000
_VRAM_PARTIAL_OFFLOAD_MB: int = 2_000
_VRAM_PARTIAL_LAYERS: int = 24
_GPU_ALL_LAYERS: int = -1  # llama.cpp sentinel: offload every layer to GPU

# ---- context-size thresholds (CPU, based on available RAM) ----
_AVAIL_5GB_MB: int = 5_000   # comfortable: ctx=32K
_AVAIL_4GB_MB: int = 4_000   # adequate: ctx=16K
_AVAIL_3_5GB_MB: int = 3_500 # tight: ctx=8K
_AVAIL_3GB_MB: int = 3_000   # minimum: ctx=4K
# Below 3 GB available: ctx=3K, mmap paging is expected

_CTX_64K: int = 65_536
_CTX_32K: int = 32_768
_CTX_16K: int = 16_384
_CTX_8K: int = 8_192
_CTX_4K: int = 4_096
_CTX_3K: int = 3_072

# GPU VRAM thresholds for context scaling (MB).
_VRAM_12GB_MB: int = 12_000
_VRAM_8GB_MB: int = 8_000
_VRAM_6GB_MB: int = 6_000
_VRAM_4GB_MB: int = 4_000
_CTX_VRAM_LOW: int = 3_072

_STDERR_KEEP_BYTES: int = 8_192


# --- GPU Detection ---
def detect_gpu() -> dict[str, Any]:
    """Probe the host machine for an available GPU accelerator.

    Tries CUDA (NVIDIA) first, then falls back to CPU.  The function
    never raises. All subprocess failures are caught and logged.

    Returns:
        A dict with keys `backend` ("cuda" or "cpu"), `vram_mb` (int),
        and `gpu_name` (str | None).

    """
    # --- CUDA probe ---
    nvidia_smi: str | None = shutil.which("nvidia-smi")
    if nvidia_smi is None and sys.platform == "win32" and _WIN_NVIDIA_SMI.exists():
        nvidia_smi = str(_WIN_NVIDIA_SMI)
        log.debug("nvidia_smi_found_via_system32_fallback", path=nvidia_smi)

    if nvidia_smi is not None:
        try:
            result = subprocess.run(
                [nvidia_smi, "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                first_line = result.stdout.strip().splitlines()[0]
                parts = first_line.split(",", 1)
                if len(parts) == 2:
                    gpu_name = parts[0].strip()
                    vram_mb = int(float(parts[1].strip()))
                    log.info("gpu_detected", backend="cuda",
                             gpu_name=gpu_name, vram_mb=vram_mb)
                    return {"backend": "cuda", "vram_mb": vram_mb, "gpu_name": gpu_name}
            else:
                log.warning("nvidia_smi_no_data", returncode=result.returncode,
                            stderr=result.stderr.strip()[:300])
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            log.warning("gpu_detection_cuda_failed", error=str(exc))
    else:
        log.info("nvidia_smi_not_found",
                 hint="No NVIDIA GPU or drivers not installed; running CPU-only.")


    # CPU fallback
    log.info("gpu_detection_result", backend="cpu")
    return {"backend": "cpu", "vram_mb": 0, "gpu_name": None}


# --- Binary Selection ---
_BASE_COMPANION_DLLS: tuple[str, ...] = (
    "llama.dll", "ggml.dll", "ggml-base.dll", "libomp140.x86_64.dll"
)
_CUDA_COMPANION_DLLS: tuple[str, ...] = ("ggml-cuda.dll",)


def _binary_installation_ok(binary: Path, backend: str = "cpu") -> bool:
    """Return True if the binary and its essential companion DLLs all exist."""
    if not binary.exists():
        log.warning("binary_not_found", binary=str(binary), backend=backend)
        return False

    bin_dir = binary.parent
    dlls_to_check = _BASE_COMPANION_DLLS
    if backend == "cuda":
        dlls_to_check = _BASE_COMPANION_DLLS + _CUDA_COMPANION_DLLS

    for dll in dlls_to_check:
        if not (bin_dir / dll).exists():
            log.warning(
                "binary_missing_dll", binary=str(binary), missing_dll=dll,
                backend=backend,
                hint=(
                    f"Incomplete {backend.upper()} installation in {bin_dir}. "
                    "Each backend folder must contain the COMPLETE contents of "
                    "one release zip — do NOT mix exe from one zip with DLLs from another. "
                    f"Download llama-bXXXX-bin-win-"
                    f"{'cuda-cu12.X.X' if backend == 'cuda' else 'noavx'}-x64.zip "
                    "from https://github.com/ggml-org/llama.cpp/releases "
                    f"and extract ALL files into {bin_dir}."
                ),
            )
            return False
    return True


def _resolve_binary(gpu_info: dict[str, Any], cfg: LLMSettings) -> tuple[Path, str]:
    """Select the llama-server binary matching the detected backend.

    Returns `(binary_path, effective_backend)`.  Falls back to CPU if the
    CUDA installation is incomplete.
    """
    backend: str = gpu_info["backend"]

    if backend == "cuda":
        cuda_bin: Path = cfg.llama_cpp_cuda_bin
        if _binary_installation_ok(cuda_bin, backend="cuda"):
            log.info("binary_selected", backend="cuda", binary=str(cuda_bin))
            return cuda_bin, "cuda"
        log.warning(
            "cuda_binary_incomplete", path=str(cuda_bin), fallback="cpu",
            hint=(
                "The CUDA llama-server installation is incomplete or uses the wrong binary. "
                "Required layout inside artifacts/servers/cuda/:\n"
                "  llama-server.exe  ← from the CUDA zip (NOT the CPU zip)\n"
                "  llama.dll, ggml.dll, ggml-base.dll  ← base DLLs\n"
                "  ggml-cuda.dll  ← CUDA plugin (only in the CUDA zip)\n"
                "Download the cuda-cu12.X.X zip from "
                "https://github.com/ggml-org/llama.cpp/releases and extract "
                "ALL files into artifacts/servers/cuda/ without mixing zips."
            ),
        )

    cpu_bin: Path = cfg.llama_cpp_cpu_bin
    if not _binary_installation_ok(cpu_bin, backend="cpu"):
        raise FileNotFoundError(
            f"CPU llama-server installation incomplete at: {cpu_bin.parent}\n"
            f"Need {cpu_bin.name} + {', '.join(_BASE_COMPANION_DLLS)}\n"
            "Download llama-bXXXX-bin-win-noavx-x64.zip from "
            "https://github.com/ggml-org/llama.cpp/releases "
            "and extract ALL files into artifacts/servers/cpu/."
        )
    log.info("binary_selected", backend="cpu", binary=str(cpu_bin))
    return cpu_bin, "cpu"


# --- Hardware Resolution ---
def _resolve_gpu_layers(effective_backend: str, vram_mb: int, cfg: LLMSettings) -> int:
    """Return the value for `--n-gpu-layers` based on hardware and config."""
    if cfg.gpu_layers != "auto":
        return int(cfg.gpu_layers)
    if effective_backend == "cpu":
        return 0

    # CUDA: VRAM is known; use it to determine offload depth.
    if vram_mb >= _VRAM_FULL_OFFLOAD_MB:
        return _GPU_ALL_LAYERS
    if vram_mb >= _VRAM_PARTIAL_OFFLOAD_MB:
        return _VRAM_PARTIAL_LAYERS
    return 0


def _resolve_context_size(
    effective_backend: str, vram_mb: int, cfg: LLMSettings
) -> int:
    """Return the value for `--ctx-size` based on hardware and config."""
    if cfg.context_window != "auto":
        return int(cfg.context_window)

    if effective_backend == "cpu":
        available_mb: int = psutil.virtual_memory().available // (1024 * 1024)
        try:
            model_size_mb = cfg.active_model_path.stat().st_size // (1024 * 1024)
        except OSError:
            model_size_mb = 2860

        size_diff = 2860 - model_size_mb

        # Absolute minimum floors to ensure OS and apps overhead is covered.
        t_32k = max(_AVAIL_5GB_MB - size_diff, 4_500)
        t_16k = max(_AVAIL_4GB_MB - size_diff, 3_500)
        t_8k  = max(_AVAIL_3_5GB_MB - size_diff, 3_000)
        t_4k  = max(_AVAIL_3GB_MB - size_diff, 2_000)

        log.info("context_size_resolution", available_ram_mb=available_mb,
                 model_size_mb=model_size_mb, backend="cpu")

        if available_mb >= t_32k:
            return _CTX_32K
        if available_mb >= t_16k:
            return _CTX_16K
        if available_mb >= t_8k:
            return _CTX_8K
        if available_mb >= t_4k:
            return _CTX_4K
        return _CTX_3K

    # GPU path — scale by VRAM.
    if vram_mb >= _VRAM_12GB_MB:
        return _CTX_64K
    if vram_mb >= _VRAM_8GB_MB:
        return _CTX_32K
    if vram_mb >= _VRAM_6GB_MB:
        return _CTX_16K
    if vram_mb >= _VRAM_4GB_MB:
        return _CTX_8K
    return _CTX_VRAM_LOW


# --- Thread Count ---
def _resolve_thread_count() -> tuple[int, int]:
    """Return (generation_threads, batch_threads)."""
    physical: int | None = psutil.cpu_count(logical=False)
    logical: int | None = psutil.cpu_count(logical=True)

    if physical is None:
        physical = 4
    if logical is None:
        logical = physical

    hyperthreading_ratio = logical / physical if physical > 0 else 1
    is_likely_hybrid = physical >= 8 and hyperthreading_ratio < 2.0

    gen_threads = max(physical // 2, 1) if is_likely_hybrid else physical

    batch_threads = physical

    log.info("thread_resolution", physical_cores=physical, logical_cores=logical,
             is_likely_hybrid=is_likely_hybrid, generation_threads=gen_threads,
             batch_threads=batch_threads)
    return gen_threads, batch_threads


# --- Orphaned Process Cleanup ---
def _kill_orphaned_servers(model_path: Path) -> None:
    """Kill stale llama-server processes left over.

    This function uses `psutil` to find any `llama-server` process
    whose command line references the same model file we are about to
    load, and kills it before we spawn a fresh instance.

    Args:
        model_path: Path to the model file being used. Only servers
            loading this specific model are terminated.
    """
    model_name = model_path.name
    killed: list[int] = []

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pname = (proc.info["name"] or "").lower()
            if "llama-server" not in pname:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if model_name in cmdline:
                proc.kill()
                killed.append(proc.pid)
                log.info("orphaned_server_killed", pid=proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if killed:
        time.sleep(1.5)
        log.info("orphaned_servers_cleaned", count=len(killed))


# --- Port Allocation ---
def _find_free_port() -> int:
    """Return an OS-assigned free TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_LLAMA_SERVER_HOST, 0))
        return int(sock.getsockname()[1])


# --- Stderr Drain ---
def _start_stderr_drain(proc: subprocess.Popen[bytes]) -> list[bytes]:
    """Spawn a background thread that continuously reads from proc.stderr.

    llama-server can write hundreds of lines during model load (CUDA device
    enumeration, layer-by-layer offload progress, etc.).  On Windows the
    default pipe buffer is 64 KB; without a drain thread the server blocks
    on its own write before the model is fully loaded, causing startup to
    hang indefinitely regardless of timeout length.

    The drained lines are stored in the returned list so that the crash
    reporter in :func:`_wait_for_server` can include recent stderr output
    when the process exits unexpectedly.

    Returns:
        A mutable list; the background thread appends `bytes` lines to it
        as they arrive.  The caller should read it only after the process
        has exited to avoid races.
    """
    lines: list[bytes] = []

    def _drain() -> None:
        if proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                # Keep a rolling window of recent output for crash reporting.
                lines.append(raw)
                total = sum(len(b) for b in lines)
                while total > _STDERR_KEEP_BYTES and lines:
                    total -= len(lines.pop(0))
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=_drain, daemon=True, name="llama-stderr-drain")
    thread.start()
    return lines


# --- Health Check ---
def _wait_for_server(
    port: int,
    proc: subprocess.Popen[bytes],
    stderr_lines: list[bytes],
    timeout_s: float = _SERVER_STARTUP_TIMEOUT_S,
) -> None:
    """Block until llama-server reports healthy or the timeout expires.

    Polls `GET /health` at a fixed interval.  Detects early crashes
    by checking `proc.poll()` on each iteration, if the process has
    exited, the collected stderr output is included in the error message.

    Args:
        port: The TCP port the server is expected to listen on.
        proc: The running server process.
        stderr_lines: Lines collected by :func:`_start_stderr_drain`.
            Used to build the error message on early crash.
        timeout_s: Maximum seconds to wait for a healthy response.
    """
    url = f"http://{_LLAMA_SERVER_HOST}:{port}/health"
    deadline = time.monotonic() + timeout_s
    last_progress_log = time.monotonic()

    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            # Give the drain thread a moment to flush remaining lines.
            time.sleep(0.1)
            stderr = b"".join(stderr_lines).decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(
                f"llama-server exited immediately with code {rc}.\n"
                f"stderr (last 2 KB):\n{stderr or '(no output captured)'}\n\n"
                "Common causes:\n"
                "- Wrong binary for backend (CPU exe in CUDA folder)\n"
                "- Missing DLL (run the DLL check in _binary_installation_ok)\n"
                "- Unsupported CLI flag on this llama.cpp build version\n"
                "- Model file corrupted or wrong architecture"
            )

        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                if resp.status == 200:
                    log.info("llama_server_healthy", port=port)
                    return
        except (urllib.error.URLError, OSError):
            pass

        now = time.monotonic()
        if now - last_progress_log >= 10.0:
            elapsed = now - (deadline - timeout_s)
            log.info("llama_server_loading", elapsed_s=round(elapsed),
                     timeout_s=round(timeout_s), port=port)
            last_progress_log = now

        time.sleep(_HEALTH_POLL_INTERVAL_S)

    raise RuntimeError(
        f"llama-server did not become healthy within {timeout_s:.0f}s on port {port}. "
        "Verify that the binary path is correct and the model file is not corrupted. "
        "On slow storage (HDD/SATA SSD), increase llm.startup_timeout in config."
    )


def _warmup_server(port: int) -> None:
    """Send a single-token completion to trigger GGML graph JIT compilation."""

    url = f"http://{_LLAMA_SERVER_HOST}:{port}/v1/completions"
    payload = json.dumps({"prompt": ".", "max_tokens": 1}).encode()
    req = urllib.request.Request(  # noqa: S310
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
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

    Performs hardware detection, resolves the appropriate binary,
    GPU layer count, and context size, then starts the llama-server
    subprocess on a dynamically allocated loopback port.  The call
    blocks until the server passes its health check and a JIT warm-up
    request completes. Teardown is registered on two channels so the
    subprocess is never orphaned:

    - `atexit`: runs when the Python interpreter exits normally.
    - `SIGTERM` handler: runs when the process receives SIGTERM.

    Returns:
        A 3-tuple of:

        - `proc` (subprocess.Popen): The running server process.
        - `port` (int): The loopback port the server is listening on.
        - `gpu_info` (dict): Hardware detection result with keys
          `backend`, `vram_mb`, and `gpu_name`.  The ``backend`` key
          reflects the *effective* backend (i.e. "cpu" if a CUDA GPU
          was detected but the CUDA binary was unavailable).
    """
    cfg = get_settings().llm

    # Detect hardware.
    gpu_info = detect_gpu()

    binary, effective_backend = _resolve_binary(gpu_info, cfg)
    gpu_info = {**gpu_info, "backend": effective_backend}

    if effective_backend == "cpu":
        cfg.active_model_path = cfg.model_path_cpu
        cfg.active_model_name = cfg.model_name_cpu
    else:
        cfg.active_model_path = cfg.model_path_cuda
        cfg.active_model_name = cfg.model_name_cuda

    _validate_paths(cfg, binary)

    # 3. Kill any stale llama-server instances loading the same model.
    _kill_orphaned_servers(cfg.active_model_path)

    # 4. Resolve hardware parameters using the EFFECTIVE backend.
    vram_mb: int = gpu_info["vram_mb"]
    gpu_layers = _resolve_gpu_layers(effective_backend, vram_mb, cfg)
    ctx_size = _resolve_context_size(effective_backend, vram_mb, cfg)
    gen_threads, batch_threads = _resolve_thread_count()
    port = _find_free_port()

    log.info(
        "llama_server_starting", backend=effective_backend,
        gpu_name=gpu_info["gpu_name"], vram_mb=vram_mb, gpu_layers=gpu_layers,
        ctx_size=ctx_size, gen_threads=gen_threads, batch_threads=batch_threads,
        port=port, binary=binary.name, model=cfg.active_model_path.name,
        available_ram_mb=psutil.virtual_memory().available // (1024 * 1024),
    )

    _win_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    proc = subprocess.Popen(
        _build_cmd(binary, cfg, port, gpu_layers, ctx_size,
                   gen_threads, batch_threads, effective_backend),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_win_flags,
    )

    stderr_lines = _start_stderr_drain(proc)

    atexit.register(_terminate_process, proc)

    def _sigterm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        """Ensure llama-server is terminated when the parent receives SIGTERM."""
        _terminate_process(proc)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    _wait_for_server(port, proc, stderr_lines, timeout_s=cfg.startup_timeout)
    _warmup_server(port)

    log.info("llama_server_ready", backend=effective_backend,
             ctx_size=ctx_size, gpu_layers=gpu_layers, port=port)
    return proc, port, gpu_info


def _validate_paths(cfg: LLMSettings, binary: Path) -> None:
    """Raise FileNotFoundError early if the binary or model path is missing."""
    if not binary.exists():
        raise FileNotFoundError(
            f"llama-server binary not found at: {binary}\n"
            "Download a pre-built release from https://github.com/ggml-org/llama.cpp/releases "
            "and place it at the configured path."
        )
    if not cfg.active_model_path.exists():
        raise FileNotFoundError(
            f"GGUF model file not found at: {cfg.active_model_path}\n"
            "Ensure the quantized model is placed at the path configured under [llm] model_path_cpu or model_path_cuda."
        )


def _build_cmd(
    binary: Path,
    cfg: LLMSettings,
    port: int,
    gpu_layers: int,
    ctx_size: int,
    gen_threads: int,
    batch_threads: int,
    effective_backend: str,
) -> list[str]:
    """Assemble the llama-server argv list from resolved hardware parameters.

    Args:
        effective_backend: The backend that will actually run ("cuda" or
            "cpu").  Controls GPU-specific flags like "--flash-attn"
            and "--no-mmap".
    """

    is_cpu = effective_backend == "cpu" or gpu_layers == 0

    if is_cpu:
        cmd = [
            str(binary),
            "--model", str(cfg.active_model_path),
            "--host", _LLAMA_SERVER_HOST,
            "--port", str(port),
            "--threads", "7",
            "--threads-batch", "12",
            "--batch-size", "1024",
            "--ubatch-size", "228",
            "--ctx-size", str(ctx_size),
            "--n-gpu-layers", "0",
            "--parallel", "4",
            "--cache-reuse", "32",
            "--cont-batching",
            "--cache-type-k", cfg.cache_type_k,
            "--cache-type-v", cfg.cache_type_v,
            "--jinja",
            "--reasoning-budget", str(cfg.reasoning_budget),
            "--reasoning-format", "none",
        ]
    else:
        cmd = [
            str(binary),
            "--model", str(cfg.active_model_path),
            "--host", _LLAMA_SERVER_HOST,
            "--port", str(port),
            "--ctx-size", str(ctx_size),
            "--n-gpu-layers", str(gpu_layers),
            "--threads", str(gen_threads),
            "--threads-batch", str(batch_threads),
            "--batch-size", "512",
            "--ubatch-size", "512",
            "--cache-type-k", cfg.cache_type_k,
            "--cache-type-v", cfg.cache_type_v,
            "--cont-batching",
            "--jinja",
            "--reasoning-budget", str(cfg.reasoning_budget),
            "--reasoning-format", "none",
        ]
        cmd.extend(["--flash-attn", "auto"])

    return cmd


# --- LangChain Factory ---
def create_llm(port: int) -> ChatOpenAI:
    """Return a ChatOpenAI client pointed at the local llama-server.

    Reads generation parameters from the application config.  When
    thinking mode is enabled and `max_tokens` is below 4096, the
    token budget is silently raised to 4096 to ensure the model has
    sufficient headroom for chain-of-thought tokens before producing
    the visible answer.

    The request timeout is wired to `agent.llm_timeout` to prevent
    indefinite hangs in multi-node agent loops when llama-server stalls.

    This function is stateless and may be called multiple times.  Each
    call returns an independent `ChatOpenAI` instance sharing the
    same underlying server.

    Args:
        port: The TCP port returned by `start_llm_server()`.

    Returns:
        A `ChatOpenAI` instance with `streaming=True` configured
        for use with `astream_events`.
    """
    cfg = get_settings().llm
    cfg_agent = get_settings().agent

    max_tok = cfg.max_tokens
    if cfg.thinking_mode and max_tok < 4096:
        max_tok = 4096
        log.debug("thinking_mode_token_budget_raised",
                  configured=cfg.max_tokens, effective=max_tok)

    llm = ChatOpenAI(
        base_url=f"http://{_LLAMA_SERVER_HOST}:{port}/v1",
        api_key="local",
        model=cfg.active_model_name,
        temperature=cfg.temperature,
        max_tokens=max_tok,
        streaming=True,
        timeout=cfg_agent.llm_timeout,
    )

    return llm.bind(
        extra_body={
            "chat_template_kwargs": {"enable_thinking": False},
        }
    )

def _with_thinking(llm: ChatOpenAI, budget: int) -> ChatOpenAI:
    """Return a new LLM binding with thinking enabled.
 
    Overrides the `enable_thinking: False` default set in `create_llm()`.
    `budget` caps how many tokens the model spends inside zimmerman; the
    server's `--reasoning-budget` sets the hard ceiling above this value.
 
    Only call this in agent nodes that explicitly need native CoT.
    """
    return llm.bind(
        extra_body={
            "chat_template_kwargs": {"enable_thinking": True},
            "thinking_budget": budget,
        }
    )