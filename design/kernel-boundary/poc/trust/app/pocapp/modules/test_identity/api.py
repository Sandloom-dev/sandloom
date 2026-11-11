"""Mint endpoint: the issuer's half of a login, without the credential check.

kb-poc-trust adds ``/hand-mint`` (T-11: module code assembling protected
claims and provenance by hand) and ``/service-mint`` (the limited path).
"""

from typing import Any
from uuid import UUID

from fastapi import Depends
from pydantic import BaseModel

from sandloom.sdk import module_router, public
from sandloom.sdk.infra import CoreRuntime, get_core
from sandloom.sdk.security import create_access_token
from sandloom.sdk.web import no_idempotency

from .._mint import mint as _mint
from . import state

try:  # TA-1 kernel only
    from sandloom.sdk.security import create_service_token
except ImportError:  # base kernel
    create_service_token = None  # type: ignore[assignment]

router = module_router()
_FIXTURE = [
    public(reason="PoC stand-in for a login"),
    no_idempotency(reason="PoC fixture; no client retries"),
]


class MintIn(BaseModel):
    user_id: UUID


class MintOut(BaseModel):
    access_token: str
    claims: dict[str, Any]


class DownIn(BaseModel):
    on: bool


class HandIn(BaseModel):
    user_id: UUID
    claims: dict[str, Any]


@router.post("/mint", dependencies=_FIXTURE)
async def mint(body: MintIn, core: CoreRuntime = Depends(get_core)) -> MintOut:
    token, claims = await _mint(core, body.user_id)
    return MintOut(access_token=token, claims=claims)


@router.post("/hand-mint", dependencies=_FIXTURE)
async def hand_mint(body: HandIn) -> dict[str, Any]:
    data = {**body.claims, "sub": str(body.user_id), "token_type": "access"}
    try:
        return {"access_token": create_access_token(data=data)}
    except Exception as exc:  # noqa: BLE001 - the refusal is the result
        return {"refused": f"{type(exc).__name__}: {exc}"}


@router.post("/service-mint", dependencies=_FIXTURE)
async def service_mint(body: HandIn) -> dict[str, Any]:
    if create_service_token is None:
        return {"refused": "no create_service_token in this kernel"}
    data = {**body.claims, "sub": str(body.user_id), "token_type": "access"}
    try:
        return {"access_token": create_service_token(data=data)}
    except Exception as exc:  # noqa: BLE001 - the refusal is the result
        return {"refused": f"{type(exc).__name__}: {exc}"}


@router.post("/down", dependencies=_FIXTURE)
async def down(body: DownIn) -> dict[str, bool]:
    state.DOWN["on"] = body.on
    return state.DOWN
