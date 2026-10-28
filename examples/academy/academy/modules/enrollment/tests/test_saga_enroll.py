"""The ENROLL saga against the REAL providers (payments + notifications are
co-loaded), driven through the real protocol by ``saga_harness``.

Routes run through ``real_uow_client`` (real commits): the POST commits the
enrollment row, the saga instance and its first nudge in one transaction;
the harness then delivers the nudges the runtime publishes.
"""

import uuid
from typing import Any

from httpx import AsyncClient

from sandloom.sdk.testing import as_user

from ..sagas import ENROLL
from ..schemas import EnrollInput


async def _enroll(http: AsyncClient, headers: dict[str, str], price: int) -> str:
    resp = await http.post(
        "/api/v1/enrollments",
        json={"course_id": str(uuid.uuid4()), "price": price},
        headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert resp.status_code == 202, resp.text
    return str(resp.json()["enrollment_id"])


async def _topup(http: AsyncClient, headers: dict[str, str], amount: int) -> None:
    resp = await http.post(
        "/api/v1/payments/topup", json={"amount": amount}, headers=headers
    )
    assert resp.status_code == 200, resp.text


async def test_enrollment_completes_charges_once_and_notifies(
    saga_harness: Any, real_uow_client: AsyncClient
) -> None:
    _, headers = as_user()
    await _topup(real_uow_client, headers, 100)
    enrollment_id = await _enroll(real_uow_client, headers, 30)

    run = await saga_harness.drive(ENROLL, enrollment_id)

    assert run.status == "completed"
    assert run.calls == ["reserve_seat:do", "charge:do", "grant_access:do", "notify:do"]
    run.assert_keys_derived()
    got = (
        await real_uow_client.get(
            f"/api/v1/enrollments/{enrollment_id}", headers=headers
        )
    ).json()
    assert got["status"] == "active" and got["saga_status"] == "completed"
    balance = (
        await real_uow_client.get("/api/v1/payments/balance", headers=headers)
    ).json()
    assert balance["balance"] == 70
    inbox = (
        await real_uow_client.get("/api/v1/notifications/inbox", headers=headers)
    ).json()
    assert [m["template"] for m in inbox] == ["enrolled"]


async def test_short_balance_compensates_and_cancels_the_seat(
    saga_harness: Any, real_uow_client: AsyncClient
) -> None:
    _, headers = as_user()
    await _topup(real_uow_client, headers, 10)
    enrollment_id = await _enroll(real_uow_client, headers, 30)

    run = await saga_harness.drive(ENROLL, enrollment_id)

    assert run.status == "compensated"
    assert run.calls == ["reserve_seat:do", "charge:do", "reserve_seat:undo"]
    assert run.step_row("charge")["last_error"]["status"] == 402
    got = (
        await real_uow_client.get(
            f"/api/v1/enrollments/{enrollment_id}", headers=headers
        )
    ).json()
    assert got["status"] == "cancelled"
    balance = (
        await real_uow_client.get("/api/v1/payments/balance", headers=headers)
    ).json()
    assert balance["balance"] == 10


async def test_crash_after_the_charge_never_charges_twice(
    saga_harness: Any, real_uow_client: AsyncClient
) -> None:
    _, headers = as_user()
    await _topup(real_uow_client, headers, 100)
    enrollment_id = await _enroll(real_uow_client, headers, 30)
    saga_harness.crash_after("charge")

    run = await saga_harness.drive(ENROLL, enrollment_id)

    assert run.status == "completed" and run.crashes == 1
    balance = (
        await real_uow_client.get("/api/v1/payments/balance", headers=headers)
    ).json()
    assert balance["balance"] == 70  # the re-call replayed the committed charge


async def test_compensations_are_idempotent(
    saga_harness: Any, real_uow_client: AsyncClient
) -> None:
    tenant = uuid.uuid4()
    user, headers = as_user(tenant_id=tenant)
    await _topup(real_uow_client, headers, 1000)
    enrollment_id = await _enroll(real_uow_client, headers, 30)
    payload = EnrollInput(
        enrollment_id=uuid.UUID(enrollment_id),
        student_id=user,
        course_id=uuid.uuid4(),
        price=30,
    )
    await saga_harness.assert_compensations_idempotent(
        ENROLL, input=payload, tenant_id=tenant
    )
    await saga_harness.assert_resolves_before_compensating(
        ENROLL, input=payload, tenant_id=tenant
    )
