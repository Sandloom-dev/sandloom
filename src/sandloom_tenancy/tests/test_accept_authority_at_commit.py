"""ADR-0003 G2, cross-service case: ``POST /invitations/accept`` re-checks the
caller's session immediately before its transaction commits.

Accepting an invitation is class A (it grants membership), and its authority
is NOT tenancy's own data: it is the access token's verified ``email`` claim,
minted by identity. No row lock in tenancy's database can hold that fact until
the commit, so the route re-reads the caller's revocation and block marks as a
``before_commit`` check (``sandloom.core.di.revocation_at_commit``). Every change
that withdraws the caller's authority (block, deactivation, password reset —
all ``require_immediate``) writes its mark BEFORE it commits: a mark written
while the request ran (before the final read) rolls the acceptance back
(401). Serializable, not linearizable: a mark written between that read and
COMMIT is not seen (ADR-0003 "Guarantees, precisely").

Real request UoW (``real_uow_client``): the rollback is observable.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom.sdk.security import block_user
from sandloom.sdk.testing import committing_uow, recording_uow
from sandloom_tenancy.models import OrganizationMember
from sandloom_tenancy.schemas import InvitationCreateIn, OrganizationCreateIn
from sandloom_tenancy.services import InvitationService, OrganizationService
from sandloom_tenancy.tests.conftest import email_of, make_user_headers

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _invitation(
    sessions: async_sessionmaker[AsyncSession], invitee: uuid.UUID
) -> tuple[uuid.UUID, str]:
    owner = uuid.uuid4()
    async with committing_uow(sessions) as db:
        org = await OrganizationService.create(
            OrganizationCreateIn(name="Commit Co", slug=f"cc-{uuid.uuid4().hex[:8]}"),
            owner_id=owner,
            db=db,
        )
        created = await InvitationService.create(
            org.id, InvitationCreateIn(email=email_of(invitee)), owner, db
        )
        return org.id, created.token


async def _members(
    sessions: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> set[uuid.UUID]:
    async with sessions() as db:
        rows = await db.execute(
            select(OrganizationMember.user_id).where(
                OrganizationMember.organization_id == org_id
            )
        )
        return set(rows.scalars())


def _block_during_accept(monkeypatch: pytest.MonkeyPatch, user_id: uuid.UUID) -> None:
    """Block ``user_id`` (a concurrent change) AFTER the route's strict check
    passed, while the acceptance is still uncommitted."""
    original = InvitationService.accept

    async def accept(*args: Any, **kwargs: Any) -> Any:
        member = await original(*args, **kwargs)
        with recording_uow():  # the blocking request's own (audited) UoW
            await block_user(user_id, reason="concurrent block", actor_id=None)
        return member

    monkeypatch.setattr(InvitationService, "accept", accept)


async def test_block_while_accepting_rolls_the_acceptance_back(
    real_uow_client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitee, headers = make_user_headers()
    org_id, token = await _invitation(sessions, invitee)
    _block_during_accept(monkeypatch, invitee)

    resp = await real_uow_client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=headers
    )

    assert resp.status_code == 401, resp.text
    assert invitee not in await _members(sessions, org_id)


async def test_unchanged_session_accepts_and_commits(
    real_uow_client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    invitee, headers = make_user_headers()
    org_id, token = await _invitation(sessions, invitee)

    resp = await real_uow_client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=headers
    )

    assert resp.status_code == 200, resp.text
    assert invitee in await _members(sessions, org_id)
