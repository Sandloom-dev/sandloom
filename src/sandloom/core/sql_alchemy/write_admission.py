"""Write admission at the cursor boundary and the kernel session class
(spec §4.3, §4.4).

**The cursor hook.** :func:`install_write_admission` wraps the engine
dialect's three statement entry points — ``do_execute``, ``do_executemany``
and ``do_execute_no_params`` — through which every statement SQLAlchemy sends
passes: ORM, Core, ``text()`` and ``Connection.exec_driver_sql()`` (also
``(await session.connection()).exec_driver_sql(...)`` under ``unscoped()``).
It is the cursor boundary ``before_cursor_execute`` would see, without the
engine event (an engine-level ``ConnectionEvents`` listener moves every
connection onto SQLAlchemy's instrumented execution path, ~160k instructions
per statement; the same reason :mod:`sandloom.core.sql_alchemy.tenant_rls` wraps
the dialect). Every kernel-constructed engine gets it (the drivers are the
only constructors of engines). Per statement, in a request's admission scope:

1. the unit is identified from the connection (the kernel session that
   checked it out; a caller-owned connection is its own unit);
2. best-effort ownership: a statement from a task that does not own the unit
   is refused (SANDLOOM-R013), a statement through an exited unit too (SANDLOOM-R002);
3. a statement that WRITES is admitted (the first writing unit becomes the
   request's writer) or refused before it reaches the cursor (SANDLOOM-R007).

**Classification is conservative** (:func:`is_write_statement`): a statement
is read-only only if it provably is — the execution context reports none of
``isinsert`` / ``isupdate`` / ``isdelete`` / ``isddl`` AND its final SQL text
(comments and string literals stripped) is one ``SELECT`` with no top-level
``INTO``, a ``WITH`` whose CTE bodies hold no DML and whose final statement is
such a ``SELECT``, a ``SHOW``, or an ``EXPLAIN`` without ``ANALYZE``.
Transaction control (``BEGIN`` / ``COMMIT`` / ``ROLLBACK`` / ``SAVEPOINT`` /
``RELEASE``) changes no data and is not a write. Everything else — ``SELECT …
INTO``, ``EXPLAIN ANALYZE``, DML in a CTE, ``COPY``, ``CALL``, ``DO``,
``SET``, ``LOCK``, several statements in one string, anything unrecognised —
is a write. Out of scope, stated: side effects of functions called inside a
read statement, and the raw DBAPI handles (``raw_connection()``,
``driver_connection``), which modules may not reach (SANDLOOM-C003).

**The kernel session class** (:class:`KernelSession`, the ``sync_session_class``
of every kernel session factory) tags each connection with its unit, refuses
use of an exited unit (no auto-begin of a new transaction through a captured
reference), and checks the owner task at its public method boundary (the
best-effort detection of a session object passed to another task).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Session

import sandloom.core.unit_of_work.request as _request
import sandloom.core.unit_of_work.risk as _risk
from sandloom.core.config.settings import settings
from sandloom.core.sql_alchemy.money_ledger import MONEY_TABLES as _MONEY_TABLES
from sandloom.core.unit_of_work.effects import UnitOfWorkRequiredError
from sandloom.core.unit_of_work.scope import _DB_NAME_KEY

#: Execution option exempting a KERNEL statement from write admission (the
#: kernel units' own bookkeeping statements only).
EXEMPT_OPTION = "sandloom_admission_exempt"
#: Engine attribute marking the hook as installed (idempotency).
_INSTALLED_ATTR = "_sandloom_write_admission_installed"
#: Connection attribute: the kernel session (unit) that checked it out.
UNIT_ATTR = "_sandloom_unit"

_OWNER_KEY = _request.OWNER_KEY
_CLOSED_KEY = _request.CLOSED_KEY
_WRITES_KEY = _request.WRITES_KEY
_admission_get = _request.admission_get
_current_task = _request._task
_scope_get = _request.request_scope_get


# --------------------------------------------------------------------------- #
# Statement classification
# --------------------------------------------------------------------------- #
_STRIP = re.compile(
    r"""
      --[^\n]*                       # line comment
    | /\*.*?\*/                      # block comment
    | \$([A-Za-z_][A-Za-z0-9_]*)?\$.*?\$\1\$   # dollar-quoted string
    | [Ee]?'(?:[^']|'')*'            # string literal (E'' too)
    | "(?:[^"]|"")*"                 # quoted identifier
    """,
    re.S | re.X,
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*|[();]")
_DML = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})
_TX_CONTROL = frozenset(
    {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "START", "END", "ABORT"}
)
#: Classification cache: compiled SQL strings recur (SQLAlchemy caches them).
_CACHE: dict[str, bool] = {}
_CACHE_MAX = 4096


def _tokens(sql: str) -> list[str] | None:
    """Upper-cased word / paren / ``;`` tokens of ``sql`` with comments and
    literals removed; ``None`` for an unterminated comment or literal."""
    stripped = _STRIP.sub(" ", sql)
    if "'" in stripped or "/*" in stripped or '"' in stripped:
        return None  # an unterminated literal / comment: not provably read-only
    return [t.upper() for t in _WORD.findall(stripped)]


def _select_is_read(tokens: list[str], start: int) -> bool:
    """``tokens[start:]`` is one SELECT with no top-level INTO."""
    depth = 0
    for tok in tokens[start:]:
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and tok == "INTO":
            return False
    return True


def _one_statement(tokens: list[str]) -> list[str] | None:
    """The tokens of ONE statement (a trailing ``;`` dropped, leading parens
    of ``(SELECT …) UNION …`` skipped); ``None`` for several statements."""
    if ";" in tokens[:-1]:
        return None
    if tokens[-1] == ";":
        tokens = tokens[:-1]
    while tokens and tokens[0] == "(":
        tokens = tokens[1:]
    return tokens


def _with_writes(tokens: list[str]) -> bool:
    """A ``WITH`` statement writes unless no CTE body holds DML and the final
    statement (after the last top-level CTE body) is a read ``SELECT``."""
    if _DML.intersection(tokens):
        return True
    depth, last_close = 0, -1
    for i, tok in enumerate(tokens):
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth == 0:
                last_close = i
    rest = tokens[last_close + 1 :]
    return not rest or rest[0] != "SELECT" or not _select_is_read(rest, 1)


def classify_sql(sql: str) -> bool:
    """True when ``sql`` WRITES (or is not provably read-only)."""
    raw = _tokens(sql)
    if not raw:
        return raw is None
    tokens = _one_statement(raw)
    if not tokens:
        return tokens is None
    head = tokens[0]
    if head in _TX_CONTROL or head == "SHOW":
        return False
    if head == "SELECT":
        return not _select_is_read(tokens, 1)
    if head == "EXPLAIN":
        return "ANALYZE" in tokens or "ANALYSE" in tokens
    if head == "WITH":
        return _with_writes(tokens)
    return True


def is_write_statement(context: Any, statement: Any) -> bool:
    """The conservative write test of the cursor hook (module docstring)."""
    if context is not None and (
        context.isinsert or context.isupdate or context.isdelete or context.isddl
    ):
        return True
    if not isinstance(statement, str):
        return True
    cached = _CACHE.get(statement)
    if cached is None:
        cached = classify_sql(statement)
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[statement] = cached
    return cached


# --------------------------------------------------------------------------- #
# The cursor hook
# --------------------------------------------------------------------------- #
#: Best-effort owner detection on session methods and at the cursor
#: (``DB_SESSION_OWNER_CHECKS``; read once when an engine is installed).
_owner_checks = False


def _check_unit(info: dict[str, Any]) -> None:
    if info.get(_CLOSED_KEY):
        raise UnitOfWorkRequiredError(
            "a statement through a Unit of Work that has exited (a captured "
            "session or connection): open a unit (SANDLOOM-R002)"
        )
    if not _owner_checks:
        return
    owner = info.get(_OWNER_KEY)
    if owner is not None and owner is not _current_task():
        raise _request.ConcurrentSessionUseError(
            "a statement on another task's unit (a session or connection "
            "passed to another task): open that task's own unit with "
            "`async with db_session() as session:` (SANDLOOM-R013)"
        )


def _check(context: Any, statement: Any) -> None:
    """Per statement (the module docstring); the common read in a request
    costs a few attribute and dictionary reads."""
    if context is None:
        return
    conn = context.root_connection
    unit = getattr(conn, UNIT_ATTR, None)
    if unit is not None:
        info = unit.info
        if info.get(_CLOSED_KEY) or _owner_checks:
            _check_unit(info)
    adm = _admission_get()
    if adm is None:
        return
    # is_write_statement, inlined (the classification cache hit first)
    if not (
        context.isinsert or context.isupdate or context.isdelete or context.isddl
    ) and isinstance(statement, str):
        cached = _CACHE.get(statement)
        if cached is None:
            cached = is_write_statement(None, statement)
        if not cached:
            return
    if context.execution_options.get(EXEMPT_OPTION):
        return
    _admit_write(context, conn, unit, adm)


def _admit_write(context: Any, conn: Any, unit: Any, adm: Any) -> None:
    """A write statement in a request's admission scope (spec §4.4)."""
    scope = _scope_get()
    if scope is not None:
        if scope.intent is not None:
            scope.intent.check_write_allowed()
        if _MONEY_TABLES and context.isinsert:
            _note_ledger_insert(context, unit, adm)
    if unit is not None:
        info = unit.info
        adm.admit(unit, info.get(_DB_NAME_KEY), "a write statement")
        info[_WRITES_KEY] = info.get(_WRITES_KEY, 0) + 1
    else:
        adm.admit(conn, None, "a write statement")


