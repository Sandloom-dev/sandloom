"""Tenant kind from the assigned tenant authority's witness (ADR-0014
decision 13, owner #7; KB-31) and the witness 3 -> 2 memo (answer #23; KB-37).

Independent oracles: the membership provider is a fake contract that counts
what it was ASKED and confirms exactly the organizations it was told about;
the witness is read from describe documents shaped as ``sandloom generate``
writes them (the ``access_check`` declaration of a contract method).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

import sandloom.core.contract_compat as contract_compat
from sandloom.core.app_factory.tenant_witness import REASON as WITNESS_REASON
from sandloom.core.app_factory.tenant_witness import add_witness_consumes
from sandloom.core.claims import ClaimAuthorities
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import (
    ContractRequirement,
    ModuleDescriptor,
    ModuleLoadSummary,
)
from sandloom.core.di import bind_tenant
from sandloom.core.facade._tenant_witness import (
    TenantWitness,
    bound_tenant_policies,
    bound_tenant_routes,
    report,
    resolve_tenant_witness,
    witness_from_describe,
)
from sandloom.core.loader.manifest import AppManifest, module_manifest
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
from sandloom.shared.authz import Account, OrgRole, PolicyIndeterminate, Role
from sandloom.shared.authz import SubscriptionActive as KernelSubscriptionActive
from sandloom.shared.authz.account import (
    record_membership,
    resolve_account,
    tenant_kind,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

MEM = "tenancy.membership.v1"
ENT = "billing.entitlements.v1"
GRANTS = "grants.access.v1"


def _method(
    params: list[str],
    bind: dict[str, list[Any]],
    witness: dict[str, Any] | None = None,
    values: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    check: dict[str, Any] = {"name": "Check", "bind": bind, "category": "forbidden"}
    if values:
        check["values"] = values
    if witness is not None:
        check["witnesses_membership"] = witness
    return {
        "kind": "read",
        "params": [{"name": p, "kind": "positional_or_keyword"} for p in params],
        "access_check": check,
    }


ROLE_BIND: dict[str, list[Any]] = {
    "organization_id": ["org", "org_id", False],
    "user_id": ["subject"],
}
ROLE_PARAMS = ["organization_id", "user_id", "minimum"]
ROLE_VALUES = {"minimum": ["member", "admin", "owner"]}
HAS_ROLE = _method(ROLE_PARAMS, ROLE_BIND, {"minimum": "member"}, ROLE_VALUES)
WITNESS = TenantWitness(
    MEM,
    "has_role_at_least",
    tuple(ROLE_PARAMS),
    "organization_id",
    "user_id",
    {"minimum": "member"},
    authority="tenancy",
)


# --------------------------------------------------------------------------- #
# The declaration read from a describe document
# --------------------------------------------------------------------------- #
def test_the_witness_is_read_from_the_declaration() -> None:
    found = witness_from_describe(MEM, "has_role_at_least", HAS_ROLE)
    assert isinstance(found, TenantWitness)
    assert found.key() == WITNESS.key()
    org, user = uuid4(), uuid4()
    assert found.arguments(org, user) == [org, user, "member"]
    # no declaration: no witness
    plain = _method(ROLE_PARAMS, ROLE_BIND, None, ROLE_VALUES)
    assert witness_from_describe(MEM, "has_role_at_least", plain) is None


@pytest.mark.parametrize(
    ("doc", "why"),
    [
        (_method(ROLE_PARAMS, ROLE_BIND, {}, ROLE_VALUES), "own parameters"),
        (
            _method(ROLE_PARAMS, ROLE_BIND, {"minimum": "viewer"}, ROLE_VALUES),
            "not declared",
        ),
        (
            _method(
                ["organization_id", "minimum"],
                {"organization_id": ["org", "org_id", False]},
                {"minimum": "member"},
            ),
            "exactly one organization",
        ),
        (
            _method(
                ["account_type", "account_id", "user_id"],
                {
                    "account_type": ["account_type"],
                    "account_id": ["account_id"],
                    "user_id": ["subject"],
                },
                {},
            ),
            "exactly one organization",
        ),
        (
            _method(ROLE_PARAMS, ROLE_BIND, ["minimum"], ROLE_VALUES),  # type: ignore[arg-type]
            "not a mapping",
        ),
    ],
)
def test_an_unusable_declaration_is_named(doc: dict[str, Any], why: str) -> None:
    found = witness_from_describe(MEM, "has_role_at_least", doc)
    assert isinstance(found, str) and why in found, found


def test_the_declaration_binds_one_organization_and_one_subject() -> None:
    from sandloom.shared.authz.contract_check import From, access_check, access_check_of

    @access_check("Ok", bind=ROLE_BIND, witnesses_membership={"minimum": "member"})
    async def ok(organization_id: UUID, user_id: UUID, minimum: str) -> bool: ...

    spec = access_check_of(ok)
    assert spec is not None and spec["witnesses_membership"] == {"minimum": "member"}
    with pytest.raises(ValueError, match="exactly one"):
        access_check(
            "Bad", bind={"organization_id": From.org()}, witnesses_membership={}
        )
    with pytest.raises(ValueError, match="own parameter"):
        access_check(
            "Bad",
            bind=ROLE_BIND,
            witnesses_membership={"minimum": ["member"]},
        )


# --------------------------------------------------------------------------- #
# Resolution from the assigned authority (boot)
# --------------------------------------------------------------------------- #
class _Registry:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def list(self) -> list[str]:
        return list(self.names)


def _boot_core(
    authority: tuple[str, str] | None,
    contracts: dict[str, dict[str, Any]],
    *,
    assigned: dict[str, str] | None = None,
) -> Any:
    verified = {"tenant_ids": authority[0]} if authority else {}
    owners = {"tenant_ids": authority[1]} if authority else {}
    return SimpleNamespace(
        claim_authorities=ClaimAuthorities(
            authorities=verified, owners=owners, assigned=bool(authority)
        ),
        module_descriptors=[],
        service_registry=_Registry(sorted(contracts)),
        settings=SimpleNamespace(service_bindings={}),
        _access_assignment=assigned or {},
        app=None,
    )


@pytest.fixture
def expectations(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """Serve the describe documents of the test's contracts as the
    composition's expectations (as if read beside the consumer's stub)."""
    served: dict[str, dict[str, Any]] = {}

    def plan(core: Any, names: Any) -> Any:
        return contract_compat.ExpectationPlan(
            {
                n: (
                    contract_compat.Expectation(
                        {"methods": served[n]}, f"<copy of {n}>", None, "consumer"
                    ),
                )
                for n in names
                if n in served
            },
            {},
        )

    monkeypatch.setattr(contract_compat, "resolve_expectations", plan)
    return served


async def test_the_assigned_tenant_authoritys_witness_is_used(
    expectations: dict[str, dict[str, Any]],
) -> None:
    expectations[MEM] = {"has_role_at_least": HAS_ROLE}
    # a third-party contract declaring a witness is NOT the tenant authority's
    expectations["acme.seats.v1"] = {"has_seat": HAS_ROLE}
    core = _boot_core(("sandloom_tenancy", "tenancy"), expectations)
    witness, why = resolve_tenant_witness(core)
    assert witness is not None and witness.key() == WITNESS.key(), why
    assert witness.authority == "tenancy" and witness.source == f"<copy of {MEM}>"


async def test_no_assigned_authority_no_witness(
    expectations: dict[str, dict[str, Any]],
) -> None:
    expectations[MEM] = {"has_role_at_least": HAS_ROLE}
    witness, why = resolve_tenant_witness(_boot_core(None, expectations))
    assert witness is None and "tenant authority" in why


async def test_a_replacement_authority_without_a_membership_method(
    expectations: dict[str, dict[str, Any]],
) -> None:
    """``grants`` writes ``tenant_ids`` and declares no witness: tenancy's
    witness (still installed) does not count, the kind stays unknown."""
    expectations[MEM] = {"has_role_at_least": HAS_ROLE}
    expectations[GRANTS] = {
        "has_grant": _method(["user_id", "scope"], {"user_id": ["subject"]})
    }
    witness, why = resolve_tenant_witness(
        _boot_core(("acme_grants", "grants"), expectations)
    )
    assert witness is None
    assert "'grants' declares no membership witness" in why


async def test_a_contract_assigned_to_the_authority_counts(
    expectations: dict[str, dict[str, Any]],
) -> None:
    """``access_providers`` assigns ``tenancy.membership.v1`` to the
    replacement authority: its witness is the authority's."""
    expectations[MEM] = {"has_role_at_least": HAS_ROLE}
    core = _boot_core(("acme_orgs", "orgs"), expectations, assigned={MEM: "acme_orgs"})
    witness, _why = resolve_tenant_witness(core)
    assert witness is not None and witness.contract == MEM


