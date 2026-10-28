"""RPC cost-decomposition stand: env, rung table, smoke (DIAGNOSTIC ONLY).

One contract call, decomposed layer by layer (``ladder/stands.py`` builds the
in-process rungs from :data:`METHODS` x :data:`LAYERS`; ``ladder/rpc_stand.py``
runs the same calls over a real loopback socket against a provider process).

    # 1. a database with the tenancy schema + one org / member (owner DSN)
    python -m benchsuite.platform seed

    # 2. env of a provider (and of the in-process rungs)
    python -m benchsuite.rpcstand env [opt]   # opt = PLANNER_OPTIMIZED_EXECUTOR

    # 3. every rung answers correctly, in-process, a handful of calls
    python -m benchsuite.rpcstand smoke

The provider runs with PRODUCTION defaults (``ENVIRONMENT=prod``) and loads
only ``sandloom_tenancy`` + the ``bench`` module (``BENCH_RPC_DIAG=1`` registers
``bench.rpcdiag.v1``, :mod:`benchsuite.bench.rpc_diag`). Internal RPC has
one authentication (ADR-0004): the stand is the service :data:`STAND_SERVICE`
calling ITSELF — pair-MAC request authentication, ``RPC_ALLOWED_CALLS``,
the X25519-keyed response binding pinned to ``service_id``, and (prod) the
caller's Ed25519 signature on commands.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from benchsuite import common, rpckeys
from benchsuite.platform import ORG, USER

#: Ops-endpoint token (/metrics, /diagnostics; ≥ 32 chars for prod
#: settings). It plays no part in internal RPC.
TOKEN = "rpc-stand-internal-token-0123456789abcdef"
DIAG = "bench.rpcdiag.v1"
MEMBERSHIP = "tenancy.membership.v1"
#: Base URL of the in-process provider (ASGI transport; never resolved).
INPROC_URL = "http://sandloom-rpc-stand"


@dataclass(frozen=True)
class Method:
    service: str
    method: str
    desc: str
    command: bool = False  # a fresh operation_key per call
    replay: bool = False  # the SAME operation_key every call (dedupe replay)
    planner_opt: bool = False  # provider with PLANNER_OPTIMIZED_EXECUTOR=true

    def args(self, i: int, nonce: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if not self.command:
            return (ORG, USER), {}
        key = f"{nonce}-replay" if self.replay else f"{nonce}-{i}"
        return (ORG, USER), {"operation_key": key}


#: Provider-side body per rung (each adds ONE thing to the previous one).
METHODS: dict[str, Method] = {
    "R0": Method(DIAG, "noop", "planned no-op: contract runtime, NO UoW, no DB"),
    "R2": Method(DIAG, "uow_noop", "+ provider read UoW opened/rolled back, no stmt"),
    "R2q": Method(DIAG, "uow_select", "+ SELECT 1 (checkout, BEGIN, ROLLBACK)"),
    "R3": Method(MEMBERSHIP, "get_role", "real membership read, planner GENERAL"),
    "R3o": Method(
        MEMBERSHIP,
        "get_role",
        "real membership read, planner OPTIMIZED",
        planner_opt=True,
    ),
    "R3u": Method(DIAG, "role_uow", "the same query as raw SQL in a read UoW"),
    "R4": Method(DIAG, "touch", "COMMAND: dedupe reserve+persist + commit", True),
    "R4r": Method(
        DIAG, "touch", "COMMAND replay (same key): dedupe hit only", True, True
    ),
}

#: Where the call goes (each layer isolates one side of the wire).
LAYERS: dict[str, str] = {
    "loc": "local core.get_service(...) call (monolith path)",
    "enc": "CLIENT only, no transport: proxy encode + context + pair-MAC "
    "(+ command signature) + envelope decode (canned response, keyed per "
    "call; its binding is not verified)",
    "cli": "CLIENT only incl. httpx (MockTransport, canned response keyed per "
    "call, binding verified, no socket)",
    "srv": "SERVER only: a pool of distinct prebuilt authenticated requests "
    "driven into the ASGI app (fast path, pair-MAC + replay guard, "
    "decode, contract runtime, encode + response binding)",
    "rpc": "client + server in-process (httpx ASGITransport, no socket)",
    "rpcn": "rpc with a NEW proxy per call (the get_service-per-request pattern)",
}


def server_env(*, planner_opt: bool = False) -> dict[str, str]:
    """Provider env (also the in-process rungs' env); production defaults."""
    modules = '["sandloom_tenancy", "benchsuite.bench"]'
    return {
        "ENVIRONMENT": os.environ.get("RPC_STAND_ENVIRONMENT", "prod"),
        "SANDLOOM_MODULES": modules,
        "MODULE_REQUIRED": modules,
        "BENCH_RPC_DIAG": "1",
        "INTERNAL_API_TOKEN": TOKEN,
        "LOG_LEVEL": "WARNING",
        "RATE_LIMIT_ENABLED": "false",
        "OUTBOX_RELAY_ENABLED": "off",
        "DOCS_ENABLED": "false",
        "DATABASE_URL": common.database_url(),
        "CACHE_URL": common.cache_url(),
        "BROKER_URL": common.broker_url(),
        "JWT_SECRET": common.jwt_secret(),
        "PLANNER_OPTIMIZED_EXECUTOR": "true" if planner_opt else "false",
        # The stand's provider is a split-production process with the shared
        # HS256 secret (it never mints tokens); main refuses that since #51
        # unless explicitly accepted — as is a prod process without a relay.
        "SANDLOOM_ACCEPTED_RISKS": "shared_hs_secret_split,relay_disabled",
        # The ``srv`` layer replays a POOL of requests prebuilt at warm-up
        # (each exactly once: the provider's nonce replay guard refuses a
        # repeat); a request is accepted while |now - iat| <= this window,
        # so a pool must be consumed within 300 s of being built (the max).
        "RPC_AUTH_MAX_SKEW_SECONDS": "300",
        # Every call's nonce stays in the per-process replay table for
        # ~skew + 2 s: room for a whole rung at in-process call rates
        # (the default 100k per caller fills in ~20 s at 5k calls/s).
        "RPC_REPLAY_CACHE_MAX_ENTRIES": str(REPLAY_TABLE),
        "RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES": str(REPLAY_TABLE),
        **rpckeys.env(STAND_SERVICE, allowed_calls={STAND_SERVICE: [DIAG, MEMBERSHIP]}),
    }


#: The stand's internal-RPC service id: it serves AND calls itself (one key
#: pair on both sides, generated when the env is built — before ``sandloom`` is
#: imported — by :mod:`benchsuite.rpckeys`).
STAND_SERVICE = "stand"
#: Replay-table bound of the stand (entries; ~200 B each at worst).
REPLAY_TABLE = 1_000_000


def binding() -> dict[str, object]:
    """The stand's binding of the in-process provider (pinned to itself)."""
    return {"base_url": INPROC_URL, "service_id": STAND_SERVICE}


#: A pooled prebuilt request is accepted for RPC_AUTH_MAX_SKEW_SECONDS (300)
#: after it was built; past this age (margin for the build itself) the
#: ``srv`` rung refuses to continue rather than measure 401s.
_POOL_FRESH_SECONDS = 270.0


def command_pool() -> int:
    """Prebuilt distinct requests of a ``srv`` rung (one per op, fresh nonces;
    commands also fresh operation keys).

    Fixed (never ``--n`` dependent) so cachegrind's Ir(K) - Ir(0) cancels the
    setup; ``ladder.py cpu`` touches op indices up to (reps + 1) * n.
    """
    return int(os.environ.get("RPC_STAND_COMMAND_POOL", "40000"))


Op = Callable[[int], Awaitable[Any]]


class _Canned:
    """A recorded provider reply (client-only layers) + recorded requests."""

    def __init__(self) -> None:
        self.reply: tuple[int, list[tuple[str, str]], bytes] | None = None
        self.sent: list[tuple[str, bytes, dict[str, str]]] = []
        self.record = False

    def response(self, request: Any = None) -> Any:
        """The recorded reply; for ``request``, re-bound to ITS nonce and
        keyed like a provider's answer to it (the response binding rejects a
        replayed reply — a stale response — as a real provider would never
        send one)."""
        import httpx

        from sandloom.core import rpc_binding, rpc_pair_mac
        from sandloom.core.config.settings import settings

        assert self.reply is not None
        status, headers, content = self.reply
        if request is not None:
            nonce = request.headers.get(rpc_binding.NONCE_HEADER)
            skip = {
                rpc_binding.NONCE_HEADER.lower(),
                rpc_binding.MAC_HEADER.lower(),
                rpc_binding.BINDING_HEADER.lower(),
            }
            headers = [(k, v) for k, v in headers if k.lower() not in skip]
            if nonce:
                # Keyed as the provider keys its answer to a VERIFIED caller:
                # the pair key towards the caller the request's MAC names.
                auth = rpc_pair_mac.parse_header(
                    request.headers[rpc_pair_mac.AUTH_HEADER]
                )
                key = rpc_binding.provider_key(settings, (auth["caller"], auth["ck"]))
                headers += rpc_binding.response_headers(
                    nonce=nonce,
                    key=key,
                    req_auth=rpc_binding.request_auth(request.headers),
                    status_code=status,
                    body=content,
                )
        return httpx.Response(status, headers=headers, content=content)


class _Sent:
    """The request :meth:`_Canned.response` answers (headers as sent: the
    proxy's own header names, so a plain dict lookup matches)."""

    __slots__ = ("headers",)

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _asgi_post(app: Any) -> Callable[..., Awaitable[bytes]]:
    """Drive one POST straight into ``app`` (no client, no socket)."""
    base = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    async def post(path: str, headers: list[tuple[bytes, bytes]], body: bytes) -> bytes:
        req = {"type": "http.request", "body": body, "more_body": False}
        out: list[Any] = []

        async def receive() -> Any:
            return req

        async def send(msg: Any) -> None:
            out.append(msg)

        scope = dict(base)
        scope["path"] = path
        scope["raw_path"] = path.encode()
        scope["headers"] = headers
        scope["state"] = {}
        await app(scope, receive, send)
        if out[0]["status"] != 200:
            raise RuntimeError(f"{path}: {out}")
        return bytes(out[1]["body"])

    return post


async def build_layer(
    app: Any,
    core: Any,
    m: Method,
    layer: str,
    *,
    tenant: bool = False,
    nonce: str | None = None,
) -> tuple[Op, Callable[[], Awaitable[None]]]:
    """``(op, aclose)`` calling ``m`` through ``layer`` (see :data:`LAYERS`).

    ``op(i)`` returns what the layer returns: the decoded result for every
    client-side layer, the provider's raw msgpack reply for ``srv``.
    ``tenant`` binds :data:`ORG` around each call (in the authenticated RPC
    context).
    """
    import httpx

    from sandloom.core import rpc, rpc_binding
    from sandloom.core.tenant import bind_tenant

    nonce = nonce or uuid.uuid4().hex[:12]
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=INPROC_URL
    )
    canned = _Canned()

    def proxy(client: Any) -> Any:
        return rpc.RemoteServiceProxy(
            service_name=m.service,
            binding=binding(),
            default_timeout_seconds=5,
            client=client,
        )

    class _CannedProxy(rpc.RemoteServiceProxy):
        async def _post(
            self,
            url: str,
            body: bytes,
            headers: dict[str, str],
            signer: Any,
            *,
            doubt: Any = None,
        ) -> Any:
            # As the real _post does per attempt: a fresh nonce, then the
            # pair-MAC (and, for a signed call, the context re-signed over it).
            nonce = headers[rpc_binding.NONCE_HEADER] = rpc_binding.new_nonce()
            signer.apply(headers, nonce)
            if canned.record:
                canned.sent.append((url, body, dict(headers)))
            return canned.response(_Sent(headers))

    def canned_proxy() -> Any:
        return _CannedProxy(
            service_name=m.service,
            binding=binding(),
            default_timeout_seconds=5,
        )

    # One REAL call describes the contract (cached per process); a second one
    # records the provider's reply for the client-only layers.
    real = proxy(http)
    args, kwargs = m.args(-1, nonce)
    with bind_tenant(ORG):
        await getattr(real, m.method)(*args, **kwargs)
    replies: list[Any] = []

    async def _capture(request: httpx.Request) -> httpx.Response:
        resp = await httpx.ASGITransport(app=app).handle_async_request(request)
        await resp.aread()
        replies.append(resp)
        return resp

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_capture), base_url=INPROC_URL
    ) as cap:
        args, kwargs = m.args(-2, nonce)
        with bind_tenant(ORG):
            await getattr(proxy(cap), m.method)(*args, **kwargs)
    reply = replies[-1]
    canned.reply = (reply.status_code, list(reply.headers.items()), reply.content)
    closers: list[Any] = [http.aclose]

    async def aclose() -> None:
        for close in closers:
            await close()

    def bound(call: Op) -> Op:
        if not tenant:
            return call

        async def with_tenant(i: int) -> Any:
            with bind_tenant(ORG):
                return await call(i)

        return with_tenant

    def calling(target: Callable[..., Awaitable[Any]]) -> Op:
        async def op(i: int) -> Any:
            a, k = m.args(i, nonce)
            return await target(*a, **k)

        return bound(op)

    if layer == "loc":
        return calling(getattr(core.get_service(m.service), m.method)), aclose
    if layer == "enc":
        return calling(getattr(canned_proxy(), m.method)), aclose
    if layer == "cli":
        mock = httpx.AsyncClient(
            transport=httpx.MockTransport(canned.response),
            base_url=INPROC_URL,
        )
        closers.append(mock.aclose)
        return calling(getattr(proxy(mock), m.method)), aclose
    if layer == "rpc":
        return calling(getattr(real, m.method)), aclose
    if layer == "rpcn":

        async def fresh(i: int) -> Any:
            a, k = m.args(i, nonce)
            return await getattr(proxy(http), m.method)(*a, **k)

        return bound(fresh), aclose
    if layer == "srv":
        return _server_only(app, m, tenant, nonce, canned, canned_proxy), aclose
    raise ValueError(f"unknown layer {layer!r}; one of {sorted(LAYERS)}")


