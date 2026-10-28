"""Hidden acceptance tests: does the product do what the spec says?"""

from __future__ import annotations

import uuid
from typing import Any

from _academy import balance, course, enroll, eventually, grant_credits, needs, org

pytestmark = []


@needs("catalog")
async def test_acc_catalog_create_list_get(live: Any) -> None:
    c = live
    _, _, h = await org(c)
    made = await course(c, h, lessons=3, price=5, capacity=None)
    assert {"id", "title", "lessons", "price_credits", "capacity"} <= set(made)
    listed = await c.get("/api/v1/catalog/courses", headers=h)
    assert listed.status_code == 200 and [x["id"] for x in listed.json()] == [
        made["id"]
    ]
    one = await c.get(f"/api/v1/catalog/courses/{made['id']}", headers=h)
    assert one.status_code == 200 and one.json()["lessons"] == 3


@needs("catalog")
async def test_acc_catalog_validates(live: Any) -> None:
    c = live
    _, _, h = await org(c)
    bad = await c.post(
        "/api/v1/catalog/courses",
        json={"title": "", "lessons": 0, "price_credits": -1, "capacity": None},
        headers={**h, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert bad.status_code == 422


@needs("catalog", "enrollment")
async def test_acc_enroll_charges_price(live: Any) -> None:
    c = live
    org_id, _, h = await org(c)
    await grant_credits(org_id, 50)
    cr = await course(c, h, price=10)
    resp = await enroll(c, h, cr["id"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "active"
    assert await balance(c, org_id, h) == 40
    listed = await c.get(
        f"/api/v1/enrollment/enrollments?course_id={cr['id']}", headers=h
    )
    assert listed.status_code == 200 and len(listed.json()) == 1


@needs("catalog", "enrollment")
async def test_acc_enroll_without_credits_is_402(live: Any) -> None:
    c = live
    org_id, _, h = await org(c)
    await grant_credits(org_id, 5)
    cr = await course(c, h, price=10)
    resp = await enroll(c, h, cr["id"])
    assert resp.status_code == 402, resp.text
    listed = await c.get("/api/v1/enrollment/enrollments", headers=h)
    assert listed.json() == []
    assert await balance(c, org_id, h) == 5


@needs("catalog", "enrollment")
async def test_acc_enroll_conflicts_are_409(live: Any) -> None:
    c = live
    org_id, _, h = await org(c)
    await grant_credits(org_id, 100)
    cr = await course(c, h, price=1, capacity=1)
    learner = uuid.uuid4()
    assert (await enroll(c, h, cr["id"], learner)).status_code == 201
    assert (
        await enroll(c, h, cr["id"], learner)
    ).status_code == 409  # already enrolled
    assert (await enroll(c, h, cr["id"])).status_code == 409  # at capacity
    assert await balance(c, org_id, h) == 99


@needs("catalog", "enrollment", "progress")
async def test_acc_progress_appears_and_completes(live: Any) -> None:
    c = live
    org_id, _, h = await org(c)
    await grant_credits(org_id, 10)
    cr = await course(c, h, lessons=2, price=0)
    e = (await enroll(c, h, cr["id"])).json()

    async def ready() -> Any:
        r = await c.get(f"/api/v1/progress/{e['id']}", headers=h)
        return r.json() if r.status_code == 200 else None

    p = await eventually(ready)
    assert p and p["completed_lessons"] == 0 and p["total_lessons"] == 2
    done1 = await c.post(f"/api/v1/progress/{e['id']}/lessons/1/complete", headers=h)
    again = await c.post(f"/api/v1/progress/{e['id']}/lessons/1/complete", headers=h)
    assert done1.status_code == 200 and again.json()["completed_lessons"] == 1
    out = await c.post(f"/api/v1/progress/{e['id']}/lessons/9/complete", headers=h)
    assert out.status_code == 422
    last = await c.post(f"/api/v1/progress/{e['id']}/lessons/2/complete", headers=h)
    assert last.json()["completed"] is True


@needs("catalog", "enrollment", "progress", "certificates")
async def test_acc_completion_issues_one_certificate(live: Any) -> None:
    c = live
    org_id, _, h = await org(c)
    cr = await course(c, h, lessons=1, price=0)
    learner = uuid.uuid4()
    e = (await enroll(c, h, cr["id"], learner)).json()

    async def progress_ready() -> bool:
        return (
            await c.get(f"/api/v1/progress/{e['id']}", headers=h)
        ).status_code == 200

    assert await eventually(progress_ready)
    await c.post(f"/api/v1/progress/{e['id']}/lessons/1/complete", headers=h)

    async def certs() -> Any:
        r = await c.get(f"/api/v1/certificates?learner_id={learner}", headers=h)
        return r.json() if r.status_code == 200 and r.json() else None

    got = await eventually(certs)
    assert got and len(got) == 1 and got[0]["course_id"] == cr["id"]


@needs("catalog", "enrollment", "progress", "reporting")
async def test_acc_reporting_counts(live: Any) -> None:
    c = live
    org_id, _, h = await org(c)
    cr = await course(c, h, lessons=1, price=0, title="Reported")
    e = (await enroll(c, h, cr["id"])).json()

    async def progress_ready() -> bool:
        return (
            await c.get(f"/api/v1/progress/{e['id']}", headers=h)
        ).status_code == 200

    await eventually(progress_ready)
    await c.post(f"/api/v1/progress/{e['id']}/lessons/1/complete", headers=h)

    async def row() -> Any:
        r = await c.get("/api/v1/reporting/courses", headers=h)
        rows = (
            [x for x in r.json() if x.get("course_id") == cr["id"]]
            if r.status_code == 200
            else []
        )
        return rows[0] if rows and rows[0].get("completions") == 1 else None

    got = await eventually(row)
    assert got and got["enrollments"] == 1 and got["title"] == "Reported"


@needs("reminders")
async def test_acc_reminders_endpoint(live: Any) -> None:
    c = live
    _, _, h = await org(c)
    resp = await c.get("/api/v1/reminders/sent", headers=h)
    assert resp.status_code == 200 and resp.json() == []


@needs("announcements")
async def test_acc_announcements_post_list_and_course_hook(
    live: Any,
) -> None:
    c = live
    _, _, h = await org(c)
    made = await c.post(
        "/api/v1/announcements",
        json={"text": "hello"},
        headers={**h, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert made.status_code == 201, made.text
    if (await c.get("/api/v1/catalog/courses", headers=h)).status_code == 200:
        await course(c, h, title="Rust")

        async def hooked() -> bool:
            r = await c.get("/api/v1/announcements", headers=h)
            return any("Rust" in x["text"] for x in r.json())

        assert await eventually(hooked)
    listed = (await c.get("/api/v1/announcements", headers=h)).json()
    assert listed[-1]["text"] == "hello"  # newest first
