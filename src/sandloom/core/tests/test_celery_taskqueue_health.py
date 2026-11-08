"""The ``celery`` taskqueue's broker probe (owner decision on infrastructure
dependencies, answer 4): a real, bounded check, not the default ``ok``.

``health()`` opens one connection to the app's broker and releases it, with
the connect and socket timeouts set to ``HEALTH_CHECK_TIMEOUT_SECONDS`` and no
retry; a failure names the broker by scheme, host and port, never its
credentials. Its readers cache it (``/ready``: one probe per TTL).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers import celery_broker_probe as probe
from sandloom.core.drivers.celery_taskqueue import create_celery_taskqueue
from sandloom.core.infra.config import InstanceConfig

celery = pytest.importorskip("celery")

#: Nothing listens on port 1: refused at once.
_REFUSED = "redis://:s3cret-pass@127.0.0.1:1/0"


def _app(broker: str) -> Any:
    return celery.Celery("kbic3-probe", broker=broker)


async def test_a_reachable_broker_is_ok() -> None:
    result = await probe.probe_broker(_app(settings.broker_url), timeout=2.0)

    assert result.status == "ok", result.details
    assert result.details["broker"].startswith("redis://")


async def test_an_unreachable_broker_fails_naming_it_without_credentials() -> None:
    started = time.monotonic()
    result = await probe.probe_broker(_app(_REFUSED), timeout=2.0)

    assert result.status == "fail"
    assert result.details["broker"] == "redis://127.0.0.1:1"
    assert "broker unreachable" in result.details["error"]
    assert "s3cret" not in repr(result.details)
    assert time.monotonic() - started < 2.0  # refused: no retry, no backoff


async def test_a_hanging_broker_is_bounded_by_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker that never answers costs at most the timeout, and the
    connect thread is bounded by it too (never left running)."""

    def hang(app: Any, timeout: float) -> None:
        time.sleep(timeout + 5)

    monkeypatch.setattr(probe, "_connect_once", hang)
    started = time.monotonic()
    result = await probe.probe_broker(_app(_REFUSED), timeout=0.2)

    assert result.status == "fail"
    assert result.details["error"] == "broker timeout > 0.2s"
    assert time.monotonic() - started < 1.0


def test_the_connect_is_bounded_and_does_not_retry() -> None:
    """The kombu connection gets the bound as its connect timeout and, on a
    Redis transport, as its socket timeouts (an operator's own win)."""
    seen: dict[str, Any] = {}

    class _Connection:
        def ensure_connection(self, **kwargs: Any) -> None:
            seen["ensure"] = kwargs

        def release(self) -> None:
            seen["released"] = True

    class _App:
        class conf:
            broker_url = "redis://cache:6379/1"
            broker_transport_options = {"socket_timeout": 9.0}

        def connection_for_write(self, **kwargs: Any) -> _Connection:
            seen["connect"] = kwargs
            return _Connection()

    probe._connect_once(_App(), 1.5)

    assert seen["connect"]["connect_timeout"] == 1.5
    assert seen["connect"]["transport_options"] == {
        "socket_timeout": 9.0,
        "socket_connect_timeout": 1.5,
    }
    assert seen["ensure"]["max_retries"] == 1
    assert seen["ensure"]["timeout"] == 1.5
    assert seen["released"] is True


async def test_the_provider_health_probes_its_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CeleryTaskQueueProvider.health()`` is the probe, per instance
    broker (no longer the contract's default ``ok``)."""
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 2.0)
    down = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name="kbic3",
            driver="celery",
            options={"broker": _REFUSED},
        )
    )
    up = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name="kbic3-up",
            driver="celery",
            options={"broker": settings.broker_url},
        )
    )

    assert (await down.health()).status == "fail"
    assert (await up.health()).status == "ok"
