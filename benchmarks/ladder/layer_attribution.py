"""Attribute a server cProfile (``server_profile.py cprof``) to request layers.

    python ladder/layer_attribution.py STATS.prof RESULT.json [--json OUT]

Own time (``tottime``) of every function goes to the layer of its file /
function (:data:`RULES`, first match). Builtins and generic stdlib helpers
(``isinstance``, ``contextlib``, ``base64``, ...) have no layer of their own:
their time is charged to their CALLERS' layers in proportion to the time each
caller edge accounts for (recursively). cProfile adds a fixed overhead per
call; it is removed by calibration, ``c = (sum(tt) - plain_cpu) / sum(ncalls)``
(``plain_cpu`` = user + system CPU per request of the UNprofiled run), so the
corrected layers add up to the real CPU per request. The event loop's idle
wait (uvloop ``run_forever`` own time) is excluded.
"""

from __future__ import annotations

import argparse
import json
import pstats
from typing import Any

#: (layer, file substrings, function names or None = any) — first match wins
RULES: list[tuple[str, tuple[str, ...], tuple[str, ...] | None]] = [
    (
        "jwt",
        ("sandloom/core/auth/tokens.py", "sandloom/core/auth/bearer.py"),
        (
            "decode_token",
            "_decode_token_uncached",
            "_verification_keys",
            "decode_token_cached",
            "_verification_settings",
            "bearer_claims",
            "bearer_token",
            "_remember_verified",
        ),
    ),
    ("jwt", ("/jwt/",), None),
    (
        "revocation",
        (
            "sandloom/core/auth/verification.py",
            "sandloom/core/auth/marks.py",
            "sandloom/core/auth/blocks.py",
            "sandloom/core/auth/local_marks.py",
        ),
        (
            "_read_marks",
            "token_access_status",
            "ensure_token_not_revoked",
            "_revoked_by_mark",
            "_revocation_key",
            "block_key",
            "_clock",
        ),
    ),
    (
        "revocation",
        (
            "/redis/",
            "hiredis",
            "sandloom/core/auth/blocks.py",
            "sandloom/core/auth/freshness.py",
            "sandloom/core/drivers/redis_cache.py",
            "sandloom/core/infra/access.py",
        ),
        None,
    ),
    ("tenant_rls", ("sandloom/core/tenant", "tenant_rls"), None),
    (
        "db_driver",
        (
            "/asyncpg/",
            "sqlalchemy/dialects/postgresql/asyncpg",
            "sqlalchemy/engine/",
            "sqlalchemy/pool/",
            "linger_pool",
            "dead_connection",
            "/greenlet/",
            "sqlalchemy/util/concurrency",
            "sqlalchemy/util/_concurrency",
            "sqlalchemy/ext/asyncio/",
        ),
        None,
    ),
    (
        "orm_planner_uow",
        (
            "/sqlalchemy/",
            "sandloom/core/planner",
            "planner_adapter",
            "planner_safety",
            "sandloom/core/orm",
            "sandloom/core/unit_of_work",
            "sandloom/core/sql_alchemy/",
            "sandloom/core/_internal/db_provider",
        ),
        None,
    ),
    (
        "orm_planner_uow",
        ("sandloom/core/di.py",),
        ("get_db", "get_planned_reads", "db_session", "_uow"),
    ),
    (
        "serialization",
        (
            "/pydantic/",
            "pydantic_core",
            "fastapi/encoders",
            "starlette/responses",
            "/json/",
            "orjson",
        ),
        None,
    ),
    (
        "serialization",
        ("fastapi/routing.py",),
        ("serialize_response", "_prepare_response_content"),
    ),
    (
        "logging_metrics",
        ("/logging/", "prometheus_client", "sandloom/core/observability/"),
        None,
    ),
    (
        "middleware",
        (
            "sandloom/core/http/",
            "starlette/middleware",
            "starlette/_exception_handler",
            "fastapi/middleware",
            "starlette/exceptions",
            "sandloom/core/idempotency/",
        ),
        None,
    ),
    ("middleware", ("sandloom/core/module_api.py",), ("__call__",)),
    ("middleware", ("sandloom/core/rpc.py", "sandloom/core/rpc/server.py"), ("__call__",)),
    (
        "routing_di",
        (
            "/fastapi/",
            "starlette/routing",
            "starlette/requests",
            "starlette/datastructures",
            "sandloom/shared/",
            "sandloom/core/di.py",
            "sandloom/core/runtime",
            "benchsuite/bench",
            "/anyio/",
            "sandloom/core/module_api.py",
            "sandloom/core/operation_classes",
            "sandloom/core/route_markers",
        ),
        None,
    ),
    ("server", ("/uvicorn/", "httptools", "uvloop", "/asyncio/", "/h11/"), None),
]
#: no layer of their own: charged to their callers
GENERIC = (
    "~",
    "contextlib",
    "typing.py",
    "functools",
    "/abc.py",
    "enum.py",
    "uuid.py",
    "_collections_abc",
    "copy.py",
    "inspect.py",
    "/re/",
    "base64.py",
    "hmac.py",
    "hashlib.py",
    "datetime",
    "weakref",
    "dataclasses",
    "<frozen",
    "types.py",
    "collections/__init__",
    "email/",
    "urllib/",
    "threading.py",
    "warnings.py",
    "string.py",
    "operator.py",
    "ipaddress.py",
    "http/",
    "random.py",
    "decimal",
    "_weakrefset",
    "posixpath",
    "genericpath",
    "os.py",
    "secrets.py",
)
IDLE = ("run_forever", "run_until_complete", "control", "select", "poll")
LAYERS = [
    "server",
    "middleware",
    "routing_di",
    "jwt",
    "revocation",
    "tenant_rls",
    "db_driver",
    "orm_planner_uow",
    "serialization",
    "logging_metrics",
    "other",
]


