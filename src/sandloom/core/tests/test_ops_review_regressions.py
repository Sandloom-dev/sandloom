"""Operational boundaries from the 22 October review."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.subprocess_probe
def test_metrics_scrape_aggregates_other_workers(tmp_path: Path) -> None:
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    for amount in (2, 3):
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from sandloom.core.observability.metrics import http_requests_total; "
                    f"http_requests_total.labels('GET','/probe','200').inc({amount})"
                ),
            ],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from sandloom.core.observability.metrics import render_metrics; "
                "print(render_metrics().decode())"
            ),
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    assert (
        'sandloom_http_requests_total{method="GET",path="/probe",status="200"} 5.0'
        in result.stdout
    )


def test_gunicorn_worker_count_is_exported_before_application_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli import gunicorn_conf as g

    # Register an undo even when the variable was originally absent. The hook
    # writes os.environ directly; delenv(absent, raising=False) records none.
    monkeypatch.setenv("SANDLOOM_WORKER_COUNT", "1")
    g.post_fork(
        SimpleNamespace(num_workers=3), SimpleNamespace(sandloom_relay_leader=True)
    )
    assert os.environ.get("SANDLOOM_WORKER_COUNT") == "3"


def test_metrics_master_cleans_only_mmaps_and_refuses_shared_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli import gunicorn_conf as g

    (tmp_path / "counter_123.db").write_text("stale")
    (tmp_path / "keep.txt").write_text("keep")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    first = SimpleNamespace(cfg=SimpleNamespace(preload_app=False))
    second = SimpleNamespace(cfg=SimpleNamespace(preload_app=False))
    g.on_starting(first)
    try:
        assert not (tmp_path / "counter_123.db").exists()
        assert (tmp_path / "keep.txt").read_text() == "keep"
        with pytest.raises(RuntimeError, match="another Gunicorn master"):
            g.on_starting(second)
    finally:
        g.on_exit(first)


def test_metrics_refuses_preload_before_silently_using_local_registry() -> None:
    from sandloom.core.cli import gunicorn_conf as g

    with pytest.raises(RuntimeError, match="preload_app=False"):
        g.on_starting(SimpleNamespace(cfg=SimpleNamespace(preload_app=True)))


def test_reexecuted_master_gets_fresh_auto_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli import gunicorn_conf as g

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.delenv("SANDLOOM_PROMETHEUS_AUTODIR", raising=False)
    first = SimpleNamespace(cfg=SimpleNamespace(preload_app=False))
    second = SimpleNamespace(cfg=SimpleNamespace(preload_app=False))
    g.on_starting(first)
    old_dir = Path(first.sandloom_metrics_dir)
    (old_dir / "counter_123.db").write_text("old worker still alive")
    try:
        # A re-exec inherits the first master's environment while it drains.
        g.on_starting(second)
        assert second.sandloom_metrics_dir != first.sandloom_metrics_dir
        assert (old_dir / "counter_123.db").exists()
    finally:
        g.on_exit(first)
        g.on_exit(second)


def test_dead_worker_live_gauges_are_removed_but_counters_retained(
    tmp_path: Path,
) -> None:
    from sandloom.core.cli import gunicorn_conf as g

    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; from sandloom.core.observability.metrics import "
                "http_requests_total,http_requests_in_progress; "
                "http_requests_total.labels('GET','/dead','200').inc(7); "
                "http_requests_in_progress.labels('GET').set(7); print(os.getpid())"
            ),
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    dead_pid = int(result.stdout.strip())
    # The real master hook receives the exited worker's PID.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        g.child_exit(
            SimpleNamespace(),
            SimpleNamespace(pid=dead_pid, sandloom_relay_leader=False),
        )
    rendered = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from sandloom.core.observability.metrics import render_metrics; "
                "print(render_metrics().decode())"
            ),
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    ).stdout
    assert (
        'sandloom_http_requests_total{method="GET",path="/dead",status="200"} 7.0'
        in rendered
    )
    assert 'sandloom_http_requests_in_progress{method="GET"} 7.0' not in rendered


def test_enabled_worker_access_logs_are_json_without_query_values() -> None:
    from sandloom.core.cli import gunicorn_conf as g

    stream = io.StringIO()
    logger = logging.getLogger("uvicorn.access")
    previous_handlers, previous_filters = logger.handlers[:], logger.filters[:]
    handler = logging.StreamHandler(stream)
    logger.handlers = [handler]
    try:
        worker = SimpleNamespace(log=SimpleNamespace(info=lambda *_: None))
        g.post_worker_init(worker)
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            '%s - "%s %s HTTP/%s" %d',
            (
                "127.0.0.1",
                "GET",
                "/reset?email=private@example.test&code=abc",
                "1.1",
                200,
            ),
            None,
        )
        logger.handle(record)
        assert '"logger": "uvicorn.access"' in stream.getvalue()
        assert "private@example.test" not in stream.getvalue()
        assert "?" not in stream.getvalue()
    finally:
        logger.handlers, logger.filters = previous_handlers, previous_filters


@pytest.mark.asyncio
async def test_shutdown_tasks_share_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings
    from sandloom.core.lifecycle import run_shutdown

    monkeypatch.setattr(settings, "shutdown_task_timeout_seconds", 0.05)
    monkeypatch.setattr(settings, "shutdown_budget_seconds", 0.07, raising=False)
    completed: list[str] = []

    async def slow() -> None:
        await asyncio.sleep(0.045)
        completed.append("done")

    core = SimpleNamespace(shutdown_tasks=[slow, slow, slow])
    started = time.monotonic()
    await run_shutdown(core)
    assert time.monotonic() - started < 0.5
    assert len(completed) <= 1


@pytest.mark.asyncio
async def test_zero_ttl_health_cache_still_single_flights() -> None:
    from sandloom.core.app_factory import _HealthCache
    from sandloom.core.descriptors import HealthResult

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def probe() -> dict[str, HealthResult]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"db": HealthResult(status="ok")}

    cache = _HealthCache(probe, ttl=0)
    requests = [asyncio.create_task(cache.get()) for _ in range(10)]
    await entered.wait()
    release.set()
    assert all(await asyncio.gather(*requests))
    assert calls == 1


def test_tracing_shutdown_flushes_owned_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.observability import tracing

    calls: list[str] = []
    provider = SimpleNamespace(shutdown=lambda: calls.append("shutdown"))
    monkeypatch.setattr(tracing, "_provider", provider, raising=False)
    monkeypatch.setattr(tracing, "_tracing_enabled", True)
    tracing.shutdown_tracing()
    tracing.shutdown_tracing()
    assert calls == ["shutdown"]
    assert tracing._tracing_enabled is False


@pytest.mark.asyncio
async def test_token_signing_refusal_happens_before_startup_network_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from sandloom.core import app_factory

    startup = AsyncMock()
    monkeypatch.setattr(app_factory, "run_startup", startup)
    monkeypatch.setattr(app_factory, "run_shutdown", AsyncMock())
    monkeypatch.setattr(
        "sandloom.core.auth.tokens.token_signing_problem",
        lambda **_: "bad signing role",
    )
    app = app_factory.create_app()
    with pytest.raises(RuntimeError, match="bad signing role"):
        async with app.router.lifespan_context(app):
            pass
    startup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["not_running", "stalled", "circuit_open"])
async def test_relay_failure_fails_readiness_without_failing_liveness(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from sandloom.core.app_factory import _install_ops_endpoints
    from sandloom.core.config.settings import settings
    from sandloom.core.facade import CoreFacade

    app = FastAPI()
    core = CoreFacade(app=app)
    core.loaded_modules = ["test"]
    monkeypatch.setattr(settings, "module_required", [])
    status = {
        "enabled": True,
        "running": failure != "not_running",
        "stalled": failure == "stalled",
        "circuit_open": failure == "circuit_open",
    }
    app.state.relay_task = object()
    monkeypatch.setattr(
        "sandloom.core.tasks.relay.relay_health_status", lambda *_: status
    )
    _install_ops_endpoints(app, core)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/ready")).status_code == 503
        assert (await client.get("/health/simple")).status_code == 200
