"""Response binding: an RPC response is never accepted for another call.

Every request carries a fresh nonce; the provider echoes it with a MAC under
the per-pair key over (nonce, the request's MAC, status, body). These tests run a scripted HTTP/1.1 peer on a real
socket that misbehaves on purpose — stale / replayed / swapped / unsolicited
/ truncated / unbound / unkeyed responses, cancellations — against BOTH transports
(aiohttp, httpx) and, for aiohttp, also with its idle-connection guard
switched OFF (stock aiohttp), proving the guarantee does not rest on the
client's internals. Each call's expected result is unique, so "returned
another call's result" is directly observable.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY

from sandloom.core import rpc, rpc_binding, rpc_pair_mac, rpc_transport
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import rpc_keys, rpc_request_headers, rpc_settings
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method
from sandloom.core.observability import metrics  # noqa: F401 - registers the series
from sandloom.core.rpc import (
    RemoteServiceBadResponse,
    RemoteServiceProxy,
    RemoteServiceUnavailable,
)
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc import wire as rpc_wire

#: This process calls itself: the scripted peer answers as ``svc``.
SVC = "svc"


@pytest.fixture(autouse=True)
def _keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in rpc_settings(
        SVC, allowed_calls={SVC: ["demo.echo.v1", "demo.nope.v1"]}
    ).items():
        monkeypatch.setattr(settings, name, value)


def _rejected(reason: str) -> float:
    value = REGISTRY.get_sample_value(
        "sandloom_rpc_response_binding_rejected_total", {"reason": reason}
    )
    return float(value or 0.0)


def _rejected_all() -> float:
    return sum(_rejected(r) for r in rpc_binding.REJECT_REASONS)


# --------------------------------------------------------------------------
# a binding-aware scripted peer
# --------------------------------------------------------------------------


def http_response(
    body: bytes,
    *,
    nonce: str | None,
    headers: dict[str, str] | None = None,
    mac_body: bytes | None = None,
    provider: Any = None,
) -> bytes:
    """A response bound like a provider: keyed with ``provider``'s (default
    ``svc``'s) pair key towards the caller the request's MAC names."""
    head = b"HTTP/1.1 200 OK\r\ncontent-type: application/msgpack\r\n"
    if nonce is not None:
        sent = httpx.Headers(headers or {})
        caller = rpc_pair_mac.parse_header(sent[rpc_pair_mac.AUTH_HEADER])
        key = rpc_binding.provider_key(
            provider or settings, (caller["caller"], caller["ck"])
        )
        for name, value in rpc_binding.response_headers(
            nonce=nonce,
            key=key,
            req_auth=rpc_binding.request_auth(sent),
            status_code=200,
            body=body if mac_body is None else mac_body,
        ):
            head += f"{name}: {value}\r\n".encode()
    return head + b"content-length: %d\r\n\r\n" % len(body) + body


def result_body(value: str) -> bytes:
    return rpc_wire._pack({"ok": True, "result": value})


@dataclass
class Req:
    n: int
    conn: int
    nonce: str | None
    arg: str
    writer: asyncio.StreamWriter
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def expected(self) -> str:
        return f"r-{self.arg}"

    @property
    def good(self) -> bytes:
        """This request's own, correctly bound response."""
        return http_response(
            result_body(self.expected), nonce=self.nonce, headers=self.headers
        )


Behavior = Callable[["BindingPeer", Req], Awaitable[bool]]


