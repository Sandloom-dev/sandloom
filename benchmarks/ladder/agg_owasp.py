"""Tables for ``run_owasp.py`` results (median of rounds, min–max).

    python ladder/agg_owasp.py provider|login|nologin <jsonl>
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict


def _load(path: str) -> list[dict]:
    recs = [json.loads(x) for x in open(path)]
    return [r for r in recs if not r.get("invalid") and not r.get("superseded")]


def _m(xs: list[float]) -> str:
    xs = [x for x in xs if x is not None]
    if not xs:
        return "–"
    med = statistics.median(xs)
    if len(xs) == 1 or max(xs) - min(xs) < 0.5:
        return f"{med:,.0f}"
    return f"{med:,.0f} ({min(xs):,.0f}–{max(xs):,.0f})"


def provider(path: str) -> None:
    g: dict = defaultdict(list)
    for r in _load(path):
        g[(r["single"], r["transport"], r["warmup_conc"])].append(r)
    print("| request | cell | caller µs/op | tenancy µs/op (user / sys) | tenancy vcs/op"
          " | PG µs/op | Redis µs/op | conns to tenancy | p50 / p99 ms | n |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for (single, tr, wc), rs in sorted(g.items()):
        caller = "apps" if single == "tickets" else "billing"
        cu = [r["per_op"][caller]["user"] + r["per_op"][caller]["sys"] for r in rs]
        tu = [r["per_op"]["tenancy"]["user"] for r in rs]
        ts = [r["per_op"]["tenancy"]["sys"] for r in rs]
        tt = [a + b for a, b in zip(tu, ts, strict=True)]
        vcs = [r["per_op"]["tenancy"]["vcs"] for r in rs]
        conns = sorted(r["conns_after"]["tenancy"] for r in rs)
        p50 = statistics.median(r["p50_ms"] for r in rs)
        p99 = statistics.median(r["p99_ms"] for r in rs)
        print(f"| {single} | {tr} w{wc} | {_m(cu)} | {_m(tt)} ({statistics.median(tu):.0f} /"
              f" {statistics.median(ts):.0f}) | {statistics.median(vcs):.2f} |"
              f" {_m([r['pg_cpu_us'] for r in rs])} | {_m([r['redis_cpu_us'] for r in rs])}"
              f" | {conns} | {p50:.2f} / {p99:.2f} | {len(rs)} |")


def provider_cycles(path: str) -> None:
    """Instructions / cycles / effective clock per op (runs with rusage)."""
    g: dict = defaultdict(list)
    for r in _load(path):
        if r.get("rusage_per_op"):
            g[(r["single"], r["transport"], r["warmup_conc"])].append(r)
    print("| request | cell | tenancy kinstr / kcycles per op | tenancy GHz | tenancy µs/op"
          " | caller kinstr / kcycles | caller GHz | caller interrupt wakeups/op | n |")
    print("|---|---|---|---|---|---|---|---|---|")
    med = statistics.median
    for (single, tr, wc), rs in sorted(g.items()):
        caller = "apps" if single == "tickets" else "billing"

        def f(n: str, k: str, rs: list = rs) -> float:
            return med(r["rusage_per_op"][n][k] for r in rs)

        def ghz(n: str, rs: list = rs) -> float:
            return med(r["rusage_per_op"][n]["cycles"] / (r["rusage_per_op"][n]["user_time"]
                       + r["rusage_per_op"][n]["system_time"]) / 1e3 for r in rs)

        tus = med(r["rusage_per_op"]["tenancy"]["user_time"]
                  + r["rusage_per_op"]["tenancy"]["system_time"] for r in rs)
        print(f"| {single} | {tr} w{wc} | {f('tenancy', 'instructions') / 1e3:,.0f} /"
              f" {f('tenancy', 'cycles') / 1e3:,.0f} | {ghz('tenancy'):.2f} | {tus:.0f} |"
              f" {f(caller, 'instructions') / 1e3:,.0f} / {f(caller, 'cycles') / 1e3:,.0f} |"
              f" {ghz(caller):.2f} | {f(caller, 'interrupt_wkups'):.1f} | {len(rs)} |")


def _app_cycles(r: dict) -> tuple[float, float] | None:
    """(Mcycles, Minstr) per op summed over the app processes (median rep)."""
    reps = r.get("rusage_per_op_all") or ([r["rusage_per_op"]] if r.get("rusage_per_op") else [])
    reps = [x for x in reps if x]
    if not reps:
        return None
    cyc = statistics.median(sum(v["cycles_k"] for v in x.values()) for x in reps)
    ins = statistics.median(sum(v["instr_k"] for v in x.values()) for x in reps)
    return cyc / 1e3, ins / 1e3


def _table(path: str, key: str) -> None:
    g: dict = defaultdict(list)
    recs = _load(path)
    for r in recs:
        g[(r["profile"], r[key], r["mode"])].append(r)
    print(f"| profile | {key} | mode | app µs/op | PG | Redis | total µs/op |"
          " app Mcycles / Minstr per op | rps | p50 / p99 ms | n |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for (prof, k, mode), rs in sorted(g.items()):
        app = [r["server_cpu_us"] for r in rs]
        pg = [r["pg_cpu_us"] for r in rs]
        rd = [r["redis_cpu_us"] for r in rs]
        tot = [a + b + c for a, b, c in zip(app, pg, rd, strict=True)]
        rps = _m([r.get("rps") for r in rs]) if mode == "closed" else "–"
        lat = (f"{statistics.median(r['p50_ms'] for r in rs):.2f} / "
               f"{statistics.median(r['p99_ms'] for r in rs):.1f}") if mode == "open" else "–"
        extra = ""
        if "per_service_cpu_us" in rs[0] and len(rs[0]["per_service_cpu_us"]) > 1:
            extra = " · " + " ".join(
                f"{n} {statistics.median(r['per_service_cpu_us'][n] for r in rs):.0f}"
                for n in rs[0]["per_service_cpu_us"])
        cy = [c for c in (_app_cycles(r) for r in rs) if c]
        cyc = (f"{statistics.median(c[0] for c in cy):.2f} / "
               f"{statistics.median(c[1] for c in cy):.2f}") if cy else "–"
        print(f"| {prof} | {k} | {mode} | {_m(app)}{extra} | {_m(pg)} | {_m(rd)} |"
              f" **{_m(tot)}** | {cyc} | {rps} | {lat} | {len(rs)} |")
    bad = [json.loads(x) for x in open(path)]
    inv = [r for r in bad if r.get("invalid")]
    print(f"\nruns: {len(bad)}, invalid: {len(inv)}, errors: "
          f"{sum(r.get('errors_total') or 0 for r in bad)}, rehashed during run: "
          f"{sum(bool(r.get('rehashed_during_run')) for r in bad)}, prefixes: "
          f"{sorted({(r.get('hash_profile'), tuple(r.get('hash_prefixes', []))) for r in bad})}")
    for r in inv:
        print("  invalid:", r.get("profile"), r.get(key), r.get("mode"), r.get("invalid_reasons"))


if __name__ == "__main__":
    sec, path = sys.argv[1], sys.argv[2]
    if sec == "provider":
        provider(path)
    elif sec == "provider-cycles":
        provider_cycles(path)
    elif sec == "login":
        _table(path, "hash_profile")
    else:
        _table(path, "topology")
