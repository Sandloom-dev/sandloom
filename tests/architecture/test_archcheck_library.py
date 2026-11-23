"""``sandloom check`` (sandloom.core.archcheck) holds the framework tree and applications
to the SAME rules.

* The framework's own manifest passes the full rule set — i.e. ``sandloom check``
  run in ``backend/`` is green exactly when the per-rule pytest suites are
  (with the rules that read the installed module packages, in
  ``tests/integration/with_batteries/test_archcheck_library_installed_modules.py``).
* The analyzers are namespace-agnostic: pointed at an application's modules
  package (``<app>.modules``) they catch the same violations.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

from sandloom.core import error_codes
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
from sandloom.core.archcheck.manifest import (
    CORE_IN_PUBLIC_API_CODE,
    DEFAULT_FORBIDDEN_PATTERNS,
    DEFAULT_PUBLIC_API,
    DEFAULT_TEST_API,
    ManifestError,
)
from sandloom.core.archcheck.runner import (
    API10_RULES,
    PENDING_RULES,
    driver_import_violations,
    iter_module_test_files,
)
from sandloom.core.cli.add import GeneratorError
from sandloom.core.cli.add import layout as add_layout
from sandloom.core.cli.check import main as check_main
from sandloom.core.cli.project import generate_project

BACKEND = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "_fixtures"


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
        "dialect-imports",  # infra-agnostic: no database dialect in a module
        "utc-datetime-columns",  # infra-agnostic: times read back in UTC
        "database-feature-forms",  # infra-agnostic: feature forms declared
        "undeclared-capability",  # tasks declare taskqueue / periodic (C033)
        "task-enqueue-ownership",  # a module enqueues only its own tasks (C035)
        "portable-sql",  # infra-agnostic: SQL every database runs alike
        "generated-reserved",  # ADR-0013: _generated/ is the generator's (C032)
        "yield-inside-unscoped",  # a generator never hands unscoped() on (C036)
        "engine-in-module",  # an engine of a module's own skips the guards (C037)
        "cursor-listener",  # no listener runs around the cursor tenant check (C039)
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
        "public_api:\n  - sandloom.sdk\n"
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
        "sandloom.core.drivers.memory_cache",
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
        "public_api:\n  - sandloom.sdk\n"
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
        "sandloom.core.drivers.memory_cache",
        "sandloom.core._internal.db_provider",
        "acme.modules.billing_lite",
        "acme.modules.billing_lite.services",
    }


def test_test_imports_is_a_whitelist() -> None:
    hits = module_test_import_hits(
        ACME / "tests" / "test_leaks.py.txt",
        own_package="acme.modules.orders",
        allowed=("sandloom.sdk", "sandloom.shared", *DEFAULT_TEST_API),
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


@pytest.mark.parametrize(
    "source",
    [
        "from sandloom.driverkit import NotifierProvider\n",
        "from sandloom import driverkit\n",
        "import sandloom.driverkit.testing\n",
        "import redis\n",
        "from celery import Celery\n",
    ],
)
def test_a_business_module_cannot_reach_a_driver_or_a_backend(
    tmp_path: Path, source: str
) -> None:
    """A module knows infrastructure by name; only a driver package implements it."""
    module = tmp_path / "services.py"
    module.write_text(source)
    assert find_legacy_infra_imports(module)


@pytest.mark.parametrize(
    ("driver", "source", "allowed"),
    [
        ("acme_search.driver", "from acme_search.contracts import SEARCH\n", True),
        ("acme_search.driver", "from acme_search import contracts\n", True),
        ("acme_search.driver", "from acme_search.driver import Search\n", False),
        ("acme_search.driver", "from acme_search import driver\n", False),
        ("acme_search.driver", "import acme_search\nacme_search.driver\n", False),
        ("acme_search.driver", '__import__("acme_search", fromlist=["x"])\n', False),
        # A namespace package: only the driver and its parents are off limits.
        ("acme.search.driver", "from acme.search.contracts import SEARCH\n", True),
        ("acme.search.driver", "from acme.audit.types import Audit\n", True),
        ("acme.search.driver", "import acme.search\n", False),
        # A driver under contracts is still a driver.
        ("acme.contracts.driver", "from acme.contracts.driver import X\n", False),
        # The application's own package.
        ("shop.driver", "from shop.helpers import x\n", True),
        ("shop.driver", "from shop.driver import Mine\n", False),
        ("shop.driver", "import shop\nshop.driver.Mine()\n", False),
        # An entry point at a package root.
        ("rooted", "from rooted.contracts import REF\n", True),
        ("rooted", "from rooted import Driver\n", False),
        ("shop", "from shop.modules.notes.schemas import Note\n", True),
        ("shop", "import shop\n", False),
    ],
)
def test_a_module_may_not_reach_a_driver(
    tmp_path: Path, driver: str, source: str, allowed: bool
) -> None:
    module = tmp_path / "shop" / "modules" / "notes" / "services.py"
    module.parent.mkdir(parents=True)
    module.write_text(source)
    (tmp_path / "architecture_rules.yml").write_text(
        "modules_path: shop/modules\nsource_root: .\n"
    )
    found = driver_import_violations(
        module, load_rules(tmp_path / "architecture_rules.yml"), [driver]
    )
    assert not found if allowed else found


@pytest.mark.parametrize(
    "source",
    [
        "import sandloom\nsandloom.driverkit.NotifierProvider\n",
        '__import__("sandloom", fromlist=["driverkit"]).driverkit\n',
    ],
)
def test_a_module_cannot_reach_the_kit_through_the_parent_package(
    tmp_path: Path, source: str
) -> None:
    module = tmp_path / "services.py"
    module.write_text(source)
    assert find_legacy_infra_imports(module)


def test_the_repository_and_every_example_list_no_kernel_module() -> None:
    """``public_api`` is ``sandloom.sdk`` in the repository's own manifest and in
    each example's: ``load_rules`` refuses a kernel entry (SANDLOOM-C038), and a
    refused file fails here before the example's CI job does."""
    manifests = [
        BACKEND / "architecture_rules.yml",
        *sorted((BACKEND / "examples").glob("*/architecture_rules.yml")),
    ]
    assert {m.parent.name for m in manifests[1:]} >= {
        "academy",
        "helpdesk",
        "linkly",
        "oidc-identity",
        "projecthub",
        "tutorial",
    }
    for manifest in manifests:
        assert "sandloom.sdk" in load_rules(manifest).public_api, manifest


