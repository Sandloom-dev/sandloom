"""Two applications built in one process each serve with their own core.

``create_app`` moves the process default (``set_active_core``) to the app it
builds last; a request of the first app must still resolve the first app's
core (its event bus, units, services), and so must the background work it
starts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from sandloom.core.app_factory import create_app
from sandloom.core.eventing.event_bus import get_event_bus, set_active_event_bus
from sandloom.core.infra.active import get_active_infra, set_active_infra
from sandloom.core.runtime import get_active_core, set_active_core


@pytest.fixture
def restore_process_runtime() -> Iterator[None]:
    core, infra, bus = get_active_core(), get_active_infra(), get_event_bus()
    try:
        yield
    finally:
        if core is not None:
            set_active_core(core)
        set_active_infra(infra)
        set_active_event_bus(bus)


def _probe(application: Any) -> None:
    @application.get("/_probe/core", include_in_schema=False)
    async def core_id() -> dict[str, int]:
        seen: dict[str, int] = {
            "request": id(get_active_core()),
            "infra": id(get_active_infra()),
        }

        async def background() -> None:
            seen["task"] = id(get_active_core())
            seen["task_infra"] = id(get_active_infra())

        await asyncio.create_task(background())
        return seen


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_process_runtime")
async def test_a_request_of_the_first_app_sees_its_own_core() -> None:
    first = create_app()
    _probe(first)
    second = create_app()  # moves the process default to the second app
    _probe(second)
    assert get_active_core() is second.state.core

    for application in (first, second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as http:
            seen = (await http.get("/_probe/core")).json()
        core = application.state.core
        assert seen == {
            "request": id(core),
            "task": id(core),
            "infra": id(core.infra),
            "task_infra": id(core.infra),
        }
    # the process defaults: one owner call bound the core, its registry and bus
    assert id(get_active_infra()) == id(second.state.core.infra)
    assert get_event_bus() is second.state.core.event_bus


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_process_runtime")
async def test_a_task_outliving_the_request_keeps_the_first_apps_core() -> None:
    """A task a request starts and does not await still sees its app's core
    after the response is sent and the second app is the process default."""
    first = create_app()
    seen: dict[str, int] = {}
    done = asyncio.Event()

    @first.get("/_probe/later", include_in_schema=False)
    async def later() -> dict[str, bool]:
        async def background() -> None:
            await asyncio.sleep(0)
            seen["task"] = id(get_active_core())
            done.set()

        asyncio.get_running_loop().create_task(background())
        return {"ok": True}

    create_app()  # the second app: the process default now
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first), base_url="http://test"
    ) as http:
        assert (await http.get("/_probe/later")).status_code == 200
    await asyncio.wait_for(done.wait(), 5)
    assert seen["task"] == id(first.state.core)


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_process_runtime")
async def test_the_lifespan_runs_with_its_own_apps_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup and shutdown of the first app run bound to its core."""
    import sandloom.core.app_factory.lifecycle as lifecycle

    phases: dict[str, int] = {}

    async def startup(_core: object) -> None:
        phases["startup"] = id(get_active_core())

    async def shutdown(_core: object, **_kw: Any) -> None:
        phases["shutdown"] = id(get_active_core())

    monkeypatch.setattr(lifecycle, "run_startup", startup)
    monkeypatch.setattr(lifecycle, "run_shutdown", shutdown)
    monkeypatch.setattr(lifecycle.settings, "outbox_relay_enabled", "off")
    first = create_app()
    create_app()
    async with first.router.lifespan_context(first):
        pass
    expected = id(first.state.core)
    assert phases == {"startup": expected, "shutdown": expected}
