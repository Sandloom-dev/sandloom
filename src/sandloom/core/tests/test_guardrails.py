from __future__ import annotations

import asyncio
import logging
import time

import pytest

from sandloom.core.config.settings import settings as global_settings
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.observability.logging import init_logging
from sandloom.core.testing import register_event_schemas


@pytest.mark.asyncio
async def test_event_payload_size_guard(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bus: InMemoryEventBus,
) -> None:
    # enforce tiny size
    monkeypatch.setattr(global_settings, "event_payload_max_bytes", 50)
    # small ok
    register_event_schemas(["core.system.heartbeat.v1"], target=bus)
    await bus.publish_event("core.system.heartbeat.v1", {"x": "ok"})
    # large fails
    big_payload = {"data": "x" * 100}
    with pytest.raises(ValueError):
        await bus.publish_event("core.system.heartbeat.v1", big_payload)


@pytest.mark.asyncio
async def test_event_callback_time_budget(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bus: InMemoryEventBus,
) -> None:
    monkeypatch.setattr(global_settings, "event_callback_time_budget_seconds", 0.05)

    slow_called = False

    async def slow_cb(msg: dict[str, object]) -> None:
        nonlocal slow_called
        slow_called = True
        await asyncio.sleep(0.08)

    register_event_schemas(["core.system.heartbeat.v1"], target=bus)
    await bus.subscribe("core.system.heartbeat.v1", slow_cb)
    with caplog.at_level(logging.WARNING):
        await bus.publish_event("core.system.heartbeat.v1", {"x": 1})
        await bus.drain()
    assert slow_called
    # check warning message present
    assert any(
        "Callback slow channel=core.system.heartbeat.v1" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_safe_task_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.observability.guardrails import safe_task

    async def boom() -> int:
        raise RuntimeError("fail")

    captured: list[str] = []

    def policy(exc: BaseException) -> None:
        captured.append(str(exc))

    result = await safe_task(boom, on_error=policy)
    assert result is None
    assert captured and "fail" in captured[0]

    start = time.perf_counter()

    async def slow() -> int:
        await asyncio.sleep(0.02)
        return 5

    out = await safe_task(slow, time_budget=0.001)
    assert out == 5
    # elapsed just to ensure call executed; rely on log for warning (not asserted)
    assert (time.perf_counter() - start) >= 0.02


def test_redaction_formatter_masks_sensitive(caplog: pytest.LogCaptureFixture) -> None:
    init_logging()
    logger = logging.getLogger("sandloom.core.testredact")
    with caplog.at_level(logging.INFO):
        logger.info({"password": "abc123", "note": "ok"})
    text = caplog.text
    assert "***" in text
    assert "abc123" not in text


def test_guardrails_module_has_a_docstring() -> None:
    # GLM review #12: the docstring sat after the imports (a no-op string
    # expression), so the module had no __doc__.
    from sandloom.core.observability import guardrails

    assert guardrails.__doc__ and "guardrail" in guardrails.__doc__.lower()
