"""``open_uow``: the one Unit-of-Work bootstrap (review map D-02).

Pins the semantics every session-opening path relies on: commit then
post-commit dispatch, rollback without dispatch, scopes bound only inside
the block and reset before the commit, enter failures rolled back.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest

from sandloom.core import tenant_context
from sandloom.core import unit_of_work as uow_mod
from sandloom.core.unit_of_work import dispatch as uow_mod_dispatch
from sandloom.core.unit_of_work import hooks as uow_mod_hooks
from sandloom.core.unit_of_work import scope as uow_mod_scope


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


class _Uow:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.session: _Session | None = None

    async def __aenter__(self) -> _Uow:
        self.session = _Session()
        self.log.append("begin")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # what the request scope looks like while the UoW ends
        self.log.append(
            f"scope-active={uow_mod_scope._current_uow_session.get() is not None}"
        )
        self.log.append("rollback" if exc else "commit")

    async def commit(self) -> None:  # pragma: no cover - UnitOfWorkContract
        raise AssertionError("open_uow never commits directly")

    async def rollback(self) -> None:  # pragma: no cover - UnitOfWorkContract
        raise AssertionError("open_uow never rolls back directly")


class _Provider:
    def outbox(self) -> object:
        return object()

    def audit(self) -> object:
        return object()


def _core(log: list[str], **settings: Any) -> Any:
    return SimpleNamespace(
        get_uow_factory=lambda name="default": lambda: _Uow(log),
        event_bus="app-bus",
        database=lambda name: _Provider(),
        settings=SimpleNamespace(**settings),
    )


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, bool]]:
    calls: list[tuple[Any, bool]] = []

    async def _dispatch(session: Any, *, wait: bool = False) -> None:
        calls.append((session, wait))

    monkeypatch.setattr(uow_mod, "_dispatch_outbox_after_commit", _dispatch)
    return calls


async def test_commit_then_dispatch(dispatched: list[tuple[Any, bool]]) -> None:
    log: list[str] = []
    async with uow_mod.open_uow(_core(log), "analytics", wait=True) as session:
        assert uow_mod_scope._current_uow_session.get() is session
        assert session.info[uow_mod_scope._DB_NAME_KEY] == "analytics"
        assert session.info[uow_mod_scope._EVENT_BUS_KEY] == "app-bus"
    assert log == ["begin", "scope-active=False", "commit"]
    assert dispatched == [(session, True)]
    assert uow_mod_scope._current_uow_session.get() is None


async def test_bus_and_factory_overrides(dispatched: list[tuple[Any, bool]]) -> None:
    log: list[str] = []
    own: list[str] = []
    async with uow_mod.open_uow(
        _core(log), factory=lambda: _Uow(own), bus="other-bus"
    ) as session:
        assert session.info[uow_mod_scope._EVENT_BUS_KEY] == "other-bus"
    assert (log, own[0], own[-1]) == ([], "begin", "commit")
    assert dispatched == [(session, False)]


async def test_body_error_rolls_back_without_dispatch(
    dispatched: list[tuple[Any, bool]],
) -> None:
    log: list[str] = []
    with pytest.raises(LookupError):
        async with uow_mod.open_uow(_core(log)):
            raise LookupError("boom")
    assert log == ["begin", "scope-active=False", "rollback"]
    assert dispatched == []


async def test_single_tenant_scope_bound_only_inside(
    dispatched: list[tuple[Any, bool]],
) -> None:
    tenant = uuid.uuid4()
    core = _core([], tenancy_mode="single", single_tenant_id=str(tenant))
    async with uow_mod.open_uow(core):
        assert tenant_context._current_tenant.get() == tenant
    assert tenant_context._current_tenant.get() is None


async def test_tenant_scope_failure_on_enter_rolls_back(
    dispatched: list[tuple[Any, bool]],
) -> None:
    log: list[str] = []
    core = _core(log, tenancy_mode="single", single_tenant_id=None)
    with pytest.raises(RuntimeError, match="SINGLE_TENANT_ID"):
        async with uow_mod.open_uow(core):
            pytest.fail("the block must not run")  # pragma: no cover
    assert log == ["begin", "scope-active=False", "rollback"]
    assert dispatched == []
    assert uow_mod_scope._current_uow_session.get() is None


async def test_public_uow_session_accessors(
    dispatched: list[tuple[Any, bool]],
) -> None:
    # GLM review: planner_adapter read _DB_NAME_KEY / _active_uow_sessions
    # directly; it now uses these accessors.
    outside = _Session()
    assert uow_mod_scope.uow_database_name(outside) is None
    assert uow_mod_scope.uow_database_name(object()) is None
    async with uow_mod.open_uow(_core([]), "analytics") as session:
        assert uow_mod_scope.uow_database_name(session) == "analytics"
        assert uow_mod_scope.is_active_uow_session(session)
        assert not uow_mod_scope.is_active_uow_session(outside)
    assert not uow_mod_scope.is_active_uow_session(session)


async def test_post_commit_callbacks_see_the_tenant_bound_at_registration(
    dispatched: list[tuple[Any, bool]],
) -> None:
    # GLM review: on_commit callbacks ran AFTER the UoW's tenant scope closed
    # on the production path but INSIDE it on the test-override path, so a
    # callback reading current_tenant_id() failed only in production. Both
    # paths now bind the tenant that was current when the callback was queued
    # (also when a TenantScope / bind_tenant block ended before the commit).
    from sandloom.core.tenant_context import bind_tenant, current_tenant_id

    seen: list[Any] = []
    single = uuid.uuid4()
    core = _core([], tenancy_mode="single", single_tenant_id=str(single))
    async with uow_mod.open_uow(core) as session:
        await uow_mod_hooks.on_commit(lambda: seen.append(current_tenant_id()))
        await uow_mod_hooks.before_commit(lambda: seen.append(current_tenant_id()))
    # the fake UoW does not run the commit checks: run them as the real one
    # does, after the tenant scope closed
    await uow_mod_hooks._run_before_commit_callbacks(session)
    await uow_mod_hooks._run_on_commit_callbacks(session)
    assert seen == [single, single]

    seen.clear()
    request_tenant = uuid.uuid4()
    async with uow_mod.open_uow(_core([])) as session:
        with bind_tenant(request_tenant):
            await uow_mod_hooks.on_commit(lambda: seen.append(current_tenant_id()))
    await uow_mod_hooks._run_on_commit_callbacks(session)
    assert seen == [request_tenant]
    assert tenant_context._current_tenant.get() is None


async def test_post_commit_dispatch_failure_never_fails_committed_uow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GLM review P1: an exception in the post-commit dispatch (bus resolution,
    # store.survived(), the dispatcher's submit) must not turn an already
    # committed UoW into an error (a request would answer 500 and a client
    # retry could re-run the effect). It is logged + counted, the on_commit
    # queue still runs, and the outbox relay delivers the committed rows.
    async def _boom(session: Any, *, wait: bool) -> None:
        raise RuntimeError("bus resolution failed")

    monkeypatch.setattr(uow_mod_dispatch, "_dispatch_pending_events", _boom)
    before = uow_mod_dispatch._METRIC_POST_COMMIT_DISPATCH_FAILED._value.get()
    ran: list[str] = []
    log: list[str] = []
    async with uow_mod.open_uow(_core(log)):
        await uow_mod_hooks.on_commit(lambda: ran.append("callback"))
    assert log[-1] == "commit"
    assert ran == ["callback"]
    assert (
        uow_mod_dispatch._METRIC_POST_COMMIT_DISPATCH_FAILED._value.get() == before + 1
    )


async def test_post_commit_dispatch_cancellation_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def _cancelled(session: Any, *, wait: bool) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(uow_mod_dispatch, "_dispatch_pending_events", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        async with uow_mod.open_uow(_core([])):
            pass


async def test_pipeline_runs_on_commit_after_the_outbox_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0005 §3: COMMIT -> outbox dispatch -> on_commit, in that order.
    order: list[str] = []

    async def _dispatch(session: Any, *, wait: bool) -> None:
        order.append(f"dispatch(wait={wait})")

    monkeypatch.setattr(uow_mod_dispatch, "_dispatch_pending_events", _dispatch)
    log: list[str] = []
    async with uow_mod.open_uow(_core(log), wait=True):
        await uow_mod_hooks.on_commit(lambda: order.append("on_commit"))
    assert log[-1] == "commit"
    assert order == ["dispatch(wait=True)", "on_commit"]


async def test_pipeline_without_outbox_stage_still_runs_on_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The test seams (get_db override, testing.run_on_commit) own the
    # transaction: dispatch=False skips stage 3 only.
    async def _never(session: Any, *, wait: bool) -> None:
        raise AssertionError("a test-owned transaction never dispatches")

    monkeypatch.setattr(uow_mod_dispatch, "_dispatch_pending_events", _never)
    ran: list[str] = []
    session = _Session()
    session.info[uow_mod_hooks._ON_COMMIT_KEY] = [
        (lambda: ran.append("callback"), None, None)
    ]
    await uow_mod._dispatch_outbox_after_commit(session, dispatch=False)  # type: ignore[arg-type]
    assert ran == ["callback"]
