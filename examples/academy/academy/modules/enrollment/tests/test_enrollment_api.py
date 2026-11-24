"""Enrollment routes: 202 + the semantic lock, before any step ran."""

import uuid

from httpx import AsyncClient

from sandloom.sdk.testing import as_user


async def test_enroll_answers_202_and_starts_the_saga(
    real_uow_client: AsyncClient,
) -> None:
    # Real request units: the saga's status is read in its own unit, which sees
    # the instance only once the enrolling request has committed.
    client = real_uow_client
    _, headers = as_user()
    resp = await client.post(
        "/api/v1/enrollments",
        json={"course_id": str(uuid.uuid4()), "price": 30},
        headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "running"
    got = await client.get(body["status_url"], headers=headers)
    assert got.status_code == 200
    assert got.json()["status"] == "pending"  # the semantic lock
    assert got.json()["saga_status"] == "running"
    assert got.json()["saga_step"] == "reserve_seat"


async def test_enroll_without_idempotency_key_is_refused(
    client: AsyncClient,
) -> None:
    # The explicit durable guard is required: a keyless POST would start a
    # second saga on every retry.
    _, headers = as_user()
    resp = await client.post(
        "/api/v1/enrollments",
        json={"course_id": str(uuid.uuid4()), "price": 30},
        headers=headers,
    )
    assert resp.status_code in (400, 428), resp.text


async def test_enroll_retry_replays_the_stored_body(
    real_uow_client: AsyncClient,
) -> None:
    # A keyed retry returns the first answer with its enrollment id (not an
    # empty body) and starts no second saga.
    client = real_uow_client
    _, headers = as_user()
    keyed = {**headers, "Idempotency-Key": uuid.uuid4().hex}
    payload = {"course_id": str(uuid.uuid4()), "price": 30}
    first = await client.post("/api/v1/enrollments", json=payload, headers=keyed)
    assert first.status_code == 202, first.text
    second = await client.post("/api/v1/enrollments", json=payload, headers=keyed)
    assert second.status_code == 202, second.text
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json() == first.json()
    assert second.json()["enrollment_id"]


async def test_enroll_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/enrollments", json={"course_id": str(uuid.uuid4()), "price": 1}
    )
    assert resp.status_code == 401


async def test_another_tenant_cannot_read(client: AsyncClient) -> None:
    _, mine = as_user()
    created = await client.post(
        "/api/v1/enrollments",
        json={"course_id": str(uuid.uuid4()), "price": 30},
        headers={**mine, "Idempotency-Key": uuid.uuid4().hex},
    )
    _, theirs = as_user()
    resp = await client.get(created.json()["status_url"], headers=theirs)
    assert resp.status_code == 404