# --- SANDLOOM-C038: a ``public_api`` that lists the kernel is refused ---------
#
# A business module imports ``sandloom.sdk`` and nothing of ``sandloom.core``. The
# manifest's ``public_api`` names what a module may import, so an entry that would
# let a module import the kernel (a ``sandloom.core`` module, ``sandloom.core``
# itself, or ``sandloom``, which the prefix match of the import check lets cover
# it) fails ``load_rules`` and with it ``sandloom check``: no warning phase.

#: What the scaffold of an earlier release listed (and the repository did).
FORMER_KERNEL_ENTRIES = (
    "sandloom.core.di",
    "sandloom.core.facade",
    "sandloom.core.module_api",
    "sandloom.core.descriptors",
    "sandloom.core.orm",
    "sandloom.core.projections",
)


def _public_api_manifest(tmp_path: Path, entries: list[str] | None) -> Path:
    """A manifest over one module; ``entries`` None leaves ``public_api`` out."""
    (tmp_path / "app" / "modules" / "m").mkdir(parents=True)
    (tmp_path / "app" / "modules" / "m" / "__init__.py").write_text("")
    text = "modules_path: app/modules\n"
    if entries is not None:
        text += "public_api:\n" + "".join(f"  - {e}\n" for e in entries)
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(text)
    return manifest


def test_the_refusal_code_is_catalogued_as_a_check() -> None:
    entry = error_codes.get(CORE_IN_PUBLIC_API_CODE)
    assert entry.kind == "check"
    assert "docs/operate/upgrade.md" in entry.fix


