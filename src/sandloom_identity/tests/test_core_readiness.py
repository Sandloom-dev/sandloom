"""Tests for core health and readiness endpoints after modular core refactor."""

import pytest
from httpx import AsyncClient


@pytest.mark.api
async def test_health_endpoint(async_client: AsyncClient) -> None:
    resp = await async_client.get("/health")
    # Contract: 200 only when every check is ok, 503 otherwise (k8s probes).
    assert resp.status_code in {200, 503}
    data = resp.json()
    assert data["status"] in {"ok", "degraded", "fail"}
    assert (resp.status_code == 200) == (data["status"] == "ok")
    assert "checks" in data
    assert "core" in data["checks"], data


@pytest.mark.api
async def test_ready_endpoint(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.sdk.infra import settings

    token = "readiness-test-internal-token-0123456789"
    monkeypatch.setattr(settings, "internal_api_token", token)
    # Module/consumer names are internal topology: visible with the token only.
    public = (await async_client.get("/ready")).json()
    assert "loaded_modules" not in public, public
    resp = await async_client.get("/ready", headers={"X-Internal-Token": token})
    # Contract: 200 only when healthy AND all required modules are loaded.
    assert resp.status_code in {200, 503}
    data = resp.json()
    assert (resp.status_code == 200) == (data["status"] == "ok")
    assert "sandloom_identity" in data["loaded_modules"], data
    assert "sandloom_identity" in data["required_modules"], data
    assert not data["missing_required"], data
    assert data["status"] in {"ok", "degraded"}, data
