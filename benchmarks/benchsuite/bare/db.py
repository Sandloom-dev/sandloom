"""SQLAlchemy (async + asyncpg) setup shared by the bare implementations.

The engine is configured like Sandloom's default database driver (LIFO pool, no
pre-ping, same pool size / overflow / recycle) so the pool is not a variable.
The tables are created by ``benchsuite.setup_db``; ``bare_outbox_messages`` is
``CREATE TABLE ... (LIKE outbox_messages INCLUDING ALL)`` — the same columns,
defaults and indexes as Sandloom's transactional outbox, so S4's index maintenance
cost is identical.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from benchsuite import common


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "bench_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)


class Write(Base):
    __tablename__ = "bench_writes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)


class BareOutbox(Base):
    """Same shape as Sandloom's ``outbox_messages`` (written, never relayed)."""

    __tablename__ = "bare_outbox_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    channel: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(10), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


def outbox_row(payload: dict[str, Any]) -> BareOutbox:
    return BareOutbox(
        id=uuid.uuid4(),
        channel=common.EVENT_CHANNEL,
        schema_version="v1",
        payload=payload,
        emitted_at=datetime.now(UTC).replace(tzinfo=None),
        delivery_attempts=0,
    )


def make_engine() -> AsyncEngine:
    return create_async_engine(
        common.database_url(),
        echo=False,
        pool_pre_ping=False,
        pool_use_lifo=True,
        pool_recycle=1800,
        pool_size=common.pool_size(),
        max_overflow=common.max_overflow(),
        pool_timeout=30,
    )


engine = make_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
