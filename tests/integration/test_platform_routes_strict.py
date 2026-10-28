"""Every authenticated route of the composed platform is strict (ADR-0009).

An independent behavioural oracle (spec §10.1): for EVERY route of the
batteries monolith that authenticates its caller, a token whose subject was
revoked, or whose subject is blocked, is refused with ``401`` on the very next
request — read routes, write routes, admin and billing routes alike. Nothing
is derived from a route policy: the sweep calls each route over HTTP.
"""

from __future__ import annotations

import re
import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

from sandloom.core.auth import marks, tokens
from sandloom.core.guard_order import authenticator_kind, resolution_order
from sandloom.core.testing.fixtures import *  # noqa: F403

_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")


def _authenticated_routes(app: Any) -> list[tuple[str, str]]:
    out = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        kinds = {authenticator_kind(c) for c in resolution_order(route.dependant)}
        if "user" not in kinds:  # service routes take a system credential
            continue
        path = _PARAM.sub(lambda _m: str(uuid4()), route.path)
        for method in sorted(route.methods - {"HEAD"}):
            out.append((method, path))
    return sorted(out)


def _token(sub: str) -> dict[str, str]:
    token = tokens.create_access_token(
        data={"sub": sub, "token_type": "access", "tenant_ids": [str(uuid4())]}
    )
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": f"k-{uuid4()}"}


@pytest.mark.parametrize("state", ["revoked", "blocked"])
async def test_every_authenticated_platform_route_refuses_a_withdrawn_session(
    sandloom_app: Any, client: Any, sandloom_cache: Any, state: str
) -> None:
    routes = _authenticated_routes(sandloom_app)
    assert len(routes) > 40  # the platform's whole authenticated surface
    sub = str(uuid4())
    headers = _token(sub)
    if state == "revoked":
        value = marks._format_mark(marks._seconds_to_us(time.time() + 1))
    else:
        value = marks._format_blocked(marks._seconds_to_us(time.time()))
    await sandloom_cache.set(marks.subject_key(sub), value)
    expected = "Token has been revoked" if state == "revoked" else "Account blocked"
    wrong: list[str] = []
    for method, path in routes:
        resp = await client.request(
            method,
            path,
            headers=headers,
            json={} if method not in ("GET", "DELETE") else None,
        )
        # 401 everywhere; the framework authenticator names the reason
        # (identity's own authenticator answers "Invalid token")
        detail = resp.json().get("detail") if resp.status_code == 401 else None
        if detail not in (expected, "Invalid token"):
            wrong.append(f"{method} {path}: {resp.status_code} {resp.text[:80]}")
    assert not wrong, "\n".join(wrong)
