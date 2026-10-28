"""Hidden footgun probes: what the framework promises to handle under the hood.

Each test is one class of production incident. Target: 0 failures.
"""

from __future__ import annotations

import ast
import re
import uuid
from typing import Any

from _academy import (
    balance,
    course,
    enroll,
    enrolled,
    eventually,
    grant_credits,
    has,
    needs,
    org,
)
from _dx import exempt_tables, module_dir

from sandloom.sdk.security import revoke_user_tokens
from sandloom.sdk.testing import as_user

PRODUCT = (
    "catalog",
    "enrollment",
    "progress",
    "certificates",
    "reporting",
    "reminders",
    "announcements",
)
PRESENT = [m for m in PRODUCT if has(m)]
PREFIXES = tuple(f"/api/v1/{m}" for m in PRESENT)


# --- cross-tenant leaks --------------------------------------------------- #


@needs("catalog")
async def test_fg_cross_tenant_course_is_invisible(live: Any) -> None:
    c = live
    _, _, a = await org(c)
    _, _, b = await org(c)
    mine = await course(c, a)
    assert (
        await c.get(f"/api/v1/catalog/courses/{mine['id']}", headers=b)
    ).status_code == 404
    assert (await c.get("/api/v1/catalog/courses", headers=b)).json() == []


@needs("catalog", "enrollment")
async def test_fg_cannot_enroll_into_foreign_course(live: Any) -> None:
    c = live
    _, _, a = await org(c)
    b_org, _, b = await org(c)
    await grant_credits(b_org, 100)
    foreign = await course(c, a, price=1)
    resp = await enroll(c, b, foreign["id"])
    assert resp.status_code == 404, resp.text
    assert await balance(c, b_org, b) == 100


@needs("catalog", "enrollment", "progress")
async def test_fg_cross_tenant_progress_is_invisible(live: Any) -> None:
    c = live
    a_org, _, a = await org(c)
    _, _, b = await org(c)
    cr = await course(c, a, price=0)
    e = await enrolled(c, a, cr["id"])

    async def ready() -> bool:
        return (
            await c.get(f"/api/v1/progress/{e['id']}", headers=a)
        ).status_code == 200

    await eventually(ready)
    assert (await c.get(f"/api/v1/progress/{e['id']}", headers=b)).status_code == 404
    hit = await c.post(f"/api/v1/progress/{e['id']}/lessons/1/complete", headers=b)
    assert hit.status_code in (403, 404)


def test_fg_per_org_tables_are_tenant_scoped() -> None:
    """Every product table is TenantScoped (or exempt with a reason)."""
    rules = exempt_tables()
    bad = []
    for module in PRESENT:
        for path in module_dir(module).rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                table = None
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.Assign)
                        and any(
                            isinstance(t, ast.Name) and t.id == "__tablename__"
                            for t in stmt.targets
                        )
                        and isinstance(stmt.value, ast.Constant)
                    ):
                        table = str(stmt.value.value)
                bases = " ".join(ast.unparse(b) for b in node.bases)
                if table and "TenantScoped" not in bases and table not in rules:
                    bad.append(f"{module}.{node.name} ({table})")
    assert not bad, bad


# --- money: double charge on retry ---------------------------------------- #


@needs("catalog", "enrollment")
async def test_fg_retry_does_not_charge_twice(live: Any) -> None:
    c = live
    org_id, _, h = await org(c)
    await grant_credits(org_id, 100)
    cr = await course(c, h, price=10)
    learner = uuid.uuid4()
    idem = {"Idempotency-Key": uuid.uuid4().hex}
    first = await enroll(c, h, cr["id"], learner, idem=idem)
    second = await enroll(c, h, cr["id"], learner, idem=idem)
    assert first.status_code == 201, first.text
    assert second.status_code in (200, 201, 409), second.text
    assert await balance(c, org_id, h) == 90
    listed = await c.get(
        f"/api/v1/enrollment/enrollments?course_id={cr['id']}", headers=h
    )
    assert len(listed.json()) == 1


