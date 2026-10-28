"""Helpers for the hidden Academy acceptance tests and footgun probes.

The probes drive the product only through its public HTTP API (the spec's
paths), seed tenancy through tenancy's API and credits through billing's
service, and wait for eventually consistent effects by polling.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from _dx import module_dir  # both layouts: DX_WORKTREE or DX_APP (the runner sets it)

from sandloom.sdk.data import open_uow
from sandloom.sdk.testing import as_user


def has(module: str) -> bool:
    return (module_dir(module) / "__init__.py").exists()


def needs(*modules: str) -> pytest.MarkDecorator:
    missing = [m for m in modules if not has(m)]
    return pytest.mark.skipif(bool(missing), reason=f"not in this product: {missing}")


async def org(client: Any) -> tuple[uuid.UUID, uuid.UUID, dict[str, str]]:
    """A fresh organization; returns (org_id, owner_id, org-scoped headers)."""
    owner, headers = as_user()
    resp = await client.post(
        "/api/v1/orgs",
        json={"name": "Org", "slug": f"org-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    org_id = uuid.UUID(resp.json()["id"])
    _, scoped = as_user(user_id=owner, tenant_id=org_id)
    return org_id, owner, dict(scoped)


async def grant_credits(org_id: uuid.UUID, amount: int) -> None:
    from sandloom_billing.services import (
        BillingService,  # the product may not load billing
    )

    async with open_uow() as s:
        customer = await BillingService.get_or_create_customer(
            s, organization_id=org_id
        )
        await BillingService.add_credits(customer, amount, f"seed-{uuid.uuid4()}", s)


async def balance(client: Any, org_id: uuid.UUID, headers: dict[str, str]) -> int:
    resp = await client.get(f"/api/v1/billing/credits?org_id={org_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return int(resp.json()["balance"])


class SetupFailed(Exception):
    """A probe's setup step did not work (the product's own feature is
    broken): the probe could not run, so it proves no footgun either way.
    The scorer reports such a probe as not evaluated, never as an escape."""


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def course(
    client: Any,
    headers: dict[str, str],
    *,
    lessons: int = 2,
    price: int = 10,
    capacity: int | None = None,
    title: str = "Python 101",
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/catalog/courses",
        json={
            "title": title,
            "lessons": lessons,
            "price_credits": price,
            "capacity": capacity,
        },
        headers={**headers, **key()},
    )
    if resp.status_code != 201:
        raise SetupFailed(f"create course: {resp.status_code} {resp.text[:200]}")
    return dict(resp.json())


async def enrolled(
    client: Any,
    headers: dict[str, str],
    course_id: str,
    learner: uuid.UUID | None = None,
) -> dict[str, Any]:
    """An enrollment the probe needs as setup (201 with an ``id``)."""
    resp = await enroll(client, headers, course_id, learner)
    body = resp.json() if resp.status_code == 201 else {}
    if not isinstance(body, dict) or "id" not in body:
        raise SetupFailed(f"enroll: {resp.status_code} {resp.text[:200]}")
    return body


async def enroll(
    client: Any,
    headers: dict[str, str],
    course_id: str,
    learner: uuid.UUID | None = None,
    *,
    idem: dict[str, str] | None = None,
) -> Any:
    return await client.post(
        "/api/v1/enrollment/enrollments",
        json={"course_id": course_id, "learner_id": str(learner or uuid.uuid4())},
        headers={**headers, **(idem or key())},
    )


async def eventually(
    probe: Callable[[], Awaitable[Any]], *, timeout: float = 15.0
) -> Any:
    """Poll ``probe`` until it returns a truthy value (events are async)."""
    deadline = asyncio.get_running_loop().time() + timeout
    last: Any = None
    while asyncio.get_running_loop().time() < deadline:
        last = await probe()
        if last:
            return last
        await asyncio.sleep(0.25)
    return last
