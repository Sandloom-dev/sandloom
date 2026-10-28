"""Write the ``cg_matrix.py`` plans of the 2026-10-13 final measurements.

    python ladder/cg_plans.py routeplan <old-export> <rebased-export> > plan.json
    python ladder/cg_plans.py release <prev-export> <rel-export> > plan.json
    python ladder/cg_plans.py hotreads <main-export> <port-export> > plan.json

Exports are ``git archive`` trees (``src/``, ``examples/``, ``COMMIT``).
Databases (``sandloom_postgres_test``, never the shared ``sandloom_test``): the mix
monolith copies ``bench_cg_{old,new,prev,rel}`` (``CREATE DATABASE … TEMPLATE``
of the seeded ``bench_oa_mixfull`` — 1898eaa/276fe84 schema — or
``bench_rf_mixfull`` — aa03e00 schema), the split sets ``bench_oa_split_*`` /
``bench_rf_split_*``, the S5 database ``bench_rp_s5`` (role ``bench_app``).
Redis ``sandloom_redis_bench`` (:6390), one db pair per lane.

* ``routeplan`` — ``HTTP_ROUTE_PLANS`` false/true on the SAME build, for the
  old base and the rebased copy: S5 ``uow`` / ``planopt`` (``S5V``) and every
  login-free mix request (``MIX``, monolith).
* ``hotreads`` — main before the hot-reads port vs the port
  (``docs/design/hot-reads-planner-2026-10-21.md``): every login-free mix
  request on the monolith, lanes ``old`` / ``new`` on ``bench_hr_{old,new}``
  (``TEMPLATE`` copies of a ``benchsuite.mix seed-full`` database seeded with
  the NEW build's migrations), Redis dbs 224-227 of ``sandloom_redis_test``
  (:6380, 256 dbs), image ``sandloom-ladder-cg:3.13`` (``ladder/cg/Dockerfile``).
* ``release`` — the previous recorded main vs the current main: every
  login-free mix request on the monolith, and on the split topology the
  CALLER (service owning the route; tenancy untraced) of every request plus
  the PROVIDER (tenancy, in-process uvicorn) of the requests whose service
  binds ``tenancy.membership.v1`` (+ the ``health`` baseline of that role).
"""

from __future__ import annotations

import json
import os
import sys

PG = "postgresql://test_user:test_password@localhost:5433"
PG_SA = "postgresql+asyncpg://test_user:test_password@localhost:5433"
JWT = "bench-local-secret-0123456789abcdef0123456789abcdef"
#: login-free catalog keys of the mixes (benchsuite.mix.PROFILES minus login)
KEYS = [
    "me",
    "org",
    "orgs",
    "members",
    "tickets",
    "taskboard",
    "notifications",
    "link",
    "subscription",
    "credits",
    "post_ticket",
    "post_task",
    "post_link",
    "health",
]
#: split service owning each key (ladder/split topology routes)
OWNER = {
    "me": "identity",
    "health": "identity",
    "org": "tenancy",
    "orgs": "tenancy",
    "members": "tenancy",
    "subscription": "billing",
    "credits": "billing",
    "tickets": "apps",
    "post_ticket": "apps",
    "taskboard": "apps",
    "notifications": "apps",
    "link": "apps",
    "post_task": "apps",
    "post_link": "apps",
}
SPLIT = ("identity", "tenancy", "billing", "apps")


def _commit(root: str) -> str:
    p = os.path.join(root, "COMMIT")
    return open(p).read().strip() if os.path.exists(p) else "?"


def _mono_lane(
    root: str,
    db: str,
    redis: tuple[int, int],
    plans: tuple[str, ...],
    redis_url: str = "redis://localhost:6390",
):
    jobs = []
    for flag in plans:
        for k in KEYS:
            env = {"LADDER_MIX_KEY": k, "LADDER_MIX_SVC": "mono"}
            if flag:
                env["HTTP_ROUTE_PLANS"] = flag
            jobs.append(
                {
                    "name": f"mono/{k}" + (f"/plans={flag}" if flag else ""),
                    "rung": "MIX",
                    "env": env,
                }
            )
    return {
        "build": root,
        "commit": _commit(root),
        "redis": list(redis),
        "reset": [db],
        "env": {
            "DATABASE_URL": f"{PG_SA}/{db}?ssl=disable",
            "CACHE_URL": f"{redis_url}/{redis[0]}",
            "BROKER_URL": f"{redis_url}/{redis[1]}",
            "JWT_SECRET": JWT,
        },
        "jobs": jobs,
    }


