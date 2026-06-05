# Institution Guide

> Customizing Sage for a specific institution or department.

## Overview

Sage is designed to be deployed across different educational environments without modifying source code. All institution-specific behavior is controlled through a single TOML configuration file, logo/asset replacement, and a curriculum ingestion pipeline.

---

## Configuration File

Edit `config/institution.toml` to customize the identity of your deployment. Values in this file override the corresponding keys in `config/default.toml`.

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
| `social` | `string` | Official website URL |

## Cross-Departmental & University Adaptation

To deploy Sage for another department (e.g., Electrical Engineering, Business Administration) or a different university, follow this step-by-step pipeline:

### Step 1: Environment Setup

Clone the repository and install all dependencies to run the application locally in development mode:

```bash
# Clone the repository
git clone https://github.com/ahmadrazacdx/Sage.git
cd Sage

# Install python and node dependencies
uv sync --all-extras
```

Ensure you download the necessary local LLM models as detailed in the [Setup Guide](SETUP.md).

### Step 2: Configure Identity

Update `config/institution.toml` with your specific institution and department details as described in the [Configuration File](#configuration-file) section.

### Step 3: Organize Raw Materials

Prepare your curriculum data inside the `raw/` directory. You must follow the exact directory structure layout below so the metadata-tagging scripts can parse course details:

```text
#   raw/
#     <PROGRAM_CODE>/             - e.g., BSCS, BSEE (must be folder name)
#       <SEMESTER_NUMBER>/        - # e.g., 1, 2, ..., 8
#         <COURSE_CODE>_<Title>/  - e.g., CS101_Programming-Fundamentals
#           <files>               - .pdf  .pptx  .docx  .md  .txt
```

### Step 4: Run the Preprocessing Pipeline

Sage uses two pipelines to process and ingest the data. Before ingestion, run the preprocessing script to extract, clean, normalize, and format the raw files into clean Markdown documents:

```bash
uv run python scripts/preprocess.py
```

This script runs an 8-stage cleaning pipeline that writes output files under the `processed/` directory.

### Step 5: Curate Processed Data (Highly Recommended)

To ensure high-quality RAG search results and keep the final installation bundle size small and manageable, we recommend **manually cleaning** the generated `processed/` folder:

- Audit and prune large or redundant text blocks, OCR errors, or duplicate syllabus files, appendices, references, exercises, table of contents, index, etc.
- *Note for BSCS, IT, and SE Departments:* Both `raw/` containing the core books recommended by HEC, and `processed/` containing the manually curated, pre-cleaned data is already available via the data link in the [README.md](../README.md). You can download this pre-cleaned data directly or add your own custom materials to it.

### Step 6: Build the Vector DB & BM25 Indexes

Once the `processed/` directory is ready and curated:

1. Zip the `processed/` folder.
2. Open `notebooks/ingest.ipynb` (you can run this in Google Colab for faster processing, or run it locally).
3. Run the cells from top to bottom. The notebook will generate:
   - A ChromaDB vector database (`vectordb`)
   - `courses.json` containing metadata mapping
   - `bm25_curriculum.pkl` representing the sparse index
4. Download these files/folders and place them inside the project at `artifacts/data/databases/vectordb/`.

### Step 7: Custom Branding & Tuning

To replace Sage branding with your own logos:

- **Application Icons**: Replace `favicon.ico` and `favicon.svg` in `frontend/artifacts/sage/public/`.
- **Installer Icon**: Replace `sage.ico` in `installer/`.
- **RAG Parameter Tuning**: To tweak chunk sizes, overlap, retrieval top-k, or reciprocal rank fusion (RRF) constants, see the `[rag]` section in `config/default.toml`.

### Step 8: Build the Distributable Installer

Run the PowerShell build orchestrator script to compile the final executables and NSIS installer ready for distribution:

```powershell
./build.ps1 -Tier All
```

You can also build a specific hardware tier (e.g., `-Tier fast` or `-Tier pro`) depending on department hardware constraints. See [DEPLOYMENT.md](DEPLOYMENT.md) for tier options.
