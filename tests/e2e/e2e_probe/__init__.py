"""Test-only probe module loaded into BOTH processes of the split e2e test.

Not shipped (lives under ``src/tests``); loaded via ``SANDLOOM_MODULES`` with
``tests/e2e`` on ``PYTHONPATH``. It exercises the framework exactly like a
business module would:

- ``GET /_e2e/contracts`` calls the bundled contracts through
  ``core.get_service`` — locally or over RPC depending on the process — so the
  test can compare both paths' answers;
- ``GET /_e2e/orgrole/{org_id}`` / ``/_e2e/subscribed/{org_id}`` are gated by
  the live authz atoms (``OrgRole`` / ``SubscriptionActive``);
- ``GET /_e2e/entitled/personal`` / ``/_e2e/entitled/org/{org_id}`` /
  ``/_e2e/subscribed-claim`` are gated by billing's access facts
  (``Entitled(...)`` / ``Subscribed(of=Account.user())``) — local in the billing process,
  resolved through ``billing.facts.v1`` in the other;
- ``POST /_e2e/block/{user_id}`` / ``/_e2e/unblock/{user_id}`` call the
  emergency ``block_user`` / ``unblock_user`` (used from the process WITHOUT
  identity, proving the block crosses the split);
- an event consumer (process B only, ``E2E_PROBE_CONSUME=1``) records
  ``tenancy.organization_created.v1`` deliveries so the test can prove
  cross-process event consumption (see ``register`` for why it is gated).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from uuid import UUID

# Module-level: FastAPI resolves the (postponed) annotations of the endpoints
# below against this module's globals.
from sandloom.core.di import DbSession
from sandloom.shared.authz import Account

if TYPE_CHECKING:  # pragma: no cover
    from sandloom.core.descriptors import ModuleDescriptor
    from sandloom.core.module_api import ModuleAPI

#: Organization ids seen on ``tenancy.organization_created.v1`` (process-local).
RECEIVED: list[str] = []


async def _on_org_created(session: Any, envelope: Any) -> None:
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    org_id = (payload or {}).get("organization_id") or (payload or {}).get("id")
    RECEIVED.append(str(org_id))


def register(core: ModuleAPI) -> ModuleDescriptor:
    from fastapi import APIRouter, Depends, HTTPException

    from sandloom.core.descriptors import CapabilityRequirement, ModuleDescriptor
    from sandloom.core.di import CoreRuntime, block_user, get_core, public, unblock_user
    from sandloom.shared.authz import (
        Entitled,
        OrgRole,
        Subscribed,
        SubscriptionActive,
        require,
    )

    router = APIRouter(prefix="/_e2e", tags=["e2e"])
    # test-only probe endpoints of the e2e harness (no caller identity)
    harness = public(reason="test-only probe of the e2e split harness")

    @router.get("/received", dependencies=[harness])
    async def received() -> dict[str, list[str]]:
        return {"organization_ids": list(RECEIVED)}

    @router.get("/contracts", dependencies=[harness])
    async def contracts(
        org_id: UUID, user_id: UUID, core_rt: CoreRuntime = Depends(get_core)
    ) -> dict[str, Any]:
        membership = core_rt.get_service("tenancy.membership.v1")
        entitlements = core_rt.get_service("billing.entitlements.v1")
        credits = core_rt.get_service("billing.credits.v1")
        spend_status: int | None = None
        try:
            await credits.spend("organization", org_id, 1, f"e2e:{org_id}")
        except HTTPException as exc:
            spend_status = exc.status_code
        return {
            "role": await membership.get_role(org_id, user_id),
            "member": await membership.is_member(org_id, user_id),
            "orgs": [
                str(o) for o in await membership.list_user_organization_ids(user_id)
            ],
            "active": await entitlements.check_subscription_active(
                "organization", org_id
            ),
            "balance": await entitlements.get_credit_balance("organization", org_id),
            "spend_status": spend_status,
        }

    @router.get("/orgrole/{org_id}", dependencies=[Depends(require(OrgRole("owner")))])
    async def orgrole(org_id: UUID) -> dict[str, str]:
        return {"ok": str(org_id)}

    @router.get(
        "/subscribed/{org_id}",
        dependencies=[Depends(require(SubscriptionActive(of=Account.org())))],
    )
    async def subscribed(org_id: UUID) -> dict[str, str]:
        return {"ok": str(org_id)}

    @router.get(
        "/entitled/personal",
        dependencies=[Depends(require(Entitled("subscription.active")))],
    )
    async def entitled_personal() -> dict[str, bool]:
        return {"ok": True}

    @router.get(
        "/entitled/org/{org_id}",
        dependencies=[Depends(require(Entitled("organization.subscription.active")))],
    )
    async def entitled_org(org_id: UUID) -> dict[str, str]:
        return {"ok": str(org_id)}

    @router.get(
        "/subscribed-claim",
        dependencies=[Depends(require(Subscribed(of=Account.user())))],
    )
    async def subscribed_claim() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/block/{user_id}", dependencies=[harness])
    async def block(user_id: UUID, db: DbSession) -> dict[str, bool]:
        # Emergency block from a service that does NOT host identity.
        await block_user(user_id, reason="e2e: compromised")
        return {"blocked": True}

    @router.post("/unblock/{user_id}", dependencies=[harness])
    async def unblock(user_id: UUID, db: DbSession) -> dict[str, bool]:
        await unblock_user(user_id, reason="e2e: cleared")
        return {"blocked": False}

    core.include_router(router)
    # Only the process that asserts delivery consumes (E2E_PROBE_CONSUME=1).
    # The Streams group is ``<module>:<name>`` (here
    # ``e2e_probe:e2e_probe@tenancy.organization_created.v1``) and is claimed by
    # an ownership fingerprint (module + physical database) at startup. This
    # probe module is loaded in BOTH processes, which run on DIFFERENT
    # databases: if both registered the consumer, the second process to start
    # would fail startup (group already owned) — by design, since two databases
    # sharing one group would split deliveries and apply each event in only
    # one inbox. So the consumer stays gated to one process.
    if os.environ.get("E2E_PROBE_CONSUME") == "1":
        core.register_event_consumer(
            "tenancy.organization_created.v1",
            _on_org_created,
            name="e2e_probe@tenancy.organization_created.v1",
            durable=False,
            required=False,
        )
    return ModuleDescriptor(
        name="e2e_probe",
        version="0.0.1",
        capabilities=[CapabilityRequirement("database")],
    )
