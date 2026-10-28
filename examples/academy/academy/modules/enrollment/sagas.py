"""Sagas of the enrollment module (ADR-0008) — the one place they live.

ENROLL: reserve_seat -> charge -> grant_access (pivot) -> notify.

* ``reserve_seat`` (local, compensatable): the row moves pending -> reserved
  in the transition's own transaction; its compensation cancels it.
* ``charge`` (``charges.charge`` of ``payments.charges.v1``, compensatable):
  a 402 refusal (short balance) compensates ``reserve_seat``; the
  compensation ``charges.refund`` accepts "nothing to undo".
* ``grant_access`` (local, the pivot): active. Past it there is no undo.
* ``notify`` (``mail.send`` of ``notifications.mail.v1``, retriable): never
  refuses; retried until the deadline, then parked for an operator.

Command steps name their command through the typed callers below (the
generated stubs of ``academy.shared.contracts``), never a string. Argument
functions are pure, named functions of ``c.input`` and the results; the
runtime injects every command's ``operation_key``
(``saga:enrollment.enroll:<id>:<step>:do|undo``). ``define_module``
discovers ``ENROLL`` here and registers it.
"""

from datetime import timedelta
from typing import Any

from academy.shared.contracts.notifications_mail_v1 import NOTIFICATIONS_MAIL_V1
from academy.shared.contracts.payments_charges_v1 import PAYMENTS_CHARGES_V1
from sandloom.sdk import Contract
from sandloom.sdk.sagas import (  # sandloom: advanced sagas (ADR-0008)
    Saga,
    SagaContext,
    local_step,
    step,
)

from .schemas.sagas import EnrollInput
from .services import Enrollments

charges = Contract(PAYMENTS_CHARGES_V1)
mail = Contract(NOTIFICATIONS_MAIL_V1)


async def reserve_seat(db: Any, c: SagaContext[EnrollInput]) -> str:
    return await Enrollments.reserve(db, c.input.enrollment_id)


async def release_seat(db: Any, c: SagaContext[EnrollInput]) -> str:
    return await Enrollments.cancel(db, c.input.enrollment_id)


def charge_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"customer_id": c.input.student_id, "amount": c.input.price}


def refund_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"customer_id": c.input.student_id, "charge_key": c.key("charge")}


async def grant_access(db: Any, c: SagaContext[EnrollInput]) -> str:
    return await Enrollments.activate(db, c.input.enrollment_id)


def notify_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"recipient_id": c.input.student_id, "template": "enrolled"}


ENROLL = Saga(
    "enroll",
    version=1,
    input=EnrollInput,
    deadline=timedelta(minutes=10),
    steps=[
        local_step("reserve_seat", apply=reserve_seat, compensate=release_seat),
        step(
            "charge",
            call=charges.charge,
            args=charge_args,
            compensate=charges.refund,
            compensate_args=refund_args,
        ),
        local_step("grant_access", kind="pivot", apply=grant_access),
        step("notify", call=mail.send, args=notify_args),
    ],
)
