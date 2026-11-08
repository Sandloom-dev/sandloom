"""Side-by-side steady-state counts of ``stand.py counts`` outputs (kb-poc-authz).

    python render_counts.py results/counts_authzstand_mono.json results/counts_authzpa2_mono.json ...

Per case: DB round trips per database role (kb_consumer = the measuring
process, kb_provider = the split provider), Redis commands, RPC requests;
``first`` = the case's first request, ``steady`` = 20 requests after warm-up.
"""

import json
import sys
from pathlib import Path


def cell(w: dict) -> str:
    # only the stand's two roles: another client of the same server (a test
    # suite under the superuser) is not the request's work
    pg = {k: v for k, v in w["pg_statements_per_req"].items() if k.startswith("kb_")}
    db = "/".join(f"{v:g}" for _, v in sorted(pg.items())) or "0"
    who = "+".join(k.replace("kb_", "") for k in sorted(pg)) or "-"
    redis = sum(w["redis_commands_per_req"].values())
    return f"{db} ({who}) r{redis:g} rpc{w['rpc_requests_per_req']:g}"


docs = [(Path(p).stem.replace("counts_", ""), json.load(open(p))["windows"]) for p in sys.argv[1:]]
cases = [k.rsplit(" ", 1)[0] for k in docs[0][1] if k.endswith(" steady")]
phase = "steady"
print("| case | " + " | ".join(n for n, _ in docs) + " | equal |")
print("|---|" + "---|" * (len(docs) + 1))
for case in cases:
    cells = [cell(d[f"{case} {phase}"]) if f"{case} {phase}" in d else "n/a" for _, d in docs]
    print(f"| {case} | " + " | ".join(cells) + f" | {'yes' if len(set(cells)) == 1 else 'NO'} |")
first = [
    (case, [d[f"{case} first"]["rpc_requests_per_req"] for _, d in docs if f"{case} first" in d])
    for case in cases
]
print("\nfirst request, RPC: " + ", ".join(f"{c} {'/'.join(f'{x:g}' for x in v)}" for c, v in first if any(v)))
