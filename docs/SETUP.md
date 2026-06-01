# Setup Guide

> Environment configuration and development setup for the Sage application.

## Prerequisites

| Requirement | Version |
| --- | --- |
| Python | 3.12 |
| Node.js | 18+ |
| pnpm | 8+ |
| Git | 2.30+ |
| OS | Windows 10/11 |
| RAM | 8 GB minimum, 16 GB recommended |
| Disk | 5 GB available space |

## Installation

### Clone

```bash
git clone https://github.com/ahmadrazacdx/Sage.git
cd Sage
```

### Python Environment

Using [uv](https://github.com/astral-sh/uv):

```bash
uv sync --all-extras
```

Or with standard pip:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Model Downloads

Sage requires three quantized GGUF model files. Download from Hugging Face and place in `artifacts/models/`:

| Model | File | Size | Purpose |
| --- | --- | --- | --- |
| Qwen3.5-2B | `Qwen3.5-2B-Q4_K_M.gguf` | ~1.5 GB | Primary model (CPU builds) |
| Qwen3.5-4B | `Qwen3.5-4B-Q4_K_M.gguf` | ~2.5 GB | Primary model (CUDA builds) |
| Qwen3.5-0.8B | `Qwen3.5-0.8B-Q4_K_M.gguf` | ~0.5 GB | Utility model (memory, compression) |

**Download sources**:

```text
https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main/Qwen3.5-2B-Q4_K_M.gguf
https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q4_K_M.gguf
https://huggingface.co/unsloth/Qwen3.5-0.8B-GGUF/resolve/main/Qwen3.5-0.8B-Q4_K_M.gguf
```

**Expected layout:**

```text
artifacts/models/
├── Qwen3.5-2B-Q4_K_M.gguf
├── Qwen3.5-4B-Q4_K_M.gguf
└── Qwen3.5-0.8B-Q4_K_M.gguf
```

Only the model matching your hardware is required at runtime. CPU-only setups need the 2B and 0.8B models. CUDA setups need the 4B and 0.8B models.

## Server Binaries

Pre-compiled `llama-server` binaries from [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases) must be present:

| Backend | Path | Required Contents |
| --- | --- | --- |
| CPU | `artifacts/servers/cpu/` | `llama-server.exe`, `llama.dll`, `ggml.dll`, `ggml-base.dll`, `libomp140.x86_64.dll` |
| CUDA | `artifacts/servers/cuda/` | All CPU DLLs + `ggml-cuda.dll` |

> [!IMPORTANT]
> Each backend folder must contain the complete contents of a single release zip. Do not mix binaries from different downloads, this causes silent startup crashes.

## Embedding Model

The embedding pipeline uses `BGE-small-en-v1.5` in ONNX format via FastEmbed.

```bash
git clone https://huggingface.co/qdrant/bge-small-en-v1.5-onnx-q artifacts/models/embedding-models/bge-small-en-v1.5
```

Expected path: `artifacts/models/embedding-models/bge-small-en-v1.5/`

## Frontend Build

```bash
cd frontend/artifacts/sage
pnpm install
pnpm build
```

For development with hot reload:

```bash
pnpm dev
```

## Running the Application

### Desktop Mode

```bash
python -m sage
```

Initializes the FastAPI backend, spawns both `llama-server` instances, and launches the `pywebview` native window.

### API / Headless Mode

```bash
python -m sage --dev
```

Starts the FastAPI server on `localhost:8765` without the desktop window. Use this for frontend development against the real backend.

## Verification

After setup, verify the environment:

```bash
# Run the test suite
uv run pytest

# Lint and type check
uv run ruff check src/ tests/
uv run mypy src/

# Start the application
python -m sage
```

The application should detect your hardware, load the appropriate model, and open a native window. Check the terminal for `llama_server_ready` log output to confirm successful initialization.
