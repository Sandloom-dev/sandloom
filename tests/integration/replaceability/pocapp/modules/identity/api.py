"""Mint endpoint (same as test_identity's)."""

from typing import Any
from uuid import UUID

from fastapi import Depends
from pydantic import BaseModel

from sandloom.sdk import module_router, public
from sandloom.sdk.infra import CoreRuntime, get_core
from sandloom.sdk.security import create_access_token
from sandloom.sdk.web import no_idempotency

router = module_router()


class MintIn(BaseModel):
    user_id: UUID


class MintOut(BaseModel):
    access_token: str
    claims: dict[str, Any]


@router.post(
    "/mint",
    dependencies=[
        public(reason="PoC stand-in for a login"),
        no_idempotency(reason="PoC fixture; no client retries"),
    ],
)
async def mint(body: MintIn, core: CoreRuntime = Depends(get_core)) -> MintOut:
    claims = await core.collect_claims(body.user_id)
    token = create_access_token(
        data={"sub": str(body.user_id), "token_type": "access"}, claims=claims
    )
    return MintOut(access_token=token, claims=dict(claims))
