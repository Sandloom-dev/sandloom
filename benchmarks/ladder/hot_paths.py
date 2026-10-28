"""Hot-path profiling stand: the per-request / per-call paths of the framework.

Installed into ``ladder.py`` like ``stands.py`` (every rung runs under every
ladder mode, one rung per process: ``cpu``, ``prof``, ``calls``, ``alloc``,
``rt`` and ``cg`` for cachegrind). The stand REUSES the existing rungs where
they already isolate a path and adds the missing ones (``H*``):

=========  ==================================================================
path       rung(s)
=========  ==================================================================
strict GET ``Pme`` (``/auth/me``: JWT + revocation/block ``MGET`` + DbSession
           UoW, identity), ``L6S5`` (S5: + TenantScope + RLS + Subscribed)
budgeted   ``HSbud``: ``/s5bud/{id}``, the S5 read as a class-R route with a
GET        5 s ``revocation_freshness`` budget (block mark still read live)
W/F write  ``L6W3`` (insert + ``publish_event`` + ``record_audit``),
           ``HW7``: W3 as a class-F route (``strict_revocation`` +
           durable ``idempotency_guard``, fresh ``Idempotency-Key``)
local      ``HCs``: ``core.get_service`` + a ``tenancy.membership.v1`` read
contract   in a request read scope, per op (what billing does per request);
           ``HK0``-``HK3``: the same runtime WITHOUT I/O (deterministic for
           cachegrind): a planned no-op fetched once / via get_service per
           call, a planned ``list[UUID]`` read in a scope, and twice (memo);
           ``HCm``: ``HCs`` + the SAME read again (memo hit); ``HCd``: an
           L1 ``Contract(REF)`` read returning a DTO (``get_member_state``:
           the provider's class converted to the stub's generated copy);
           ``C2`` = the
           read on a provider fetched once; ``R4.loc`` / ``R4r.loc`` = a
           command (durable command log claim/complete) and its replay
RPC        ``R3t.rpc`` / ``R3t.srv`` / ``R0t.enc``: the one internal-RPC
           authentication (pair-MAC request, allowlist, X25519-keyed
           response binding; commands also Ed25519-signed)
planner    ``L6S5p`` / ``L6S5po`` (``PlannedReads``, general / optimized)
consumer   ``HEp``: ``publish_event`` in a UoW + post-commit dispatch only;
delivery   ``HEc``: the same event consumed by a durable consumer
           (XREADGROUP, decode, UoW + inbox claim, COMMIT, XACK);
           ``HEc - HEp`` = one delivery
issuance   ``HTr``: ``POST /auth/refresh`` (refresh-token rotation + access
           token mint with claims from identity, tenancy and billing)
=========  ==================================================================

Seed once (owner DSN in ``DATABASE_URL``)::

    python -m benchsuite.setup_db && python -m benchsuite.platform seed

Profile one rung, or the whole stand (``run`` writes one JSON document)::

    uv run python ladder/ladder.py prof HTr --n 2000
    uv run python ladder/hot_paths.py run --modes prof,calls,rt \\
        --out ladder/results/hot_paths_<date>.json
    CG_K=200 ladder/cg/run_cg.sh Pme HSbud HW7 HCs HCm HEp HEc HTr

Every number is per op and sequential (conc 1). The machine may be shared:
prefer ``cg`` (instruction counts) and ``calls`` (Python call counts) over
``cpu``; ``prof`` locates costs (cProfile inflates small functions).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from typing import Any

#: The stand's own rungs (the reused ones are listed in :data:`SUITE`).
OWN = (
    "HSbud",
    "HW7",
    "HCs",
    "HCm",
    "HCd",
    "HEp",
    "HEc",
    "HTr",
    "HK0",
    "HK1",
    "HK2",
    "HK3",
)
#: rung -> extra env of its process.
SUITE: dict[str, dict[str, str]] = {
    "Pme": {},
    "L6S5": {},
    "HSbud": {},
    "L6W3": {},
    "HW7": {},
    "C2": {},
    "HCs": {},
    "HCm": {},
    "HCd": {},
    "HK0": {},
    "HK1": {},
    "HK2": {},
    "HK3": {},
    "R4.loc": {},
    "R4r.loc": {},
    "R0t.enc": {},
    "R3t.srv": {},
    "R3t.rpc": {},
    "R3t.rpcn": {},
    "L6S5p": {},
    "L6S5po": {},
    "HEp": {},
    "HEc": {},
    "HTr": {},
}


def _require_fresh(stand: str) -> None:
    if "sandloom.core.config.settings" in sys.modules:
        raise RuntimeError(f"the {stand} env must be set before importing sandloom")


# --------------------------------------------------------------------------- #
# S5 env rungs: budgeted GET, class-F write
# --------------------------------------------------------------------------- #
async def _hsbud(ns: Any) -> Any:
    token = ns._s5_env("bud")
    app, life = await ns._sandloom_app()
    return ns._s5_op(app, "/s5bud/", token), life.aclose


async def _hw7(ns: Any) -> Any:
    _require_fresh("write")
    from benchsuite import s5, writes

    os.environ.update(writes.server_env())
    token = s5.mint()
    app, life = await ns._sandloom_app()
    from sandloom.core.unit_of_work.dispatch import drain_post_commit

    nonce = os.urandom(6).hex()
    base = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/dw/7",
        "raw_path": b"/dw/7",
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    async def op(i: int) -> Any:
        body = writes.body_of("W3", i)
        req = {"type": "http.request", "body": body, "more_body": False}
        out: list[Any] = []

        async def receive() -> Any:
            return req

        async def send(msg: Any) -> None:
            out.append(msg)

        scope = dict(base)
        scope["headers"] = [
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {token}".encode()),
            (b"idempotency-key", f"hp-{nonce}-{i}".encode()),
            (b"content-length", str(len(body)).encode()),
        ]
        scope["state"] = {}
        await app(scope, receive, send)
        if out[0]["status"] != 201:
            raise RuntimeError(f"/dw/7: {out}")
        await drain_post_commit()
        return out[1]["body"]

    return op, life.aclose


# --------------------------------------------------------------------------- #
# Platform env rungs: local contract calls, token refresh
# --------------------------------------------------------------------------- #
def _platform_env() -> Any:
    _require_fresh("platform")
    from benchsuite import platform as plat

    os.environ.update(plat.server_env())
    os.environ.pop("MODULE_REQUIRED", None)
    return plat


async def _contract(ns: Any, twice: bool) -> Any:
    plat = _platform_env()
    from sandloom.core.module_api import contract_read_scope
    from sandloom.core.runtime import get_active_core
    from sandloom.main import app

    life = await ns._Lifespan(app).__aenter__()
    core = get_active_core()
    user = plat.USER

    async def op(_i: int) -> Any:
        async with contract_read_scope():
            svc = core.get_service("tenancy.membership.v1")
            first = await svc.list_user_organization_ids(user)
            if twice:
                again = core.get_service("tenancy.membership.v1")
                if await again.list_user_organization_ids(user) != first:
                    raise RuntimeError("memo returned another result")
            return first

    return op, life.aclose


async def _contract_dto(ns: Any) -> Any:
    plat = _platform_env()
    from sandloom.core.module_api import contract_read_scope
    from sandloom.main import app
    from sandloom.sdk import Contract
    from sandloom.shared.contracts.tenancy_membership_v1 import TENANCY_MEMBERSHIP_V1

    life = await ns._Lifespan(app).__aenter__()
    membership = Contract(TENANCY_MEMBERSHIP_V1)

    async def op(_i: int) -> Any:
        async with contract_read_scope():
            state = await membership.get_member_state(plat.ORG, plat.USER)
            if state.role is None:
                raise RuntimeError("the seeded user is not a member")
            return state

    return op, life.aclose


async def _htr(ns: Any) -> Any:
    plat = _platform_env()
    import orjson

    from sandloom.core.di import db_session
    from sandloom.core.runtime import get_active_core
    from sandloom_identity.models.user import User
    from sandloom_identity.services.token_service import TokenService
    from sandloom.main import app

    life = await ns._Lifespan(app).__aenter__()
    core = get_active_core()
    async with db_session() as db:
        user = await db.get(User, plat.USER)
        pair = await TokenService.create_tokens(user, db, core)
    state = {"refresh": pair.refresh_token}
    base = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/auth/refresh",
        "raw_path": b"/auth/refresh",
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    async def op(_i: int) -> Any:
        body = orjson.dumps({"refresh_token": state["refresh"]})
        req = {"type": "http.request", "body": body, "more_body": False}
        out: list[Any] = []

        async def receive() -> Any:
            return req

        async def send(msg: Any) -> None:
            out.append(msg)

        scope = dict(base)
        scope["headers"] = [
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        scope["state"] = {}
        await app(scope, receive, send)
        if out[0]["status"] != 200:
            raise RuntimeError(f"/auth/refresh: {out}")
        state["refresh"] = orjson.loads(out[1]["body"])["refresh_token"]
        return out[1]["body"]

    return op, life.aclose


# --------------------------------------------------------------------------- #
# Contract runtime without I/O (RPC stand env: bench.rpcdiag.v1, planned)
# --------------------------------------------------------------------------- #
async def _runtime(ns: Any, kind: str) -> Any:
    _require_fresh("RPC")
    from benchsuite import rpcstand

    os.environ.update(rpcstand.server_env())
    from sandloom.core.module_api import contract_read_scope
    from sandloom.core.runtime import get_active_core
    from sandloom.main import app

    life = await ns._Lifespan(app).__aenter__()
    core = get_active_core()
    org, user = rpcstand.ORG, rpcstand.USER
    once = core.get_service(rpcstand.DIAG)

    async def fetched_once(_i: int) -> Any:
        return await once.noop(org, user)

    async def per_call(_i: int) -> Any:
        return await core.get_service(rpcstand.DIAG).noop(org, user)

    async def scoped(_i: int) -> Any:
        async with contract_read_scope():
            return await core.get_service(rpcstand.DIAG).noop_ids(org, user)

    async def scoped_twice(_i: int) -> Any:
        async with contract_read_scope():
            first = await core.get_service(rpcstand.DIAG).noop_ids(org, user)
            again = await core.get_service(rpcstand.DIAG).noop_ids(org, user)
            if again != first or again is first:
                raise RuntimeError("the memo must return an equal copy")
            return again

    ops = {"K0": fetched_once, "K1": per_call, "K2": scoped, "K3": scoped_twice}
    return ops[kind], life.aclose


# --------------------------------------------------------------------------- #
# Event rungs: publish (outbox + post-commit XADD) and one durable delivery
# --------------------------------------------------------------------------- #
async def _reset_streams() -> None:
    import redis.asyncio as redis

    from benchsuite import common

    for url in {common.cache_url(), common.broker_url()}:
        client = redis.Redis.from_url(url)
        try:
            await client.delete(common.EVENT_CHANNEL, f"{common.EVENT_CHANNEL}.dlq")
        finally:
            await client.aclose()


async def _events(ns: Any, consume: bool) -> Any:
    _require_fresh("event")
    import asyncio

    os.environ["EVENT_BUS_BACKEND"] = "streams"
    if consume:
        os.environ["BENCH_HOTPATH_CONSUMER"] = "1"
    await _reset_streams()
    app, life = await ns._sandloom_app()
    from sandloom.core.di import db_session, publish_event
    from sandloom.core.unit_of_work.dispatch import drain_post_commit
    from benchsuite.bench import hotpath
    from benchsuite.common import EVENT_CHANNEL

    loop = asyncio.get_running_loop()

    async def op(i: int) -> Any:
        waiter = None
        if consume:
            waiter = hotpath.WAITERS[i] = loop.create_future()
        async with db_session():
            await publish_event(EVENT_CHANNEL, {"id": i, "name": f"hp{i}"})
        await drain_post_commit()
        if waiter is not None:
            await asyncio.wait_for(waiter, 10)
        return i

    return op, life.aclose


def install(ns: Any) -> None:
    """Register the stand's rungs in ``ladder.RUNGS`` (``ns`` = ladder module)."""

    def add(name: str, desc: str, build: Any, *args: Any) -> None:
        async def factory() -> Any:
            return await build(ns, *args)

        ns.RUNGS[name] = (desc, factory)

    add(
        "HSbud",
        "S5 read as class R with a 5 s revocation budget: GET /s5bud/{id}",
        _hsbud,
    )
    add(
        "HW7",
        "W3 as class F: strict revocation + durable idempotency_guard, POST /dw/7",
        _hw7,
    )
    add(
        "HCs",
        "get_service + local tenancy.membership.v1 read in a read scope",
        _contract,
        False,
    )
    add("HCm", "HCs + the same read again in the scope (memo hit)", _contract, True)
    add(
        "HCd",
        "L1 Contract(REF) get_member_state read returning a DTO, in a read scope",
        _contract_dto,
    )
    add(
        "HEp",
        "publish_event in db_session + post-commit XADD (no consumer)",
        _events,
        False,
    )
    add(
        "HEc",
        "HEp + one durable consumer delivery (XREADGROUP, UoW + inbox claim, XACK)",
        _events,
        True,
    )
    add("HTr", "POST /auth/refresh: rotation + access-token mint with claims", _htr)
    for kind, desc in (
        ("K0", "planned no-op contract read on a provider fetched once (no I/O)"),
        ("K1", "HK0 with core.get_service(...) per call"),
        ("K2", "get_service + planned list[UUID] read in a read scope (no I/O)"),
        ("K3", "HK2 + the same read again (memo hit)"),
    ):
        add(f"H{kind}", desc, _runtime, kind)


