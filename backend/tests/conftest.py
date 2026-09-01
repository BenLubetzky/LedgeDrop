"""Shared test fixtures.

The suite runs against a real PostgreSQL database (the project uses PostgreSQL
only). It connects using the application's ``DATABASE_URL`` but swaps in a
dedicated ``*_test`` database, which is created automatically if missing and has
its schema rebuilt for every test. No external services beyond PostgreSQL are
required.

Point the tests at a different server by exporting ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("ENVIRONMENT", "test")

from app.api.deps import get_db  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.database.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.main import create_app  # noqa: E402


def _test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = sa.engine.make_url(settings.database_url)
    name = url.database or "ledgerdrop"
    if not name.endswith("_test"):
        name = f"{name}_test"
    return url.set(database=name).render_as_string(hide_password=False)


TEST_DATABASE_URL = _test_database_url()


async def _ensure_test_database() -> None:
    url = sa.engine.make_url(TEST_DATABASE_URL)
    admin_url = url.set(database="postgres").render_as_string(hide_password=False)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with admin_engine.connect() as conn:
            exists = await conn.scalar(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            )
            if not exists:
                await conn.execute(sa.text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await admin_engine.dispose()


@pytest_asyncio.fixture
async def engine():
    await _ensure_test_database()
    eng = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await eng.dispose()


@pytest_asyncio.fixture
def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def db_session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(session_factory) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.clear()
