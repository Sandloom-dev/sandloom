"""A saga with a typed step (spec §3)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from sdkfix_shared.contracts.sdkfix_stats_quota_v1 import SDKFIX_STATS_QUOTA_V1

from sandloom.sdk import Contract
from sandloom.sdk.sagas import Saga, step

quota = Contract(SDKFIX_STATS_QUOTA_V1)


class ReserveInput(BaseModel):
    amount: int


def _args(ctx: Any) -> dict[str, Any]:
    return {"amount": ctx.input.amount}


RESERVE = Saga(
    "reserve",
    version=1,
    input=ReserveInput,
    deadline=timedelta(minutes=5),
    steps=[
        step("reserve", call=quota.reserve, args=_args, compensate=quota.release),
    ],
)
