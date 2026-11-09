"""The broker probe of the built-in ``celery`` taskqueue driver.

``CeleryTaskQueueProvider.health()`` answers with :func:`probe_broker`: one
kombu connection to the app's broker, opened and released, bounded by
``HEALTH_CHECK_TIMEOUT_SECONDS`` (the connect timeout and, for the Redis
transports, the socket timeouts), with no retry. Building the provider sends
nothing to the broker, so without this probe a broker outage was invisible to
every health reader (``/ready``, ``/diagnostics/infra``, ``sandloom doctor``).

The probe is not cached here: its readers are. ``/ready`` runs it through the
TTL / single-flight cache of the readiness probes (one probe per instance per
TTL, however many hits arrive); ``/diagnostics/infra`` and ``sandloom doctor``
are operator calls behind a credential or a shell.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult

#: Broker URL schemes of the kombu Redis transports, which take their socket
#: timeouts from the transport options (``connect_timeout`` alone does not
#: bound them).
_REDIS_SCHEMES = frozenset({"redis", "rediss", "sentinel"})


def _where(url: str) -> str:
    """``scheme://host:port`` of a broker URL: never its credentials."""
    try:
        parts = urlsplit(url)
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname or ''}{port}"
    except ValueError:
        return "?"


def _transport_options(app: Any, scheme: str, timeout: float) -> dict[str, Any]:
    options = dict(getattr(app.conf, "broker_transport_options", None) or {})
    if scheme in _REDIS_SCHEMES:
        # An operator's own socket timeouts win; the probe only bounds what
        # is unset.
        options.setdefault("socket_connect_timeout", timeout)
        options.setdefault("socket_timeout", timeout)
    return options


def _connect_once(app: Any, timeout: float) -> None:
    """Open one connection to ``app``'s broker and release it (blocking)."""
    url = str(getattr(app.conf, "broker_url", None) or "")
    scheme = urlsplit(url).scheme if url else ""
    connection = app.connection_for_write(
        connect_timeout=timeout,
        transport_options=_transport_options(app, scheme, timeout),
    )
    try:
        connection.ensure_connection(
            max_retries=1,
            interval_start=0,
            interval_step=0,
            interval_max=0,
            timeout=timeout,
        )
    finally:
        connection.release()


class BrokerUnreachableError(RuntimeError):
    """The broker refused or dropped the probe's connection (any transport
    error); the message names the broker by scheme, host and port."""


async def probe_broker(app: Any, *, limit_seconds: float | None = None) -> HealthResult:
    """``ok`` when ``app``'s broker accepts a connection within
    ``limit_seconds`` (default ``HEALTH_CHECK_TIMEOUT_SECONDS``); ``fail`` when
    it does not answer in time. A refused or dropped connection raises
    :class:`BrokerUnreachableError`, which every health reader turns into a
    failed probe carrying its message (``_capability_health``,
    ``InfraRegistry.health_all``).

    The blocking connect runs in a worker thread, so the event loop never
    waits on the broker; the connect and socket timeouts end that thread
    too, so an unreachable broker never leaves one behind.
    """
    bound = float(
        settings.health_check_timeout_seconds
        if limit_seconds is None
        else limit_seconds
    )
    broker = _where(str(getattr(app.conf, "broker_url", None) or ""))
    try:
        await asyncio.wait_for(asyncio.to_thread(_connect_once, app, bound), bound)
    except TimeoutError:
        return HealthResult(
            status="fail",
            details={
                "broker": broker,
                "error": f"broker {broker} timeout > {bound:g}s",
            },
        )
    except Exception as exc:
        raise BrokerUnreachableError(
            f"broker {broker} unreachable: {type(exc).__name__}: {exc}"
        ) from exc
    return HealthResult(status="ok", details={"broker": broker})


__all__ = ["BrokerUnreachableError", "probe_broker"]
