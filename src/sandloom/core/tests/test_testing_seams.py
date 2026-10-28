"""R8: the test seams — deliver_event and fake_access_fact."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import ModuleAPI
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing import (
    deliver_event,
    fake_access_fact,
    recording_uow,
    register_event_schemas,
)

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
    from sandloom.shared.authz.context import AuthzContext
    from sandloom.shared.subject import UserContext

    core = CoreFacade(app=FastAPI())
    subject = UserContext(user_id=uuid.uuid4())

    async def decide() -> Any:
        return await Entitled("plan.pro").decide(AuthzContext(subject, core=core))

    with fake_access_fact("plan.pro", True, core=core):
        assert (await decide()).allowed
    with fake_access_fact("plan.pro", lambda ctx: False, core=core):
        assert not (await decide()).allowed
    assert core.get_access_fact("plan.pro") is None  # restored


async def test_fake_access_fact_drives_the_org_billing_atoms_without_billing() -> None:
    """``fake_access_fact("organization.subscription.active", ...)`` answers
    ``SubscriptionActive`` / ``Subscribed`` with ``of=Account.org(...)`` too,
    with no billing module loaded (the live atoms ask the same question)."""
    from sandloom.core.tenant_context import bind_tenant
    from sandloom.shared.authz import Account, Subscribed, SubscriptionActive
    from sandloom.shared.authz.context import AuthzContext
    from sandloom.shared.subject import UserContext

    core = CoreFacade(app=FastAPI())
    subject = UserContext(user_id=uuid.uuid4())
    org = uuid.uuid4()
    gates = [
        SubscriptionActive(of=Account.org(param="org_id")),
        Subscribed(of=Account.org(param="org_id")),
    ]

    async def allowed(gate: Any) -> bool:
        with bind_tenant(org):
            decision = await gate.decide(AuthzContext(subject, core=core))
        return bool(decision.allowed)

    for gate in gates:
        with fake_access_fact("organization.subscription.active", True, core=core):
            assert await allowed(gate)
        with fake_access_fact("organization.subscription.active", False, core=core):
            assert not await allowed(gate)
    # Restored: billing's contract is not configured here again.
    assert not await allowed(gates[0])
    assert "get_service" not in vars(core)


async def test_fake_contract_reaches_the_kernels_get_service() -> None:
    """The authz atoms resolve contracts through ``core.get_service``, not the
    SDK resolver: ``fake_contract`` serves both."""
    from sandloom.core.runtime import use_active_core
    from sandloom.core.tenant_context import bind_tenant
    from sandloom.sdk.testing import fake_contract
    from sandloom.shared.authz import Account, SubscriptionActive
    from sandloom.shared.authz.context import AuthzContext
    from sandloom.shared.subject import UserContext

    class Entitlements:
        async def check_subscription_active(self, kind: str, tenant: Any) -> bool:
            return kind == "organization"

    core = CoreFacade(app=FastAPI())
    subject = UserContext(user_id=uuid.uuid4())
    gate = SubscriptionActive(of=Account.org(param="org_id"))
    with (
        use_active_core(core),
        fake_contract("billing.entitlements.v1", Entitlements()),
    ):
        assert isinstance(core.get_service("billing.entitlements.v1"), Entitlements)
        with bind_tenant(uuid.uuid4()):
            assert (await gate.decide(AuthzContext(subject, core=core))).allowed
    assert "get_service" not in vars(core)
