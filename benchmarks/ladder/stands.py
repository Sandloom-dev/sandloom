"""Ladder rungs of the two DIAGNOSTIC stands (installed into ``ladder.py``).

A. RPC cost decomposition — ``R<m>[t].<layer>``: method ``m`` of
   ``benchsuite.rpcstand.METHODS`` (what the provider body adds) through
   ``layer`` of ``benchsuite.rpcstand.LAYERS`` (which side of the wire is
   measured); a ``t`` suffix binds a tenant, so the authenticated RPC
   context carries it (``bind_tenant`` on the server). Every call carries
   the pair-MAC (MAC on the client, verify + replay guard on the server).
   ``R0raw`` is the bare coroutine (the floor).

       R0raw  < R0.loc   = contract runtime (local, planned, no UoW)
       R0.enc            = client codec + context/pair-MAC + envelope decode
       R0.cli - R0.enc   = httpx client machinery (no socket)
       R0.srv            = server side (fast path, verify, decode, runtime, encode)
       R0.rpc ~ R0.cli + R0.srv (+ ASGITransport glue)
       R0t.* - R0.*      = tenant in the context (encode / decode / bind)
       R2.* - R0.*       = provider read UoW (no statement)
       R2q.* - R2.*      = + connection checkout, BEGIN, SELECT 1, ROLLBACK
       R3.* / R3o.*      = the real tenancy.membership.v1 read (planner gen/opt)
       R3u.*             = the same query in a plain read UoW (pre-planner form)
       R4.* - R2.*       = command: dedupe reserve + persist, COMMIT vs ROLLBACK
       R4r.*             = command replay (dedupe hit; the body never runs)

   Env: ``benchsuite.rpcstand.server_env`` (ENVIRONMENT=prod, sandloom_tenancy +
   bench, BENCH_RPC_DIAG=1); the DB must be seeded by
   ``python -m benchsuite.platform seed``.

B. Write path without argon2 — ``L6W<k>``: POST ``/dw/<k>`` through the full
   Sandloom ASGI stack in-process, on the S5 env (JWT + TenantScope + Subscribed +
   FORCEd RLS, NOBYPASSRLS role) with ``BENCH_WRITE_DIAG=1``
   (``benchsuite.writes.RUNGS``). Seed with ``python -m benchsuite.setup_db``.
   Each op awaits ``drain_post_commit()``: the deferred outbox dispatch (bus
   XADD + the outbox row marked delivered in its own transaction) is counted
   in the op that caused it.
"""

from __future__ import annotations

import os
import sys
from typing import Any


def _rpc_boot(planner_opt: bool) -> None:
    if "sandloom.core.config.settings" in sys.modules:
        raise RuntimeError("the RPC stand env must be set before importing sandloom")
    from benchsuite import rpcstand

    os.environ.update(rpcstand.server_env(planner_opt=planner_opt))


def install(ns: Any) -> None:
    """Register the stands' rungs in ``ladder.RUNGS`` (``ns`` = ladder module)."""
    from benchsuite import rpcstand, writes

    rung = ns.rung
    rung("R0raw", "the bare no-op coroutine (no contract machinery): the floor")(
        _raw_rung
    )
    for base, m in rpcstand.METHODS.items():
        for tenant in (False, True):
            for layer, ldesc in rpcstand.LAYERS.items():
                name = f"{base}{'t' if tenant else ''}.{layer}"
                desc = f"{m.service}.{m.method} [{m.desc}] via {ldesc}"
                if tenant:
                    desc += " + signed tenant context"
                ns.RUNGS[name] = (desc, _rpc_rung(ns, m, layer, tenant))
    for key, (method, path, _body, expect) in writes.RUNGS.items():
        ns.RUNGS[f"L6{key}"] = (
            f"write stand {key}: {method} {path} full ASGI in-process (S5 auth "
            f"stack, RLS, expect {expect})",
            _write_rung(ns, key),
        )


async def _raw_rung():
    from benchsuite.bench.rpc_diag import raw_noop
    from benchsuite.rpcstand import ORG, USER

    async def op(_i: int) -> Any:
        return await raw_noop(ORG, USER)

    async def teardown() -> None:
        return None

    return op, teardown


# --------------------------------------------------------------------------- #
# A. RPC rungs (the layer builders live in benchsuite.rpcstand: the tests
#    exercise exactly the code the rungs measure)
# --------------------------------------------------------------------------- #
def _rpc_rung(ns: Any, m: Any, layer: str, tenant: bool):
    async def factory():
        _rpc_boot(m.planner_opt)
        from sandloom.core.runtime import get_active_core
        from sandloom.main import app
        from benchsuite import rpcstand

        life = await ns._Lifespan(app).__aenter__()
        op, close = await rpcstand.build_layer(
            app, get_active_core(), m, layer, tenant=tenant
        )

        async def teardown() -> None:
            await close()
            await life.aclose()

        return op, teardown

    return factory


# --------------------------------------------------------------------------- #
# B. Write-path rungs
# --------------------------------------------------------------------------- #
def _write_rung(ns: Any, key: str):
    async def factory():
        if "sandloom.core.config.settings" in sys.modules:
            raise RuntimeError("the write stand env must be set before importing sandloom")
        from benchsuite import s5, writes

        os.environ.update(writes.server_env())
        token = s5.mint()
        app, life = await ns._sandloom_app()
        method, _path, _body, expect = writes.RUNGS[key]
        post = _write_post(app, method, int(expect), token)
        from sandloom.core.unit_of_work.dispatch import drain_post_commit

        async def op(i: int) -> Any:
            body = await post(writes.path_of(key, i), writes.body_of(key, i))
            # the post-commit outbox dispatch (XADD + mark delivered) is part
            # of the op: wait for it so per-op counts / CPU never leak
            await drain_post_commit()
            return body

        return op, life.aclose

    return factory


def _write_post(app: Any, method: str, expect: int, token: str):
    headers = [
        (b"host", b"127.0.0.1:8000"),
        (b"user-agent", b"ladder"),
        (b"content-type", b"application/json"),
        (b"authorization", f"Bearer {token}".encode()),
    ]
    base = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    async def post(path: str, body: bytes) -> Any:
        req = {"type": "http.request", "body": body, "more_body": False}
        out: list[Any] = []

        async def receive() -> Any:
            return req

        async def send(msg: Any) -> None:
            out.append(msg)

        scope = dict(base)
        scope["path"] = path
        scope["raw_path"] = path.encode()
        scope["headers"] = [*headers, (b"content-length", str(len(body)).encode())]
        scope["state"] = {}
        await app(scope, receive, send)
        if out[0]["status"] != expect:
            raise RuntimeError(f"{path}: {out}")
        return out[1]["body"]

    return post


__all__ = ["install"]
