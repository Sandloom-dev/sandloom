"""Aggregate run_int JSONL into Markdown tables: median of the per-round
values (min–max). Invalid runs are listed, never silently averaged in.

    python ladder/agg_int.py validity|matrix|login|scaling|topology|rpc FILE...
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from typing import Any

CONFIGS = ("pre38", "main38", "planner_off", "planner_on")
PROFILES = (
    "mixed",
    "mixed-nologin",
    "read-heavy",
    "write-heavy",
    "write-heavy-nologin",
)
#: login weight (%) of the profiles that contain it
LOGIN_PCT = {"mixed": 1.0, "write-heavy": 5.0}


def load(paths: list[str]) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        for line in open(path):
            if line.startswith("{"):
                rows.append(json.loads(line))
    return rows


def med(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None, None
    return statistics.median(vals), min(vals), max(vals)


def fmt(t, d=0):
    m, lo, hi = t
    if m is None:
        return "–"
    if f"{lo:,.{d}f}" == f"{hi:,.{d}f}":
        return f"{m:,.{d}f}"
    return f"{m:,.{d}f} ({lo:,.{d}f}–{hi:,.{d}f})"


def group(rows, keys):
    g = defaultdict(list)
    for r in rows:
        if r.get("invalid") or r.get("superseded"):
            continue
        g[tuple(r.get(k) for k in keys)].append(r)
    return g


def total(r):
    if r.get("server_cpu_us") is None or r.get("pg_cpu_us") is None:
        return None
    return r["server_cpu_us"] + r["pg_cpu_us"] + (r.get("redis_cpu_us") or 0)


def validity(rows):
    by = defaultdict(lambda: [0, 0, 0])
    classes: dict[str, int] = defaultdict(int)
    planned = ok = 0
    for r in rows:
        k = r.get("section")
        by[k][0] += 1
        by[k][1] += bool(r.get("invalid"))
        by[k][2] += bool(r.get("superseded"))
        for part in (r, r.get("warmup") or {}, r.get("rate_warmup") or {}):
            for c, v in (part.get("errors_by_class") or {}).items():
                classes[c] += v
            planned += part.get("planned") or 0
            ok += part.get("ok") or 0
    print("| section | runs | invalid | superseded (re-run) |\n|---|---|---|---|")
    for k, (n, bad, sup) in by.items():
        print(f"| {k} | {n} | {bad} | {sup} |")
    print(
        f"\nrequests planned {planned:,}, ok {ok:,}; errors by class: {dict(classes)}"
    )
    diffs = [r for r in rows if r.get("rows_diff")]
    print(f"runs whose dataset differed from its first snapshot: {len(diffs)}")
    totals = defaultdict(set)
    for r in rows:
        for db, n in (r.get("rows_total") or {}).items():
            totals[db].add(n)
    print(
        "rows at run start per database (distinct totals):",
        {db: sorted(v) for db, v in totals.items()},
    )
    for r in rows:
        if r.get("invalid"):
            print(
                "INVALID:",
                r.get("label"),
                r.get("invalid_reasons"),
                json.dumps(r.get("error_samples"))[:400],
            )


def matrix(rows):
    g = group(rows, ["profile", "config", "mode"])
    for prof in PROFILES:
        print(f"\n#### {prof}\n")
        print(
            "| config | app µs/op | PG µs/op | Redis µs/op | total µs/op | rps (closed) "
            "| RSS MB | p50 ms | p99 ms | total µs/op @rate |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|")
        for cfg in CONFIGS:
            c = g.get((prof, cfg, "closed"), [])
            o = g.get((prof, cfg, "open"), [])
            print(
                f"| {cfg} (n={len(c)}/{len(o)}) | {fmt(med([r['server_cpu_us'] for r in c]))} "
                f"| {fmt(med([r['pg_cpu_us'] for r in c]))} | {fmt(med([r['redis_cpu_us'] for r in c]))} "
                f"| {fmt(med([total(r) for r in c]))} | {fmt(med([r['rps'] for r in c]))} "
                f"| {fmt(med([r['rss_mb_per_worker'] for r in c]))} "
                f"| {fmt(med([r['p50_ms'] for r in o]), 2)} | {fmt(med([r['p99_ms'] for r in o]), 1)} "
                f"| {fmt(med([total(r) for r in o]))} |"
            )
    print("\n#### headline: planner_on vs main38 / pre38 (total µs/op, closed)\n")
    print(
        "| profile | pre38 | main38 | planner_off | planner_on | vs main38 | vs pre38 |"
    )
    print("|---|---|---|---|---|---|---|")
    for prof in PROFILES:
        t = {
            c: med([total(r) for r in g.get((prof, c, "closed"), [])])[0]
            for c in CONFIGS
        }
        if None in t.values():
            continue
        on = t["planner_on"]
        print(
            f"| {prof} | {t['pre38']:,.0f} | {t['main38']:,.0f} | {t['planner_off']:,.0f} "
            f"| {on:,.0f} | {on - t['main38']:+,.0f} ({(on / t['main38'] - 1) * 100:+.1f} %) "
            f"| {on - t['pre38']:+,.0f} ({(on / t['pre38'] - 1) * 100:+.1f} %) |"
        )
    print("\n#### attribution (total µs/op, closed)\n")
    print(
        "| profile | #38 (pre38→main38) | planner code, executor off (main38→off) "
        "| executor (off→on) |"
    )
    print("|---|---|---|---|")
    for prof in PROFILES:
        t = {
            c: med([total(r) for r in g.get((prof, c, "closed"), [])])[0]
            for c in CONFIGS
        }
        if None in t.values():
            continue
        print(
            f"| {prof} | {t['main38'] - t['pre38']:+,.0f} | {t['planner_off'] - t['main38']:+,.0f} "
            f"| {t['planner_on'] - t['planner_off']:+,.0f} |"
        )


def login(rows):
    g = group(rows, ["section", "config", "mode"])
    mg = group(rows, ["profile", "config", "mode"])
    print(
        "| config | conc | app µs/login | PG µs | Redis µs | total µs/login | logins/s |"
    )
    print("|---|---|---|---|---|---|---|")
    for cfg in CONFIGS:
        for mode in ("login1", "login4"):
            c = g.get(("login", cfg, mode), [])
            print(
                f"| {cfg} (n={len(c)}) | {mode[-1]} | {fmt(med([r['server_cpu_us'] for r in c]))} "
                f"| {fmt(med([r['pg_cpu_us'] for r in c]))} | {fmt(med([r['redis_cpu_us'] for r in c]))} "
                f"| {fmt(med([total(r) for r in c]))} | {fmt(med([r['rps'] for r in c]), 1)} |"
            )
    print(
        "\n| config | mixed − mixed-nologin (µs/op) | implied µs per login (÷ 1 %) "
        "| write-heavy − nologin | implied µs per login (÷ 5 %) |"
    )
    print("|---|---|---|---|---|")
    for cfg in CONFIGS:
        cells = []
        for prof in ("mixed", "write-heavy"):
            full = med([total(r) for r in mg.get((prof, cfg, "closed"), [])])[0]
            ex = med(
                [total(r) for r in mg.get((prof + "-nologin", cfg, "closed"), [])]
            )[0]
            if full is None or ex is None:
                cells += ["–", "–"]
                continue
            # profile = (1 - p) * nologin + p * login  =>  login = ex + (full - ex) / p
            p = LOGIN_PCT[prof] / 100
            cells += [
                f"{full - ex:+,.0f} ({(full / ex - 1) * 100:+.1f} %)",
                f"{ex + (full - ex) / p:,.0f}",
            ]
        print(f"| {cfg} | {' | '.join(cells)} |")


def scaling(rows):
    g = group(rows, ["workers", "mode"])
    print(
        "| workers | app µs/op | PG µs/op | Redis µs/op | total | rps (closed) "
        "| RSS MB/worker | p50 ms @400 | p99 ms @400 |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for w in (1, 2, 4):
        c, o = g.get((w, "closed"), []), g.get((w, "open"), [])
        print(
            f"| {w} (n={len(c)}/{len(o)}) | {fmt(med([r['server_cpu_us'] for r in c]))} "
            f"| {fmt(med([r['pg_cpu_us'] for r in c]))} | {fmt(med([r['redis_cpu_us'] for r in c]))} "
            f"| {fmt(med([total(r) for r in c]))} | {fmt(med([r['rps'] for r in c]))} "
            f"| {fmt(med([r['rss_mb_per_worker'] for r in c]))} "
            f"| {fmt(med([r['p50_ms'] for r in o]), 2)} | {fmt(med([r['p99_ms'] for r in o]), 1)} |"
        )


def _per(c):
    svcs = c[0]["per_service_cpu_us"].keys() if c else []
    return " · ".join(
        f"{s} {med([r['per_service_cpu_us'][s] for r in c])[0]:,.0f}" for s in svcs
    )


def topology(rows):
    g = group(rows, ["topology", "config", "mode"])
    print(
        "| topology | config | app µs/op (Σ processes) | per process | PG | Redis | total "
        "| rps (closed) | p50 ms @400 | p99 ms @400 | total @400 |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for t in ("mono", "split"):
        for cfg in ("planner_off", "planner_on"):
            c, o = g.get((t, cfg, "closed"), []), g.get((t, cfg, "open"), [])
            print(
                f"| {t} (n={len(c)}/{len(o)}) | {cfg} | {fmt(med([r['server_cpu_us'] for r in c]))} "
                f"| {_per(c) if t == 'split' else '–'} | {fmt(med([r['pg_cpu_us'] for r in c]))} "
                f"| {fmt(med([r['redis_cpu_us'] for r in c]))} | {fmt(med([total(r) for r in c]))} "
                f"| {fmt(med([r['rps'] for r in c]))} "
                f"| {fmt(med([r['p50_ms'] for r in o]), 2)} | {fmt(med([r['p99_ms'] for r in o]), 1)} "
                f"| {fmt(med([total(r) for r in o]))} |"
            )


def rpc(rows):
    g = group(rows, ["request", "topology", "config"])
    print(
        "| request | topology | config | app µs (Σ) | per process | PG | Redis | total | rps |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for req in ("subscription", "credits", "tickets", "org"):
        for t in ("mono", "split"):
            for cfg in ("planner_off", "planner_on"):
                c = g.get((req, t, cfg), [])
                if not c:
                    continue
                print(
                    f"| {req} | {t} | {cfg} (n={len(c)}) | {fmt(med([r['server_cpu_us'] for r in c]))} "
                    f"| {_per(c) if t == 'split' else '–'} | {fmt(med([r['pg_cpu_us'] for r in c]))} "
                    f"| {fmt(med([r['redis_cpu_us'] for r in c]))} | {fmt(med([total(r) for r in c]))} "
                    f"| {fmt(med([r['rps'] for r in c]))} |"
                )


if __name__ == "__main__":
    {
        "validity": validity,
        "matrix": matrix,
        "login": login,
        "scaling": scaling,
        "topology": topology,
        "rpc": rpc,
    }[sys.argv[1]](load(sys.argv[2:]))
