"""Provider down inside the algebra (K5) and the per-atom request session (K6).

ADR-0014 section 16, owner answer #19, requirements I-07 / H-4 / C-F7 and
I-14 / C-B7. Pinned here with the generated atoms of two fixture contracts
(no battery involved), rendered as ``sandloom generate`` renders them:

* K5 / Z-9: a bound provider that is down (``remote_service_unavailable``,
  ``rpc_timeout``) is indeterminate 503 ``owner_unavailable`` inside the
  algebra: ``OrgRole | SubscriptionActive`` allows only on a subscription that
  reliably allowed, ``~OrgRole`` never allows, each indeterminate branch is a
  warning in the deny log and one ``access_indeterminate_branch_total``; the
  provider is asked exactly as often as before (M-calls unchanged). An
  authentication or allowlist refusal (``rpc_caller_forbidden``, ...) and any
  other failure class stay errors, never an indeterminate the ``Or`` could
  step over.
* K6 / Z-4: ``require()`` opens the request session only for a policy one of
  whose atoms may use it (``needs_request_session``): a contract-anchored
  check runs in its provider's own unit and opens none; the tier rule stays
  the default for every other atom.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from sandloom.core.cli.generated import render_stub
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
from sandloom.core.rpc.errors import RemoteServiceBadResponse, RemoteServiceUnavailable
from sandloom.sdk import contract
from sandloom.shared.authz import (
    Account,
    Category,
    Credits,
    From,
    Not,
    Or,
    Owner,
    Role,
    access_check,
    require,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.contract_check import OWNER_UNAVAILABLE
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

MEMBERS = "fixture.members.v1"
PLANS = "fixture.plans.v1"
BRANCH_METRIC = "access_indeterminate_branch_total"


@contract(MEMBERS)
class MembersContract:
    """Organization membership (a fixture provider)."""

    @access_check(
        "OrgRole",
        bind={
            "organization_id": From.org(param="org_id", validate_against_token=False),
            "user_id": From.subject_id(),
        },
        values={"minimum": ["member", "admin", "owner"]},
        describe="organization role '{minimum}'",
    )
    async def has_role_at_least(
        self, organization_id: UUID, user_id: UUID, minimum: str
    ) -> bool:
        """True when the user's role ranks at least ``minimum``."""
        return True


@contract(PLANS)
class PlansContract:
    """Subscriptions (a fixture provider)."""

    @access_check(
        "SubscriptionActive",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.PAYMENT_REQUIRED,
        describe="active subscription",
    )
    async def check_subscription_active(
        self, tenant_type: str, tenant_id: UUID
    ) -> bool:
        """True while a subscription grants access."""
        return True