async def test_two_witnesses_are_ambiguous(
    expectations: dict[str, dict[str, Any]],
) -> None:
    other = dict(HAS_ROLE, params=HAS_ROLE["params"])
    expectations[MEM] = {"has_role_at_least": HAS_ROLE, "is_member": other}
    witness, why = resolve_tenant_witness(
        _boot_core(("sandloom_tenancy", "tenancy"), expectations)
    )
    assert witness is None and "more than one" in why


# --------------------------------------------------------------------------- #
# tenant_kind: four kinds, with and without the witness
# --------------------------------------------------------------------------- #
class _Core:
    def __init__(self, members: set[UUID], witness: TenantWitness | None) -> None:
        self.members = members
        self.tenant_witness = witness
        self.calls: list[str] = []
        core = self

        async def has_role_at_least(org: UUID, user: UUID, minimum: str) -> bool:
            core.calls.append(f"has_role_at_least:{minimum}")
            return org in core.members

        async def check_subscription_active(tt: str, tid: UUID) -> bool:
            core.calls.append("check_subscription_active")
            return True

        self.services: dict[str, Any] = {
            MEM: SimpleNamespace(has_role_at_least=has_role_at_least),
            ENT: SimpleNamespace(check_subscription_active=check_subscription_active),
        }

    def get_service(self, name: str) -> Any:
        if name not in self.services:
            raise ServiceUnavailableInDeployment(name)
        return self.services[name]

    def get_metric(self, name: str) -> Any:
        return None