def _note_ledger_insert(context: Any, unit: Any, adm: Any) -> None:
    """An INSERT into a ``MoneyLedger`` table in a request: a money effect,
    deduped by the ledger's unique operation key (ADR-0009 items 7, 10). A
    money effect in an opted-out secondary (best-effort 1PC) is refused
    (SANDLOOM-B014): it must commit with the write store."""
    statement = getattr(context.compiled, "statement", None)
    name = getattr(getattr(statement, "table", None), "name", None)
    if name not in _MONEY_TABLES:
        return
    database = unit.info.get(_DB_NAME_KEY) if unit is not None else None
    if database is not None and database in adm.secondaries:
        raise MoneyEffectOutsideWriteStoreError(
            f"a money ledger insert ({name}) in the opted-out secondary "
            f"{database!r}: a money effect commits with the request's write "
            "store, never in an allow_multi_db_write secondary (SANDLOOM-B014)"
        )
    _risk.note_effect("money", f"ledger:{name}", deduped=True)


class MoneyEffectOutsideWriteStoreError(RuntimeError):
    """A money effect in an ``allow_multi_db_write`` secondary (SANDLOOM-B014)."""

    sandloom_code = "SANDLOOM-B014"


def _wrap_execute(original: Callable[..., Any]) -> Callable[..., Any]:
    def do_execute(
        cursor: Any, statement: Any, parameters: Any, context: Any = None
    ) -> Any:
        _check(context, statement)
        return original(cursor, statement, parameters, context)

    return do_execute


