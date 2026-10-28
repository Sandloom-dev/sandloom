"""external_call() / release_check() open their window atomically with the
"no unit open" check (G-UNIT-02, G-BILLING-19): a unit another task tries to
open while the window waits for the contract read scope to drain is refused,
never admitted next to the external call or the final re-read."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from sandloom.core.unit_of_work.intent import (
    IntentState,
    ResolvedAccount,
    external_call,
    release_check,
)
from sandloom.core.unit_of_work.request import (
    RequestScope,
    UnitRuleError,
    bind_request_scope,
)

pytestmark = pytest.mark.asyncio


async def test_a_unit_racing_the_window_entry_is_refused() -> None:
    scope = RequestScope(core=None, policy=None)
    intent = scope.intent = IntentState()
    outcomes: list[str] = []

    async def child_opens_a_unit() -> None:
        try:
            intent.on_unit_open(object())
        except UnitRuleError:
            outcomes.append("refused")
        else:
            outcomes.append("admitted")

    async def drain_read_scope() -> None:
        # the await of the window's entry: another task runs meanwhile
        await asyncio.create_task(child_opens_a_unit())

    intent.read_scope_release = drain_read_scope
    with bind_request_scope(scope):
        async with external_call("psp.create"):
            assert intent.open_units == []
        await release_check(ResolvedAccount(kind="user", id=uuid.uuid4()))
    assert outcomes == ["refused", "refused"]
    assert intent.open_units == []
    assert [kind for kind, _ in intent.events] == ["external", "release"]


async def test_a_unit_left_open_by_the_drain_refuses_the_window() -> None:
    """Defence in depth: a unit that is open after the drain (whatever opened
    it) refuses the window instead of running beside it."""
    scope = RequestScope(core=None, policy=None)
    intent = scope.intent = IntentState()

    async def drain_leaves_a_unit() -> None:
        intent.open_units.append(object())  # bypassing on_unit_open

    intent.read_scope_release = drain_leaves_a_unit
    with bind_request_scope(scope):
        with pytest.raises(UnitRuleError, match="SANDLOOM-R008"):
            async with external_call("psp.create"):
                pass
        assert intent.external_active == 0
        intent.open_units.clear()
        with pytest.raises(UnitRuleError, match="SANDLOOM-R008"):
            await release_check(ResolvedAccount(kind="user", id=uuid.uuid4()))
        assert intent.release_active is False
