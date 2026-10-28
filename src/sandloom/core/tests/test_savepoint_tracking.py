"""Savepoint tracking stays bounded (GLM review finding 15).

Every rolled-back transaction of a session was appended to a list in
``session.info`` for the session's whole life and scanned linearly for each
commit-time callback: a long-lived session with many ``begin_nested()``
rollbacks grew it without bound. It is now a weak set: membership is O(1)
and transactions nobody references any more (no queued callback holds them)
drop out.
"""

from __future__ import annotations

import gc

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from sandloom.core.unit_of_work import savepoints


def _session() -> Session:
    engine = create_engine("sqlite://")
    return Session(engine)


def test_rolled_back_savepoint_kills_its_token_and_survivors_live() -> None:
    with _session() as session:
        session.execute(text("SELECT 1"))
        kept = savepoints.savepoint_token(session)
        nested = session.begin_nested()
        doomed = savepoints.savepoint_token(session)
        nested.rollback()
        assert not savepoints.token_survived(session, doomed)
        assert savepoints.token_survived(session, kept)
        assert savepoints.token_survived(session, None)


def test_rolled_back_transactions_do_not_accumulate() -> None:
    with _session() as session:
        session.execute(text("SELECT 1"))
        savepoints.savepoint_token(session)  # installs the listener
        for _ in range(500):
            session.begin_nested().rollback()
        gc.collect()
        tracked = session.info.get(savepoints._ROLLED_BACK_KEY) or ()
        assert len(tracked) < 10
