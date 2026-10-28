"""Refresh reads identity's roles / permissions in its CAS statement.

The rotation's compare-and-set already returns the user; identity's claims
(the contributor's own aggregate, ``identity_claims_select``) are LATERAL-joined
into it and handed to claims collection as precomputed for
``contribute_identity_claims`` — one PostgreSQL round trip less per refresh
(perf review 2026-10-22, item 11). The token must carry exactly what the
contributor returns, with ``identity`` in ``claims_sources``.
"""

from typing import Any
from unittest.mock import Mock

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from sandloom.sdk.contracts import contract_method
from sandloom.sdk.security import decode_token
from sandloom.sdk.testing import bind_contract_session
from sandloom_identity.claims import contribute_identity_claims
from sandloom_identity.models import Permission, Role, RolePermission, UserRole

PW = "TestPassword123!"


def test_identity_has_exactly_the_one_contributor_the_cas_precomputes() -> None:
    """The refresh CAS stands in for ``contribute_identity_claims`` only. A
    second identity contributor would still run (``precomputed`` is per
    function), but the refresh would lose its saving: keep it at one, or fold
    the new one into ``identity_claims_select`` too."""
    from sandloom_identity import register

    recorder = Mock()
    register(recorder)
    calls = recorder.add_claims_contributor.call_args_list
    assert [c.args[0] for c in calls] == [contribute_identity_claims]


async def _grant(test_db: Any, user_id: Any) -> None:
    editor = Role(name="Editor", code="editor_t11")
    viewer = Role(name="Viewer", code="viewer_t11")  # no permissions
    write = Permission(name="Write", code="posts.write_t11")
    read = Permission(name="Read", code="posts.read_t11")
    test_db.add_all([editor, viewer, write, read])
    await test_db.flush()
    test_db.add_all(
        [
            RolePermission(role_id=editor.id, permission_id=write.id),
            RolePermission(role_id=editor.id, permission_id=read.id),
            UserRole(user_id=user_id, role_id=editor.id),
            UserRole(user_id=user_id, role_id=viewer.id),
        ]
    )
    await test_db.commit()


async def _refresh(
    async_client: AsyncClient, test_db: Any, email: str
) -> tuple[dict[str, Any], list[str]]:
    login = await async_client.post(
        "/auth/login", json={"email": email, "password": PW}
    )
    assert login.status_code == 200, login.text
    await test_db.flush()
    statements: list[str] = []

    def _count(_conn: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        statements.append(sql)

    engine = test_db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        resp = await async_client.post(
            "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
        )
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert resp.status_code == 200, resp.text
    return decode_token(resp.json()["access_token"]), statements


@pytest.mark.asyncio
async def test_refresh_claims_equal_the_contributor_without_its_query(
    async_client: AsyncClient, test_db: Any, make_user: Any
) -> None:
    user = await make_user("refresh-claims@example.com", password=PW)
    await _grant(test_db, user.id)

    claims, statements = await _refresh(
        async_client, test_db, "refresh-claims@example.com"
    )

    with bind_contract_session(test_db):
        expected = await contract_method(contribute_identity_claims)(user.id)
    assert expected == {
        "roles": ["editor_t11", "viewer_t11"],
        "permissions": ["posts.read_t11", "posts.write_t11"],
    }
    assert claims["roles"] == expected["roles"]
    assert claims["permissions"] == expected["permissions"]
    assert "identity" in claims["claims_sources"]
    assert "identity" not in claims["claims_sources_failed"]
    # Identity's claims came with the CAS; its own query did not run.
    assert sum("array_agg" in s for s in statements) == 1
    assert not any(s.lstrip().startswith("SELECT array_agg") for s in statements)


@pytest.mark.asyncio
async def test_refresh_of_a_user_without_roles(
    async_client: AsyncClient, test_db: Any, make_user: Any
) -> None:
    """The aggregate always yields one row: no roles never drops the user."""
    await make_user("refresh-noroles@example.com", password=PW)

    claims, _ = await _refresh(async_client, test_db, "refresh-noroles@example.com")

    assert claims["roles"] == [] and claims["permissions"] == []
    assert "identity" in claims["claims_sources"]
