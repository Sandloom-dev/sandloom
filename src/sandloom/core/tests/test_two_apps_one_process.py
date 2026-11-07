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
    assert id(get_active_infra()) == id(second.state.core.infra)  # the default
