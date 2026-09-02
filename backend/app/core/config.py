"""Environment-based application configuration.

All settings are read from environment variables (or a local ``.env`` file during
development). Nothing here should carry a usable production default for secrets or
connection strings - those must be supplied by the environment.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repository layout: this file is backend/app/core/config.py, so three parents up
# is the backend/ directory and four is the repository root.
BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Absolute path so `backend/.env` is picked up no matter which directory
        # the process is launched from. Real environment variables still win over
        # the file. Values in the file are for local development only; credentials
        # and machine-specific values live here and the file is git-ignored.
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application -------------------------------------------------------
    app_name: str = "LedgerDrop"
    environment: str = Field(
        default="development",
        description="One of: development, test, production.",
    )
    debug: bool = False

    # Comma-separated list of origins allowed to call the API from a browser.
    # NoDecode: keep pydantic-settings from JSON-parsing the raw env value so the
    # validator below can accept a plain "a,b,c" string.
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # --- Database -------------------------------------------------------- -
    # Async SQLAlchemy URL, e.g.
    #   postgresql+asyncpg://ledgerdrop:ledgerdrop@localhost:5432/ledgerdrop
    database_url: str = Field(
        default="postgresql+asyncpg://ledgerdrop:ledgerdrop@localhost:5432/ledgerdrop",
    )
    db_echo: bool = False

    # --- File storage ---------------------------------------------------- -
    # Where accepted PDFs are written during development. Relative paths are
    # resolved against the repository root so behaviour does not depend on the
    # process working directory.
    upload_directory: Path = Field(default=Path("storage/uploads"))

    # --- Document constraints (Stage 2 MVP limits) ---------------------- --
    max_file_size_mb: int = 20
    max_pdf_pages: int = 10

    # --- Stage 3 extraction provider -------------------------------------
    # "fake" (deterministic, offline) or "openai" (GPT-5-mini, real calls).
    extraction_provider: Literal["fake", "openai"] = "fake"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5-mini"
    extraction_provider_timeout_seconds: int = Field(default=60, gt=0)

    @field_validator("openai_model")
    @classmethod
    def _require_model_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("OPENAI_MODEL must not be blank")
        return value

    @model_validator(mode="after")
    def _require_openai_key(self) -> "Settings":
        if self.extraction_provider == "openai":
            if self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip():
                raise ValueError("OPENAI_API_KEY is required when EXTRACTION_PROVIDER=openai")
        return self

    @field_validator("upload_directory")
    @classmethod
    def _resolve_upload_directory(cls, value: Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = REPO_ROOT / path
        return path

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def is_test(self) -> bool:
        return self.environment.lower() == "test"


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance."""
    return Settings()


settings = get_settings()
