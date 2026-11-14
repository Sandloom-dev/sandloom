"""Default-database raw request dependency (framework-internal).

``get_db`` is the raw session provider the request UoW wraps. It lives under
``sandloom.core._internal`` deliberately: it is NOT a public surface. Only the
composition root (``create_app``) and test conftests touch it — the latter to
override it via ``app.dependency_overrides``. The sessions it yields come from
the session factory the ``default`` database instance of the active core
declares (``DatabaseProvider.session_factory``), so they follow the configured
instance (the infrastructure may override its URL); this module imports no
driver and holds no engine of its own. Business modules use ``DbSession`` from
``sandloom.core.di`` instead.
"""

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.runtime import get_active_core


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """A raw session of the ``default`` database, from its declared factory.

    Only the key of ``app.dependency_overrides`` in practice (a request's
    session is the UoW's, ``sandloom.core.di.get_db``).
    """
    core = get_active_core()
    if core is None:
        raise RuntimeError(
            "get_db: no active core. fix: call it inside a running application "
            "(create_app binds one) or a use_active_core() scope"
        )
    factory = core.database("default").session_factory
    if factory is None:
        raise RuntimeError(
            "get_db: the default database driver offers no raw session factory "
            "(DatabaseProvider.session_factory); use the unit of work instead"
        )
    async with factory() as session:
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


__all__ = [
    "get_db",
    "is_relational_session",
    "is_relational_session_type",
]
