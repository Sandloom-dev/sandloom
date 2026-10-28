"""Test-only probe module for the split conformance matrix.

Not shipped (lives under ``src/tests``); loaded into EVERY process of every
deployment of the matrix via ``SANDLOOM_MODULES`` with ``tests/split_conformance``
on ``PYTHONPATH``. Each endpoint is gated by one authorization atom so the
matrix can compare the decision a process takes when the deciding module is
local, bound remotely (``SERVICE_BINDINGS``) or absent:

- ``OrgRole`` (``tenancy.membership.v1``);
- ``SubscriptionActive`` / ``Credits`` (``billing.entitlements.v1``, live);
- ``Entitled(...)`` (billing's access facts, ``billing.facts.v1`` when remote);
- ``Subscribed(of=Account.user())`` (claim-cheap, through the ``subscription.active`` fact).

It registers no event consumers and owns no tables.

With ``CONF_FAULT_INJECTION=1`` (only the matrix's ``fault-revocation``
deployment sets it) it also exposes ``POST /_conf/fault/revocation/{mode}``,
which makes THIS process's revocation-mark writes (``revoke_user_tokens``)
behave as: ``off`` (normal), ``outage`` (the cache is unreachable: the write
reports failure) or ``lost`` (the write is acknowledged but never lands — an
acknowledged write lost in a Redis failover). The read path is untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sandloom.shared.authz import Account

if TYPE_CHECKING:  # pragma: no cover
    from sandloom.core.descriptors import ModuleDescriptor
    from sandloom.core.module_api import ModuleAPI


def register(core: ModuleAPI) -> ModuleDescriptor:
    from fastapi import APIRouter, Depends

    from sandloom.core.descriptors import ModuleDescriptor
    from sandloom.shared.authz import (
        Credits,
        Entitled,
        OrgRole,
        Subscribed,
        SubscriptionActive,
        require,
    )

    router = APIRouter(prefix="/_conf", tags=["conformance"])

    @router.get("/orgrole/{org_id}", dependencies=[Depends(require(OrgRole("owner")))])
    async def orgrole(org_id: UUID) -> dict[str, bool]:
        return {"ok": True}

    @router.get("/member/{org_id}", dependencies=[Depends(require(OrgRole("member")))])
    async def member(org_id: UUID) -> dict[str, bool]:
        return {"ok": True}

    @router.get(
        "/subscription-active/{org_id}",
        dependencies=[Depends(require(SubscriptionActive(of=Account.org())))],
    )
    async def subscription_active(org_id: UUID) -> dict[str, bool]:
        return {"ok": True}

    @router.get(
        "/credits/{org_id}",
        dependencies=[Depends(require(Credits(5, of=Account.org())))],
    )
    async def credits(org_id: UUID) -> dict[str, bool]:
        return {"ok": True}

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
    async def entitled_org(org_id: UUID) -> dict[str, bool]:
        return {"ok": True}

    @router.get(
        "/subscribed", dependencies=[Depends(require(Subscribed(of=Account.user())))]
    )
    async def subscribed() -> dict[str, bool]:
        return {"ok": True}

    import os

    if os.environ.get("CONF_FAULT_INJECTION") == "1":
        _install_revocation_fault(router)

    core.include_router(router)
    return ModuleDescriptor(name="conformance_probe", version="0.0.1", capabilities=[])


_FAULT = {"mode": "off"}


def _install_revocation_fault(router: object) -> None:
    from typing import Any

    from fastapi import HTTPException

    from sandloom.core.auth import revocation
    from sandloom.core.di import public

    real = revocation.revoke_user_tokens

    async def _faulty(user_id: Any, *, valid_after: float | None = None) -> bool:
        mode = _FAULT["mode"]
        if mode == "outage":
            return False
        if mode == "lost":
            return True
        return await real(user_id, valid_after=valid_after)

    revocation.revoke_user_tokens = _faulty  # type: ignore[assignment]

    async def set_fault(mode: str) -> dict[str, str]:
        if mode not in ("off", "outage", "lost"):
            raise HTTPException(422, "unknown fault mode")
        _FAULT["mode"] = mode
        return {"mode": mode}

    router.add_api_route(  # type: ignore[attr-defined]
        "/fault/revocation/{mode}",
        set_fault,
        methods=["POST"],
        dependencies=[
            public(reason="test-only fault switch of the conformance harness")
        ],
    )
