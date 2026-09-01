"""Async database engine and session management.

A single :data:`engine` and :data:`SessionLocal` factory are created per process.
Request handlers receive a session through the :func:`get_db` FastAPI dependency,
which commits on success and rolls back if the handler raises.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# The engine configures and manages the communication line between SQLAlchemy/Python
# and the database, make sure to check whether a connection is alive before reusing,
# use newer versions.
engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a database session for the lifetime of one request.

    The session is committed when the handler returns normally and rolled back
    if it raises, so route code never has to manage the transaction boundary
    itself. This keeps failed uploads from leaving a half-written ``documents``
    row behind.
    """
    async with SessionLocal() as session:
        try:
            # give the database session to the end point
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def dispose_engine() -> None:
    """Close all pooled connections. Called on application shutdown."""
    await engine.dispose()