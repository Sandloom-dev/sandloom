"""R3.4: the module-level ``sandloom check`` rules catch the probe's static gaps.

Each rule is run over a small throwaway module and must report the mistake
(positive detection) and stay silent on the correct form and on what it
cannot decide.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import sandloom.core.archcheck.module_rules as mr
from sandloom.core.archcheck import analyzers as az
from sandloom.core.archcheck.manifest import load_rules
from sandloom.core.archcheck.runner import run_checks


def _write(root: Path, files: dict[str, str]) -> list[Path]:
    out = []
    for rel, src in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(src))
        out.append(path)
    return out


# --- event-schema-channels -------------------------------------------------
def test_unregistered_channel_is_reported(tmp_path: Path) -> None:
    files = _write(
        tmp_path,
        {
            "events.py": """
            CREATED = "notes.created.v1"
            EVENT_SCHEMAS = {CREATED: {"type": "object"}}
            """,
            "services/s.py": """
            from sandloom.core.di import publish_event
            from .. import events
            from ..events import CREATED

            async def go():
                await publish_event(events.CREATED, {})
                await publish_event(CREATED, {})
                await publish_event("notes.deleted.v1", {})
            """,
        },
    )
    hits = mr.find_unregistered_event_channels(tmp_path, files)
    assert [d.split(" —")[0] for _, d in hits] == [
        "Line 9: publish_event('notes.deleted.v1')"
    ]


def test_dynamic_schemas_are_undecidable(tmp_path: Path) -> None:
    files = _write(
        tmp_path,
        {
            "events.py": "EVENT_SCHEMAS = build()\n",
            "s.py": 'async def go():\n    await publish_event("x.y.v1", {})\n',
        },
    )
    assert mr.find_unregistered_event_channels(tmp_path, files) == []


# --- guard-order -----------------------------------------------------------
def test_guard_before_signature_auth_is_reported(tmp_path: Path) -> None:
    files = _write(
        tmp_path,
        {
            "api/x.py": """
            from fastapi import APIRouter, Depends
            router = APIRouter()

            @router.post("/a", dependencies=[Depends(idempotency_guard(scope="a"))])
            async def a(user_id=Depends(get_current_user_id)): ...

            @router.post("/b")
            async def b(user_id=Depends(get_current_user_id),
                        _g=Depends(idempotency_guard(scope="b"))): ...
            """
        },
    )
    hits = mr.find_guard_order_problems(files)
    assert [d.split(":")[0] for _, d in hits] == ["Line 5"]


def test_unknown_router_dependency_may_authenticate(tmp_path: Path) -> None:
    files = _write(
        tmp_path,
        {
            "api/x.py": """
            from fastapi import APIRouter, Depends
            router = APIRouter(dependencies=[Depends(require_platform_admin)])

            @router.post("/a", dependencies=[Depends(idempotency_guard(scope="a"))])
            async def a(user_id=Depends(get_current_user_id)): ...
            """
        },
    )
    assert mr.find_guard_order_problems(files) == []


def test_org_gate_before_tenant_scope_is_reported(tmp_path: Path) -> None:
    files = _write(
        tmp_path,
        {
            "api/x.py": """
            from fastapi import APIRouter, Depends
            router = APIRouter()
            GATE = require(SubscriptionActive(of=Account.org()))

            @router.post("/notes", dependencies=[Depends(GATE)])
            async def trap(tenant=TenantScope()): ...

            @router.post("/orgs/{org_id}/notes", dependencies=[Depends(GATE)])
            async def from_path(tenant=TenantScope()): ...

            @router.post("/ok")
            async def ok(tenant=TenantScope(), _=Depends(GATE)): ...

            @router.post("/personal",
                         dependencies=[Depends(require(SubscriptionActive(of=Account.user())))])
            async def personal(tenant=TenantScope()): ...
            """
        },
    )
    hits = mr.find_guard_order_problems(files)
    assert len(hits) == 1 and hits[0][1].startswith("Line 6: a require(...) gate")


# --- consumed-contracts ----------------------------------------------------
def test_undeclared_contract_is_reported(tmp_path: Path) -> None:
    files = _write(
        tmp_path,
        {
            "__init__.py": """
            register = define_module(
                __name__, consumes=[ContractRequirement("tenancy.membership.v1")]
            )
            """,
            "services/s.py": """
            def go(core):
                core.get_service("tenancy.membership.v1")
                core.get_service("quotas.read.v1")
                core.get_service("notes.self.v1")
                core.register_service("notes.self.v1", object())
            """,
        },
    )
    hits = mr.find_undeclared_contracts(files)
    assert [d.split(" is")[0] for _, d in hits] == [
        "Line 4: get_service('quotas.read.v1')"
    ]


# --- db-dependency-scope ---------------------------------------------------
def test_get_db_without_function_scope_is_reported(tmp_path: Path) -> None:
    (py,) = _write(
        tmp_path,
        {
            "api/x.py": """
            from fastapi import Depends
            async def a(db=Depends(get_db)): ...
            async def b(db=Depends(get_db, scope="function")): ...
            async def c(db=Depends(make_db_dependency("analytics"))): ...
            """
        },
    )
    hits = mr.find_unscoped_db_dependencies(py)
    assert [h.split(":")[0] for h in hits] == ["Line 3", "Line 5"]


# --- tenant-like columns (tenant-scoped-models) ----------------------------
def test_organization_id_without_tenant_scoped_is_reported(tmp_path: Path) -> None:
    (py,) = _write(
        tmp_path,
        {
            "models.py": """
            class Quota(Base):
                __tablename__ = "quotas_quotas"
                organization_id = mapped_column(UUID)

            class Scoped(TenantScoped, Base):
                __tablename__ = "quotas_scoped"
                organization_id = mapped_column(UUID)
            """
        },
    )
    assert az.tenant_column_hits(py) == [
        (2, "Quota", "quotas_quotas", "organization_id")
    ]


# --- the runner: waivers ---------------------------------------------------
def test_inline_waiver_needs_the_rule_and_a_reason(tmp_path: Path) -> None:
    (tmp_path / "architecture_rules.yml").write_text(
        "version: 1\nmodules_path: app/modules\nmodules_package: app.modules\n"
        "source_root: .\n"
    )
    _write(
        tmp_path / "app" / "modules",
        {
            "__init__.py": "",
            "notes/__init__.py": "",
            "notes/api/__init__.py": "",
            "notes/api/x.py": """
            from fastapi import Depends

            async def a(db=Depends(get_db)): ...  # sandloom: allow db-dependency-scope legacy probe

            async def b(db=Depends(get_db)): ...  # sandloom: allow db-dependency-scope
            """,
        },
    )
    (tmp_path / "app" / "__init__.py").write_text("")
    rules = load_rules(tmp_path / "architecture_rules.yml")
    found = run_checks(rules, only=["db-dependency-scope"])
    assert [v.line for v in found] == [6]


# --- module-boundaries: relative imports resolve against the file's package --
def test_relative_import_inside_the_module_is_not_cross_module(
    tmp_path: Path,
) -> None:
    """Regression (DX eval): billing's ``from .catalog import X`` (its own
    ``models/catalog.py``) was flagged once a business module named
    ``catalog`` existed — the name after the dots was compared with the
    sibling list instead of being resolved."""
    (tmp_path / "architecture_rules.yml").write_text(
        "version: 1\nmodules_path: app/modules\nmodules_package: app.modules\n"
        "source_root: .\n"
    )
    _write(
        tmp_path / "app",
        {
            "__init__.py": "",
            "modules/__init__.py": "",
            "modules/catalog/__init__.py": "",
            "modules/billing/__init__.py": "",
            "modules/billing/models/__init__.py": "from .catalog import Plan\n",
            "modules/billing/models/catalog.py": "class Plan: ...\n",
            "modules/billing/services.py": "from ..catalog import Thing\n",
            "modules/billing/models/deep.py": "from ...catalog import Other\n",
        },
    )
    rules = load_rules(tmp_path / "architecture_rules.yml")
    found = run_checks(rules, only=["module-boundaries"])
    assert sorted((v.file.name, v.line) for v in found) == [
        ("deep.py", 1),
        ("services.py", 1),
    ]
