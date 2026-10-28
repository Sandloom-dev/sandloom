"""The write guard of a READ contract call (relational sessions).

While a read contract's body runs on its provider session, a write through
that session raises; after the body, anything it queued (published events,
``on_commit`` / ``before_commit`` effects, ORM changes, audit rows, DML) is
reported — its Unit of Work is always rolled back, so a write would otherwise
be dropped silently while the call returns success. Used by the contract
runtime (:mod:`sandloom.core.module_api._call`, ``_read_scope``); the relational
detection lives in :mod:`sandloom.core.sql_alchemy.read_guard`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any, cast

from sandloom.core.sql_alchemy import read_guard
from sandloom.core.unit_of_work.hooks import _BEFORE_COMMIT_KEY, _ON_COMMIT_KEY
from sandloom.core.unit_of_work.scope import _OUTBOX_PENDING_KEY


def _guard_read_contract(
    session: Any, make_error: Callable[[str], BaseException]
) -> AbstractContextManager[None]:
    """While a READ contract's body runs: a write through ``session`` raises.

    Relational sessions only (a flush or a DML statement raises
    ``make_error(what)`` at the write); any other session is not guarded
    here — :func:`_read_contract_writes` still reports its queued effects.
    """
    if not hasattr(session, "sync_session"):
        return nullcontext()
    return cast(
        "AbstractContextManager[None]",
        read_guard.guard_read_session(session, make_error),
    )


def _read_contract_writes(session: Any) -> str | None:
    """What a READ contract tried to write through ``session``, or ``None``.

    Its UoW is always rolled back, so any write would be silently dropped
    while the call returns success: published events, queued ``on_commit`` /
    ``before_commit`` effects and (relational) ORM changes, audit rows and
    DML statements — including one the body caught and swallowed.
    """
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return None
    if info.get(_OUTBOX_PENDING_KEY):
        return "published events"
    if info.get(_ON_COMMIT_KEY) or info.get(_BEFORE_COMMIT_KEY):
        return "queued on_commit / before_commit effects"
    if not hasattr(session, "sync_session"):
        return None
    return read_guard.pending_writes(session)
