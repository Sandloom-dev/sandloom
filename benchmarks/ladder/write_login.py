"""Write / login path profiling stand (``WL*`` rungs).

Installed into ``ladder.py`` like ``hot_paths.py``: every rung runs under every
ladder mode (``cpu``, ``prof``, ``calls``, ``alloc``, ``rt``, ``cg``), one rung
per process. It REUSES the rungs that already isolate a write path and adds
the missing ones:

=========  ==================================================================
path       rung(s)
=========  ==================================================================
class F    ``HW7`` (``POST /dw/7``: strict revocation, durable
write      ``idempotency_guard``, insert + ``publish_event`` +
           ``record_audit``, a fresh ``Idempotency-Key``)
class W    ``L6W3`` (``POST /dw/3``: the same write without the guard)
credits    ``WLc``: ``billing.credits.v1.spend`` with a fresh
spend      ``operation_key`` (local command: durable command log, savepoint
           debit + ledger insert, outbox), what a consumer module does
role       ``WLr``: ``PATCH /api/v1/orgs/{org}/members/{user}`` alternating
change     ``admin`` / ``member`` (row locks, member version upsert, event,
           IMMEDIATE token revocation of the member, audit)
login      ``WLl``: ``POST /auth/login`` with the default (``strong``)
           argon2id parameters; ``WLlf``: the same login with the cheapest
           argon2 parameters (m=8 KiB, t=1, p=1), i.e. the framework's
           share without the password hash
2FA login  ``WL2``: ``WLlf`` for a user with TOTP enabled (+ ``otp``)
refresh    ``HTr`` (``POST /auth/refresh``: rotation + claims mint)
=========  ==================================================================

Seed once (owner DSN in ``DATABASE_URL``, after ``benchsuite.setup_db`` and
``benchsuite.platform seed``)::

    uv run --frozen python ladder/write_login.py seed
    uv run --frozen python ladder/ladder.py rt WLr --n 50
    uv run --frozen python ladder/write_login.py run --modes calls,rt \\
        --out ladder/results/write_login_<date>.json
    CG_K=100 ladder/cg/run_cg.sh HW7 L6W3 WLc WLr WLlf WL2 HTr

``WL2`` uses a different seeded 2FA user per op (a TOTP step is accepted once
per user) and resets their last accepted step when the rung starts: keep
``--n + --warmup`` below :data:`TWOFA_USERS`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform as _platform
import subprocess
import sys
import time
import uuid
from typing import Any

#: The stand's own rungs.
OWN = ("WLc", "WLr", "WLl", "WLlf", "WL2")
#: rung -> extra env (the suite = the reused rungs + the stand's own).
SUITE: dict[str, dict[str, str]] = {
    "HW7": {},
    "L6W3": {},
    "WLc": {},
    "WLr": {},
    "WLlf": {},
    "WL2": {},
    "HTr": {},
    "WLl": {},
}

LOGIN_USER = uuid.UUID("00000000-0000-4000-8000-00000000c001")
LOGIN_FAST_USER = uuid.UUID("00000000-0000-4000-8000-00000000c002")
MEMBER = uuid.UUID("00000000-0000-4000-8000-00000000c003")
LOGIN_EMAIL = "wl-login@example.com"
LOGIN_FAST_EMAIL = "wl-login-fast@example.com"
PASSWORD = "Wl-Bench-Password-0123456789!"
TWOFA_USERS = 5000
#: Users with the default (strong) hash, for concurrent login load.
STRONG_USERS = 64
TWOFA_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
#: A fixed dev key (the stand's processes all share it; never a real key).
TWOFA_KEY = "7b" * 32
#: argon2id parameters of the ``fast`` rungs (framework cost without the hash).
FAST_HASH = {"time_cost": 1, "memory_kib": 8, "parallelism": 1}
STRONG_HASH = {"time_cost": 3, "memory_kib": 65536, "parallelism": 4}


def strong_email(k: int) -> str:
    """A seeded user with the ``strong`` hash (the login-concurrency stand)."""
    return f"wl-strong-{k}@example.com"


def _twofa_email(i: int) -> str:
    return f"wl2fa-{i}@example.com"


def _hash_env(fast: bool) -> dict[str, str]:
    if not fast:
        return {"PASSWORD_HASH_PROFILE": "strong"}
    return {
        "PASSWORD_HASH_PROFILE": "custom",
        "PASSWORD_HASH_TIME_COST": str(FAST_HASH["time_cost"]),
        "PASSWORD_HASH_MEMORY_KIB": str(FAST_HASH["memory_kib"]),
        "PASSWORD_HASH_PARALLELISM": str(FAST_HASH["parallelism"]),
    }


def _env(*, fast: bool = True) -> Any:
    """The platform env (identity + tenancy + billing) of the stand's rungs."""
    if "sandloom.core.config.settings" in sys.modules:
        raise RuntimeError("the write/login env must be set before importing sandloom")
    from benchsuite import platform as plat

    os.environ.update(plat.server_env())
    os.environ.pop("MODULE_REQUIRED", None)
    os.environ.update(
        {
            "TWOFA_ENCRYPTION_KEY": TWOFA_KEY,
            # the relay is a background poller: its CPU would leak into per-op
            # numbers; post-commit dispatch still publishes every event.
            "OUTBOX_RELAY_ENABLED": "off",
            "SANDLOOM_ACCEPTED_RISKS": "relay_disabled",
            "EVENT_BUS_BACKEND": "streams",
            **_hash_env(fast),
        }
    )
    return plat


