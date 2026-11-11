"""Grant / revoke a tenant; the fault switch."""

from uuid import UUID

from pydantic import BaseModel

from sandloom.sdk import module_router, public
from sandloom.sdk.security import revoke_user_tokens_on_commit
from sandloom.sdk.web import DbSession, no_idempotency

from . import state

router = module_router()
_FIXTURE = [
    public(reason="PoC fixture control (an operator in a real provider)"),
    no_idempotency(reason="PoC fixture; no client retries"),
]


class GrantIn(BaseModel):
    user_id: UUID
    tenant_id: UUID


class DownIn(BaseModel):
    on: bool


@router.post("/grant", dependencies=_FIXTURE)
async def grant(body: GrantIn) -> dict[str, list[str]]:
    state.GRANTS.setdefault(str(body.user_id), set()).add(str(body.tenant_id))
    return {"tenant_ids": sorted(state.GRANTS[str(body.user_id)])}


@router.post("/revoke", dependencies=_FIXTURE)
async def revoke(body: GrantIn, db: DbSession) -> dict[str, object]:
    state.GRANTS.get(str(body.user_id), set()).discard(str(body.tenant_id))
    valid_after = await revoke_user_tokens_on_commit(
        body.user_id, reason="grant revoked"
    )
    return {
        "tenant_ids": sorted(state.GRANTS.get(str(body.user_id), set())),
        "valid_after": valid_after,
    }


@router.post("/down", dependencies=_FIXTURE)
async def down(body: DownIn) -> dict[str, bool]:
    state.DOWN["on"] = body.on
    return state.DOWN
