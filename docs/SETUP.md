# Setup Guide

This document provides step-by-step instructions for setting up a **Sage** development environment, downloading required model files, configuring the system, and running the application.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Repository Setup](#repository-setup)
- [Python Environment](#python-environment)
- [Model Downloads](#model-downloads)
- [Server Binaries](#server-binaries)
- [Embedding Model](#embedding-model)
- [Frontend Setup](#frontend-setup)
- [Configuration](#configuration)
- [Running the Application](#running-the-application)
- [Verifying the Installation](#verifying-the-installation)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Version | Notes |
| --- | --- | --- |
| Python | 3.12 | 3.13 is not yet supported |
| Node.js | 18+ | Required for frontend builds |
| pnpm | 8+ | Package manager for the frontend monorepo |
| Git | 2.30+ | Large file handling via `.gitattributes` |
| Windows | 10 or 11 | Desktop mode requires pywebview (Windows-only) |
| RAM | 8 GB minimum | 16 GB recommended for CUDA builds |
| Disk | 5 GB free | Models, server binaries, and vector database |

Optional:

| Requirement | Notes |
| --- | --- |
| NVIDIA GPU | CUDA 12.1+ for GPU-accelerated inference |
| Vulkan SDK | Alternative GPU backend via Vulkan |

## Repository Setup

```bash
git clone https://github.com/ahmadrazacdx/Sage.git
cd Sage
```

## Python Environment

### Using uv (Recommended)

```bash
uv sync --all-extras
```

This creates a virtual environment in `.venv/` and installs all dependencies including development extras.

### Using pip

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

### Dependency Groups

The project defines the following optional dependency groups in `pyproject.toml`:

| Group | Contents |
| --- | --- |
| (default) | Core runtime dependencies |
| `dev` | Ruff, mypy, pytest, pre-commit, and other development tools |

## Model Downloads

Sage requires three model files, all in GGUF format:

| Model | File | Size | Purpose |
| --- | --- | --- | --- |
| Qwen3.5-2B (CPU) | `Qwen3.5-2B-Q4_K_M.gguf` | ~1.5 GB | Primary model for CPU-only builds |
| Qwen3.5-4B (CUDA) | `Qwen3.5-4B-Q4_K_M.gguf` | ~2.5 GB | Primary model for GPU builds |
| Qwen3.5-0.8B | `Qwen3.5-0.8B-Q4_K_M.gguf` | ~0.5 GB | Utility model (memory, compression, extraction) |

### Automated Download

```bash
python scripts/download_model.py
```

This script downloads all required models to `artifacts/models/`.

### Manual Download

Download GGUF files from Hugging Face and place them in:

```text
artifacts/models/Qwen3.5-2B-Q4_K_M.gguf
artifacts/models/Qwen3.5-4B-Q4_K_M.gguf
artifacts/models/Qwen3.5-0.8B-Q4_K_M.gguf
```

## Server Binaries

Sage requires llama-server (from llama.cpp) to serve models. Three variants are needed depending on the target hardware:

| Binary | Path | Backend |
| --- | --- | --- |
| CPU | `artifacts/servers/cpu/llama-server.exe` | CPU-only (AVX2) |
| CUDA | `artifacts/servers/cuda/llama-server.exe` | NVIDIA GPU |
| Vulkan | `artifacts/servers/vulkan/llama-server.exe` | Vulkan GPU |

Pre-built binaries can be obtained from the [llama.cpp releases](https://github.com/ggerganov/llama.cpp/releases) page.
Select the appropriate Windows build for your hardware and place the executable in the corresponding directory.

## Embedding Model

The **BGE-small-en-v1.5** embedding model is required for the RAG pipeline, running via **FastEmbed** (ONNX):

```text
artifacts/models/embedding-models/bge-small-en-v1.5/
```

This should be an ONNX-compatible model directory containing `model.onnx` (or `model_optimized.onnx`), `tokenizer.json`, and `config.json`. Using FastEmbed provides significantly faster embedding generation on CPU compared to standard transformers. [Download ONNX version here](https://huggingface.co/qdrant/bge-small-en-v1.5-onnx-q).

## Frontend Setup

The frontend is a pnpm monorepo. From the repository root:

```bash
cd frontend
pnpm install
```

### Development Build

```bash
cd frontend/artifacts/sage
pnpm dev
```

This starts the Vite development server on `http://localhost:5173` with hot module replacement.

### Production Build

```bash
cd frontend/artifacts/sage
pnpm build
```

The production bundle is output to `frontend/artifacts/sage/dist/` and is served automatically by the FastAPI backend.

## Configuration

### Configuration Files

| File | Purpose | Git-tracked |
| --- | --- | --- |
| `config/default.toml` | Shipped defaults | Yes |
| `config/institution.toml` | Institution-specific settings | Yes |

### Key Settings Reference

| Setting | Default | Description |
| --- | --- | --- |
| `llm.context_window` | `auto` | Auto-scaled based on available RAM |
| `llm.gpu_layers` | `auto` | Number of layers offloaded to GPU |
| `llm.temperature` | `0.3` | Generation temperature |
| `llm.max_tokens` | `4096` | Maximum output tokens |
| `llm.thinking_mode` | `true` | Enable chain-of-thought reasoning |
| `llm.reasoning_budget` | `512` | Token budget for thinking |
| `rag.top_k` | `5` | Number of retrieval results |
| `rag.chunk_size` | `512` | Document chunk size in tokens |
| `agent.llm_timeout` | `180` | Agent LLM call timeout in seconds |
| `memory.max_memories` | `1000` | Maximum stored memory facts |
| `network.force_offline` | `false` | Disable all online features |

## Running the Application

### Desktop Mode (default)

```bash
python -m sage
```

Opens a native window using pywebview. The FastAPI backend, LLM servers, and
frontend are all managed automatically.

### Browser Mode

```bash
python -m sage --browser
```

Starts the backend and opens the default browser. No pywebview dependency
required.

### Development Mode

```bash
python -m sage --dev
```

Starts the backend only. Pair with the Vite dev server for frontend
hot-reloading:

```bash
cd frontend/artifacts/sage
pnpm dev
```

The Vite dev server proxies API requests to the backend on port 8765.

## Verifying the Installation

1. Start the application in any mode.
2. Wait for the startup log message `app_startup_complete`.
3. Open the application (desktop window or browser).
4. The status indicator should show the model name and "Ready" state.
5. Send a test message in any mode (for example, "Explain binary search").
6. Verify that a response is generated and streamed to the interface.

### Health Check

```bash
curl http://localhost:8765/api/healthz
```

Expected response: `{"status": "ok"}`

### System Status

```bash
curl http://localhost:8765/api/status
```

Returns model readiness, LLM port, embedding model status, and network state.

## Troubleshooting

### Model server fails to start

- Verify that the model file exists at the configured path.
- Verify that the server binary is present and matches your hardware (CPU,
  CUDA, or Vulkan).
- Check that no other process is occupying the configured port.
- Review the startup logs for specific error messages.

### Out of memory during startup

- Reduce `context_window` in the configuration to a fixed value (for example,
  2048 or 4096).
- On CPU builds, ensure at least 8 GB of RAM is available.
- Close other memory-intensive applications before starting Sage.

### Frontend shows "Model not ready"

- The LLM server takes 30 to 180 seconds to load, depending on hardware. Wait
  for the `app_startup_complete` log message.
- If the message does not appear, check the backend logs for startup errors.

### ChromaDB or embedding errors

- Verify that the embedding model directory contains all required files.
- Delete the `artifacts/data/databases/vectordb/` directory to reset the vector
  store.