async def _release_group_owners() -> None:
    """Forget the Streams group-owner fingerprints of THIS stand's Redis.

    The fingerprint names the database URL, and ``ladder.py rt`` serves the
    app through a counting proxy on a random port: without this the identity
    consumers refuse to start on the second run.
    """
    import redis.asyncio as redis

    from benchsuite import common

    for url in {common.cache_url(), common.broker_url()}:
        client = redis.Redis.from_url(url)
        try:
            keys = [k async for k in client.scan_iter("sandloom:group-owner:*")]
            if keys:
                await client.delete(*keys)
        finally:
            await client.aclose()


def _asgi(app: Any, method: str, path: str, headers: list[tuple[bytes, bytes]]):
    """One in-process request: ``await call(body) -> (status, body)``."""
    base = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    async def call(body: bytes, extra: list[tuple[bytes, bytes]] = ()) -> Any:
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
            *headers,
            *extra,
        ]
        scope["state"] = {}
        await app(scope, receive, send)
        return out[0]["status"], out[1]["body"]

    return call


# --------------------------------------------------------------------------- #
# Rungs
# --------------------------------------------------------------------------- #
async def _credits(ns: Any) -> Any:
    plat = _env()
    from sandloom.core.runtime import get_active_core
    from sandloom.main import app

    await _release_group_owners()
    life = await ns._Lifespan(app).__aenter__()
    core = get_active_core()
    nonce = os.urandom(6).hex()
    user = plat.USER

    async def op(i: int) -> Any:
        svc = core.get_service("billing.credits.v1")
        return await svc.spend("user", user, 1, f"wl-{nonce}-{i}", reason="bench")

    return op, life.aclose


async def _role(ns: Any) -> Any:
    plat = _env()
    from sandloom.core.unit_of_work.dispatch import drain_post_commit
    from sandloom.main import app

    await _release_group_owners()
    life = await ns._Lifespan(app).__aenter__()
    token = plat.mint()
    call = _asgi(
        app,
        "PATCH",
        f"/api/v1/orgs/{plat.ORG}/members/{MEMBER}",
        [(b"authorization", f"Bearer {token}".encode())],
    )
    bodies = (b'{"role":"admin"}', b'{"role":"member"}')
    # start from a known state: the member is a plain member
    await call(bodies[1])
    await drain_post_commit()

    async def op(i: int) -> Any:
        status, body = await call(bodies[i % 2])
        if status != 200:
            raise RuntimeError(f"role change: {status} {body!r}")
        await drain_post_commit()
        return body

    return op, life.aclose


