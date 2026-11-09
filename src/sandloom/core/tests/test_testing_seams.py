"""R8: the test seams — deliver_event, fake_access_fact (generic, by fact
name) and fake_contract for the generated live access atoms."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.cli.generated import render_stub
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import ModuleAPI
from sandloom.core.runtime import use_active_core
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tenant_context import bind_tenant
from sandloom.core.testing import (
    deliver_event,
    fake_access_fact,
    recording_uow,
    register_event_schemas,
)
from sandloom.sdk import contract
from sandloom.sdk.testing import fake_contract
from sandloom.shared.authz import (
    AccessFact,
    Account,
    Category,
    ClaimRule,
    From,
    access_check,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext

CHANNEL = "seams.thing.happened.v1"


def _core_with_consumer(seen: list[Any]) -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    registry = InMemorySchemaRegistry()
    core.register_schema_registry(registry)
    core.set_event_bus(InMemoryEventBus(registry))
    register_event_schemas(
        {CHANNEL: {"type": "object", "required": ["n"]}}, target=core
    )

    async def handler(session: Any, envelope: Any) -> None:
        seen.append((session, envelope["id"], envelope["payload"]))

    with core.module_registration("seams") as api:
        assert isinstance(api, ModuleAPI)
        api.register_event_consumer(CHANNEL, handler, name="recorder", durable=False)
    return core


async def test_deliver_event_runs_the_registered_consumer() -> None:
    seen: list[Any] = []
    core = _core_with_consumer(seen)
    with recording_uow() as session:
        applied = await deliver_event(
            CHANNEL, {"n": 1}, times=2, session=session, core=core
        )
    assert applied == 1
    assert [s for s, _, _ in seen] == [session, session]
    assert seen[0][1] == seen[1][1]  # a redelivery keeps the envelope id
    assert seen[0][2] == {"n": 1}


async def test_deliver_event_validates_like_a_publish() -> None:
    core = _core_with_consumer([])
    with pytest.raises(ValueError, match="SANDLOOM-R004"):
        await deliver_event(CHANNEL, {"wrong": 1}, core=core)
    register_event_schemas(["seams.nobody.listens.v1"], target=core)
    with pytest.raises(LookupError, match="no consumer"):
        await deliver_event("seams.nobody.listens.v1", {}, core=core)


async def test_fake_access_fact_answers_entitled_without_its_owner() -> None:
    from sandloom.shared.authz import Entitled

    core = CoreFacade(app=FastAPI())
    subject = UserContext(user_id=uuid.uuid4())

    async def decide() -> Any:
        return await Entitled("plan.pro").decide(AuthzContext(subject, core=core))

    with fake_access_fact("plan.pro", True, core=core):
        assert (await decide()).allowed
    with fake_access_fact("plan.pro", lambda ctx: False, core=core):
        assert not (await decide()).allowed
    assert core.get_access_fact("plan.pro") is None  # restored


# --------------------------------------------------------------------------- #
# Live access atoms: fake_contract(REF, Fake()) (ADR-0014 decision 15)
# --------------------------------------------------------------------------- #
ENTITLEMENTS = "billing.entitlements.v1"

#: The subscription fact the generated ``Subscribed`` reads for a user, as
#: billing declares it (no billing module is loaded here).
SUBSCRIPTION_FACT = AccessFact(
    name="subscription.active",
    owner_module="billing",
    claim_key="subscription_status",
    claim_rule=ClaimRule.one_of(("active", "trialing")),
    live_service=ENTITLEMENTS,
    deny_category=Category.PAYMENT_REQUIRED,
    atom="Subscribed",
    atom_statuses=("active", "trialing"),
    atom_claim_keys=("subscription_status",),
    atom_describe="active subscription",
)


@contract(ENTITLEMENTS)
class EntitlementsContract:
    """``billing.entitlements.v1``'s subscription check, declared as billing
    declares it: its access check ``SubscriptionActive`` and, through the
    fact, the claim atom ``Subscribed``."""

    @access_check(
        "SubscriptionActive",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.PAYMENT_REQUIRED,
        describe="active subscription",
        claim=SUBSCRIPTION_FACT,
    )
    async def check_subscription_active(
        self, tenant_type: str, tenant_id: uuid.UUID
    ) -> bool:
        """True while a subscription grants access."""
        return False


@pytest.fixture
def entitlements(tmp_path: Path) -> Iterator[Any]:
    """The atoms ``sandloom generate`` writes beside a consumer's stub of
    ``billing.entitlements.v1``, imported as a consumer imports them."""
    path = tmp_path / "billing_entitlements_v1.py"
    path.write_text(render_stub(ENTITLEMENTS, EntitlementsContract))
    spec = importlib.util.spec_from_file_location("_seams_entitlements_v1", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


class _Entitlements:
    """A fake ``billing.entitlements.v1`` answering the subscription check."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[str, Any]] = []

    async def check_subscription_active(self, kind: str, tenant: Any) -> bool:
        self.calls.append((kind, tenant))
        return self.answer


async def test_fake_contract_drives_the_org_billing_atoms_without_billing(
    entitlements: Any,
) -> None:
    """``fake_contract("billing.entitlements.v1", Fake())`` answers the
    generated ``SubscriptionActive`` / ``Subscribed`` with
    ``of=Account.org(...)``, with no billing module loaded (both ask the
    subscription check)."""
    core = CoreFacade(app=FastAPI())
    subject = UserContext(user_id=uuid.uuid4())
    org = uuid.uuid4()
    gates = [
        entitlements.SubscriptionActive(of=Account.org(param="org_id")),
        entitlements.Subscribed(of=Account.org(param="org_id")),
    ]

    async def decide(gate: Any) -> Any:
        with bind_tenant(org):
            return await gate.decide(AuthzContext(subject, core=core))

    for gate in gates:
        for answer in (True, False):
            fake = _Entitlements(answer)
            with use_active_core(core), fake_contract(ENTITLEMENTS, fake):
                assert (await decide(gate)).allowed is answer
            assert fake.calls == [("organization", org)]
    # Restored: billing's contract is not configured here again, so the
    # atom cannot answer (501, never an allow).
    restored = await decide(gates[0])
    assert not restored.allowed and restored.http_status == 501
    assert "get_service" not in vars(core)


async def test_fake_contract_reaches_the_kernels_get_service(entitlements: Any) -> None:
    """The authz atoms resolve contracts through ``core.get_service``, not the
    SDK resolver: ``fake_contract`` serves both."""

    class Entitlements:
        async def check_subscription_active(self, kind: str, tenant: Any) -> bool:
            return kind == "organization"

    core = CoreFacade(app=FastAPI())
    subject = UserContext(user_id=uuid.uuid4())
    gate = entitlements.SubscriptionActive(of=Account.org(param="org_id"))
    with (
        use_active_core(core),
        fake_contract(ENTITLEMENTS, Entitlements()),
    ):
        assert isinstance(core.get_service(ENTITLEMENTS), Entitlements)
        with bind_tenant(uuid.uuid4()):
            assert (await gate.decide(AuthzContext(subject, core=core))).allowed
    assert "get_service" not in vars(core)
