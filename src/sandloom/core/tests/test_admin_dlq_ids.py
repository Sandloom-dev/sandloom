"""The admin API takes a dead-letter entry id as an opaque, bounded token.

A Redis stream id looks like ``1700000000000-0``, but the event transport is a
port: another one may mint UUIDs or any other token. The ids of a replay and the
page cursor of the DLQ listing are checked for their shape only (letters,
digits, ``_ . : -``, 1 to 80 characters), never for the stream format. The
transport refuses an id of a shape it does not mint (``InvalidArgument``), which
the API answers with 422.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sandloom.core.admin.api import platform
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.infra.errors import InvalidArgument, Unavailable
from sandloom.core.schema.registry import InMemorySchemaRegistry

_CHANNEL = "billing.invoice.created"

_ACCEPTED = [
    "1700000000000-0",
    "0b9f3a52-6c1e-4f43-9a52-5f9d3c0f8e11",
    "evt_01HZ.part:2",
    "a",
    "x" * 80,
]
_REFUSED = ["", "x" * 81, "with space", "a/b", "a;b", "a\nb", "é"]


def _replay(ids: list[str]) -> platform.ReplayIn:
    return platform.ReplayIn(
        channel="billing.invoice.created", group="g", ids=ids, reason="manual replay"
    )


@pytest.mark.parametrize("entry_id", _ACCEPTED)
def test_a_replay_takes_any_opaque_token(entry_id: str) -> None:
    assert _replay([entry_id]).ids == [entry_id]


@pytest.mark.parametrize("entry_id", _REFUSED)
def test_a_replay_refuses_what_is_no_token(entry_id: str) -> None:
    with pytest.raises(ValidationError):
        _replay([entry_id])


@pytest.fixture
def client() -> TestClient:
    router = APIRouter()
    platform._stream_routes(
        router, SimpleNamespace(), platform._Source("billing", "default")
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("cursor", _ACCEPTED)
def test_the_listing_takes_any_opaque_cursor(client: TestClient, cursor: str) -> None:
    # Past validation the handler answers 404: no consumer of the channel runs here.
    response = client.get(
        "/streams/dlq", params={"channel": "billing.invoice.created", "cursor": cursor}
    )
    assert response.status_code == 404


@pytest.fixture
def served_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The listing route over a real in-memory transport that consumes the channel."""
    monkeypatch.setattr(platform, "_local_channels", lambda request: {_CHANNEL})
    core = SimpleNamespace(
        event_bus=StreamsEventBus(InMemorySchemaRegistry(), MemoryTransport())
    )
    router = APIRouter()
    platform._stream_routes(router, core, platform._Source("billing", "default"))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("cursor", ["abc", "x", "evt_01HZ.part:2"])
def test_a_cursor_the_transport_does_not_know_is_a_422_not_a_500(
    served_client: TestClient, cursor: str
) -> None:
    # It passes the opaque token check (above) and reaches the transport, which
    # refuses its own id shape: the caller's input, never a server error.
    response = served_client.get(
        "/streams/dlq", params={"channel": _CHANNEL, "cursor": cursor}
    )
    assert response.status_code == 422
    assert cursor not in response.text.replace(_CHANNEL, "")


def test_a_cursor_in_the_transports_own_shape_lists(served_client: TestClient) -> None:
    response = served_client.get(
        "/streams/dlq", params={"channel": _CHANNEL, "cursor": "1700000000000-0"}
    )
    assert response.status_code == 200
    assert response.json() == []


async def test_the_replay_maps_a_refused_id_to_422_and_lets_any_other_error_through() -> (
    None
):
    with pytest.raises(HTTPException) as caught:
        async with platform._entry_id_refused_as_422():
            raise InvalidArgument("x", capability="eventbus")
    assert caught.value.status_code == 422
    # An allow-list of one type: an outage is not the caller's input.
    with pytest.raises(Unavailable):
        async with platform._entry_id_refused_as_422():
            raise Unavailable("down", capability="eventbus")


@pytest.mark.parametrize("cursor", [c for c in _REFUSED if c])
def test_the_listing_refuses_a_cursor_that_is_no_token(
    client: TestClient, cursor: str
) -> None:
    response = client.get(
        "/streams/dlq", params={"channel": "billing.invoice.created", "cursor": cursor}
    )
    assert response.status_code == 422
