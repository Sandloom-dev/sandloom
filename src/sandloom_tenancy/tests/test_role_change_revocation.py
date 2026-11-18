"""Role changes revoke the member's tokens (owner decision, PR #46).

Roles are read live today (``tenancy.membership.v1``), so a stale token does
not grant a stale role yet — but a token minted under the old role must not
outlive the change once roles are cached or carried as claims, and the stale
session should end. Every role-altering path — ``change_role`` and ownership
transfer (both parties) — revokes IMMEDIATELY (``require_immediate``: the mark
is written before commit, then re-marked after commit) or refuses the change
with ``503``. UX: the affected user re-authenticates (refresh or log in).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom.sdk.testing import bind_contract_session, committing_uow
from sandloom_tenancy import events
from sandloom_tenancy.contracts import MembershipReadContract
from sandloom_tenancy.models import ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER
from sandloom_tenancy.schemas import OrganizationCreateIn
from sandloom_tenancy.schemas.membership import MemberState
from sandloom_tenancy.services import MembershipService, OrganizationService
from sandloom_tenancy.tests.conftest import email_of


async def _join(
    client: AsyncClient, org_id: str, owner_headers: dict, user: Any, role: str
) -> None:
    user_id, headers = user
    invite = await client.post(
        f"/api/v1/orgs/{org_id}/invitations",
        json={"email": email_of(user_id), "role": role},
        headers=owner_headers,
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        "/api/v1/invitations/accept",
        json={"token": invite.json()["token"]},
        headers=headers,
    )
    assert accept.status_code == 200, accept.text


@pytest.mark.asyncio
async def test_role_change_revokes_the_members_tokens(
    async_client: AsyncClient, user_a, user_b, org_with_owner, revoked_users
) -> None:
    _, headers_a = user_a
    user_b_id, _ = user_b
    org_id = org_with_owner["id"]
    await _join(async_client, org_id, headers_a, user_b, ROLE_MEMBER)
    assert revoked_users == []

    resp = await async_client.patch(
        f"/api/v1/orgs/{org_id}/members/{user_b_id}",
        json={"role": ROLE_ADMIN},
        headers=headers_a,
    )
    assert resp.status_code == 200, resp.text
    # Immediate (before commit) + the post-commit re-mark; nobody else.
    assert revoked_users == [user_b_id, user_b_id]


@pytest.mark.asyncio
async def test_no_op_role_change_revokes_nobody(
    async_client: AsyncClient, user_a, user_b, org_with_owner, revoked_users
) -> None:
    _, headers_a = user_a
    user_b_id, _ = user_b
    org_id = org_with_owner["id"]
    await _join(async_client, org_id, headers_a, user_b, ROLE_MEMBER)
    resp = await async_client.patch(
        f"/api/v1/orgs/{org_id}/members/{user_b_id}",
        json={"role": ROLE_MEMBER},
        headers=headers_a,
    )
    assert resp.status_code == 200, resp.text
    assert revoked_users == []


@pytest.mark.asyncio
async def test_ownership_transfer_revokes_both_parties(
    async_client: AsyncClient, user_a, user_b, org_with_owner, revoked_users
) -> None:
    owner_id, headers_a = user_a
    user_b_id, _ = user_b
    org_id = org_with_owner["id"]
    await _join(async_client, org_id, headers_a, user_b, ROLE_ADMIN)

    resp = await async_client.post(
        f"/api/v1/orgs/{org_id}/transfer-ownership/{user_b_id}", headers=headers_a
    )
    assert resp.status_code == 200, resp.text
    # The new owner AND the demoted previous owner (who made the request:
    # they re-authenticate and continue as admin).
    assert sorted(map(str, revoked_users)) == sorted(
        [str(user_b_id), str(owner_id)] * 2
    )


@pytest.mark.asyncio
async def test_role_change_is_refused_when_the_mark_cannot_be_written(
    async_client: AsyncClient,
    user_a,
    user_b,
    org_with_owner,
    captured,
    revoked_users,
) -> None:
    _, headers_a = user_a
    user_b_id, _ = user_b
    org_id = org_with_owner["id"]
    await _join(async_client, org_id, headers_a, user_b, ROLE_MEMBER)
    captured["events"].clear()

    revoked_users.available = False  # cache unreachable: no mark written
    resp = await async_client.patch(
        f"/api/v1/orgs/{org_id}/members/{user_b_id}",
        json={"role": ROLE_ADMIN},
        headers=headers_a,
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "Authentication store temporarily unavailable"


@pytest.mark.asyncio
async def test_refused_role_change_rolls_back(
    test_engine: AsyncEngine, revoked_users
) -> None:
    """In a real Unit of Work the refusal rolls the role change back: no
    committed change is ever left without its revocation mark."""
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    async with committing_uow(sessions) as db:
        org = await OrganizationService.create(
            OrganizationCreateIn(name="Refuse Co", slug=f"r-{uuid.uuid4().hex[:8]}"),
            owner_id=owner_id,
            db=db,
        )
        org_id = org.id
        await MembershipService.add_member(org_id, member_id, ROLE_MEMBER, db)

    revoked_users.available = False
    with pytest.raises(RuntimeError) as refused:
        async with committing_uow(sessions) as db:
            await MembershipService.change_role(
                org_id, member_id, ROLE_ADMIN, db, actor_id=owner_id
            )
    assert type(refused.value).__name__ == "RevocationUnavailableError"
    async with sessions() as db:
        assert await MembershipService.get_role(org_id, member_id, db) == ROLE_MEMBER


@pytest.mark.asyncio
async def test_member_state_contract_reads_role_and_version_together(
    test_db: AsyncSession, captured
) -> None:
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    org = await OrganizationService.create(
        OrganizationCreateIn(name="State Co", slug=f"s-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=test_db,
    )
    await MembershipService.add_member(org.id, member_id, ROLE_MEMBER, test_db)
    await MembershipService.change_role(org.id, member_id, ROLE_ADMIN, test_db)
    published = [
        p["version"]
        for c, p in captured["events"]
        if c in (events.MEMBER_ADDED_V1, events.MEMBER_ROLE_CHANGED_V1)
        and p["user_id"] == str(member_id)
    ]
    c = MembershipReadContract
    with bind_contract_session(test_db):
        state = await c.get_member_state(org.id, member_id)
        assert state == MemberState(
            organization_id=org.id,
            user_id=member_id,
            role=ROLE_ADMIN,
            version=published[-1],
        )
        owner = await c.get_member_state(org.id, owner_id)
        assert owner.role == ROLE_OWNER and owner.version >= 1
        stranger = await c.get_member_state(org.id, uuid.uuid4())
        assert stranger.role is None and stranger.version == 0

        await MembershipService.remove_member(org.id, member_id, test_db)
        removed = await c.get_member_state(org.id, member_id)
        assert removed.role is None
        assert removed.version == published[-1] + 1  # survives the removal