@dataclass
class BindingPeer:
    behavior: Behavior
    requests: list[Req] = field(default_factory=list)
    connections: int = 0
    writers: list[asyncio.StreamWriter] = field(default_factory=list)
    server: Any = None
    port: int = 0

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        conn = self.connections
        self.writers.append(writer)
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                headers: dict[str, str] = {}
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.decode("latin-1").partition(":")
                    headers[name.strip().lower()] = value.strip()
                length = int(headers.get("content-length", "0") or 0)
                body = await reader.readexactly(length) if length else b""
                args = rpc_wire._unpack(body).get("args", []) if body else []
                req = Req(
                    n=len(self.requests) + 1,
                    conn=conn,
                    nonce=headers.get("x-sandloom-rpc-nonce"),
                    arg=str(args[0]) if args else "",
                    writer=writer,
                    headers=headers,
                )
                self.requests.append(req)
                keep = await self.behavior(self, req)
                with _suppress_conn():
                    await writer.drain()
                if not keep:
                    writer.close()
                    return
        except ConnectionError:
            return
        finally:
            if not writer.is_closing():
                writer.close()

    def other(self, req: Req) -> Req | None:
        """Some OTHER call (another request) to impersonate."""
        others = [r for r in self.requests if r.arg != req.arg]
        return others[-1] if others else None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class _suppress_conn:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return exc[0] is not None and issubclass(exc[0], ConnectionError)


async def answer_ok(peer: BindingPeer, req: Req) -> bool:
    req.writer.write(req.good)
    return True


@asynccontextmanager
async def binding_peer(behavior: Behavior = answer_ok) -> AsyncIterator[BindingPeer]:
    p = BindingPeer(behavior)
    p.server = await asyncio.start_server(p.handle, "127.0.0.1", 0)
    p.port = p.server.sockets[0].getsockname()[1]
    try:
        yield p
    finally:
        p.server.close()
        for writer in p.writers:
            writer.close()
        await p.server.wait_closed()


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

#: aiohttp, aiohttp with its idle-connection guard OFF (stock aiohttp), httpx.
TRANSPORTS = ["aiohttp", "aiohttp-unguarded", "httpx"]


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc_proxy, "_RPC_RETRY_DELAY_SECONDS", 0)


