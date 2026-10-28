"""Regression tests for the tenancy security review findings.

Covers: admin cannot deactivate via PATCH, invitation tokens hashed at rest and
bound to the invited e-mail, single-use acceptance under concurrency, ownership
transfer serialization, inactive organizations, PII-free events, audit records,
duplicate-invitation races and the invitation e-mail.
"""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom.sdk.testing import committing_uow
from sandloom_tenancy import events
from sandloom_tenancy.exceptions import (
    TenancyConflictError,
    TenancyForbiddenError,
    TenancyNotFoundError,
)
from sandloom_tenancy.models import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    OrganizationInvitation,
    OrganizationMember,
)
from sandloom_tenancy.schemas import InvitationCreateIn, OrganizationCreateIn
from sandloom_tenancy.services import (
    InvitationService,
    MembershipService,
    OrganizationService,
)
from sandloom_tenancy.tests.conftest import email_of, make_user_headers


async def _invite(
    client: AsyncClient, org_id: str, headers: dict, email: str, role: str = "member"
) -> dict[str, Any]:
    resp = await client.post(
        f"/api/v1/orgs/{org_id}/invitations",
        json={"email": email, "role": role},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _accept(client: AsyncClient, token: str, headers: dict) -> Any:
    return await client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=headers
    )


# --- Finding 1: admin cannot deactivate via PATCH ----------------------------


@pytest.mark.asyncio
async def test_admin_patch_cannot_deactivate_org(
    async_client: AsyncClient, user_a, user_b, org_with_owner
) -> None:
    _, headers_a = user_a
    user_b_id, headers_b = user_b
    org_id = org_with_owner["id"]
    inv = await _invite(async_client, org_id, headers_a, email_of(user_b_id), "admin")
    assert (await _accept(async_client, inv["token"], headers_b)).status_code == 200

    resp = await async_client.patch(
        f"/api/v1/orgs/{org_id}", json={"is_active": False}, headers=headers_b
    )
    assert resp.status_code == 422  # field no longer accepted
    resp = await async_client.patch(
        f"/api/v1/orgs/{org_id}",
        json={"name": "Still here", "is_active": False},
        headers=headers_a,
    )
    assert resp.status_code == 422  # not even for the owner: use DELETE

    org = await async_client.get(f"/api/v1/orgs/{org_id}", headers=headers_a)
    assert org.status_code == 200
    assert org.json()["is_active"] is True

    # admin cannot DELETE either
    resp = await async_client.delete(f"/api/v1/orgs/{org_id}", headers=headers_b)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_owner_delete_publishes_deleted_event_and_audits(
    async_client: AsyncClient, user_a, org_with_owner, captured
) -> None:
    owner_id, headers_a = user_a
    org_id = org_with_owner["id"]
    resp = await async_client.delete(f"/api/v1/orgs/{org_id}", headers=headers_a)
    assert resp.status_code == 204
    assert (
        events.ORG_DELETED_V1,
        {"organization_id": org_id, "actor_id": str(owner_id)},
    ) in captured["events"]
    actions = [a for a, _ in captured["audit"]]
    assert actions == ["tenancy.organization.deleted"]
    assert captured["audit"][0][1]["actor_id"] == owner_id


# --- Finding 3: tokens hashed at rest, e-mail bound --------------------------


@pytest.mark.asyncio
async def test_token_is_stored_only_as_sha256(
    async_client: AsyncClient, user_a, org_with_owner, test_db: AsyncSession
) -> None:
    _, headers_a = user_a
    inv = await _invite(async_client, org_with_owner["id"], headers_a, "h@example.com")
    row = (
        await test_db.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.id == uuid.UUID(inv["id"])
            )
        )
    ).scalar_one()
    assert row.token_hash == hashlib.sha256(inv["token"].encode()).hexdigest()
    assert not hasattr(row, "token")
    cols = {c.name for c in OrganizationInvitation.__table__.columns}
    assert "token" not in cols


