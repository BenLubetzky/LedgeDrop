from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    LOCAL_DATABASE_URL,
    REPO_ROOT,
    DeploymentConfigError,
    Settings,
    check_deployment_safety,
)


def _valid_deployed_kwargs(**overrides: object) -> dict[str, object]:
    """A minimal configuration that passes the deployed-environment check."""
    base: dict[str, object] = {
        "_env_file": None,
        "ENVIRONMENT": "production",
        "DEBUG": "false",
        "LOG_FORMAT": "json",
        "CORS_ALLOW_ORIGINS": "https://app.example.com",
        "TRUSTED_HOSTS": "api.example.com",
        "DATABASE_URL": "postgresql+asyncpg://u:p@db.internal:5432/ledgerdrop",
        "STORAGE_BACKEND": "s3",
        "S3_BUCKET": "ledgerdrop-uploads",
        "S3_ENDPOINT_URL": "https://acct.r2.cloudflarestorage.com",
        "S3_ACCESS_KEY_ID": "r2-key-id",
        "S3_SECRET_ACCESS_KEY": "r2-secret",
        "EXTRACTION_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-live-value",
    }
    base.update(overrides)
    return base


def test_defaults_and_derived_values() -> None:
    settings = Settings(_env_file=None)
    assert settings.max_file_size_mb == 20
    assert settings.max_pdf_pages == 10
    assert settings.max_file_size_bytes == 20 * 1024 * 1024


def test_relative_upload_directory_is_resolved_against_repo_root() -> None:
    settings = Settings(_env_file=None, UPLOAD_DIRECTORY="storage/uploads")
    assert settings.upload_directory == REPO_ROOT / "storage" / "uploads"
    assert settings.upload_directory.is_absolute()


def test_absolute_upload_directory_is_left_alone(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, UPLOAD_DIRECTORY=str(tmp_path))
    assert settings.upload_directory == tmp_path


def test_cors_origins_accepts_comma_separated_string() -> None:
    settings = Settings(
        _env_file=None,
        CORS_ALLOW_ORIGINS="http://localhost:3000, https://app.example.com",
    )
    assert settings.cors_allow_origins == [
        "http://localhost:3000",
        "https://app.example.com",
    ]


def test_is_test_flag() -> None:
    assert Settings(_env_file=None, ENVIRONMENT="test").is_test is True
    assert Settings(_env_file=None, ENVIRONMENT="development").is_test is False


def test_extraction_provider_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EXTRACTION_PROVIDER="typo")


def test_openai_provider_requires_a_non_empty_key() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EXTRACTION_PROVIDER="openai")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EXTRACTION_PROVIDER="openai", OPENAI_API_KEY="   ")


def test_model_validation_error_does_not_leak_other_secrets() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            EXTRACTION_PROVIDER="openai",
            OPENAI_API_KEY=" ",
            S3_SECRET_ACCESS_KEY="super-secret-r2",
        )
    message = str(exc.value)
    assert "super-secret-r2" not in message
    assert "input_value" not in message


def test_openai_configuration_is_validated_and_key_is_secret() -> None:
    settings = Settings(
        _env_file=None,
        EXTRACTION_PROVIDER="openai",
        OPENAI_API_KEY="sk-test-value",
        OPENAI_MODEL="  gpt-5-mini  ",
        EXTRACTION_PROVIDER_TIMEOUT_SECONDS=30,
    )
    assert settings.openai_model == "gpt-5-mini"
    assert settings.openai_api_key.get_secret_value() == "sk-test-value"
    assert "sk-test-value" not in repr(settings)


def test_provider_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EXTRACTION_PROVIDER_TIMEOUT_SECONDS=0)


def test_environment_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ENVIRONMENT="prod")


