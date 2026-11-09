"""Access-bearing contract providers, monolith and split (ADR-0014 decisions 6, 14).

Two third-party modules, no battery involved, over one ledger:

- ``acme_billing`` serves ``acmebill.plans.v1``, whose boolean reads declare
  the access checks ``SubscriptionActive`` and ``Credits`` (the shape of the
  kernel atoms of those names), and ``billing.entitlements.v1`` (the contract
  the kernel atoms ask today);
- ``acme_orgs`` serves ``acmeorgs.roles.v1``, declaring ``OrgRole``, and
  ``tenancy.membership.v1`` (the contract the kernel ``OrgRole`` asks today).

The consumer's atoms are the ones ``sandloom generate`` renders beside the
stubs, imported as a consumer imports them. Two in-process apps wired like a
split deployment, over the real internal RPC (proxy -> ``/_sandloom/rpc`` ->
contract runtime, ASGI transport):

- **acme** loads both modules under the composition root's assignment
  (``access_providers``) and serves the RPC routes; without the assignment the
  provider process refuses the registration (SANDLOOM-B034);
- **api** loads no provider and binds every contract through
  ``SERVICE_BINDINGS``, which is the remote assignment: only the pinned
  ``service_id`` answers (pair-MAC authenticated). A process holding other
  keys behind the same URL never decides access.

G-AUTHZ-05 (decision 14): the third party's generated atoms decide what
today's kernel atoms decide on the same inputs, in the provider process
(in-process call) and in the consumer (RPC): allow, deny, every indeterminate
(400, 403, 501), and a provider that is down is 503 both ways.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

import sandloom.shared.authz as kernel
from sandloom.core import rpc
from sandloom.core.app_factory import access_providers as _access
from sandloom.core.cli.generated import render_stub
from sandloom.core.contract_testing import rpc_settings
from sandloom.core.facade import CoreFacade
from sandloom.core.facade._services import ForeignOwnerRegistration
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc.errors import RemoteServiceUnavailable
from sandloom.sdk import contract
from sandloom.shared.authz import (
    Account,
    And,
    Category,
    From,
    Not,
    Or,
    Requirement,
    access_check,
)
from sandloom.shared.authz import contract_check as _contract_check
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.results import AccessDecision
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

LOG = logging.getLogger("test.access_providers_split")
MEMBERSHIP = "tenancy.membership.v1"
ENTITLEMENTS = "billing.entitlements.v1"
PLANS = "acmebill.plans.v1"
ROLES_CONTRACT = "acmeorgs.roles.v1"
ALL = (MEMBERSHIP, ENTITLEMENTS, PLANS, ROLES_CONTRACT)
ASSIGNED = {
    MEMBERSHIP: "acme_orgs",
    ROLES_CONTRACT: "acme_orgs",
    ENTITLEMENTS: "acme_billing",
    PLANS: "acme_billing",
}
USER = uuid4()
ORG = uuid4()
OTHER_ORG = uuid4()
RANKS = {"member": 0, "admin": 1, "owner": 2}

# The one ledger both contract shapes read.
ROLE = {(ORG, USER): "admin"}
RIVAL_ROLE = {(ORG, USER): "member"}
ACTIVE = {("organization", ORG)}
BALANCE = {("user", USER): 7, ("organization", ORG): 3}


def _ranks(
    table: dict[tuple[UUID, UUID], str], org: UUID, user: UUID, minimum: str
) -> bool:
    role = table.get((org, user))
    return role is not None and RANKS[role] >= RANKS[minimum]


# --------------------------------------------------------------------------- #
# The third party's declared contracts (what `sandloom generate` renders from)
# --------------------------------------------------------------------------- #
@contract(PLANS)
class PlansContract:
    """Subscriptions and credits of an account (a third-party provider)."""

    @access_check(
        "SubscriptionActive",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.PAYMENT_REQUIRED,
        describe="active subscription",
    )
    async def subscription_active(self, tenant_type: str, tenant_id: UUID) -> bool:
        """True while the account's plan grants access."""
        return True

    @access_check(
        "Credits",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.PAYMENT_REQUIRED,
        describe="at least {minimum} credits",
    )
    async def has_credits_at_least(
        self, tenant_type: str, tenant_id: UUID, minimum: int
    ) -> bool:
        """True when the account's balance is at least ``minimum``."""
        return True