def _s5_lane(old: str, new: str) -> dict:
    jobs = []
    for build, root in (("old", old), ("new", new)):
        for v, planner in (("uow", "false"), ("planopt", "true")):
            for flag in ("false", "true"):
                lv = (
                    v
                    if flag == "false"
                    else {"uow": "uowhttp", "planopt": "planhttp"}[v]
                )
                jobs.append(
                    {
                        "name": f"{build}/s5_{v}/plans={flag}",
                        "rung": "S5V",
                        "build": root,
                        "env": {
                            "LADDER_S5_VARIANT": lv,
                            "HTTP_ROUTE_PLANS": flag,
                            "PLANNER_OPTIMIZED_EXECUTOR": planner,
                        },
                    }
                )
    return {
        "build": old,
        "redis": [5, 6],
        "reset": [],
        "env": {
            "DATABASE_URL": f"{PG_SA}/bench_rp_s5?ssl=disable",
            "CACHE_URL": "redis://localhost:6390/5",
            "BROKER_URL": "redis://localhost:6390/6",
            "JWT_SECRET": JWT,
            "DB_RLS_MERGED_BEGIN": "true",
            "S5_APP_ROLE": "bench_app",
            "S5_APP_PASSWORD": "bench-app-local-password",
        },
        "jobs": jobs,
    }


def _split_lane(root: str, prefix: str, redis: tuple[int, int]) -> dict:
    jobs = []
    for k in KEYS:
        jobs.append(
            {
                "name": f"split/{k}/caller",
                "rung": "MIX",
                "env": {
                    "LADDER_MIX_KEY": k,
                    "LADDER_MIX_SVC": OWNER[k],
                    "LADDER_MIX_ROLE": "caller",
                },
            }
        )
    for svc in ("apps", "billing"):
        for k in [x for x in KEYS if OWNER[x] == svc] + ["health"]:
            jobs.append(
                {
                    "name": f"split/{k}@{svc}/provider",
                    "rung": "MIX",
                    "env": {
                        "LADDER_MIX_KEY": k,
                        "LADDER_MIX_SVC": svc,
                        "LADDER_MIX_ROLE": "provider",
                    },
                }
            )
    return {
        "build": root,
        "commit": _commit(root),
        "redis": list(redis),
        "reset": [f"{prefix}_{s}" for s in SPLIT],
        "env": {
            "DATABASE_URL": f"{PG_SA}/{prefix}_identity?ssl=disable",
            "LADDER_MIX_SPLIT_PREFIX": prefix,
            "CACHE_URL": f"redis://localhost:6390/{redis[0]}",
            "BROKER_URL": f"redis://localhost:6390/{redis[1]}",
            "JWT_SECRET": JWT,
        },
        "jobs": jobs,
    }


def main() -> None:
    kind, a, b = sys.argv[1:4]
    harness = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plan = {
        "image": "sandloom-ladder-cg:3.13-rf",
        "harness": harness,
        "k": 200,
        "pg": PG,
        "lanes": {},
    }
    if kind == "routeplan":
        plan["lanes"]["old"] = _mono_lane(a, "bench_cg_old", (1, 2), ("false", "true"))
        plan["lanes"]["new"] = _mono_lane(b, "bench_cg_new", (3, 4), ("false", "true"))
        plan["lanes"]["s5"] = _s5_lane(a, b)
    elif kind == "hotreads":
        plan["image"] = "sandloom-ladder-cg:3.13"
        plan["redis_container"] = "sandloom_redis_test"
        test_redis = "redis://localhost:6380"
        plan["lanes"]["old"] = _mono_lane(
            a, "bench_hr_old", (224, 225), ("",), test_redis
        )
        plan["lanes"]["new"] = _mono_lane(
            b, "bench_hr_new", (226, 227), ("",), test_redis
        )
    else:
        plan["lanes"]["prev"] = _mono_lane(a, "bench_cg_prev", (7, 8), ("",))
        plan["lanes"]["rel"] = _mono_lane(b, "bench_cg_rel", (10, 11), ("",))
        plan["lanes"]["prev_split"] = _split_lane(a, "bench_oa_split", (9, 13))
        plan["lanes"]["rel_split"] = _split_lane(b, "bench_rf_split", (12, 14))
    json.dump(plan, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
