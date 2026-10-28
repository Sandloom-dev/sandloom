"""The ``sandloom.sdk`` skeleton of Public API 1.0 (spec §2, §3; hand-off H0).

- the migration scanner (``tools/packaging/public_api_migration.py``) finds no
  unmapped name: every kernel name of Appendix C and every kernel import of
  the bundled modules and examples has a public home, and every home exists;
- L1 is built only from public L2 (``test_l1_uses_only_public_l2``);
- every public module declares ``__all__`` and ``__beginner__``, and the
  root re-exports exactly the 21 beginner code names;
- the names new in 1.0 whose kernel mechanism has not landed yet are typed
  stubs that raise ``NotImplementedError`` naming their spec section and
  hand-off (the L1 names of hand-off H2 are tested in ``tests/sdk``).
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import sandloom.sdk
from sandloom.sdk._decl.contracts import ContractRef, OptionalContract
from sandloom.sdk._prim.web import ResolvedAccount

BACKEND = Path(__file__).resolve().parents[2]
SDK = BACKEND / "src" / "sandloom" / "sdk"


def _scanner() -> ModuleType:
    path = BACKEND / "tools" / "packaging" / "public_api_migration.py"
    spec = importlib.util.spec_from_file_location("public_api_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _public_sdk_modules() -> list[ModuleType]:
    names = ["sandloom.sdk"] + [
        info.name
        for info in pkgutil.walk_packages(sandloom.sdk.__path__, prefix="sandloom.sdk.")
        if not any(p.startswith("_") for p in info.name.split("."))
    ]
    return [importlib.import_module(n) for n in names]


def test_scanner_reports_no_unmapped_name() -> None:
    assert _scanner().run() == []


def test_scanner_detects_a_missing_home() -> None:
    scanner = _scanner()
    row = scanner.Row("core.di.DbSession", "sandloom.sdk.web", "kept", "")
    assert scanner.missing_homes([row]) == []
    ghost = scanner.Row("core.di.NoSuchName", "sandloom.sdk.web", "kept", "")
    assert scanner.missing_homes([ghost]) == [
        "core.di.NoSuchName: sandloom.sdk.web.NoSuchName not exported"
    ]
    renamed = scanner.Row(
        "core.di.db_session", "sandloom.sdk.data.open_uow", "renamed", ""
    )
    assert renamed.targets() == [("sandloom.sdk.data", "open_uow")]


def test_scanner_table_covers_appendix_c() -> None:
    rows = _scanner().parse_table()
    statuses = {r.status for r in rows}
    assert statuses == {"kept", "renamed", "replaced", "changed", "adr0009"}
    assert len([r for r in rows if r.status == "adr0009"]) >= 6


def _imports_from(path: Path, prefix: str) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(prefix):
            out.extend((node.module or "", a.name) for a in node.names)
    return out


def test_l1_uses_only_public_l2() -> None:
    """Every name ``_decl`` takes from ``_prim`` is exported by a public
    facade, and every ``_prim`` name is public (spec §3)."""
    public: set[str] = set()
    for module in _public_sdk_modules():
        public.update(getattr(module, "__all__", ()))
    for path in sorted((SDK / "_decl").glob("*.py")):
        for module, name in _imports_from(path, "sandloom.sdk._prim"):
            assert name in public, f"{path.name}: {module}.{name} is not public L2"
    for path in sorted((SDK / "_prim").glob("*.py")):
        if path.name == "__init__.py":
            continue
        prim = importlib.import_module(f"sandloom.sdk._prim.{path.stem}")
        missing = set(prim.__all__) - public
        assert not missing, f"_prim.{path.stem}: {sorted(missing)} not in a facade"


def test_every_public_module_declares_its_tiers() -> None:
    for module in _public_sdk_modules():
        names = getattr(module, "__all__", None)
        assert isinstance(names, list) and names, module.__name__
        beginner = getattr(module, "__beginner__", None)
        assert isinstance(beginner, tuple), module.__name__
        assert set(beginner) <= set(names), module.__name__
        for name in names:
            assert hasattr(module, name), f"{module.__name__}.{name}"


def test_root_reexports_the_beginner_code_names() -> None:
    assert sorted(sandloom.sdk.__all__) == sorted(
        [
            "define_module", "module_router", "public", "CurrentUser", "Base",
            "TenantScoped", "db", "Event", "on", "contract", "command",
            "Contract", "require", "Subscribed", "SubscriptionActive",
            "Credits", "OrgRole", "Owner", "authorize_resource",
            "record_audit", "on_commit", "new_id", "CreatedAt",
        ]
    )  # fmt: skip
    testing = importlib.import_module("sandloom.sdk.testing")
    assert set(testing.__beginner__) == {
        "as_user", "published", "deliver", "fake_contract", "fake_access_fact",
    }  # fmt: skip


def _stub_calls() -> list[tuple[str, Any]]:
    projections = importlib.import_module("sandloom.sdk.projections")
    view = projections.ProjectionView.__new__(projections.ProjectionView)
    return [
        ("ProjectionView.search", lambda: asyncio.run(view.search("q"))),
    ]


@pytest.mark.parametrize(
    ("name", "call"), _stub_calls(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_new_names_are_stubs_naming_their_section(name: str, call: Any) -> None:
    """The names whose mechanism lands after H1 still raise, naming it."""
    with pytest.raises(
        NotImplementedError, match=r"spec §.* lands at H[1-4] \(WS-[AB]\)"
    ):
        call()


#: (public home, name, kernel home) of every name H1 implemented.
_H1_NAMES = [
    ("sandloom.sdk.web", n, "sandloom.core.di")
    for n in (
        "public", "no_idempotency", "replay_safe", "sensitive", "at_commit",
        "operation_intent", "external_call", "release_check", "ResolvedAccount",
        "writes", "allow_multi_db_write", "request_operation_key",
    )
] + [
    ("sandloom.sdk.web", "current_service", "sandloom.shared.guards.service"),
    ("sandloom.sdk.effects", "sensitive_effect", "sandloom.core.di"),
    ("sandloom.sdk.effects", "mark_reauthenticated", "sandloom.core.di"),
    ("sandloom.sdk.infra", "objectstore", "sandloom.core.di"),
    ("sandloom.sdk.infra", "taskqueue", "sandloom.core.di"),
    ("sandloom.sdk.infra", "capability", "sandloom.core.di"),
    ("sandloom.sdk.data", "MoneyLedger", "sandloom.core.orm"),
    ("sandloom.sdk.authz", "Account", "sandloom.shared.authz"),
    ("sandloom.sdk.tenant", "tenant_kind", "sandloom.shared.authz.account"),
    ("sandloom.sdk.tenant", "TenantKind", "sandloom.shared.authz.account"),
    ("sandloom.sdk.contracts", "caller_user_id", "sandloom.core.module_api"),
    ("sandloom.sdk.web", "authorizes", "sandloom.core.di"),
]  # fmt: skip


@pytest.mark.parametrize(("home", "name", "kernel"), _H1_NAMES)
def test_h1_names_are_the_kernels(home: str, name: str, kernel: str) -> None:
    """Hand-off H1: each name is the kernel object itself, no wrapper."""
    assert getattr(importlib.import_module(home), name) is getattr(
        importlib.import_module(kernel), name
    )


def test_db_and_open_uow_are_the_kernel_units() -> None:
    """``db()`` outside a unit is the kernel's SANDLOOM-R002 refusal; ``open_uow``
    is ``db_session`` (an async context manager)."""
    from sandloom.core.di import UnitOfWorkRequiredError
    from sandloom.sdk.data import db, open_uow

    with pytest.raises(UnitOfWorkRequiredError):
        db()
    cm = open_uow()
    assert hasattr(cm, "__aenter__")  # never entered: nothing is opened


def test_public_is_the_kernel_marker() -> None:
    """``public()`` builds the kernel's model E marker (``PUBLIC_ATTR`` kept)."""
    from sandloom.core.route_policy import PUBLIC_ATTR, PublicDecl
    from sandloom.sdk._decl import web as decl_web
    from sandloom.sdk.web import public

    marker = public(reason="probe", issues_tokens=True).dependency
    assert decl_web.PUBLIC_ATTR == PUBLIC_ATTR
    assert getattr(marker, PUBLIC_ATTR) == PublicDecl("probe", True)


def test_pure_data_types_are_real() -> None:
    ref: ContractRef[Any] = ContractRef("stats.quota.v1")
    assert ref.name == "stats.quota.v1"
    assert OptionalContract(ref).ref is ref
    account = ResolvedAccount(kind="org", id=__import__("uuid").uuid4())
    assert account.kind == "org"
