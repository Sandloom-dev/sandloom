"""Savepoint tracking for after-commit callbacks (relational paradigm only).

``sandloom.core.unit_of_work.on_commit`` must not run a callback registered inside a
``begin_nested()`` SAVEPOINT that later rolled back — exactly as an event
published there is never dispatched. A callback is tagged with the innermost
transaction active when it was registered; a session listener records every
transaction that rolls back, and at commit a callback survives only if neither
its transaction nor any ancestor rolled back.
"""

from __future__ import annotations

import weakref
from typing import Any

_ROLLED_BACK_KEY = "sandloom_rolled_back_transactions"
_LISTENING_KEY = "sandloom_rollback_listener"


def _sync_session(session: Any) -> Any:
    return getattr(session, "sync_session", session)


def _ensure_listener(sync_session: Any) -> None:
    if sync_session.info.get(_LISTENING_KEY):
        return
    from sqlalchemy import event  # noqa: PLC0415 - optional: relational sessions only (the UoW engine imports no SQLAlchemy)

    def _record(sess: Any, previous_transaction: Any) -> None:
        # A WEAK set: O(1) membership, and a rolled-back transaction nobody
        # references any more (no queued callback's token, no live child)
        # drops out — a long-lived session with many savepoint rollbacks no
        # longer grows this without bound. A token keeps its own transaction
        # (and, via ``parent``, its ancestors) alive, so every transaction a
        # survival check can still ask about stays recorded.
        rolled_back = sess.info.get(_ROLLED_BACK_KEY)
        if rolled_back is None:
            rolled_back = sess.info[_ROLLED_BACK_KEY] = weakref.WeakSet()
        rolled_back.add(previous_transaction)

    event.listen(sync_session, "after_soft_rollback", _record)
    sync_session.info[_LISTENING_KEY] = True


def savepoint_token(session: Any) -> Any:
    """The innermost active transaction of ``session`` (opaque token)."""
    sync = _sync_session(session)
    _ensure_listener(sync)
    return sync.get_nested_transaction() or sync.get_transaction()


def token_survived(session: Any, token: Any) -> bool:
    """True unless ``token``'s transaction (or an ancestor) rolled back."""
    if token is None:
        return True
    sync = _sync_session(session)
    rolled_back = sync.info.get(_ROLLED_BACK_KEY) or []
    if not rolled_back:
        return True
    txn = token
    while txn is not None:
        if txn in rolled_back:
            return False
        txn = getattr(txn, "parent", None)
    return True


__all__ = ["savepoint_token", "token_survived"]
