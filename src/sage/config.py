"""
Configuration loader for Sage.

default.toml is read once at startup and merged with institution.toml
(institution values win). The merged result is validated into a typed
Settings object and exposed as a module-level singleton via `get_settings()`.

Usage anywhere in the codebase:
    from sage.config import get_settings
    settings = get_settings()
    settings.llm.model_path
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ----Paths----
_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # d:/Sage
_DEFAULT_TOML = _PROJECT_ROOT / "config" / "default.toml"
_INSTITUTION_TOML = _PROJECT_ROOT / "config" / "institution.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* on top of *base* (override wins)."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# ----Sub-models: one per TOML section----


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    name: str = "Sage"
    data_dir: Path = Path("artifacts/data")
    log_level: Literal["debug", "info", "warning", "error"] = "info"


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # --- Deployment tier ---
    # "auto" = inferred from available RAM.
    deployment_tier: Literal["auto", "nano", "mid", "turbo"] = "auto"

    model_path_cpu: Path = Path("artifacts/models/Qwen3.5-2B-Q4_K_M.gguf")
    model_path_cuda: Path = Path("artifacts/models/Qwen3.5-4B-Q4_K_M.gguf")
    model_name_cpu: str = "Qwen3.5-2B"
    model_name_cuda: str = "Qwen3.5-4B"

    # Active instance configuration dynamically populated by llm.py start_llm_server
    active_model_path: Path = Path(".")
    active_model_name: str = "uninitialized"
    active_context_size: int = 0
    active_parallel_slots: int = 1

    # Per-backend llama-server binaries.
    llama_cpp_cpu_bin: Path = Path("artifacts/servers/cpu/llama-server.exe")
    llama_cpp_cuda_bin: Path = Path("artifacts/servers/cuda/llama-server.exe")
    llama_cpp_vulkan_bin: Path = Path("artifacts/servers/vulkan/llama-server.exe")

    # "auto" or an integer string
    gpu_layers: str = "auto"
    context_window: str = "auto"

    cache_type_k: Literal["f16", "q8_0", "q5_0", "q4_0"] = "q4_0"
    cache_type_v: Literal["f16", "q8_0", "q5_0", "q4_0"] = "q4_0"

    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=256, le=131072)
    thinking_mode: bool = True
    reasoning_budget: int = Field(default=512, ge=0, le=32768)
    startup_timeout: float = Field(default=180.0, ge=30.0, le=600.0)
    port: int = Field(default=8080, ge=1, le=65535)

    @field_validator("gpu_layers", "context_window", mode="before")
    @classmethod
    def _coerce_auto_or_int(cls, v: object) -> str:
        """Accept "auto" or any integer-castable value; normalise to str."""
        sv = str(v)
        if sv.lower() == "auto":
            return "auto"
        try:
            int(sv)
            return sv
        except ValueError as err:
            raise ValueError(f"Expected 'auto' or an integer, got {v!r}") from err


class EmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    embed_model: Path = Path(
        "artifacts/models/embedding-models/models--qdrant--bge-small-en-v1.5-onnx-q/snapshots/52398278842ec682c6f32300af41344b1c0b0bb2"
    )


class RAGSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    curriculum_collection: str = "curriculum"
    vectordb: Path = Path("artifacts/data/databases/vectordb")

    # BM25 index paths
    bm25_curriculum_file: Path = Path("artifacts/data/databases/vectordb/bm25_curriculum.pkl")
    # Hybrid retrieval
    top_k: int = Field(default=3, ge=1, le=50)
    retrieval_multiplier: int = Field(default=2, ge=1, le=10)
    rrf_k_constant: int = Field(default=60, ge=1)
    max_retrieval_iterations: int = Field(default=2, ge=1, le=10)


class PreprocessingSettings(BaseSettings):
    """Parameters for the offline preprocessing pipeline."""

    model_config = SettingsConfigDict(extra="ignore")

    max_file_size_mb: int = Field(default=200, ge=1)
    min_chars_per_page: int = Field(default=50, ge=1)

    # OCR
    ocr_dpi: int = Field(default=300, ge=72, le=600)
    ocr_engine: Literal["tesseract", "easyocr"] = "tesseract"
    ocr_language: str = "eng"

    # LLM-based KU extraction window
    llm_window_tokens: int = Field(default=1500, ge=100, le=8000)
    llm_window_overlap: int = Field(default=200, ge=0, le=500)
    llm_concurrency: int = Field(default=5, ge=1, le=20)
    llm_retry_attempts: int = Field(default=3, ge=1, le=10)

    # Process-level parallelism
    workers: int = Field(default=0, ge=0)


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    path: Path = Path("artifacts/data/databases/sage.db")
    wal_mode: bool = True


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    max_input_tokens: int = Field(default=2000, ge=256)
    max_history_tokens: int = Field(default=800, ge=128)
    max_conversations: int = Field(default=100, ge=1)
    llm_timeout: int = Field(default=180, ge=10)
    research_writer_timeout: int = Field(default=300, ge=30)
    diagram_max_retries: int = Field(default=3, ge=1, le=10)
    research_max_iters: int = Field(default=2, ge=1, le=5)


class SandboxSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    timeout: int = Field(default=10, ge=1, le=60)
    max_code_length: int = Field(default=8000, ge=100)
    sessions_dir: Path = Path("artifacts/sandbox/data/sessions")
    figures_dir: Path = Path("artifacts/sandbox/data/figures")


class MermaidSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    mmdr_bin_path: Path = Path("artifacts/mmdr/mmdr.exe")
    render_timeout: float = Field(default=15.0, ge=1.0, le=60.0)


class SearchSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    arxiv_timeout: int = Field(default=30, ge=1)
    web_timeout: int = Field(default=30, ge=1)
    wiki_timeout: int = Field(default=30, ge=1)
    max_results: int = Field(default=5, ge=1, le=20)


class ExportSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    typst_bin: str = "typst"
    output_dir: Path = Path("artifacts/data/exports")


class ToolsSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    sandbox: SandboxSettings = SandboxSettings()
    search: SearchSettings = SearchSettings()
    export: ExportSettings = ExportSettings()
    mermaid: MermaidSettings = MermaidSettings()


class CorpusSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    max_user_documents: int = Field(default=100, ge=1)
    allowed_extensions: list[str] = [".pdf", ".docx", ".pptx", ".md", ".txt"]

    @field_validator("allowed_extensions", mode="before")
    @classmethod
    def _normalise_exts(cls, v: list[str]) -> list[str]:
        return [e if e.startswith(".") else f".{e}" for e in v]


class NetworkSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    force_offline: bool = False
    check_interval: int = Field(default=5, ge=5)
    timeout: float = Field(default=2.0, ge=0.5, le=30.0)


class UISettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    host: str = "localhost"
    port: int = Field(default=8765, ge=1024, le=65535)
    browser_auto_open: bool = True


class InstitutionSettings(BaseSettings):
    """Identity and program registry loaded from institution.toml."""

    model_config = SettingsConfigDict(extra="ignore")

    name: str = "Sage University"
    department: str = ""
    contact_email: str = ""
    academic_year: str = ""
    total_semesters: int = Field(default=8, ge=1, le=12)
    programs: dict[str, str] = Field(default_factory=dict)
    social: dict[str, str] = Field(default_factory=dict)


# ----Root Settings object----


class Settings(BaseSettings):
    """
    Fully-typed, validated configuration for Sage.

    Loaded once via get_settings(). All values originate from TOML files;
    no environment variable injection, secrets are handled separately.
    """

    model_config = SettingsConfigDict(extra="ignore")

    app: AppSettings = AppSettings()
    llm: LLMSettings = LLMSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    rag: RAGSettings = RAGSettings()
    preprocessing: PreprocessingSettings = PreprocessingSettings()
    database: DatabaseSettings = DatabaseSettings()
    agent: AgentSettings = AgentSettings()
    tools: ToolsSettings = ToolsSettings()
    corpus: CorpusSettings = CorpusSettings()
    network: NetworkSettings = NetworkSettings()
    ui: UISettings = UISettings()
    institution: InstitutionSettings = InstitutionSettings()


# ----Public API----


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Load, merge, and validate configuration exactly once per process.

    Merge order (later wins):
        default.toml  →  institution.toml

    The institution.toml [institution] section (semesters/courses registry)
    is intentionally excluded from Settings — it is read directly by the
    ingestion pipeline via load_institution_config().
    """
    raw = _deep_merge(_load_toml(_DEFAULT_TOML), _load_toml(_INSTITUTION_TOML))

    # Flatten nested TOML tables into each sub-model's constructor kwargs.
    tools_raw: dict[str, Any] = raw.get("tools", {})
    inst_raw: dict[str, Any] = raw.get("institution", {})

    return Settings(
        app=AppSettings(**raw.get("app", {})),
        llm=LLMSettings(**raw.get("llm", {})),
        embedding=EmbeddingSettings(**raw.get("embedding", {})),
        rag=RAGSettings(**raw.get("rag", {})),
        preprocessing=PreprocessingSettings(**raw.get("preprocessing", {})),
        database=DatabaseSettings(**raw.get("database", {})),
        agent=AgentSettings(**raw.get("agent", {})),
        tools=ToolsSettings(
            sandbox=SandboxSettings(**tools_raw.get("sandbox", {})),
            search=SearchSettings(**tools_raw.get("search", {})),
            export=ExportSettings(**tools_raw.get("export", {})),
            mermaid=MermaidSettings(**tools_raw.get("mermaid", {})),
        ),
        corpus=CorpusSettings(**raw.get("corpus", {})),
        network=NetworkSettings(**raw.get("network", {})),
        ui=UISettings(**raw.get("ui", {})),
        institution=InstitutionSettings(**inst_raw),
    )


def load_institution_config() -> dict[str, Any]:
    """
    Return the raw [institution] section from institution.toml.

    Used by the ingestion pipeline for display-name lookups (programs dict)
    and semester-count validation (total_semesters). Course and semester
    discovery is filesystem-driven -- no course registry lives in TOML.
    Returns an empty dict if institution.toml is absent.
    """
    raw = _load_toml(_INSTITUTION_TOML)
    result: dict[str, Any] = raw.get("institution", {})
    return result