class _Req:
    def __init__(self, path: dict[str, str] | None = None) -> None:
        self.path_params = path or {}
        self.query_params: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.scope: dict[str, Any] = {"state": {}}


def _ctx(core: _Core, user: UUID, grants: list[UUID], **kw: Any) -> AuthzContext:
    subject = UserContext(
        user_id=user,
        tenant_ids=[str(t) for t in grants],
        roles=["admin"],
        claims={"subscription_status": "active"},
    )
    return AuthzContext(subject, request=_Req(**kw), core=core)  # type: ignore[arg-type]


async def test_four_kinds_with_the_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    user, org, stranger = uuid4(), uuid4(), uuid4()
    core = _Core({org}, WITNESS)
    ctx = _ctx(core, user, [org, stranger])
    assert await tenant_kind(user, ctx=ctx) == "personal"
    assert await tenant_kind(org, ctx=ctx) == "organization"
    # granted by tenant_ids but not a member: never an organization
    assert await tenant_kind(stranger, ctx=ctx) == "unknown"
    assert core.calls == ["has_role_at_least:member"] * 2
    monkeypatch.setattr(settings, "tenancy_mode", "single")
    assert await tenant_kind(org, ctx=_ctx(core, user, [org])) == "canonical"


async def test_tenant_ids_alone_is_never_an_organization() -> None:
    """No witness (no tenant authority, or one without a membership
    method): every granted organization's kind is unknown, without a call,
    and ``Account.bound_tenant()`` is indeterminate."""
    user, org = uuid4(), uuid4()
    core = _Core({org}, None)
    ctx = _ctx(core, user, [org])
    assert await tenant_kind(org, ctx=ctx) == "unknown"
    assert await tenant_kind(user, ctx=ctx) == "personal"
    assert core.calls == []
    with bind_tenant(org), pytest.raises(PolicyIndeterminate) as exc:
        await resolve_account(ctx, Account.bound_tenant(), requirement="t")
    assert exc.value.decision.code == "tenant_kind_unresolved"
    # the witness's provider absent: unknown as well
    absent = _Core({org}, WITNESS)
    del absent.services[MEM]
    assert await tenant_kind(org, ctx=_ctx(absent, user, [org])) == "unknown"


