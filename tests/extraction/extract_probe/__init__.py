"""Test-only probe module for the module-extraction scenario (not shipped).

Loaded into every process of the scenario (``tests/extraction`` on
``PYTHONPATH``). It spends / reads credits through the ``billing.credits.v1``
contract — locally in the monolith, over internal RPC once billing runs as
its own service — so the scenario can drive money-like commands through a
service that is killed mid-workload. It owns no tables and no consumers.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from sandloom.core.descriptors import ContractRequirement, ModuleDescriptor
from sandloom.core.di import get_core
from sandloom.sdk import public

Core = Annotated[Any, Depends(get_core)]


class SpendIn(BaseModel):
    tenant_type: str
    tenant_id: UUID
    amount: int
    key: str


router = APIRouter(
    prefix="/_x",
    tags=["extraction-probe"],
    dependencies=[
        public(reason="Test-only synthetic credit workload on loopback servers")
    ],
)


@router.post("/spend")
async def spend(body: SpendIn, rt: Core) -> dict[str, int]:
    credits = rt.get_service("billing.credits.v1")
    balance = await credits.spend(
        body.tenant_type, body.tenant_id, body.amount, body.key, reason="e2e"
    )
    return {"balance": int(balance)}


@router.get("/balance/{tenant_type}/{tenant_id}")
async def balance(tenant_type: str, tenant_id: UUID, rt: Core) -> dict[str, int]:
    credits = rt.get_service("billing.credits.v1")
    return {"balance": int(await credits.get_balance(tenant_type, tenant_id))}


def register(core: Any) -> ModuleDescriptor:
    core.include_router(router)
    return ModuleDescriptor(
        name="extract_probe",
        version="0.0.1",
        capabilities=[],
        consumes=[ContractRequirement("billing.credits.v1", required=False)],
    )
