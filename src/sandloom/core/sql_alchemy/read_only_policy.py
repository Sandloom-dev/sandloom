"""Read-only statement policy of ``ReadOnlyDbSession`` (``DB_READ_ONLY_SESSIONS``).

``ReadOnlyDbSession`` begins its transactions read-only with the renderer's
``transaction_options`` on its option-engine, and uses this dialect hook as a
second, early refusal layer for writes, locking reads and raw statements.
Where the database has ``readonly_transactions`` its read-only transaction
remains authoritative for SELECT functions: SQL syntax alone cannot prove a
read is side-effect free.

The hook is installed where the engine's DBAPI adapter supports
``read_only_policy`` (asyncpg today). On another DBAPI the session relies on
the database's read-only transaction; a database with neither is refused
(:func:`read_only_sessions_problem`, SANDLOOM-B031).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.selectable import CompoundSelect, Select

from sandloom.core.sql_alchemy.dbapi import (
    READ_ONLY_POLICY,
    dbapi_adapter,
    dbapi_adapter_of,
    dbapi_of_url,
)
from sandloom.core.sql_alchemy.portable import dialect_of_url, renderer_features

_INSTALLED_ATTR = "_sandloom_read_executor_installed"
#: Execution option set by the READ-ONLY session policy (``ReadOnlyDbSession``):
#: permitted reads keep BEGIN READ ONLY; other statements are refused early.
READ_ONLY_OPTION = "sandloom_read_only"


class ReadOnlySessionError(RuntimeError):
    """A write / locking read / raw statement on a read-only session."""


class ReadOnlySessionsUnavailable(RuntimeError):
    """``DB_READ_ONLY_SESSIONS`` on a database instance whose database has no
    read-only transactions and whose DBAPI has no client read-only policy:
    nothing would refuse a write (SANDLOOM-B031)."""


def read_only_sessions_problem(name: str, url: str) -> str | None:
    """Why the database instance ``name`` at ``url`` cannot serve
    ``ReadOnlyDbSession``, or ``None``. Build-free: the URL's renderer
    features and DBAPI adapter only."""
    dialect, dbapi = dialect_of_url(url), dbapi_of_url(url)
    if "readonly_transactions" in renderer_features(dialect):
        return None
    if READ_ONLY_POLICY in dbapi_adapter(dbapi).capabilities:
        return None
    return (
        f"DB_READ_ONLY_SESSIONS=true, and the database instance {name!r} "
        f"({dialect}+{dbapi or 'default'}) has neither read-only transactions "
        "nor a client read-only policy for its DBAPI [SANDLOOM-B031]; set "
        "DB_READ_ONLY_SESSIONS=false (read-only routes then use DbSession), or "
        "run it on a database with read-only transactions (PostgreSQL)"
    )


def _is_plain_read(context: Any) -> bool:
    compiled = getattr(context, "compiled", None)
    stmt = getattr(compiled, "statement", None)
    if isinstance(stmt, Select):
        return stmt._for_update_arg is None
    return isinstance(stmt, CompoundSelect)


def _refuse(statement: Any) -> ReadOnlySessionError:
    text = str(statement).split(None, 1)[0] if statement else "?"
    return ReadOnlySessionError(
        f"read-only session refused a non-read statement ({text} ...): only "
        "plain SELECTs are allowed; use DbSession for writes and locking reads"
    )


def _wrap(original: Callable[..., Any]) -> Callable[..., Any]:
    def do_execute(
        cursor: Any, statement: Any, parameters: Any, context: Any = None
    ) -> Any:
        if (
            context is not None
            and context.execution_options.get(READ_ONLY_OPTION, False)
            and not _is_plain_read(context)
        ):
            raise _refuse(statement)
        return original(cursor, statement, parameters, context)

    return do_execute


def _wrap_refusing(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(cursor: Any, statement: Any, *args: Any) -> Any:
        opts = getattr(args[-1] if args else None, "execution_options", None)
        if opts and opts.get(READ_ONLY_OPTION, False):
            raise _refuse(statement)
        return original(cursor, statement, *args)

    return wrapper


def install_read_executor(engine: AsyncEngine) -> bool:
    """Install the read-only statement policy on ``engine`` when its DBAPI
    adapter supports ``read_only_policy`` (idempotent). False: not installed,
    the database's read-only transaction is the only guard (module doc)."""
    sync_engine = engine.sync_engine
    if getattr(sync_engine, _INSTALLED_ATTR, False):
        return True
    dialect = sync_engine.dialect
    if READ_ONLY_POLICY not in dbapi_adapter_of(dialect).capabilities:
        return False
    dialect.do_execute = _wrap(dialect.do_execute)  # type: ignore[method-assign]
    dialect.do_executemany = _wrap_refusing(dialect.do_executemany)  # type: ignore[method-assign]
    dialect.do_execute_no_params = _wrap_refusing(  # type: ignore[method-assign]
        dialect.do_execute_no_params
    )
    setattr(sync_engine, _INSTALLED_ATTR, True)
    return True


__all__ = [
    "READ_ONLY_OPTION",
    "ReadOnlySessionError",
    "ReadOnlySessionsUnavailable",
    "install_read_executor",
    "read_only_sessions_problem",
]
