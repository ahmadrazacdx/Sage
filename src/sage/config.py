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

from pydantic import Field, field_validator, model_validator
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

    tier: Literal["lite", "standard"] = "lite"
    model_lite: Path = Path("artifacts/models/embedding-models/arctic-embed-s")
    model_standard: Path = Path("artifacts/models/embedding-models/arctic-embed-m-v2")

    @property
    def active_model(self) -> Path:
        return self.model_lite if self.tier == "lite" else self.model_standard


class RAGSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    curriculum_collection: str = "curriculum"
    user_uploads_collection: str = "user_uploads"
    vectordb_lite_dir: Path = Path("artifacts/data/databases/vectordb-lite")
    vectordb_standard_dir: Path = Path("artifacts/data/databases/vectordb-standard")

    chunk_size: int = Field(default=512, ge=64, le=4096)
    chunk_overlap: int = Field(default=64, ge=0, le=512)

    top_k: int = Field(default=5, ge=1, le=50)
    rrf_k_constant: int = Field(default=60, ge=1)
    max_retrieval_iterations: int = Field(default=3, ge=1, le=10)

    @property
    def active_vectordb_dir(self) -> Path:
        # Resolved at call-time so EmbeddingSettings.tier changes propagate.
        raise NotImplementedError("Use settings.vectordb_dir_for(tier) instead")

    def vectordb_dir_for(self, tier: Literal["lite", "standard"]) -> Path:
        return self.vectordb_lite_dir if tier == "lite" else self.vectordb_standard_dir

    @model_validator(mode="after")
    def _overlap_lt_size(self) -> RAGSettings:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"chunk_overlap ({self.chunk_overlap}) must be "
                f"less than chunk_size ({self.chunk_size})"
            )
        return self


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
    check_interval: int = Field(default=60, ge=5)
    timeout: float = Field(default=2.0, ge=0.5, le=30.0)


class UISettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    host: str = "localhost"
    port: int = Field(default=8501, ge=1024, le=65535)
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