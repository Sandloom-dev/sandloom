"""``sandloom check`` (sandloom.core.archcheck) holds the framework tree and applications
to the SAME rules.

* The framework's own manifest passes the full rule set — i.e. ``sandloom check``
  run in ``backend/`` is green exactly when the per-rule pytest suites are.
* The analyzers are namespace-agnostic: pointed at an application's modules
  package (``<app>.modules``) they catch the same violations.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

from sandloom.core.archcheck import RULES, load_rules, run_checks
from sandloom.core.archcheck.analyzers import (
    dynamic_imports,
    find_contract_dto_leaks,
    find_cross_module_imports,
    find_kernel_handle_access,
    find_kernel_internal_test_imports,
    find_legacy_infra_imports,
    find_orm_leakage,
    find_schema_orm_imports,
    module_test_import_hits,
    parse_file,
    parse_imports,
)
from sandloom.core.archcheck.manifest import DEFAULT_TEST_API
from sandloom.core.archcheck.runner import (
    API10_RULES,
    PENDING_RULES,
    iter_module_test_files,
)

BACKEND = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "_fixtures"


def test_framework_manifest_passes_every_rule() -> None:
    rules = load_rules(BACKEND / "architecture_rules.yml")
    assert rules.modules_package == ""  # top-level module packages
    assert rules.is_framework_tree
    assert {"sandloom_identity", "sandloom_tenancy", "sandloom_billing"} <= set(
        rules.business_modules()
    )
    assert "sandloom" not in rules.business_modules()
    violations = run_checks(rules)
    errors = [v for v in violations if v.severity == "error"]
    assert errors == [], "\n".join(v.format(BACKEND) for v in errors)
    # SANDLOOM-C022 is a warning: it lists the hand-rolled authority routes and the
    # untagged credential effects until they declare a risk (C-ROUTE-03).
    warnings = {v.rule for v in violations if v.severity != "error"}
    assert warnings <= {"untagged-sensitive"}, sorted(warnings)


def test_rule_ids_are_stable() -> None:
    assert set(RULES) == {
        "module-boundaries",
        "framework-imports",
        "raw-provider",
        "infra-imports",
        "kernel-handles",
        "uow-boundary",
        "best-effort-effects",  # 0.7: guaranteed publish/audit by default
        "get-core-await",  # 0.6.0: get_core is async
        "endpoints-outside-api",
        "orm-leakage",
        "schema-orm-imports",
        "test-imports",
        "contract-dtos",
        "unsafe-tenant-sql",
        "tenant-scoped-models",  # 0.8: a tenant_id table is TenantScoped
        # DX R3: the probe's static gaps (docs/design/dx-cognitive-load-*.md)
        "event-schema-channels",
        "guard-order",
        "consumed-contracts",
        "db-dependency-scope",
    } | {
        # Public API 1.0 (C021-C027, e.g. "module-local-imports"): each is an
        # sandloom check rule exactly when its catalogue code is no longer planned.
        rule
        for rule in API10_RULES
        if rule not in PENDING_RULES
    }
    assert "module-local-imports" in API10_RULES


def test_cli_help_lists_every_rule() -> None:
    """``sandloom check``'s module help names every rule ``--list-rules`` prints."""
    from sandloom.core.cli import check

    doc = check.__doc__ or ""
    missing = [rule for rule in RULES if f"    {rule} " not in doc]
    assert missing == [], f"add to sandloom/core/cli/check.py's docstring: {missing}"


def test_cli_rejects_missing_module_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandloom.core.cli.check import main

    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text("modules_path: typo/modules\nmodules_package: app.modules\n")
    assert main(["--rules", str(manifest)]) == 2
    assert "modules_path" in capsys.readouterr().err


def test_empty_existing_module_directory_is_valid(tmp_path: Path) -> None:
    (tmp_path / "modules").mkdir()
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text("modules_path: modules\nmodules_package: app.modules\n")
    assert run_checks(load_rules(manifest)) == []


def test_contract_dto_leaks_detected() -> None:
    """SANDLOOM-C012, static: an own-package DTO a contract signature names
    needs its generated copy (``sandloom generate``)."""
    leak = FIXTURES / "broken_module" / "services" / "contract_dto_leak.py.txt"
    found = find_contract_dto_leaks(
        leak,
        "broken_module",
        source_root=FIXTURES,
        generated=FIXTURES / "broken_module" / "broken_module_dto.py.txt",
    )
    joined = "\n".join(found)
    assert len(found) == 3, joined
    assert "'get' (return) uses WidgetOut imported from broken_module.schemas" in joined
    assert "'find' (return) uses WidgetOut imported from" in joined  # L1 @contract
    assert "own_schemas.Gadget imported from broken_module.schemas" in joined
    assert "run `sandloom generate`" in joined
    assert "LocalDTO" not in joined  # its copy exists
    assert "MemberState" not in joined  # shared DTOs are fine
    assert "Hidden" not in joined  # a private helper is not the contract
    # Without the copy file's location nothing can be judged.
    assert find_contract_dto_leaks(leak, "broken_module", source_root=FIXTURES) == []


