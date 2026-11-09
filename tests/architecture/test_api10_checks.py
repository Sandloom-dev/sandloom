"""The Public API 1.0 static checks SANDLOOM-C021 … SANDLOOM-C027 (spec Appendix A) and C034.

Each analyzer on a minimal source; the runner wiring (severity, the
pending-until-unplanned gate, the tree-wide view check).
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandloom.core import error_codes
from sandloom.core.archcheck import api10_rules as a10
from sandloom.core.archcheck import atom_metadata
from sandloom.core.archcheck.manifest import load_rules
from sandloom.core.archcheck.runner import API10_RULES, PENDING_RULES, RULES, run_checks


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def test_every_api10_rule_has_its_appendix_a_code() -> None:
    codes = {
        "module-local-imports": "SANDLOOM-C021",
        "untagged-sensitive": "SANDLOOM-C022",
        "multi-store-waivers": "SANDLOOM-C023",
        "unsafe-tenant-query": "SANDLOOM-C024",
        "event-view-compat": "SANDLOOM-C025",
        "contract-union-hint": "SANDLOOM-C026",
        "account-route-mismatch": "SANDLOOM-C027",
        "handwritten-access-check": "SANDLOOM-C034",
    }
    assert set(API10_RULES) == set(codes) == set(a10.SEVERITY)
    for rule, code in codes.items():
        entry = error_codes.lookup(rule)
        assert entry is not None and entry.code == code
    # Gate: a rule is enforced exactly when its code is no longer planned.
    for rule in API10_RULES:
        planned = error_codes.lookup(rule).planned is not None  # type: ignore[union-attr]
        assert (rule in PENDING_RULES) == planned
        assert (rule in RULES) == (not planned)


def test_c021_function_local_imports(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "m.py",
        """
        from typing import TYPE_CHECKING
        import os

        if TYPE_CHECKING:
            from x import Y

        def f():
            import json
            from . import sibling
            import qrcode  # sandloom: optional-import the QR extra is optional
            return json, sibling, qrcode

        async def g():
            if TYPE_CHECKING:
                from z import W
        """,
    )
    hits = a10.find_module_local_imports(path)
    assert [h.split(":")[0] for h in hits] == ["Line 9", "Line 10"]
    assert "json" in hits[0] and "f()" in hits[0]


def test_c022_untagged_money_and_security_effects(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "s.py",
        """
        class Credits:
            @command(idempotency_key="k")
            async def spend(self, amount: int, k: str) -> int: ...

            @command(idempotency_key="k", risk="money")
            async def refund(self, amount: int, k: str) -> int: ...

            @command(idempotency_key="k")
            async def rename(self, name: str, k: str) -> int: ...

        async def change_password(user_id):
            revoke_user_tokens_on_commit(user_id, reason="password")

        @sensitive_effect("security")
        async def reset(user_id):
            revoke_user_tokens_on_commit(user_id, reason="reset")
        """,
    )
    hits = a10.find_untagged_sensitive(path)
    assert len(hits) == 2
    assert "spend()" in hits[0] and 'risk="money"' in hits[0]
    assert "change_password()" in hits[1]


_ORG_ROLE_STUB = """
class OrgRole(ContractCheck):
    __sandloom_atom__: ClassVar[dict[str, Any]] = {
        "contract": "orgs.membership.v1",
        "method": "has_role_at_least",
        "args": ["organization_id", "user_id", "minimum"],
        "binds": {"organization_id": ["org", "org_id", False], "user_id": ["subject"]},
        "tier": 3,
        "category": "forbidden",
        "values": {"minimum": ["member", "admin", "owner"]},
        "risk": {"admin": {"minimum": ["admin", "owner"]}},
    }
