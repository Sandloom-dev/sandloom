"""``sandloom check`` over the framework tree and against the installed module
packages (moved from ``tests/architecture/test_archcheck_library.py``, KB-05c).

The framework's own manifest names the bundled modules and passes every
rule; an application module that reaches an installed module package
(``sandloom_identity``, by import, ``import_module`` or a constant) breaks
the module boundary, and its tests may not import one. The analyzers
themselves stay in the architecture suite.
"""

from __future__ import annotations

import os
from pathlib import Path

from sandloom.core.archcheck import load_rules, run_checks
from sandloom.core.cli.check import RECORD_ENV, recorded_violations

BACKEND = Path(__file__).resolve().parents[3]
FIXTURES = BACKEND / "tests" / "architecture" / "_fixtures"
ACME = FIXTURES / "acme" / "modules" / "orders"


def test_framework_manifest_passes_every_rule() -> None:
    rules = load_rules(BACKEND / "architecture_rules.yml")
    assert rules.modules_package == ""  # top-level module packages
    assert rules.is_framework_tree
    assert {"sandloom_identity", "sandloom_tenancy", "sandloom_billing"} <= set(
        rules.business_modules()
    )
    assert "sandloom" not in rules.business_modules()
    # Inside `sandloom test`, the `sandloom check` step already ran every rule
    # over this manifest and recorded what it found: the same violations,
    # asserted the same way, without a second run. Alone (CI's job, plain
    # pytest) the rules run here.
    record = os.environ.get(RECORD_ENV)
    violations = (
        recorded_violations(Path(record), BACKEND / "architecture_rules.yml")
        if record
        else None
    )
    if violations is None:
        violations = run_checks(rules)
    errors = [v for v in violations if v.severity == "error"]
    assert errors == [], "\n".join(v.format(BACKEND) for v in errors)
    # SANDLOOM-C022 is a warning: it lists the hand-rolled authority routes and the
    # untagged credential effects until they declare a risk (C-ROUTE-03).
    warnings = {v.rule for v in violations if v.severity != "error"}
    assert warnings <= {"untagged-sensitive"}, sorted(warnings)


def _acme_app(tmp_path: Path, *files: str) -> Path:
    """Materialise fixture files of ``acme.modules.orders`` as a real app tree.

    The fixtures are ``.py.txt`` data; ``sandloom check`` scans ``*.py``, so each
    one is copied under ``tmp_path`` (with a sibling ``billing_lite`` module).
    """
    pkg = tmp_path / "acme" / "modules"
    for mod in ("orders", "billing_lite"):
        (pkg / mod).mkdir(parents=True, exist_ok=True)
        (pkg / mod / "__init__.py").write_text("")
    (tmp_path / "acme" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    for rel in files:
        target = pkg / "orders" / rel.removesuffix(".txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ACME / rel).read_text())
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(
        "modules_path: acme/modules\n"
        "modules_package: acme.modules\n"
        "public_api:\n  - sandloom.core.di\n"
    )
    return manifest


def test_framework_imports_catch_from_sandloom_import_and_dynamic_aliases(
    tmp_path: Path,
) -> None:
    rules = load_rules(_acme_app(tmp_path, "services/framework_leaks.py.txt"))
    by_rule: dict[str, list[str]] = {}
    for v in run_checks(rules):
        by_rule.setdefault(v.rule, []).append(v.detail)
    details = by_rule.pop("framework-imports")
    assert all(detail.startswith("Line ") for detail in details)
    framework = sorted(d.split(": ", 1)[1].split(" (")[0] for d in details)
    assert framework == [
        "sandloom.core",
        "sandloom.core._internal.db_provider",
        "sandloom.core.config.settings",  # static and dynamic: one report per module
        "sandloom.core.drivers.redis_cache",
    ], framework
    infra = "\n".join(by_rule.pop("infra-imports"))
    assert "sandloom.core.drivers.redis_cache" in infra
    assert "sandloom.core._internal.db_provider" in infra
    boundaries = by_rule.pop("module-boundaries")
    # Two sibling modules (static + relative), and every spelling of an
    # installed module package (import, import_module, constant concat).
    assert len(boundaries) == 5, boundaries
    installed = sorted(
        d.split(": ", 1)[1].split(" (")[0]
        for d in boundaries
        if "installed module package" in d
    )
    assert installed == [
        "sandloom_billing",
        "sandloom_identity",
        "sandloom_tenancy.services",
    ]
    assert by_rule == {}, by_rule


def test_test_imports_whitelist_runs_in_sandloom_check(tmp_path: Path) -> None:
    manifest = _acme_app(tmp_path, "tests/test_leaks.py.txt")
    violations = run_checks(load_rules(manifest), only=["test-imports"])
    assert len(violations) == 6, violations
    assert any("from ...billing_lite import services" in v.detail for v in violations)
