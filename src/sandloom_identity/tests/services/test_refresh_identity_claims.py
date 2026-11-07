"""Refresh reads identity's roles / permissions with the token it rotates.

The statement that loads the presented token already loads its user;
identity's claims (the contributor's own aggregates, ``identity_claims_columns``)
ride it as correlated scalar subqueries and are handed to claims collection as
precomputed for ``contribute_identity_claims`` — one round trip less per
refresh (perf review 2026-10-22, item 11), on every database. The token must
carry exactly what the contributor returns, with ``identity`` in
``claims_sources``.
"""

from typing import Any
from unittest.mock import Mock

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select

from sandloom.sdk.contracts import contract_method
from sandloom.sdk.data import aggregate_list
from sandloom.sdk.security import decode_token
from sandloom.sdk.testing import bind_contract_session
from sandloom_identity.claims import contribute_identity_claims
from sandloom_identity.models import Permission, Role, RolePermission, UserRole

PW = "TestPassword123!"

#: A refresh's statements, transaction control aside, as they were when the
#: claims rode the compare-and-set (PostgreSQL, 2026-10-22): the token with its
#: user and claims, the compare-and-set, billing's and tenancy's claims, the
#: new refresh token. One more is a claims query that came back.
REFRESH_STATEMENTS = 5
_CONTROL = ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE")


def test_identity_has_exactly_the_one_contributor_the_cas_precomputes() -> None:
    """The refresh CAS stands in for ``contribute_identity_claims`` only. A
    second identity contributor would still run (``precomputed`` is per
    function), but the refresh would lose its saving: keep it at one, or fold
    the new one into ``identity_claims_columns`` too."""
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
    # Identity's claims came with the token; the contributor's query did not
    # run, and the refresh took no more statements than before.
    dialect = test_db.bind.dialect
    aggregate = str(select(aggregate_list(Role.code)).compile(dialect=dialect))
    aggregate = aggregate.split("FROM")[0].removeprefix("SELECT").split(" AS ")[0]
    with_claims = [s for s in statements if aggregate.strip() in s]
    assert len(with_claims) == 1, statements
    assert "refresh_tokens" in with_claims[0] and "users" in with_claims[0]
    assert not any(s.lstrip().startswith("SELECT (SELECT") for s in statements)
    work = [s for s in statements if not s.lstrip().upper().startswith(_CONTROL)]
    assert len(work) == REFRESH_STATEMENTS, work


@pytest.mark.asyncio
async def test_refresh_of_a_user_without_roles(
    async_client: AsyncClient, test_db: Any, make_user: Any
) -> None:
    """The aggregate always yields one row: no roles never drops the user."""
    await make_user("refresh-noroles@example.com", password=PW)

    claims, _ = await _refresh(async_client, test_db, "refresh-noroles@example.com")

    assert claims["roles"] == [] and claims["permissions"] == []
    assert "identity" in claims["claims_sources"]


@pytest.mark.asyncio
async def test_refresh_stamps_the_token_at_the_claims_read(
    async_client: AsyncClient,
    test_db: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The access token's revocation instant is the moment the claims were
    read, before the rotation: a role change committed after the read revokes
    it however long the refresh takes."""
    from datetime import UTC, datetime

    import sandloom_identity.services.token_service as token_service

    await make_user("refresh-stamp@example.com", password=PW)
    login = await async_client.post(
        "/auth/login", json={"email": "refresh-stamp@example.com", "password": PW}
    )
    assert login.status_code == 200, login.text
    seen: list[Any] = []
    mint = token_service.create_access_token

    def _mint(*, data: dict[str, Any], **kwargs: Any) -> str:
        seen.append(kwargs.get("claims_read_at"))
        return mint(data=data, **kwargs)

    monkeypatch.setattr(token_service, "create_access_token", _mint)
    before = datetime.now(UTC)
    resp = await async_client.post(
        "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert resp.status_code == 200, resp.text
    assert len(seen) == 1 and seen[0] is not None
    assert before <= seen[0] <= datetime.now(UTC)
    claims = decode_token(resp.json()["access_token"])
    assert claims["iat_us"] <= int(seen[0].timestamp() * 1_000_000) + 1