"""


@pytest.fixture
def atoms_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """``tmp_path`` on ``sys.path`` with fresh metadata caches: the rules
    resolve a name a file imports to the file that defines it."""
    monkeypatch.syspath_prepend(str(tmp_path))
    atom_metadata.clear_caches()
    yield tmp_path
    atom_metadata.clear_caches()


def test_c022_hand_rolled_authority_routes(atoms_on_path: Path) -> None:
    """The three hand-rolled authority shapes WS-A found in identity and
    tenancy are reported; admin-gated, tagged, public and read routes are not.
    The member / admin level of a gate is read from its atom's literal."""
    tmp_path = atoms_on_path
    _write(tmp_path, "orgs_stub.py", _ORG_ROLE_STUB)
    path = _write(
        tmp_path,
        "api.py",
        """
        from orgs_stub import OrgRole

        ROLE_MEMBER = "member"
        ROLE_ADMIN = "admin"
        router = APIRouter()
        admin_router = APIRouter(dependencies=[Depends(require_platform_admin)])

        @router.put("/users/{user_id}")
        async def update_user(user_id, actor_id=Depends(get_current_user_id)):
            if user_id != actor_id and not await user_has_admin_role(actor_id):
                raise HTTPException(status_code=403)

        @router.post("/accept", dependencies=[at_commit()])
        async def accept(user_id=Depends(get_current_user_id)): ...

        @router.delete("/{org_id}/members/{member_id}")
        async def remove(org_id, user_id=Depends(require(OrgRole(ROLE_MEMBER)))): ...

        @router.put("/x", dependencies=[sensitive("admin")])
        async def tagged():
            raise HTTPException(status.HTTP_403_FORBIDDEN)

        @router.delete("/{org_id}/members/{m}")
        async def admin_remove(org_id, u=Depends(require(OrgRole(ROLE_ADMIN)))):
            raise HTTPException(status.HTTP_403_FORBIDDEN)

        @admin_router.post("/users/{user_id}/block")
        async def block(user_id):
            await block_user(user_id)

        @router.get("/users/{user_id}")
        async def read(user_id):
            raise HTTPException(status_code=403)

        @router.post("/login", dependencies=[public(reason="credentials")])
        async def login():
            raise HTTPException(status_code=403)
        """,
    )
    hits = a10.find_untagged_sensitive(path)
    assert len(hits) == 3, hits
    assert "PUT /users/{user_id}" in hits[0] and "in its body" in hits[0]
    assert "POST /accept" in hits[1] and "at_commit()" in hits[1]
    assert "DELETE /{org_id}/members/{member_id}" in hits[2]
    assert all('sensitive("admin")' in h for h in hits)
    assert all("Depends(sensitive(" not in h for h in hits)


@pytest.mark.parametrize(
    "manifest",
    [
        "architecture_rules.yml",
        "examples/academy/architecture_rules.yml",
        "examples/helpdesk/architecture_rules.yml",
        "examples/linkly/architecture_rules.yml",
        "examples/projecthub/architecture_rules.yml",
    ],
)
def test_the_trees_pass_the_10_checks_once_unplanned(manifest: str) -> None:
    """Clearing ``planned`` for SANDLOOM-C021…C027 (a WS-A one-line change each)
    activates the rules in ``sandloom check``; the framework tree and the examples
    then report warnings and notes only, so un-planning breaks no gate."""
    path = Path(__file__).resolve().parents[2] / manifest
    if not path.is_file():
        pytest.skip(f"{manifest} not in this checkout")
    found = run_checks(load_rules(path), only=set(API10_RULES), pending=True)
    errors = [f"{v.location()}: {v.message}" for v in found if v.severity == "error"]
    assert errors == []


def test_c022_bundled_operations_have_no_unreviewed_sensitive_effects() -> None:
    """Built-in authority operations declare risk; the shared revocation
    mechanism has one local, reasoned waiver rather than a blanket exclusion."""
    rules = load_rules(Path(__file__).resolve().parents[2] / "architecture_rules.yml")
    found = run_checks(rules, only=["untagged-sensitive"], pending=True)
    assert not found, [f"{v.location()}: {v.message}" for v in found]
    session_helper = rules.modules_path / "sandloom_identity" / "sessions.py"
    raw_hits = a10.find_untagged_sensitive(session_helper)
    assert len(raw_hits) == 1
    assert "revoke_sessions()" in raw_hits[0]


