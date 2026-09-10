"""S3FileStorage against a local moto server (no real network, no AWS)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import boto3
import pytest

pytest.importorskip("moto")
from moto.server import ThreadedMotoServer  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.services.storage import FileStorage, StorageError, build_storage  # noqa: E402
from app.services.storage.s3 import S3FileStorage  # noqa: E402

_ACCESS_KEY = "test-access"
_SECRET_KEY = "test-secret"
_REGION = "us-east-1"


@pytest.fixture(scope="module")
def moto_endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    _, port = server.get_host_and_port()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


@pytest.fixture
def bucket(moto_endpoint: str) -> Iterator[str]:
    name = f"ledgerdrop-{uuid.uuid4().hex[:12]}"
    client = boto3.client(
        "s3",
        endpoint_url=moto_endpoint,
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        region_name=_REGION,
    )
    client.create_bucket(Bucket=name)
    try:
        yield name
    finally:
        objects = client.list_objects_v2(Bucket=name).get("Contents", [])
        for obj in objects:
            client.delete_object(Bucket=name, Key=obj["Key"])
        client.delete_bucket(Bucket=name)


def _storage(endpoint: str, bucket: str, *, key_prefix: str = "") -> S3FileStorage:
    return S3FileStorage(
        bucket=bucket,
        endpoint_url=endpoint,
        region=_REGION,
        access_key_id=_ACCESS_KEY,
        secret_access_key=_SECRET_KEY,
        key_prefix=key_prefix,
    )


def _raw_client(endpoint: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        region_name=_REGION,
    )


async def test_save_then_get_round_trips(moto_endpoint: str, bucket: str) -> None:
    storage = _storage(moto_endpoint, bucket)
    document_id = uuid.uuid4()

    stored = await storage.save_bytes(document_id, b"%PDF-1.7 hello")

    assert stored.location == f"{document_id}/original.pdf"
    assert stored.path is None
    assert stored.size_bytes == len(b"%PDF-1.7 hello")
    assert await storage.get_bytes(stored.location) == b"%PDF-1.7 hello"


async def test_exists_reflects_state(moto_endpoint: str, bucket: str) -> None:
    storage = _storage(moto_endpoint, bucket)
    document_id = uuid.uuid4()
    location = storage.location_for(document_id)

    assert await storage.exists(location) is False
    await storage.save_bytes(document_id, b"data")
    assert await storage.exists(location) is True


async def test_get_missing_object_raises_storage_error(moto_endpoint: str, bucket: str) -> None:
    storage = _storage(moto_endpoint, bucket)
    with pytest.raises(StorageError):
        await storage.get_bytes(storage.location_for(uuid.uuid4()))


async def test_delete_is_idempotent(moto_endpoint: str, bucket: str) -> None:
    storage = _storage(moto_endpoint, bucket)
    document_id = uuid.uuid4()
    await storage.save_bytes(document_id, b"data")

    await storage.delete(document_id)
    assert await storage.exists(storage.location_for(document_id)) is False
    await storage.delete(document_id)  # no error the second time


async def test_key_prefix_is_applied(moto_endpoint: str, bucket: str) -> None:
    storage = _storage(moto_endpoint, bucket, key_prefix="tenant/a/")
    document_id = uuid.uuid4()
    await storage.save_bytes(document_id, b"data")

    keys = [
        obj["Key"]
        for obj in _raw_client(moto_endpoint).list_objects_v2(Bucket=bucket).get("Contents", [])
    ]
    assert keys == [f"tenant/a/{document_id}/original.pdf"]
    # The stored location (persisted in documents.file_location) stays prefix-free.
    assert await storage.get_bytes(f"{document_id}/original.pdf") == b"data"


@pytest.mark.parametrize(
    "bad_location",
    ["../escape.pdf", "../../etc/passwd", "sub/../../outside.pdf", "/absolute/path.pdf", "C:\\x.pdf"],
)
async def test_key_for_rejects_unsafe_locations(
    moto_endpoint: str, bucket: str, bad_location: str
) -> None:
    storage = _storage(moto_endpoint, bucket)
    with pytest.raises(StorageError):
        await storage.get_bytes(bad_location)


async def test_health_check_ok_and_missing_bucket(moto_endpoint: str, bucket: str) -> None:
    await _storage(moto_endpoint, bucket).health_check()  # existing bucket: no raise
    with pytest.raises(StorageError):
        await _storage(moto_endpoint, "nonexistent-bucket-xyz").health_check()


def test_build_storage_selects_backend_from_settings(tmp_path) -> None:
    local = build_storage(Settings(_env_file=None, STORAGE_BACKEND="local"))
    assert isinstance(local, FileStorage)
    assert type(local).__name__ == "LocalFileStorage"

    s3 = build_storage(
        Settings(
            _env_file=None,
            STORAGE_BACKEND="s3",
            S3_BUCKET="b",
            S3_ENDPOINT_URL="https://example.r2.cloudflarestorage.com",
            S3_ACCESS_KEY_ID="k",
            S3_SECRET_ACCESS_KEY="s",
        )
    )
    assert isinstance(s3, S3FileStorage)