async def _login(ns: Any, fast: bool, twofa: bool) -> Any:
    _env(fast=fast)
    import orjson
    import pyotp

    from sandloom.core.unit_of_work.dispatch import drain_post_commit
    from sandloom.main import app

    if twofa:
        await _reset_totp_steps()
    await _release_group_owners()
    life = await ns._Lifespan(app).__aenter__()
    call = _asgi(app, "POST", "/auth/login", [])
    email = LOGIN_FAST_EMAIL if fast else LOGIN_EMAIL
    totp = pyotp.TOTP(TWOFA_SECRET)

    async def op(i: int) -> Any:
        if twofa:
            payload = {
                "email": _twofa_email(i % TWOFA_USERS),
                "password": PASSWORD,
                "otp": totp.now(),
            }
        else:
            payload = {"email": email, "password": PASSWORD}
        status, body = await call(orjson.dumps(payload))
        if status != 200:
            raise RuntimeError(f"login: {status} {body!r}")
        await drain_post_commit()
        return body

    return op, life.aclose


async def _reset_totp_steps() -> None:
    import asyncpg

    from benchsuite import common

    conn = await asyncpg.connect(common.asyncpg_dsn())
    try:
        await conn.execute(
            """UPDATE twofa_secrets SET last_used_step = NULL
               WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'wl2fa-%')"""
        )
    finally:
        await conn.close()


def install(ns: Any) -> None:
    """Register the stand's rungs in ``ladder.RUNGS`` (``ns`` = ladder module)."""

    def add(name: str, desc: str, build: Any, *args: Any) -> None:
        async def factory() -> Any:
            return await build(ns, *args)

        ns.RUNGS[name] = (desc, factory)

    add("WLc", "billing.credits.v1.spend, local command, fresh operation_key", _credits)
    add("WLr", "PATCH member role (admin <-> member): locks, revocation, audit", _role)
    add(
        "WLl",
        "POST /auth/login, strong argon2id (m=64 MiB, t=3, p=4)",
        _login,
        False,
        False,
    )
    add(
        "WLlf",
        "POST /auth/login, cheapest argon2id (framework share)",
        _login,
        True,
        False,
    )
    add(
        "WL2",
        "WLlf for a TOTP user (+ otp; one seeded user per op)",
        _login,
        True,
        True,
    )


