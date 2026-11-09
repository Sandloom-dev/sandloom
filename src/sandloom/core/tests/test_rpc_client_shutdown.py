"""The application's shutdown closes the pooled outbound RPC client."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import sandloom.core.rpc.client as rpc_client
from sandloom.core.eventing.event_bus import get_event_bus, set_active_event_bus
from sandloom.core.infra.active import get_active_infra, set_active_infra
from sandloom.core.lifecycle import run_shutdown
from sandloom.core.runtime import get_active_core, set_active_core
from sandloom.core.tests._root_support import create_root_app


@pytest.fixture
def restore_process_runtime() -> Iterator[None]:
    """Restore the process default core, registry and bus after the test."""
    core, infra, bus = get_active_core(), get_active_infra(), get_event_bus()
    try:
        yield
    finally:
        if core is not None:
            set_active_core(core)
        set_active_infra(infra)
        set_active_event_bus(bus)


class _Client:
    """A stand-in pooled client that records its close."""

    is_closed = False

    async def aclose(self) -> None:
        """Mark the client closed."""
        self.is_closed = True


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_process_runtime")
async def test_shutdown_closes_the_shared_rpc_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only app's shutdown closes the shared client."""
    application = create_root_app()
    client = _Client()
    monkeypatch.setattr(rpc_client, "_rpc_client_instance", client)
    await run_shutdown(application.state.core)
    assert client.is_closed
    assert rpc_client._rpc_client_instance is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_process_runtime")
async def test_shutdown_of_an_older_app_retires_the_client_another_app_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two apps in one process share the client: the first one's shutdown
    retires it (in-flight calls of the second finish) instead of closing it."""
    first = create_root_app()
    create_root_app()  # the process default now
    client = _Client()
    retired: list[object] = []
    monkeypatch.setattr(rpc_client, "_rpc_client_instance", client)
    monkeypatch.setattr(rpc_client, "_retire_client", retired.append)
    await run_shutdown(first.state.core)
    assert not client.is_closed
    assert retired == [client]
    assert rpc_client._rpc_client_instance is None  # the next call builds anew