def _server_only(
    app: Any, m: Method, tenant: bool, nonce: str, canned: _Canned, make: Any
) -> Op:
    """Prebuilt authenticated requests driven straight into the app (server
    side), each exactly once."""
    from sandloom.core.tenant import bind_tenant

    post = _asgi_post(app)
    target = getattr(make(), m.method)

    async def build(i: int) -> tuple[str, list[tuple[bytes, bytes]], bytes]:
        canned.record = True
        canned.sent.clear()
        a, k = m.args(i, nonce)
        try:
            if tenant:
                with bind_tenant(ORG):
                    await target(*a, **k)
            else:
                await target(*a, **k)
        finally:
            canned.record = False
        url, body, headers = canned.sent[-1]
        path = url[url.index("/_sandloom/") :]
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        raw += [
            (b"host", b"sandloom-rpc-stand"),
            (b"content-length", str(len(body)).encode()),
        ]
        return path, raw, body

    # One distinct prebuilt request per op: the provider's nonce replay guard
    # refuses a request it has already served, so even a read (or the
    # command REPLAY rung, whose operation key repeats) cannot resend one.
    size = command_pool()
    pool: list[Any] = []
    built = [0.0]

    async def op(i: int) -> bytes:
        if not pool:  # built on first use (the warm-up), then indexed
            built[0] = time.monotonic()
            for j in range(size):
                pool.append(await build(j))
        if i >= size:  # never silently resend a served request
            raise RuntimeError(
                f"op {i} >= RPC_STAND_COMMAND_POOL={size}: lower --n/--reps "
                "or raise the pool"
            )
        if time.monotonic() - built[0] > _POOL_FRESH_SECONDS:
            raise RuntimeError(
                f"the srv pool is older than {_POOL_FRESH_SECONDS:.0f} s (its "
                "requests leave RPC_AUTH_MAX_SKEW_SECONDS): lower --n/--reps "
                "or RPC_STAND_COMMAND_POOL"
            )
        path, headers, body = pool[i]
        return await post(path, headers, body)

    return op


