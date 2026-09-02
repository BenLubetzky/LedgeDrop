from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import REPO_ROOT, Settings


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
