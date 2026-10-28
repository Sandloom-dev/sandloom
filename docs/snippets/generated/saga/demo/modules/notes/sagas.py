"""The onboarding saga (ADR-0008): ``reserve``, then ``confirm``.

Replace the stand-in ledger contract (contracts.py) with the real provider's
command contract; the typed step names it through its generated reference.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from demo.shared.contracts.notes_onboarding_ledger_v1 import NOTES_ONBOARDING_LEDGER_V1
from sandloom.sdk import Contract
from sandloom.sdk.sagas import Saga, local_step, step  # sandloom: advanced sagas

ledger = Contract(NOTES_ONBOARDING_LEDGER_V1)


class OnboardingInput(BaseModel):
    owner_id: UUID
    amount: int


def _amount(ctx: Any) -> dict[str, Any]:
    return {"amount": ctx.input.amount}


async def _confirm(session: Any, ctx: Any) -> dict[str, str]:
    return {"owner_id": str(ctx.input.owner_id)}


ONBOARDING = Saga(
    "onboarding",
    version=1,
    input=OnboardingInput,
    deadline=timedelta(minutes=10),
    steps=[
        step("reserve", call=ledger.record, args=_amount, compensate=ledger.record),
        local_step("confirm", apply=_confirm, kind="pivot"),
    ],
)
