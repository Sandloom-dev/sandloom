"""A fact is identified by ``(owner, fact_name)``, never by its name alone.

Owner decision 2026-11-09 (KB-34k): ``Entitled("subscription.active",
owner="billing")`` accepts only billing's fact. A rogue module that registers
a fact of the same name, whose answer is always ``True``, never satisfies it —
in a process that loads the rogue module, or with the rogue module bound
remotely, and whether billing is local, bound over real ASGI-RPC, absent or
down:

| billing                    | rogue (always True) | decision                  |
|----------------------------|---------------------|---------------------------|
| present, denies            | present             | billing's deny            |
| absent (not loaded/bound)  | present             | 501 ``fact_unregistered`` |
| bound, RPC unreachable     | present             | 503, no fallback to rogue |
| present                    | present             | only billing is asked     |
| ``owner=`` missing         |                     | error at policy build     |

plus ``fake_access_fact(..., owner="rogue")`` never satisfying
``owner="billing"``, and ``Not`` / ``Or`` over the missing-owner rows never
allowing (indeterminate, never an invertible deny).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from sandloom.core import rpc
from sandloom.core.contract_testing import rpc_settings
from sandloom.core.descriptors.module_names import InvalidFactOwner
from sandloom.core.facade import CoreFacade
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.sdk.testing import fake_access_fact
from sandloom.shared.authz import (
    AccessFact,
    Entitled,
    Not,
    Perm,
    lookup_access_fact,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.results import AccessDecision
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

FACT = "subscription.active"
BILLING_URL = "http://billing"
DOWN_URL = "http://billing-down"

#: The import path each module registers under (its short name: the owner).
_IMPORT_PATH = {"billing": "app.modules.billing", "rogue": "app.modules.rogue"}

#: What the owners answer and who was asked (reset per test).
STATE: dict[str, Any] = {}


def _entitled() -> Entitled:
    return Entitled(FACT, owner="billing")


async def _billing_reader(ctx: Any) -> bool:
    STATE["asked"].append("billing")
    return bool(STATE["billing"])


async def _rogue_reader(ctx: Any) -> bool:
    STATE["asked"].append("rogue")
    return True


def _fact(owner: str, reader: Any) -> AccessFact:
    return AccessFact(
        name=FACT,
        owner_module=owner,
        projection_reader=reader,
        min_freshness="projection",
        default_mode="auto",
    )


class _FakeDatabase:
    """The owner's contract runtime opens a (read-only) unit per call."""

    class _UoW:
        session = SimpleNamespace(info={})

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    def uow_factory(self) -> Any:
        return self._UoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _module_core(module: str, fact: AccessFact, **core_kwargs: Any) -> CoreFacade:
    """A process that loads ``module`` and its fact (exposes <module>.facts.v1)."""
    app = FastAPI()
    core = CoreFacade(app=app, **core_kwargs)
    app.state.core = core
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    with core.module_registration(_IMPORT_PATH[module]) as api:
        api.register_access_fact(fact)
    return core


class _CountingService:
    """An in-process ``rogue.facts.v1`` that records every call."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def describe(self) -> Any:
        STATE["asked"].append("rogue.describe")
        return await self._inner.describe()

    async def evaluate(self, *args: Any) -> Any:
        STATE["asked"].append("rogue.evaluate")
        return await self._inner.evaluate(*args)


class _LyingBillingFacts:
    """A ``billing.facts.v1`` endpoint describing a fact under another owner."""

    async def describe(self) -> list[dict[str, Any]]:
        STATE["asked"].append("billing.describe")
        return [
            {
                "name": FACT,
                "owner_module": "rogue",
                "min_freshness": "live",
                "default_mode": "live",
            }
        ]

    async def evaluate(self, *args: Any) -> dict[str, Any]:
        STATE["asked"].append("billing.evaluate")
        return {"value": True}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A billing process over real ASGI-RPC, a rogue process in-process, and
    a factory of consumer processes binding either."""
    from sandloom.core.config.settings import settings

    owner_keys = rpc_settings(
        "billing", peers=("tenancy",), allowed_calls={"tenancy": ["billing.facts.v1"]}
    )
    for name, value in owner_keys.items():
        monkeypatch.setattr(settings, name, value)
    STATE.clear()
    STATE.update(billing=False, asked=[])
    billing = _module_core("billing", _fact("billing", _billing_reader))
    rpc.install_service_rpc_routes(billing.app, billing)
    rogue = _module_core("rogue", _fact("rogue", _rogue_reader))

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("billing is down", request=request)

    client = httpx.AsyncClient(
        mounts={
            BILLING_URL: httpx.ASGITransport(app=billing.app),
            DOWN_URL: httpx.MockTransport(_refuse),
        }
    )
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: client)

    def consumer(
        *,
        billing_url: str | None = None,
        rogue_bound: bool = False,
        rogue_local: bool = False,
        lying_billing: bool = False,
    ) -> CoreFacade:
        bindings: dict[str, Any] = {}
        if billing_url is not None or lying_billing:
            bindings["billing.facts.v1"] = {
                "base_url": billing_url or BILLING_URL,
                "service_id": "billing",
            }
        if rogue_bound:
            bindings["rogue.facts.v1"] = {"base_url": "http://rogue"}
        cfg = settings.model_copy(
            update={
                **rpc_settings("tenancy", peers=("billing",)),
                "service_bindings": bindings,
            }
        )
        if rogue_local:
            core = _module_core(
                "rogue", _fact("rogue", _rogue_reader), settings_obj=cfg
            )
        else:
            core = CoreFacade(app=FastAPI(), settings_obj=cfg)
            core.app.state.core = core

        def resolve(
            service_name: str,
            binding: dict[str, object],
            *,
            default_timeout_seconds: float,
            settings: object = None,
        ) -> Any:
            if service_name == "rogue.facts.v1":
                return _CountingService(rogue.get_service("rogue.facts.v1"))
            if lying_billing:
                return _LyingBillingFacts()
            return rpc.RemoteServiceProxy(
                service_name=service_name,
                binding=binding,
                default_timeout_seconds=default_timeout_seconds,
                settings=cfg,
            )

        core.set_service_resolver(resolve)
        return core

    yield SimpleNamespace(billing=billing, rogue=rogue, consumer=consumer)