@contract(ROLES_CONTRACT)
class RolesContract:
    """Organization roles (a third-party provider)."""

    @access_check(
        "OrgRole",
        bind={
            "organization_id": From.org(param="org_id", validate_against_token=False),
            "user_id": From.subject_id(),
        },
        values={"minimum": ["member", "admin", "owner"]},
        risk={"admin": {"minimum": ["admin", "owner"]}},
        describe="organization role '{minimum}'",
    )
    async def has_role_at_least(
        self, organization_id: UUID, user_id: UUID, minimum: str
    ) -> bool:
        """True when the user's role ranks at least ``minimum``."""
        return True


# --------------------------------------------------------------------------- #
# The implementations the provider process registers
# --------------------------------------------------------------------------- #
class _AcmeMembership:
    """``acme_orgs``'s implementation of today's membership contract."""

    @classmethod
    @contract_method
    async def has_role_at_least(cls, org_id: UUID, user_id: UUID, minimum: str) -> bool:
        return _ranks(ROLE, org_id, user_id, minimum)


class _AcmeRoles:
    """``acme_orgs``'s implementation of its own declared contract."""

    @classmethod
    @contract_method
    async def has_role_at_least(
        cls, organization_id: UUID, user_id: UUID, minimum: str
    ) -> bool:
        return _ranks(ROLE, organization_id, user_id, minimum)


class _RivalRoles:
    """``rival_orgs``: the same contract, a different ledger."""

    @classmethod
    @contract_method
    async def has_role_at_least(
        cls, organization_id: UUID, user_id: UUID, minimum: str
    ) -> bool:
        return _ranks(RIVAL_ROLE, organization_id, user_id, minimum)


class _RivalMembership:
    @classmethod
    @contract_method
    async def has_role_at_least(cls, org_id: UUID, user_id: UUID, minimum: str) -> bool:
        return _ranks(RIVAL_ROLE, org_id, user_id, minimum)


class _AcmeEntitlements:
    """``acme_billing``'s implementation of today's entitlements contract."""

    @classmethod
    @contract_method
    async def check_subscription_active(cls, tenant_type: str, tenant_id: UUID) -> bool:
        return (tenant_type, tenant_id) in ACTIVE

    @classmethod
    @contract_method
    async def get_credit_balance(cls, tenant_type: str, tenant_id: UUID) -> int:
        return BALANCE.get((tenant_type, tenant_id), 0)


class _AcmePlans:
    """``acme_billing``'s implementation of its own declared contract."""

    @classmethod
    @contract_method
    async def subscription_active(cls, tenant_type: str, tenant_id: UUID) -> bool:
        return (tenant_type, tenant_id) in ACTIVE

    @classmethod
    @contract_method
    async def has_credits_at_least(
        cls, tenant_type: str, tenant_id: UUID, minimum: int
    ) -> bool:
        return BALANCE.get((tenant_type, tenant_id), 0) >= minimum


MODULES: dict[str, dict[str, Any]] = {
    "acme_billing": {ENTITLEMENTS: _AcmeEntitlements, PLANS: _AcmePlans},
    "acme_orgs": {MEMBERSHIP: _AcmeMembership, ROLES_CONTRACT: _AcmeRoles},
    "rival_orgs": {MEMBERSHIP: _RivalMembership, ROLES_CONTRACT: _RivalRoles},
}


# --------------------------------------------------------------------------- #
# The two atom kits: the kernel's and the generated ones
# --------------------------------------------------------------------------- #
KERNEL = SimpleNamespace(
    SubscriptionActive=kernel.SubscriptionActive,
    Credits=kernel.Credits,
    OrgRole=kernel.OrgRole,
)


