"""Pooled HTTP/1.1 client for internal RPC (``RPC_TRANSPORT``).

Why not httpx: on the RPC hot path the httpx / httpcore / h11 / anyio stack
costs ~0.7–0.85 ms of caller CPU per call at concurrency 64 (pool scheduling
polls every idle connection per request, the absolute URL is re-parsed, h11
parses in Python, cookies / auth / redirect layers run for nothing). With
aiohttp (C HTTP parser, its own pool) the same call costs ~0.12 ms; one R3o
RPC drops from ~1.25 ms to ~0.36 ms CPU in total (caller + provider + PG)
(``benchmarks/ladder/RESULTS.md``, "RPC transport A/B"). A purpose-built
asyncio + httptools client saved a further ~0.08 ms per call; that was not
judged worth owning an HTTP client (it lives in commit 27d822b).

:class:`AiohttpRpcClient` (``aiohttp``, the default) and httpx
(``RPC_TRANSPORT=httpx``, the previous client, built in :mod:`sandloom.core.rpc`)
expose the same ``post(url, content=, headers=, timeout=)`` /
``get(url, headers=, timeout=)`` surface and the aiohttp client raises
**httpx's exception classes**, so the proxy's error mapping (and the reasons
in its ``503`` details) and its single transport retry are identical.

aiohttp client semantics:

* HTTP/1.1, one request in flight per connection (no pipelining), at most
  ``RPC_POOL_MAX_CONNECTIONS`` connections per peer (callers beyond it wait
  within their timeout), idle keep-alive connections evicted after
  ``RPC_POOL_IDLE_SECONDS``; no proxies (``HTTP(S)_PROXY`` are ignored — use
  ``RPC_TRANSPORT=httpx`` behind an egress proxy), no redirects, no cookies,
  no ``User-Agent`` / ``Accept-Encoding``.
* TLS through :mod:`ssl` with the SAME default context httpx builds
  (certifi, or ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``) and hostname checks.
* The call's timeout bounds the wait for a pooled connection + the connect,
  and the response's read inactivity (httpx's read-timeout semantics).
* ``Content-Length``, chunked and close-delimited bodies;
  ``Connection: close`` closes the connection; a body above
  ``RPC_MAX_RESPONSE_BYTES`` is :class:`RpcResponseTooLarge` (→ 502).
* A connection goes back to the pool only at a clean message boundary.
  Timeout, cancellation, a parse error close it; bytes arriving on an IDLE
  pooled connection poison it (stock aiohttp would queue them as the next
  caller's response). A pooled connection the peer already closed is
  replaced before anything is written; once a byte of the request was written
  a failure is surfaced and the proxy's single transport retry decides.
* Pool metrics (``sandloom_rpc_client_*``): connections by state, callers
  waiting, pool wait time, connections opened, connections closed by reason.

The pool observation and the idle-connection guard subclass aiohttp's
``TCPConnector`` / ``ResponseHandler`` and read their internals
(``_acquired``, ``_acquired_per_host``, ``_conns``, ``_factory``,
``_create_connection``, ``_should_close``): the pin in ``pyproject.toml`` is
the verified range; :mod:`sandloom.core.rpc_transport_compat` probes them (names,
kinds and a loopback self-test of the idle-connection guard) and the shared
client falls back to httpx when anything moved. Correctness does not rest on
them: every response is bound to its request (nonce + MAC, checked in
:mod:`sandloom.core.rpc` for both transports), so a misrouted response is rejected
and the pool discarded (:meth:`AiohttpRpcClient.reset_pool`).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import ssl
import sys
import time
import weakref
from collections.abc import Callable
from typing import Any

import httpx

from sandloom.core.observability import metrics as _metrics


class RpcResponseTooLarge(httpx.DecodingError):
    """The provider's response exceeds ``RPC_MAX_RESPONSE_BYTES``."""


class RpcResponseBindingError(httpx.TransportError):
    """A response that is not bound to its own request (another call's, a
    stale or unsolicited one, or unbound in strict mode). Never returned to
    the caller; a transport error, so the proxy's single retry applies."""


#: How long a discarded pool keeps serving its in-flight calls before it is
#: closed (no NEW call ever uses it).
RETIRED_POOL_GRACE_SECONDS = 60.0

_closing_tasks: set[asyncio.Task[Any]] = set()