def test_c023_lists_waivers_and_refuses_a_missing_reason(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "a.py",
        """
        deps = [allow_multi_db_write(secondaries=("analytics",), reason="metrics copy")]
        bad = allow_multi_db_write(secondaries=("x",), reason="")
        """,
    )
    assert [level for level, _ in a10.find_multi_store_waivers(path)] == [
        "info",
        "error",
    ]


def test_c024_raw_queries_need_unscoped(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "q.py",
        """
        async def f(store):
            await store.raw_query({"a": 1})
            with unscoped():
                await store.raw_query({"a": 1})
            await store.get("k")
        """,
    )
    hits = a10.find_unsafe_tenant_queries(path)
    assert len(hits) == 1 and hits[0].startswith("Line 3")


def test_c026_ambiguous_unions_in_contracts(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "c.py",
        """
        @contract("x.y.v1")
        class P:
            async def by_ref(self, ref: UUID | str) -> int: ...
            async def fine(self, ref: UUID | None) -> int: ...
            async def _private(self, ref: int | str) -> int: ...

        class L2:
            @classmethod
            @contract_method
            async def legacy(cls, key: int | str) -> int: ...
        """,
    )
    hits = a10.find_contract_union_hints(path)
    assert len(hits) == 2
    assert "P.by_ref" in hits[0] and "UUID | str" in hits[0]
    assert "L2.legacy" in hits[1]


def test_c027_organization_routes_and_the_personal_account(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "r.py",
        """
        from sandloom.sdk import Credits, Subscribed, SubscriptionActive
        from sandloom.sdk.authz import Feature

        @router.get(
            "/orgs/{org_id}/export",
            dependencies=[Depends(require(Subscribed(of=Account.user())))],
        )
        async def export(org_id: UUID): ...

        @router.get(
            "/orgs/{org_id}/mine",
            dependencies=[Depends(require(Credits(1, of=Account.user(reason="per-seat"))))],
        )
        async def mine(org_id: UUID): ...

        @router.get(
            "/orgs/{org_id}/ok",
            dependencies=[Depends(require(SubscriptionActive(of=Account.org(param="org_id"))))],
        )
        async def ok(org_id: UUID): ...

        @router.get("/me", dependencies=[Depends(require(Subscribed(of=Account.user())))])
        async def me(): ...

        @router.get("/teams", dependencies=[Depends(require(Feature("x", of=Account.org(param="team_id"))))])
        async def teams(): ...
        """,
    )
    hits = a10.find_account_route_mismatches(path)
    assert len(hits) == 2
    assert "/orgs/{org_id}/export" in hits[0] and "Account.user()" in hits[0]
    assert "team_id" in hits[1] and "/teams" in hits[1]


def _tree(tmp_path: Path) -> Path:
    """A minimal app tree: a producer module, a consumer module, an exported schema."""
    app = tmp_path / "app"
    _write(app, "app/__init__.py", "")
    _write(app, "app/shared/__init__.py", "")
    _write(app, "app/modules/__init__.py", "")
    _write(app, "app/modules/notes/__init__.py", "")
    _write(
        app,
        "app/modules/notes/events.py",
        """
        class NoteCreated(Event, channel="notes.note.created.v1"):
            note_id: UUID
            tenant_id: UUID
            title: str = ""
        """,
    )
    _write(app, "app/modules/stats/__init__.py", "")
    _write(
        app,
        "app/modules/stats/consumers.py",
        """
        class NoteCreated(Event, channel="notes.note.created.v1"):
            tenant_id: UUID
            title: str

        class Paid(Event, channel="billing.payment.done.v1"):
            amount: int

        class Gone(Event, channel="ghost.thing.v1"):
            x: int

        async def bad():
            await NoteCreated(tenant_id=1, title="x").publish()
        """,
    )
    events = app / "app" / "shared" / "events"
    events.mkdir(parents=True)
    (events / "billing.payment.done.v1.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"amount": {"type": "string"}},
                "required": ["amount"],
            }
        )
    )
    _write(
        app,
        "architecture_rules.yml",
        """
        version: 1
        modules_path: app/modules
        modules_package: app.modules
        source_root: .
        """,
    )
    return app


