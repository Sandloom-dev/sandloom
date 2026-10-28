"""``MIX`` rung: one request of the SaaS mix catalog, in-process, any topology.

Installed into ``ladder.py`` (``ladder.py cg MIX``). Env:

* ``LADDER_MIX_KEY`` — a ``benchsuite.mix.CATALOG`` key (reads AND writes:
  ``post_ticket`` / ``post_task`` / ``post_link`` bodies are filled per op as
  ``benchsuite.loadclient`` does), or an ``EXTRA`` key of this rung (requests
  measured outside the fixed mix catalog, e.g. ``export``: the helpdesk
  ticket export, a 100-row planner list behind ``Subscribed``);
* ``LADDER_MIX_SVC`` — ``mono`` (``sandloom.main`` with ``mix.FULL_MODULES`` on
  ``DATABASE_URL``) or the split service owning the route, ``identity |
  tenancy | billing | apps`` (its modules as ``split_seed.SPLIT``, its
  database ``<LADDER_MIX_SPLIT_PREFIX>_<svc>`` on ``DATABASE_URL``'s server);
* ``LADDER_MIX_ROLE`` (split only):

  - ``caller`` (default) — the measured process is the service owning the
    route, driven in-process through ASGI. For ``apps`` / ``billing`` the
    tenancy service (``tenancy.membership.v1``) runs as a SEPARATE uvicorn
    process, which cachegrind does not trace (no ``--trace-children``): the
    count is the CALLER only, its real RPC client (aiohttp) included;
  - ``provider`` — the measured process is the TENANCY service, served by an
    in-process uvicorn (httptools) on a loopback port; the service owning the
    route runs as a separate, untraced uvicorn process bound to it, and each
    op sends the route's request to that process over a kept-alive socket
    and reads the reply. Provider cost per request = ``Ir(key) − Ir(health)``
    (``health`` answers in the caller without any RPC, so it carries exactly
    the measured process's own socket-client cost and nothing else).

  When ``LADDER_MIX_TAG`` is set, the number of RPC calls the provider served
  during the run is appended to ``/out/<tag>.rpc``.
* ``HTTP_ROUTE_PLANS`` is passed through (``false`` by default).

In-process ops await ``drain_post_commit()`` (a write's post-commit outbox
dispatch belongs to the op that caused it); in-memory consumers that run
later are counted in the run's tail, which ``Ir(K) − Ir(0)`` keeps. The env
mirrors the stand's servers: ``ENVIRONMENT=dev``,
``PLANNER_OPTIMIZED_EXECUTOR=true``, ``RPC_TRANSPORT=aiohttp``,
``PASSWORD_HASH_PROFILE=owasp_min`` (no login row is measured), fake PSP,
``INTERNAL_API_TOKEN`` set (ops endpoints only). Split services authenticate
internal RPC as in production (ADR-0004 pair-MAC + keyed response binding):
every service gets its own keys and every peer's public keys
(:mod:`benchsuite.rpckeys`, generated in THIS process before ``sandloom`` is
imported), bindings pin ``"service_id"``, and ``RPC_ALLOWED_CALLS`` lets
``apps`` / ``billing`` call ``tenancy.membership.v1``.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

INTERNAL_TOKEN = "bench-internal-token-0123456789abcdef0123456789abcdef"


def _extra(mix: Any) -> dict[str, tuple[tuple[str, str, str, str], dict[str, Any]]]:
    """Rung-only requests (NOT in ``mix.CATALOG``: the mix profiles stay fixed):
    key -> (catalog entry, extra token claims)."""
    return {
        "export": (
            ("GET", f"/api/v1/orgs/{mix.ORG}/tickets/export", "", "200"),
            {"subscription_status": "active"},
        ),
        # the taskboard with its optional filters (status + ILIKE prefix)
        "taskboard_filtered": (
            (
                "GET",
                "/api/v1/taskboard/tasks?task_status=active&project_prefix=dev"
                "&limit=50",
                "",
                "200",
            ),
            {},
        ),
    }


def _split_modules() -> dict[str, list[str]]:
    from benchsuite import mix

    return {
        "identity": ["sandloom_identity"],
        "tenancy": ["sandloom_tenancy"],
        "billing": ["sandloom_billing"],
        "apps": mix.FULL_MODULES[3:],
    }


def _db_url(name: str) -> str:
    base = os.environ["DATABASE_URL"]
    head, _, tail = base.rpartition("/")
    q = tail.partition("?")[2]
    return f"{head}/{name}" + (f"?{q}" if q else "?ssl=disable")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _common_env() -> dict[str, str]:
    return {
        "ENVIRONMENT": "dev",
        "LOG_LEVEL": "WARNING",
        "RATE_LIMIT_ENABLED": "false",
        "DOCS_ENABLED": "false",
        "BILLING_PROVIDER": "fake",
        "MODULE_REQUIRED": "[]",
        "OUTBOX_RELAY_ENABLED": "off",
        "SANDLOOM_ACCEPTED_RISKS": "relay_disabled",
        "PLANNER_OPTIMIZED_EXECUTOR": "true",
        "RPC_TRANSPORT": "aiohttp",
        "PASSWORD_HASH_PROFILE": "owasp_min",
        "INTERNAL_API_TOKEN": INTERNAL_TOKEN,  # ops endpoints (not RPC)
        "HTTP_ROUTE_PLANS": os.environ.get("HTTP_ROUTE_PLANS", "false"),
    }


#: the contract the split callers bind (and the provider allows them)
MEMBERSHIP = "tenancy.membership.v1"


def _rpc_env(svc: str) -> dict[str, str]:
    """``svc``'s internal-RPC keys (+ every split peer's public keys)."""
    from benchsuite import rpckeys

    return rpckeys.env(
        svc,
        peers=_split_modules(),
        allowed_calls={caller: [MEMBERSHIP] for caller in ("apps", "billing")},
    )


def _membership_binding(port: int) -> dict[str, Any]:
    return {
        MEMBERSHIP: {"base_url": f"http://127.0.0.1:{port}", "service_id": "tenancy"}
    }


def _svc_env(svc: str, prefix: str, bindings: dict[str, Any]) -> dict[str, str]:
    return {
        **_common_env(),
        **_rpc_env(svc),
        "SANDLOOM_MODULES": json.dumps(_split_modules()[svc]),
        "DATABASE_URL": _db_url(f"{prefix}_{svc}"),
        "SERVICE_BINDINGS": json.dumps(bindings),
        "DB_POOL_SIZE": "5",
        "DB_MAX_OVERFLOW": "5",
        "CONTRACT_DB_POOL_SIZE": "1",
        "CONTRACT_DB_MAX_OVERFLOW": "2",
    }


#: a service process that is NOT measured: sandloom.main behind a counter of the
#: RPC requests it served (``GET /__rpc_count``)
_SERVER = """
import sys, uvicorn
from sandloom.main import app
n = [0]
async def wrapped(scope, receive, send):
    if scope["type"] == "http":
        if scope["path"] == "/__rpc_count":
            body = str(n[0]).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        if scope["path"].startswith("/_sandloom/rpc"):
            n[0] += 1
    await app(scope, receive, send)
