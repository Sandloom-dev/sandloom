"""``saga_status(..., with_row=loader)`` (dx-sp-s1-view-status).

The call reads the saga instance first and awaits the loader after it, so a
terminal status is never paired with a row older than the transition that
produced it. The projection half of the task (``Projection.view``) is tested
in ``tests/sdk/test_sdk_projection_ops.py``, next to its store fixtures.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

import sandloom.core.sagas as sagas_package
from sandloom.core.sagas import SagaHandle, saga_status
from sandloom.core.testing.fixtures import *  # noqa: F403 - saga_harness + DB fixtures
from sandloom.core.tests.saga_fixtures import (
    Ledger,
    enroll_input,
    enroll_saga,
    install_fakes,
)
from sandloom.core.unit_of_work import db_session

ENROLL = enroll_saga("enroll_with_row")


@pytest_asyncio.fixture
async def finished(saga_harness: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A completed saga ``wr-1``; ``saga_status`` reads through the harness
    runtime, and every instance read is logged in the returned list."""
    install_fakes(saga_harness, Ledger())
    await saga_harness.run(ENROLL, saga_id="wr-1", input=enroll_input())
    log: list[str] = []
    real_status = saga_harness.runtime.status

    async def status(definition: Any, saga_id: object) -> SagaHandle | None:
        log.append("status")
        return await real_status(definition, saga_id)

    monkeypatch.setattr(saga_harness.runtime, "status", status)
    monkeypatch.setattr(
        sagas_package, "runtime_of", lambda core=None: saga_harness.runtime
    )
    return log


async def test_with_row_reads_the_instance_first_then_awaits_the_loader(
    finished: list[str],
) -> None:
    async def loader() -> str:
        finished.append("row")
        return "the row"

    async with db_session():
        handle, row = await saga_status(ENROLL, "wr-1", with_row=loader)

    assert finished == ["status", "row"]
    assert handle is not None and handle.status == "completed"
    assert row == "the row"


async def test_a_saga_that_never_started_pairs_none_with_the_row(
    finished: list[str],
) -> None:
    async def loader() -> int:
        finished.append("row")
        return 7

    async with db_session():
        handle, row = await saga_status(ENROLL, "never-started", with_row=loader)

    assert (handle, row) == (None, 7)
    assert finished == ["status", "row"]


async def test_without_with_row_the_result_is_the_handle_alone(
    finished: list[str],
) -> None:
    async with db_session():
        handle = await saga_status(ENROLL, "wr-1")

    assert isinstance(handle, SagaHandle) and handle.status == "completed"
    assert finished == ["status"]


async def test_a_loader_that_raises_propagates_after_the_instance_read(
    finished: list[str],
) -> None:
    async def missing() -> str:
        finished.append("row")
        raise LookupError("no such row")

    with pytest.raises(LookupError, match="no such row"):
        async with db_session():
            await saga_status(ENROLL, "wr-1", with_row=missing)

    assert finished == ["status", "row"]


async def test_a_coroutine_in_place_of_a_loader_is_refused_before_any_read(
    finished: list[str],
) -> None:
    async def get_row() -> str:
        finished.append("row")
        return "never"

    not_a_loader: Any = get_row()  # the loader's result, the usual mistake
    with pytest.raises(TypeError, match="takes a loader"):
        async with db_session():
            await saga_status(ENROLL, "wr-1", with_row=not_a_loader)

    assert finished == []  # no instance read, the loader never ran
    assert not_a_loader.cr_frame is None  # closed: no "never awaited" warning