def test_c025_views_are_checked_against_the_producer(tmp_path: Path) -> None:
    app = _tree(tmp_path)
    rules = load_rules(app / "architecture_rules.yml")
    found = run_checks(rules, only=["event-view-compat"], pending=True)
    by_level = sorted((v.severity, v.message) for v in found)
    errors = [m for level, m in by_level if level == "error"]
    warnings = [m for level, m in by_level if level == "warning"]
    assert any("only the producing module publishes it" in m for m in errors)
    assert any("'title' is optional in the producer" in m for m in errors)
    assert any("'amount' is int here but string" in m for m in errors)
    assert len(warnings) == 1 and "ghost.thing.v1" in warnings[0]


def test_warnings_and_notes_do_not_fail_sandloom_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandloom.core.archcheck.runner import Violation
    from sandloom.core.cli.check import _report

    rules = load_rules(_tree(tmp_path) / "architecture_rules.yml")
    note = Violation("contract-union-hint", rules.root / "x.py", "Line 1: hint", "info")
    assert _report(rules, [note], [], boot_requested=False) == 0
    error = Violation("unsafe-tenant-query", rules.root / "x.py", "Line 1: raw")
    assert _report(rules, [note, error], [], boot_requested=False) == 1
    out = capsys.readouterr().out
    assert "info[SANDLOOM-C026" in out and "error[SANDLOOM-C024" in out


def test_c025_sees_fields_inherited_from_an_abstract_base(tmp_path: Path) -> None:
    """A view's or producer's fields include those of a same-file base."""
    path = _write(
        tmp_path,
        "events.py",
        """
        class Base(Event):
            tenant_id: UUID
            note: str = ""

        class Created(Base, channel="notes.note.created.v1"):
            note_id: UUID
            note: str

        class Deep(Created, channel="notes.note.deep.v1"):
            extra: int = 0
        """,
    )
    found = {e.channel: e for e in a10.event_classes(path)}
    created = found["notes.note.created.v1"]
    assert created.fields == {"tenant_id": "UUID", "note": "str", "note_id": "UUID"}
    assert created.optional == set()  # the override made ``note`` required
    deep = found["notes.note.deep.v1"]
    assert set(deep.fields) == {"tenant_id", "note", "note_id", "extra"}
    assert deep.optional == {"extra"}


# --------------------------------------------------------------------------- #
# archcheck from atom metadata (H-20, answer #22): the Z-8 fixture
# --------------------------------------------------------------------------- #
_FIXTURES = Path(__file__).parent / "_fixtures"


