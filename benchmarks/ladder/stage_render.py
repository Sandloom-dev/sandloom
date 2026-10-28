"""Tables from ``stage_ab.py`` results: medians over rounds, per-change deltas.

    uv run python ladder/stage_render.py [socket|latency] [results file]
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def socket(path: str = "results/stage_socket.jsonl") -> None:
    rows: dict[tuple[str, str], list[dict]] = defaultdict(list)
    stages: list[str] = []
    for line in (HERE / path).read_text().splitlines():
        r = json.loads(line)
        rows[(r["variant"], r["stage"])].append(r)
        if r["stage"] not in stages:
            stages.append(r["stage"])
    variants = list(dict.fromkeys(v for v, _ in rows))
    print("| variant | " + " | ".join(f"{s} CPU µs (rounds) | {s} rps" for s in stages)
          + " | Δ change 1 | Δ change 2 |")
    print("|---" * (2 * len(stages) + 3) + "|")
    for v in variants:
        cells, med = [], {}
        for s in stages:
            rs = rows.get((v, s), [])
            cpus = [r["server_cpu_us"] for r in rs]
            med[s] = statistics.median(cpus) if cpus else float("nan")
            rps = statistics.median([r["rps"] for r in rs]) if rs else float("nan")
            cells += [f"**{med[s]:.0f}** ({', '.join(f'{c:.0f}' for c in cpus)})", f"{rps:,.0f}"]
        d1 = med[stages[1]] - med[stages[0]] if len(stages) > 1 else 0
        d2 = med[stages[2]] - med[stages[1]] if len(stages) > 2 else 0
        print(f"| {v} | " + " | ".join(cells) + f" | {d1:+.0f} | {d2:+.0f} |")


def latency(path: str = "results/stage_latency.jsonl") -> None:
    rows: dict[tuple[str, float, str], list[dict]] = defaultdict(list)
    for line in (HERE / path).read_text().splitlines():
        r = json.loads(line)
        rows[(r["variant"], r["rate"], r["stage"])].append(r)
    print("| variant | rate | stage | achieved | p50 ms | p99 ms (runs) | p99.9 ms | app CPU µs/req |")
    print("|---|---|---|---|---|---|---|---|")
    for (v, rate, s), rs in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        m = lambda k: statistics.median([r[k] for r in rs])  # noqa: E731
        p99s = ", ".join(f"{r['p99_ms']:.1f}" for r in rs)
        print(f"| {v} | {rate:.0f} | {s} | {m('achieved_rps'):.0f} | {m('p50_ms'):.2f} | "
              f"{m('p99_ms'):.1f} ({p99s}) | {m('p999_ms'):.1f} | {m('app_cpu_us_per_req'):.0f} |")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "socket"
    fn = {"socket": socket, "latency": latency}[mode]
    fn(*sys.argv[2:3])
