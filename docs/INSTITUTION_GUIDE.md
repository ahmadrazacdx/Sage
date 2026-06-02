# Institution Guide

> Customizing Sage for a specific institution or department.

## Overview

Sage is designed to be deployed across different educational environments without modifying source code. All institution-specific behavior is controlled through a single TOML configuration file and a curriculum ingestion script.

## Configuration File

Edit `config/institution.toml`. Values in this file override the corresponding keys in `config/default.toml`.

```toml
[institution]
name            = "Example University"
department      = "Department of Computer Science"
contact_email   = "cs@example.edu"
academic_year   = "2025-2026"

[institution.social]
website = "https://example.edu"
```

### Supported Fields

| Field | Type | Description |
| --- | --- | --- |
| `name` | `string` | Institution display name |
| `department` | `string` | Department name |
| `contact_email` | `string` | Contact email for support references |
| `academic_year` | `string` | Active academic year |
| `social` | `dict` | Official website URL |

## Curriculum Ingestion

Course materials must be indexed into the ChromaDB vector store before deployment.

### Usage

```bash
python scripts/ingest.py --source /path/to/materials --course CS101
```

### Supported Document Formats

| Format | Extension |
| --- | --- |
| PDF (Text-based only) | `.pdf` |
| Word | `.docx` |
| PowerPoint | `.pptx` |
| Markdown | `.md` |
| Plain text | `.txt` |

### Indexing Details

- Documents are chunked at 512 tokens with 64-token overlap.
- Chunks are embedded using `BGE-small-en-v1.5` (ONNX via FastEmbed).
- Vectors are stored in ChromaDB at `artifacts/data/databases/vectordb/`.
- Each course gets its own metadata filter within the `curriculum` collection.
- Multiple courses can be ingested by running the script for each.

### Organizing Materials

```text
# Enforced directory structure:
#   raw/
#     <PROGRAM_CODE>/             - folder name IS the program code
#       <1-8>/                    - semester number
#         <COURSE_CODE>_<Title>/  - enables course-level metadata (RECOMMENDED)
#           <files>               - .pdf  .pptx  .docx  .md  .txt
```

```bash
python scripts/ingest.py --source materials/CS101 --course CS101
python scripts/ingest.py --source materials/CS201 --course CS201
```

## Deployment Workflow

1. **Configure identity:** Update `config/institution.toml` with institution details and any setting overrides.
2. **Ingest curriculum:** Run `scripts/ingest.py` to get the vector store.
3. **Build installer:** Run `build.ps1 -Tier <tier>` to compile the NSIS installer. See [DEPLOYMENT.md](DEPLOYMENT.md) for tier options.
4. **Distribute:** Provide the generated installer to end users. No development tools are required on target machines.

For build pipeline details, see [DEPLOYMENT.md](DEPLOYMENT.md).