def _import_stub(tmp_path: Path, name: str, provider: type) -> ModuleType:
    path = tmp_path / f"{name.replace('.', '_')}.py"
    path.write_text(render_stub(name, provider))
    spec = importlib.util.spec_from_file_location(f"_kb34a_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gen(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """The consumer's generated atoms of the third party's contracts."""
    plans = _import_stub(tmp_path, PLANS, PlansContract)
    roles = _import_stub(tmp_path, ROLES_CONTRACT, RolesContract)
    kit = SimpleNamespace(
        SubscriptionActive=plans.SubscriptionActive,
        Credits=plans.Credits,
        OrgRole=roles.OrgRole,
    )
    yield kit
    for module in (plans, roles):
        sys.modules.pop(module.__name__, None)
    for cls in vars(kit).values():  # leave no access-bearing name behind
        _contract_check._GENERATED.discard(cls)


# --------------------------------------------------------------------------- #
# The processes
# --------------------------------------------------------------------------- #
class _FakeUoW:
    session = SimpleNamespace(info={})

    async def __aenter__(self) -> _FakeUoW:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeDatabase:
    def uow_factory(self) -> Callable[[], _FakeUoW]:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _provider(
    access_providers: dict[str, str] | None,
    modules: tuple[str, ...] = ("acme_billing", "acme_orgs"),
) -> tuple[FastAPI, CoreFacade]:
    """The provider process, built in ``create_app``'s order: the root's
    assignment, the module load, the post-load sweep, the RPC routes."""
    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    _access.install_access_providers(core, access_providers=access_providers, log=LOG)
    for module in modules:
        with core.module_registration(module) as api:
            for name, impl in MODULES[module].items():
                api.register_service(name, impl)
    _access.enforce_access_providers(core, log=LOG)
    rpc.install_service_rpc_routes(app, core)
    return app, core


def _consumer(
    service_id: str, peers: tuple[str, ...], bound: tuple[str, ...] = ALL
) -> CoreFacade:
    """The api process: no provider, every contract of ``bound`` bound to
    ``service_id``."""
    from sandloom.core.config.settings import settings

    app = FastAPI()
    cfg = settings.model_copy(
        update={
            **rpc_settings("api", peers=peers),
            "service_bindings": {
                name: {"base_url": "http://acme", "service_id": service_id}
                for name in bound
            },
        }
    )
    core = CoreFacade(app=app, settings_obj=cfg)
    app.state.core = core
    _access.install_access_providers(core, access_providers=None, log=LOG)
    _access.enforce_access_providers(core, log=LOG)

    def resolve(
        service_name: str,
        binding: dict[str, object],
        *,
        default_timeout_seconds: float,
        settings: object = None,
    ) -> rpc.RemoteServiceProxy:
        return rpc.RemoteServiceProxy(
            service_name=service_name,
            binding=binding,
            default_timeout_seconds=default_timeout_seconds,
            settings=cfg,
        )

    core.set_service_resolver(resolve)
    return core


def _serve(
    monkeypatch: pytest.MonkeyPatch, provider_id: str
) -> tuple[CoreFacade, httpx.AsyncClient]:
    """Key the provider process as ``provider_id`` and route RPC to it."""
    from sandloom.core.config.settings import settings

    keys = rpc_settings(provider_id, peers=("api",), allowed_calls={"api": list(ALL)})
    for name, value in keys.items():
        monkeypatch.setattr(settings, name, value)
    app, core = _provider(ASSIGNED)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://acme"
    )
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: client)
    return core, client


@pytest.fixture
def split(monkeypatch: pytest.MonkeyPatch) -> dict[str, CoreFacade]:
    provider, _client = _serve(monkeypatch, "acme")
    return {"provider": provider, "consumer": _consumer("acme", ("acme",))}


# --------------------------------------------------------------------------- #
# Requests and decisions
# --------------------------------------------------------------------------- #
_NO_ORG = object()


def _ctx(core: Any, org: Any = _NO_ORG, granted: tuple[UUID, ...] = ()) -> AuthzContext:
    """``org`` is the ``org_id`` path value (a UUID, a raw string, or none);
    ``granted`` the organizations the token grants."""
    subject = UserContext.model_validate(
        {"sub": str(USER), "tenant_ids": [str(g) for g in granted]}
    )
    path = {} if org is _NO_ORG else {"org_id": str(org)}
    request: Any = SimpleNamespace(
        path_params=path,
        query_params={},
        headers={},
        scope={"state": {}},
        state=SimpleNamespace(),
        app=None,
    )
    return AuthzContext(subject, core=core, request=request)


def _key(d: AccessDecision) -> tuple[Any, ...]:
    return (
        d.allowed,
        d.http_status,
        d.category,
        d.code,
        d.indeterminate,
        d.failed_requirement,
    )


async def _outcome(policy: Requirement, ctx: AuthzContext) -> tuple[Any, ...]:
    """The decision, or the status an error carries to the caller."""
    try:
        return _key(await policy.decide(ctx))
    except RemoteServiceUnavailable as exc:
        return ("raised", exc.status_code)


Policy = Callable[[SimpleNamespace], Requirement]
USER_ACCOUNT = Account.user()
ORG_ACCOUNT = Account.org()

#: (policy over a kit, org path value, granted orgs, (allowed, status, indeterminate))
CASES: dict[str, tuple[Policy, Any, tuple[UUID, ...], tuple[bool, int, bool]]] = {
    # SubscriptionActive
    "sub-user-inactive": (
        lambda k: k.SubscriptionActive(of=USER_ACCOUNT),
        _NO_ORG,
        (),
        (False, 402, False),
    ),
    "sub-org-active": (
        lambda k: k.SubscriptionActive(of=ORG_ACCOUNT),
        ORG,
        (ORG,),
        (True, 200, False),
    ),
    "sub-org-inactive": (
        lambda k: k.SubscriptionActive(of=ORG_ACCOUNT),
        OTHER_ORG,
        (OTHER_ORG,),
        (False, 402, False),
    ),
    "sub-org-not-granted": (
        lambda k: k.SubscriptionActive(of=ORG_ACCOUNT),
        ORG,
        (),
        (False, 403, True),
    ),
    "sub-org-invalid": (
        lambda k: k.SubscriptionActive(of=ORG_ACCOUNT),
        "not-a-uuid",
        (ORG,),
        (False, 400, True),
    ),
    "not-sub-org-missing": (
        lambda k: Not(k.SubscriptionActive(of=ORG_ACCOUNT)),
        _NO_ORG,
        (ORG,),
        (False, 400, True),
    ),
    # Credits
    "credits-user-enough": (
        lambda k: k.Credits(5, of=USER_ACCOUNT),
        _NO_ORG,
        (),
        (True, 200, False),
    ),
    "credits-user-short": (
        lambda k: k.Credits(10, of=USER_ACCOUNT),
        _NO_ORG,
        (),
        (False, 402, False),
    ),
    "credits-org-exact": (
        lambda k: k.Credits(3, of=ORG_ACCOUNT),
        ORG,
        (ORG,),
        (True, 200, False),
    ),
    "credits-org-short": (
        lambda k: k.Credits(4, of=ORG_ACCOUNT),
        ORG,
        (ORG,),
        (False, 402, False),
    ),
    "credits-org-missing": (
        lambda k: k.Credits(1, of=ORG_ACCOUNT),
        _NO_ORG,
        (),
        (False, 400, True),
    ),
    # OrgRole
    "role-member": (lambda k: k.OrgRole("member"), ORG, (), (True, 200, False)),
    "role-admin": (lambda k: k.OrgRole("admin"), ORG, (), (True, 200, False)),
    "role-owner": (lambda k: k.OrgRole("owner"), ORG, (), (False, 403, False)),
    "role-other-org": (
        lambda k: k.OrgRole("member"),
        OTHER_ORG,
        (),
        (False, 403, False),
    ),
    "not-role-owner": (lambda k: Not(k.OrgRole("owner")), ORG, (), (True, 200, False)),
    "role-invalid": (lambda k: k.OrgRole("member"), "nope", (), (False, 400, True)),
    "not-role-missing": (
        lambda k: Not(k.OrgRole("member")),
        _NO_ORG,
        (),
        (False, 400, True),
    ),
    # composed
    "owner-or-credits": (
        lambda k: Or(k.OrgRole("owner"), k.Credits(5, of=USER_ACCOUNT)),
        ORG,
        (ORG,),
        (True, 200, False),
    ),
    "admin-and-org-sub": (
        lambda k: And(k.OrgRole("admin"), k.SubscriptionActive(of=ORG_ACCOUNT)),
        OTHER_ORG,
        (OTHER_ORG,),
        (False, 403, False),
    ),
}


# --------------------------------------------------------------------------- #
# G-CONTRACT-24: the provider assignment, both sides of a split
# --------------------------------------------------------------------------- #
async def test_the_provider_process_refuses_an_unassigned_third_party() -> None:
    """Split boot: the ``acme`` process loads ``acme_orgs`` with no assignment."""
    with pytest.raises(ForeignOwnerRegistration, match="SANDLOOM-B034"):
        _provider(None, ("acme_orgs",))


async def test_the_provider_process_refuses_an_app_local_namesake() -> None:
    """X3 in the provider process: ``myapp.modules.acme_orgs`` is not it."""
    core = CoreFacade(app=FastAPI())
    _access.install_access_providers(
        core, access_providers={MEMBERSHIP: "acme_orgs"}, log=LOG
    )
    with (
        pytest.raises(ForeignOwnerRegistration, match="'acme_orgs'"),
        core.module_registration("myapp.modules.acme_orgs") as api,
    ):
        api.register_service(MEMBERSHIP, _AcmeMembership)


@pytest.mark.parametrize(
    ("org_id", "minimum", "allowed"),
    [
        (ORG, "member", True),
        (ORG, "admin", True),
        (ORG, "owner", False),
        (OTHER_ORG, "member", False),
    ],
)
async def test_an_assigned_provider_decides_the_same_mono_and_split(
    split: dict[str, CoreFacade],
    gen: SimpleNamespace,
    org_id: UUID,
    minimum: str,
    allowed: bool,
) -> None:
    """The third party's generated ``OrgRole`` decides the same in the
    provider process (in-process call) and in the consumer (RPC)."""
    policy = gen.OrgRole(minimum)
    mono = await policy.decide(_ctx(split["provider"], org_id))
    remote = await policy.decide(_ctx(split["consumer"], org_id))
    assert _key(remote) == _key(mono)
    assert mono.allowed is allowed


async def test_only_the_pinned_service_id_answers_remotely(
    monkeypatch: pytest.MonkeyPatch, gen: SimpleNamespace
) -> None:
    """The binding pins ``acme``; a process keyed ``impostor`` behind the same
    URL serves an assigned provider, yet its answer is never taken: the call
    is refused (401 at the provider, 503 to the caller), negation included,
    never an allow."""
    _serve(monkeypatch, "impostor")
    consumer = _consumer("acme", ("acme", "impostor"))
    for kit in (KERNEL, gen):
        for policy in (
            kit.OrgRole("member"),
            ~kit.OrgRole("member"),
            kit.Credits(1, of=USER_ACCOUNT),
            ~kit.SubscriptionActive(of=USER_ACCOUNT),
        ):
            with pytest.raises(RemoteServiceUnavailable) as exc:
                await policy.decide(_ctx(consumer, ORG))
            assert exc.value.status_code == 503


# --------------------------------------------------------------------------- #
# G-AUTHZ-05: a third-party provider decides as today's kernel atoms
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", list(CASES))
async def test_a_third_party_provider_decides_as_the_kernel_atoms_mono_and_split(
    split: dict[str, CoreFacade], gen: SimpleNamespace, case: str
) -> None:
    """Entitlement (``SubscriptionActive`` / ``Credits``) and membership
    (``OrgRole``) checks answered by ``acme_billing`` / ``acme_orgs``: the
    generated atoms over the third party's declared contracts and the kernel
    atoms over today's contracts decide the same, in-process and over RPC."""
    policy, org, granted, (allowed, status, indeterminate) = CASES[case]
    decisions = {
        (kit_name, side): await _outcome(policy(kit), _ctx(core, org, granted))
        for kit_name, kit in (("kernel", KERNEL), ("generated", gen))
        for side, core in split.items()
    }
    assert len(set(decisions.values())) == 1, decisions
    decided = decisions["generated", "consumer"]
    assert decided[:2] == (allowed, status), decided
    assert decided[4] is indeterminate


async def test_the_root_chooses_the_provider_by_configuration(
    gen: SimpleNamespace,
) -> None:
    """``access_providers`` decides which third party answers: the assigned
    module registers, any other candidate of the contract is refused (B034),
    and the decisions follow the chosen provider's ledger."""
    for chosen, other, admin in (
        ("acme_orgs", "rival_orgs", True),
        ("rival_orgs", "acme_orgs", False),
    ):
        assignment = {MEMBERSHIP: chosen, ROLES_CONTRACT: chosen}
        with pytest.raises(ForeignOwnerRegistration, match="SANDLOOM-B034"):
            _provider(assignment, (other,))
        _app, core = _provider(assignment, (chosen,))
        assert core.contract_owner(ROLES_CONTRACT) == chosen
        for kit in (KERNEL, gen):
            decision = await kit.OrgRole("admin").decide(_ctx(core, ORG))
            assert decision.allowed is admin
            assert (await kit.OrgRole("member").decide(_ctx(core, ORG))).allowed


async def test_without_a_provider_both_are_501_even_negated(
    gen: SimpleNamespace,
) -> None:
    """No local provider and no binding: indeterminate 501
    ``service_unavailable``, the same for both kits, ``~`` included."""
    consumer = _consumer("acme", ("acme",), bound=())
    builds: tuple[Policy, ...] = (
        lambda k: k.SubscriptionActive(of=USER_ACCOUNT),
        lambda k: ~k.Credits(1, of=USER_ACCOUNT),
        lambda k: ~k.OrgRole("member"),
    )
    for build in builds:
        kernel_decision = await build(KERNEL).decide(_ctx(consumer, ORG))
        generated = await build(gen).decide(_ctx(consumer, ORG))
        assert _key(generated) == _key(kernel_decision)
        assert generated.indeterminate and generated.http_status == 501
        assert generated.code == "service_unavailable"


async def test_a_provider_down_is_503_both_ways(
    monkeypatch: pytest.MonkeyPatch, gen: SimpleNamespace
) -> None:
    """The bound provider unreachable: the caller is answered 503 either way.
    The kernel atom raises it; the generated atom is indeterminate 503
    ``owner_unavailable`` inside the algebra (K5), so ``~`` never allows."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="http://acme"
    )
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: client)
    monkeypatch.setattr(rpc_proxy, "_RPC_RETRY_DELAY_SECONDS", 0)
    consumer = _consumer("acme", ("acme",))
    builds: tuple[Policy, ...] = (
        lambda k: k.SubscriptionActive(of=USER_ACCOUNT),
        lambda k: k.Credits(1, of=USER_ACCOUNT),
        lambda k: k.OrgRole("member"),
    )
    for build in builds:
        with pytest.raises(RemoteServiceUnavailable) as exc:
            await build(KERNEL).decide(_ctx(consumer, ORG))
        assert exc.value.status_code == 503
        for policy in (build(gen), ~build(gen)):
            decision = await policy.decide(_ctx(consumer, ORG))
            assert not decision.allowed and decision.indeterminate
            assert decision.http_status == 503
            assert decision.code == _contract_check.OWNER_UNAVAILABLE
