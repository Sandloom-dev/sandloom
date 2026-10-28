"""Observability: structured logging, request/trace correlation, RED metrics."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from prometheus_client import generate_latest
from starlette.testclient import TestClient

from sandloom.core.config.common import Environment
from sandloom.core.config.settings import settings
from sandloom.core.http.middlewares import register_middlewares, request_id_var
from sandloom.core.observability.logging import ContextFilter, JsonFormatter


def test_context_filter_injects_ids() -> None:
    record = logging.makeLogRecord({"msg": "hello"})
    token = request_id_var.set("req-123")
    try:
        assert ContextFilter().filter(record) is True
        assert record.request_id == "req-123"  # type: ignore[attr-defined]
    finally:
        request_id_var.reset(token)


def test_json_formatter_emits_structured_line() -> None:
    record = logging.makeLogRecord(
        {
            "msg": "user %s logged in",
            "args": ("u1",),
            "levelname": "INFO",
            "name": "sandloom.test",
        }
    )
    record.request_id = "req-abc"
    record.trace_id = None
    out = JsonFormatter().format(record)
    parsed = json.loads(out)
    assert parsed["message"] == "user u1 logged in"
    assert parsed["level"] == "INFO"
    assert parsed["request_id"] == "req-abc"
    assert "trace_id" not in parsed  # None values omitted


def test_json_formatter_redacts_sensitive_extra() -> None:
    record = logging.makeLogRecord(
        {"msg": "login", "password": "supersecret", "name": "sandloom.test"}
    )
    out = JsonFormatter().format(record)
    assert "supersecret" not in out


def test_log_as_json_follows_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "log_format", "auto", raising=False)
    monkeypatch.setattr(settings, "environment", Environment.prod, raising=False)
    prod_auto = settings.log_as_json
    monkeypatch.setattr(settings, "environment", Environment.dev, raising=False)
    dev_auto = settings.log_as_json
    monkeypatch.setattr(settings, "log_format", "json", raising=False)
    forced_json = settings.log_as_json
    assert prod_auto and forced_json and not dev_auto


def test_red_metrics_recorded_on_request() -> None:
    app = FastAPI()
    register_middlewares(app)

    @app.get("/widgets/{wid}")
    async def _widget(wid: str) -> dict[str, str]:
        return {"wid": wid}

    client = TestClient(app)
    resp = client.get("/widgets/42")
    assert resp.status_code == 200
    assert "X-Process-Time" in resp.headers
    assert resp.headers.get("X-Request-ID")

    exposition = generate_latest().decode()
    # Route TEMPLATE is used as the label, not the raw path.
    assert "sandloom_http_requests_total" in exposition
    assert "/widgets/{wid}" in exposition
    assert 'status="200"' in exposition


def test_init_logging_is_idempotent_across_create_app_calls() -> None:
    # create_app runs init_logging on every app build (tests, --reload, the
    # split matrix): each call must not stack another ContextFilter on the
    # root handlers (O(N) filters per record).
    from sandloom.core.observability.logging import init_logging

    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        for _ in range(5):
            init_logging()
        context_filters = [f for f in handler.filters if isinstance(f, ContextFilter)]
        assert len(context_filters) == 1
        assert handler.formatter is not None
    finally:
        root.removeHandler(handler)
