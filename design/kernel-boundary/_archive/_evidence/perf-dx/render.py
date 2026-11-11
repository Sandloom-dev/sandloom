"""Render the stand's JSON results as the Markdown tables of perf-dx-baseline.md.

    python render.py results/          # reads counts_*.json, run_*.json, cg.txt
"""

from __future__ import annotations

import json
import re
import statistics as st
import sys
from pathlib import Path

ORDER = [
    "control",
    "role.admin",
    "role.user",
    "perm.admin",
    "subscribed",
    "subactive",
    "credits",
    "role_or_credits.admin",
    "role_or_credits.user",
    "orgrole",
    "orgrole_and_subactive",
    "orgrole_and_subactive.outsider",
    "bound.control",
    "bound.orgrole_and_subactive",
]


def _counts(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text())["windows"]


def _pg(w: dict) -> float:
    return sum(w["pg_statements_per_req"].values())


def _redis(w: dict) -> float:
    return sum(w["redis_commands_per_req"].values())


def counts_table(d: Path) -> str:
    mono, split = _counts(d / "counts_mono.json"), _counts(d / "counts_split.json")
    rows = [
        "| case | mono DB stmts | mono Redis | split RPC | split DB (provider) "
        "| split Redis | first request, split: extra RPC |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in ORDER:
        m, s, sf = mono[f"{c} steady"], split[f"{c} steady"], split[f"{c} first"]
        extra = sf["rpc_requests_per_req"] - s["rpc_requests_per_req"]
        first = ", ".join(x for x in sf["rpc_sample"] if x.startswith("GET") or "describe" in x)
        rows.append(
            f"| {c} | {_pg(m):g} | {_redis(m):g} | {s['rpc_requests_per_req']:g} "
            f"| {_pg(s):g} | {_redis(s):g} | {extra:g} {first} |"
        )
    return "\n".join(rows)


def _med(rows: list[dict], key: str) -> tuple[float, float, float] | None:
    xs = [r[key] for r in rows if key in r]
    return (st.median(xs), min(xs), max(xs)) if xs else None


def run_table(path: Path) -> str:
    doc = json.loads(path.read_text())
    res = doc["results"]
    ctl = _med(res["control"], "instr_k")
    head = (
        "| case | p50 µs | p95 µs | p99 µs | instr k/req (this process) "
        "| Δ instr k vs control | provider instr k/req |"
    )
    rows = [head, "|---|---|---|---|---|---|---|"]
    for c in ORDER:
        r = res[c]
        p50, p95, p99 = (_med(r, k) for k in ("p50_us", "p95_us", "p99_us"))
        ins = _med(r, "instr_k")
        prov = _med(r, "provider_instr_k")
        base = _med(res["bound.control"], "instr_k") if c.startswith("bound.") else ctl
        delta = f"{ins[0] - base[0]:+.0f}" if ins and base else "-"
        rows.append(
            f"| {c} | {p50[0]:.0f} | {p95[0]:.0f} | {p99[0]:.0f} "
            f"| {ins[0]:.0f} ({ins[1]:.0f}–{ins[2]:.0f}) | {delta} "
            f"| {f'{prov[0]:.0f}' if prov else '-'} |"
        )
    return "\n".join(rows)


def cg_table(path: Path) -> str:
    runs: dict[str, list[int]] = {}
    for line in path.read_text().splitlines():
        m = re.match(r"(\S+) (\d+) instr/op", line)
        if m:
            runs.setdefault(m.group(1), []).append(int(m.group(2)))
    ctl = st.median(runs["control"])
    bctl = st.median(runs.get("bound.control", [ctl]))
    rows = ["| case | Ir/req (cachegrind) | runs | Δ vs control |", "|---|---|---|---|"]
    for c in ORDER:
        if c not in runs:
            continue
        v = st.median(runs[c])
        base = bctl if c.startswith("bound.") else ctl
        rows.append(
            f"| {c} | {v:,.0f} | {', '.join(f'{x:,}' for x in runs[c])} | {v - base:+,.0f} |"
        )
    return "\n".join(rows)


def main() -> None:
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "results")
    print("## Counts\n\n" + counts_table(d) + "\n")
    for name in sorted(d.glob("run*.json")):
        print(f"## {name.name}\n\n" + run_table(name) + "\n")
    if (d / "cg.txt").exists():
        print("## cachegrind\n\n" + cg_table(d / "cg.txt") + "\n")


if __name__ == "__main__":
    main()