def _z8_consumer(root: Path) -> Path:
    """The Z-8 consumer app on ``root``: a third-party provider's generated
    stub (``ProjectRole``, ``SeatsAvailable``) and a module's ``api.py``."""
    contracts = root / "z8app" / "shared" / "contracts"
    reports = root / "z8app" / "modules" / "reports"
    for package in (root / "z8app", root / "z8app" / "shared", contracts, reports):
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("")
    stub = (_FIXTURES / "z8_stub.py.txt").read_text(encoding="utf-8")
    (contracts / "projects_roles_v1.py").write_text(stub, encoding="utf-8")
    api = reports / "api.py"
    api.write_text(
        (_FIXTURES / "z8_reports_api.py.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return api


def test_z8_gate_order_reads_the_literal_and_module_router(
    atoms_on_path: Path,
) -> None:
    """A third-party atom with an organization bind is found by the gate-order
    rule with no name in a list: through an alias, and on a ``module_router()``
    route (the pre-existing gap: only ``APIRouter`` was read)."""
    from sandloom.core.archcheck import module_rules as mr

    api = _z8_consumer(atoms_on_path)
    hits = [hit for _path, hit in mr.find_guard_order_problems([api])]
    assert len(hits) == 2, hits
    assert "(board)" in hits[0] and "TenantScope()" in hits[0]
    assert "(board_two)" in hits[1]


def test_z8_c027_account_atom_from_its_literal(atoms_on_path: Path) -> None:
    api = _z8_consumer(atoms_on_path)
    hits = a10.find_account_route_mismatches(api)
    assert len(hits) == 1, hits
    assert "/orgs/{org_id}/seats" in hits[0]
    assert "SeatsAvailable(of=Account.user())" in hits[0]


def test_z8_sensitive_route_classified_from_the_risk_table(
    atoms_on_path: Path,
) -> None:
    """``ProjectRole("lead")`` carries the admin risk, ``("viewer")`` does not:
    the write route behind the viewer gate decides an authority by hand."""
    api = _z8_consumer(atoms_on_path)
    hits = a10.find_untagged_sensitive(api)
    assert len(hits) == 1, hits
    assert "POST /orgs/{org_id}/notes" in hits[0]


def test_z8_handwritten_access_check_c034(atoms_on_path: Path) -> None:
    """``CheapLead`` calls the declared check with a lower tier and without its
    risk; ``HonestLead`` states the declared metadata and is not reported."""
    api = _z8_consumer(atoms_on_path)
    hits = a10.find_handwritten_access_checks(api)
    assert len(hits) == 2, hits
    assert all(
        "CheapLead" in h and "projects.roles.v1.has_project_role" in h for h in hits
    )
    assert "tier 1 (declared 3)" in hits[0]
    assert "does not state its declared risk ['admin'] (risks())" in hits[1]
    assert not any("HonestLead" in h for h in hits)


def test_a_reexported_atom_is_found_where_it_is_defined(atoms_on_path: Path) -> None:
    api = _z8_consumer(atoms_on_path)
    contracts = atoms_on_path / "z8app" / "shared" / "contracts"
    (contracts / "__init__.py").write_text(
        "from .projects_roles_v1 import ProjectRole as Reexported\n"
    )
    other = _write(
        atoms_on_path,
        "other.py",
        """
        from z8app.shared.contracts import Reexported
        from z8app.shared import contracts as c

        @router.get("/orgs/{org_id}/seats", dependencies=[Depends(require(Reexported("lead")))])
        async def a(org_id): ...

        @router.post("/orgs/{org_id}/x", dependencies=[Depends(require(c.Reexported("lead")))])
        async def b(org_id):
            raise HTTPException(status_code=403)
        """,
    )
    assert api.is_file()
    assert a10.find_untagged_sensitive(other) == []
    (contracts / "__init__.py").write_text("")  # nothing re-exported any more
    atom_metadata.clear_caches()
    assert len(a10.find_untagged_sensitive(other)) == 1


def test_a_name_alone_is_not_an_atom(atoms_on_path: Path) -> None:
    """A class called ``OrgRole`` without a generated literal binds nothing the
    rules can read: no gate-order or account report, and no tag."""
    from sandloom.core.archcheck import module_rules as mr

    _write(atoms_on_path, "plain.py", "class OrgRole:\n    pass\n")
    api = _write(
        atoms_on_path,
        "api.py",
        """
        from plain import OrgRole

        @router.post("/x", dependencies=[Depends(require(OrgRole("admin")))])
        async def x(tenant=TenantScope()):
            raise HTTPException(status_code=403)
        """,
    )
    assert mr.find_guard_order_problems([api]) == []
    assert len(a10.find_untagged_sensitive(api)) == 1


def test_no_atom_name_list_remains() -> None:
    """B-N17: neither a billing or tenancy atom list nor tenancy's function."""
    from sandloom.core.archcheck import module_rules as mr

    assert not hasattr(a10, "BILLING_ATOMS")
    assert not hasattr(mr, "ORG_ATOMS") and not hasattr(mr, "ORG_IF_ORGANIZATION")
    for module in (a10, mr):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "require_org_role" not in source
