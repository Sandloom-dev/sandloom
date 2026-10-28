from __future__ import annotations

from http import HTTPStatus
from typing import Any

import pytest
from httpx import AsyncClient

_TOKEN = "contract-endpoint-test-internal-token-0123456789"
_HEADERS = {"X-Internal-Token": _TOKEN}


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # /contracts/* is internal: closed unless INTERNAL_API_TOKEN is configured.
    from sandloom.sdk.infra import settings

    monkeypatch.setattr(settings, "internal_api_token", _TOKEN)


async def test_contract_diff_new_channel(async_client: AsyncClient) -> None:
    body: dict[str, object] = {
        "channel": "demo.orders.created.v1",
        "new_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    }
    resp = await async_client.post("/contracts/diff", json=body, headers=_HEADERS)
    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert data["existing"] is False
    assert data["breaking"] is False


@pytest.fixture
def _baseline(sandloom_core: Any) -> None:
    """A registered baseline schema on the registry wired into the app's
    CoreFacade (the harness restores the registry after the test)."""
    base_schema: dict[str, object] = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }
    sandloom_core.get_schema_registry().register(
        "demo.orders.created.v1", "orders_created", base_schema
    )


@pytest.mark.usefixtures("_baseline")
async def test_contract_diff_existing_non_breaking(async_client: AsyncClient) -> None:
    # Add an optional field (non-breaking)
    body: dict[str, object] = {
        "channel": "demo.orders.created.v1",
        "new_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["id"],
        },
    }
    resp = await async_client.post("/contracts/diff", json=body, headers=_HEADERS)
    assert resp.status_code == HTTPStatus.OK
    diff = resp.json()
    assert diff["existing"] is True
    report = diff["report"]
    assert report["breaking"] is False
    assert report["added_optional"] == ["note"]


@pytest.mark.usefixtures("_baseline")
async def test_contract_diff_existing_breaking(async_client: AsyncClient) -> None:
    body: dict[str, object] = {
        "channel": "demo.orders.created.v1",
        "new_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},  # type change
            "required": ["id"],
        },
    }
    resp = await async_client.post("/contracts/diff", json=body, headers=_HEADERS)
    assert resp.status_code == HTTPStatus.OK
    diff = resp.json()
    assert diff["existing"] is True
    report = diff["report"]
    assert report["breaking"] is True
    assert "id" in report["type_changed"]
