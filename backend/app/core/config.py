"""Environment-based application configuration.

All settings are read from environment variables (or a local ``.env`` file during
development). Nothing here should carry a usable production default for secrets or
connection strings - those must be supplied by the environment.

When ``ENVIRONMENT`` is ``staging`` or ``production`` a fail-fast check
(:func:`check_deployment_safety`, run by :func:`get_settings`) rejects a
configuration that is unsafe to deploy - debug left on, a localhost database or
CORS origin, a wildcard host allow-list, missing durable-storage or provider
credentials, or unstructured logging. The process refuses to start rather than
booting a misconfigured deployment. The check runs *after* model construction
and raises a plain :class:`RuntimeError` whose message names only the offending
settings, so no secret value can appear in the error or traceback.
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

# The database URL baked into the settings default and into docker-compose.yml.
# The deployed-environment validator rejects it (and any other localhost target)
# so a staging or production process can never silently run against a local
# throwaway database.
LOCAL_DATABASE_URL = (
    "postgresql+asyncpg://ledgerdrop:ledgerdrop@localhost:5432/ledgerdrop"
)

# Environments that must satisfy the deployment-safety checks in
# ``check_deployment_safety``.
DEPLOYED_ENVIRONMENTS = frozenset({"staging", "production"})


def _looks_local(value: str) -> bool:
    """True if ``value`` names a local loopback host anywhere in it."""
    lowered = value.lower()
    return any(token in lowered for token in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Absolute path so `backend/.env` is picked up no matter which directory
        # the process is launched from. Real environment variables still win over
        # the file. Values in the file are for local development only; credentials
        # and machine-specific values live here and the file is git-ignored.
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # Model-level errors otherwise include the complete raw settings input,
        # exposing unrelated credentials in startup logs when one field fails.
        hide_input_in_errors=True,
    )

    # --- Application -------------------------------------------------------
    app_name: str = "LedgerDrop"
    environment: Literal["development", "test", "staging", "production"] = Field(
        default="development",
        description="One of: development, test, staging, production.",
    )
    debug: bool = False

    # How application logs are rendered. Deployed environments must use "json"
    # so log aggregation and request-correlation (Stage 9 Package 6) work.
    log_format: Literal["text", "json"] = "text"

    # Comma-separated list of origins allowed to call the API from a browser.
    # NoDecode: keep pydantic-settings from JSON-parsing the raw env value so the
    # validator below can accept a plain "a,b,c" string.
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # Host header allow-list for ``TrustedHostMiddleware`` (middleware is wired
    # in Stage 9 Package 5). "*" is acceptable for local development; deployed
    # environments must pin an explicit list.
    trusted_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"]
    )

    # --- Database -------------------------------------------------------- -
    # Async SQLAlchemy URL, e.g.
    #   postgresql+asyncpg://ledgerdrop:ledgerdrop@localhost:5432/ledgerdrop
    # Render's managed database injects a sync-scheme "postgresql://" URL; the
    # validator below coerces it to the asyncpg driver the app and Alembic use.
    database_url: str = Field(default=LOCAL_DATABASE_URL)
    db_echo: bool = False

    # --- File storage ---------------------------------------------------- -
    # Where accepted PDFs are written during development. Relative paths are
    # resolved against the repository root so behaviour does not depend on the
    # process working directory.
    upload_directory: Path = Field(default=Path("storage/uploads"))

    # "local" (filesystem, development/test default) or "s3" (S3-compatible
    # object storage, e.g. Cloudflare R2 in deployed environments). The "s3"
    # backend itself lands in Stage 9 Package 4; this setting and its validation
    # are defined now so configuration and code ship separately.
    storage_backend: Literal["local", "s3"] = "local"

    # S3 / Cloudflare R2 connection settings. Optional at the type level and
    # required by ``check_deployment_safety`` when ``storage_backend`` is
    # "s3"; ignored entirely for "local".
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None  # e.g. https://<account>.r2.cloudflarestorage.com
    s3_region: str = "auto"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_key_prefix: str = ""  # optional object-key namespace within the bucket

    # --- Document constraints (Stage 2 MVP limits) ---------------------- --
    max_file_size_mb: int = 20
    max_pdf_pages: int = 10

    # --- Stage 3 extraction provider -------------------------------------
    # "fake" (deterministic, offline) or "openai" (GPT-5-mini, real calls).
    # Deployed environments must use "openai".
    extraction_provider: Literal["fake", "openai"] = "fake"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5-mini"
    extraction_provider_timeout_seconds: int = Field(default=60, gt=0)

    # --- Stage 9 edge hardening (middleware wired in Package 5) ----------
    rate_limit_enabled: bool = False
    rate_limit_default: str = "60/minute"
    rate_limit_upload: str = "10/minute"

    # --- Stage 9 error monitoring (wired in Packages 6-7) ---------------
    sentry_dsn: SecretStr | None = None

    @field_validator("openai_model")
    @classmethod
    def _require_model_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("OPENAI_MODEL must not be blank")
        return value

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("DATABASE_URL must not be blank")
        # A managed PostgreSQL add-on typically emits a driver-less
        # "postgresql://" (or legacy "postgres://") URL. The app and Alembic
        # both use the async engine, so coerce the scheme. A URL that already
        # names a driver ("postgresql+asyncpg://", "postgresql+psycopg://", ...)
        # is left untouched.
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix) :]
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

    @field_validator("cors_allow_origins", "trusted_hosts", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def is_test(self) -> bool:
        return self.environment.lower() == "test"


class DeploymentConfigError(RuntimeError):
    """Raised when a staging/production configuration is unsafe to deploy."""


def check_deployment_safety(settings: Settings) -> None:
    """Fail fast on a staging/production configuration that is unsafe to deploy.

    Runs after model construction so the error message is fully under our
    control: it lists only the names of the offending settings and never a
    value. A no-op for ``development`` and ``test``.
    """
    if settings.environment not in DEPLOYED_ENVIRONMENTS:
        return

    problems: list[str] = []

    if settings.debug:
        problems.append("DEBUG must be false")

    if settings.database_url == LOCAL_DATABASE_URL or _looks_local(settings.database_url):
        problems.append("DATABASE_URL must point at a managed database, not localhost")
    elif not settings.database_url.startswith("postgresql+asyncpg://"):
        problems.append("DATABASE_URL must use PostgreSQL with the asyncpg driver")

    if not settings.cors_allow_origins:
        problems.append("CORS_ALLOW_ORIGINS must list at least one origin")
    elif any(origin == "*" or _looks_local(origin) for origin in settings.cors_allow_origins):
        problems.append("CORS_ALLOW_ORIGINS must not contain a wildcard or localhost origin")

    if not settings.trusted_hosts or any("*" in host for host in settings.trusted_hosts):
        problems.append("TRUSTED_HOSTS must be an explicit list without wildcards")

    if settings.storage_backend != "s3":
        problems.append("STORAGE_BACKEND must be 's3'")
    else:
        missing = [
            name
            for name, present in (
                ("S3_BUCKET", bool((settings.s3_bucket or "").strip())),
                ("S3_ENDPOINT_URL", bool((settings.s3_endpoint_url or "").strip())),
                ("S3_REGION", bool(settings.s3_region.strip())),
                (
                    "S3_ACCESS_KEY_ID",
                    settings.s3_access_key_id is not None
                    and bool(settings.s3_access_key_id.get_secret_value().strip()),
                ),
                (
                    "S3_SECRET_ACCESS_KEY",
                    settings.s3_secret_access_key is not None
                    and bool(settings.s3_secret_access_key.get_secret_value().strip()),
                ),
            )
            if not present
        ]
        if missing:
            problems.append("missing S3 configuration: " + ", ".join(missing))

    if settings.extraction_provider != "openai":
        problems.append("EXTRACTION_PROVIDER must be 'openai'")
    elif settings.openai_api_key is None or not settings.openai_api_key.get_secret_value().strip():
        problems.append("OPENAI_API_KEY must be set")

    if settings.log_format != "json":
        problems.append("LOG_FORMAT must be 'json'")

    if problems:
        raise DeploymentConfigError(
            f"Unsafe configuration for ENVIRONMENT={settings.environment!r}: "
            + "; ".join(problems)
        )


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance.

    Constructs the settings and then, for a deployed environment, refuses to
    return them (aborting process start) if the configuration is unsafe.
    """
    loaded = Settings()
    check_deployment_safety(loaded)
    return loaded


settings = get_settings()
