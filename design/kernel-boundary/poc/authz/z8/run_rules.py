"""Run the static rules on the Z-8 fixture consumer (base or candidate kernel).

    PYTHONPATH=app python run_rules.py
"""

from pathlib import Path

import sandloom.core.archcheck.api10_rules as api10
import sandloom.core.archcheck.module_rules as mr

api = Path(__file__).resolve().parent / "app/z8app/modules/reports/api.py"
print("guard-order (gate before TenantScope):")
for _path, hit in mr.find_guard_order_problems([api]):
    print("  ", hit)
print("account-route-mismatch (C027):")
for hit in api10.find_account_route_mismatches(api):
    print("  ", hit[:150])
print("untagged-sensitive (C022):")
for hit in api10.find_untagged_sensitive(api):
    print("  ", hit[:150])
rule = getattr(api10, "find_handwritten_access_checks", None)
print("handwritten-access-check (C032):", "" if rule else "rule absent")
for hit in rule(api) if rule else []:
    print("  ", hit)
