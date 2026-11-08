"""kb-poc-contracts proof 6: `sandloom generate` where the provider of a
consumed contract (sandloom_tenancy) is NOT importable.

    uv run --no-sync python p6_gen_without_provider.py [--check]

A meta-path finder makes ``sandloom_tenancy`` unimportable (as if the wheel
were not installed), the entry-point cache forgets it, and the composition
leaves it out; then the real ``sandloom generate`` entry runs.
"""

import importlib.abc
import os
import sys


class _Absent(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):  # noqa: ANN001
        if name == "sandloom_tenancy" or name.startswith("sandloom_tenancy."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, _Absent())
os.environ["SANDLOOM_MODULES"] = '["identity","billing"]'
import sandloom.core.descriptors.module_names as mn  # noqa: E402

_real = mn.installed_modules
mn.installed_modules = lambda: tuple(m for m in _real() if m.name != "tenancy")  # type: ignore[assignment]
import sandloom.core.cli.consumer_copies as cc  # noqa: E402

cc.installed_modules = mn.installed_modules  # type: ignore[assignment]
_pkgs = cc.provider_packages
cc.provider_packages = lambda root: [p for p in _pkgs(root) if p[0] != "sandloom_tenancy"]  # type: ignore[assignment]
from sandloom.core.cli.generated import main  # noqa: E402

print("importable sandloom_tenancy:", end=" ")
try:
    __import__("sandloom_tenancy")
    print("yes")
except ModuleNotFoundError:
    print("no")
raise SystemExit(main(sys.argv[1:]))
