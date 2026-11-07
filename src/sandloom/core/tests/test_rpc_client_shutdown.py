"""The application's shutdown closes the pooled outbound RPC client."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import sandloom.core.rpc.client as rpc_client
from sandloom.core.app_factory import create_app
from sandloom.core.eventing.event_bus import get_event_bus, set_active_event_bus
from sandloom.core.infra.active import get_active_infra, set_active_infra
from sandloom.core.lifecycle import run_shutdown
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


class _Client:
    is_closed = False

    async def aclose(self) -> None:
        self.is_closed = True


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_process_runtime")
async def test_shutdown_closes_the_shared_rpc_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app()
    client = _Client()
    monkeypatch.setattr(rpc_client, "_rpc_client_instance", client)
    await run_shutdown(application.state.core)
    assert client.is_closed
    assert rpc_client._rpc_client_instance is None