def test_staging_and_production_are_valid_environments() -> None:
    staging = Settings(**_valid_deployed_kwargs(ENVIRONMENT="staging"))
    production = Settings(**_valid_deployed_kwargs())
    assert staging.environment == "staging"
    assert production.environment == "production"
    # A fully-valid deployed configuration passes the safety check.
    check_deployment_safety(staging)
    check_deployment_safety(production)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (
            "postgresql://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
        ),
        (
            "postgres://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
        ),
        (
            "postgresql+asyncpg://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
        ),
        (
            "postgresql+psycopg://u:p@host:5432/db",
            "postgresql+psycopg://u:p@host:5432/db",
        ),
    ],
)
def test_database_url_scheme_is_coerced_to_asyncpg(given: str, expected: str) -> None:
    assert Settings(_env_file=None, DATABASE_URL=given).database_url == expected


def test_trusted_hosts_accepts_comma_separated_string() -> None:
    settings = Settings(_env_file=None, TRUSTED_HOSTS="api.example.com, admin.example.com")
    assert settings.trusted_hosts == ["api.example.com", "admin.example.com"]


def test_dev_defaults_do_not_trigger_deployed_check() -> None:
    # The bundled localhost database URL and wildcard hosts are fine outside
    # staging/production.
    settings = Settings(_env_file=None, ENVIRONMENT="development")
    assert settings.database_url == LOCAL_DATABASE_URL
    assert settings.trusted_hosts == ["*"]
    check_deployment_safety(settings)  # no-op, does not raise


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"DEBUG": "true"}, "DEBUG"),
        ({"DATABASE_URL": LOCAL_DATABASE_URL}, "DATABASE_URL"),
        (
            {"DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db"},
            "DATABASE_URL",
        ),
        ({"DATABASE_URL": "sqlite+aiosqlite:///tmp/app.db"}, "DATABASE_URL"),
        ({"DATABASE_URL": "postgresql+psycopg://u:p@db.internal/db"}, "DATABASE_URL"),
        ({"CORS_ALLOW_ORIGINS": "http://localhost:3000"}, "CORS_ALLOW_ORIGINS"),
        ({"CORS_ALLOW_ORIGINS": "*"}, "CORS_ALLOW_ORIGINS"),
        ({"TRUSTED_HOSTS": "*"}, "TRUSTED_HOSTS"),
        ({"TRUSTED_HOSTS": "*.example.com"}, "TRUSTED_HOSTS"),
        ({"STORAGE_BACKEND": "local"}, "STORAGE_BACKEND"),
        ({"S3_BUCKET": ""}, "S3_BUCKET"),
        ({"S3_ENDPOINT_URL": ""}, "S3_ENDPOINT_URL"),
        ({"S3_REGION": ""}, "S3_REGION"),
        ({"EXTRACTION_PROVIDER": "fake"}, "EXTRACTION_PROVIDER"),
        ({"LOG_FORMAT": "text"}, "LOG_FORMAT"),
    ],
)
def test_deployed_check_rejects_unsafe_configuration(
    override: dict[str, object], needle: str
) -> None:
    settings = Settings(**_valid_deployed_kwargs(**override))
    with pytest.raises(DeploymentConfigError) as exc:
        check_deployment_safety(settings)
    assert needle in str(exc.value)


def test_deployed_check_error_does_not_leak_secret_values() -> None:
    settings = Settings(
        **_valid_deployed_kwargs(
            DEBUG="true",
            S3_SECRET_ACCESS_KEY="super-secret-r2",
            OPENAI_API_KEY="sk-super-secret",
        )
    )
    with pytest.raises(DeploymentConfigError) as exc:
        check_deployment_safety(settings)
    message = str(exc.value)
    assert "super-secret-r2" not in message
    assert "sk-super-secret" not in message


def test_deployed_check_reports_every_problem_at_once() -> None:
    settings = Settings(
        **_valid_deployed_kwargs(DEBUG="true", LOG_FORMAT="text", TRUSTED_HOSTS="*")
    )
    with pytest.raises(DeploymentConfigError) as exc:
        check_deployment_safety(settings)
    message = str(exc.value)
    assert "DEBUG" in message
    assert "LOG_FORMAT" in message
    assert "TRUSTED_HOSTS" in message
