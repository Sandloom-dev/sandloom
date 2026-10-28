"""``tenant-scoped-models``: a table with ``tenant_id`` must be ``TenantScoped``.

AGENTS.md promises fail-closed tenant scoping for tenant-owned models; a model
that declares its own ``tenant_id`` silently opts out of the ORM guard. The
fixture proves the analyzer catches every spelling; the framework tree passing
is pinned by ``test_archcheck_library.test_framework_manifest_passes_every_rule``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import tenant_model_hits
from sandloom.core.archcheck.manifest import ManifestError, load_rules
from sandloom.core.archcheck.runner import run_checks

FIXTURE = (
    Path(__file__).parent
    / "_fixtures"
    / "broken_module"
    / "models"
    / "untenanted.py.txt"
)


def test_fixture_violations_detected() -> None:
    hits = tenant_model_hits(FIXTURE)
    assert [(name, table) for _line, name, table in hits] == [
        ("Invoice", "broken_invoices"),
        ("Ledger", "broken_ledger"),
        ("Shipment", "broken_shipments"),
        ("Table('broken_audit_rows')", "broken_audit_rows"),
    ]


def _app(tmp_path: Path, exempt: str = "") -> Path:
    (tmp_path / "app" / "modules" / "shop" / "models").mkdir(
        parents=True, exist_ok=True
    )
    for init in ("app", "app/modules", "app/modules/shop", "app/modules/shop/models"):
        (tmp_path / init / "__init__.py").write_text("")
    shutil.copy(FIXTURE, tmp_path / "app/modules/shop/models/broken.py")
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(
        "version: 1\nmodules_path: app/modules\nsource_root: .\n" + exempt
    )
    return manifest


def test_rule_runs_in_sandloom_check_and_honours_exemptions(tmp_path: Path) -> None:
    rules = load_rules(_app(tmp_path))
    found = run_checks(rules, only=["tenant-scoped-models"])
    assert len(found) == 4 and all(v.rule == "tenant-scoped-models" for v in found)

    exempt = (
        "tenant_scope_exempt:\n"
        "  - table: broken_ledger\n    reason: global ledger, scanned by ops\n"
        "  - table: no_such_table\n    reason: gone\n"
    )
    rules = load_rules(_app(tmp_path, exempt))
    details = [v.detail for v in run_checks(rules, only=["tenant-scoped-models"])]
    assert not any("broken_ledger" in d for d in details)
    assert any("stale tenant_scope_exempt entry 'no_such_table'" in d for d in details)


def test_exemption_requires_a_reason(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="reason"):
        load_rules(_app(tmp_path, "tenant_scope_exempt:\n  - table: broken_ledger\n"))