# --------------------------------------------------------------------------- #
# Suite runner
# --------------------------------------------------------------------------- #
def _run(rung: str, mode: str, n: int, warmup: int) -> dict[str, Any]:
    env = dict(os.environ, **SUITE.get(rung, {}))
    cmd = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "ladder.py"),
        mode,
        rung,
        "--n",
        str(n),
        "--warmup",
        str(warmup),
        "--reps",
        "3",
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        return {"rung": rung, "mode": mode, "error": proc.stderr[-2000:]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="every SUITE rung in every mode -> one JSON")
    run.add_argument("--modes", default="calls,rt")
    run.add_argument("--rungs", default=",".join(SUITE))
    run.add_argument("--n", type=int, default=1000)
    run.add_argument("--warmup", type=int, default=200)
    run.add_argument("--out", required=True)
    a = parser.parse_args()
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    results: list[dict[str, Any]] = []
    for rung in a.rungs.split(","):
        for mode in a.modes.split(","):
            res = _run(rung, mode, a.n, a.warmup)
            res["env"] = SUITE.get(rung, {})
            results.append(res)
            print(f"{rung:9s} {mode:6s} {'ERROR' if 'error' in res else 'ok'}")
    rev = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    with open(a.out, "w") as fh:
        json.dump(
            {
                "started": started,
                "commit": rev,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "n": a.n,
                "warmup": a.warmup,
                "results": results,
            },
            fh,
            indent=1,
        )


if __name__ == "__main__":
    main()
