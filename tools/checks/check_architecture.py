"""Import-boundary manifest check for the framework tree.

Thin wrapper over :mod:`sandloom.core.archcheck` (the same library ``sandloom check``
runs for applications): validates every non-core, non-test, non-migration file
under ``src/sandloom`` against ``architecture_rules.yml`` — ``sandloom.core.*`` imports
outside ``public_api`` (unless allowlisted), forbidden patterns, and the
optional kernel layering.

Usage (from the repository root)::

    uv run python tools/checks/check_architecture.py

The full rule set (module boundaries, ORM leakage, ...) runs with
``uv run sandloom check`` or the pytest suite under ``tests/architecture``.

The archcheck package is loaded straight from ``src/`` WITHOUT executing
``sandloom.core/__init__`` (which builds the settings object and would demand
DATABASE_URL & co.), so this static check needs no environment.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
RULES_PATH = ROOT / "architecture_rules.yml"


def _load_archcheck() -> types.ModuleType:
    if "sandloom.core" not in sys.modules:
        if str(SRC) not in sys.path:
            sys.path.insert(0, str(SRC))
        spec = importlib.util.find_spec("sandloom.core")
        if spec is None or not spec.submodule_search_locations:
            raise SystemExit("cannot locate the sandloom.core package")
        pkg = types.ModuleType("sandloom.core")
        pkg.__path__ = list(spec.submodule_search_locations)
        pkg.__spec__ = spec
        pkg.__package__ = "sandloom.core"
        sys.modules["sandloom.core"] = pkg
    import sandloom.core.archcheck.runner as runner

    return runner


def main() -> int:
    runner = _load_archcheck()
    from sandloom.core.archcheck.manifest import load_rules

    rules = load_rules(RULES_PATH)
    violations = runner.check_manifest_imports(rules)
    if not violations:
        print("Architecture check: OK")
        return 0
    print("Architecture violations:")
    for i, viol in enumerate(violations, 1):
        print(f"{i:3}. {viol.format(ROOT)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
