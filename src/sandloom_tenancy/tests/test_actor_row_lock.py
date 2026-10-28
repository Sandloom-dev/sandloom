"""ADR-0003 gap G3: the ACTING member's row is locked in the write transaction.

A class-A tenancy write (role change, removal, organization update,
invitation create/revoke) authorizes on the actor's own membership row. That
read must hold until the write commits: the actor row is read ``FOR SHARE`` in
the same transaction, so a concurrent demotion / removal of the actor either
commits FIRST (the action then sees the new role and is refused) or waits for
the action to commit (serialised) — never "checked as admin, demoted, acted
anyway". Rows are locked in ``user_id`` order, so two actors acting on each
other cannot deadlock.

Real, separately committed transactions on the test database.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom_tenancy.exceptions import TenancyForbiddenError, TenancyNotFoundError
from sandloom_tenancy.models import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    OrganizationInvitation,
    OrganizationMember,
)
from sandloom_tenancy.schemas import (
    InvitationCreateIn,
    OrganizationCreateIn,
    OrganizationUpdateIn,
)
from sandloom_tenancy.services import (
    InvitationService,
    MembershipService,
    OrganizationService,
)

pytestmark = pytest.mark.asyncio

#: How long the racing action gets to run while the demotion is still open.
#: Without the actor lock it completes well within this; with it, it blocks.
_HOLD_S = 0.5


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _org(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """An org with owner, admin (the actor) and a plain member (the target)."""
    owner, admin, member = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sessions() as db:
        org = await OrganizationService.create(
            OrganizationCreateIn(name="Lock Co", slug=f"lock-{uuid.uuid4().hex[:8]}"),
            owner_id=owner,
            db=db,
        )
        await MembershipService.add_member(org.id, admin, ROLE_ADMIN, db)
        await MembershipService.add_member(org.id, member, ROLE_MEMBER, db)
        await db.commit()
        return org.id, owner, admin, member


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


async def _race_with_open_demotion(
    sessions: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    owner: uuid.UUID,
    actor: uuid.UUID,
    action: Callable[[AsyncSession], Awaitable[Any]],
    *,
    remove: bool = False,
) -> Any:
    """Demote (or remove) ``actor`` in an OPEN transaction, run ``action`` by
    the actor concurrently, then commit the demotion; return the action's
    outcome."""
    async with sessions() as demote_db:
        if remove:
            await MembershipService.remove_member(
                org_id, actor, demote_db, actor_id=owner
            )
        else:
            await MembershipService.change_role(
                org_id, actor, ROLE_MEMBER, demote_db, actor_id=owner
            )
        racer = asyncio.create_task(_committed(sessions, action))
        await asyncio.sleep(_HOLD_S)  # the action reaches its authority check
        await demote_db.commit()
    return await asyncio.wait_for(racer, timeout=10)


async def test_demoted_actor_cannot_remove_a_member(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    org_id, owner, admin, member = await _org(sessions)
    outcome = await _race_with_open_demotion(
        sessions,
        org_id,
        owner,
        admin,
        lambda db: MembershipService.remove_member(org_id, member, db, actor_id=admin),
    )
    assert isinstance(outcome, TenancyForbiddenError), outcome
    async with sessions() as db:
        assert await MembershipService.get_role(org_id, member, db) == ROLE_MEMBER


async def test_removed_actor_cannot_change_a_role(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    org_id, owner, admin, member = await _org(sessions)
    outcome = await _race_with_open_demotion(
        sessions,
        org_id,
        owner,
        admin,
        lambda db: MembershipService.change_role(
            org_id, member, ROLE_ADMIN, db, actor_id=admin
        ),
        remove=True,
    )
    assert isinstance(outcome, TenancyNotFoundError), outcome
    async with sessions() as db:
        assert await MembershipService.get_role(org_id, member, db) == ROLE_MEMBER


async def test_demoted_actor_cannot_update_the_organization(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    org_id, owner, admin, _ = await _org(sessions)
    outcome = await _race_with_open_demotion(
        sessions,
        org_id,
        owner,
        admin,
        lambda db: OrganizationService.update(
            org_id, OrganizationUpdateIn(name="Renamed"), db, actor_id=admin
        ),
    )
    assert isinstance(outcome, TenancyForbiddenError), outcome
    async with sessions() as db:
        assert (await OrganizationService.get_or_404(org_id, db)).name == "Lock Co"


async def test_demoted_actor_cannot_invite(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    org_id, owner, admin, _ = await _org(sessions)
    outcome = await _race_with_open_demotion(
        sessions,
        org_id,
        owner,
        admin,
        lambda db: InvitationService.create(
            org_id, InvitationCreateIn(email="late@example.com"), admin, db
        ),
    )
    assert isinstance(outcome, TenancyForbiddenError), outcome
    async with sessions() as db:
        pending = await InvitationService.list_pending(org_id, db)
    assert pending == []


async def test_demoted_actor_cannot_revoke_an_invitation(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    org_id, owner, admin, _ = await _org(sessions)
    async with sessions() as db:
        created = await InvitationService.create(
            org_id, InvitationCreateIn(email="keep@example.com"), owner, db
        )
        await db.commit()
    outcome = await _race_with_open_demotion(
        sessions,
        org_id,
        owner,
        admin,
        lambda db: InvitationService.revoke(
            org_id, created.invitation.id, db, actor_id=admin
        ),
    )
    assert isinstance(outcome, TenancyForbiddenError), outcome
    async with sessions() as db:
        row = await db.get(OrganizationInvitation, created.invitation.id)
    assert row is not None and row.revoked_at is None


async def test_action_committed_first_serialises_the_demotion(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    """The other order: the actor's action holds the actor row, so the
    demotion waits for it — the action commits as an admin action, then the
    demotion applies (serialised, never interleaved)."""
    org_id, owner, admin, member = await _org(sessions)
    async with sessions() as act_db:
        await MembershipService.remove_member(org_id, member, act_db, actor_id=admin)
        demotion = asyncio.create_task(
            _committed(
                sessions,
                lambda db: MembershipService.change_role(
                    org_id, admin, ROLE_MEMBER, db, actor_id=owner
                ),
            )
        )
        await asyncio.sleep(_HOLD_S)
        assert not demotion.done(), "the demotion must wait for the actor's write"
        await act_db.commit()
    result = await asyncio.wait_for(demotion, timeout=10)
    assert isinstance(result, OrganizationMember), result
    async with sessions() as db:
        assert await MembershipService.get_role(org_id, member, db) is None
        assert await MembershipService.get_role(org_id, admin, db) == ROLE_MEMBER


async def test_two_admins_acting_on_each_other_do_not_deadlock(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    """Admin X removes admin Y while Y removes X: both lock the pair in the
    same (user_id) order, so neither deadlocks — both are refused (only the
    owner may remove an admin)."""
    org_id, _owner, x, _ = await _org(sessions)
    y = uuid.uuid4()
    async with sessions() as db:
        await MembershipService.add_member(org_id, y, ROLE_ADMIN, db)
        await db.commit()
    results = await asyncio.wait_for(
        asyncio.gather(
            _committed(
                sessions,
                lambda db: MembershipService.remove_member(org_id, y, db, actor_id=x),
            ),
            _committed(
                sessions,
                lambda db: MembershipService.remove_member(org_id, x, db, actor_id=y),
            ),
        ),
        timeout=10,
    )
    assert all(isinstance(r, TenancyForbiddenError) for r in results), results
    async with sessions() as db:
        roles = (
            await db.execute(
                select(OrganizationMember.user_id).where(
                    OrganizationMember.organization_id == org_id,
                    OrganizationMember.role == ROLE_ADMIN,
                )
            )
        ).scalars()
        assert set(roles) == {x, y}
