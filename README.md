Sage is a desktop application that runs large language models locally to provide students with an intelligent study companion. It operates entirely offline after initial setup, ensuring data privacy and removing dependency on cloud services.

> Final Year Project, BS Software Engineering (Session 2022-2026)
> Department of Computer Science, Thal University Bhakkar
> Authors: Ahmad Raza, Abdullah Khan

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [System Architecture](#system-architecture)
- [Technology Stack](#technology-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [Agent Flows](#agent-flows)
- [Development](#development)
- [Deployment](#deployment)
- [Institutional Customization](#institutional-customization)
- [Limitations and Future Work](#limitations-and-future-work)
- [License](#license)
- [References](#references)

---

## Overview

Students in resource-constrained institutions often lack access to reliable internet or cloud-based AI tools. **Sage** addresses this gap by packaging a complete AI assistant into a standalone Windows desktop application. It uses quantized open-weight language models, a hybrid retrieval-augmented generation (RAG) pipeline, and a multi-agent orchestration framework to deliver seven distinct academic workflows, all running on consumer-grade hardware without requiring a GPU.

The system employs a hierarchical model strategy: a primary model (`2B` parameters on `CPU`, `4B` on `CUDA`) handles complex reasoning and generation, while a secondary `0.8B` utility model offloads structured extraction and memory management tasks.
This separation allows concurrent processing and reduces latency on CPU-only deployments.

## Features

| Capability | Description |
| --- | --- |
| **Explain** | Context-aware explanations with cited references from ingested course materials |
| **Quiz** | Adaptive quiz generation with structured evaluation and per-question feedback |
| **Diagram** | Mermaid diagram generation with syntax validation and SVG rendering |
| **Roadmap** | Personalized day-by-day study plans with prerequisite sequencing |
| **Research** | Multi-source academic research with arXiv, web, and Wikipedia integration |
| **Code Fix** | Code diagnosis, automated repair, sandboxed verification, and explanation |
| **Thinking** | Extended chain-of-thought reasoning with visible thought process |

**Additional capabilities:**

- Offline-first execution with optional online mode for research
- Hybrid RAG pipeline combining dense vector retrieval and sparse BM25 search
- Long-term semantic memory with automatic fact extraction and deduplication
- Context-aware history compression using sliding-window summarization
- PDF and Markdown export for research reports and study materials
- Sandboxed Python code execution with figure generation
- Desktop application with system tray integration via pywebview
- Windows installer with NSIS packaging and automated build pipeline
- CI/CD pipeline with linting, type checking, security scanning, and release automation

## System Architecture

Sage follows a modular pipeline architecture built on LangGraph for agent orchestration. A detailed breakdown is available [here](docs/ARCHITECTURE.md).

### High-Level Data Flow

```text
User Input
    |
    v
[Router] -- intent classification --> [Retrieval] -- RAG context -->
    |                                      |
    v                                      v
[General / Reasoning / Quiz / Diagram / Planner / Research / CodeFix]
    |
    v
[Response Formatter] --> SSE Stream --> Frontend
```

### Hierarchical Model Strategy

#### Primary LLM (2B CPU / 4B CUDA)

- Reasoning, generation, report writing, schedule planning
- Creative and complex structured output

#### Utility LLM (0.8B, CPU-only offload)

- Memory extraction and history compression
- Diagnosis and analysis steps in agent flows
- Quiz evaluation and diagram validation

## Technology Stack

| Layer | Technology |
| --- | --- |
| Language | `Python` 3.12+ |
| LLM Inference | `llama.cpp` (llama-server), GGUF quantized models |
| Agent Framework | `LangGraph` (stateful multi-node graph orchestration) |
| LLM Client | `LangChain` (ChatOpenAI interface to local server) |
| RAG | `ChromaDB` (vector store), `FastEmbed` (BGE-small-en-v1.5 ONNX), `BM25` (sparse) |
| API Server | `FastAPI`, `Uvicorn`, `Server-Sent Events` |
| Database | `SQLite` with WAL mode, `aiosqlite` |
| Frontend | `React`, `TypeScript`, `Vite` |
| Desktop | `pywebview` (system webview), `pystray` (system tray) |
| Installer | `NSIS` (Windows installer packaging) |
| CI/CD | `GitHub Actions` (lint, test, build, release, security scan) |
| Code Quality | `Ruff` (linting/formatting), `mypy` (type checking), `pre-commit hooks` |
| Configuration | `TOML`-based with `Pydantic` settings validation |
| Logging | `structlog` (structured JSON logging) |

## Project Structure

```text
📦 sage/
├── 📂 config/                     # TOML configuration files
│   └── 📄 default.toml            # Default system configuration
├── 📂 docs/                       # Technical documentation
├── 📂 frontend/                   # React/TypeScript SPA (pnpm monorepo)
│   ├── 📂 artifacts/sage/         # Vite application
│   └── 📂 lib/                    # Shared libraries (API client, DB, Zod schemas)
├── 📂 installer/                  # NSIS installer scripts and staging
├── 📂 scripts/                    # Utility scripts (model download, ingestion, benchmark)
├── 📂 src/sage/                   # Python package root
│   ├── 📂 agents/                 # LangGraph agent nodes
│   │   ├── 📄 graph.py            # Graph assembly and compilation
│   │   ├── 📄 router.py           # Intent classification
│   │   ├── 📄 retrieval.py        # RAG retrieval node
│   │   ├── 📄 reasoning.py        # Chain-of-thought reasoning
│   │   ├── 📄 general.py          # General conversation
│   │   ├── 📄 quiz.py             # Quiz generation and evaluation
│   │   ├── 📄 diagram.py          # Mermaid diagram pipeline
│   │   ├── 📄 planner.py          # Study roadmap generation
│   │   ├── 📄 research.py         # Multi-source research pipeline
│   │   ├── 📄 code_fix.py         # Code diagnosis and repair
│   │   ├── 📄 response.py         # Citation formatting
│   │   └── 📄 state.py            # AgentState TypedDict
│   ├── 📂 routers/                # FastAPI endpoint modules
│   │   ├── 📄 chat.py             # Chat submission and SSE streaming
│   │   ├── 📄 sessions.py         # Conversation session management
│   │   ├── 📄 documents.py        # Document upload and management
│   │   └── 📄 system.py           # Health and status endpoints
│   ├── 📂 rag/                    # Retrieval-augmented generation
│   ├── 📂 tools/                  # LangChain tool implementations
│   │   ├── 📄 search.py           # arXiv, web, Wikipedia search
│   │   ├── 📄 sandbox.py          # Sandboxed Python execution
│   │   ├── 📄 mermaid.py          # Mermaid validation and rendering
│   │   ├── 📄 calculator.py       # Mathematical computation
│   │   └── 📄 export.py           # PDF and Markdown export
│   ├── 📄 app.py                  # FastAPI application factory
│   ├── 📄 config.py               # Pydantic settings with TOML loading
│   ├── 📄 database.py             # SQLite schema and queries
│   ├── 📄 desktop.py              # pywebview and system tray integration
│   ├── 📄 embedding.py            # Embedding model management
│   ├── 📄 llm.py                  # LLM server lifecycle management
│   ├── 📄 memory.py               # Semantic memory extraction and compression
│   ├── 📄 network.py              # Network connectivity monitoring
│   ├── 📄 prompts.py              # All prompt templates
│   └── 📄 utils.py                # Shared utilities
├── 📂 tests/                      # Test suite
├── 📂 .github/workflows/          # CI/CD pipeline definitions
├── 📄 build.ps1                   # Windows build orchestration script
└── 📄 pyproject.toml              # Project metadata and dependencies
```

## Getting Started

### Prerequisites

- Python >=3.12
- Node.js 18+ and pnpm (for frontend development)
- Windows 10/11 (desktop mode requires pywebview)
- 8 GB RAM minimum (16 GB recommended for CUDA builds)

### Installation

```bash
# Clone the repository
git clone https://github.com/ahmadrazacdx/Sage.git
cd Sage

# Create a virtual environment and install dependencies
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

#Using uv
uv sync --all-extras
```

**Download the required model files:**

```bash
python scripts/download_model.py
```

This places GGUF model files and the embedding model into `artifacts/models/`.

**Build the frontend (production):**

```bash
cd frontend/artifacts/sage
pnpm install
pnpm build
cd ../../..
```

### Running Sage

**Desktop mode** (default, pywebview window):

```bash
python -m sage
```

**Browser mode** (backend + auto-open browser tab):

```bash
python -m sage --browser
```

**Development mode** (backend only, use with Vite dev server):

```bash
python -m sage --dev
```

In a separate terminal for frontend hot-reload:

```bash
cd frontend/artifacts/sage
pnpm dev
```

## Configuration

Sage uses a TOML configuration system. The default configuration is in `config/default.toml`. Refer to [docs/SETUP.md](docs/SETUP.md) for a detailed configuration reference.

## Agent Flows

All agent flows are orchestrated as a `LangGraph` state graph. The router node classifies user intent and dispatches to the appropriate pipeline.

| Flow | Pipeline Stages |
| --- | --- |
| Explain | Router, Retrieval, Reasoning, Response Formatter |
| Quiz | Router, Retrieval, Quiz Generation or Evaluation |
| Diagram | Router, Retrieval, Description, Mermaid Generation, Validation, SVG Render |
| Roadmap | Router, Analysis, Schedule Generation |
| Research | Router, Plan, Parallel Search, Digest, Report Writing, Review |
| Code Fix | Router, Diagnosis, Fix Generation, Sandbox Verification, Explanation |
| Thinking | Router, Retrieval, Extended Reasoning (visible thought chain) |
| General | Router, Direct LLM Response |

> On CPU-only builds, the utility model (0.8B) handles the structured extraction steps (diagnosis, analysis, description, evaluation, digest) to reduce latency, while the primary model handles all generation and reasoning steps.

## Development

### Code Quality

```bash
# Lint and format
ruff check src/ tests/
ruff format src/ tests/

# Type checking
mypy src/

# Run tests
python -m pytest tests/

# Pre-commit hooks (runs automatically on commit)
pre-commit run --all-files
```

### CI/CD Pipeline

The project uses GitHub Actions with the following workflows:

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `ci.yml` | Push, Pull Request | Linting, type checking, unit tests |
| `build-test.yml` | Push, Pull Request | Build verification |
| `security.yml` | Push, Schedule | Dependency vulnerability scanning |
| `release.yml` | Tag push | Build installer, create GitHub Release |

Refer to [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for development guidelines.

## Deployment

Sage is distributed as a Windows installer built with NSIS. The build pipeline
is orchestrated by `build.ps1`, which:

1. Builds the frontend production bundle
2. Packages the Python application
3. Stages all artifacts (models, servers, configuration)
4. Compiles the NSIS installer

Refer to [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the complete build and distribution process.

## Institutional Customization

Sage can be adapted for any educational institution. The primary customization
points are:

- **Curriculum ingestion**: ingest course materials via `scripts/ingest.py` into the RAG vector store
- **Configuration overrides**: institution-specific settings in `config/institution.toml`
- **Network policy**: enable or disable online research via the `[network].force_offline` flag

Refer to [docs/INSTITUTION_GUIDE.md](docs/INSTITUTION_GUIDE.md) for a complete customization guide.

## Limitations and Future Work

### Current Limitations

- Windows-only for the desktop application (the backend is cross-platform)
- Model quality is constrained by the parameter count of models that fit
  in consumer-grade RAM (2B to 4B parameters)
- RAG retrieval quality depends on the volume and format of ingested materials
- Research mode requires an active internet connection
- No multi-user or authentication support (single-user desktop application)

### Future Improvements

- Cross-platform desktop support (macOS, Linux)
- Larger model support for systems with dedicated GPUs (7B+ parameters)
- Collaborative features for classroom and instructor use
- Automated curriculum ingestion from institutional LMS platforms
- Voice input and accessibility enhancements
- Mobile companion application

## License

This project is licensed under the Apache License 2.0. See [LICENSE.md](LICENSE.md) for the full license text.

Copyright 2026 Ahmad Raza.

## References

1. LangGraph Documentation. <https://langchain-ai.github.io/langgraph/>
2. LangChain Documentation. <https://python.langchain.com/>
3. llama.cpp. <https://github.com/ggerganov/llama.cpp>
4. ChromaDB. <https://docs.trychroma.com/>
5. FastEmbed (Qdrant). <https://github.com/qdrant/fastembed>
6. FastAPI. <https://fastapi.tiangolo.com/>
7. Qwen 3.5 Model Family. <https://huggingface.co/Qwen>
8. NSIS (Nullsoft Scriptable Install System). <https://nsis.sourceforge.io/>
