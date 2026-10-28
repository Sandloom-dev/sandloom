"""``before_commit``: a last-moment check that aborts the Unit of Work.

The kernel half of ADR-0003 G2's cross-service case: an authorization input
owned by another service (identity's revocation / block marks) is re-read
immediately before the transaction commits; a failing check rolls the
transaction back instead of committing it.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sandloom.core.auth import revocation
from sandloom.core.di import before_commit, subject_commit_check
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.testing import make_user_headers
from sandloom.core.tests.test_revocation_windows import mem_cache, skew  # noqa: F401
from sandloom.core.unit_of_work.scope import _bind_uow_scope

pytestmark = pytest.mark.asyncio


class _Session:
    """Just enough of an AsyncSession for the UoW (records the outcome)."""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.calls: list[str] = []

    async def begin(self) -> None:
        self.calls.append("begin")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def close(self) -> None:
        self.calls.append("close")


class _Boom(Exception):
    pass


async def _run_uow(*checks: Any) -> _Session:
    session = _Session()
    uow = SQLAlchemyUnitOfWork(session_factory=lambda: session)  # type: ignore[arg-type]
    async with uow:
        with _bind_uow_scope(session, "default"):  # type: ignore[arg-type]
            for check in checks:
                await before_commit(check)
            session.calls.append("handler-done")
    return session


async def test_checks_run_in_order_after_the_handler_and_before_commit() -> None:
    order: list[str] = []

    async def first() -> None:
        order.append("first")

    session = await _run_uow(first, lambda: order.append("second"))

    assert order == ["first", "second"]
    assert session.calls == ["begin", "handler-done", "commit", "close"]


async def test_a_failing_check_rolls_back_instead_of_committing() -> None:
    async def refuse() -> None:
        raise _Boom()

    session = _Session()
    uow = SQLAlchemyUnitOfWork(session_factory=lambda: session)  # type: ignore[arg-type]
    with pytest.raises(_Boom):
        async with uow:
            with _bind_uow_scope(session, "default"):  # type: ignore[arg-type]
                await before_commit(refuse)

    assert "commit" not in session.calls
    assert session.calls == ["begin", "rollback", "close"]


async def test_outside_a_unit_of_work_the_check_runs_at_once() -> None:
    ran: list[bool] = []
    await before_commit(lambda: ran.append(True))
    assert ran == [True]


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "method": "POST", "path": "/"})


async def test_subject_check_passes_without_a_mark_and_refuses_after_one(
    mem_cache: Any,  # noqa: F811
    skew: Any,  # noqa: F811
) -> None:
    user_id, headers = make_user_headers(uuid4())
    check = subject_commit_check(_request(headers))

    await check()  # no mark: the action is ordered before any revocation

    assert await revocation.revoke_user_tokens(user_id) is True
    with pytest.raises(HTTPException) as exc:
        await check()
    assert exc.value.status_code == 401


async def test_subject_check_sees_a_block_written_during_the_request(
    mem_cache: Any,  # noqa: F811
) -> None:
    from sandloom.core.auth.marks import _format_blocked, subject_key

    user_id, headers = make_user_headers(uuid4())
    check = subject_commit_check(_request(headers))
    await mem_cache.set(subject_key(user_id), _format_blocked(1))

    with pytest.raises(HTTPException) as exc:
        await check()
    assert exc.value.status_code == 401
    assert exc.value.detail == "Account blocked"


async def test_subject_check_is_a_no_op_without_a_bearer() -> None:
    await subject_commit_check(_request({}))()


async def test_testing_run_on_commit_runs_the_before_commit_checks_first() -> None:
    """``sandloom.core.testing.run_on_commit`` treats the test-owned UoW as
    committed: its ``before_commit`` checks must run (and may refuse) first,
    or tests on ``bind_test_uow`` / ``recording_uow`` silently skip them."""
    from sandloom.core.testing import recording_uow, run_on_commit
    from sandloom.core.unit_of_work.hooks import on_commit

    ran: list[str] = []
    with recording_uow() as session:
        await before_commit(lambda: ran.append("check"))
        await on_commit(lambda: ran.append("after"))
        await run_on_commit(session)
    assert ran == ["check", "after"]

    with recording_uow() as session:

        async def refuse() -> None:
            raise _Boom()

        await before_commit(refuse)
        await on_commit(lambda: ran.append("must-not-run"))
        with pytest.raises(_Boom):
            await run_on_commit(session)
    assert "must-not-run" not in ran
