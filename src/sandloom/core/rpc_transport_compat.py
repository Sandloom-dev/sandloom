"""Capability probe for the aiohttp internals behind ``RPC_TRANSPORT=aiohttp``.

:mod:`sandloom.core.rpc_transport` observes and guards aiohttp's pool through
PRIVATE attributes (there is no public API for them): the connector's
``_acquired`` / ``_acquired_per_host`` / ``_conns`` / ``_factory`` / ``_ssl``
/ ``_create_connection``, the protocol's ``_should_close`` and the field
names of ``ConnectionKey``. The guard that matters for correctness — bytes
arriving on an IDLE pooled connection close it instead of being queued as
the NEXT caller's response — lives in an overridden
``ResponseHandler.data_received``.

A dependency upgrade may rename or change any of them. So, as for the DB
fast paths (:mod:`sandloom.core.sql_alchemy.driver_compat`):

* :func:`static_probe` (sync, cached, no I/O) checks every name the transport
  touches and that it has the expected kind (callable / mapping / set /
  property, the ``ConnectionKey`` fields, the ``close(abort_ssl=)`` keyword);
* :func:`full_probe` (async, cached; run once at startup when the process has
  ``SERVICE_BINDINGS``) adds the instance-level checks and a behavioural
  self-test on loopback: a local server answers one call and then writes an
  unsolicited response on the idle keep-alive connection; the transport must
  close that connection and the next call must get its OWN response on a new
  one.

A failing probe never fails a request: it logs ONE warning, increments
``sandloom_rpc_transport_fallback_total{reason}`` and the shared RPC client is
built with httpx instead (:func:`sandloom.core.rpc.client._rpc_client`).

The probe is defence in depth only. Mixed-up responses are excluded at the
protocol level, independent of any client internals: every RPC response is
bound to its request (``X-SANDLOOM-RPC-Nonce`` + ``X-SANDLOOM-RPC-Response-MAC``, see
:mod:`sandloom.core.rpc`) and the caller rejects a response bound to another call.

Verified aiohttp range: :data:`VERIFIED_AIOHTTP_RANGE`, pinned in
``pyproject.toml`` (``test_rpc_transport_compat.py`` pins both together).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from typing import Any

import sandloom.core.observability.metrics as _metrics

logger = logging.getLogger("sandloom.core.rpc_transport")

#: aiohttp versions the transport's internals were verified against:
#: ``[low, high)``. Keep in sync with ``pyproject.toml`` (a test pins both
#: together); widen only after ``docs/operate/operator-reference.md``
#: -> "Upgrading aiohttp".
VERIFIED_AIOHTTP_RANGE: tuple[str, str] = ("3.14.5", "3.15")

#: ``reason`` label values of ``sandloom_rpc_transport_fallback_total``.
REASON_IMPORT = "aiohttp_unavailable"
REASON_INTERNALS = "internals_changed"
REASON_SELFTEST = "selftest_failed"
REASON_CRASH = "probe_error"

#: ConnectionKey fields the transport fills (rpc_transport._connection_key).
_KNOWN_KEY_FIELDS = frozenset(
    {
        "host",
        "port",
        "is_ssl",
        "ssl",
        "proxy",
        "proxy_auth",
        "proxy_headers_hash",
        "server_hostname",
    }
)
_REQUIRED_KEY_FIELDS = ("host", "port", "is_ssl", "ssl")

#: Public aiohttp names the transport maps errors from / builds with.
_PUBLIC_NAMES = (
    "ClientSession",
    "ClientTimeout",
    "TCPConnector",
    "TraceConfig",
    "DummyCookieJar",
    "ConnectionTimeoutError",
    "SocketTimeoutError",
    "ClientConnectorError",
    "ServerDisconnectedError",
    "ClientPayloadError",
    "ClientResponseError",
    "InvalidURL",
    "ClientOSError",
    "ClientError",
)

_SELFTEST_TIMEOUT_SECONDS = 5.0
_STALE = b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\nsandloom-probe-STALE"


@dataclass(frozen=True)
class TransportProbe:
    """The verdict: ``ok``, or the fallback ``reason`` + what is wrong."""

    ok: bool
    reason: str | None = None
    problems: tuple[str, ...] = ()


_OK = TransportProbe(True)
_static: TransportProbe | None = None
_full: TransportProbe | None = None
_reported: set[tuple[str, ...]] = set()
_lock = threading.Lock()


def _installed() -> str:
    try:
        return metadata.version("aiohttp")
    except metadata.PackageNotFoundError:
        return "not installed"


# --- static checks ------------------------------------------------------------


def _name(obj: Any) -> str:
    return f"{getattr(obj, '__module__', '?')}.{getattr(obj, '__qualname__', obj)}"


def _need_callable(cls: Any, attr: str, problems: list[str]) -> None:
    member = getattr(cls, attr, None)
    if member is None:
        problems.append(f"{_name(cls)}.{attr}() is missing")
    elif not callable(member):
        problems.append(f"{_name(cls)}.{attr} is not callable")


def _static_problems() -> tuple[str | None, list[str]]:  # noqa: C901
    try:
        import aiohttp  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed
        from aiohttp.client_proto import ResponseHandler  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed
        from aiohttp.client_reqrep import ConnectionKey  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed
    except ImportError as exc:
        return REASON_IMPORT, [f"aiohttp cannot be imported: {exc}"]
    problems: list[str] = []
    for name in _PUBLIC_NAMES:
        if not hasattr(aiohttp, name):
            problems.append(f"aiohttp.{name} is missing")  # noqa: PERF401 - cold path (startup / diagnostics)
    trace = getattr(aiohttp, "TraceConfig", None)
    for hook in ("on_connection_queued_start", "on_connection_queued_end"):
        if trace is not None and hook not in dir(trace()):
            problems.append(f"aiohttp.TraceConfig.{hook} is missing")  # noqa: PERF401 - cold path (startup / diagnostics)

    connector = aiohttp.TCPConnector
    _need_callable(connector, "_create_connection", problems)
    _need_callable(connector, "close", problems)
    with contextlib.suppress(TypeError, ValueError):
        params = inspect.signature(connector.close).parameters
        if "abort_ssl" not in params:
            problems.append(f"{_name(connector)}.close() lost its abort_ssl keyword")
    with contextlib.suppress(TypeError, ValueError):
        init = inspect.signature(connector.__init__).parameters
        for kw in ("limit", "limit_per_host", "keepalive_timeout", "ssl"):
            if kw not in init:
                problems.append(f"{_name(connector)}() lost its {kw!r} keyword")  # noqa: PERF401 - cold path (startup / diagnostics)

    for attr in ("data_received", "close", "connection_lost", "force_close"):
        _need_callable(ResponseHandler, attr, problems)
    if not callable(getattr(ResponseHandler, "__len__", None)):
        problems.append(f"{_name(ResponseHandler)} has no __len__ (buffered messages)")
    if not isinstance(
        inspect.getattr_static(ResponseHandler, "should_close", None), property
    ):
        problems.append(f"{_name(ResponseHandler)}.should_close is not a property")

    fields = tuple(getattr(ConnectionKey, "_fields", ()))
    for field in _REQUIRED_KEY_FIELDS:
        if field not in fields:
            problems.append(f"ConnectionKey.{field} is missing")  # noqa: PERF401 - cold path (startup / diagnostics)
    unknown = sorted(set(fields) - _KNOWN_KEY_FIELDS)
    if unknown:
        problems.append(f"ConnectionKey has unknown fields {unknown}")
    return (REASON_INTERNALS if problems else None), problems


async def _instance_problems() -> list[str]:
    """Instance attributes (set in ``__init__``; need a running loop)."""
    import aiohttp  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed
    from aiohttp.client_proto import ResponseHandler  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed

    from sandloom.core.rpc_transport import _observed_connector_class  # noqa: PLC0415 - cold: rpc_transport imports httpx; it loads on the first outbound call (test_rpc_lazy_httpx)

    problems: list[str] = []
    plain = aiohttp.TCPConnector(limit=0, limit_per_host=2, keepalive_timeout=1.0)
    try:
        if not callable(getattr(plain, "_factory", None)):
            problems.append("TCPConnector()._factory (protocol factory) is missing")
        for attr in ("_ssl", "_loop"):
            if not hasattr(plain, attr):
                problems.append(f"TCPConnector().{attr} is missing")  # noqa: PERF401 - cold path (startup / diagnostics)
    finally:
        await plain.close()
    observed = _observed_connector_class()(
        limit=0, limit_per_host=2, keepalive_timeout=1.0
    )
    try:
        if not isinstance(getattr(observed, "_acquired", None), set):
            problems.append("TCPConnector()._acquired is not a set")
        for attr in ("_acquired_per_host", "_conns"):
            if not isinstance(getattr(observed, attr, None), Mapping):
                problems.append(f"TCPConnector().{attr} is not a mapping")  # noqa: PERF401 - cold path (startup / diagnostics)
    finally:
        await observed.close()
    handler = ResponseHandler(asyncio.get_running_loop())
    if not isinstance(getattr(handler, "_should_close", None), bool):
        problems.append("ResponseHandler()._should_close is not a bool")
    if not hasattr(handler, "transport"):
        problems.append("ResponseHandler().transport is missing")
    return problems


# --- behavioural self-test ------------------------------------------------------


class _SelfTestPeer:
    """Loopback HTTP/1.1 peer: answers ``GET /<path>`` with ``<path>``; after
    the FIRST answer it writes an unsolicited response on the idle
    connection."""

    def __init__(self) -> None:
        self.connections = 0
        self.first_closed = asyncio.Event()
        self.writers: list[asyncio.StreamWriter] = []

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        index = self.connections
        self.writers.append(writer)
        answered = 0
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                path = head.split(b" ", 2)[1].lstrip(b"/") if b" " in head else b""
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: %d\r\n\r\n%s" % (len(path), path)
                )
                await writer.drain()
                answered += 1
                if index == 1 and answered == 1:
                    await asyncio.sleep(0.02)  # the client parked it idle
                    writer.write(_STALE)
                    await writer.drain()
        except ConnectionError:
            return
        finally:
            if index == 1:
                self.first_closed.set()
            writer.close()


async def _selftest_problems() -> list[str]:
    from sandloom.core.rpc_transport import AiohttpRpcClient  # noqa: PLC0415 - cold: rpc_transport imports httpx; it loads on the first outbound call (test_rpc_lazy_httpx)

    peer = _SelfTestPeer()
    server = await asyncio.start_server(peer.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    client = AiohttpRpcClient(max_connections=4, idle_seconds=30.0)
    problems: list[str] = []
    try:
        # full_probe owns the deadline; a shorter nested connect limit can
        # reject a valid transport when the host is briefly descheduled.
        first = await client.get(f"{base}/sandloom-probe-1", headers={}, timeout=None)
        if first.content != b"sandloom-probe-1":
            problems.append(f"first call got {first.content[:32]!r}")
        try:
            await asyncio.wait_for(peer.first_closed.wait(), 2.0)
        except TimeoutError:
            problems.append(
                "an idle pooled connection that received unsolicited bytes "
                "was not closed"
            )
        second = await client.get(f"{base}/sandloom-probe-2", headers={}, timeout=None)
        if second.content != b"sandloom-probe-2":
            problems.append(
                "the next call got another response "
                f"({second.content[:32]!r}) instead of its own"
            )
        if peer.connections < 2:
            problems.append("the poisoned connection was reused for the next call")
        # The pool key the transport computes (per-peer metrics) is the key
        # aiohttp itself pooled the (now idle) second connection under.
        from aiohttp.client_reqrep import ConnectionKey  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed
        from yarl import URL  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed

        from sandloom.core.rpc_transport import _connection_key  # noqa: PLC0415 - cold: rpc_transport imports httpx; it loads on the first outbound call (test_rpc_lazy_httpx)

        connector = client._session.connector
        key = _connection_key(ConnectionKey, URL(base), connector)
        if key not in connector._conns and key not in connector._acquired_per_host:
            problems.append("the computed pool key does not match aiohttp's")
    finally:
        await client.aclose()
        server.close()
        for writer in peer.writers:
            writer.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()
    return problems


# --- verdicts -------------------------------------------------------------------


def _report(result: TransportProbe) -> None:
    if result.ok:
        return
    key = (result.reason or "", *result.problems)
    with _lock:
        if key in _reported:
            return
        _reported.add(key)
    _metrics.rpc_transport_fallback_total.labels(reason=result.reason).inc()
    logger.warning(
        "aiohttp %s is not usable as the internal-RPC transport (%s): %s. "
        "Outbound RPC falls back to httpx (requests keep working); see "
        "docs/operate/operator-reference.md 'Upgrading aiohttp'.",
        _installed(),
        result.reason,
        "; ".join(result.problems),
        extra={"event": "rpc.transport_fallback", "reason": result.reason},
    )


def static_probe() -> TransportProbe:
    """The cached static verdict (names / kinds; no I/O); never raises."""
    global _static
    result = _static
    if result is not None:
        return result
    with _lock:
        if _static is None:
            try:
                reason, problems = _static_problems()
            except Exception as error:  # noqa: BLE001 - never fail a caller
                reason, problems = REASON_CRASH, [f"probe crashed: {error!r}"]
            _static = (
                _OK if not problems else TransportProbe(False, reason, tuple(problems))
            )
        result = _static
    _report(result)
    return result


async def full_probe(
    timeout: float = _SELFTEST_TIMEOUT_SECONDS,  # noqa: ASYNC109 - explicit per-call timeout is the API
) -> TransportProbe:
    """Static + instance checks + the loopback self-test (cached)."""
    global _full
    if _full is not None:
        return _full
    result = static_probe()
    if result.ok:
        try:
            problems = await asyncio.wait_for(_instance_problems(), timeout)
            if problems:
                result = TransportProbe(False, REASON_INTERNALS, tuple(problems))
            else:
                problems = await asyncio.wait_for(_selftest_problems(), timeout)
                if problems:
                    result = TransportProbe(False, REASON_SELFTEST, tuple(problems))
        except TimeoutError:
            result = TransportProbe(
                False, REASON_SELFTEST, ("the loopback self-test timed out",)
            )
        except Exception as error:  # noqa: BLE001 - never fail startup
            result = TransportProbe(False, REASON_CRASH, (f"probe crashed: {error!r}",))
    _full = result
    _report(result)
    return result


def aiohttp_usable() -> bool:
    """True if the aiohttp transport may be used (full verdict if known)."""
    result = _full if _full is not None else static_probe()
    return result.ok


def reset_transport_probes() -> None:
    """Forget cached verdicts (tests only)."""
    global _static, _full
    with _lock:
        _static = None
        _full = None
        _reported.clear()


__all__ = [
    "REASON_CRASH",
    "REASON_IMPORT",
    "REASON_INTERNALS",
    "REASON_SELFTEST",
    "VERIFIED_AIOHTTP_RANGE",
    "TransportProbe",
    "aiohttp_usable",
    "full_probe",
    "reset_transport_probes",
    "static_probe",
]
