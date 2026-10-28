"""Default-database raw request dependency (framework-internal).

``get_db`` is the raw session provider the request UoW wraps. It lives under
``sandloom.core._internal`` deliberately: it is NOT a public surface. Only the
composition root (``create_app``) and test conftests touch it — the latter to
override it via ``app.dependency_overrides``. The engine/sessionmaker it uses
are owned and constructed by the database DRIVER
(``sandloom.core.drivers.sqlalchemy_database``); this module holds no engine of
its own. Business modules use ``DbSession`` from ``sandloom.core.di`` instead.
"""

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.drivers.sqlalchemy_database import (
    build_disposable_engine,
    get_default_sessionmaker,
    release_default_pools,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with get_default_sessionmaker()() as session:
        yield session


def is_relational_session(value: Any) -> bool:
    """True if ``value`` is a relational (UoW) session instance.

    Lets paradigm-agnostic kernel code (e.g. the RPC proxy) recognise an
    injected database session without importing SQLAlchemy itself — the
    relational session type stays known only to the relational surface.
    """
    return isinstance(value, AsyncSession)


def is_relational_session_type(annotation: Any) -> bool:
    """True if ``annotation`` is the relational session type (see above)."""
    return annotation is AsyncSession


# Test-harness seams (``sandloom.core.testing``): the harness reaches the driver's
# default pools and a caller-owned engine through this adapter, so no other
# kernel module needs to import a concrete driver.
__all__ = [
    "build_disposable_engine",
    "get_db",
    "is_relational_session",
    "is_relational_session_type",
    "release_default_pools",
]