# --------------------------------------------------------------------------- #
# Layer attribution (``ladder.py layers <rung>``)
# --------------------------------------------------------------------------- #
#: Top-level layers: (label, file substring, function names or None = any,
#: sticky). The innermost matching frame owns the time, EXCEPT under a sticky
#: label, which keeps everything below it (an inclusive layer: claims
#: collection owns its own SQL, the idempotency guard its Redis round trip)
#: unless a :data:`SUB_RULES` entry splits it.
_AUTH = "auth guard (JWT verify, revocation/block MGET)"
_MODULE = "module code (endpoint + services)"
LAYER_RULES: tuple[tuple[str, str, frozenset[str] | None, bool], ...] = (
    (
        "argon2 (await; the hash runs on the pool thread)",
        "/sandloom/core/auth/hashing.py",
        frozenset({"run_password_hashing", "ahash_password", "averify_password"}),
        True,
    ),
    ("claims collection", "/sandloom/core/facade/", frozenset({"collect_claims"}), True),
    (
        "idempotency reserve",
        "/sandloom/core/idempotency/",
        frozenset({"_dependency"}),
        True,
    ),
    (
        "idempotency complete",
        "/sandloom/core/idempotency/",
        frozenset({"_persist_success", "_release_key", "_persist_terminal"}),
        True,
    ),
    (
        "token revocation write",
        "/sandloom/core/auth/revocation.py",
        frozenset({"revoke_user_tokens_on_commit", "revoke_user_tokens", "_revoke"}),
        True,
    ),
    ("lockout", "/sandloom_identity/lockout.py", None, True),
    (
        "block check",
        "/sandloom/core/auth/blocks.py",
        frozenset({"ensure_user_not_blocked"}),
        True,
    ),
    ("JWT mint", "/sandloom/core/auth/tokens.py", frozenset({"create_access_token"}), True),
    (
        "outbox/audit enqueue",
        "/sandloom/core/unit_of_work/",
        frozenset({"publish_event", "record_audit"}),
        True,
    ),
    (
        "post-commit dispatch",
        "/sandloom/core/unit_of_work/",
        frozenset(
            {
                "_dispatch_outbox_after_commit",
                "_process_round",
                "_process",
                "_publish_prefix",
                "_publish_in_order",
                "drain_post_commit",
                "_dispatch_pending_events",
            }
        ),
        True,
    ),
    ("COMMIT", "sqlalchemy/ext/asyncio/session.py", frozenset({"commit"}), True),
    (
        "BEGIN (+ tenant set_config)",
        "/sandloom/core/sql_alchemy/uow.py",
        frozenset({"__aenter__"}),
        True,
    ),
    (
        "SQL statements",
        "sqlalchemy/ext/asyncio/session.py",
        frozenset(
            {
                "execute",
                "scalar",
                "scalars",
                "get",
                "flush",
                "refresh",
                "stream",
                "connection",
            }
        ),
        True,
    ),
    ("contract runtime (+ command log)", "/sandloom/core/module_api.py", None, False),
    ("event consumers (in-process)", "/sandloom/core/eventing/", None, False),
    (_AUTH, "/sandloom/shared/guards/", None, True),
    (_AUTH, "/sandloom/core/auth/", None, True),
    (
        "response serialization",
        "fastapi/routing.py",
        frozenset({"serialize_response"}),
        True,
    ),
    (
        "FastAPI dependency resolution",
        "fastapi/dependencies/utils.py",
        frozenset({"solve_dependencies"}),
        False,
    ),
    (_MODULE, "/sandloom_identity/", None, False),
    (_MODULE, "/sandloom_tenancy/", None, False),
    (_MODULE, "/sandloom_billing/", None, False),
    (_MODULE, "/benchsuite/bench/", None, False),
    ("routing / endpoint call", "fastapi/routing.py", None, False),
    ("routing / endpoint call", "starlette/routing.py", None, False),
    ("middleware pipeline", "/sandloom/core/http/", None, False),
    ("logging + metrics", "/sandloom/core/observability/", None, False),
    ("HTTP app (Starlette / FastAPI)", "/starlette/", None, False),
    ("HTTP app (Starlette / FastAPI)", "fastapi/applications.py", None, False),
    ("Redis client", "/redis/", None, False),
    ("event loop (asyncio)", "/asyncio/", None, False),
)
#: Split a sticky layer: (sub-label, file substring, names, parents or None =
#: any sticky parent). Recorded as ``"<parent> > <sub-label>"``; sticky.
SUB_RULES: tuple[tuple[str, str, frozenset[str] | None, frozenset[str] | None], ...] = (
    (
        "ORM flush (pending INSERT/UPDATE)",
        "sqlalchemy/orm/session.py",
        frozenset({"flush"}),
        frozenset({"COMMIT"}),
    ),
    (
        "mark delivered UPDATE",
        "/sandloom/core/sql_alchemy/outbox_store/",
        frozenset({"mark_delivered"}),
        frozenset({"post-commit dispatch"}),
    ),
    (
        "bus publish (XADD)",
        "/sandloom/core/eventing/",
        None,
        frozenset({"post-commit dispatch"}),
    ),
    ("Redis client", "/redis/", None, None),
)


def _rule_for(code: Any, cache: dict[Any, Any]) -> Any:
    hit = cache.get(code, False)
    if hit is not False:
        return hit
    found = None
    fn = code.co_filename
    for label, needle, names, sticky in LAYER_RULES:
        if needle in fn and (names is None or code.co_name in names):
            found = (label, sticky)
            break
    cache[code] = found
    return found


