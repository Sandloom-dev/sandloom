"""Optional origin diagnostics retain bounded metadata, never credentials."""

import json

import httpx
import pytest
from starlette.responses import JSONResponse

from qualification.edge_trace import EdgeTrace

RAY = "0123456789abcdef"
TOKEN = "edge-trace-test-internal-token"


async def test_trace_preserves_response_and_requires_internal_auth(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)

    async def app(scope, receive, send):
        await JSONResponse({"id": 1})(scope, receive, send)

    trace = EdgeTrace(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=trace), base_url="http://fixture"
    ) as client:
        response = await client.get(
            "/s5plan/1",
            headers={"CF-Ray": RAY + "-LIS", "Authorization": "Bearer private-token"},
        )
        assert response.status_code == 200 and response.json() == {"id": 1}
        path = "/_qualification/edge-trace?ray=" + RAY
        assert (await client.get(path)).status_code == 404
        assert (
            await client.get(path, headers={"X-Internal-Token": "incorrect"})
        ).status_code == 404
        record = (await client.get(path, headers={"X-Internal-Token": TOKEN})).json()
        assert len(record["hits"]) == 1
        assert record["hits"][0]["status"] == 200
        assert record["hits"][0]["completed"] is True
        assert "private-token" not in json.dumps(record)
        assert TOKEN not in json.dumps(record)


async def test_trace_preserves_exception_without_storing_sensitive_text(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)

    async def app(scope, receive, send):
        raise RuntimeError("private-sensitive-detail")

    trace = EdgeTrace(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=trace), base_url="http://fixture"
    ) as client:
        with pytest.raises(RuntimeError, match="private-sensitive-detail"):
            await client.get("/s5plan/1", headers={"CF-Ray": RAY})
        record = (
            await client.get(
                "/_qualification/edge-trace?ray=" + RAY,
                headers={"X-Internal-Token": TOKEN},
            )
        ).json()
        assert record["hits"][0]["error_type"] == "RuntimeError"
        assert record["hits"][0]["completed"] is False
        assert "private-sensitive-detail" not in json.dumps(record)
