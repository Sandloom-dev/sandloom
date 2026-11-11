"""Fixture control: set the forged contributor's answer."""

from typing import Any

from pydantic import BaseModel

from sandloom.sdk import module_router, public
from sandloom.sdk.web import no_idempotency

from . import state

router = module_router()


class PayloadIn(BaseModel):
    payload: dict[str, Any]


@router.post(
    "/set",
    dependencies=[
        public(reason="PoC fixture control"),
        no_idempotency(reason="PoC fixture; no client retries"),
    ],
)
async def set_payload(body: PayloadIn) -> dict[str, Any]:
    state.PAYLOAD.clear()
    state.PAYLOAD.update(body.payload)
    return state.PAYLOAD
