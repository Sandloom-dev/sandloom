"""Write detection for READ contracts (relational paradigm only).

A read contract's Unit of Work is always rolled back. A write it attempts —
an ORM ``add`` / change / ``delete`` (flushed or not), a Core
``insert`` / ``update`` / ``delete`` through the session, an audit row — would
be silently discarded while the call returns success. While a read contract's
body runs, :func:`guard_read_session` makes such a write fail loudly at the
point it happens (a flush, a DML ``execute``), and :func:`pending_writes`
reports what is still queued when the body returns. A violation the body
caught and swallowed is remembered and reported at the end too.

Not covered: a raw ``text()`` statement that writes (its kind is opaque to the
client). PostgreSQL ``READ ONLY`` sessions are the backstop for that.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event

from sandloom.core.sql_alchemy import effect_rows

_GUARD_KEY = "sandloom_read_contract_guard"
_VIOLATION_KEY = "sandloom_read_contract_violation"
_LISTENING_KEY = "sandloom_read_guard_listening"

ErrorFactory = Callable[[str], BaseException]


def _sync(session: Any) -> Any:
    return getattr(session, "sync_session", session)


def _orm_changes(sync: Any) -> str | None:
    trigger = sync.info.get(effect_rows._TRIGGER_KEY)
    if sync.info.get(effect_rows._ROWS_KEY):
        return "wrote audit / outbox rows"
    if any(obj is not trigger for obj in sync.new):
        return "added ORM objects"
    if sync.deleted:
        return "deleted ORM objects"
    if any(sync.is_modified(obj) for obj in sync.dirty):
        return "modified ORM objects"
    return None


def _violate(sync: Any, what: str) -> None:
    factory: ErrorFactory | None = sync.info.get(_GUARD_KEY)
    if factory is None:
        return
    sync.info.setdefault(_VIOLATION_KEY, what)
    raise factory(what)


def _before_flush(sync: Any, _flush_context: Any, _instances: Any) -> None:
    if sync.info.get(_GUARD_KEY) is None:
        return
    what = _orm_changes(sync)
    if what is not None:
        _violate(sync, what)


def _do_orm_execute(state: Any) -> None:
    sync = state.session
    if sync.info.get(_GUARD_KEY) is None:
        return
    if state.is_insert or state.is_update or state.is_delete:
        _violate(sync, "executed an INSERT / UPDATE / DELETE")


def _ensure_listening(sync: Any) -> None:
    if sync.info.get(_LISTENING_KEY):
        return
    event.listen(sync, "before_flush", _before_flush)
    event.listen(sync, "do_orm_execute", _do_orm_execute)
    sync.info[_LISTENING_KEY] = True


@contextmanager
def guard_read_session(session: Any, make_error: ErrorFactory) -> Iterator[None]:
    """While active, a write through ``session`` raises ``make_error(what)``."""
    sync = _sync(session)
    if not hasattr(sync, "new"):  # not a relational session (test double)
        yield
        return
    _ensure_listening(sync)
    previous = sync.info.get(_GUARD_KEY)
    sync.info[_GUARD_KEY] = make_error
    sync.info.pop(_VIOLATION_KEY, None)
    try:
        yield
    finally:
        if previous is None:
            sync.info.pop(_GUARD_KEY, None)
        else:
            sync.info[_GUARD_KEY] = previous


def pending_writes(session: Any) -> str | None:
    """What a read contract wrote (pending or attempted), or ``None``."""
    sync = _sync(session)
    info = getattr(sync, "info", None)
    if not isinstance(info, dict):
        return None
    violation = info.pop(_VIOLATION_KEY, None)
    if violation is not None:
        return str(violation)
    if not hasattr(sync, "new"):
        return None
    return _orm_changes(sync)


__all__ = ["guard_read_session", "pending_writes"]
