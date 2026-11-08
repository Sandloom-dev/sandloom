"""Compare two ``stand.py decisions`` outputs case by case (kb-poc-authz).

    python compare.py results/dec_base_split.json results/dec_pa2_split.json

Equal = same status, same denial code and the same provider calls in the
same order; ``get_credit_balance`` and ``has_credits_at_least`` count as the
same call (answer #24: Credits moves onto the boolean read).
"""

import json
import sys


def norm(run: dict) -> tuple:
    body = run["body"]
    code = body.get("code") if isinstance(body, dict) else None
    rpc = [x.replace("has_credits_at_least", "get_credit_balance") for x in run["rpc"]]
    return run["status"], code, rpc


a = json.load(open(sys.argv[1]))["cases"]
b = json.load(open(sys.argv[2]))["cases"]
diff = 0
for case in [k for k in a if not k.startswith("_")]:
    if case not in b:
        print("MISSING", case)
        continue
    ra = [norm(r) for r in a[case]["runs"]]
    rb = [norm(r) for r in b[case]["runs"]]
    same = ra == rb
    diff += not same
    print("equal" if same else "DIFF ", case, ra[-1][:2], "" if same else (ra, rb))
n = len([k for k in a if not k.startswith("_")])
print(f"{n - diff}/{n} cases equal")
