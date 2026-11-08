"""Saga definitions and their deterministic checks (ADR-0008 §1, §3, §14).

Structure at construction, contracts at registry build (startup), the
consumed-contract requirements, the outcome table (§7) and the guarantee
window (§9). No database.

Moved from ``sandloom/core/tests/test_sagas_definition.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from sandloom.core.descriptors import (
    ModuleDescriptor,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.loader import plugin_loader
from sandloom.core.module_api import (
    Saga,
    SagaContext,
    SagaDefinitionError,
    contract_method,
    local_step,
    step,
)
from sandloom.core.module_api._registration import pending_saga_requirements
from sandloom.core.sagas.registry import build_registry


class In(BaseModel):
    user_id: uuid.UUID
    amount: int


def grant_args(c: SagaContext[In]) -> dict[str, object]:
    return {"tenant_id": c.input.user_id, "amount": c.input.amount}


async def _apply(db: Any, c: SagaContext[In]) -> None:
    return None


class Credits:
    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def grant(cls, tenant_id: uuid.UUID, amount: int, operation_key: str) -> int:
        return amount

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def revoke(cls, tenant_id: uuid.UUID, amount: int, operation_key: str) -> int:
        return amount

    @classmethod
    @contract_method
    async def balance(cls, tenant_id: uuid.UUID) -> int:
        return 0


def _saga(*steps: Any, name: str = "org_setup", **kw: Any) -> Saga[In]:
    return Saga[In](
        name,
        version=1,
        input=In,
        deadline=timedelta(minutes=5),
        steps=list(steps),
        **kw,
    )


def _grant(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "call": "dfbilling.credits.v1.grant",
        "args": grant_args,
        "compensate": "dfbilling.credits.v1.revoke",
    }
    base.update(kw)
    return step("grant_trial_credits", **base)


def _core(*sagas: Saga[Any], module: str = "sandloom_tenancy") -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.register_service("dfbilling.credits.v1", Credits)
    for saga in sagas:
        core.register_saga(saga, module)
    return core


def test_unregistered_definition_has_no_type() -> None:
    saga = _saga(_grant(), local_step("p", kind="pivot", apply=_apply))
    with pytest.raises(SagaDefinitionError, match="not registered"):
        _ = saga.saga_type
    assert saga.bind("sandloom_tenancy") == "tenancy.org_setup"
    with pytest.raises(SagaDefinitionError, match="already registered"):
        saga.bind("sandloom_billing")


# --------------------------------------------------------------------------- #
# contracts (startup)
# --------------------------------------------------------------------------- #
def test_a_matching_definition_builds() -> None:
    saga = _saga(_grant(), local_step("p", kind="pivot", apply=_apply))
    registry = build_registry(_core(saga))
    reg = next(iter(registry))
    assert reg.saga_type == "tenancy.org_setup"
    assert reg.channel == "tenancy.saga_advance.v1"
    assert reg.key_params[("grant_trial_credits", "do")] == "operation_key"
    assert reg.key_params[("grant_trial_credits", "undo")] == "operation_key"
    assert registry.describe()[0]["steps"][0]["call"] == "dfbilling.credits.v1.grant"


def test_register_saga_buffers_and_the_loader_merges_consumes() -> None:
    core = CoreFacade(app=FastAPI())
    saga = _saga(_grant(), local_step("p", kind="pivot", apply=_apply), name="onboard")
    with core.module_registration("sandloom_tenancy") as api:
        api.register_saga(saga)
        assert pending_saga_requirements(api)[0].methods == ("grant", "revoke")
        desc = ModuleDescriptor(name="sandloom_tenancy", version="1")
        plugin_loader._merge_saga_requirements(desc, api)
        plugin_loader._merge_saga_requirements(desc, api)  # idempotent
    assert [r.name for r in desc.consumes] == ["dfbilling.credits.v1"]
    assert core.sagas == [(saga, "sandloom_tenancy")]
    assert saga.saga_type == "tenancy.onboard"
