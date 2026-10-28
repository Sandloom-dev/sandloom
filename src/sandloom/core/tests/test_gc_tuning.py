"""GC tuning of server processes (sandloom.core.gc_tuning, GC_* settings).

Anything that freezes runs in a CHILD interpreter: gc.freeze() in the test
process would pin every later test's heap for the rest of the session.
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/sandloom_test",
    "CACHE_URL": "redis://localhost:6379/0",
    "BROKER_URL": "redis://localhost:6379/1",
    "JWT_SECRET": "test-secret-0123456789abcdef0123456789abcdef",
}


def _settings(**env: str) -> object:
    from sandloom.core.config.settings import Settings

    return Settings(_env_file=None, **{**_BASE, **env})  # type: ignore[call-arg]


def test_gc_settings_defaults() -> None:
    s = _settings()
    assert s.gc_freeze_after_startup is True  # type: ignore[attr-defined]
    # CPython 3.13+'s own default; raises 3.12's 700
    assert s.gc_thresholds == (2000, 10, 10)  # type: ignore[attr-defined]
    assert s.gc_metrics_enabled is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10000,10,100", (10000, 10, 100)),
        (" 2000 , 10 , 10 ", (2000, 10, 10)),
        ("[50000, 20, 10]", (50000, 20, 10)),
        ("", None),
    ],
)
def test_gc_thresholds_parse(raw: str, expected: object) -> None:
    assert _settings(GC_THRESHOLDS=raw).gc_thresholds == expected  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "raw",
    [
        "0,10,10",  # 0 disables automatic collection
        "50,10,10",
        "2000000,10,10",
        "10000,0,10",
        "10000,10,20000",
        "10000,10",
        "a,b,c",
        "[1, 2, 3, 4]",
    ],
)
def test_gc_thresholds_rejected(raw: str) -> None:
    with pytest.raises(ValidationError, match="GC_THRESHOLDS"):
        _settings(GC_THRESHOLDS=raw)


def test_thresholds_skipped_on_incremental_gc_releases() -> None:
    from sandloom.core.gc_tuning import thresholds_supported

    assert thresholds_supported((3, 12, 15))
    assert thresholds_supported((3, 13, 0))
    assert not thresholds_supported((3, 14, 0))
    assert not thresholds_supported((3, 14, 4))
    assert thresholds_supported((3, 14, 5))
    assert thresholds_supported((3, 15, 0))


def test_gc_metrics_buffer_flushes_into_prometheus() -> None:
    """The callback only fills a ring; flush() (loop side) observes."""
    from sandloom.core.gc_tuning import GcMetrics
    from sandloom.core.observability import metrics

    def count(gen: str) -> float:
        for metric in metrics.gc_pause_seconds.collect():
            for sample in metric.samples:
                if (
                    sample.name.endswith("_count")
                    and sample.labels.get("generation") == gen
                ):
                    return float(sample.value)
        return 0.0

    buf = GcMetrics(capacity=4)
    before = count("2")
    for _ in range(3):
        buf("start", {"generation": 2})
        buf("stop", {"generation": 2, "collected": 5, "uncollectable": 0})
    assert count("2") == before  # nothing observed inside the "collector"
    assert buf.flush() == 3
    assert count("2") == before + 3
    assert buf.flush() == 0
    for _ in range(6):  # a full ring drops instead of allocating
        buf("start", {"generation": 0})
        buf("stop", {"generation": 0, "collected": 0, "uncollectable": 0})
    assert buf.dropped == 2
    assert buf.flush() == 4


def test_install_gc_metrics_is_idempotent() -> None:
    from sandloom.core import gc_tuning

    first = gc_tuning.install_gc_metrics()
    try:
        assert gc_tuning.install_gc_metrics() is first
        assert gc.callbacks.count(first) == 1
    finally:
        gc.callbacks.remove(first)


def test_post_worker_init_wraps_the_app_and_keeps_attributes() -> None:
    from sandloom.core.cli import gunicorn_conf as g
    from sandloom.core.gc_tuning import GcAfterStartup

    app = SimpleNamespace(state="kept")
    log = SimpleNamespace(info=lambda *a: None)
    worker = SimpleNamespace(log=log, cfg=SimpleNamespace(keepalive=75), wsgi=app)
    g.post_worker_init(worker)
    assert isinstance(worker.wsgi, GcAfterStartup)
    assert worker.wsgi.app is app
    assert worker.wsgi.state == "kept"


_WORKER_SCRIPT = textwrap.dedent(
    """
    import asyncio, gc, json, sys
    from types import SimpleNamespace

    BASE = gc.get_freeze_count()  # 3.12+: static objects start out "frozen"

    from sandloom.core.cli import gunicorn_conf as g
    from sandloom.core.observability.metrics import render_metrics

    events = []

    async def app(scope, receive, send):
        assert scope["type"] == "lifespan"
        await receive()
        app.objects = [[i] for i in range(5000)]  # built during startup
        events.append(("before_complete", gc.get_freeze_count()))
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.complete"})

    worker = SimpleNamespace(
        log=SimpleNamespace(info=lambda *a: None),
        cfg=SimpleNamespace(keepalive=75),
        wsgi=app,
    )
    g.post_worker_init(worker)

    async def lifespan():
        inbox = asyncio.Queue()
        sent = []
        async def receive():
            return await inbox.get()
        async def send(message):
            sent.append((message["type"], gc.get_freeze_count()))
        await inbox.put({"type": "lifespan.startup"})
        task = asyncio.ensure_future(worker.wsgi({"type": "lifespan"}, receive, send))
        while not sent:
            await asyncio.sleep(0)
        gc.collect(2)  # observed by the metrics callback
        await asyncio.sleep(1.2)  # the loop-side flusher runs
        await inbox.put({"type": "lifespan.shutdown"})
        await task
        return sent

    sent = asyncio.run(lifespan())
    print(json.dumps({
        "base": BASE,
        "before_complete": events[0][1],
        "sent": sent,
        "threshold": gc.get_threshold(),
        "metrics": render_metrics().decode(),
    }))
    """
)


def test_worker_applies_gc_config_after_lifespan_startup() -> None:
    """A real child interpreter through the gunicorn hook: frozen at
    ``lifespan.startup.complete`` (before the server would accept traffic),
    thresholds applied, pauses exported."""
    env = {
        **os.environ,
        "GC_FREEZE_AFTER_STARTUP": "true",
        "GC_THRESHOLDS": "12345,11,99",
        "GC_METRICS_ENABLED": "true",
    }
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    out = subprocess.run(
        [sys.executable, "-c", _WORKER_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    doc = json.loads(out.stdout.strip().splitlines()[-1])
    assert doc["before_complete"] == doc["base"]
    (kind, frozen), *_ = doc["sent"]
    assert kind == "lifespan.startup.complete"
    assert (
        frozen > doc["base"] + 5000
    )  # the startup heap, including the app's own objects
    assert doc["threshold"] == [12345, 11, 99]
    assert 'sandloom_gc_pause_seconds_count{generation="2"}' in doc["metrics"]
    assert f"sandloom_gc_frozen_objects {float(frozen)}" in doc["metrics"]


def test_worker_gc_tuning_can_be_switched_off() -> None:
    env = {
        **os.environ,
        "GC_FREEZE_AFTER_STARTUP": "false",
        "GC_METRICS_ENABLED": "false",
        "GC_THRESHOLDS": "",
    }
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    out = subprocess.run(
        [sys.executable, "-c", _WORKER_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    doc = json.loads(out.stdout.strip().splitlines()[-1])
    assert doc["sent"][0] == ["lifespan.startup.complete", doc["base"]]
    assert doc["threshold"] == list(gc.get_threshold())
    assert 'sandloom_gc_pause_seconds_count{generation="2"}' not in doc["metrics"]


def test_celery_worker_init_freezes_in_the_parent() -> None:
    script = textwrap.dedent(
        """
        import gc, json
        from celery import signals
        from sandloom.core.gc_tuning import GcConfig, install_celery_hooks

        base = gc.get_freeze_count()
        install_celery_hooks(GcConfig(True, (3000, 10, 50), True))
        assert gc.get_freeze_count() == base
        signals.worker_init.send(sender=None)
        print(json.dumps([gc.get_freeze_count() - base, gc.get_threshold()]))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    frozen, threshold = json.loads(out.stdout.strip().splitlines()[-1])
    assert frozen > 0
    assert threshold == [3000, 10, 50]
