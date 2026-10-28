"""Enrollment routes: 202 + the semantic lock, before any step ran."""

import uuid

from httpx import AsyncClient

from sandloom.sdk.testing import as_user


async def test_enroll_answers_202_and_starts_the_saga(client: AsyncClient) -> None:
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