@pytest.mark.parametrize(
    "entry",
    [
        *FORMER_KERNEL_ENTRIES,
        "sandloom.core",  # the package itself
        "sandloom",  # covers sandloom.core by prefix
        "sandloom.core.testing",  # a test surface is not module API either
        "sandloom.core.*",  # a glob is still a sandloom.core entry
    ],
)
def test_a_public_api_listing_the_kernel_is_refused(tmp_path: Path, entry: str) -> None:
    manifest = _public_api_manifest(tmp_path, ["sandloom.sdk", entry])
    with pytest.raises(ManifestError) as raised:
        load_rules(manifest)
    message = str(raised.value)
    assert CORE_IN_PUBLIC_API_CODE in message
    assert f"delete {entry} from `public_api`" in message
    assert "docs/operate/upgrade.md" in message
    assert str(manifest) in message


def test_the_refusal_names_every_entry_to_delete(tmp_path: Path) -> None:
    manifest = _public_api_manifest(
        tmp_path,
        ["sandloom.core.orm", "sandloom.sdk", "sandloom.core.di", "sandloom.shared"],
    )
    with pytest.raises(ManifestError) as raised:
        load_rules(manifest)
    assert "delete sandloom.core.di, sandloom.core.orm from `public_api`" in str(
        raised.value
    )


@pytest.mark.parametrize(
    "entry",
    [
        "sandloom.sdk",
        "sandloom.sdk.testing.fixtures",
        "sandloom.shared",  # shared_api's default, not the kernel
        "sandloom.corelib",  # a sibling name that only starts like the kernel
        "sandloom_core",
        "app.modules",
    ],
)
def test_an_entry_that_does_not_cover_the_kernel_loads(
    tmp_path: Path, entry: str
) -> None:
    rules = load_rules(_public_api_manifest(tmp_path, [entry]))
    assert rules.public_api == {entry}


def test_a_manifest_without_public_api_takes_the_sdk_default(tmp_path: Path) -> None:
    for entries in (None, []):
        rules = load_rules(_public_api_manifest(tmp_path / str(entries), entries))
        assert rules.public_api == set(DEFAULT_PUBLIC_API)
    assert all(e.startswith("sandloom.sdk") for e in DEFAULT_PUBLIC_API)
    assert "sandloom.sdk" in DEFAULT_PUBLIC_API


def _sdk_private_import_violations(tmp_path: Path, extra: str, line: str) -> list[str]:
    """The `framework-imports` details of a module doing ``line``, over a
    manifest that names only the module location (``extra`` is appended)."""
    module = tmp_path / "app" / "modules" / "m"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    (module / "leak.py").write_text(line)
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(
        "modules_path: app/modules\nmodules_package: app.modules\n" + extra
    )
    found = run_checks(load_rules(manifest), only=["framework-imports"])
    assert {v.rule for v in found} <= {"framework-imports"}
    return [v.detail for v in found]


def test_the_default_forbids_the_private_layers_of_the_sdk() -> None:
    assert DEFAULT_FORBIDDEN_PATTERNS == ("sandloom.sdk._*", "sandloom.sdk.*._*")


@pytest.mark.parametrize(
    "line",
    [
        "from sandloom.sdk._prim import data\n",
        "import sandloom.sdk._decl.module\n",
        "from sandloom.sdk.testing._helpers import x\n",
    ],
)
def test_a_manifest_naming_no_patterns_still_refuses_the_sdk_private_layers(
    tmp_path: Path, line: str
) -> None:
    """The sdk default of `public_api` matches by prefix and admits
    ``sandloom.sdk._prim``; the default forbidden patterns close that, with no
    ``forbidden_patterns`` key in the manifest."""
    details = _sdk_private_import_violations(tmp_path, "", line)
    assert details
    assert all("forbidden_pattern" in d for d in details)


def test_a_public_sdk_import_is_not_refused_by_the_default_patterns(
    tmp_path: Path,
) -> None:
    assert (
        _sdk_private_import_violations(
            tmp_path, "", "from sandloom.sdk.data import open_uow\n"
        )
        == []
    )