@pytest.mark.parametrize("fixture", ["boards", "broken_module"])
def test_fixture_violations_detected_via_shared_analyzer(fixture: str) -> None:
    found = [
        hit
        for py in (FIXTURES / fixture).glob("**/*.py.txt")
        for hit in find_cross_module_imports(
            py, fixture, ["identity", "tenancy", "billing", fixture]
        )
    ]
    assert found


def test_app_namespace_cross_module_spellings(tmp_path: Path) -> None:
    sample = tmp_path / "leak.py"
    sample.write_text(
        "import importlib, sys\n"
        "import helpdesk.modules as mods\n"
        "from helpdesk.modules.billing_lite import api\n"
        "from helpdesk.modules import billing_lite\n"
        "import helpdesk.modules.billing_lite.models\n"
        "from ..billing_lite import services\n"
        "importlib.import_module('helpdesk.modules.billing_lite')\n"
        "x = sys.modules['helpdesk.modules.billing_lite']\n"
        "y = getattr(mods, 'billing_lite')\n"
        "z = mods.billing_lite.thing\n"
        "from helpdesk.modules.tickets import own\n"
    )
    found = find_cross_module_imports(
        sample,
        "tickets",
        ["tickets", "billing_lite"],
        modules_package="helpdesk.modules",
    )
    assert len(found) >= 9, found
    assert not any("tickets" in f for f in found)


def test_orm_leakage_with_app_model_roots(tmp_path: Path) -> None:
    sample = tmp_path / "router.py"
    sample.write_text(
        "from fastapi import APIRouter\n"
        "from helpdesk.modules.tickets.models import Ticket\n"
        "router = APIRouter()\n"
        "@router.get('/x', response_model=Ticket)\n"
        "async def x() -> Ticket | None:\n"
        "    return Ticket()\n"
    )
    assert find_orm_leakage(sample) == []  # not an sandloom.* model root
    found = find_orm_leakage(sample, model_roots=("sandloom", "helpdesk"))
    assert any("import of ORM models" in f for f in found)
    assert any("response_model" in f for f in found)
    assert any("function return" in f for f in found)


def test_relative_model_imports_count_in_schemas(tmp_path: Path) -> None:
    sample = tmp_path / "dto.py"
    sample.write_text("from ..models import Ticket\nfrom sqlalchemy import String\n")
    assert len(find_schema_orm_imports(sample)) == 2


def test_kernel_handles_rule_detects_the_fixture() -> None:
    """``kernel-handles`` is an archcheck rule: positive detection on a fixture."""
    found = find_kernel_handle_access(
        FIXTURES / "broken_module" / "kernel_handles.py.txt"
    )
    for shape in (".event_bus", ".infra", ".service_registry", ".state.core"):
        assert any(shape in f for f in found), (shape, found)
    assert not any("get_service" in f for f in found)


def test_kernel_handles_rule_runs_in_sandloom_check(tmp_path: Path) -> None:
    """The same rule fires through run_checks (what ``sandloom check`` executes)."""
    pkg = tmp_path / "src" / "acme" / "modules"
    (pkg / "orders").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "orders" / "__init__.py").write_text(
        "def register(core):\n    core.event_bus.publish('a.b.v1', {})\n"
    )
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(
        "modules_path: src/acme/modules\n"
        "modules_package: acme.modules\n"
        "public_api:\n  - sandloom.core.di\n"
    )
    violations = run_checks(load_rules(manifest), only=["kernel-handles"])
    assert [v.rule for v in violations] == ["kernel-handles"]
    assert ".event_bus" in violations[0].detail


def test_test_imports_rule_detects_the_fixture() -> None:
    """``test-imports``: module tests reach the framework via sandloom.core.testing."""
    found = find_kernel_internal_test_imports(
        FIXTURES / "broken_module" / "tests" / "conftest.py.txt"
    )
    for shape in (
        "sandloom.core.drivers.redis_cache",
        "from sandloom.core import database",
        "sandloom.core._internal.db_provider",
        "from sandloom.core.database import Base",
        "sandloom.core.sql_alchemy.testing",
        "from sandloom.core.drivers import sqlalchemy_database",
    ):
        assert any(shape in f for f in found), (shape, found)
    assert not any(
        "sandloom.core.testing" in f or "sandloom.core.orm" in f for f in found
    )
    assert len(found) == 6, found


def _app_with_module_test(tmp_path: Path, body: str, allowlist: str = "[]") -> Path:
    pkg = tmp_path / "acme" / "modules"
    (pkg / "orders" / "tests").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "orders" / "__init__.py").write_text("")
    (pkg / "orders" / "tests" / "test_orders.py").write_text(body)
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(
        "modules_path: acme/modules\n"
        "modules_package: acme.modules\n"
        "public_api:\n  - sandloom.core.di\n"
        f"allowlist: {allowlist}\n"
    )
    return manifest


