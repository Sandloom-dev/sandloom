"""Internal-RPC transports (``RPC_TRANSPORT``) under faults, on a real socket.

Every test runs against every transport (aiohttp, and httpx wherever the
behaviour is shared or the test needs no pool internals): a scripted asyncio
peer misbehaves in one specific way and the test checks what the proxy
surfaces, that the pool never hands a damaged connection to the next call
(no hung connection, no half-read response reused, no re-executed call), and
that the pool metrics (``sandloom_rpc_client_*``) move as expected.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from prometheus_client import REGISTRY

from sandloom.core import rpc, rpc_pair_mac
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import rpc_response_headers, rpc_settings
from sandloom.core.observability import metrics  # noqa: F401 - registers the series
from sandloom.core.rpc import (
    RemoteServiceBadResponse,
    RemoteServiceProxy,
    RemoteServiceUnavailable,
)
from sandloom.core.rpc import client as rpc_client
from sandloom.core.rpc import context as rpc_context
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.tenant import bind_tenant

OK_BODY = rpc_wire._pack({"ok": True, "result": "pong"})

#: This process calls itself: the scripted peer answers as ``svc``.
SVC = "svc"


@pytest.fixture(autouse=True)
def _keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in rpc_settings(
        SVC, allowed_calls={SVC: ["demo.scope.v1"]}
    ).items():
        monkeypatch.setattr(settings, name, value)


@dataclass(frozen=True)
class Call:
    """One request the peer read: its 1-based number (across connections)
    and its headers (the binding of the answer is keyed to them)."""

    n: int
    headers: dict[str, str]

    def binding(self, body: bytes = OK_BODY, status_code: int = 200) -> bytes:
        """The response-binding header lines a keyed ``svc`` provider adds."""
        lines = rpc_response_headers(
            settings, httpx.Headers(self.headers), status_code, body
        )
        return b"".join(f"{k}: {v}\r\n".encode() for k, v in lines.items())

    def ok(self, extra: bytes = b"") -> bytes:
        """This request's own, correctly bound ``"pong"`` answer."""
        return (
            b"HTTP/1.1 200 OK\r\ncontent-type: application/msgpack\r\n"
            + self.binding()
            + extra
            + b"content-length: %d\r\n\r\n" % len(OK_BODY)
            + OK_BODY
        )


def _parse_headers(head: bytes) -> dict[str, str]:
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        if not line:
            continue
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


Behavior = Callable[[asyncio.StreamWriter, Call], Awaitable[bool]]