def _stub(tmp_path: Path, name: str, cls: type) -> ModuleType:
    path = tmp_path / f"{name.replace('.', '_')}.py"
    path.write_text(render_stub(name, cls))
    spec = importlib.util.spec_from_file_location(f"_kb35_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gen(tmp_path: Path) -> Iterator[Any]:
    members = _stub(tmp_path, MEMBERS, MembersContract)
    plans = _stub(tmp_path, PLANS, PlansContract)
    yield SimpleNamespace(
        OrgRole=members.OrgRole, SubscriptionActive=plans.SubscriptionActive
    )
    for module in (members, plans):
        sys.modules.pop(module.__name__, None)


class _Down:
    """A bound provider whose every call fails with ``exc``; counts the calls."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def has_role_at_least(self, *_args: Any) -> bool:
        self.calls += 1
        raise self.exc


class _Plans:
    def __init__(self, active: bool) -> None:
        self.active = active
        self.calls = 0

    async def check_subscription_active(self, *_args: Any) -> bool:
        self.calls += 1
        return self.active


class _Core:
    def __init__(self, services: dict[str, Any]) -> None:
        self.services = services
        self.metrics: dict[str, int] = {}

    def get_service(self, name: str) -> Any:
        if name not in self.services:
            raise ServiceUnavailableInDeployment(name)
        return self.services[name]

    def get_access_fact(self, name: str) -> Any:
        return None

    def remote_fact_owners(self) -> list[str]:
        return []

    def increment_metric(self, name: str, amount: int) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount


class _Req:
    def __init__(self, org: UUID) -> None:
        self.path_params = {"org_id": str(org)}
        self.query_params: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.scope: dict[str, Any] = {"state": {}}
        self.state = SimpleNamespace()


def _ctx(core: _Core) -> AuthzContext:
    org = uuid4()
    subject = UserContext(user_id=uuid4(), tenant_ids=[str(org)])
    request: Any = _Req(org)
    return AuthzContext(subject, request=request, core=core)


def _down(code: str = "remote_service_unavailable") -> RemoteServiceUnavailable:
    return RemoteServiceUnavailable(MEMBERS, reason="unreachable", code=code)


# --------------------------------------------------------------------------- #
# K5 / Z-9: the provider down is an indeterminate 503 inside the algebra
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", ["remote_service_unavailable", "rpc_timeout"])
async def test_a_down_provider_is_indeterminate_503(
    gen: Any, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    members = _Down(_down(code))
    core = _Core({MEMBERS: members})
    with caplog.at_level(logging.WARNING, logger="sandloom.access"):
        decision = await gen.OrgRole("admin").decide(_ctx(core))
    assert not decision.allowed and decision.indeterminate
    assert (decision.http_status, decision.code) == (503, OWNER_UNAVAILABLE)
    assert decision.failed_requirement == "organization role 'admin'"
    assert members.calls == 1
    assert core.metrics == {BRANCH_METRIC: 1}
    [record] = [r for r in caplog.records if r.name == "sandloom.access"]
    assert record.levelno == logging.WARNING
    assert getattr(record, "access_code", None) == OWNER_UNAVAILABLE
    assert getattr(record, "service", None) == MEMBERS


async def test_or_allows_only_on_the_branch_that_reliably_allowed(
    gen: Any, caplog: pytest.LogCaptureFixture
) -> None:
    policy = gen.OrgRole("admin") | gen.SubscriptionActive(of=Account.user())
    members, plans = _Down(_down()), _Plans(active=True)
    core = _Core({MEMBERS: members, PLANS: plans})
    with caplog.at_level(logging.WARNING, logger="sandloom.access"):
        assert (await policy.decide(_ctx(core))).allowed
    # the broken branch is reported even though its sibling allowed
    assert core.metrics == {BRANCH_METRIC: 1}
    assert any(
        getattr(r, "access_code", None) == OWNER_UNAVAILABLE for r in caplog.records
    )
    # M-calls unchanged: each provider asked once, as before K5
    assert (members.calls, plans.calls) == (1, 1)

    plans.active = False
    denied = await policy.decide(_ctx(core))
    assert not denied.allowed and denied.indeterminate
    assert (denied.http_status, denied.code) == (503, OWNER_UNAVAILABLE)
    assert core.metrics == {BRANCH_METRIC: 2}


async def test_a_negated_down_provider_never_allows(gen: Any) -> None:
    core = _Core({MEMBERS: _Down(_down("rpc_timeout"))})
    for policy in (
        Not(gen.OrgRole("member")),
        Not(Not(gen.OrgRole("member"))),
        Or(Not(gen.OrgRole("member")), Role("nobody")),
    ):
        decision = await policy.decide(_ctx(core))
        assert not decision.allowed and decision.indeterminate
        assert (decision.http_status, decision.code) == (503, OWNER_UNAVAILABLE)


@pytest.mark.parametrize(
    "code",
    [
        "rpc_caller_forbidden",
        "rpc_auth_invalid",
        "rpc_identity_unknown",
        "rpc_unauthorized",
        "rpc_context_invalid",
    ],
)
async def test_an_authentication_or_allowlist_refusal_stays_an_error(
    gen: Any, code: str
) -> None:
    policy = gen.OrgRole("admin") | gen.SubscriptionActive(of=Account.user())
    plans = _Plans(active=True)
    core = _Core({MEMBERS: _Down(_down(code)), PLANS: plans})
    with pytest.raises(RemoteServiceUnavailable) as raised:
        await policy.decide(_ctx(core))
    assert raised.value.code == code
    assert plans.calls == 0  # the Or never stepped over the broken invariant
    assert core.metrics == {}


@pytest.mark.parametrize(
    "exc",
    [
        RemoteServiceBadResponse(MEMBERS, reason="not an envelope"),
        HTTPException(503, detail="a provider's own 503"),
        RuntimeError("a provider bug"),
    ],
    ids=["bad-response", "domain-503", "runtime-error"],
)
async def test_other_failure_classes_are_untouched(gen: Any, exc: Exception) -> None:
    core = _Core({MEMBERS: _Down(exc)})
    with pytest.raises(type(exc)):
        await gen.OrgRole("admin").decide(_ctx(core))
    assert core.metrics == {}


# --------------------------------------------------------------------------- #
# K6 / Z-4: the request session per atom
# --------------------------------------------------------------------------- #
def _opens_session(policy: Any) -> bool:
    return "db" in inspect.signature(require(policy)).parameters


async def test_a_contract_check_opens_no_request_session(gen: Any) -> None:
    org_role = gen.OrgRole("admin")
    subscription = gen.SubscriptionActive(of=Account.bound_tenant())
    for policy in (
        org_role,
        subscription,
        org_role & subscription,
        Role("admin") | gen.SubscriptionActive(of=Account.user()),
        Not(org_role),
    ):
        assert not policy.needs_request_session(), policy.describe()
        assert not _opens_session(policy), policy.describe()


async def test_the_tier_rule_stays_the_default(gen: Any) -> None:
    assert not Role("admin").needs_request_session()
    assert not _opens_session(Role("admin"))
    credits = Credits(10, of=Account.user())  # a kernel live atom: unchanged
    assert credits.needs_request_session() and _opens_session(credits)
    for policy in (
        credits | gen.OrgRole("admin"),
        gen.OrgRole("admin") & Not(credits),
    ):
        assert policy.needs_request_session() and _opens_session(policy)
    assert Owner(lambda r: r.owner_id).needs_request_session()


# --------------------------------------------------------------------------- #
# K6: the boot probe is registered whenever there is a remote binding
# --------------------------------------------------------------------------- #
class _Facade:
    def __init__(self) -> None:
        self.startup: dict[str, tuple[bool, str]] = {}

    def add_startup_task(
        self, fn: Any, *, critical: bool = False, phase: str = "modules"
    ) -> None:
        self.startup[fn.__name__] = (critical, phase)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("off", {"describe_bindings": (False, "post")}),
        ("ready", {"contract_compat_check": (False, "post")}),
        ("strict", {"contract_compat_check": (True, "post")}),
    ],
)
async def test_the_boot_probe_describes_whatever_the_compat_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: dict[str, Any]
) -> None:
    from sandloom.core.app_factory import lifecycle

    monkeypatch.setattr(lifecycle.settings, "contract_compat_check", mode)
    monkeypatch.setattr(
        lifecycle.settings, "service_bindings", {MEMBERS: {"base_url": "http://p"}}
    )
    facade: Any = _Facade()
    lifecycle._register_contract_compat_check(facade)
    assert facade.startup == expected
    monkeypatch.setattr(lifecycle.settings, "service_bindings", {})
    none: Any = _Facade()
    lifecycle._register_contract_compat_check(none)
    assert none.startup == {}
