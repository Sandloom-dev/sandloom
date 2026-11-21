"""A unit of work opened for a named module (SDK-internal, not public API).

``open_uow()`` resolves the module of its *caller*. Code that acts for another
module, such as a projection rebuild running for the module that owns the
projection, names that module here instead. Kept out of ``sandloom.sdk._prim``
because L1 (``_decl``) may import only public L2 names, and out of the public
``open_uow`` signature so that no public API changes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.di import db_session, module_database_of


@asynccontextmanager
async def open_uow_for_module(module: object) -> AsyncGenerator[AsyncSession, None]:
    """A unit of work on the primary database of ``module`` (its import path,
    as ``ModuleDescriptor.name`` holds it). Like ``open_uow()`` without a
    name, a module that no loaded module holds gets ``default`` and one that
    requires several databases (``default`` not among them) and has no
    ``migrations_database`` raises ``MigrationTargetError``.

    The database is resolved on entering the block, against the core active
    then, as ``db_session`` resolves its core: a manager created under one
    ``use_active_core`` scope and entered under another uses the second."""
    async with db_session(module_database_of(module)) as session:
        yield session
