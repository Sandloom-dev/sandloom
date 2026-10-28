"""Summarise ``ladder/gc_stand.py`` results: one Markdown table per window.

    uv run python ladder/gc_stand_report.py results/gc_stand.jsonl [--label X]

Cells are the MEDIAN over repetitions (min..max in brackets for p99) of
client p50/p99/p99.9, collections per generation, GC thread-CPU per gen2 /
gen1 collection, the largest gen2 wall pause, GC share of worker CPU and
worker CPU per OK request. On a shared host prefer the CPU columns: wall
pauses include the OS descheduling the worker mid-collection.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from typing import Any


def _med(xs: list[float]) -> float:
    return statistics.median(xs) if xs else float("nan")


def _per(r: dict[str, Any], gen: str) -> float:
    """GC thread-CPU per collection (ms): descheduling-proof, unlike wall."""
    n = r[gen]["n"]
    return 1e3 * r[gen]["cpu_s"] / n if n else 0.0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="+")
    p.add_argument("--label", action="append", default=[])
    a = p.parse_args()
    rows: list[dict[str, Any]] = []
    for f in a.files:
        with open(f) as fh:
            rows.extend(json.loads(line) for line in fh if line.strip())
    if a.label:
        rows = [r for r in rows if r.get("label") in a.label]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for r in rows:
        groups[(r["window"], r["variant"])].append(r)
        if r["variant"] not in order:
            order.append(r["variant"])
    for window in ("plan", "s5", "f", "soak"):
        present = [v for v in order if (window, v) in groups]
        if not present:
            continue
        rate = groups[(window, present[0])][0]["rate"]
        print(f"\n### `{window}` @ {rate:.0f}/s\n")
        print(
            "| variant | n | p50 ms | p99 ms (min..max) | p99.9 ms | gen0 | gen1 "
            "| gen2 | gen2 CPU ms/coll | gen1 CPU ms/coll | gen2 wall max ms "
            "| GC % CPU | GC CPU µs/OK | CPU µs/OK | err |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for v in present:
            g = groups[(window, v)]
            p99 = [r["p99_ms"] for r in g]
            g2max = [r["gen2"]["pause_max_ms"] or 0 for r in g]
            g2cpu = [_per(r, "gen2") for r in g]
            g1cpu = [_per(r, "gen1") for r in g]
            print(
                f"| {v} | {len(g)} | {_med([r['p50_ms'] for r in g]):.1f} "
                f"| {_med(p99):.1f} ({min(p99):.0f}..{max(p99):.0f}) "
                f"| {_med([r['p999_ms'] for r in g]):.1f} "
                f"| {_med([r['gen0']['n'] for r in g]):.0f} "
                f"| {_med([r['gen1']['n'] for r in g]):.0f} "
                f"| {_med([r['gen2']['n'] for r in g]):.0f} "
                f"| {_med(g2cpu):.2f} | {_med(g1cpu):.2f} | {_med(g2max):.1f} "
                f"| {100 * _med([r['gc_share_of_cpu'] or 0 for r in g]):.1f} "
                f"| {_med([r['gc_cpu_us_per_ok'] for r in g]):.0f} "
                f"| {_med([r['cpu_us_per_ok'] for r in g]):.0f} "
                f"| {sum(r['errors'] for r in g)} |"
            )


if __name__ == "__main__":
    main()