def test_the_default_patterns_are_kept_when_the_manifest_names_its_own(
    tmp_path: Path,
) -> None:
    extra = 'forbidden_patterns:\n  - "app.modules.*._internal*"\n'
    rules = load_rules(_public_api_manifest(tmp_path, None))
    assert rules.forbidden_patterns == list(DEFAULT_FORBIDDEN_PATTERNS)
    (tmp_path / "second").mkdir()
    details = _sdk_private_import_violations(
        tmp_path / "second", extra, "from sandloom.sdk._prim import data\n"
    )
    assert details
    manifest = tmp_path / "second" / "architecture_rules.yml"
    own = load_rules(manifest)
    assert own.forbidden_patterns == [
        *DEFAULT_FORBIDDEN_PATTERNS,
        "app.modules.*._internal*",
    ]
    # a manifest that repeats a default does not list it twice
    manifest.write_text(manifest.read_text() + '  - "sandloom.sdk._*"\n')
    assert load_rules(manifest).forbidden_patterns == own.forbidden_patterns


def test_the_default_is_the_public_api_of_the_repository_and_the_scaffold(
    tmp_path: Path,
) -> None:
    """One list, three places: a drift here is a module that passes
    ``sandloom check`` in one tree and fails it in another."""
    assert load_rules(BACKEND / "architecture_rules.yml").public_api == set(
        DEFAULT_PUBLIC_API
    )
    generate_project("probe", tmp_path)
    scaffold = load_rules(tmp_path / "probe" / "architecture_rules.yml")
    assert scaffold.public_api == set(DEFAULT_PUBLIC_API)


def test_sandloom_add_does_not_send_a_refused_manifest_to_the_project_root(
    tmp_path: Path,
) -> None:
    """The root hint is for a manifest that is not found or not readable; on
    C038 the manifest was read, and the hint would mislead."""
    refused = tmp_path / "refused"
    _public_api_manifest(refused, ["sandloom.core.di"])
    with pytest.raises(GeneratorError) as raised:
        add_layout(refused)
    assert CORE_IN_PUBLIC_API_CODE in str(raised.value)
    assert "from the project root" not in str(raised.value)

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(GeneratorError) as missing:
        add_layout(other)
    assert "manifest not found" in str(missing.value)
    assert "from the project root" in str(missing.value)


def test_sandloom_check_refuses_the_manifest_with_exit_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal comes before the scan: a module that would be a violation
    does not turn it into exit code 1."""
    generate_project("probe", tmp_path, sample_module="gadgets")
    rules = tmp_path / "probe" / "architecture_rules.yml"
    leak = tmp_path / "probe" / "probe" / "modules" / "gadgets" / "leak.py"
    leak.write_text("async def f(db):\n    await db.rollback()\n")
    assert check_main(["--rules", str(rules)]) == 1  # the leak alone is a violation
    capsys.readouterr()

    rules.write_text(
        rules.read_text().replace(
            "public_api:\n",
            "public_api:\n  - sandloom.core.di\n  - sandloom.core.orm\n",
        )
    )
    assert check_main(["--rules", str(rules)]) == 2
    err = capsys.readouterr().err
    assert CORE_IN_PUBLIC_API_CODE in err
    assert "delete sandloom.core.di, sandloom.core.orm from `public_api`" in err


@pytest.mark.parametrize(
    "line",
    [
        "from sandloom.core.di import db_session\n",
        "from sandloom.core.module_api import define_module\n",
        "import sandloom.core.orm\n",
        "from sandloom.core.projections import Projection\n",
    ],
)
def test_a_module_importing_the_kernel_fails_sandloom_check_in_a_fresh_app(
    tmp_path: Path, line: str
) -> None:
    generate_project("probe", tmp_path, sample_module="gadgets")
    rules = load_rules(tmp_path / "probe" / "architecture_rules.yml")
    assert run_checks(rules, only=["framework-imports"]) == []
    leak = tmp_path / "probe" / "probe" / "modules" / "gadgets" / "leak.py"
    leak.write_text(line)
    found = run_checks(rules, only=["framework-imports"])
    assert [v.rule for v in found] == ["framework-imports"]
    assert "non_public_api" in found[0].detail