def layer_of(key: tuple[str, int, str]) -> str | None:
    file, _line, func = key
    for layer, files, funcs in RULES:
        if any(f in file for f in files) and (funcs is None or func in funcs):
            return layer
    if file == "~" or any(g in file for g in GENERIC):
        return None
    return "other"


def attribute(stats_file: str, plain_cpu_us: float, requests: int) -> dict[str, Any]:
    st = pstats.Stats(stats_file).stats  # type: ignore[attr-defined]
    idle = {k for k in st if k[0] == "~" and any(i in k[2] for i in IDLE)}
    total_tt = sum(v[2] for k, v in st.items() if k not in idle) / requests * 1e6
    total_nc = sum(v[1] for k, v in st.items() if k not in idle) / requests
    c = (total_tt - plain_cpu_us) / total_nc  # µs per call
    memo: dict[Any, dict[str, float]] = {}

    def share(key: Any, depth: int = 0) -> dict[str, float]:
        """Layer distribution of the time spent in ``key`` (sums to 1)."""
        if key in memo:
            return memo[key]
        own = layer_of(key)
        if own is not None:
            memo[key] = {own: 1.0}
            return memo[key]
        memo[key] = {"other": 1.0}  # cycle guard
        if depth > 40:
            return memo[key]
        callers = st[key][4]
        weights: dict[str, float] = {}
        total = 0.0
        for caller, edge in callers.items():
            w = max(edge[2] - c * 1e-6 * edge[1], 0.0) or 1e-12
            total += w
            for layer, frac in share(caller, depth + 1).items():
                weights[layer] = weights.get(layer, 0.0) + w * frac
        if total > 0:
            memo[key] = {k: v / total for k, v in weights.items()}
        return memo[key]

    layers = dict.fromkeys(LAYERS, 0.0)
    for key, v in st.items():
        if key in idle:
            continue
        own_us = max(v[2] / requests * 1e6 - c * v[1] / requests, 0.0)
        for layer, frac in share(key).items():
            layers[layer] += own_us * frac
    total = sum(layers.values())
    return {
        "per_call_overhead_us": c,
        "calls_per_request": total_nc,
        "attributed_us": total,
        "layers_us": {k: round(v, 1) for k, v in layers.items()},
        "layers_share": {k: round(v / total, 4) for k, v in layers.items()},
    }


# --------------------------------------------------------------------------- #
# perf call chains (server_profile.py perf): the authoritative attribution
# --------------------------------------------------------------------------- #
#: (layer, python-frame path/qualname substrings, C dso/symbol substrings);
#: the INNERMOST frame matching any rule decides (see classify_chain).
PERF_RULES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    (
        "jwt",
        (
            "/jwt/",
            "security.py:decode_token",
            "decode_token:",
            "_decode_token_uncached",
            "_verification_keys",
            "decode_token_cached",
            "_remember_verified",
            "_verification_settings",
        ),
        (),
    ),
    (
        "revocation",
        (
            "/redis/",
            "auth_freshness.py",
            "redis_cache.py",
            "_read_marks",
            "token_access_status",
            "ensure_token_not_revoked",
            "_revoked_by_mark",
        ),
        ("hiredis",),
    ),
    ("tenant_rls", ("tenant_rls.py", "sandloom/core/tenant.py", "tenant_context.py"), ()),
    (
        "db_driver",
        (
            "/asyncpg/",
            "dialects/postgresql/asyncpg",
            "sqlalchemy/engine/",
            "sqlalchemy/pool/",
            "linger_pool",
            "dead_connection",
            "util/concurrency",
            "util/_concurrency",
            "ext/asyncio/",
        ),
        ("asyncpg", "greenlet"),
    ),
    ("logging_metrics", ("/logging/", "prometheus_client", "observability/"), ()),
    (
        "serialization",
        (
            "serialize_response",
            "_prepare_response_content",
            "fastapi/encoders",
            "starlette/responses",
        ),
        ("orjson",),
    ),
    (
        "orm_planner_uow",
        (
            "/sqlalchemy/",
            "sandloom/core/planner",
            "planner_adapter",
            "planner_safety",
            "sandloom/core/orm",
            "sandloom/core/unit_of_work/",
            "sql_alchemy/uow.py",
            "_internal/db_provider",
            "di.py:get_db",
            "get_planned_reads",
            "di.py:_",
        ),
        (),
    ),
    (
        "middleware",
        (
            "sandloom/core/http/",
            "starlette/middleware",
            "starlette/_exception_handler",
            "fastapi/middleware",
            "sandloom/core/idempotency/",
            "ContractReadScopeMiddleware",
            "RpcFastPathMiddleware",
        ),
        (),
    ),
    (
        "routing_di",
        (
            "/fastapi/",
            "starlette/routing",
            "starlette/requests",
            "starlette/datastructures",
            "sandloom/shared/",
            "sandloom/core/di.py",
            "route_index",
            "benchsuite/bench",
            "/anyio/",
            "sandloom/core/module_api.py",
            "sandloom/core/runtime",
        ),
        (),
    ),
    ("server", ("/uvicorn/", "/asyncio/", "/h11/"), ("uvloop", "httptools", "libuv")),
]
#: deferred libraries: charged to the innermost OTHER layer above them
DEFERRED_PY = ("/pydantic/", "/json/")
DEFERRED_C = ("pydantic_core",)


