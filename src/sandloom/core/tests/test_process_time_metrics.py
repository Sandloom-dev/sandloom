"""ProcessTimeMiddleware: cached metric children, identical /metrics output.

The middleware caches the labelled Prometheus children per (method, route
template, status) and reads method / route straight off the ASGI scope. These
tests run the SAME request script through the previous implementation (kept
verbatim below as the reference) and the current one, each against its own
fresh metric objects, and require identical exposition (counters, gauge,
histogram counts; bucket placement and sums are timing-dependent and masked).
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.datastructures import MutableHeaders

from sandloom.core.http import middlewares
from sandloom.core.http.middlewares import (
    ProcessTimeMiddleware,
    SandloomPipelineMiddleware,
    _method_label,
    _route_template,
)
from sandloom.core.observability import metrics


class _ReferenceProcessTimeMiddleware:
    """The implementation before the child cache (verbatim behaviour)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        method = _method_label(request)
        start_time = time.perf_counter()
        status = 500
        metrics.http_requests_in_progress.labels(method=method).inc()

        async def send_wrapper(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(raw=message["headers"])["X-Process-Time"] = str(
                    time.perf_counter() - start_time
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start_time
            path = _route_template(request)
            metrics.http_requests_in_progress.labels(method=method).dec()
            metrics.http_request_duration_seconds.labels(
                method=method, path=path
            ).observe(elapsed)
            metrics.http_requests_total.labels(
                method=method, path=path, status=str(status)
            ).inc()


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    """Swap the three HTTP metrics for fresh ones on a private registry."""
    reg = CollectorRegistry()
    monkeypatch.setattr(
        metrics,
        "http_requests_total",
        Counter(
            "sandloom_http_requests_total",
            "x",
            ["method", "path", "status"],
            registry=reg,
        ),
    )
    monkeypatch.setattr(
        metrics,
        "http_request_duration_seconds",
        Histogram(
            "sandloom_http_request_duration_seconds",
            "x",
            ["method", "path"],
            registry=reg,
        ),
    )
    monkeypatch.setattr(
        metrics,
        "http_requests_in_progress",
        Gauge("sandloom_http_requests_in_progress", "x", ["method"], registry=reg),
    )
    yield reg


def _app(mw: Any) -> FastAPI:
    app = FastAPI()
    app.add_middleware(mw)

    @app.get("/items/{iid}")
    async def _item(iid: int) -> dict[str, int]:
        if iid == 404:
            raise HTTPException(404, "nope")
        return {"iid": iid}

    @app.post("/items")
    async def _create() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("boom")

    @app.get("/preset")
    async def _preset() -> Response:
        # The handler sets the header itself: replaced, never duplicated.
        return Response("x", headers={"X-Process-Time": "handler"})

    return app


def _script(client: TestClient) -> list[dict[str, Any]]:
    seen = []
    calls: list[tuple[str, str]] = [
        ("GET", "/items/1"),
        ("GET", "/items/2"),
        ("GET", "/items/404"),
        ("POST", "/items"),
        ("GET", "/no/such/route"),
        ("PATCH", "/items"),  # 405 on a known path
        ("FOOBAR", "/items/1"),  # unknown verb → OTHER
        ("get", "/items/3"),  # lowercase verb
        ("GET", "/boom"),
        ("GET", "/preset"),
        ("GET", "/items/1"),
    ]
    for method, path in calls:
        r = client.request(method, path)
        values = [v for k, v in r.headers.items() if k.lower() == "x-process-time"]
        seen.append({"status": r.status_code, "x_process_time": values})
    return seen


def _normalise(exposition: str) -> str:
    out = []
    for line in exposition.splitlines():
        if line.startswith("sandloom_http_request_duration_seconds_bucket") or (
            line.startswith("sandloom_http_request_duration_seconds_sum")
        ):
            continue  # timing-dependent
        if "_created" in line:
            continue
        out.append(line)
    return "\n".join(sorted(out))


def _run(mw: Any, reg: CollectorRegistry) -> tuple[str, list[dict[str, Any]]]:
    with TestClient(_app(mw), raise_server_exceptions=False) as client:
        seen = _script(client)
    return _normalise(generate_latest(reg).decode()), seen


@pytest.mark.parametrize(
    "middleware", [ProcessTimeMiddleware, SandloomPipelineMiddleware]
)
def test_metrics_and_header_identical_to_reference(
    fresh_registry: CollectorRegistry, monkeypatch: pytest.MonkeyPatch, middleware: Any
) -> None:
    ref_expo, ref_seen = _run(_ReferenceProcessTimeMiddleware, fresh_registry)
    # Fresh metric objects again for the new implementation.
    new_reg = CollectorRegistry()
    monkeypatch.setattr(
        metrics,
        "http_requests_total",
        Counter(
            "sandloom_http_requests_total",
            "x",
            ["method", "path", "status"],
            registry=new_reg,
        ),
    )
    monkeypatch.setattr(
        metrics,
        "http_request_duration_seconds",
        Histogram(
            "sandloom_http_request_duration_seconds",
            "x",
            ["method", "path"],
            registry=new_reg,
        ),
    )
    monkeypatch.setattr(
        metrics,
        "http_requests_in_progress",
        Gauge("sandloom_http_requests_in_progress", "x", ["method"], registry=new_reg),
    )
    new_expo, new_seen = _run(middleware, new_reg)

    assert new_expo == ref_expo
    assert 'path="<unmatched>"' in new_expo and 'method="OTHER"' in new_expo
    assert 'path="/items/{iid}",status="200"} 4.0' in new_expo
    assert 'status="500"' in new_expo
    for ref, new in zip(ref_seen, new_seen, strict=True):
        assert ref["status"] == new["status"]
        # Same header count (0 only where the response is built OUTSIDE the
        # middleware: Starlette's ServerErrorMiddleware 500), never duplicated.
        assert len(new["x_process_time"]) == len(ref["x_process_time"]) <= 1
        for value in new["x_process_time"]:
            float(value)  # a float string, as before (the handler's value replaced)
    assert sum(len(s["x_process_time"]) for s in new_seen) == len(new_seen) - 1
    # In-flight gauge back to zero for every method.
    assert re.findall(r"sandloom_http_requests_in_progress\{[^}]*\} (\S+)", new_expo)
    assert set(
        re.findall(r"sandloom_http_requests_in_progress\{[^}]*\} (\S+)", new_expo)
    ) == {"0.0"}


def test_children_are_cached_and_rebound_when_metrics_are_swapped(
    fresh_registry: CollectorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(ProcessTimeMiddleware)
    with TestClient(app) as client:
        client.get("/items/1")
        client.get("/items/2")
        mw = _find_mw(app)
        assert ("GET", "/items/{iid}", 200) in mw._done
        assert len(mw._done) == 1
        # Swap the counter (as tests / a registry reset would): no stale child.
        reg2 = CollectorRegistry()
        c2 = Counter(
            "sandloom_http_requests_total",
            "x",
            ["method", "path", "status"],
            registry=reg2,
        )
        monkeypatch.setattr(metrics, "http_requests_total", c2)
        client.get("/items/3")
    assert 'status="200"} 1.0' in generate_latest(reg2).decode()


def test_cache_is_bounded(
    fresh_registry: CollectorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(middlewares, "_METRIC_CHILD_CACHE_MAX", 1)
    app = _app(ProcessTimeMiddleware)
    with TestClient(app) as client:
        for path in ("/items/1", "/items/404", "/nope", "/items/1"):
            client.get(path)
        mw = _find_mw(app)
        assert len(mw._done) == 1  # bounded; uncached keys still counted below
    expo = generate_latest(fresh_registry).decode()
    assert 'path="/items/{iid}",status="200"} 2.0' in expo
    assert 'path="/items/{iid}",status="404"} 1.0' in expo
    assert 'path="<unmatched>",status="404"} 1.0' in expo


def _find_mw(app: FastAPI) -> Any:
    node: Any = app.middleware_stack
    while node is not None and not isinstance(node, ProcessTimeMiddleware):
        node = getattr(node, "app", None)
    assert node is not None
    return node
