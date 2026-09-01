"""Environment-based application configuration.

All settings are read from environment variables (or a local ``.env`` file during
development). Nothing here should carry a usable production default for secrets or
connection strings - those must be supplied by the environment.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repository layout: this file is backend/app/core/config.py, so three parents up
# is the backend/ directory and four is the repository root.
BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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