def _frame_key(line: str) -> tuple[str, str]:
    """``(python 'qualname:path' or '', C 'symbol (dso)')`` of one frame line."""
    body = line.strip().split(" ", 1)[1] if " " in line.strip() else line.strip()
    if body.startswith("py::"):
        name, _, _dso = body[4:].rpartition(" (")
        qual, _, path = name.partition(":")
        # "path:qualname" so rules can match a file and a function together
        leaf = path.rsplit("/", 2)[-2:]
        return f"{path}:{qual} {'/'.join(leaf)}:{qual}", ""
    return "", body


def classify_chain(frames: list[str]) -> tuple[str, bool]:
    """``(layer, kernel)`` of one sample (frames leaf -> root)."""
    kernel = bool(frames) and "[kernel.kallsyms]" in frames[0]
    if any("gc_collect" in f or "_PyGC_Collect" in f for f in frames):
        return "gc", kernel
    deferred = False
    for line in frames:
        py, c = _frame_key(line)
        if py and any(d in py for d in DEFERRED_PY):
            deferred = True
            continue
        if c and any(d in c for d in DEFERRED_C):
            deferred = True
            continue
        for layer, pys, cs in PERF_RULES:
            if (py and any(x in py for x in pys)) or (c and any(x in c for x in cs)):
                if layer == "db_driver":
                    # a driver frame under the tenant sync is the RLS BEGIN
                    for up in frames[frames.index(line) + 1 :]:
                        upy, _uc = _frame_key(up)
                        if upy and any(x in upy for x in PERF_RULES[2][1]):
                            return "tenant_rls", kernel
                        if upy and "sandloom/" in upy and "linger_pool" not in upy:
                            break
                return layer, kernel
    return ("serialization" if deferred else "other"), kernel


def attribute_perf(path: str) -> dict[str, Any]:
    user: dict[str, int] = {}
    kern: dict[str, int] = {}
    chain: list[str] = []
    n = 0

    def flush() -> None:
        nonlocal n
        if not chain:
            return
        layer, kernel = classify_chain(chain)
        bucket = kern if kernel else user
        bucket[layer] = bucket.get(layer, 0) + 1
        n += 1

    with open(path, errors="replace") as fh:
        for raw in fh:
            if raw.startswith("\t") and raw.strip():
                chain.append(raw.rstrip("\n"))
            elif not raw.strip():
                flush()
                chain = []
        flush()
    nu, nk = sum(user.values()), sum(kern.values())
    return {
        "samples": n,
        "kernel_share_of_samples": nk / max(n, 1),
        "user_layers_share": {k: user.get(k, 0) / max(nu, 1) for k in PERF_LAYERS},
        "kernel_by_caller_layer": {k: kern.get(k, 0) / max(nk, 1) for k in PERF_LAYERS},
    }


PERF_LAYERS = [*LAYERS, "gc"]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("stats", help="cProfile .prof, or perf script text with --perf")
    p.add_argument("result", nargs="?", help="server_profile.py cprof JSON line")
    p.add_argument("--perf", action="store_true")
    p.add_argument("--json", default="")
    a = p.parse_args()
    if a.perf:
        out = attribute_perf(a.stats)
        print(json.dumps(out, indent=1))
        if a.json:
            with open(a.json, "w") as fh:
                json.dump(out, fh, indent=1)
        return
    res = json.loads(open(a.result).read().strip().splitlines()[-1])
    plain = res["plain"]["user_us_per_req"] + res["plain"]["sys_us_per_req"]
    out = attribute(a.stats, plain, res["profiled"]["requests"])
    out.update(variant=res["variant"], plain_cpu_us=plain, run=res)
    print(json.dumps(out, indent=1))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(out, fh, indent=1)


if __name__ == "__main__":
    main()
