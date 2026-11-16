"""The admin API takes a dead-letter entry id as an opaque, bounded token.

A Redis stream id looks like ``1700000000000-0``, but the event transport is a
port: another one may mint UUIDs or any other token. The ids of a replay and the
page cursor of the DLQ listing are checked for their shape only (letters,
digits, ``_ . : -``, 1 to 80 characters), never for the stream format.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sandloom.core.admin.api import platform

_ACCEPTED = [
    "1700000000000-0",
    str(uuid4()),
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


@pytest.mark.parametrize("cursor", [c for c in _REFUSED if c])
def test_the_listing_refuses_a_cursor_that_is_no_token(
    client: TestClient, cursor: str
) -> None:
    response = client.get(
        "/streams/dlq", params={"channel": "billing.invoice.created", "cursor": cursor}
    )
    assert response.status_code == 422
