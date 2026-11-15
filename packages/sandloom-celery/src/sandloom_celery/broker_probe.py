"""The broker probe of the ``celery`` taskqueue driver (``sandloom-celery``).

``CeleryTaskQueueProvider.health()`` answers with :func:`probe_broker`: one
kombu connection to the app's broker, opened and released, bounded by
the ``health_timeout_seconds`` option (the connect timeout and, for the Redis
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

from sandloom.driverkit import HealthResult

#: Broker URL schemes of the kombu Redis transports, which take their socket
#: timeouts from the transport options (``connect_timeout`` alone does not
#: bound them).
_REDIS_SCHEMES = frozenset({"redis", "rediss", "sentinel"})


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
    error); the message carries the error's type name and nothing of the broker
    (no URL, no credential, no host): a health detail reaches an operator
    endpoint, a log and a metric label."""


async def probe_broker(app: Any, *, limit_seconds: float) -> HealthResult:
    """``ok`` when ``app``'s broker accepts a connection within
    ``limit_seconds`` (the driver's ``health_timeout_seconds`` option, which
    the kernel fills from ``HEALTH_CHECK_TIMEOUT_SECONDS``); ``fail`` with
    the details ``{"error": "timeout"}`` (one token, the shape of every health
    detail) when it does not answer in time. A refused or dropped connection
    raises :class:`BrokerUnreachableError`, which
    ``CeleryTaskQueueProvider.health`` turns into a failed probe carrying its
    type name, never its text (spec 2.4); any other reader of the probe does the
    same. No detail names the broker.

    The blocking connect runs in a worker thread, so the event loop never
    waits on the broker; the connect and socket timeouts end that thread
    too, so an unreachable broker never leaves one behind.
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_connect_once, app, limit_seconds), limit_seconds
        )
    except TimeoutError:
        return HealthResult(status="fail", details={"error": "timeout"})
    except Exception as exc:
        # The type name only: a client error's text can carry the broker URL.
        raise BrokerUnreachableError(
            f"broker unreachable: {type(exc).__name__}"
        ) from exc
    return HealthResult(status="ok")


__all__ = ["BrokerUnreachableError", "probe_broker"]
