# Deployment Guide

> Build pipeline, installer packaging, and release process for Sage.

## Overview

Sage is distributed as a self-contained Windows NSIS installer that bundles a portable Python runtime, all application code, the frontend build, pre-compiled `llama-server` binaries, and optionally the GGUF model files. The end user does not need Python, Node.js, or any development tools installed.

## Installer Tiers

The build system produces four installer variants:

| Tier | Backend | Models Bundled | Typical Size | Use Case |
| --- | --- | --- | --- | --- |
| `fast` | CPU | Yes | ~3.42 GB | CPU-only machines, fully offline |
| `pro` | CUDA | Yes | ~5.41 GB | GPU-accelerated, fully offline |
| `fast-lite` | CPU | No | ~1.61 GB | Smaller download; models added separately |
| `pro-lite` | CUDA | No | ~2.14 GB | Smaller download; models added separately |

Tier configuration is defined in `installer/build-manifest.json`, which specifies download sources, SHA-256 checksums, and per-tier artifact inclusion rules.

## Build Pipeline

The primary build script is `build.ps1` (PowerShell). It performs the following steps:

### Pipeline Steps

| Step | Description |
| --- | --- |
| **1. Frontend Build** | Compiles the React SPA via `pnpm build` in `frontend/artifacts/sage/` |
| **2. Python Packaging** | Builds the wheel distribution via `uv build` |
| **3. Artifact Download** | Downloads models, server binaries, embedding model, and Typst from sources in `build-manifest.json`. Cached in `installer/.cache/`. |
| **4. Python Standalone** | Downloads a portable CPython 3.12 distribution (no system install required) |
| **5. Staging** | Copies all components into `installer/staging/` in the final application layout |
| **6. NSIS Compilation** | Executes `makensis` against `installer/sage.nsi` to produce the installer executable |
| **7. Checksum** | Generates `SHA256SUMS.txt` for the output archive |

## Build Script

```bash
# Build the 'fast' tier (CPU with bundled models)
.\build.ps1 -Tier fast

# Build the 'pro' tier (CUDA with bundled models)
.\build.ps1 -Tier pro

# Build a lite variant (no models)
.\build.ps1 -Tier fast-lite
```

The compiled installer is placed in `installer/output/`.

## Staging Layout

The staging directory represents the exact filesystem layout installed on the end-user machine:

```text
installer/staging/
├── artifacts/
│   ├── models/              # GGUF model files + embedding model
│   ├── servers/             # llama-server binaries (cpu/ and/or cuda/)
│   ├── typst/               # Typst binary for PDF export
│   ├── mmdr/                # Mermaid renderer binary
│   └── data/                # Runtime data (databases, exports, sandbox)
├── config/
│   ├── default.toml         # Base configuration
│   └── institution.toml     # Institution overrides
├── frontend/
│   └── artifacts/sage/dist/ # Compiled React SPA
├── src/
│   └── sage/                # Python application source
├── python/                  # Portable CPython 3.12 runtime
└── launcher/                # Native application entry point
```

## CI/CD Pipeline

### Continuous Integration (`ci.yml`)

Runs on every push to `main`, `dev`, and `dev/**` branches, and on PRs to `main`:

| Job | Description |
| --- | --- |
| `lint` | Ruff lint + format check, Mypy type checking |
| `test` | pytest on Ubuntu + Windows × Python 3.12 |
| `build` | Wheel build + install verification |
| `architecture-check` | Validates import layering |

### Security (`security.yml`)

| Job | Trigger | Description |
| --- | --- | --- |
| `secrets-scan` | Every push | Gitleaks secret detection |
| `dependency-audit` | PRs + weekly | pip-audit against exported requirements |
| `codeql` | PRs + weekly | GitHub CodeQL static analysis |

### Release (`release.yml`)

Triggered by pushing a semver tag (e.g., `v0.1.0`):

| Step | Description |
| --- | --- |
| Build | Runs `build.ps1` for all four tiers on `windows-latest` |
| Upload to R2 | Pushes installer archives to Cloudflare R2 CDN |
| GitHub Release | Creates a release with auto-generated changelog (via `git-cliff`) and attached installer archives |

## Release Process

1. Ensure all CI checks pass on `main`.
2. Update version in `pyproject.toml` and `installer/build-manifest.json`.
3. Update `CHANGELOG.md`.
4. Tag the release:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

5. The release workflow builds all four installer tiers, uploads to R2, and creates a GitHub Release.

## Release Validation

Before tagging a release:

- [ ] All CI checks pass (lint, test, build, architecture).
- [ ] Model SHA-256 checksums in `build-manifest.json` are current.
- [ ] Application starts successfully on a clean Windows 10/11 environment.
- [ ] All agent modes produce valid responses.
- [ ] RAG retrieval returns relevant results from ingested curriculum.
- [ ] Online tools (arXiv, web search) function when `force_offline = false`.
- [ ] Uninstaller removes all registry entries and filesystem traces.
