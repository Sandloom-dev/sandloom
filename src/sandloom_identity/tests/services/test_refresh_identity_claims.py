"""Refresh reads identity's roles / permissions after the issuance check.

The refresh loads the presented token with its user, runs the issuance check
(``ensure_user_not_blocked``) and the compare-and-set consume, and only then
collects the claims, identity's included: the access token is stamped at
that read (``claims_read_at``), so a role or membership change committed
after it is revoked by its mark, whatever the clocks of issuer and revoker
(within the skew). The token must carry exactly what the contributor
returns, with ``identity`` in ``claims_sources``.
"""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from sandloom.sdk.contracts import contract_method
from sandloom.sdk.security import decode_token
from sandloom.sdk.testing import bind_contract_session
from sandloom_identity.claims import contribute_identity_claims
from sandloom_identity.models import Permission, Role, RolePermission, UserRole

PW = "TestPassword123!"

#: A refresh's statements, transaction control aside: the token with its
#: user, the compare-and-set, identity's, billing's and tenancy's claims, the
#: new refresh token. One more is a query that came back.
REFRESH_STATEMENTS = 6
_CONTROL = ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE")


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
async def test_refresh_claims_equal_the_contributor_read_after_the_consume(
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
    # The claims were read after the consume: the compare-and-set comes
    # first, then the claims queries.
    work = [s for s in statements if not s.lstrip().upper().startswith(_CONTROL)]
    assert len(work) == REFRESH_STATEMENTS, work
    consume = next(
        i for i, s in enumerate(work) if s.lstrip().upper().startswith("UPDATE")
    )
    roles = next(i for i, s in enumerate(work) if "user_roles" in s)
    assert consume < roles, work


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


async def _refresh_then_me(
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    email: str,
    *,
    revoke_before: float | None = None,
    revoke_after_read: float | None = None,
) -> int:
    """Log in, refresh, call ``/auth/me`` with the new access token.

    ``revoke_before``: another request revokes the user before the refresh,
    with a mark that many seconds from now. ``revoke_after_read``: a change
    commits after the refresh read its claims, and its mark is that many
    seconds after the read (a revoker clock behind the issuer's, within the
    skew, makes it look close to the read)."""
    import asyncio
    from datetime import UTC, datetime

    import sandloom_identity.services.token_service as token_service
    from sandloom.sdk.security import revoke_user_tokens

    login = await async_client.post(
        "/auth/login", json={"email": email, "password": PW}
    )
    assert login.status_code == 200, login.text
    sub = decode_token(login.json()["access_token"])["sub"]
    if revoke_before is not None:
        mark = datetime.now(UTC).timestamp() + revoke_before
        await asyncio.create_task(revoke_user_tokens(sub, valid_after=mark))
    reads: list[Any] = []
    mint = token_service.create_access_token

    def _mint(*, data: dict[str, Any], **kwargs: Any) -> str:
        reads.append(kwargs.get("claims_read_at"))
        return mint(data=data, **kwargs)

    monkeypatch.setattr(token_service, "create_access_token", _mint)
    resp = await async_client.post(
        "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert resp.status_code == 200, resp.text
    if revoke_after_read is not None:
        mark = reads[0].timestamp() + revoke_after_read
        await asyncio.create_task(revoke_user_tokens(sub, valid_after=mark))
    me = await async_client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
    )
    return me.status_code


@pytest.mark.asyncio
async def test_a_change_after_the_claims_read_revokes_the_refreshed_token(
    async_client: AsyncClient, make_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role change committed after the refresh read its claims, its mark
    stamped by a clock behind the issuer's (within the skew): the token the
    refresh minted is refused."""
    await make_user("refresh-stale@example.com", password=PW)
    status = await _refresh_then_me(
        async_client, monkeypatch, "refresh-stale@example.com", revoke_after_read=0.5
    )
    assert status == 401


@pytest.mark.asyncio
async def test_a_change_before_the_refresh_leaves_its_fresh_token_valid(
    async_client: AsyncClient, make_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revocation committed before the refresh (its mark still in the
    future): the refresh sees it first, reads fresh claims and its token is
    accepted."""
    await make_user("refresh-fresh@example.com", password=PW)
    status = await _refresh_then_me(
        async_client, monkeypatch, "refresh-fresh@example.com", revoke_before=1.5
    )
    assert status == 200


@pytest.mark.asyncio
async def test_a_mark_the_issuer_sees_after_the_read_never_lifts_the_token(
    async_client: AsyncClient, make_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the claims' read and the mint, a role change commits on a
    revoker whose clock runs 1.5 s behind the issuer's (within the 2 s skew),
    and the issuer itself reads that mark (the issuance check, run again):
    the mark looks earlier than the read + skew, yet it must not lift the
    stale token past itself."""
    import asyncio
    import time

    import sandloom_identity.services.token_service as token_service
    from sandloom.sdk.security import ensure_user_not_blocked, revoke_user_tokens

    await make_user("refresh-seen-after@example.com", password=PW)
    login = await async_client.post(
        "/auth/login",
        json={"email": "refresh-seen-after@example.com", "password": PW},
    )
    assert login.status_code == 200, login.text
    sub = decode_token(login.json()["access_token"])["sub"]

    async def _revoke_on_a_slow_clock() -> None:
        with monkeypatch.context() as patch:
            behind = time.time() - 1.5
            patch.setattr(time, "time", lambda: behind)
            assert await revoke_user_tokens(sub) is True

    class _Core:
        """The refresh's core, with a change landing after its claims read."""

        def __init__(self, real: Any) -> None:
            self._real = real

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

        async def collect_claims(self, *args: Any, **kwargs: Any) -> Any:
            claims = await self._real.collect_claims(*args, **kwargs)
            await asyncio.create_task(_revoke_on_a_slow_clock())
            await ensure_user_not_blocked(sub)  # the issuer sees the mark now
            return claims

    create = token_service.TokenService.create_tokens.__func__

    async def _create_tokens(
        cls: Any, user: Any, db: Any, core: Any = None, **kw: Any
    ) -> Any:
        return await create(cls, user, db, _Core(core) if core else core, **kw)

    monkeypatch.setattr(
        token_service.TokenService, "create_tokens", classmethod(_create_tokens)
    )
    resp = await async_client.post(
        "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert resp.status_code == 200, resp.text
    me = await async_client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
    )
    assert me.status_code == 401
