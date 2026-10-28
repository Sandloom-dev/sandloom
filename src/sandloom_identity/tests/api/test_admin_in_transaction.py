"""ADR-0003 gap G2: identity's class-A admin actions re-check the actor's
``admin`` role INSIDE the write transaction.

``require(Role("admin"))`` reads the access-token claim — fresh as of the
request start (``session_not_revoked`` + immediate revocation), but not at
commit: a demotion committing while an admin request is in flight did not stop
it. Every RBAC / user-administration mutation now also runs
``admin_in_transaction``: it takes the RBAC administration lock (a
transaction-scoped advisory lock every such mutation takes first, so they
serialise without deadlocks) and reads the actor's ``admin`` assignment
``FOR SHARE`` in the request's own Unit of Work.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom.sdk.security import create_access_token
from sandloom_identity.events import EventActor
from sandloom_identity.models.permission import Permission
from sandloom_identity.models.role import Role
from sandloom_identity.models.user import User
from sandloom_identity.models.user_role import UserRole
from sandloom_identity.services.role_service import RoleService

pytestmark = pytest.mark.asyncio


def _claim_only_admin_headers(user: User) -> dict[str, str]:
    """An access token CLAIMING ``admin`` for a user whose admin assignment
    is gone — the in-flight window of a demotion (the token predates no
    revocation mark, so every request-start check passes)."""
    token = create_access_token(
        data={
            "sub": str(user.id),
            "token_type": "access",
            "roles": ["admin"],
            "permissions": [],
            "iat": int(time.time()),
        }
    )
    return {"Authorization": f"Bearer {token}"}


async def test_admin_claim_without_the_role_cannot_administer(
    async_client: AsyncClient, test_db: AsyncSession, test_user: User, make_user
) -> None:
    target = await make_user(f"target-{uuid.uuid4().hex[:6]}@example.com")
    role = Role(name="Editor", code="editor", description="", is_system=False)
    perm = Permission(name="Read X", code="x.read", resource="x", action="read")
    test_db.add_all([role, perm])
    await test_db.commit()
    role_id, perm_id, target_id = role.id, perm.id, target.id
    headers = _claim_only_admin_headers(test_user)

    attempts = [
        ("POST", "/api/v1/roles", {"name": "Evil", "code": "evil"}),
        ("PUT", f"/api/v1/roles/{role_id}", {"name": "Renamed"}),
        ("DELETE", f"/api/v1/roles/{role_id}", None),
        (
            "POST",
            "/api/v1/permissions",
            {"name": "P", "code": "p.x", "resource": "p", "action": "x"},
        ),
        ("POST", f"/api/v1/roles/{role_id}/permissions/{perm_id}", None),
        ("DELETE", f"/api/v1/roles/{role_id}/permissions/{perm_id}", None),
        ("POST", f"/api/v1/users/{target_id}/roles/{role_id}", None),
        ("DELETE", f"/api/v1/users/{target_id}/roles/{role_id}", None),
        ("DELETE", f"/api/v1/users/{target_id}", None),
        ("PUT", f"/api/v1/users/{target_id}", {"is_active": False}),
    ]
    for method, path, body in attempts:
        resp = await async_client.request(method, path, json=body, headers=headers)
        assert resp.status_code == 403, (method, path, resp.status_code, resp.text)

    refreshed = await test_db.get(User, target_id, populate_existing=True)
    assert refreshed is not None and refreshed.is_active
    roles = await RoleService.list_user_roles(target_id, test_db)
    assert roles == []


async def test_real_admin_still_administers(
    async_client: AsyncClient,
    test_db: AsyncSession,
    admin_headers: dict[str, str],
    make_user,
) -> None:
    target = await make_user(f"t2-{uuid.uuid4().hex[:6]}@example.com")
    role = Role(name="Viewer", code="viewer", description="", is_system=False)
    test_db.add(role)
    await test_db.commit()
    resp = await async_client.post(
        f"/api/v1/users/{target.id}/roles/{role.id}", headers=admin_headers
    )
    assert resp.status_code == 204, resp.text
    resp = await async_client.put(
        f"/api/v1/users/{target.id}", json={"is_active": False}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False


async def test_self_update_needs_no_admin(
    async_client: AsyncClient, authenticated_headers: dict[str, str], test_user: User
) -> None:
    """A plain user still edits their own profile; ``is_active`` is ignored."""
    resp = await async_client.put(
        f"/api/v1/users/{test_user.id}",
        json={"full_name": "Renamed Me", "is_active": False},
        headers=authenticated_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["full_name"] == "Renamed Me"
    assert resp.json()["is_active"] is True


async def test_plain_self_update_with_is_active_takes_no_rbac_lock(
    async_client: AsyncClient,
    authenticated_headers: dict[str, str],
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-admin PATCHing themselves with ``is_active`` must not take the
    global RBAC administration lock (it would serialise every such request
    with all admin mutations): a cheap, lock-free read decides first."""
    locked: list[Any] = []
    original = RoleService.holds_admin_in_tx

    async def spy(actor_id: Any, db: Any) -> bool:
        locked.append(actor_id)
        return await original(actor_id, db)

    monkeypatch.setattr(RoleService, "holds_admin_in_tx", spy)
    resp = await async_client.put(
        f"/api/v1/users/{test_user.id}",
        json={"is_active": False},
        headers=authenticated_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is True
    assert locked == []


# --- Concurrency: real, separately committed transactions --------------------

_HOLD_S = 0.5


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _seed(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Admins A (the actor) and C, a plain user B, the admin and editor roles."""
    async with sessions() as db:
        admin_role = (
            await db.execute(select(Role).where(Role.code == "admin"))
        ).scalar_one_or_none()
        if admin_role is None:
            admin_role = Role(name="Admin", code="admin", is_system=True)
            db.add(admin_role)
        editor = Role(name=f"Ed {uuid.uuid4().hex[:6]}", code=uuid.uuid4().hex[:12])
        users = [
            User(
                email=f"{n}-{uuid.uuid4().hex[:6]}@example.com",
                full_name=n.upper(),
                is_verified=True,
            )
            for n in ("a", "b", "c")
        ]
        db.add_all([editor, *users])
        await db.flush()
        a, b, c = users
        db.add_all(
            [
                UserRole(user_id=a.id, role_id=admin_role.id),
                UserRole(user_id=c.id, role_id=admin_role.id),
            ]
        )
        await db.commit()
        return a.id, b.id, c.id, admin_role.id, editor.id


async def _committed(
    sessions: async_sessionmaker[AsyncSession],
    fn: Callable[[AsyncSession], Awaitable[Any]],
) -> Any:
    async with sessions() as db:
        try:
            result = await fn(db)
            await db.commit()
            return result
        except Exception as exc:  # noqa: BLE001 - returned for assertions
            await db.rollback()
            return exc


def _assign_as(
    actor: uuid.UUID, user: uuid.UUID, role: uuid.UUID
) -> Callable[[AsyncSession], Awaitable[None]]:
    """What the assign route does: the in-transaction admin check, then the write."""

    async def run(db: AsyncSession) -> None:
        await RoleService.require_admin_in_tx(actor, db)
        await RoleService.assign_role(user, role, db, actor=EventActor(user_id=actor))

    return run


async def test_demotion_committing_mid_request_refuses_the_admin_action(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    a, b, c, admin_role, editor = await _seed(sessions)
    async with sessions() as demote_db:
        await RoleService.require_admin_in_tx(c, demote_db)
        await RoleService.revoke_role(
            a, admin_role, demote_db, actor=EventActor(user_id=c)
        )
        racer = asyncio.create_task(_committed(sessions, _assign_as(a, b, editor)))
        await asyncio.sleep(_HOLD_S)
        await demote_db.commit()
    outcome = await asyncio.wait_for(racer, timeout=10)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403, outcome
    async with sessions() as db:
        assert await RoleService.list_user_roles(b, db) == []


async def test_deactivation_committing_mid_request_refuses_the_admin_action(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom_identity.services.user_service import UserService

    a, b, c, _, editor = await _seed(sessions)
    async with sessions() as demote_db:
        await RoleService.require_admin_in_tx(c, demote_db)
        await UserService.delete_user(a, demote_db, actor=EventActor(user_id=c))
        racer = asyncio.create_task(_committed(sessions, _assign_as(a, b, editor)))
        await asyncio.sleep(_HOLD_S)
        await demote_db.commit()
    outcome = await asyncio.wait_for(racer, timeout=10)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403, outcome


async def test_admin_action_in_flight_serialises_the_demotion(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The other order: the admin's action holds the lock, the demotion waits
    and applies after it (serialised, never interleaved)."""
    a, b, c, admin_role, editor = await _seed(sessions)
    async with sessions() as act_db:
        await _assign_as(a, b, editor)(act_db)

        async def demote(db: AsyncSession) -> None:
            await RoleService.require_admin_in_tx(c, db)
            await RoleService.revoke_role(
                a, admin_role, db, actor=EventActor(user_id=c)
            )

        demotion = asyncio.create_task(_committed(sessions, demote))
        await asyncio.sleep(_HOLD_S)
        assert not demotion.done(), "the demotion must wait for the admin's write"
        await act_db.commit()
    assert await asyncio.wait_for(demotion, timeout=10) is None
    async with sessions() as db:
        assert [r.id for r in await RoleService.list_user_roles(b, db)] == [editor]
        assert await RoleService.list_user_roles(a, db) == []


async def test_two_admins_demoting_each_other_do_not_deadlock(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A revokes C's admin while C revokes A's: the RBAC lock serialises them
    (no deadlock, no 500) — the first wins, the second is no longer an admin."""
    a, _, c, admin_role, _ = await _seed(sessions)

    def revoke(actor: uuid.UUID, user: uuid.UUID) -> Callable[..., Awaitable[None]]:
        async def run(db: AsyncSession) -> None:
            await RoleService.require_admin_in_tx(actor, db)
            await RoleService.revoke_role(
                user, admin_role, db, actor=EventActor(user_id=actor)
            )

        return run

    results = await asyncio.wait_for(
        asyncio.gather(
            _committed(sessions, revoke(a, c)), _committed(sessions, revoke(c, a))
        ),
        timeout=10,
    )
    assert sorted(r is None for r in results) == [False, True], results
    loser = next(r for r in results if r is not None)
    assert isinstance(loser, HTTPException) and loser.status_code == 403