def _sub_rule_for(code: Any, parent: str, cache: dict[Any, Any]) -> Any:
    key = (code, parent)
    hit = cache.get(key, False)
    if hit is not False:
        return hit
    found = None
    fn = code.co_filename
    for label, needle, names, parents in SUB_RULES:
        if (
            needle in fn
            and (names is None or code.co_name in names)
            and (parents is None or parent in parents)
        ):
            found = f"{parent} > {label}"
            break
    cache[key] = found
    return found


async def mode_layers(op: Any, a: Any) -> dict[str, Any]:
    """Main-thread CPU (``thread_time``) per op by layer, innermost frame wins.

    A ``sys.setprofile`` hook keeps a stack of labelled frames (coroutines
    pop on every suspension and push on every resume, so the stack is exact
    for one thread). Each event costs the hook's own overhead, calibrated on
    a trivial call loop and subtracted per label in proportion to its events.
    Other threads (the argon2 pool) are not seen: ``process_cpu_us`` -
    ``main_thread_us`` is their share (plus the hook's residual overhead).
    """
    import time as _t

    clock = _t.thread_time_ns
    await ns_run(op, 0, a.warmup)

    untagged = "other (harness, untagged)"

    def make_hook(times: dict[str, int], events: dict[str, int]) -> Any:
        cache: dict[Any, Any] = {}
        sub_cache: dict[Any, Any] = {}
        stack: list[tuple[Any, str, bool]] = []
        state = {"last": clock()}

        def hook(frame: Any, event: str, _arg: Any) -> None:
            now = clock()
            top = stack[-1][1] if stack else untagged
            times[top] = times.get(top, 0) + now - state["last"]
            events[top] = events.get(top, 0) + 1
            if event == "call":
                if stack and stack[-1][2]:
                    label = stack[-1][1]
                    if " > " not in label:
                        sub = _sub_rule_for(frame.f_code, label, sub_cache)
                        if sub is not None:
                            stack.append((frame, sub, True))
                else:
                    rule = _rule_for(frame.f_code, cache)
                    if rule is not None:
                        stack.append((frame, rule[0], rule[1]))
            elif event == "return" and stack and stack[-1][0] is frame:
                stack.pop()
            state["last"] = clock()

        return hook, state

    def calibrate() -> float:
        """Residual hook cost per event that still lands between the clocks."""

        def leaf() -> None:
            return None

        def loop(k: int) -> None:
            for _ in range(k):
                leaf()

        k = 200_000
        best = []
        for _ in range(3):
            t0 = clock()
            loop(k)
            bare = clock() - t0
            ctimes: dict[str, int] = {}
            cevents: dict[str, int] = {}
            probe, pstate = make_hook(ctimes, cevents)
            pstate["last"] = clock()
            sys.setprofile(probe)
            loop(k)
            sys.setprofile(None)
            seen = sum(ctimes.values())
            best.append((seen - bare) / max(sum(cevents.values()), 1))
        return max(min(best), 0.0)

    per_event = calibrate()
    times: dict[str, int] = {}
    events: dict[str, int] = {}
    hook, state = make_hook(times, events)

    c0 = _t.process_time_ns()
    state["last"] = clock()
    sys.setprofile(hook)
    try:
        await ns_run(op, a.warmup, a.n)
    finally:
        sys.setprofile(None)
    c1 = _t.process_time_ns()
    n = a.n
    out = {
        label: max(times[label] - events[label] * per_event, 0.0) / n / 1000
        for label in times
    }
    total = sum(out.values())
    ordered = sorted(out.items(), key=lambda kv: -kv[1])
    return {
        "per_event_overhead_ns": round(per_event, 1),
        "main_thread_us": round(total, 1),
        "process_cpu_us_profiled": round((c1 - c0) / n / 1000, 1),
        "layers_us": {k: round(v, 1) for k, v in ordered},
        "layers_share": {k: round(v / total, 4) for k, v in ordered},
        "events_per_op": {k: round(v / n, 1) for k, v in events.items()},
    }