@pytest.mark.asyncio
async def test_accept_by_different_email_is_forbidden_and_not_consumed(
    async_client: AsyncClient, user_a, user_b, user_c, org_with_owner
) -> None:
    _, headers_a = user_a
    user_b_id, headers_b = user_b
    _, headers_c = user_c
    org_id = org_with_owner["id"]
    inv = await _invite(async_client, org_id, headers_a, email_of(user_b_id), "admin")

    stolen = await _accept(async_client, inv["token"], headers_c)
    assert stolen.status_code == 403
    # the intended invitee can still use it
    ok = await _accept(async_client, inv["token"], headers_b)
    assert ok.status_code == 200
    assert ok.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_accept_requires_email_claim(
    async_client: AsyncClient, user_a, org_with_owner
) -> None:
    _, headers_a = user_a
    uid, no_email_headers = make_user_headers(email=None)
    inv = await _invite(async_client, org_with_owner["id"], headers_a, email_of(uid))
    resp = await _accept(async_client, inv["token"], no_email_headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_accept_rejects_unverified_email_claim(
    async_client: AsyncClient, user_a, org_with_owner
) -> None:
    _, headers_a = user_a
    uid, headers = make_user_headers(extra_claims={"email_verified": False})
    inv = await _invite(async_client, org_with_owner["id"], headers_a, email_of(uid))
    assert (await _accept(async_client, inv["token"], headers)).status_code == 403
    _, verified = make_user_headers(uid, extra_claims={"email_verified": True})
    assert (await _accept(async_client, inv["token"], verified)).status_code == 200


@pytest.mark.asyncio
async def test_accept_requires_email_verified_claim_present(
    async_client: AsyncClient, user_a, org_with_owner
) -> None:
    """A token WITHOUT ``email_verified`` (legacy / foreign issuer) cannot claim
    an invitation: verification must be asserted (``True``), not merely absent
    a ``False``."""
    _, headers_a = user_a
    uid, legacy = make_user_headers(email_verified=None)
    inv = await _invite(async_client, org_with_owner["id"], headers_a, email_of(uid))
    resp = await _accept(async_client, inv["token"], legacy)
    assert resp.status_code == 403
    assert "Verify" in resp.json()["detail"]
    _, verified = make_user_headers(uid)
    assert (await _accept(async_client, inv["token"], verified)).status_code == 200


@pytest.mark.asyncio
async def test_expired_invitation_cannot_be_accepted(
    async_client: AsyncClient, user_a, user_b, org_with_owner, test_db: AsyncSession
) -> None:
    _, headers_a = user_a
    user_b_id, headers_b = user_b
    inv = await _invite(
        async_client, org_with_owner["id"], headers_a, email_of(user_b_id)
    )
    await test_db.execute(
        update(OrganizationInvitation)
        .where(OrganizationInvitation.id == uuid.UUID(inv["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    assert (await _accept(async_client, inv["token"], headers_b)).status_code == 404

    # expired invitations are not listed and do not block a fresh invitation
    listing = await async_client.get(
        f"/api/v1/orgs/{org_with_owner['id']}/invitations", headers=headers_a
    )
    assert inv["id"] not in [i["id"] for i in listing.json()]
    again = await _invite(
        async_client, org_with_owner["id"], headers_a, email_of(user_b_id)
    )
    assert (await _accept(async_client, again["token"], headers_b)).status_code == 200


@pytest.mark.asyncio
async def test_already_member_is_409_and_invitation_stays_open(
    async_client: AsyncClient, user_a, user_b, org_with_owner, test_db: AsyncSession
) -> None:
    _, headers_a = user_a
    user_b_id, headers_b = user_b
    org_id = org_with_owner["id"]
    first = await _invite(async_client, org_id, headers_a, email_of(user_b_id))
    assert (await _accept(async_client, first["token"], headers_b)).status_code == 200

    # a second invitation for the same address is allowed once the first closed
    second = await _invite(async_client, org_id, headers_a, email_of(user_b_id))
    resp = await _accept(async_client, second["token"], headers_b)
    assert resp.status_code == 409
    row = (
        await test_db.execute(
            select(OrganizationInvitation.accepted_at).where(
                OrganizationInvitation.id == uuid.UUID(second["id"])
            )
        )
    ).scalar_one()
    assert row is None  # the failed attempt consumed nothing


@pytest.mark.asyncio
async def test_duplicate_pending_invitation_is_case_insensitive(
    async_client: AsyncClient, user_a, org_with_owner
) -> None:
    _, headers_a = user_a
    org_id = org_with_owner["id"]
    await _invite(async_client, org_id, headers_a, "Case@Example.com")
    resp = await async_client.post(
        f"/api/v1/orgs/{org_id}/invitations",
        json={"email": "case@example.com"},
        headers=headers_a,
    )
    assert resp.status_code == 409


# --- Finding 7: inactive organization is not operational ---------------------


@pytest.mark.asyncio
async def test_operations_on_deleted_org(
    async_client: AsyncClient, user_a, user_b, org_with_owner
) -> None:
    _owner_id, headers_a = user_a
    user_b_id, headers_b = user_b
    org_id = org_with_owner["id"]
    inv = await _invite(async_client, org_id, headers_a, email_of(user_b_id))
    assert (
        await async_client.delete(f"/api/v1/orgs/{org_id}", headers=headers_a)
    ).status_code == 204

    # pending invitation into a deleted org cannot be accepted
    assert (await _accept(async_client, inv["token"], headers_b)).status_code == 404
    # the guard no longer resolves a role in the deleted org
    for method, path, body in (
        ("GET", f"/api/v1/orgs/{org_id}/members", None),
        ("PATCH", f"/api/v1/orgs/{org_id}", {"name": "zombie"}),
        ("POST", f"/api/v1/orgs/{org_id}/invitations", {"email": "z@example.com"}),
        ("POST", f"/api/v1/orgs/{org_id}/transfer-ownership/{user_b_id}", None),
        ("DELETE", f"/api/v1/orgs/{org_id}", None),
    ):
        resp = await async_client.request(method, path, json=body, headers=headers_a)
        assert resp.status_code == 404, (method, path, resp.status_code)


# --- Findings 4 & 10: audit trail and PII-free events -------------------------


@pytest.mark.asyncio
async def test_invitation_events_carry_no_email_and_actions_are_audited(
    async_client: AsyncClient, user_a, user_b, org_with_owner, captured
) -> None:
    owner_id, headers_a = user_a
    user_b_id, headers_b = user_b
    org_id = org_with_owner["id"]
    captured["events"].clear()
    captured["audit"].clear()

    inv = await _invite(async_client, org_id, headers_a, email_of(user_b_id))
    revoked = await _invite(async_client, org_id, headers_a, "other@example.com")
    assert (
        await async_client.delete(
            f"/api/v1/orgs/{org_id}/invitations/{revoked['id']}", headers=headers_a
        )
    ).status_code == 204
    assert (await _accept(async_client, inv["token"], headers_b)).status_code == 200
    assert (
        await async_client.patch(
            f"/api/v1/orgs/{org_id}/members/{user_b_id}",
            json={"role": "admin"},
            headers=headers_a,
        )
    ).status_code == 200
    assert (
        await async_client.post(
            f"/api/v1/orgs/{org_id}/transfer-ownership/{user_b_id}", headers=headers_a
        )
    ).status_code == 200
    _, new_owner_headers = user_b
    assert (
        await async_client.delete(
            f"/api/v1/orgs/{org_id}/members/{owner_id}", headers=new_owner_headers
        )
    ).status_code == 204

    for channel, payload in captured["events"]:
        assert "email" not in payload, (channel, payload)
        assert "token" not in payload and "token_hash" not in payload
    channels = [c for c, _ in captured["events"]]
    assert channels.count(events.INVITATION_CREATED_V1) == 2
    assert events.INVITATION_REVOKED_V1 in channels
    assert events.INVITATION_ACCEPTED_V1 in channels
    accepted = next(
        p for c, p in captured["events"] if c == events.INVITATION_ACCEPTED_V1
    )
    assert accepted["invitation_id"] == inv["id"]

    # transfer publishes role_changed for BOTH the new and the demoted owner
    role_changes = [
        (p["user_id"], p["role"])
        for c, p in captured["events"]
        if c == events.MEMBER_ROLE_CHANGED_V1
    ]
    assert (str(user_b_id), ROLE_OWNER) in role_changes
    assert (str(owner_id), ROLE_ADMIN) in role_changes

    actions = [a for a, _ in captured["audit"]]
    assert actions == [
        "tenancy.invitation.created",
        "tenancy.invitation.created",
        "tenancy.invitation.revoked",
        "tenancy.invitation.accepted",
        "tenancy.member.role_changed",
        "tenancy.organization.ownership_transferred",
        "tenancy.member.removed",
    ]
    for _, kwargs in captured["audit"]:
        flat = repr(kwargs.get("data"))
        assert "@" not in flat  # no e-mail in audit data
        assert kwargs["actor_id"] is not None


@pytest.mark.asyncio
async def test_member_leave_is_audited_as_left(
    async_client: AsyncClient, user_a, user_b, org_with_owner, captured
) -> None:
    _, headers_a = user_a
    user_b_id, headers_b = user_b
    org_id = org_with_owner["id"]
    inv = await _invite(async_client, org_id, headers_a, email_of(user_b_id))
    assert (await _accept(async_client, inv["token"], headers_b)).status_code == 200
    captured["audit"].clear()
    resp = await async_client.delete(
        f"/api/v1/orgs/{org_id}/members/{user_b_id}", headers=headers_b
    )
    assert resp.status_code == 204
    assert [a for a, _ in captured["audit"]] == ["tenancy.member.left"]
    assert captured["audit"][0][1]["required"] is True


# --- Finding 11: stale tenant_ids after removal / org deletion -----------------


@pytest.mark.asyncio
async def test_member_removal_and_leave_revoke_the_users_tokens(
    async_client: AsyncClient, user_a, user_b, user_c, org_with_owner, revoked_users
) -> None:
    _, headers_a = user_a
    user_b_id, headers_b = user_b
    user_c_id, headers_c = user_c
    org_id = org_with_owner["id"]
    for uid, headers in ((user_b_id, headers_b), (user_c_id, headers_c)):
        inv = await _invite(async_client, org_id, headers_a, email_of(uid))
        assert (await _accept(async_client, inv["token"], headers)).status_code == 200
    assert revoked_users == []

    removed = await async_client.delete(
        f"/api/v1/orgs/{org_id}/members/{user_b_id}", headers=headers_a
    )
    assert removed.status_code == 204
    left = await async_client.delete(
        f"/api/v1/orgs/{org_id}/members/{user_c_id}", headers=headers_c
    )
    assert left.status_code == 204
    # immediate (before commit) + post-commit re-mark, per user
    assert revoked_users == [user_b_id, user_b_id, user_c_id, user_c_id]


@pytest.mark.asyncio
async def test_org_deletion_revokes_every_members_tokens(
    async_client: AsyncClient, user_a, user_b, org_with_owner, revoked_users
) -> None:
    owner_id, headers_a = user_a
    user_b_id, headers_b = user_b
    org_id = org_with_owner["id"]
    inv = await _invite(async_client, org_id, headers_a, email_of(user_b_id))
    assert (await _accept(async_client, inv["token"], headers_b)).status_code == 200
    resp = await async_client.delete(f"/api/v1/orgs/{org_id}", headers=headers_a)
    assert resp.status_code == 204
    assert set(revoked_users) == {owner_id, user_b_id}


# --- Required audits land in audit_log inside the request UoW ------------------


@pytest.mark.asyncio
async def test_required_audit_is_persisted_in_the_request_transaction(
    async_client: AsyncClient,
    user_a,
    user_b,
    org_with_owner,
    test_db: AsyncSession,
    _ambient_uow,
) -> None:
    _, headers_a = user_a
    user_b_id, headers_b = user_b
    org_id = org_with_owner["id"]
    inv = await _invite(async_client, org_id, headers_a, email_of(user_b_id))
    assert (await _accept(async_client, inv["token"], headers_b)).status_code == 200
    await test_db.flush()  # the audit store adds rows; flush before a Core read
    rows = (
        await test_db.execute(
            text("SELECT action FROM audit_log WHERE resource_id = :rid"),
            {"rid": inv["id"]},
        )
    ).all()
    assert sorted(r.action for r in rows) == [
        "tenancy.invitation.accepted",
        "tenancy.invitation.created",
    ]
    assert _ambient_uow.audits == []  # nothing fell through to the ambient scope


# --- Invitation e-mail --------------------------------------------------------


@pytest.mark.asyncio
async def test_invitation_email_is_sent_with_accept_link(
    async_client: AsyncClient,
    user_a,
    org_with_owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom_tenancy.services import invitation_service

    sent: list[Any] = []

    class _Notifier:
        async def send(self, notification: Any) -> None:
            sent.append(notification)

    monkeypatch.setattr(invitation_service, "notifier", lambda *a: _Notifier())
    _, headers_a = user_a
    inv = await _invite(
        async_client, org_with_owner["id"], headers_a, "mail@example.com"
    )

    assert len(sent) == 1
    n = sent[0]
    assert n.channel == "email"
    assert n.recipient == "mail@example.com"
    assert f"#token={inv['token']}" in n.body
    assert org_with_owner["name"] in n.subject
    assert n.data == {
        "organization_id": org_with_owner["id"],
        "invitation_id": inv["id"],
    }


@pytest.mark.asyncio
async def test_invitation_email_failure_does_not_fail_request(
    async_client: AsyncClient,
    user_a,
    org_with_owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom_tenancy.services import invitation_service

    class _Broken:
        async def send(self, notification: Any) -> None:
            raise RuntimeError(f"smtp down: {notification.body}")

    monkeypatch.setattr(invitation_service, "notifier", lambda *a: _Broken())
    _, headers_a = user_a
    resp = await async_client.post(
        f"/api/v1/orgs/{org_with_owner['id']}/invitations",
        json={"email": "down@example.com"},
        headers=headers_a,
    )
    assert resp.status_code == 201


# --- Concurrency (real, separately committed transactions) -------------------


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _seed_org(
    sessions: async_sessionmaker[AsyncSession], owner_id: uuid.UUID
) -> uuid.UUID:
    async with sessions() as db:
        org = await OrganizationService.create(
            OrganizationCreateIn(name="Race Co", slug=f"race-{uuid.uuid4().hex[:8]}"),
            owner_id=owner_id,
            db=db,
        )
        await db.commit()
        return org.id


class _Barrier:
    """Minimal asyncio barrier (``asyncio.Barrier`` needs Python 3.11)."""

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrived = 0
        self._event = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived >= self._parties:
            self._event.set()
        await self._event.wait()


async def _run_committed(
    sessions: async_sessionmaker[AsyncSession],
    fn: Any,
    barrier: "_Barrier | None" = None,
) -> Any:
    """Run ``fn(db)`` in its own committed transaction.

    With a ``barrier`` every racer first opens its connection + transaction
    and waits, so the racing statements genuinely overlap.
    """
    async with sessions() as db:
        try:
            if barrier is not None:
                await db.execute(text("SELECT 1"))
                await barrier.wait()
            result = await fn(db)
            await db.commit()
            return result
        except Exception as exc:  # noqa: BLE001 - returned for assertions
            await db.rollback()
            return exc


@pytest.mark.asyncio
async def test_concurrent_accept_admits_exactly_one(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    owner_id = uuid.uuid4()
    org_id = await _seed_org(sessions, owner_id)
    email = "race@example.com"
    async with sessions() as db:
        created = await InvitationService.create(
            org_id, InvitationCreateIn(email=email), owner_id, db
        )
        await db.commit()
    token = created.token

    user_1, user_2 = uuid.uuid4(), uuid.uuid4()
    barrier = _Barrier(2)
    results = await asyncio.gather(
        _run_committed(
            sessions,
            lambda db: InvitationService.accept(
                token, user_1, db, email=email, email_verified=True
            ),
            barrier,
        ),
        _run_committed(
            sessions,
            lambda db: InvitationService.accept(
                token, user_2, db, email=email, email_verified=True
            ),
            barrier,
        ),
    )
    winners = [r for r in results if isinstance(r, OrganizationMember)]
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1 and isinstance(losers[0], TenancyNotFoundError)

    async with sessions() as db:
        members = await MembershipService.list_members(org_id, db)
    assert len(members) == 2  # owner + exactly one invitee


@pytest.mark.asyncio
async def test_concurrent_accept_same_user_no_500(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    owner_id = uuid.uuid4()
    org_id = await _seed_org(sessions, owner_id)
    email = "twice@example.com"
    async with sessions() as db:
        created = await InvitationService.create(
            org_id, InvitationCreateIn(email=email), owner_id, db
        )
        await db.commit()
    user = uuid.uuid4()
    barrier = _Barrier(2)
    results = await asyncio.gather(
        *(
            _run_committed(
                sessions,
                lambda db: InvitationService.accept(
                    created.token, user, db, email=email, email_verified=True
                ),
                barrier,
            )
            for _ in range(2)
        )
    )
    assert sum(isinstance(r, OrganizationMember) for r in results) == 1
    for r in results:
        if isinstance(r, Exception):
            assert isinstance(r, TenancyNotFoundError | TenancyConflictError), r


@pytest.mark.asyncio
async def test_concurrent_ownership_transfers_leave_one_owner(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    owner_id, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    org_id = await _seed_org(sessions, owner_id)
    async with sessions() as db:
        await MembershipService.add_member(org_id, b, ROLE_MEMBER, db)
        await MembershipService.add_member(org_id, c, ROLE_MEMBER, db)
        await db.commit()

    barrier = _Barrier(2)
    results = await asyncio.gather(
        _run_committed(
            sessions,
            lambda db: OrganizationService.transfer_ownership(
                org_id, b, db, actor_id=owner_id
            ),
            barrier,
        ),
        _run_committed(
            sessions,
            lambda db: OrganizationService.transfer_ownership(
                org_id, c, db, actor_id=owner_id
            ),
            barrier,
        ),
    )
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(failures) == 1 and isinstance(failures[0], TenancyForbiddenError)

    async with sessions() as db:
        owners = (
            (
                await db.execute(
                    select(OrganizationMember.user_id).where(
                        OrganizationMember.organization_id == org_id,
                        OrganizationMember.role == ROLE_OWNER,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(owners) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_invitations_one_wins(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    owner_id = uuid.uuid4()
    org_id = await _seed_org(sessions, owner_id)
    data = InvitationCreateIn(email="dup-race@example.com")
    barrier = _Barrier(2)
    results = await asyncio.gather(
        *(
            _run_committed(
                sessions,
                lambda db: InvitationService.create(org_id, data, owner_id, db),
                barrier,
            )
            for _ in range(2)
        )
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], TenancyConflictError)


@pytest.mark.asyncio
async def test_slug_race_is_conflict_not_500(
    sessions: async_sessionmaker[AsyncSession], captured
) -> None:
    slug = f"slug-{uuid.uuid4().hex[:8]}"
    barrier = _Barrier(2)
    results = await asyncio.gather(
        *(
            _run_committed(
                sessions,
                lambda db: OrganizationService.create(
                    OrganizationCreateIn(name="S", slug=slug), uuid.uuid4(), db
                ),
                barrier,
            )
            for _ in range(2)
        )
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], TenancyConflictError)


@pytest.mark.asyncio
async def test_transfer_by_non_owner_rejected(test_db: AsyncSession, captured) -> None:
    owner_id, admin_id = uuid.uuid4(), uuid.uuid4()
    org = await OrganizationService.create(
        OrganizationCreateIn(name="T", slug=f"t-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=test_db,
    )
    await MembershipService.add_member(org.id, admin_id, ROLE_ADMIN, test_db)
    with pytest.raises(TenancyForbiddenError):
        await OrganizationService.transfer_ownership(
            org.id, admin_id, test_db, actor_id=admin_id
        )
    with pytest.raises(TenancyConflictError):
        await OrganizationService.transfer_ownership(
            org.id, owner_id, test_db, actor_id=owner_id
        )


@pytest.mark.asyncio
async def test_migration_hash_matches_python_hash(test_db: AsyncSession) -> None:
    """The 0002 backfill SQL must hash exactly like ``hash_invitation_token``."""
    from sandloom_tenancy.models.invitation import hash_invitation_token

    token = "abc-DEF_123~ünï"
    sql = (
        await test_db.execute(
            text("SELECT encode(sha256(convert_to(:t, 'UTF8')), 'hex')"),
            {"t": token},
        )
    ).scalar_one()
    assert sql == hash_invitation_token(token)


# --- Token revocation is a post-COMMIT effect ----------------------------------


class _Abort(Exception):
    pass


async def _org_with_member(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    org_id = await _seed_org(sessions, owner_id)
    async with sessions() as db:
        await MembershipService.add_member(org_id, member_id, ROLE_MEMBER, db)
        await db.commit()
    return org_id, owner_id, member_id


@pytest.mark.asyncio
async def test_rolled_back_member_removal_skips_the_post_commit_revocation(
    sessions: async_sessionmaker[AsyncSession], revoked_users: list[Any]
) -> None:
    """Removal revokes IMMEDIATELY (``require_immediate``): the mark is written
    before commit — so a later rollback leaves the user logged out (the safe
    direction) — and the post-commit re-mark runs only when the change commits.
    """
    org_id, owner_id, member_id = await _org_with_member(sessions)
    with pytest.raises(_Abort):
        async with committing_uow(sessions) as db:
            await MembershipService.remove_member(
                org_id, member_id, db, actor_id=owner_id
            )
            assert revoked_users == [member_id]  # written before the commit
            raise _Abort  # e.g. a later step of the same request fails
    assert revoked_users == [member_id]  # no post-commit write
    revoked_users.clear()

    async with committing_uow(sessions) as db:
        await MembershipService.remove_member(org_id, member_id, db, actor_id=owner_id)
        assert revoked_users == [member_id]
    assert revoked_users == [member_id, member_id]  # + the post-commit re-mark


@pytest.mark.asyncio
async def test_rolled_back_org_deletion_skips_the_post_commit_revocation(
    sessions: async_sessionmaker[AsyncSession], revoked_users: list[Any]
) -> None:
    org_id, owner_id, member_id = await _org_with_member(sessions)
    with pytest.raises(_Abort):
        async with committing_uow(sessions) as db:
            await OrganizationService.soft_delete(org_id, db, actor_id=owner_id)
            raise _Abort
    assert sorted(map(str, revoked_users)) == sorted([str(owner_id), str(member_id)])
    revoked_users.clear()

    async with committing_uow(sessions) as db:
        await OrganizationService.soft_delete(org_id, db, actor_id=owner_id)
    assert sorted(map(str, revoked_users)) == sorted(
        [str(owner_id), str(member_id)] * 2
    )