@pytest.fixture
async def shared_client(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[str]:
    """Select the SHARED client of ``RPC_TRANSPORT`` (the production path,
    where a rejected response discards the whole pool)."""
    kind: str = request.param
    await rpc.close_rpc_client()
    if kind == "aiohttp-unguarded":
        import aiohttp

        # aiohttp builds every connection with its stock ResponseHandler: no
        # idle-bytes guard, no pool observation.
        monkeypatch.setattr(
            rpc_transport, "_observed_connector_class", lambda: aiohttp.TCPConnector
        )
    monkeypatch.setattr(
        settings, "rpc_transport", "httpx" if kind == "httpx" else "aiohttp"
    )
    yield kind
    await rpc.close_rpc_client()


def proxy_for(url: str, timeout: float = 2.0) -> RemoteServiceProxy:
    return RemoteServiceProxy(
        service_name="demo.echo.v1",
        binding={"base_url": url, "service_id": SVC},
        default_timeout_seconds=timeout,
    )


parametrize_transports = pytest.mark.parametrize(
    "shared_client", TRANSPORTS, indirect=True
)


# --------------------------------------------------------------------------
# the binding on the wire
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@parametrize_transports
async def test_each_attempt_sends_a_fresh_nonce_and_a_bound_reply_is_accepted(
    shared_client: str,
) -> None:
    async with binding_peer() as p:
        proxy = proxy_for(p.url)
        assert await proxy.echo("a") == "r-a"
        assert await proxy.echo("b") == "r-b"
    nonces = [r.nonce for r in p.requests]
    assert all(n and len(n) == 32 for n in nonces)
    assert len(set(nonces)) == 2


@pytest.mark.asyncio
@parametrize_transports
async def test_unsolicited_bytes_on_an_idle_pooled_connection(
    shared_client: str,
) -> None:
    """A complete, CORRECTLY BOUND response of another call arrives on an
    idle keep-alive connection. Stock aiohttp would queue it as the next
    call's response: the binding rejects it and the retry gets its own."""
    async with binding_peer() as p:
        proxy = proxy_for(p.url)
        assert await proxy.echo("first") == "r-first"
        first = p.requests[0]
        p.writers[0].write(first.good)  # a second copy, unsolicited
        await asyncio.sleep(0.05)
        before = _rejected("nonce_mismatch")
        assert await proxy.echo("second") == "r-second"
        assert await proxy.echo("third") == "r-third"
        if shared_client == "aiohttp-unguarded":
            # The stale message DID reach a call — and was rejected.
            assert _rejected("nonce_mismatch") == before + 1
        else:
            assert _rejected("nonce_mismatch") == before  # the guard caught it


@pytest.mark.asyncio
@parametrize_transports
async def test_peer_returning_a_stale_response_of_another_call(
    shared_client: str,
) -> None:
    """A buggy / malicious peer answers with an earlier call's response
    (correctly bound to THAT call): rejected in every binding mode."""

    async def replay_once(peer: BindingPeer, req: Req) -> bool:
        other = peer.other(req)
        if req.arg == "victim" and sum(r.arg == "victim" for r in peer.requests) == 1:
            assert other is not None
            req.writer.write(other.good)
            return True
        req.writer.write(req.good)
        return True

    async with binding_peer(replay_once) as p:
        proxy = proxy_for(p.url)
        assert await proxy.echo("earlier") == "r-earlier"
        before = _rejected("nonce_mismatch")
        assert await proxy.echo("victim") == "r-victim"  # the retry's own answer
        assert _rejected("nonce_mismatch") == before + 1
        assert [r.arg for r in p.requests] == ["earlier", "victim", "victim"]


@pytest.mark.asyncio
@parametrize_transports
async def test_a_peer_that_always_answers_another_call_is_503_never_a_result(
    shared_client: str,
) -> None:
    async def replay_always(peer: BindingPeer, req: Req) -> bool:
        other = peer.other(req)
        req.writer.write(other.good if other is not None else req.good)
        return True

    async with binding_peer(replay_always) as p:
        proxy = proxy_for(p.url)
        assert await proxy.echo("one") == "r-one"
        with pytest.raises(RemoteServiceUnavailable) as info:
            await proxy.echo("two")
        assert "RpcResponseBindingError" in str(info.value.detail)
        assert len(p.requests) == 3  # one call + exactly one retry


@pytest.mark.asyncio
@parametrize_transports
async def test_response_with_a_wrong_request_id(shared_client: str) -> None:
    async def wrong_nonce(peer: BindingPeer, req: Req) -> bool:
        if req.n == 1:
            forged = os.urandom(16).hex()  # a validly MACed, OTHER request id
            body = result_body(req.expected)
            req.writer.write(http_response(body, nonce=forged, headers=req.headers))
            return True
        req.writer.write(req.good)
        return True

    async with binding_peer(wrong_nonce) as p:
        before = _rejected("nonce_mismatch")
        assert await proxy_for(p.url).echo("x") == "r-x"
        assert _rejected("nonce_mismatch") == before + 1


@pytest.mark.asyncio
@parametrize_transports
async def test_right_request_id_but_another_calls_body(shared_client: str) -> None:
    """Headers of one message, body of another (a desync): the MAC covers
    the body digest."""

    async def spliced(peer: BindingPeer, req: Req) -> bool:
        if req.n == 2:
            own = result_body(req.expected)
            req.writer.write(
                http_response(
                    result_body("r-someone-else"),
                    nonce=req.nonce,
                    headers=req.headers,
                    mac_body=own,
                )
            )
            return True
        req.writer.write(req.good)
        return True

    async with binding_peer(spliced) as p:
        proxy = proxy_for(p.url)
        assert await proxy.echo("a") == "r-a"
        before = _rejected("mac_mismatch")
        assert await proxy.echo("b") == "r-b"
        assert _rejected("mac_mismatch") == before + 1


@pytest.mark.asyncio
@parametrize_transports
async def test_mac_is_keyed_with_the_pair_key(shared_client: str) -> None:
    """A response keyed by a holder of another X25519 key (an impostor
    claiming to be the pinned provider) never verifies."""
    impostor = settings.model_copy(
        update={
            "rpc_dh_private_key": rpc_keys("impostor").dh_private_key,
            "rpc_service_dh_keys": {SVC: {"k1": rpc_keys("impostor").dh_public_key}},
        }
    )

    async def other_key(peer: BindingPeer, req: Req) -> bool:
        req.writer.write(
            http_response(
                result_body(req.expected),
                nonce=req.nonce,
                headers=req.headers,
                provider=impostor if req.n == 1 else None,
            )
        )
        return True

    async with binding_peer(other_key) as p:
        before = _rejected("mac_mismatch")
        assert await proxy_for(p.url).echo("k") == "r-k"
        assert _rejected("mac_mismatch") == before + 1


@pytest.mark.asyncio
@parametrize_transports
async def test_partial_response_then_close(shared_client: str) -> None:
    async def cut(peer: BindingPeer, req: Req) -> bool:
        if req.n == 1:
            req.writer.write(req.good[:-5])
            return False
        req.writer.write(req.good)
        return True

    async with binding_peer(cut) as p:
        assert await proxy_for(p.url).echo("p") == "r-p"
        assert len(p.requests) == 2


@pytest.mark.asyncio
@parametrize_transports
async def test_interleaved_responses_are_swapped_between_connections(
    shared_client: str,
) -> None:
    """Two calls in flight on two connections; the peer answers each with
    the OTHER's (bound) response — the pipelining-desync shape."""
    held: list[Req] = []
    both = asyncio.Event()

    async def swap(peer: BindingPeer, req: Req) -> bool:
        if req.n <= 2:
            held.append(req)
            if len(held) == 2:
                both.set()
            await both.wait()
            mine, theirs = (held[0], held[1]) if req is held[0] else (held[1], held[0])
            mine.writer.write(theirs.good)
            return True
        req.writer.write(req.good)
        return True

    async with binding_peer(swap) as p:
        proxy = proxy_for(p.url)
        before = _rejected("nonce_mismatch")
        results = await asyncio.gather(proxy.echo("left"), proxy.echo("right"))
        assert results == ["r-left", "r-right"]
        assert _rejected("nonce_mismatch") == before + 2


@pytest.mark.asyncio
@parametrize_transports
async def test_cancellation_mid_read_never_leaks_the_late_answer(
    shared_client: str,
) -> None:
    partial_sent = asyncio.Event()
    cancelled = asyncio.Event()

    async def late(peer: BindingPeer, req: Req) -> bool:
        if req.n == 1:
            head, _, body = req.good.partition(b"\r\n\r\n")
            req.writer.write(head + b"\r\n\r\n" + body[:3])  # mid-body
            await req.writer.drain()
            partial_sent.set()
            # the rest only after the caller was cancelled (event-ordered, not
            # timed: a loaded runner cannot let the answer win the race)
            await asyncio.wait_for(cancelled.wait(), 10)
            await asyncio.sleep(0.05)
            req.writer.write(body[3:])
            return True
        req.writer.write(req.good)
        return True

    async with binding_peer(late) as p:
        proxy = proxy_for(p.url)
        task = asyncio.create_task(proxy.echo("cancelled"))
        await asyncio.wait_for(partial_sent.wait(), 10)
        await asyncio.sleep(0.01)  # the caller is reading the body now
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cancelled.set()
        await asyncio.sleep(0.3)
        assert await proxy.echo("next") == "r-next"
        assert await proxy.echo("after") == "r-after"


@pytest.mark.asyncio
@parametrize_transports
async def test_an_unbound_reply_is_rejected(shared_client: str) -> None:
    """A peer that is not a Sandloom RPC endpoint (no binding at all)."""

    async def unbound(peer: BindingPeer, req: Req) -> bool:
        req.writer.write(http_response(result_body(req.expected), nonce=None))
        return True

    async with binding_peer(unbound) as p:
        rejected = _rejected("unbound")
        with pytest.raises(RemoteServiceUnavailable):
            await proxy_for(p.url).echo("old")
        assert _rejected("unbound") == rejected + 2  # the call and its retry


@pytest.mark.asyncio
@parametrize_transports
async def test_a_rejected_response_discards_the_connection_pool(
    shared_client: str,
) -> None:
    async def replay_second(peer: BindingPeer, req: Req) -> bool:
        other = peer.other(req)
        if req.n == 2 and other is not None:
            req.writer.write(other.good)
            return True
        req.writer.write(req.good)
        return True

    async with binding_peer(replay_second) as p:
        proxy = proxy_for(p.url)
        assert await proxy.echo("a") == "r-a"
        assert p.connections == 1
        assert await proxy.echo("b") == "r-b"
        # The retry ran on a NEW connection (the old pool is never reused).
        assert p.requests[2].conn == 2


# --------------------------------------------------------------------------
# concurrency 64 with randomised fault injection
# --------------------------------------------------------------------------

FAULTS = (
    "ok",
    "ok",
    "ok",
    "delay",
    "replay_other",
    "wrong_nonce",
    "spliced_body",
    "unsolicited_after",
    "partial_close",
    "unbound",
)


def _faulty(rng: random.Random) -> Behavior:
    async def behave(peer: BindingPeer, req: Req) -> bool:
        fault = rng.choice(FAULTS)
        other = peer.other(req)
        if fault in {"replay_other", "spliced_body", "unsolicited_after"} and (
            other is None
        ):
            fault = "ok"
        if fault == "delay":
            await asyncio.sleep(rng.random() * 0.02)
            fault = "ok"
        if fault == "ok":
            req.writer.write(req.good)
        elif fault == "replay_other":
            assert other is not None
            req.writer.write(other.good)
        elif fault == "wrong_nonce":
            req.writer.write(
                http_response(
                    result_body(req.expected),
                    nonce=os.urandom(16).hex(),
                    headers=req.headers,
                )
            )
        elif fault == "spliced_body":
            assert other is not None
            req.writer.write(
                http_response(
                    result_body(other.expected),
                    nonce=req.nonce,
                    headers=req.headers,
                    mac_body=result_body(req.expected),
                )
            )
        elif fault == "unsolicited_after":
            assert other is not None
            req.writer.write(req.good + other.good)
        elif fault == "partial_close":
            req.writer.write(req.good[: rng.randrange(1, len(req.good))])
            return False
        elif fault == "unbound":
            req.writer.write(http_response(result_body(req.expected), nonce=None))
        return True

    return behave


@pytest.mark.asyncio
@parametrize_transports
@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_concurrency_64_with_random_faults_never_returns_another_result(
    shared_client: str, seed: int
) -> None:
    rng = random.Random(seed)
    async with binding_peer(_faulty(rng)) as p:
        proxy = proxy_for(p.url)

        async def call(i: int) -> tuple[int, Any]:
            try:
                return i, await proxy.echo(f"c{i}")
            except (RemoteServiceUnavailable, RemoteServiceBadResponse) as exc:
                return i, exc

        for _round in range(3):
            tasks = [asyncio.create_task(call(i)) for i in range(64)]
            await asyncio.sleep(0.005)
            cancelled = {i for i in range(64) if rng.random() < 0.1}
            for i in cancelled:
                tasks[i].cancel()
            done = await asyncio.gather(*tasks, return_exceptions=True)
            ok = 0
            for i, outcome in enumerate(done):
                if isinstance(outcome, asyncio.CancelledError):
                    assert i in cancelled
                    continue
                assert isinstance(outcome, tuple)
                index, value = outcome
                assert index == i
                if isinstance(value, Exception):
                    continue
                # THE property: a call returns its own result or fails.
                assert value == f"r-c{i}", (i, value)
                ok += 1
            assert ok > 0
    assert _rejected_all() > 0


# --------------------------------------------------------------------------
# the provider binds every response (fast path, plain router, the 413 cap)
# --------------------------------------------------------------------------


def _echo_app(*, fast_path: bool) -> FastAPI:
    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core

    class Echo:
        @classmethod
        @contract_method
        async def echo(cls, value: str) -> str:
            return f"r-{value}"

    from sandloom.core.tests.test_service_rpc import _wire_fake_database

    _wire_fake_database(core, [])
    core.register_service("demo.echo.v1", Echo)
    if fast_path:
        rpc.install_service_rpc_routes(app, core)
    else:
        app.include_router(rpc.rpc_router(core))
    return app


def _sent(target: str, body: bytes = b"", **kwargs: Any) -> dict[str, str]:
    return rpc_request_headers(settings, target, body, **kwargs)


def _keyed_to(response: Any, sent: dict[str, str]) -> bool:
    return (
        rpc_binding.verify(
            settings,
            response.headers,
            nonce=sent[rpc_wire._NONCE_HEADER],
            sent=sent,
            expect_service=SVC,
            status_code=response.status_code,
            body=response.content,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fast_path", [True, False], ids=["fast-path", "router"])
async def test_provider_binds_call_describe_and_error_responses(
    fast_path: bool,
) -> None:
    app = _echo_app(fast_path=fast_path)
    body = rpc_wire._pack({"args": ["v"], "kwargs": {}})
    call_sent = _sent("demo.echo.v1/echo", body)
    describe_sent = _sent("demo.echo.v1", http_method="GET")
    missing_sent = _sent("demo.nope.v1/echo", rpc_wire._pack({}))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        call = await client.post(
            "/_sandloom/rpc/demo.echo.v1/echo", content=body, headers=call_sent
        )
        describe = await client.get(
            "/_sandloom/rpc/demo.echo.v1", headers=describe_sent
        )
        missing = await client.post(
            "/_sandloom/rpc/demo.nope.v1/echo",
            content=rpc_wire._pack({}),
            headers=missing_sent,
        )
        nonce = os.urandom(16).hex()
        hidden = await client.post(
            "/_sandloom/rpc/demo.echo.v1/echo",
            content=rpc_wire._pack({}),
            headers={rpc_wire._NONCE_HEADER: nonce},  # no credential: hidden 404
        )
        no_nonce = {k: v for k, v in call_sent.items() if k != rpc_wire._NONCE_HEADER}
        legacy = await client.post(
            "/_sandloom/rpc/demo.echo.v1/echo", content=body, headers=no_nonce
        )
        junk = await client.post(
            "/_sandloom/rpc/demo.echo.v1/echo",
            content=body,
            headers={**no_nonce, rpc_wire._NONCE_HEADER: "bad nonce!"},
        )
    assert rpc_wire._unpack(call.content)["result"] == "r-v"
    for response, sent in ((call, call_sent), (describe, describe_sent)):
        assert _keyed_to(response, sent)
    assert missing.status_code == 404 and _keyed_to(missing, missing_sent)
    # An unauthenticated request is answered with its nonce alone: nothing
    # keyed (the caller refuses it as unkeyed).
    assert hidden.status_code == 404
    assert hidden.headers[rpc_wire._NONCE_HEADER] == nonce
    assert rpc_wire._RESPONSE_MAC_HEADER not in hidden.headers
    for response in (legacy, junk):  # nothing (valid) to bind to
        assert rpc_wire._NONCE_HEADER not in response.headers
        assert rpc_wire._RESPONSE_MAC_HEADER not in response.headers


@pytest.mark.asyncio
async def test_the_body_cap_413_echoes_the_nonce_unkeyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap refuses before the request is verified: the 413 carries the
    nonce but no keyed MAC — a refusal, never a result."""
    monkeypatch.setattr(settings, "max_request_body_bytes", 1024)
    app = _echo_app(fast_path=True)
    body = b"x" * 4096
    sent = _sent("demo.echo.v1/echo", body)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/_sandloom/rpc/demo.echo.v1/echo", content=body, headers=sent
        )
    assert response.status_code == 413
    assert response.headers[rpc_wire._NONCE_HEADER] == sent[rpc_wire._NONCE_HEADER]
    assert rpc_wire._RESPONSE_MAC_HEADER not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("fast_path", [True, False], ids=["fast-path", "router"])
async def test_proxy_against_a_real_provider(fast_path: bool) -> None:
    app = _echo_app(fast_path=fast_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        proxy = RemoteServiceProxy(
            service_name="demo.echo.v1",
            binding={"base_url": "http://test", "service_id": SVC},
            default_timeout_seconds=2,
            client=client,
        )
        before = _rejected_all()
        assert await proxy.echo("live") == "r-live"
        assert _rejected_all() == before


def test_httpx_transport_error_class_is_retryable() -> None:
    assert issubclass(rpc_transport.RpcResponseBindingError, httpx.TransportError)
