"""Tables of ``cg_matrix.py`` results (instructions per request).

    python ladder/agg_cg.py routeplan <cg_routeplan.jsonl> [<repeat.jsonl> ...]
    python ladder/agg_cg.py release <cg_release.jsonl> [<repeat.jsonl> ...]
    python ladder/agg_cg.py memray <memray_routeplan.jsonl>

Mix-weighted numbers use the FIXED profiles of ``benchsuite.mix.PROFILES``
without the login row (``mixed-nologin``, ``read-heavy``,
``write-heavy-nologin``): Σ wᵢ·Irᵢ / Σ wᵢ. µs figures are ESTIMATES at the
ladder's documented 0.197 µs per 1 000 instructions (L6 at conc 64, M1 Max:
445 µs / 2.26 M instr) — the app's average, not a measurement; requests with
more I/O run at lower IPC (RESULTS.md, "Latency vs CPU").
Several result lines for one job (repeats) are averaged.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from benchsuite import mix  # noqa: E402

US_PER_KINSTR = 0.197


def profiles() -> dict[str, dict[str, float]]:
    out = {}
    for name in ("mixed", "read-heavy", "write-heavy"):
        w = {k: v for k, v in mix.PROFILES[name].items() if k != "login"}
        out[name if name == "read-heavy" else f"{name}-nologin"] = w
    return out


def load(paths: list[str]) -> dict[tuple[str, str], dict]:
    acc: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for line in (x for p in paths for x in open(p)):
        r = json.loads(line)
        if r.get("ir_per_op") is not None:
            acc[(r["lane"], r["job"])].append(r)
    out = {}
    for k, rs in acc.items():
        out[k] = {
            "ir": statistics.mean(r["ir_per_op"] for r in rs),
            "spread": max(r["ir_per_op"] for r in rs) - min(r["ir_per_op"] for r in rs),
            "n": len(rs),
            "rpc": statistics.mean(r["rpc_per_op"] for r in rs)
            if "rpc_per_op" in rs[0]
            else None,
            "commit": rs[0].get("commit"),
        }
    return out


def k(x: float) -> str:
    return f"{x / 1e3:,.0f}"


def pct(a: float, b: float) -> str:
    return f"{(b - a) / a * 100:+.1f} %" if a else "–"


def weighted(ir: dict[str, float], w: dict[str, float]) -> float:
    return sum(ir[key] * wt for key, wt in w.items()) / sum(w.values())


def routeplan(paths: list[str]) -> None:
    d = load(paths)
    print("### S5 (instructions per request, k)\n")
    print("| base | variant | plans off | plans on | Δ | Δ % | Δ ≈ µs (est.) |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for base in ("old", "new"):
        for v in ("uow", "planopt"):
            a = d.get(("s5", f"{base}/s5_{v}/plans=false"))
            b = d.get(("s5", f"{base}/s5_{v}/plans=true"))
            if a and b:
                dd = b["ir"] - a["ir"]
                print(
                    f"| {base} ({a['commit']}) | {v} | {k(a['ir'])} | {k(b['ir'])} | "
                    f"{k(dd)} | {pct(a['ir'], b['ir'])} | {dd / 1e3 * US_PER_KINSTR:+.0f} |"
                )
    for base in ("old", "new"):
        rows = {}
        print(
            f"\n### Monolith, every login-free mix request — lane `{base}` "
            f"(instructions per request, k)\n"
        )
        print("| request | plans off | plans on | Δ | Δ % |")
        print("|---|---:|---:|---:|---:|")
        for key in mix.CATALOG:
            a = d.get((base, f"mono/{key}/plans=false"))
            b = d.get((base, f"mono/{key}/plans=true"))
            if not (a and b):
                continue
            rows[key] = (a["ir"], b["ir"])
            print(
                f"| {key} | {k(a['ir'])} | {k(b['ir'])} | {k(b['ir'] - a['ir'])} | "
                f"{pct(a['ir'], b['ir'])} |"
            )
        print(f"\n| mix ({base}) | plans off | plans on | Δ | Δ % | Δ ≈ µs (est.) |")
        print("|---|---:|---:|---:|---:|---:|")
        for name, w in profiles().items():
            if not all(x in rows for x in w):
                continue
            off = weighted({x: rows[x][0] for x in rows}, w)
            on = weighted({x: rows[x][1] for x in rows}, w)
            print(
                f"| {name} | {k(off)} | {k(on)} | {k(on - off)} | {pct(off, on)} | "
                f"{(on - off) / 1e3 * US_PER_KINSTR:+.0f} |"
            )


def release(paths: list[str]) -> None:
    d = load(paths)
    commits = {lane: v["commit"] for (lane, _j), v in d.items()}
    print(f"builds: prev = {commits.get('prev')}, rel = {commits.get('rel')}\n")
    mono: dict[str, dict[str, float]] = {"prev": {}, "rel": {}}
    split: dict[str, dict[str, float]] = {"prev": {}, "rel": {}}
    detail: dict[str, dict[str, tuple]] = {"prev": {}, "rel": {}}
    for b in ("prev", "rel"):
        for key in mix.CATALOG:
            m = d.get((b, f"mono/{key}"))
            if m:
                mono[b][key] = m["ir"]
            c = d.get((f"{b}_split", f"split/{key}/caller"))
            if not c:
                continue
            prov, rpc = 0.0, c["rpc"] or 0.0
            for svc in ("apps", "billing"):
                p = d.get((f"{b}_split", f"split/{key}@{svc}/provider"))
                h = d.get((f"{b}_split", f"split/health@{svc}/provider"))
                if p and h:
                    prov = p["ir"] - h["ir"]
            split[b][key] = c["ir"] + prov
            detail[b][key] = (c["ir"], prov, rpc)
    print("### Monolith (instructions per request, k)\n")
    print("| request | prev | rel | Δ | Δ % |")
    print("|---|---:|---:|---:|---:|")
    for key in mix.CATALOG:
        if key in mono["prev"] and key in mono["rel"]:
            a, b = mono["prev"][key], mono["rel"][key]
            print(f"| {key} | {k(a)} | {k(b)} | {k(b - a)} | {pct(a, b)} |")
    print("\n### Split: caller (service owning the route) + provider (tenancy), k\n")
    print(
        "| request | RPC calls/req | caller prev → rel | provider prev → rel | "
        "split total prev → rel | Δ total | split − mono (rel) |"
    )
    print("|---|---:|---|---|---|---:|---:|")
    for key in mix.CATALOG:
        if key in detail["prev"] and key in detail["rel"]:
            cp, pp, rp = detail["prev"][key]
            cr, pr, rr = detail["rel"][key]
            prem = split["rel"][key] - mono["rel"].get(key, float("nan"))
            print(
                f"| {key} | {rp:g} / {rr:g} | {k(cp)} → {k(cr)} | {k(pp)} → {k(pr)} | "
                f"{k(split['prev'][key])} → {k(split['rel'][key])} | "
                f"{pct(split['prev'][key], split['rel'][key])} | "
                f"{k(prem)} ({pct(mono['rel'][key], split['rel'][key])}) |"
            )
    print("\n### Mixes (weighted, k instructions per request; µs = estimate)\n")
    print("| mix | topology | prev | rel | Δ | Δ % | Δ ≈ µs (est.) |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for name, w in profiles().items():
        for topo, src in (("mono", mono), ("split", split)):
            if all(x in src["prev"] and x in src["rel"] for x in w):
                a, b = weighted(src["prev"], w), weighted(src["rel"], w)
                print(
                    f"| {name} | {topo} | {k(a)} | {k(b)} | {k(b - a)} | {pct(a, b)} | "
                    f"{(b - a) / 1e3 * US_PER_KINSTR:+.0f} |"
                )
    print("\n### Split premium in instructions (rel)\n")
    print("| mix | mono | split | premium |")
    print("|---|---:|---:|---:|")
    for name, w in profiles().items():
        if all(x in mono["rel"] and x in split["rel"] for x in w):
            a, b = weighted(mono["rel"], w), weighted(split["rel"], w)
            print(f"| {name} | {k(a)} | {k(b)} | {k(b - a)} ({pct(a, b)}) |")


def memray(paths: list[str]) -> None:
    """``mem.sh``-style lines: {"base", "scenario", "plans", "res": ladder memray}."""
    a: dict[tuple[str, str, str], float] = {}
    for line in (x for p in paths for x in open(p)):
        r = json.loads(line)
        if r.get("res"):
            a[(r["base"], r["scenario"], r["plans"])] = r["res"]["allocs_per_op"]
    for base in sorted({x[0] for x in a}):
        print(f"\n### Allocations per request (memray), lane `{base}`\n")
        print("| request | plans off | plans on | Δ | Δ % |")
        print("|---|---:|---:|---:|---:|")
        rows = {}
        for sc in ["s5_uow", "s5_planopt", *mix.CATALOG]:
            off, on = a.get((base, sc, "false")), a.get((base, sc, "true"))
            if off is None or on is None:
                continue
            rows[sc] = (off, on)
            print(
                f"| {sc} | {off:,.0f} | {on:,.0f} | {on - off:+,.0f} | {pct(off, on)} |"
            )
        for name, w in profiles().items():
            if all(x in rows for x in w):
                off = weighted({x: rows[x][0] for x in rows}, w)
                on = weighted({x: rows[x][1] for x in rows}, w)
                print(
                    f"| **{name}** (weighted) | {off:,.0f} | {on:,.0f} | "
                    f"{on - off:+,.0f} | {pct(off, on)} |"
                )


if __name__ == "__main__":
    {"routeplan": routeplan, "release": release, "memray": memray}[sys.argv[1]](
        sys.argv[2:]
    )
