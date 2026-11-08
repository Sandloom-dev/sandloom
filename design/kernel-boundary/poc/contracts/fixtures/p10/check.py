"""kb-poc-contracts proof 10: two contracts whose stub file names collide."""

import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
import sandloom.core.cli.consumer_copies as cc  # noqa: E402
import sandloom.core.cli.generated as gen  # noqa: E402

print("stub_names('a.b_c.v1'):", gen.stub_names("a.b_c.v1"))
print("stub_names('a_b.c.v1'):", gen.stub_names("a_b.c.v1"))
print("1) module distribution `consumer` imports _generated.contracts.a_b_c_v1:")
for f in cc.module_files("consumer", root / "consumer", root, ("sandloom.shared",), lambda t, p: t):
    for problem in f.problems:
        print(f"   {f.path.relative_to(root)}: error[{f.code}]: {problem}")
print("2) application shared package, both providers composed (plan() check):")
print("  ", cc.stub_collisions(["a.b_c.v1", "a_b.c.v1", "tenancy.membership.v1"]))
print("3) written:", sorted(str(p.relative_to(root)) for p in (root / "consumer").rglob("_generated/**/*")) or "nothing")
