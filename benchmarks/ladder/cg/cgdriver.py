"""Run INSIDE the cachegrind container: instructions per op for each rung.

    python /w/benchmarks/ladder/cg/cgdriver.py L0 L2 L3 ...

Ir/op = (Ir(K ops) - Ir(0 ops)) / K. Both runs do the same imports, setup,
20 warm-up ops and teardown (``ladder.py cg``), so all of that cancels out.
"""

import json
import os
import re
import subprocess
import sys

LADDER = "/w/benchmarks/ladder/ladder.py"
K = int(os.environ.get("CG_K", "200"))
#: CG_ANNOTATE=1: also write a per-C-function Ir/op diff (K ops minus 0 ops)
#: to /out/<rung>.annot (cg_diff + cg_annotate).
ANNOTATE = os.environ.get("CG_ANNOTATE") == "1"
#: CG_CACHESIM=1: simulate caches and also report I1/D1/LL misses per op.
CACHESIM = os.environ.get("CG_CACHESIM") == "1"
MISSES: dict[str, list[int]] = {}


def ir(rung: str, n: int) -> int:
    out = f"/out/{rung}.{n}.cg" if ANNOTATE else "/dev/null"
    r = subprocess.run(
        [
            "valgrind",
            "--tool=cachegrind",
            "--cache-sim=yes" if CACHESIM else "--cache-sim=no",
            f"--cachegrind-out-file={out}",
            sys.executable,
            LADDER,
            "cg",
            rung,
            "--n",
            str(n),
        ],
        capture_output=True,
        text=True,
        cwd="/tmp",
        env=dict(os.environ, PYTHONHASHSEED="0"),
    )
    if CACHESIM:
        for key in ("I1  misses", "D1  misses", "LLd misses", "LLi misses", "D   refs"):
            mm = re.search(key.replace(" ", r"\s+") + r":\s+([\d,]+)", r.stderr)
            if mm:
                MISSES.setdefault(f"{rung}:{key}", []).append(
                    int(mm.group(1).replace(",", ""))
                )
    m = re.search(r"I\s+refs:\s+([\d,]+)", r.stderr)
    if not m or r.returncode != 0:
        raise RuntimeError(r.stderr[-3000:])
    return int(m.group(1).replace(",", ""))


res = {}
for rung in sys.argv[1:]:
    try:
        a, z = ir(rung, 0), ir(rung, K)
        res[rung] = (z - a) / K
        if ANNOTATE:
            with open(f"/out/{rung}.diff", "w") as f:
                subprocess.run(
                    ["cg_diff", f"/out/{rung}.0.cg", f"/out/{rung}.{K}.cg"],
                    stdout=f,
                    check=True,
                )
            with open(f"/out/{rung}.annot", "w") as f:
                subprocess.run(
                    ["cg_annotate", "--auto=no", f"/out/{rung}.diff"],
                    stdout=f,
                    check=False,
                )
        print(f"{rung:6s} {res[rung]:12,.0f} instr/op", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"{rung:6s} ERROR {str(e)[-1500:]}", flush=True)
if CACHESIM:
    per_op = {k: (v[1] - v[0]) / K for k, v in MISSES.items() if len(v) == 2}
    for k, v in sorted(per_op.items()):
        print(f"{k:28s} {v:12,.0f} /op")
    print("MISSJSON " + json.dumps(per_op))
print("JSON " + json.dumps(res))