@needs("catalog", "enrollment")
async def test_fg_rejected_enrollment_is_not_charged(live: Any) -> None:
    c = live
    org_id, _, h = await org(c)
    await grant_credits(org_id, 100)
    cr = await course(c, h, price=10, capacity=1)
    await enroll(c, h, cr["id"])
    full = await enroll(c, h, cr["id"])  # at capacity -> 409, must not spend
    assert full.status_code == 409
    assert await balance(c, org_id, h) == 90


def test_fg_mutating_routes_keep_automatic_idempotency() -> None:
    """Keyed retries are deduplicated by default (status replay); a write route
    may opt out only with a stated reason (no_idempotency(reason=...))."""
    from _dx import mutating_routes, opts_out_of_idempotency

    opted_out = [
        f"{module}: {r.method} {r.path} ({r.function})"
        for module in PRESENT
        for r in mutating_routes(module_dir(module))
        if opts_out_of_idempotency(r)
    ]
    assert not opted_out, opted_out


# --- events: lost or duplicated effects ------------------------------------ #


def test_fg_consumers_dedupe_durably() -> None:
    """Every module that consumes events claims them in a DB-atomic record."""
    bad = []
    for module in PRESENT:
        src = "\n".join(
            p.read_text()
            for p in module_dir(module).rglob("*.py")
            if "tests" not in p.parts
        )
        if "@on(" in src or "Projection(" in src:
            ok = (
                "durable=False" not in src  # @on claims each envelope id by default
                or "durable_inbox(" in src
                or "monotonic_upsert" in src
                or re.search(
                    r"UniqueConstraint|unique\s*=\s*True|primary_key=True", src
                )
            )
            if not ok:
                bad.append(module)
    assert not bad, bad


@needs("catalog", "enrollment", "progress", "certificates")
async def test_fg_completion_twice_one_certificate(live: Any) -> None:
    c = live
    _, _, h = await org(c)
    cr = await course(c, h, lessons=1, price=0)
    learner = uuid.uuid4()
    e = await enrolled(c, h, cr["id"], learner)

    async def ready() -> bool:
        return (
            await c.get(f"/api/v1/progress/{e['id']}", headers=h)
        ).status_code == 200

    await eventually(ready)
    for _ in range(2):
        await c.post(f"/api/v1/progress/{e['id']}/lessons/1/complete", headers=h)

    async def certs() -> Any:
        r = await c.get(f"/api/v1/certificates?learner_id={learner}", headers=h)
        return r.json() if r.status_code == 200 and r.json() else None

    got = await eventually(certs)
    assert got and len(got) == 1


# --- auth: unauthenticated routes, revoked sessions ----------------------- #


async def test_fg_every_product_route_requires_auth(
    live: Any, sandloom_app: Any
) -> None:
    open_routes = []
    for route in sandloom_app.routes:
        path = getattr(route, "path", "")
        if not path.startswith(PREFIXES):
            continue
        concrete = re.sub(r"\{[^}]*lesson[^}]*\}", "1", path)
        concrete = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), concrete)
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            resp = await live.request(method, concrete, json={})
            if resp.status_code not in (401, 403):
                open_routes.append(f"{method} {path} -> {resp.status_code}")
    assert PRESENT == [] or open_routes == [], open_routes


@needs("catalog")
async def test_fg_revoked_session_is_refused(live: Any) -> None:
    c = live
    _, owner, h = await org(c)
    assert (await c.get("/api/v1/catalog/courses", headers=h)).status_code == 200
    await revoke_user_tokens(owner)
    assert (await c.get("/api/v1/catalog/courses", headers=h)).status_code == 401
    _, fresh = as_user(user_id=owner)
    assert fresh  # a new login would mint a new token; the old one stays dead