def close_later(
    close: Callable[[], Any], delay: float = RETIRED_POOL_GRACE_SECONDS
) -> None:
    """Run ``close()`` (sync or async) after ``delay`` on the running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    def fire() -> None:
        with contextlib.suppress(Exception):
            result = close()
            if asyncio.iscoroutine(result):
                task = loop.create_task(result)
                _closing_tasks.add(task)
                task.add_done_callback(_closing_tasks.discard)

    loop.call_later(delay, fire)


class RpcResponse:
    """The parts of a response the RPC proxy reads (httpx-compatible names)."""

    __slots__ = ("status_code", "headers", "content")

    def __init__(self, status_code: int, headers: Any, content: bytes) -> None:
        self.status_code = status_code
        #: ``.get(name)`` is case-insensitive.
        self.headers = headers
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class HttpxRpcClient(httpx.AsyncClient):
    """Bounded streaming fallback with the same response cap as aiohttp.

    Inherit the public httpx surface for transport compatibility, but consume
    each body under the cap before the proxy can decode it.
    """

    kind = "httpx"

    def __init__(
        self,
        *,
        max_connections: int = 100,
        max_response_bytes: int = 32 * 1024 * 1024,
        trust_env: bool = False,
        transport: Any = None,
    ) -> None:
        self._max_response_bytes = max_response_bytes
        super().__init__(
            trust_env=trust_env,
            transport=transport,
            # Disabling ambient proxies must not disable the documented custom
            # CA-file/directory support used by the primary transport too.
            verify=_default_tls(),
            headers={"Accept-Encoding": "identity"},
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=min(20, max_connections),
            ),
        )

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: Any = httpx.USE_CLIENT_DEFAULT,
        follow_redirects: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        response = await super().send(
            request, stream=True, auth=auth, follow_redirects=follow_redirects
        )
        try:
            limit = self._max_response_bytes
            if limit > 0:
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > limit:
                    raise RpcResponseTooLarge(
                        "RPC response exceeds RPC_MAX_RESPONSE_BYTES"
                    )
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if limit > 0 and len(body) + len(chunk) > limit:
                    raise RpcResponseTooLarge(
                        "RPC response exceeds RPC_MAX_RESPONSE_BYTES"
                    )
                body.extend(chunk)
            response._content = bytes(body)
            return response
        finally:
            await response.aclose()


# ---------------------------------------------------------------------------
# pool metrics
# ---------------------------------------------------------------------------

#: Why a pooled connection closed (``reason`` label).
CLOSE_REASONS = (
    "peer_close",
    "idle_eviction",
    "connection_close",
    "timeout",
    "cancelled",
    "oversize",
    "error",
    "shutdown",
)


class PoolMetrics:
    """Prometheus children of one (transport, peer) pool, resolved once."""

    __slots__ = ("labels", "opened", "waiting", "wait_seconds", "_closed")

    def __init__(self, transport: str, peer: str) -> None:
        self.labels = (transport, peer)
        self.opened = _metrics.rpc_client_connections_opened_total.labels(
            transport, peer
        )
        self.waiting = _metrics.rpc_client_pool_waiting.labels(transport, peer)
        self.wait_seconds = _metrics.rpc_client_pool_wait_seconds.labels(
            transport, peer
        )
        self._closed: dict[str, Any] = {}

    def closed(self, reason: str) -> None:
        child = self._closed.get(reason)
        if child is None:
            child = _metrics.rpc_client_connections_closed_total.labels(
                *self.labels, reason
            )
            self._closed[reason] = child
        child.inc()

    def track_states(self, active: Callable[[], int], idle: Callable[[], int]) -> None:
        """Scrape-time gauges (no bookkeeping on the hot path)."""
        transport, peer = self.labels
        _metrics.rpc_client_connections.labels(transport, peer, "active").set_function(
            active
        )
        _metrics.rpc_client_connections.labels(transport, peer, "idle").set_function(
            idle
        )


def _weak_count(obj: Any, fn: Callable[[Any], int]) -> Callable[[], int]:
    ref = weakref.ref(obj)

    def count() -> int:
        target = ref()
        if target is None:
            return 0
        try:
            return fn(target)
        except Exception:  # noqa: BLE001 - a scrape must never fail
            return 0

    return count


def _reason_from_exception(exc: BaseException | None) -> str:
    if exc is None:
        return "error"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, RpcResponseTooLarge):
        return "oversize"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    return "error"


def _default_tls() -> ssl.SSLContext:
    # The exact context httpx builds by default (certifi, or SSL_CERT_FILE /
    # SSL_CERT_DIR), so TLS trust is unchanged by the transport switch.
    return httpx.create_ssl_context()


def _peer_label(host: str, port: int) -> str:
    return f"{host}:{port}"


#: Bound of the per-URL caches.
_MAX_ROUTES = 4096


# ===========================================================================
# aiohttp (default)
# ===========================================================================


def _aiohttp() -> Any:
    import aiohttp  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed

    return aiohttp


def _observed_handler_class() -> Any:  # noqa: C901
    """A ResponseHandler that records WHY its connection closed."""
    from aiohttp.client_proto import ResponseHandler  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed

    class _ObservedHandler(ResponseHandler):
        def __init__(self, loop: asyncio.AbstractEventLoop, connector: Any) -> None:
            super().__init__(loop)
            self._sandloom_connector = connector
            self._sandloom_metrics: PoolMetrics | None = None
            self._sandloom_reason: str | None = None

        def data_received(self, data: bytes) -> None:
            acquired = self._sandloom_connector._acquired
            # (aiohttp calls data_received(b"") itself to resume parsing.)
            if data and self not in acquired:
                # Bytes on an IDLE pooled connection (nobody asked): aiohttp
                # would queue them as the NEXT caller's response. Never reuse.
                self._poison()
                return
            super().data_received(data)
            if data and len(self) and self not in acquired:
                # A complete response released the connection and MORE bytes
                # followed in the same read: an unsolicited message is queued.
                self._poison()

        def _poison(self) -> None:
            if self._sandloom_reason is None:
                self._sandloom_reason = "error"
            self.force_close()
            transport = self.transport
            if transport is not None:
                transport.abort()

        def close(self) -> None:
            if self._sandloom_reason is None and self.transport is not None:
                in_flight = sys.exc_info()[1]
                if in_flight is not None:
                    # Closed while an exception unwinds: timeout, cancel, …
                    self._sandloom_reason = _reason_from_exception(in_flight)
                elif self._sandloom_connector._sandloom_closing:
                    self._sandloom_reason = "shutdown"
                elif self.should_close:
                    self._sandloom_reason = "connection_close"
                else:
                    self._sandloom_reason = "idle_eviction"
            super().close()

        def connection_lost(self, exc: BaseException | None) -> None:
            if self._sandloom_reason is None:
                if exc is not None:
                    self._sandloom_reason = "error"
                elif getattr(self, "_should_close", False):
                    self._sandloom_reason = "connection_close"  # announced close
                else:
                    self._sandloom_reason = "peer_close"
            if self._sandloom_metrics is not None:
                self._sandloom_metrics.closed(self._sandloom_reason)
                self._sandloom_metrics = None
            super().connection_lost(exc)

    return _ObservedHandler


@functools.cache
def _observed_connector_class() -> Any:
    base: Any = _aiohttp().TCPConnector
    handler_class = _observed_handler_class()

    class _ObservedConnector(base):  # type: ignore[misc]
        """TCPConnector + per-peer pool metrics (opened / closed by reason /
        active / idle)."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._sandloom_closing = False
            self._sandloom_metrics: dict[Any, PoolMetrics] = {}
            # aiohttp builds every connection's protocol with this factory.
            self._factory = functools.partial(
                handler_class, loop=self._loop, connector=self
            )

        def pool_metrics(self, key: Any, host: str, port: int) -> PoolMetrics:
            pm = self._sandloom_metrics.get(key)
            if pm is None:
                pm = PoolMetrics("aiohttp", _peer_label(host, port))
                pm.track_states(
                    _weak_count(self, lambda c: len(c._acquired_per_host.get(key, ()))),
                    _weak_count(self, lambda c: len(c._conns.get(key, ()))),
                )
                self._sandloom_metrics[key] = pm
            return pm

        async def _create_connection(self, req: Any, traces: Any, timeout: Any) -> Any:  # noqa: ASYNC109 - aiohttp TCPConnector override signature
            proto = await super()._create_connection(req, traces, timeout)
            pm = self.pool_metrics(req.connection_key, req.host, req.port)
            pm.opened.inc()
            proto._sandloom_metrics = pm
            return proto

        def close(self, *, abort_ssl: bool = False) -> Any:
            self._sandloom_closing = True
            return super().close(abort_ssl=abort_ssl)

    return _ObservedConnector


