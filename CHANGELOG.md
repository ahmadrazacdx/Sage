# Changelog

All notable changes to this project will be documented in this file.

This changelog follows [Keep a Changelog](https://keepachangelog.com/) and
adheres to [Semantic Versioning](https://semver.org/).

---

## [0.1.0] - 2026-XX-XX

Initial release.

### Added

- Multi-agent orchestration framework using LangGraph
- Seven agent flows: Explain, Quiz, Diagram, Roadmap, Research, Code Fix, Thinking
- Hybrid RAG pipeline with ChromaDB vector store and BM25 sparse retrieval
- Hierarchical model strategy (primary 2B/4B + utility 0.8B)
- Long-term semantic memory with automatic fact extraction
- Context-aware history compression
- FastAPI backend with SSE streaming
- React/TypeScript frontend SPA
- pywebview desktop integration with system tray
- Sandboxed Python code execution
- Mermaid diagram generation, validation, and SVG rendering
- PDF and Markdown export for research reports
- arXiv, web, and Wikipedia search integration
- TOML-based configuration with Pydantic validation
- SQLite persistence with WAL mode
- Network connectivity monitoring with offline fallback
- NSIS Windows installer
- CI/CD pipeline (lint, test, build, release, security scan)
- Pre-commit hooks with Ruff, mypy, and conventional commits