def _wrap_execute_no_params(original: Callable[..., Any]) -> Callable[..., Any]:
    def do_execute_no_params(cursor: Any, statement: Any, context: Any = None) -> Any:
        _check(context, statement)
        return original(cursor, statement, context)

    return do_execute_no_params


def install_write_admission(engine: AsyncEngine | Engine) -> None:
    """Install the cursor hook on ``engine`` (idempotent)."""
    global _owner_checks
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    _owner_checks = bool(getattr(settings, "db_session_owner_checks", False))
    if _owner_checks:
        _install_method_guards()
    if getattr(sync_engine, _INSTALLED_ATTR, False):
        return
    dialect = sync_engine.dialect
    dialect.do_execute = _wrap_execute(dialect.do_execute)  # type: ignore[method-assign]
    dialect.do_executemany = _wrap_execute(dialect.do_executemany)  # type: ignore[method-assign]
    dialect.do_execute_no_params = _wrap_execute_no_params(  # type: ignore[method-assign]
        dialect.do_execute_no_params
    )
    setattr(sync_engine, _INSTALLED_ATTR, True)


def is_write_admission_installed(engine: AsyncEngine | Engine) -> bool:
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    return bool(getattr(sync_engine, _INSTALLED_ATTR, False))


# --------------------------------------------------------------------------- #
# The kernel session class
# --------------------------------------------------------------------------- #
def _guard(session: Session) -> None:
    info = session.info
    if info.get(_CLOSED_KEY):
        raise UnitOfWorkRequiredError(
            "this session's Unit of Work has exited (a captured reference): open "
            "a unit, never reuse a finished one (SANDLOOM-R002)"
        )
    if not _owner_checks:
        return
    owner = info.get(_OWNER_KEY)
    if owner is not None and owner is not _current_task():
        raise _request.ConcurrentSessionUseError(
            "a session used from a task that does not own its unit (passing a "
            "session to another task is unsupported): open that task's own "
            "unit with `async with db_session() as session:` (SANDLOOM-R013)"
        )


