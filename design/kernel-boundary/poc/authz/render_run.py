"""Medians of ``stand.py run`` outputs, base vs candidate (kb-poc-authz).

    python render_run.py results/run_authzstand_mono_1.json results/run_authzpa2_mono_1.json ...

Per case and file: retired instructions of the measuring process (k/req;
split: the consumer) and of the split provider, p50 / p95 / p99 latency (µs),
each the median over the file's repetitions.
"""

import json
import statistics
import sys
from pathlib import Path

docs = [(Path(p).stem.replace("run_", ""), json.load(open(p))["results"]) for p in sys.argv[1:]]
cases = list(docs[0][1])


def med(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if key in r]
    return statistics.median(vals) if vals else None


print("| case | run | instr k (proc) | provider k | p50 | p95 | p99 |")
print("|---|---|---|---|---|---|---|")
for case in cases:
    for name, res in docs:
        rows = res.get(case)
        if not rows:
            continue
        cells = [med(rows, k) for k in ("instr_k", "provider_instr_k", "p50_us", "p95_us", "p99_us")]
        txt = ["-" if c is None else f"{c:,.0f}" for c in cells]
        print(f"| {case} | {name} | " + " | ".join(txt) + " |")
