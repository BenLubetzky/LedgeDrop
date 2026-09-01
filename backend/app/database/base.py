"""Declarative base for all ORM models.

Models import :class:`Base` from here. Alembic's ``env.py`` imports the same
:data:`Base.metadata` (via ``app.models``) so autogenerate sees every table.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase

# Explicit, predictable constraint and index names keep Alembic migrations
# stable and readable instead of relying on the database's defaults.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Every bare ``datetime`` column is timezone-aware; timestamps are stored in UTC.
    type_annotation_map = {
        datetime: DateTime(timezone=True),
    }