def _trace_config() -> Any:
    """Pool-wait observability through aiohttp's public tracing hooks."""
    aiohttp = _aiohttp()

    async def queued_start(session: Any, ctx: Any, params: Any) -> None:
        probe = ctx.trace_request_ctx
        if probe is not None:
            probe.start()

    async def queued_end(session: Any, ctx: Any, params: Any) -> None:
        probe = ctx.trace_request_ctx
        if probe is not None:
            probe.end()

    config = aiohttp.TraceConfig()
    config.on_connection_queued_start.append(queued_start)
    config.on_connection_queued_end.append(queued_end)
    return config


class AiohttpRpcClient:
    """Process-wide pooled RPC client on aiohttp (one session per loop)."""

    kind = "aiohttp"

    def __init__(
        self,
        *,
        max_connections: int = 100,
        idle_seconds: float = 4.0,
        max_response_bytes: int = 32 * 1024 * 1024,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._aiohttp = _aiohttp()
        self._max_connections = max(1, int(max_connections))
        self._idle_seconds = float(idle_seconds)
        self._max_response_bytes = int(max_response_bytes)
        self._ssl_context = ssl_context
        self._session: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timeouts: dict[float | None, Any] = {}
        #: url -> (yarl URL, pool metrics of its peer)
        self._urls: dict[str, tuple[Any, PoolMetrics | None]] = {}
        self._retired: list[Any] = []
        self.is_closed = False

    async def post(
        self,
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
        timeout: float | None,  # noqa: ASYNC109 - explicit per-call timeout is the API
    ) -> RpcResponse:
        return await self._request("POST", url, content, headers, timeout)

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float | None,  # noqa: ASYNC109 - explicit per-call timeout is the API
    ) -> RpcResponse:
        return await self._request("GET", url, None, headers, timeout)

    async def aclose(self) -> None:
        self.is_closed = True
        session, self._session = self._session, None
        self._urls = {}
        retired, self._retired = self._retired, []
        for each in (*retired, session):
            if each is not None and not each.closed:
                await each.close()

    def reset_pool(self) -> None:
        """Stop using every pooled connection: the next call opens a fresh
        pool; calls in flight on the old one finish, then it is closed.

        Called when a response was not bound to its request — a connection's
        message stream can no longer be trusted, whichever one it was.
        """
        session, self._session = self._session, None
        self._urls = {}
        if session is not None and not session.closed:
            self._retired = [s for s in self._retired if not s.closed]
            self._retired.append(session)
            close_later(session.close)

    # -- internals -----------------------------------------------------------
    def _session_for(self, loop: asyncio.AbstractEventLoop) -> Any:
        session = self._session
        if session is not None and loop is self._loop and not session.closed:
            return session
        if session is not None and not session.closed:
            # A new event loop (tests, a forked worker): abandon the old pool.
            with contextlib.suppress(Exception):
                session.connector.close()
        aiohttp = self._aiohttp
        connector = _observed_connector_class()(
            limit=0,
            limit_per_host=self._max_connections,
            keepalive_timeout=self._idle_seconds,
            ssl=self._ssl_context or _default_tls(),
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.DummyCookieJar(),
            skip_auto_headers=("User-Agent", "Accept-Encoding"),
            trace_configs=[_trace_config()],
            raise_for_status=False,
        )
        self._loop = loop
        self._urls = {}
        return self._session

    def _timeout(self, timeout: float | None) -> Any:
        value = self._timeouts.get(timeout)
        if value is None:
            # connect = pool wait + TCP/TLS connect; sock_read = read
            # inactivity (httpx's read-timeout semantics).
            value = self._aiohttp.ClientTimeout(
                total=None, connect=timeout, sock_read=timeout
            )
            if len(self._timeouts) > 64:
                self._timeouts.clear()
            self._timeouts[timeout] = value
        return value

    def _target(self, session: Any, url: str) -> tuple[Any, PoolMetrics | None]:
        target = self._urls.get(url)
        if target is None:
            from yarl import URL  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed

            parsed = URL(url)
            if parsed.scheme not in ("http", "https") or not parsed.host:
                raise httpx.UnsupportedProtocol(
                    f"Request URL has an unsupported protocol or no host: {url!r}"
                )
            from aiohttp.client_reqrep import ConnectionKey  # noqa: PLC0415 - cold: aiohttp / yarl internals are imported only when the aiohttp transport is built or probed

            pm = None
            with contextlib.suppress(Exception):
                pm = session.connector.pool_metrics(
                    _connection_key(ConnectionKey, parsed, session.connector),
                    parsed.host,
                    parsed.port,
                )
            target = (parsed, pm)
            if len(self._urls) >= _MAX_ROUTES:
                self._urls.clear()
            self._urls[url] = target
        return target

    async def _request(  # noqa: C901
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float | None,  # noqa: ASYNC109 - explicit per-call timeout is the API
    ) -> RpcResponse:
        if self.is_closed:
            raise httpx.ConnectError("RPC client is closed")
        aiohttp = self._aiohttp
        session = self._session_for(asyncio.get_running_loop())
        target, pm = self._target(session, url)
        probe = _WaitProbe(pm) if pm is not None else None
        try:
            async with session.request(
                method,
                target,
                data=body,
                headers=headers,
                timeout=self._timeout(timeout),
                allow_redirects=False,
                trace_request_ctx=probe,
            ) as resp:
                try:
                    content = await self._read(resp)
                except BaseException as exc:
                    _mark_close(resp, _reason_from_exception(exc))
                    resp.close()
                    raise
                return RpcResponse(resp.status, resp.headers, content)
        except (httpx.HTTPError, asyncio.CancelledError):
            raise
        except aiohttp.ConnectionTimeoutError as exc:
            raise httpx.ConnectTimeout(str(exc) or "RPC connect timed out") from exc
        except (aiohttp.SocketTimeoutError, TimeoutError) as exc:
            raise httpx.ReadTimeout(str(exc) or "RPC response timed out") from exc
        except aiohttp.ClientConnectorError as exc:
            raise httpx.ConnectError(str(exc)) from exc
        except (aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError) as exc:
            raise httpx.RemoteProtocolError(str(exc)) from exc
        except aiohttp.ClientResponseError as exc:
            raise httpx.RemoteProtocolError(str(exc)) from exc
        except aiohttp.InvalidURL as exc:
            raise httpx.UnsupportedProtocol(str(exc)) from exc
        except aiohttp.ClientOSError as exc:
            raise httpx.ReadError(str(exc)) from exc
        except aiohttp.ClientError as exc:
            raise httpx.TransportError(str(exc) or exc.__class__.__name__) from exc
        except ValueError as exc:  # e.g. an illegal header value
            raise httpx.LocalProtocolError(str(exc)) from exc
        finally:
            if probe is not None and probe.started is not None:
                probe.end()

    async def _read(self, resp: Any) -> bytes:
        limit = self._max_response_bytes
        length = resp.content_length
        if length is not None:
            if length > limit:
                raise RpcResponseTooLarge(
                    f"response of {length} bytes exceeds RPC_MAX_RESPONSE_BYTES={limit}"
                )
            data: bytes = await resp.read()
            return data
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await resp.content.read(65536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > limit:
                raise RpcResponseTooLarge(
                    f"response exceeds RPC_MAX_RESPONSE_BYTES={limit}"
                )
            chunks.append(chunk)


class _WaitProbe:
    """One call's pool wait (aiohttp fires no "queued end" on a timeout or
    cancellation, so the call itself closes an open wait)."""

    __slots__ = ("metrics", "started")

    def __init__(self, metrics: PoolMetrics) -> None:
        self.metrics = metrics
        self.started: float | None = None

    def start(self) -> None:
        self.started = time.perf_counter()
        self.metrics.waiting.inc()

    def end(self) -> None:
        started, self.started = self.started, None
        if started is not None:
            self.metrics.waiting.dec()
            self.metrics.wait_seconds.observe(time.perf_counter() - started)


def _connection_key(key_class: Any, url: Any, connector: Any) -> Any:
    """aiohttp's pool key of ``url`` (host, port, is_ssl, ssl, proxy…)."""
    fields = getattr(key_class, "_fields", ())
    values = {
        "host": url.raw_host,
        "port": url.port,
        "is_ssl": url.scheme == "https",
        "ssl": connector._ssl if url.scheme == "https" else True,
        "proxy": None,
        "proxy_auth": None,
        "proxy_headers_hash": None,
        "server_hostname": None,
    }
    return key_class(*(values.get(name) for name in fields))


def _mark_close(resp: Any, reason: str) -> None:
    proto = getattr(getattr(resp, "connection", None), "protocol", None)
    if proto is not None and getattr(proto, "_sandloom_reason", "") is None:
        proto._sandloom_reason = reason


#: RPC_TRANSPORT values -> client class (``httpx`` is built in sandloom.core.rpc).
CLIENTS: dict[str, Any] = {"aiohttp": AiohttpRpcClient}

__all__ = [
    "CLIENTS",
    "CLOSE_REASONS",
    "AiohttpRpcClient",
    "PoolMetrics",
    "RpcResponse",
    "RpcResponseBindingError",
    "RpcResponseTooLarge",
    "close_later",
]