class Peer:
    """A scripted HTTP/1.1 peer: ``behavior(writer, call)`` answers request
    ``call.n`` (1-based, across connections) and returns whether to keep the
    connection."""

    def __init__(self, behavior: Behavior | None = None) -> None:
        self.behavior = behavior or self._ok
        self.requests: list[bytes] = []
        self.connections = 0
        self.closed_by_client = 0
        self.writers: list[asyncio.StreamWriter] = []
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    @staticmethod
    async def _ok(writer: asyncio.StreamWriter, call: Call) -> bool:
        writer.write(call.ok())
        return True

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self.writers.append(writer)
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    self.closed_by_client += 1
                    return
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                body = await reader.readexactly(length) if length else b""
                self.requests.append(head + body)
                call = Call(len(self.requests), _parse_headers(head))
                keep = await self.behavior(writer, call)
                await writer.drain()
                if not keep:
                    writer.close()
                    return
        except ConnectionError:
            self.closed_by_client += 1
        finally:
            if not writer.is_closing():
                writer.close()

    async def start(self, ssl_ctx: ssl.SSLContext | None = None, port: int = 0) -> None:
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", port, ssl=ssl_ctx
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Stop like a restarting process: close every live connection too."""
        assert self.server is not None
        self.server.close()
        for writer in self.writers:
            writer.close()
        self.writers.clear()
        await self.server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@asynccontextmanager
async def peer(
    behavior: Behavior | None = None, ssl_ctx: ssl.SSLContext | None = None
) -> AsyncIterator[Peer]:
    p = Peer(behavior)
    await p.start(ssl_ctx)
    try:
        yield p
    finally:
        if p.server is not None and p.server.is_serving():
            await p.stop()


def proxy_for(
    url: str, client: Any, timeout: float = 2.0, service: str = "demo.scope.v1"
) -> RemoteServiceProxy:
    return RemoteServiceProxy(
        service_name=service,
        binding={"base_url": url, "service_id": SVC},
        default_timeout_seconds=timeout,
        client=client,
    )


POOLED = ["aiohttp"]
ALL = ["aiohttp", "httpx"]


def make_client(kind: str, **kw: Any) -> Any:
    from sandloom.core.rpc_transport import CLIENTS

    if kind == "httpx":
        assert not kw
        return rpc_client._new_rpc_client("httpx")
    return CLIENTS[kind](**kw)


def _sample(name: str, labels: dict[str, str]) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return float(value or 0.0)


class Pool:
    """The pool metrics of one (transport, peer), as deltas since creation."""

    REASONS = (
        "peer_close",
        "idle_eviction",
        "connection_close",
        "timeout",
        "cancelled",
        "oversize",
        "error",
        "shutdown",
    )

    def __init__(self, kind: str, port: int) -> None:
        self.labels = {"transport": kind, "peer": f"127.0.0.1:{port}"}
        self.base = self._counters()

    def _counters(self) -> dict[str, float]:
        out = {
            "opened": _sample(
                "sandloom_rpc_client_connections_opened_total", self.labels
            )
        }
        for reason in self.REASONS:
            out[reason] = _sample(
                "sandloom_rpc_client_connections_closed_total",
                {**self.labels, "reason": reason},
            )
        return out

    @property
    def opened(self) -> int:
        return int(self._counters()["opened"] - self.base["opened"])

    def closed(self) -> dict[str, int]:
        now = self._counters()
        return {
            r: int(now[r] - self.base[r])
            for r in self.REASONS
            if now[r] != self.base[r]
        }

    def state(self, state: str) -> int:
        return int(
            _sample("sandloom_rpc_client_connections", {**self.labels, "state": state})
        )

    @property
    def open_now(self) -> int:
        return self.state("active") + self.state("idle")


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc_proxy, "_RPC_RETRY_DELAY_SECONDS", 0)


# --------------------------------------------------------------------------
# the request on the wire: pair-MACed, signed, tenant context
# --------------------------------------------------------------------------

#: The tagged (codec 1) body of ``get_role(UUID(int=1), UUID(int=2))``.
GOLDEN_BODY = (
    b"\x82\xa4args\x92\x82\xae$sandloom_type\xa4uuid\xa5value\xd9$"
    b"00000000-0000-0000-0000-000000000001\x82\xae$sandloom_type\xa4uuid\xa5value"
    b"\xd9$00000000-0000-0000-0000-000000000002\xa6kwargs\x80"
)


def _parse_request(raw: bytes) -> tuple[dict[str, str], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    return _parse_headers(head), body


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ALL)
async def test_request_on_the_wire_is_pair_maced_with_tenant_context(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    from sandloom.core.observability.request_context import request_id_var

    monkeypatch.setattr(
        rpc_context, "time", types.SimpleNamespace(time=lambda: 1760000000.0)
    )
    client = make_client(transport)
    async with peer() as p:
        proxy = RemoteServiceProxy(
            service_name="tenancy.membership.v1",
            binding={"base_url": p.url, "service_id": SVC},
            default_timeout_seconds=2,
            client=client,
        )
        token = request_id_var.set("req-1")
        try:
            with bind_tenant(UUID(int=7)):
                assert await proxy.get_role(UUID(int=1), UUID(int=2)) == "pong"
        finally:
            request_id_var.reset(token)
        await client.aclose()
    (raw,) = p.requests
    headers, body = _parse_request(raw)
    assert body == GOLDEN_BODY
    assert headers["content-length"] == str(len(GOLDEN_BODY))
    assert headers["content-type"] == "application/msgpack"
    assert headers["accept"] == "application/msgpack"
    assert headers["x-request-id"] == "req-1"
    nonce = headers["x-sandloom-rpc-nonce"]
    # An undescribed method may write: the context names the signer and the
    # audience and carries the attempt's nonce under the Ed25519 signature.
    context = json.loads(rpc_wire._unb64(headers["x-sandloom-rpc-context"]))
    assert context == {
        "tenant_id": str(UUID(int=7)),
        "iat": 1760000000,
        "svc": SVC,
        "kid": "k1",
        "aud": SVC,
        "n": nonce,
    }
    assert headers["x-sandloom-rpc-service-signature"]
    auth = rpc_pair_mac.parse_header(headers["x-sandloom-rpc-auth"])
    assert auth["caller"] == SVC
    # No legacy credential travels any more.
    assert "x-internal-token" not in headers
    assert "x-sandloom-rpc-signature" not in headers


# --------------------------------------------------------------------------
# pooling
# --------------------------------------------------------------------------


async def _settle() -> None:
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_keep_alive_connection_is_reused(kind: str) -> None:
    client = make_client(kind)
    async with peer() as p:
        pool = Pool(kind, p.port)
        proxy = proxy_for(p.url, client)
        for _ in range(5):
            assert await proxy.ping() == "pong"
        assert p.connections == 1
        assert len(p.requests) == 5
        if kind != "httpx":
            assert pool.opened == 1
            assert (pool.state("active"), pool.state("idle")) == (0, 1)
    await client.aclose()
    await _settle()
    if kind != "httpx":
        assert pool.closed() == {"shutdown": 1}
        assert pool.open_now == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", POOLED)
async def test_pool_is_bounded_per_peer_and_waits_are_observed(kind: str) -> None:
    async def slow_ok(writer: asyncio.StreamWriter, call: Call) -> bool:
        await asyncio.sleep(0.02)
        writer.write(call.ok())
        return True

    client = make_client(kind, max_connections=2)
    async with peer(slow_ok) as p:
        pool = Pool(kind, p.port)
        proxy = proxy_for(p.url, client)
        waited = _sample("sandloom_rpc_client_pool_wait_seconds_count", pool.labels)
        results = await asyncio.gather(*(proxy.ping() for _ in range(10)))
        assert results == ["pong"] * 10
        assert p.connections == 2
        assert pool.opened == 2
        assert pool.open_now == 2
        assert (
            _sample("sandloom_rpc_client_pool_wait_seconds_count", pool.labels) - waited
            >= 8
        )
        assert _sample("sandloom_rpc_client_pool_waiting", pool.labels) == 0
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", POOLED)
async def test_pool_wait_is_bounded_by_the_timeout(kind: str) -> None:
    async def hang(writer: asyncio.StreamWriter, call: Call) -> bool:
        await asyncio.sleep(5)
        return False

    client = make_client(kind, max_connections=1)
    async with peer(hang) as p:
        pool = Pool(kind, p.port)
        proxy = proxy_for(p.url, client, timeout=0.2)
        outcomes = await asyncio.gather(
            proxy.ping(), proxy.ping(), return_exceptions=True
        )
        assert all(isinstance(o, RemoteServiceUnavailable) for o in outcomes)
        assert {o.code for o in outcomes} == {"rpc_timeout"}  # type: ignore[union-attr]
        await _settle()
        assert pool.open_now == 0  # nothing hung
        assert set(pool.closed()) == {"timeout"}
        assert _sample("sandloom_rpc_client_pool_waiting", pool.labels) == 0
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", POOLED)
async def test_idle_connections_are_evicted(kind: str) -> None:
    client = make_client(kind, idle_seconds=0.05)
    async with peer() as p:
        pool = Pool(kind, p.port)
        proxy = proxy_for(p.url, client)
        assert await proxy.ping() == "pong"
        await asyncio.sleep(0.2)
        assert await proxy.ping() == "pong"
        assert p.connections == 2
        assert pool.opened == 2
        assert pool.closed() == {"idle_eviction": 1}
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_connection_close_response_is_not_reused(kind: str) -> None:
    async def close_each(writer: asyncio.StreamWriter, call: Call) -> bool:
        writer.write(call.ok(b"connection: close\r\n"))
        return False

    client = make_client(kind)
    async with peer(close_each) as p:
        pool = Pool(kind, p.port)
        proxy = proxy_for(p.url, client)
        assert await proxy.ping() == "pong"
        assert await proxy.ping() == "pong"
        assert p.connections == 2
        assert len(p.requests) == 2  # no retry was needed
        await _settle()
        if kind != "httpx":
            assert pool.closed() == {"connection_close": 2}
            assert pool.open_now == 0
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_chunked_and_close_delimited_bodies(kind: str) -> None:
    half = len(OK_BODY) // 2

    async def chunked(writer: asyncio.StreamWriter, call: Call) -> bool:
        if call.n == 1:
            writer.write(
                b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n"
                + call.binding()
                + b"\r\n"
                + b"%x\r\n" % half
                + OK_BODY[:half]
                + b"\r\n"
                + b"%x\r\n" % (len(OK_BODY) - half)
                + OK_BODY[half:]
                + b"\r\n0\r\n\r\n"
            )
            return True
        writer.write(
            b"HTTP/1.1 200 OK\r\nconnection: close\r\n"
            + call.binding()
            + b"\r\n"
            + OK_BODY
        )
        return False

    client = make_client(kind)
    async with peer(chunked) as p:
        proxy = proxy_for(p.url, client)
        assert await proxy.ping() == "pong"  # chunked, kept alive
        assert await proxy.ping() == "pong"  # close-delimited
        assert p.connections == 1
    await client.aclose()


# --------------------------------------------------------------------------
# faults
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_peer_closes_mid_response_is_retried_once(kind: str) -> None:
    async def truncate_first(writer: asyncio.StreamWriter, call: Call) -> bool:
        if call.n == 1:
            writer.write(call.ok()[:-5])  # declared length, short body
            return False
        writer.write(call.ok())
        return True

    client = make_client(kind)
    async with peer(truncate_first) as p:
        pool = Pool(kind, p.port)
        proxy = proxy_for(p.url, client)
        assert await proxy.ping() == "pong"
        assert len(p.requests) == 2  # the existing single transport retry
        if kind != "httpx":
            await _settle()
            assert pool.open_now == 1
            assert sum(pool.closed().values()) == 1
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_peer_closing_mid_response_twice_is_503(kind: str) -> None:
    async def truncate(writer: asyncio.StreamWriter, call: Call) -> bool:
        writer.write(call.ok()[:-5])
        return False

    client = make_client(kind)
    async with peer(truncate) as p:
        pool = Pool(kind, p.port)
        proxy = proxy_for(p.url, client)
        with pytest.raises(RemoteServiceUnavailable) as info:
            await proxy.ping()
        assert "RemoteProtocolError" in str(info.value.detail)
        assert len(p.requests) == 2
        if kind != "httpx":
            await _settle()
            assert pool.open_now == 0
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_peer_restart_reconnects_transparently_when_nothing_was_sent(
    kind: str,
) -> None:
    client = make_client(kind)
    p = Peer()
    await p.start()
    pool = Pool(kind, p.port)
    proxy = proxy_for(p.url, client)
    assert await proxy.ping() == "pong"
    port = p.port
    await p.stop()  # the pooled connection is now stale
    await asyncio.sleep(0.05)  # the FIN reaches the client
    restarted = Peer()
    await restarted.start(port=port)
    try:
        assert await proxy.ping() == "pong"
        # Nothing was written to the dead connection and nothing was retried:
        # the restarted peer saw exactly one request, the old one exactly one.
        assert len(restarted.requests) == 1
        assert len(p.requests) == 1
        if kind != "httpx":
            assert pool.opened == 2
            assert pool.closed() == {"peer_close": 1}
    finally:
        await restarted.stop()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_request_written_then_peer_dies_uses_the_existing_retry(
    kind: str,
) -> None:
    async def die_first(writer: asyncio.StreamWriter, call: Call) -> bool:
        if call.n == 1:
            return False  # read the request, close without a byte of answer
        writer.write(call.ok())
        return True

    client = make_client(kind)
    async with peer(die_first) as p:
        proxy = proxy_for(p.url, client)
        assert await proxy.ping() == "pong"
        assert len(p.requests) == 2  # exactly one retry, never more
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_slow_peer_times_out_and_discards_the_connection(kind: str) -> None:
    async def never(writer: asyncio.StreamWriter, call: Call) -> bool:
        await asyncio.sleep(5)
        return False

    client = make_client(kind)
    async with peer(never) as p:
        pool = Pool(kind, p.port)
        proxy = proxy_for(p.url, client, timeout=0.1)
        with pytest.raises(RemoteServiceUnavailable) as info:
            await proxy.ping()
        assert info.value.code == "rpc_timeout"
        assert len(p.requests) == 2  # one retry, as before
        if kind != "httpx":
            await _settle()
            assert pool.open_now == 0
            assert pool.closed() == {"timeout": 2}
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", POOLED)
@pytest.mark.parametrize("framing", ["content-length", "chunked"])
async def test_oversized_response_is_502_and_not_retried(
    kind: str, framing: str
) -> None:
    big = b"x" * 4096

    async def huge(writer: asyncio.StreamWriter, call: Call) -> bool:
        if framing == "content-length":
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: %d\r\n\r\n" % len(big))
            writer.write(big)
        else:
            writer.write(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n")
            for _ in range(4):
                writer.write(b"400\r\n" + big[:1024] + b"\r\n")
            writer.write(b"0\r\n\r\n")
        return True

    client = make_client(kind, max_response_bytes=2048)
    async with peer(huge) as p:
        pool = Pool(kind, p.port)
        proxy = proxy_for(p.url, client)
        with pytest.raises(RemoteServiceBadResponse) as info:
            await proxy.ping()
        assert info.value.status_code == 502
        assert "RPC_MAX_RESPONSE_BYTES" in str(info.value.detail)
        assert len(p.requests) == 1
        await _settle()
        # aiohttp reads ahead: a body it already received IN FULL left the
        # connection at a clean message boundary and back in the pool;
        # otherwise it is closed as "oversize".
        assert pool.open_now <= 1
        assert set(pool.closed()) <= {"oversize"}
        # Never a corrupted next response.
        assert len(p.requests) == 1
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_cancellation_mid_call_discards_the_connection(kind: str) -> None:
    # Ordered by events, not sleeps (deterministic under machine load): the
    # call is cancelled only once the peer HAS the request, and the cancelled
    # call's late answer is on the wire before the next call is made.
    received = asyncio.Event()
    release = asyncio.Event()
    stale_written = asyncio.Event()

    async def late(writer: asyncio.StreamWriter, call: Call) -> bool:
        if call.n == 1:
            received.set()
            await release.wait()
            # The stale answer to the cancelled call, on its connection.
            writer.write(rpc_wire._pack({"ok": True, "result": "STALE"}))
            stale_written.set()
            return True
        writer.write(call.ok())
        return True

    client = make_client(kind)
    async with peer(late) as p:
        try:
            pool = Pool(kind, p.port)
            proxy = proxy_for(p.url, client)
            task = asyncio.create_task(proxy.ping())
            await asyncio.wait_for(received.wait(), timeout=10)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            await asyncio.wait_for(stale_written.wait(), timeout=10)
            # The next call gets a clean answer on a NEW connection, never the
            # cancelled call's late bytes.
            assert await proxy.ping() == "pong"
            assert p.connections == 2
            if kind != "httpx":
                assert pool.closed() == {"cancelled": 1}
                assert pool.open_now == 1
        finally:
            release.set()  # never leave the peer's handler parked
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", POOLED)
async def test_unsolicited_bytes_never_reach_the_next_call(kind: str) -> None:
    client = make_client(kind)
    async with peer() as p:
        proxy = proxy_for(p.url, client)
        assert await proxy.ping() == "pong"
        stale = rpc_wire._pack({"ok": True, "result": "STALE"})
        p.writers[0].write(
            b"HTTP/1.1 200 OK\r\ncontent-length: %d\r\n\r\n" % len(stale) + stale
        )
        await _settle()
        assert await proxy.ping() == "pong"
        assert await proxy.ping() == "pong"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", POOLED)
async def test_commands_are_not_reexecuted_by_the_transport(kind: str) -> None:
    """A transport fault after the provider ran a command: the single retry
    carries the SAME arguments — the command's own idempotency key, deduped
    by the provider — and nothing is sent a third time."""

    async def answer_then_die(writer: asyncio.StreamWriter, call: Call) -> bool:
        if call.n == 1:
            writer.write(call.ok()[:20])  # ran, reply cut
            return False
        writer.write(call.ok())
        return True

    client = make_client(kind)
    async with peer(answer_then_die) as p:
        proxy = proxy_for(p.url, client)
        assert await proxy.charge(5, "order-7") == "pong"
        assert len(p.requests) == 2
        bodies = {r.partition(b"\r\n\r\n")[2] for r in p.requests}
        assert len(bodies) == 1  # byte-identical retry (same key, same args)
        # ... under a fresh nonce: a retry is never a replay of attempt 1.
        nonces = {_parse_headers(r)["x-sandloom-rpc-nonce"] for r in p.requests}
        assert len(nonces) == 2
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_unreachable_peer_is_503_after_one_retry(kind: str) -> None:
    p = Peer()
    await p.start()
    url = p.url
    await p.stop()
    client = make_client(kind)
    proxy = proxy_for(url, client)
    with pytest.raises(RemoteServiceUnavailable) as info:
        await proxy.ping()
    assert "ConnectError" in str(info.value.detail)  # the same reason as httpx
    await client.aclose()


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------


def _self_signed(tmp: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL)
async def test_tls_self_signed_trusted_via_ssl_cert_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    cert_path, key_path = _self_signed(tmp_path)
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ctx.load_cert_chain(cert_path, key_path)
    async with peer(ssl_ctx=server_ctx) as p:
        url = f"https://localhost:{p.port}"
        # Untrusted: the handshake fails like httpx's (ConnectError -> 503).
        untrusted = make_client(kind)
        with pytest.raises(RemoteServiceUnavailable) as info:
            await proxy_for(url, untrusted).ping()
        assert "ConnectError" in str(info.value.detail)
        await untrusted.aclose()
        # Trusted the way httpx trusts it: SSL_CERT_FILE.
        monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
        client = make_client(kind)
        proxy = proxy_for(url, client)
        assert await proxy.ping() == "pong"
        assert await proxy.ping() == "pong"
        assert p.connections == 1  # TLS connection kept alive
        await client.aclose()


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_transport_setting_selects_the_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.rpc_transport import AiohttpRpcClient

    await rpc.close_rpc_client()
    assert type(settings).model_fields["rpc_transport"].default == "aiohttp"
    monkeypatch.setattr(settings, "rpc_transport", "aiohttp")
    assert isinstance(rpc_client._rpc_client(), AiohttpRpcClient)
    monkeypatch.setattr(settings, "rpc_transport", "httpx")
    assert isinstance(rpc_client._rpc_client(), httpx.AsyncClient)
    await rpc.close_rpc_client()
