# Deployment Guide

This document describes the build pipeline, artifact staging, Windows installer packaging, and release workflow for distributing Sage as a standalone application.

---

## Table of Contents

- [Overview](#overview)
- [Build Pipeline](#build-pipeline)
- [Artifact Structure](#artifact-structure)
- [Frontend Build](#frontend-build)
- [Python Package Build](#python-package-build)
- [Staging](#staging)
- [NSIS Installer](#nsis-installer)
- [Release Workflow](#release-workflow)
- [Build Manifest](#build-manifest)
- [Distribution Checklist](#distribution-checklist)

---

## Overview

Sage is distributed as a self-contained Windows installer. The build pipeline orchestrates the following steps:

1. Build the frontend production bundle.
2. Package the Python application and its dependencies.
3. Stage all required artifacts (models, server binaries, configuration, embedding model) into a single directory tree.
4. Compile the NSIS installer script into an executable installer.

The entire process is automated by `build.ps1` (PowerShell) and can be triggered
manually or via the GitHub Actions release workflow.

## Build Pipeline

### build.ps1

The primary build script is `build.ps1` in the repository root. It performs:

1. **Environment validation**: Checks for required tools (Python, Node.js, pnpm,
   NSIS compiler).
2. **Frontend build**: Runs `pnpm build` in `frontend/artifacts/sage/`.
3. **Python packaging**: Builds the wheel distribution using the standard Python
   build toolchain.
4. **Staging**: Copies all artifacts into `installer/staging/` with the required
   directory layout.
5. **Installer compilation**: Invokes `makensis` on `installer/sage.nsi` to
   produce the final installer executable.

### Usage

```powershell
.\build.ps1
```

The resulting installer is placed in `installer/output/`.

## Artifact Structure

The staging directory mirrors the installed application layout:

```text
installer/staging/
  artifacts/
    models/
      Qwen3.5-2B-Q4_K_M.gguf
      Qwen3.5-4B-Q4_K_M.gguf
      Qwen3.5-0.8B-Q4_K_M.gguf
      embedding-models/
        bge-small-en-v1.5/
    servers/
      cpu/llama-server.exe
      cuda/llama-server.exe
      vulkan/llama-server.exe
    data/
      databases/
      exports/
    sandbox/
      data/
    mmdr/
      mmdr.exe
    typst/
      typst.exe
  config/
    default.toml
  frontend/
    artifacts/sage/dist/
  src/
    sage/
  launcher/
    sage-launcher.exe
```

## Frontend Build

The frontend is built independently before staging:

```bash
cd frontend/artifacts/sage
pnpm install
pnpm build
```

The output in `dist/` contains the production-ready static files (HTML, CSS,
JavaScript) that the FastAPI backend serves.

## Python Package Build

The Python package is built as a standard wheel:

```bash
python -m build
```

The built distribution is placed in `dist/`. For the installer, the source tree (`src/sage/`) is staged directly rather than the wheel, to simplify the runtime
layout.

## Staging

The staging step copies all required files into `installer/staging/`. This
includes:

- Python source tree (`src/sage/`)
- Frontend production build (`frontend/artifacts/sage/dist/`)
- Configuration files (`config/default.toml`)
- Model files (GGUF format)
- Server binaries (llama-server for CPU, CUDA, Vulkan)
- Tool binaries (Typst for PDF export)
- Embedding model directory

The staging directory is self-contained: the application can run from this directory without any external dependencies beyond a Python interpreter.

## NSIS Installer

The installer is defined in `installer/sage.nsi` using the Nullsoft Scriptable Install System (NSIS). It provides:

- Standard Windows installer UI with license agreement
- Installation directory selection
- Start menu and desktop shortcut creation
- Uninstaller registration in Windows Add/Remove Programs
- File association and registry entries
- Complete uninstall procedure

### Compiling the Installer

```bash
makensis installer/sage.nsi
```

The output executable is placed in `installer/output/`.

### Launcher

The installer includes a compiled launcher (`launcher/sage-launcher.exe`) that serves as the application entry point. It locates the bundled Python interpreter
and starts the Sage application.

## Release Workflow

The GitHub Actions workflow `release.yml` automates the release process:

1. **Trigger**: Pushing a tag matching the pattern `v*` (for example, `v0.1.0`).
2. **Build**: Runs the full build pipeline on a Windows runner.
3. **Test**: Executes the test suite to verify the build.
4. **Package**: Stages artifacts and compiles the NSIS installer.
5. **Release**: Creates a GitHub Release with the installer attached as a downloadable asset.

### Triggering a Release

```bash
git tag v0.1.0
git push origin v0.1.0
```

## Build Manifest

The file `installer/build-manifest.json` records metadata about each build:

- Build timestamp
- Git commit hash
- Model versions and checksums
- Server binary versions
- Frontend build hash

This manifest is included in the installer for traceability.

## Distribution Checklist

Before creating a release:

1. All CI checks pass (linting, type checking, tests).
2. `CHANGELOG.md` is updated with the release notes.
3. `pyproject.toml` version is incremented.
4. Model files are verified against their checksums.
5. The installer is tested on a clean Windows system.
6. The application starts successfully in desktop mode.
7. All agent flows produce valid outputs.
8. The uninstaller removes all files and registry entries.
