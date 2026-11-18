"""Joining an organization makes the joiner's access token stale (client: 401, refresh).

A token minted before a join lists the organizations the user belonged to then
(``tenant_ids``): selecting the new one answers ``403 tenant_not_authorized``
until the client refreshes. Every path that adds a member revokes the added
user's tokens on commit, so the client meets the 401 it already refreshes on.
The revocation is NOT immediate (a removal's is): a token missing an
organization is a usability problem, not a security one, so an unreachable
revocation store never refuses the creation or the join with 503.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom.sdk.testing import committing_uow
from sandloom_tenancy.models import ROLE_MEMBER
from sandloom_tenancy.schemas import OrganizationCreateIn
from sandloom_tenancy.services import MembershipService, OrganizationService
from sandloom_tenancy.tests.conftest import email_of


async def _invite_token(
    client: AsyncClient, org_id: str, owner_headers: dict, user_id: uuid.UUID
) -> str:
    invite = await client.post(
        f"/api/v1/orgs/{org_id}/invitations",
        json={"email": email_of(user_id), "role": ROLE_MEMBER},
        headers=owner_headers,
    )
    assert invite.status_code == 201, invite.text
    return str(invite.json()["token"])


async def test_creating_an_organization_revokes_the_creators_tokens_once(
    async_client: AsyncClient, user_a: Any, revoked_users: list[Any]
) -> None:
    owner_id, headers = user_a
    resp = await async_client.post(
        "/api/v1/orgs", json={"name": "Fresh Co", "slug": "fresh-co"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    # Post-commit only (not immediate): one mark, the creator's, nobody else.
    assert revoked_users == [owner_id]


async def test_a_refused_creation_revokes_nobody(
    async_client: AsyncClient, user_b: Any, org_with_owner: Any, revoked_users: list[Any]
) -> None:
    _, headers = user_b
    revoked_users.clear()  # the fixture's own creation revoked its owner
    resp = await async_client.post(
        "/api/v1/orgs",
        json={"name": "Taken", "slug": org_with_owner["slug"]},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert revoked_users == []


async def test_accepting_an_invitation_revokes_the_acceptors_tokens(
    async_client: AsyncClient,
    user_a: Any,
    user_b: Any,
    org_with_owner: Any,
    revoked_users: list[Any],
) -> None:
    _, headers_a = user_a
    user_b_id, headers_b = user_b
    token = await _invite_token(async_client, org_with_owner["id"], headers_a, user_b_id)
    revoked_users.clear()

    accept = await async_client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=headers_b
    )
    assert accept.status_code == 200, accept.text
    assert revoked_users == [user_b_id]  # the acceptor, once; not the inviter


async def test_a_refused_acceptance_revokes_nobody(
    async_client: AsyncClient,
    user_a: Any,
    user_b: Any,
    org_with_owner: Any,
    revoked_users: list[Any],
) -> None:
    _, headers_a = user_a
    user_b_id, headers_b = user_b
    token = await _invite_token(async_client, org_with_owner["id"], headers_a, user_b_id)
    first = await async_client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=headers_b
    )
    assert first.status_code == 200, first.text
    revoked_users.clear()

    again = await async_client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=headers_b
    )
    assert again.status_code == 404, again.text  # single use
    assert revoked_users == []


async def test_an_unreachable_revocation_store_never_refuses_a_join(
    async_client: AsyncClient,
    user_a: Any,
    user_b: Any,
    org_with_owner: Any,
    revoked_users: Any,
) -> None:
    """Not ``require_immediate``: with the mark unwritable, creating an
    organization and accepting an invitation still succeed (a removal would
    answer 503, see ``test_role_change_is_refused_when_the_mark_cannot_be_written``)."""
    _, headers_a = user_a
    user_b_id, headers_b = user_b
    token = await _invite_token(async_client, org_with_owner["id"], headers_a, user_b_id)
    revoked_users.available = False

    created = await async_client.post(
        "/api/v1/orgs", json={"name": "Outage Co", "slug": "outage-co"}, headers=headers_a
    )
    accepted = await async_client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=headers_b
    )

    assert created.status_code == 201, created.text
    assert accepted.status_code == 200, accepted.text


async def test_adding_a_member_revokes_that_members_tokens_only(
    test_engine: AsyncEngine, revoked_users: list[Any]
) -> None:
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    async with committing_uow(sessions) as db:
        org = await OrganizationService.create(
            OrganizationCreateIn(name="Add Co", slug=f"add-{uuid.uuid4().hex[:8]}"),
            owner_id=owner_id,
            db=db,
        )
    revoked_users.clear()

    async with committing_uow(sessions) as db:
        await MembershipService.add_member(
            org.id, member_id, ROLE_MEMBER, db, actor_id=owner_id
        )
    assert revoked_users == [member_id]


async def test_a_rolled_back_add_revokes_nobody(
    test_engine: AsyncEngine, revoked_users: list[Any]
) -> None:
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    async with committing_uow(sessions) as db:
        org = await OrganizationService.create(
            OrganizationCreateIn(name="Undo Co", slug=f"undo-{uuid.uuid4().hex[:8]}"),
            owner_id=owner_id,
            db=db,
        )
    revoked_users.clear()

    class _Abort(Exception):
        pass

    session: AsyncSession
    with pytest.raises(_Abort):
        async with committing_uow(sessions) as session:
            await MembershipService.add_member(
                org.id, member_id, ROLE_MEMBER, session, actor_id=owner_id
            )
            raise _Abort
    assert revoked_users == []
