"""Read-only statement policy of ``ReadOnlyDbSession`` (``DB_READ_ONLY_SESSIONS``).

``ReadOnlyDbSession`` declares ``postgresql_readonly=True`` on its option-engine
and uses this dialect hook as a second, early refusal layer for writes, locking
reads and raw statements. The server's BEGIN READ ONLY remains authoritative
for SELECT functions: SQL syntax alone cannot prove a read is side-effect free.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.selectable import CompoundSelect, Select

_INSTALLED_ATTR = "_sandloom_read_executor_installed"
#: Execution option set by the READ-ONLY session policy (``ReadOnlyDbSession``):
#: permitted reads keep BEGIN READ ONLY; other statements are refused early.
READ_ONLY_OPTION = "sandloom_read_only"


class ReadOnlySessionError(RuntimeError):
    """A write / locking read / raw statement on a read-only session."""


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


def install_read_executor(engine: AsyncEngine) -> None:
    """Install the read-only statement policy on an asyncpg ``engine`` (idempotent)."""
    sync_engine = engine.sync_engine
    if getattr(sync_engine, _INSTALLED_ATTR, False):
        return
    dialect = sync_engine.dialect
    if dialect.driver != "asyncpg":
        return
    dialect.do_execute = _wrap(dialect.do_execute)  # type: ignore[method-assign]
    dialect.do_executemany = _wrap_refusing(dialect.do_executemany)  # type: ignore[method-assign]
    dialect.do_execute_no_params = _wrap_refusing(  # type: ignore[method-assign]
        dialect.do_execute_no_params
    )
    setattr(sync_engine, _INSTALLED_ATTR, True)


__all__ = [
    "READ_ONLY_OPTION",
    "ReadOnlySessionError",
    "install_read_executor",
]