class KernelSession(Session):
    """The sync session behind every kernel ``AsyncSession`` (module docstring).

    Nothing is overridden on the hot path: every connection a transaction
    takes is attributed to the unit by the ``after_begin`` listener below, an
    exited unit's session switches to :class:`ClosedKernelSession` (every
    entry refuses), and the best-effort owner checks are installed only with
    ``DB_SESSION_OWNER_CHECKS`` (:func:`_install_method_guards`).
    """


def _guarded_connection_for_bind(
    self: Session, engine: Any, execution_options: Any = None, **kw: Any
) -> Any:
    _guard(self)
    return Session._connection_for_bind(self, engine, execution_options, **kw)


def _guarded(name: str) -> Any:
    base = getattr(Session, name)

    def method(self: Session, *args: Any, **kwargs: Any) -> Any:
        _guard(self)
        return base(self, *args, **kwargs)

    method.__name__ = name
    method.__qualname__ = f"KernelSession.{name}"
    return method


#: Session methods guarded for best-effort owner detection (SANDLOOM-R013) and the
#: in-memory closed-unit refusal. Installed only with DB_SESSION_OWNER_CHECKS
#: (the §10.4 gate moved them off the hot path): every I/O path is still
#: refused on a closed unit by ``_connection_for_bind`` and the cursor hook.
_GUARDED_METHODS = (
    "add",
    "add_all",
    "delete",
    "merge",
    "expunge",
    "expunge_all",
    "refresh",
    "flush",
    "begin_nested",
    "get",
)


def _install_method_guards() -> None:
    for name in _GUARDED_METHODS:
        if name not in KernelSession.__dict__:
            setattr(KernelSession, name, _guarded(name))
    if "_connection_for_bind" not in KernelSession.__dict__:
        # the owner check at every statement's checkout (execute / connection)
        KernelSession._connection_for_bind = _guarded_connection_for_bind  # type: ignore[method-assign]


def _refuse_closed(name: str) -> Any:
    def method(self: Session, *args: Any, **kwargs: Any) -> Any:
        raise UnitOfWorkRequiredError(
            f"session.{name}(): this session's Unit of Work has exited (a "
            "captured reference): open a unit, never reuse a finished one "
            "(SANDLOOM-R002)"
        )

    method.__name__ = name
    method.__qualname__ = f"ClosedKernelSession.{name}"
    return method


class ClosedKernelSession(KernelSession):
    """The class an exited unit's session takes (:func:`mark_unit_closed`):
    every in-memory mutation and every I/O entry refuses (SANDLOOM-R002), at no
    cost to the open sessions' hot path (G-UNIT-01: a closed unit refuses
    reuse whatever ``DB_SESSION_OWNER_CHECKS`` says)."""


for _name in (*_GUARDED_METHODS, "execute", "scalar", "scalars", "connection"):
    setattr(ClosedKernelSession, _name, _refuse_closed(_name))
del _name


def mark_unit_closed(session: Any) -> None:
    """An exited unit's session refuses any later use (kernel sessions only;
    ``session`` is the ``AsyncSession`` or its sync session)."""
    sync = getattr(session, "sync_session", session)
    if type(sync) is KernelSession:
        sync.__class__ = ClosedKernelSession


def _attribute_connection(session: Session, _transaction: Any, conn: Any) -> None:
    """Attribute every connection a kernel session's transaction takes to the
    unit, whatever path took it: ``Session._connection_for_bind`` covers
    ``execute()``/``connection()``, but a FLUSH asks the session transaction
    directly, so an ORM-only write would otherwise reach the cursor hook
    unattributed (no owner check, no write count, no re-check)."""
    if getattr(conn, UNIT_ATTR, None) is not session:
        scope = _scope_get()
        if scope is not None and scope.intent is not None:
            scope.intent.check_checkout()
        setattr(conn, UNIT_ATTR, session)


# once per connection per transaction (not per statement)
event.listen(KernelSession, "after_begin", _attribute_connection)


__all__ = [
    "EXEMPT_OPTION",
    "ClosedKernelSession",
    "mark_unit_closed",
    "UNIT_ATTR",
    "KernelSession",
    "classify_sql",
    "install_write_admission",
    "is_write_admission_installed",
    "is_write_statement",
]