uvicorn.run(wrapped, host="127.0.0.1", port=int(sys.argv[1]), loop="uvloop",
            http="httptools", log_level="warning", lifespan="on")
"""


def _wait_http(port: int, alive: Any, timeout: float = 90.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not alive():
            raise RuntimeError("server died")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5) as s:
                s.sendall(b"GET /health/simple HTTP/1.1\r\nHost: x\r\n\r\n")
                if b" 200 " in s.recv(200):
                    return
        except OSError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"server on {port} did not start")


def _spawn(env: dict[str, str]) -> tuple[subprocess.Popen, int, Any]:
    port = _free_port()
    log = tempfile.TemporaryFile()
    p = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(port)],
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=log,
    )
    try:
        _wait_http(port, lambda: p.poll() is None)
    except RuntimeError:
        p.kill()
        log.seek(0)
        raise RuntimeError(log.read().decode(errors="replace")[-3000:]) from None
    return p, port, log


def _count(port: int, path: bytes = b"/__rpc_count") -> float:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET " + path + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return float(data.rpartition(b"\r\n\r\n")[2] or 0)


def _write_tag(value: float) -> None:
    tag = os.environ.get("LADDER_MIX_TAG")
    if tag:
        with open(f"/out/{tag}.rpc", "a") as fh:
            fh.write(f"{value}\n")


def install(ns: Any) -> None:
    ns.RUNGS["MIX"] = (
        "one request of benchsuite.mix.CATALOG (LADDER_MIX_KEY) on LADDER_MIX_SVC "
        "(mono | split service); LADDER_MIX_ROLE caller | provider (split)",
        lambda: _mix(ns),
    )


def _body(body_t: str, i: int) -> bytes:
    if not body_t:
        return b""
    u = f"{i:06d}{os.getpid() % 997:03d}"
    return body_t.replace("{i}", str(i)).replace("{u}", u).encode()


async def _mix(ns: Any):
    if "sandloom.core.config.settings" in sys.modules:
        raise RuntimeError("the MIX env must be set before importing sandloom")
    from benchsuite import mix

    # the example apps' source roots (the container's PYTHONPATH has them; a
    # host run does not): this process and every service it spawns
    roots = [
        os.path.join(mix.EXAMPLES, a) for a in ("helpdesk", "projecthub", "linkly")
    ]
    sys.path[:0] = [r for r in roots if r not in sys.path]
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [*roots, *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
    )
    key = os.environ["LADDER_MIX_KEY"]
    svc = os.environ.get("LADDER_MIX_SVC", "mono")
    role = os.environ.get("LADDER_MIX_ROLE", "caller")
    extra_claims: dict[str, Any] = {}
    if key in mix.CATALOG:
        method, full, body_t, expect = mix.CATALOG[key]
    else:
        (method, full, body_t, expect), extra_claims = _extra(mix)[key]
    token = mix.mint(hours=24, tenant=True)
    if extra_claims:
        import jwt

        claims = jwt.decode(token, options={"verify_signature": False})
        token = jwt.encode(
            {**claims, **extra_claims},
            mix.common.jwt_secret(),
            algorithm="HS256",
        )
    if svc != "mono" and role == "provider":
        return await _provider_role(ns, svc, method, full, body_t, expect, token)

    provider = None
    if svc == "mono":
        os.environ.update(_common_env())
        os.environ["SANDLOOM_MODULES"] = json.dumps(mix.FULL_MODULES)
    else:
        prefix = os.environ["LADDER_MIX_SPLIT_PREFIX"]
        bindings: dict[str, Any] = {}
        if svc in ("apps", "billing"):
            provider = _spawn(_svc_env("tenancy", prefix, {}))
            bindings = _membership_binding(provider[1])
        os.environ.update(_svc_env(svc, prefix, bindings))

    path, _, qs = full.partition("?")
    from sandloom.main import app

    try:
        from sandloom.core.unit_of_work.dispatch import drain_post_commit
    except ImportError:  # pragma: no cover - older builds

        async def drain_post_commit() -> None:
            return None

    life = await ns._Lifespan(app).__aenter__()
    base_headers = [
        (b"host", b"127.0.0.1:8000"),
        (b"user-agent", b"ladder"),
        (b"accept", b"*/*"),
        (b"authorization", f"Bearer {token}".encode()),
    ]
    scope0 = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": qs.encode(),
        "root_path": "",
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    ok = expect.split(",")
    served0 = _count(provider[1]) if provider else 0.0

    async def op(i: int) -> Any:
        body = _body(body_t, i)
        req = {"type": "http.request", "body": body, "more_body": False}
        out: list[Any] = []

        async def receive() -> Any:
            return req

        async def send(msg: Any) -> None:
            out.append(msg)

        scope = dict(scope0)
        headers = list(base_headers)
        if body:
            headers += [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ]
        scope["headers"] = headers
        scope["state"] = {}
        await app(scope, receive, send)
        await drain_post_commit()
        if str(out[0]["status"]) not in ok:
            raise RuntimeError(f"{method} {path}: {out[0]['status']} {out[1:2]}")
        return out

    async def teardown() -> None:
        try:
            await asyncio.sleep(0.2)  # let queued in-memory deliveries run
            await life.aclose()
        finally:
            if provider:
                _write_tag(_count(provider[1]) - served0)
                provider[0].terminate()
                provider[0].wait(10)

    return op, teardown


async def _provider_role(
    ns: Any, svc: str, method: str, full: str, body_t: str, expect: str, token: str
):
    import uvicorn

    prefix = os.environ["LADDER_MIX_SPLIT_PREFIX"]
    os.environ.update(_svc_env("tenancy", prefix, {}))
    from sandloom.main import app

    n = [0]

    async def counted(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/_sandloom/rpc"):
            n[0] += 1
        await app(scope, receive, send)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            counted,
            host="127.0.0.1",
            port=port,
            loop="none",
            http="httptools",
            log_level="warning",
            lifespan="on",
        )
    )
    serving = asyncio.ensure_future(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    bindings = _membership_binding(port) if svc in ("apps", "billing") else {}
    caller = await asyncio.to_thread(_spawn, _svc_env(svc, prefix, bindings))
    reader, writer = await asyncio.open_connection("127.0.0.1", caller[1])
    ok = expect.split(",").__contains__

    async def op(i: int) -> Any:
        body = _body(body_t, i)
        head = (
            f"{method} {full} HTTP/1.1\r\nHost: 127.0.0.1\r\nUser-Agent: ladder\r\n"
            f"Authorization: Bearer {token}\r\n"
        )
        if body:
            head += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        writer.write(head.encode() + b"\r\n" + body)
        status_line = await reader.readline()
        length = 0
        while (line := await reader.readline()) not in (b"\r\n", b""):
            name, _, value = line.decode().partition(":")
            if name.lower() == "content-length":
                length = int(value)
        data = await reader.readexactly(length) if length else b""
        status = status_line.split()[1].decode()
        if not ok(status):
            raise RuntimeError(f"{method} {full}: {status} {data[:300]!r}")
        return data

    async def teardown() -> None:
        try:
            writer.close()
            await asyncio.sleep(0.2)
            _write_tag(n[0])
        finally:
            caller[0].terminate()
            await asyncio.to_thread(caller[0].wait, 10)
            server.should_exit = True
            await serving

    return op, teardown