async def test_a_witness_answer_must_be_a_boolean_true() -> None:
    user, org = uuid4(), uuid4()
    core = _Core({org}, WITNESS)

    async def truthy(org_id: UUID, user_id: UUID, minimum: str) -> Any:
        return "yes"

    core.services[MEM] = SimpleNamespace(has_role_at_least=truthy)
    assert await tenant_kind(org, ctx=_ctx(core, user, [org])) == "unknown"


# --------------------------------------------------------------------------- #
# KB-37: an allowing witness check records the kind (3 -> 2)
# --------------------------------------------------------------------------- #
def _decisive() -> Any:
    return OrgRole("admin") & KernelSubscriptionActive(of=Account.bound_tenant())


async def _decide(core: _Core, user: UUID, org: UUID, bound: UUID) -> Any:
    ctx = _ctx(core, user, [org], path={"org_id": str(org)})
    with bind_tenant(bound):
        decision = await _decisive().decide(ctx)
    return (decision.allowed, decision.http_status, decision.code)


async def test_an_allowing_witness_check_saves_the_second_membership_call() -> None:
    user, org = uuid4(), uuid4()
    core = _Core({org}, WITNESS)
    assert (await _decide(core, user, org, org))[0] is True
    # decisive shape: one membership call, then the subscription
    assert core.calls == ["has_role_at_least:admin", "check_subscription_active"]
    # a denying check records nothing: the decision is unchanged (403)
    denied = _Core(set(), WITNESS)
    outcome = await _decide(denied, user, org, org)
    assert outcome[0] is False
    assert denied.calls == ["has_role_at_least:admin"]


async def test_decisions_equal_with_and_without_the_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sandloom.shared.authz.atoms_live as atoms_live

    user, org, other = uuid4(), uuid4(), uuid4()
    cases = [(members, bound) for members in ({org}, set()) for bound in (org, other)]
    for members, bound in cases:
        with_memo = await _decide(_Core(set(members), WITNESS), user, org, bound)
        monkeypatch.setattr(atoms_live, "record_membership", lambda *a, **k: None)
        base_core = _Core(set(members), WITNESS)
        without = await _decide(base_core, user, org, bound)
        monkeypatch.undo()
        assert with_memo == without, (members, bound)


async def test_only_the_authoritys_witness_records() -> None:
    user, org = uuid4(), uuid4()
    core = _Core({org}, WITNESS)
    ctx = _ctx(core, user, [org])
    record_membership(ctx, "acme.seats.v1", "has_role_at_least", org, user)
    record_membership(ctx, MEM, "has_role_at_least", org, uuid4())  # not the caller
    record_membership(ctx, MEM, "has_role_at_least", user, user)  # personal id
    assert ctx.request is not None
    assert ctx.request.scope["state"].get("_authz_tenant_kinds", {}) == {}
    assert await tenant_kind(org, ctx=ctx) == "organization"
    assert core.calls == ["has_role_at_least:member"]
    no_witness = _Core({org}, None)
    ctx = _ctx(no_witness, user, [org])
    record_membership(ctx, MEM, "has_role_at_least", org, user)
    assert await tenant_kind(org, ctx=ctx) == "unknown"


# --------------------------------------------------------------------------- #
# The startup report (H-19)
# --------------------------------------------------------------------------- #
def _app_with(policy: Any) -> Any:
    from fastapi import Depends, FastAPI

    from sandloom.shared.authz import require

    app = FastAPI()

    @app.get("/reports", dependencies=[Depends(require(policy))])
    async def reports() -> dict[str, str]:  # pragma: no cover - never served
        return {}

    return app