def decode_reply(raw: bytes) -> Any:
    """The ``result`` of a raw provider reply (``srv`` layer)."""
    import msgpack

    data = msgpack.unpackb(raw, raw=False, strict_map_key=False)
    assert data.get("ok") is True, data
    return data["result"]


async def _smoke() -> None:
    from sandloom.main import app

    async with app.router.lifespan_context(app):
        core = app.state.core
        for name, m in METHODS.items():
            if m.planner_opt:
                continue  # needs its own process (settings are read at import)
            for tenant in (False, True):
                for layer in LAYERS:
                    op, aclose = await build_layer(app, core, m, layer, tenant=tenant)
                    try:
                        for i in range(3):
                            got = await op(i)
                            if layer == "srv":
                                got = decode_reply(got)
                            if got != "owner":
                                raise SystemExit(f"[{name}.{layer}] {got!r}")
                    finally:
                        await aclose()
    print(f"rpc stand: every rung x layer OK ({len(METHODS) - 1} methods)")


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "help"
    if cmd == "env":
        opt = len(argv) > 1 and argv[1] == "opt"
        for k, v in server_env(planner_opt=opt).items():
            print(f"export {k}={shlex.quote(v)}")
    elif cmd == "smoke":
        os.environ.update(server_env())
        os.environ.setdefault("RPC_STAND_COMMAND_POOL", "8")  # 3 ops per rung
        asyncio.run(_smoke())
    elif cmd == "list":
        print(json.dumps({"methods": list(METHODS), "layers": LAYERS}, indent=1))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
