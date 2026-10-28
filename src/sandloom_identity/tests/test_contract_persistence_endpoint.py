from __future__ import annotations

from http import HTTPStatus

import pytest
from httpx import AsyncClient

_TOKEN = "contract-endpoint-test-internal-token-0123456789"
_HEADERS = {"X-Internal-Token": _TOKEN}


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # /contracts/* is internal: closed unless INTERNAL_API_TOKEN is configured.
    from sandloom.sdk.infra import settings

    monkeypatch.setattr(settings, "internal_api_token", _TOKEN)


async def test_contract_persistence_endpoint(async_client: AsyncClient) -> None:
    resp = await async_client.get("/contracts/persistence", headers=_HEADERS)
    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert "persistence_path" in data
    assert "loaded_from_disk" in data
    assert "count" in data
    assert isinstance(data["count"], int)