async def test_a_bound_tenant_policy_without_a_witness_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _app_with(Role("admin") | _decisive())
    [(route, _policy)] = bound_tenant_policies(app)
    assert route == "GET /reports"
    core = SimpleNamespace(app=app)
    with caplog.at_level(logging.WARNING, "sandloom.authz.tenant_witness"):
        report(core, None, "the tenant authority 'grants' declares no witness")
    [record] = [r for r in caplog.records if "Account.bound_tenant()" in r.message]
    assert "GET /reports" in record.message and "'grants'" in record.message
    caplog.clear()
    with caplog.at_level(logging.WARNING, "sandloom.authz.tenant_witness"):
        report(core, WITNESS, "")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert bound_tenant_policies(_app_with(Role("admin"))) == []


# --------------------------------------------------------------------------- #
# A bound_tenant() policy consumes the witness contract (KB-31b)
# --------------------------------------------------------------------------- #
def _composition(
    policies: dict[str, Any], consumes: dict[str, list[Any]] | None = None
) -> tuple[Any, ModuleLoadSummary]:
    """An app mounting one router per module (under a prefix, as the loader
    mounts a draft's router) and the load pass's summary and manifest."""
    from fastapi import APIRouter, Depends, FastAPI

    from sandloom.shared.authz import require

    app = FastAPI()
    summary = ModuleLoadSummary()
    for module, policy in policies.items():
        router = APIRouter()

        @router.get("/items", dependencies=[Depends(require(policy))])
        async def items() -> dict[str, str]:  # pragma: no cover - never served
            return {}

        prefix = f"/api/{module}"
        app.include_router(router, prefix=prefix)
        desc = ModuleDescriptor(
            name=module, version="1", consumes=list((consumes or {}).get(module, []))
        )
        summary.loaded.append(desc)
        draft = SimpleNamespace(routers=[(router, {"prefix": prefix})])
        summary.manifests[module] = module_manifest(desc, draft)
    return app, summary


def _witness_core(witness: TenantWitness | None) -> Any:
    return SimpleNamespace(
        tenant_witness=witness,
        service_registry=SimpleNamespace(contains=lambda name: name == MEM),
        settings=SimpleNamespace(service_bindings={}),
    )


def _add(witness: TenantWitness | None, app: Any, summary: Any) -> list[str]:
    core = _witness_core(witness)
    routes = bound_tenant_routes(app)
    return add_witness_consumes(core, summary, witness, routes)


def _consumed(summary: ModuleLoadSummary, module: str) -> list[tuple[Any, ...]]:
    entry = AppManifest.from_summary(summary).module(module)
    return [(u.name, u.required, u.methods) for u in entry.contracts_consumed]


async def test_a_bound_tenant_policy_consumes_the_witness_contract() -> None:
    app, summary = _composition({"reports": _decisive(), "notes": Role("admin")})
    changed = _add(WITNESS, app, summary)
    assert changed == ["reports"]
    reports, notes = summary.loaded
    [added] = [r for r in reports.consumes if r.name == MEM]
    assert added == ContractRequirement(
        MEM, required=False, reason=WITNESS_REASON, methods=("has_role_at_least",)
    )
    # optional: an absent provider keeps the policy indeterminate, never open
    assert (MEM, False, ("has_role_at_least",)) in _consumed(summary, "reports")
    assert {"contract": MEM, "required": False, "resolution": "local"} in (
        summary.consumes["reports"]
    )
    assert notes.consumes == [] and _consumed(summary, "notes") == []
    # once: a second pass finds the method already consumed
    assert _add(WITNESS, app, summary) == []


async def test_a_declared_use_of_the_witness_contract_is_merged() -> None:
    declared = {
        "narrow": [ContractRequirement(MEM, required=True, methods=("list_members",))],
        "whole": [ContractRequirement(MEM, required=True)],
    }
    app, summary = _composition({"narrow": _decisive(), "whole": _decisive()}, declared)
    assert _add(WITNESS, app, summary) == ["narrow"]
    narrow, whole = summary.loaded
    assert narrow.consumes == [
        ContractRequirement(
            MEM, required=True, methods=("list_members", "has_role_at_least")
        )
    ]
    assert whole.consumes == declared["whole"]  # no method list: every method


async def test_without_a_witness_nothing_is_consumed() -> None:
    app, summary = _composition({"reports": _decisive()})
    assert _add(None, app, summary) == []
    assert summary.loaded[0].consumes == []
