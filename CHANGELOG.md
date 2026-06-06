# Changelog

All notable changes to this project will be documented in this file.

This changelog follows [Keep a Changelog](https://keepachangelog.com/) and
adheres to [Semantic Versioning](https://semver.org/).

---

## [0.1.0] - 2026-06-06

Initial release of **Sage**, an offline-first academic assistant using Retrieval-Augmented Generation (RAG) with multi-agent orchestration via LangGraph.

### Added

- **Multi-Agent Orchestration**: LangGraph-based workflow router executing 7 specialized agent flows (Explain, Quiz Me, Diagram, Study Plan, Research, Fix Code, and Thinking) using a hierarchical model strategy (primary 2B/4B + utility 0.8B models).
- **Hybrid RAG Pipeline**: Local document ingestion and search combining dense vector retrieval (ChromaDB + ONNX BGE-small embeddings) and sparse keyword retrieval (BM25) fused via Reciprocal Rank Fusion (RRF).
- **Desktop Shell Integration**: Wrapped python backend using `pywebview` with a custom C-based launcher, system tray control via `pystray`, and offline fallback check.
- **Interactive Diagram Rendering**: Client-side rendering of generated diagrams using Mermaid with robust server-side syntax validation and sanitization.
- **Long-term Semantic Memory**: Automated memory extraction and history compression to persist learning facts across chat sessions.
- **Typst-Powered Exports**: Report exports in PDF and Markdown format, with patched checkpointer-based retrieval for persistent download access.
- **Sandboxed Code Execution**: Safe local code execution environment pre-configured with scientific libraries (NumPy, Pandas, SciPy, SymPy, Matplotlib, Scikit-learn).
- **Academic Search Engines**: Integrated arXiv API, Wikipedia search, and DuckDuckGo search APIs.
- **Windows Installer (NSIS)**: Complete Windows packaging scripts (`build.ps1`) with auto-configuration and model checksum verification.
- **Staging Hub & Frontend UI**: Responsive React/TypeScript SPA with dynamic loading, landing page flow, staging hub, and prompt length limits.
- **Comprehensive CI/CD Pipeline**: GitHub Actions workflows for linting, security scans, unit tests, release installers, R2 bucket storage uploads, and GitHub Pages deployments.
- **Developer Documentation**: Complete guides for Architecture, Environment Setup, API Contracts, Deployment, and Departmental customizability.

### Changed

- **Unified Release CDN**: Standardized CI/CD builds (main and dev-main branches) to upload release artifacts to a single, persistent Cloudflare R2 directory (`vdev-ci`) to resolve download path fragmentation.
- **Embeddings & Fonts**: Standardized font-embedding configuration for consistent local PDF exports.
- **Performance & Structure**: Deduplicated LLM parameters in `llm.py`, removed redundant retrieval modules for Code Fix and Diagram modes to speed up generation, and solved Ruff/Mypy linting errors across all modules.

### Fixed

- **Mermaid Syntax Errors**: Sanitized Mermaid generation output to eliminate class suffix syntax errors and label formatting issues.
- **Course List Ingestion**: Patched `/api/courses` endpoint to dynamically read and populate the UI from `courses.json`.
- **UI Overflow**: Capped maximum prompt input length in the chat panel to prevent visual breaks.
- **Download Persistence**: Solved retrieval bugs in SQLite memory checkpointing to ensure research download list persists across user sessions.
- **Unit Tests**: Repaired failing agent core tests (`test_agents_core.py` and `test_agents_response.py`) and reached 85%+ test coverage.
