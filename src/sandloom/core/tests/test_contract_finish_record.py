"""The post-commit dedupe-record write of a command (G-SENS-04, round 6):
shielded from its caller's cancellation, bounded, never raising."""

from __future__ import annotations

import asyncio

import pytest

from sandloom.core.module_api import _call

pytestmark = pytest.mark.asyncio


async def test_a_failing_write_never_raises_into_the_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def write() -> None:
        raise RuntimeError("store down")

    await _call._finish_record(write, "Ledger.settle")
    assert "Ledger.settle" in caplog.text


async def test_a_hanging_write_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_call, "_FINISH_TIMEOUT_S", 0.05)

    async def write() -> None:
        await asyncio.Event().wait()

    await asyncio.wait_for(_call._finish_record(write, "Ledger.settle"), 2)
    assert not _call._FINISHING


async def test_the_write_outlives_its_cancelled_caller() -> None:
    """Cancelled at every await (anyio's level-triggered scopes), the caller
    stops waiting, and the write still runs to its end."""
    started, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def write() -> None:
        started.set()
        await release.wait()
        done.set()

    caller = asyncio.create_task(_call._finish_record(write, "Ledger.settle"))
    await started.wait()
    while not caller.done():
        caller.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not done.is_set()
    release.set()
    await asyncio.wait_for(done.wait(), 2)
