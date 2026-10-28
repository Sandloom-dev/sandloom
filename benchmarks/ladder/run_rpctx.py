"""Transport A/B on the integration topology (monolith vs split, mixed profile).

    INT_SP=<stand root> python ladder/run_rpctx.py --rounds 3 --out FILE

Same stand layout and guards as ``run_int.py`` (whose helpers it reuses:
dataset reset + row-count check before every server start, Redis flush,
foreign-pytest quiet wait, invalid runs kept and flagged):

* ``$INT_SP/b/{main,branch}/`` — ``git archive`` exports with their own
  ``benchmarks/.venv``; ``$INT_SP/int/{main,branch}.env`` — ``mix env-full``;
* ``$INT_SP/int/topo_{mono,split}.json`` — topologies for ``split_bench``;
* databases ``$RPCTX_MONO_DB`` (monolith) and ``$SPLIT_DB_PREFIX_<service>``
  (split), Redis dbs ``$RPCTX_MONO_REDIS`` / ``$RPCTX_SPLIT_REDIS``.

Cells (interleaved, order rotated per round): the monolith and the split of
``main`` (httpx, the only client there) and of ``branch`` with each
``RPC_TRANSPORT``. Every cell runs the mixed profile closed (conc 64) and open
(fixed rate, CO-corrected), plus the single RPC-crossing requests (closed).
Only the transport changes between the branch's split cells.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "scripts"))
import ci_lock  # noqa: E402  (the lock local CI holds; tools/ci/ci_lock.py)
import run_int as ri  # noqa: E402

_foreign_pytest = ri._foreign_pytest


def _foreign_load() -> int:
    """Foreign pytest processes + the local CI / CPU-benchmark lock (held)."""
    return _foreign_pytest() + int(ci_lock.is_held())


ri._foreign_pytest = _foreign_load

MONO_DB = os.environ.get("RPCTX_MONO_DB", "bench_rpctx_mixfull")
SPLIT_PREFIX = os.environ.get("SPLIT_DB_PREFIX", "bench_rpctx_split")
MONO_REDIS = tuple(
    int(x) for x in os.environ.get("RPCTX_MONO_REDIS", "14,15").split(",")
)
SPLIT_REDIS = tuple(
    int(x) for x in os.environ.get("RPCTX_SPLIT_REDIS", "9,13").split(",")
)
SPLIT_DBS = [f"{SPLIT_PREFIX}_{s}" for s in ri.SPLIT]

#: (topology, build, RPC_TRANSPORT)
CELLS = [
    ("mono", "main", "httpx"),
    ("split", "main", "httpx"),
    ("mono", "branch", "aiohttp"),
    ("split", "branch", "aiohttp"),
    ("split", "branch", "lean"),
    ("split", "branch", "httpx"),
]
SINGLES = ("tickets", "subscription")


def _cmd(
    topo: str, build: str, transport: str, profile: str, mode: str, label: str, n: int
) -> list[str]:
    cmd = [
        ri.PY,
        "ladder/split_bench.py",
        "--topology",
        f"{ri.INT}/topo_{topo}.json",
        "--mix",
        f"{ri.PROF}/{profile}.json",
        "--env-file",
        f"{ri.INT}/{build}.env",
        "--env",
        "PLANNER_OPTIMIZED_EXECUTOR=true",
        "--env",
        f"RPC_TRANSPORT={transport}",
        "--header",
        "Authorization: Bearer " + ri._token(),
        "--python",
        f"{ri.B}/{build}/benchmarks/.venv/bin/python",
        "--cwd",
        f"{ri.B}/{build}/benchmarks",
        "--label",
        label,
    ]
    if mode == "closed":
        cmd += ["--conc", "64", "--n", str(n // 64 * 64), "--reps", "3"]
    else:
        cmd += ["--rate", str(ri.RATES[profile]), "--seconds", "30"]
    return cmd


def _prepare(topo: str, out: str) -> dict:
    if topo == "mono":
        return ri._prepare([MONO_DB], MONO_REDIS, out)
    return ri._prepare(SPLIT_DBS, SPLIT_REDIS, out)


def run(rounds: list[int], out: str, singles: bool) -> None:
    for rnd in rounds:
        for slot, (topo, build, transport) in enumerate(ri._order(CELLS, rnd)):
            cell = f"{topo}|{build}|{transport}"
            jobs = [("mixed", "closed", 8000), ("mixed", "open", 0)]
            if singles:
                jobs += [(f"single_{k}", "closed", 4000) for k in SINGLES]
            for profile, mode, n in jobs:
                prep = _prepare(topo, out)
                ri._run(
                    _cmd(topo, build, transport, profile, mode, f"{profile}|{cell}|{mode}", n),
                    {
                        "section": "rpctx",
                        "round": rnd,
                        "slot": slot,
                        "topology": topo,
                        "build": build,
                        "transport": transport,
                        "profile": profile,
                        "mode": mode,
                    },
                    out,
                    prep,
                )


def open_singles(rounds: list[int], out: str, rate: int) -> None:
    """The RPC-crossing requests at an equal FIXED rate (open loop): does a
    PG / Redis CPU change survive when the incoming rate is held constant?"""
    cells = [c for c in CELLS if c[0] == "split" or c[1] == "main"]
    cells = [c for c in cells if not (c[1] == "branch" and c[2] in ("httpx", "lean"))]
    for key in SINGLES:
        ri.RATES[f"single_{key}"] = rate
    for rnd in rounds:
        for slot, (topo, build, transport) in enumerate(ri._order(cells, rnd)):
            cell = f"{topo}|{build}|{transport}"
            for key in SINGLES:
                profile = f"single_{key}"
                prep = _prepare(topo, out)
                ri._run(
                    _cmd(topo, build, transport, profile, "open", f"{profile}|{cell}|open", 0),
                    {
                        "section": "rpctx_open_singles",
                        "round": rnd,
                        "slot": slot,
                        "topology": topo,
                        "build": build,
                        "transport": transport,
                        "profile": profile,
                        "mode": "open",
                        "rate": rate,
                    },
                    out,
                    prep,
                )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--out", required=True)
    p.add_argument("--no-singles", action="store_true")
    p.add_argument(
        "--open-singles",
        type=int,
        default=0,
        help="instead: the single RPC requests open-loop at this fixed rate",
    )
    a = p.parse_args()
    ri.write_profiles()
    rounds = list(range(a.start, a.start + a.rounds))
    if a.open_singles:
        open_singles(rounds, a.out, a.open_singles)
    else:
        run(rounds, a.out, not a.no_singles)
