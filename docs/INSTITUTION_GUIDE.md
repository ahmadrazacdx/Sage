# Institution Guide

This document describes how to customize and deploy Sage for a specific educational institution or department, including curriculum ingestion, network policy, and branding.

## Table of Contents

- [Overview](#overview)
- [Configuration Hierarchy](#configuration-hierarchy)
- [Curriculum Ingestion](#curriculum-ingestion)
- [Network Policy](#network-policy)
- [Supported Document Formats](#supported-document-formats)
- [Vector Store Management](#vector-store-management)
- [Deployment Considerations](#deployment-considerations)

---

## Overview

Sage is designed to be institution-agnostic. Any university or college can deploy it by:

1. Ingesting their course materials into the RAG vector store.
2. Providing institution-specific configuration overrides.
3. Setting the network policy (online or offline operation).
4. Distributing the configured installer to students.

No source code modifications are required for standard institutional deployments.

## Configuration Hierarchy

Institution-specific settings should be placed in `config/institution.toml`.

Example institution configuration:

```toml
[app]
name = "Sage - University of Example"

[network]
force_offline = true

[corpus]
max_user_documents = 50
allowed_extensions = [".pdf", ".docx", ".pptx", ".md", ".txt"]

[rag]
chunk_size = 512
chunk_overlap = 64
top_k = 5
```

## Curriculum Ingestion

Course materials are ingested into the ChromaDB vector store using the ingestion script:

```bash
python scripts/ingest.py --source /path/to/materials --course CS101
```

### Parameters

| Parameter | Description |
| --- | --- |
| `--source` | Path to a directory containing course documents |
| `--course` | Course code used as metadata for filtered retrieval |

### Process

1. The script recursively scans the source directory for supported file types.
2. Each document is parsed, chunked (recursive text splitter, 512 tokens with 64-token overlap), and embedded using BGE-small-en-v1.5.
3. Chunks are indexed in the `curriculum` ChromaDB collection with metadata including the source filename and course code.

### Multiple Courses

Run the ingestion script:

```bash
python scripts/ingest.py
```

Students can then filter retrieval by course code in the application interface.

### Re-ingestion

To update materials for an existing course, re-ingest. The vector store does not perform automatic deduplication of document content.

## Network Policy

The `[network].force_offline` flag controls whether online tools (arXiv search,web search, Wikipedia) are available.

| Setting | Behavior |
| --- | --- |
| `force_offline = false` | Online tools are available when connectivity is detected |
| `force_offline = true` | All online tools are disabled regardless of connectivity |

For air-gapped deployments (no internet access), set `force_offline = true` to
prevent timeout errors when students attempt to use the Research mode.

The network monitor probes connectivity every 5 seconds (configurable via
`check_interval`). When `force_offline` is `false`, the system automatically
enables or disables online tools based on the probe result.

## Supported Document Formats

The following file formats are supported for curriculum ingestion:

| Format | Extension | Notes |
| --- | --- | --- |
| PDF | `.pdf` | Text-based PDFs; scanned image PDFs are not supported |
| Word | `.docx` | Microsoft Word documents |
| PowerPoint | `.pptx` | Slide text is extracted; images are not indexed |
| Markdown | `.md` | Plain text with markdown formatting |
| Plain text | `.txt` | Raw text files |

## Vector Store Management

The vector database is stored at `artifacts/data/databases/vectordb/`. To reset
the vector store entirely, delete this directory and re-run the ingestion
script.

## Deployment Considerations

### Lab or Shared Computer Deployment

For shared computers, configure:

```toml
[memory]
max_memories = 0
```

This disables the long-term memory system, preventing student information from
persisting between sessions on shared machines.

### Institutional Distribution

1. Configure `config/institution.toml` with institution-specific settings.
2. Ingest course materials using `scripts/ingest.py`.
3. Build the installer using `build.ps1`.
4. Distribute the installer to students via institutional channels.

The installer is self-contained and does not require internet access during
installation or runtime (when `force_offline = true`).

### Minimum Hardware Requirements for Student Machines

| Component | Minimum | Recommended |
| --- | --- | --- |
| RAM | 8 GB | 16 GB |
| Disk (free) | 4 GB | 8 GB |
| CPU | 4 cores, AVX2 | 6+ cores, AVX2 |
| GPU | Not required | NVIDIA with 4+ GB VRAM |
| OS | Windows 10 | Windows 11 |