def _ctx(core: CoreFacade) -> AuthzContext:
    return AuthzContext(UserContext(user_id=uuid4()), core=core)


async def _decide(policy: Any, core: CoreFacade) -> AccessDecision:
    decision: AccessDecision = await policy.decide(_ctx(core))
    return decision


def _rogue_never_asked() -> None:
    assert not [a for a in STATE["asked"] if a.startswith("rogue")], STATE["asked"]


# -- row 1: billing present and denying, rogue allowing -> billing's deny ----


@pytest.mark.parametrize(
    "shape",
    ["rogue_local_billing_rpc", "both_bound", "billing_local_rogue_bound"],
)
async def test_billing_deny_is_the_answer_while_rogue_allows(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    STATE["billing"] = False
    if shape == "rogue_local_billing_rpc":
        core = world.consumer(billing_url=BILLING_URL, rogue_local=True)
    elif shape == "both_bound":
        core = world.consumer(billing_url=BILLING_URL, rogue_bound=True)
    else:
        # The monolith shape: billing loaded here, rogue's facts contract bound.
        core = world.billing
        monkeypatch.setattr(
            core.settings, "service_bindings", {"rogue.facts.v1": {"base_url": "x"}}
        )
        core.set_service_resolver(
            lambda name, binding, **_: _CountingService(
                world.rogue.get_service("rogue.facts.v1")
            )
        )
    decision = await _decide(_entitled(), core)
    assert not decision.allowed and not decision.indeterminate
    assert decision.code == FACT and decision.http_status == 403
    assert "billing" in STATE["asked"]
    _rogue_never_asked()


# -- row 2: billing absent, rogue allowing -> 501 fact_unregistered ----------


@pytest.mark.parametrize("rogue", ["local", "bound"])
async def test_billing_absent_is_501_whatever_rogue_answers(
    world: SimpleNamespace, rogue: str
) -> None:
    core = world.consumer(rogue_local=rogue == "local", rogue_bound=rogue == "bound")
    decision = await _decide(_entitled(), core)
    assert decision.indeterminate and decision.http_status == 501
    assert decision.code == "fact_unregistered"
    _rogue_never_asked()


# -- row 3: billing bound but unreachable -> 503, no fallback to rogue -------


@pytest.mark.parametrize("rogue", ["local", "bound"])
async def test_billing_down_is_503_never_rogue(
    world: SimpleNamespace, rogue: str
) -> None:
    core = world.consumer(
        billing_url=DOWN_URL, rogue_local=rogue == "local", rogue_bound=rogue == "bound"
    )
    decision = await _decide(_entitled(), core)
    assert decision.indeterminate and decision.http_status == 503
    assert decision.code == "fact_owner_unavailable"
    _rogue_never_asked()


# -- row 4: both available -> only billing is used ---------------------------


@pytest.mark.parametrize("billing_says", [True, False])
@pytest.mark.parametrize("rogue", ["local", "bound"])
async def test_both_available_only_billing_is_asked(
    world: SimpleNamespace, billing_says: bool, rogue: str
) -> None:
    STATE["billing"] = billing_says
    core = world.consumer(
        billing_url=BILLING_URL,
        rogue_local=rogue == "local",
        rogue_bound=rogue == "bound",
    )
    decision = await _decide(_entitled(), core)
    assert decision.allowed is billing_says and not decision.indeterminate
    assert STATE["asked"] == ["billing"]
    # A policy naming rogue gets rogue's fact: the owners never mix.
    STATE["asked"].clear()
    rogue_says = await _decide(Entitled(FACT, owner="rogue"), core)
    assert rogue_says.allowed
    assert "billing" not in STATE["asked"]


async def test_one_request_never_serves_rogue_answer_to_billing(
    world: SimpleNamespace,
) -> None:
    """The request-local fact cache is keyed by owner too."""
    core = world.consumer(billing_url=BILLING_URL, rogue_local=True)
    ctx = _ctx(core)
    assert (await Entitled(FACT, owner="rogue").decide(ctx)).allowed
    assert not (await _entitled().decide(ctx)).allowed
    assert not (await (Entitled(FACT, owner="rogue") & _entitled()).decide(ctx)).allowed


async def test_owner_contract_describing_another_owner_is_ignored(
    world: SimpleNamespace,
) -> None:
    """``billing.facts.v1`` answering a descriptor whose owner_module is not
    billing: the descriptor is ignored (SANDLOOM-B034), the fact unknown."""
    core = world.consumer(lying_billing=True)
    decision = await _decide(_entitled(), core)
    assert decision.indeterminate and decision.code == "fact_unregistered"
    assert "billing.evaluate" not in STATE["asked"]
    assert await lookup_access_fact(_ctx(core), FACT, owner="billing") is None


async def test_two_owners_of_one_name_in_one_process_are_refused(
    world: SimpleNamespace,
) -> None:
    """A monolith never hosts both: the second registration fails the boot."""
    with (
        pytest.raises(ValueError, match="already registered by module 'billing'"),
        world.billing.module_registration(_IMPORT_PATH["rogue"]) as api,
    ):
        api.register_access_fact(_fact("rogue", _rogue_reader))


# -- Not / Or over the missing-owner rows never allow ------------------------


@pytest.mark.parametrize("row", ["absent", "down"])
async def test_negation_and_disjunction_never_allow(
    world: SimpleNamespace, row: str
) -> None:
    core = world.consumer(
        billing_url=DOWN_URL if row == "down" else None, rogue_local=True
    )
    status = 503 if row == "down" else 501
    for policy in (
        ~_entitled(),
        Not(_entitled()),
        Not(Not(_entitled())),
        Perm("nobody.has") | ~_entitled(),
        Perm("nobody.has") | _entitled(),
        ~(Perm("nobody.has") | _entitled()),
    ):
        decision = await _decide(policy, core)
        assert not decision.allowed, policy
        assert decision.indeterminate and decision.http_status == status, policy
    _rogue_never_asked()


# -- row 5: owner missing -> error at policy construction --------------------


def test_entitled_without_owner_fails_at_construction() -> None:
    with pytest.raises(TypeError, match="owner"):
        Entitled(FACT)  # type: ignore[call-arg]


@pytest.mark.parametrize("owner", [None, "", "  ", 3, b"billing", "bad-name", "a.b"])
def test_entitled_invalid_owner_fails_at_construction(owner: object) -> None:
    with pytest.raises(InvalidFactOwner, match="SANDLOOM-B038") as raised:
        Entitled(FACT, owner=owner)  # type: ignore[arg-type]
    assert isinstance(raised.value, TypeError)
    assert isinstance(raised.value, ValueError)


async def test_lookup_and_fake_require_an_owner(world: SimpleNamespace) -> None:
    with pytest.raises(InvalidFactOwner):
        await lookup_access_fact(_ctx(world.billing), FACT, owner=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="owner"):
        with fake_access_fact(FACT, True, core=world.billing):  # type: ignore[call-arg]
            pass
    with pytest.raises(InvalidFactOwner):
        with fake_access_fact(FACT, True, owner="", core=world.billing):
            pass


def test_entitled_names_only_its_owner_contract() -> None:
    assert _entitled().contracts() == frozenset({"billing.facts.v1"})
    assert Entitled("organization.member", owner="tenancy").contracts() == frozenset(
        {"tenancy.facts.v1"}
    )


# -- fake_access_fact: a fake of another owner does not satisfy billing ------


async def test_fake_of_another_owner_does_not_satisfy_billing() -> None:
    core = CoreFacade(app=FastAPI())
    with fake_access_fact(FACT, True, owner="rogue", core=core):
        decision = await _decide(_entitled(), core)
        assert decision.indeterminate and decision.code == "fact_unregistered"
        assert not (await _decide(~_entitled(), core)).allowed
        assert (await _decide(Entitled(FACT, owner="rogue"), core)).allowed
    with fake_access_fact(FACT, True, owner="billing", core=core):
        assert (await _decide(_entitled(), core)).allowed
    with fake_access_fact(FACT, False, owner="billing", core=core):
        denied = await _decide(_entitled(), core)
        assert not denied.allowed and not denied.indeterminate
