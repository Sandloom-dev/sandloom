"""A saga used by the kernel's saga tests: charge -> grant (pivot) -> notify.

The providers are harness fakes (``saga_harness.fake_contract``) registered
per test, so every command call goes through the real durable command log.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from sandloom.core.module_api import Saga, SagaContext, local_step, step

BILLING = "sgbilling.credits.v1"
MAIL = "sgmail.outbox.v1"


class EnrollInput(BaseModel):
    customer_id: uuid.UUID
    course_id: uuid.UUID
    price: int


class Ledger:
    """In-memory provider state shared by the fakes of one test."""

    def __init__(self) -> None:
        self.balance = 100
        self.charges: dict[str, int] = {}
        self.refunds: dict[str, int] = {}
        self.mails: list[str] = []
        self.granted: list[str] = []

    async def spend(
        self, customer_id: uuid.UUID, amount: int, operation_key: str
    ) -> int:
        self.balance -= amount
        self.charges[operation_key] = amount
        return self.balance

    async def reverse(
        self,
        customer_id: uuid.UUID,
        amount: int,
        of_operation_key: str,
        operation_key: str,
    ) -> int:
        # "nothing to undo" is a success: refund only what was charged.
        charged = self.charges.pop(of_operation_key, 0)
        if charged:
            self.balance += charged
            self.refunds[of_operation_key] = charged
        return self.balance

    async def send(
        self, customer_id: uuid.UUID, template: str, operation_key: str
    ) -> str:
        self.mails.append(operation_key)
        return f"mail-{len(self.mails)}"


def charge_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"customer_id": c.input.customer_id, "amount": c.input.price}


def refund_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {
        "customer_id": c.input.customer_id,
        "amount": c.input.price,
        "of_operation_key": c.key("charge"),
    }


def notify_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"customer_id": c.input.customer_id, "template": "enrolled"}


GRANTS: list[str] = []


async def grant_access(db: Any, c: SagaContext[EnrollInput]) -> dict[str, object]:
    GRANTS.append(c.saga_id)
    return {"course_id": str(c.input.course_id), "balance": c.result("charge")}


def enroll_saga(
    name: str = "enroll",
    *,
    deadline: timedelta = timedelta(minutes=30),
    compensation_deadline: timedelta | None = None,
) -> Saga[EnrollInput]:
    return Saga[EnrollInput](
        name,
        version=1,
        input=EnrollInput,
        deadline=deadline,
        compensation_deadline=compensation_deadline,
        steps=[
            step(
                "charge",
                call=f"{BILLING}.spend",
                args=charge_args,
                compensate=f"{BILLING}.reverse",
                compensate_args=refund_args,
            ),
            local_step("grant_access", kind="pivot", apply=grant_access),
            step("notify", call=f"{MAIL}.send", args=notify_args),
        ],
    )


def install_fakes(harness: Any, ledger: Ledger) -> None:
    harness.fake_contract(BILLING, spend=ledger.spend, reverse=ledger.reverse)
    harness.fake_contract(MAIL, send=ledger.send)


def enroll_input(price: int = 30) -> EnrollInput:
    return EnrollInput(customer_id=uuid.uuid4(), course_id=uuid.uuid4(), price=price)
