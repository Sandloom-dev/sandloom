"""Aggregate contract-cache SHADOW reports (ADR-0003, "Then").

Every app process started with ``CONTRACT_CACHE_SHADOW`` (and
``CONTRACT_CACHE_SHADOW_REPORT_DIR``) writes ``shadow-<caller>-<pid>.json`` at
exit (``sandloom.core.contract_cache_shadow``). This sums them and prints what a
caller-side TTL cache WOULD have done — nothing in the measured run was ever
served from a cache:

    python ladder/agg_shadow.py <report dir> [--json] [--routes]

Per caller / contract / method / TTL (``methods``):

* ``calls`` = provider calls observed (hit + miss + negative + unkeyable;
  per-request memo hits are not provider calls and not counted);
* ``hit_pct`` = share a cache would have answered; ``stale_pct`` = share of
  hits whose cached answer differed from the live one, and ``over_grant`` the
  stale hits that would have granted MORE (removed / downgraded / narrowed —
  ``stale_kinds`` has every kind);
* ``read_ms`` = mean measured read duration; ``saved_ms`` = summed duration
  of the reads that would have been hits (wall clock — an upper bound of the
  caller's latency saving, not of CPU);
* ``inv_live`` = invalidations that dropped a still-live entry.

Per caller / TTL over HTTP requests (``requests``; kernel ops paths excluded):
``req`` requests, ``reads_pct`` share making at least one watched read,
``reads_per_req``, ``hits_per_req`` and ``hit_req_pct`` (share of requests
with a would-be hit), ``saved_ms_per_req`` — the per-method hit rate weighted
by how often requests actually make those reads. ``--routes`` adds the same
per route.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict
from typing import Any

OVER_GRANT = ("removed", "downgraded", "narrowed")


def _reports(directory: str) -> list[dict[str, Any]]:
    out = []
    for path in sorted(glob.glob(os.path.join(directory, "shadow-*.json"))):
        with open(path, encoding="utf-8") as fh:
            out.append(json.load(fh))
    return out


def aggregate(directory: str) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """(caller, contract, method, ttl) -> outcome counts + stale kinds + times."""
    out: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: defaultdict(float)
    )
    reads: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0, 0.0])
    for report in _reports(directory):
        caller = report["caller"]
        for row in report["counts"]:
            key = (caller, row["contract"], row["method"], row["ttl"])
            out[key][row["outcome"]] += int(row["count"])
        for row in report.get("stale", []):
            key = (caller, row["contract"], row["method"], row["ttl"])
            out[key]["stale:" + row["kind"]] += int(row["count"])
        for row in report.get("saved_s", []):
            key = (caller, row["contract"], row["method"], row["ttl"])
            out[key]["saved_s"] += float(row["seconds"])
        for row in report.get("read_s", []):
            acc = reads[(caller, row["contract"], row["method"])]
            acc[0] += int(row["reads"])
            acc[1] += float(row["seconds"])
    for (caller, contract, method, _ttl), c in out.items():
        n, s = reads.get((caller, contract, method), [0, 0.0])
        c["read_n"], c["read_s"] = n, s
    return out


def rows(agg: dict[tuple[str, str, str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for (caller, contract, method, ttl), c in sorted(agg.items()):
        calls = int(c["hit"] + c["miss"] + c["negative"] + c["unkeyable"])
        hit, stale = int(c["hit"]), int(c["stale_hit"])
        kinds = {
            k.split(":", 1)[1]: int(v) for k, v in c.items() if k.startswith("stale:")
        }
        result.append(
            {
                "caller": caller,
                "contract": contract,
                "method": method,
                "ttl": ttl,
                "calls": calls,
                "hit": hit,
                "hit_pct": round(100.0 * hit / calls, 2) if calls else 0.0,
                "stale_hit": stale,
                "stale_pct": round(100.0 * stale / hit, 3) if hit else 0.0,
                "over_grant": sum(kinds.get(k, 0) for k in OVER_GRANT),
                "stale_kinds": kinds,
                "read_ms": round(1000.0 * c["read_s"] / c["read_n"], 3)
                if c["read_n"]
                else 0.0,
                "saved_ms": round(1000.0 * c["saved_s"], 3),
                "negative": int(c["negative"]),
                "inv_live": int(c["invalidated_live"]),
                "inv_expired": int(c["invalidated_expired"]),
            }
        )
    return result


def _empty_stats() -> dict[str, Any]:
    return {"requests": 0, "with_reads": 0, "reads": 0, "by_ttl": {}}


def _merge(into: dict[str, Any], other: dict[str, Any]) -> None:
    for key in ("requests", "with_reads", "reads"):
        into[key] += int(other.get(key, 0))
    for ttl, row in other.get("by_ttl", {}).items():
        dst = into["by_ttl"].setdefault(
            ttl, {"hits": 0, "requests_with_hit": 0, "saved_s": 0.0, "stale": 0}
        )
        for k in ("hits", "requests_with_hit", "stale"):
            dst[k] += int(row.get(k, 0))
        dst["saved_s"] += float(row.get("saved_s", 0.0))


def request_stats(
    directory: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """(caller, route or "*") -> summed per-request stats."""
    out: dict[tuple[str, str], dict[str, Any]] = defaultdict(_empty_stats)
    for report in _reports(directory):
        caller = report["caller"]
        for route, stats in report.get("requests", {}).get("routes", {}).items():
            _merge(out[(caller, route)], stats)
            _merge(out[(caller, "*")], stats)
    return out


def request_rows(
    stats: dict[tuple[str, str], dict[str, Any]], *, routes: bool
) -> list[dict[str, Any]]:
    result = []
    for (caller, route), s in sorted(stats.items()):
        if route != "*" and not routes:
            continue
        req = s["requests"]
        ttls = sorted(s["by_ttl"], key=float) or ["-"]
        for ttl in ttls:
            row = s["by_ttl"].get(ttl, {})
            result.append(
                {
                    "caller": caller,
                    "route": route,
                    "ttl": ttl,
                    "req": req,
                    "reads_pct": round(100.0 * s["with_reads"] / req, 2) if req else 0.0,
                    "reads_per_req": round(s["reads"] / req, 3) if req else 0.0,
                    "hits_per_req": round(row.get("hits", 0) / req, 3) if req else 0.0,
                    "hit_req_pct": round(
                        100.0 * row.get("requests_with_hit", 0) / req, 2
                    )
                    if req
                    else 0.0,
                    "saved_ms_per_req": round(
                        1000.0 * row.get("saved_s", 0.0) / req, 4
                    )
                    if req
                    else 0.0,
                    "stale": row.get("stale", 0),
                }
            )
    return result


def _print(table: list[dict[str, Any]], head: tuple[str, ...]) -> None:
    print(" | ".join(head))
    for r in table:
        print(" | ".join(str(r[h]) for h in head))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("directory")
    p.add_argument("--json", action="store_true")
    p.add_argument("--routes", action="store_true", help="per-route request rows")
    a = p.parse_args()
    methods = rows(aggregate(a.directory))
    requests = request_rows(request_stats(a.directory), routes=a.routes)
    if a.json:
        print(json.dumps({"methods": methods, "requests": requests}, indent=1))
        return
    _print(
        methods,
        (
            "caller",
            "method",
            "ttl",
            "calls",
            "hit_pct",
            "stale_pct",
            "over_grant",
            "read_ms",
            "saved_ms",
            "negative",
            "inv_live",
        ),
    )
    print()
    _print(
        requests,
        (
            "caller",
            "route",
            "ttl",
            "req",
            "reads_pct",
            "reads_per_req",
            "hits_per_req",
            "hit_req_pct",
            "saved_ms_per_req",
            "stale",
        ),
    )


if __name__ == "__main__":
    main()