def test_test_imports_rule_runs_in_sandloom_check(tmp_path: Path) -> None:
    manifest = _app_with_module_test(
        tmp_path,
        "from sandloom.core._internal.db_provider import get_db\n"
        "from sandloom.core.testing import make_user_headers\n",
    )
    violations = run_checks(load_rules(manifest), only=["test-imports"])
    assert [v.rule for v in violations] == ["test-imports"]
    assert "sandloom.core._internal.db_provider" in violations[0].detail
    # Module test files stay exempt from the production-code rules.
    assert run_checks(load_rules(manifest), only=["infra-imports"]) == []


def test_test_imports_rule_honours_the_allowlist(tmp_path: Path) -> None:
    manifest = _app_with_module_test(
        tmp_path,
        "from sandloom.core.database import Base\n",
        allowlist='[{file: "acme/modules/orders/tests/*", '
        'imports: ["sandloom.core.database*"]}]',
    )
    assert run_checks(load_rules(manifest), only=["test-imports"]) == []


# --------------------------------------------------------------------------- #
# Code review 2026-10-21 ("Инструменты" 2–8, 13): closed false negatives
# --------------------------------------------------------------------------- #
ACME = FIXTURES / "acme" / "modules" / "orders"


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


def test_from_package_import_name_resolves_per_name(tmp_path: Path) -> None:
    sample = tmp_path / "x.py"
    sample.write_text(
        "from sandloom import identity, core\nfrom sandloom.core import di\nfrom m import *\n"
    )
    assert parse_imports(sample, source_root=tmp_path) == [
        "sandloom.identity",
        "sandloom.core",
        "sandloom.core.di",
        "m",
    ]


def test_dynamic_imports_under_every_importlib_alias() -> None:
    tree = parse_file(ACME / "services" / "framework_leaks.py.txt")
    found = {name for _line, name, _text in dynamic_imports(tree)}
    assert found == {
        "sandloom.core.config.settings",
        "sandloom_tenancy.services",
        "sandloom_billing",
        "sandloom.core.drivers.redis_cache",
        "sandloom.core._internal.db_provider",
        "acme.modules.billing_lite",
        "acme.modules.billing_lite.services",
    }


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


def test_test_imports_is_a_whitelist() -> None:
    hits = module_test_import_hits(
        ACME / "tests" / "test_leaks.py.txt",
        own_package="acme.modules.orders",
        allowed=("sandloom.core.di", "sandloom.shared", *DEFAULT_TEST_API),
        namespaces=(
            "sandloom",
            "acme.modules",
            "sandloom_identity",
            "sandloom_tenancy",
            "sandloom_billing",
        ),
        source_root=FIXTURES,
    )
    assert sorted(module for _line, module, _text in hits) == [
        "acme.modules.billing_lite.models.Plan",
        "acme.modules.billing_lite.services",
        "sandloom.core.config.settings.settings",
        "sandloom_billing",
        "sandloom_identity.services.TokenService",
        "sandloom_tenancy",
    ]


def test_test_imports_whitelist_runs_in_sandloom_check(tmp_path: Path) -> None:
    manifest = _acme_app(tmp_path, "tests/test_leaks.py.txt")
    violations = run_checks(load_rules(manifest), only=["test-imports"])
    assert len(violations) == 6, violations
    assert any("from ...billing_lite import services" in v.detail for v in violations)


def test_legacy_test_import_baseline_has_no_stale_entries() -> None:
    """The bundled tests' whitelist debt may shrink, never silently persist.

    Every manifest allowlist entry for a module TEST file must still excuse a
    real import there — drop the entry once the test is fixed.
    """
    rules = load_rules(BACKEND / "architecture_rules.yml")
    used: dict[str, set[str]] = {}
    for module in rules.business_modules():
        for py in iter_module_test_files(rules, module):
            rel = py.relative_to(rules.root).as_posix()
            for _line, imported, _text in module_test_import_hits(
                py,
                own_package=rules.module_package(module),
                allowed=(*rules.public_api, *rules.shared_api, *rules.test_api),
                source_root=rules.source_root,
            ):
                used.setdefault(rel, set()).add(imported)
    stale = [
        f"{entry['file']}: {pattern}"
        for entry in rules.allowlist
        if "/tests/" in str(entry.get("file"))
        for pattern in entry.get("imports") or []
        if not any(fnmatch.fnmatch(i, pattern) for i in used.get(entry["file"], ()))
    ]
    assert stale == [], stale


def test_parsed_ast_is_cached_per_file_version(tmp_path: Path) -> None:
    sample = tmp_path / "m.py"
    sample.write_text("x = 1\n")
    first = parse_file(sample)
    assert parse_file(sample) is first  # one parse per file per run
    sample.write_text("x = 22\n")  # a changed file is re-parsed
    assert parse_file(sample) is not first


def test_a_business_module_cannot_import_the_driver_api(tmp_path: Path) -> None:
    """A module knows infrastructure by name; only a driver package implements it."""
    module = tmp_path / "services.py"
    module.write_text("from sandloom.driver import NotifierProvider\n")
    assert find_legacy_infra_imports(module)