async def ns_run(op: Any, start: int, n: int) -> None:
    for i in range(start, start + n):
        await op(i)


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #
async def seed() -> None:
    """Users, the 2FA users, a member to toggle and a funded billing customer."""
    plat = _env()
    import asyncpg

    from sandloom.core.auth.hashing import build_password_hasher
    from sandloom.core.config.password_params import PasswordHashParams
    from sandloom_identity.services.twofa_service import _encrypt_secret
    from benchsuite import common

    strong = build_password_hasher(PasswordHashParams(**STRONG_HASH)).hash(PASSWORD)
    fast = build_password_hasher(PasswordHashParams(**FAST_HASH)).hash(PASSWORD)
    secret = _encrypt_secret(TWOFA_SECRET)
    users = [
        (LOGIN_USER, LOGIN_EMAIL, strong),
        (LOGIN_FAST_USER, LOGIN_FAST_EMAIL, fast),
        (MEMBER, "wl-member@example.com", fast),
    ] + [
        (uuid.uuid5(uuid.NAMESPACE_URL, strong_email(k)), strong_email(k), strong)
        for k in range(STRONG_USERS)
    ]
    twofa = [
        (uuid.uuid5(uuid.NAMESPACE_URL, _twofa_email(i)), _twofa_email(i), fast)
        for i in range(TWOFA_USERS)
    ]
    conn = await asyncpg.connect(common.asyncpg_dsn())
    try:
        await conn.executemany(
            """INSERT INTO users (id, email, full_name, hashed_password, is_active,
                   is_superuser, is_verified, created_at, updated_at)
               VALUES ($1, $2, 'WL Bench', $3, true, false, true, now(), now())
               ON CONFLICT (id) DO UPDATE SET hashed_password = EXCLUDED.hashed_password""",
            users + twofa,
        )
        await conn.executemany(
            """INSERT INTO twofa_secrets (id, user_id, secret, is_enabled, created_at)
               VALUES ($1, $2, $3, true, now())
               ON CONFLICT (user_id) DO UPDATE SET secret = EXCLUDED.secret,
                   is_enabled = true, last_used_step = NULL""",
            [(uuid.uuid4(), u, secret) for u, _e, _h in twofa],
        )
        await conn.execute(
            """INSERT INTO tenancy_members (id, organization_id, user_id, role, created_at)
               VALUES ($1, $2, $3, 'member', now())
               ON CONFLICT (organization_id, user_id) DO UPDATE SET role = 'member'""",
            uuid.uuid4(),
            plat.ORG,
            MEMBER,
        )
        await conn.execute(
            """INSERT INTO billing_customers (id, user_id) VALUES ($1, $2)
               ON CONFLICT DO NOTHING""",
            uuid.uuid4(),
            plat.USER,
        )
        await conn.execute(
            """INSERT INTO billing_credit_balances (customer_id, amount)
               SELECT id, 1000000000000 FROM billing_customers WHERE user_id = $1
               ON CONFLICT (customer_id) DO UPDATE SET amount = 1000000000000""",
            plat.USER,
        )
    finally:
        await conn.close()
    print(f"write/login stand seeded: {len(users)} users + {TWOFA_USERS} TOTP users")


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
    sub.add_parser("seed", help="seed the stand's users / member / customer")
    run = sub.add_parser("run", help="every SUITE rung in every mode -> one JSON")
    run.add_argument("--modes", default="calls,rt")
    run.add_argument("--rungs", default=",".join(r for r in SUITE if r != "WLl"))
    run.add_argument("--n", type=int, default=1000)
    run.add_argument("--warmup", type=int, default=200)
    run.add_argument("--out", required=True)
    a = parser.parse_args()
    if a.cmd == "seed":
        asyncio.run(seed())
        return
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
                "python": _platform.python_version(),
                "platform": _platform.platform(),
                "n": a.n,
                "warmup": a.warmup,
                "results": results,
            },
            fh,
            indent=1,
        )


if __name__ == "__main__":
    main()
