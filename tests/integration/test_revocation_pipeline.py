"""Failure / concurrency scenarios of the revocation + membership-event pipeline.

The reviewer's scenarios (PR #45 follow-up), each against real PostgreSQL and
(where the cache semantics matter) the real Redis CAS:

(a) commit succeeds, the mark write fails and the process dies before its
    retry -> the committed ``core.auth.tokens_revoked.v1`` outbox row, applied
    by identity's durable applier (at-least-once, idempotent), still revokes;
(b) concurrent revocations of one user -> the mark never moves backwards;
(c) a member removal racing a request that ALREADY passed the membership
    check -> that request completes (authorization is fresh at read time
    only: the documented TOCTOU window); the NEXT request is rejected;
(d) redelivered / out-of-order member events -> a consumer using ``version``
    never regresses; rolling upgrade: a version-less event (old instance)
    triggers a refresh from the owner's read contract, in either order;
plus the measured commit -> access-stops window with and without
``require_immediate``.
"""

from __future__ import annotations

import asyncio
import contextvars
import random
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sandloom.core.auth import marks, revocation, tokens, verification
from sandloom.core.config.settings import settings
from sandloom.core.infra.contracts import portable_set_max
from sandloom.core.module_api import bind_contract_session
from sandloom.core.testing import InMemoryCache, committing_uow, make_user_headers
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.shared.contracts.tenancy_dto import MemberState
from sandloom.shared.membership import member_event_action, refreshed_state_wins
from sandloom_identity.sessions import apply_token_revocation
from sandloom_tenancy import events
from sandloom_tenancy.contracts import MembershipReadContract
from sandloom_tenancy.models import ROLE_ADMIN, ROLE_MEMBER, OrganizationMember
from sandloom_tenancy.schemas import OrganizationCreateIn
from sandloom_tenancy.services import MembershipService, OrganizationService


class _Crash(BaseException):
    """The process dies (not an ``Exception``: nothing may swallow it)."""


@pytest.fixture
def sessions(db_engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
def mem_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryCache:
    import sandloom.core.infra.access as access

    fake = InMemoryCache()
    monkeypatch.setattr(access, "cache", lambda name="default": fake)
    return fake


@pytest.fixture
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "auth_revocation_retry_base_seconds", 0.01, raising=False
    )
    monkeypatch.setattr(settings, "auth_revocation_write_attempts", 3, raising=False)


def _claims(sub: Any) -> dict[str, Any]:
    claims = tokens.decode_token(
        tokens.create_access_token({"sub": str(sub), "token_type": "access"})
    )
    assert claims is not None
    return claims


async def _outbox(
    sessions: async_sessionmaker[AsyncSession], channel: str
) -> list[dict[str, Any]]:
    """Committed outbox rows of ``channel`` as the envelopes the relay sends."""
    async with sessions() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT id, payload FROM outbox_messages WHERE channel = :c "
                    "ORDER BY emitted_at"
                ),
                {"c": channel},
            )
        ).all()
    return [{"id": str(r.id), "payload": r.payload} for r in rows]


async def _org_with_member(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    async with committing_uow(sessions) as db:
        org = await OrganizationService.create(
            OrganizationCreateIn(name="Pipe Co", slug=f"p-{uuid.uuid4().hex[:8]}"),
            owner_id=owner_id,
            db=db,
        )
        org_id = org.id
        await MembershipService.add_member(org_id, member_id, ROLE_MEMBER, db)
    return org_id, owner_id, member_id


# --------------------------------------------------------------------------- #
# (a) commit ok, mark write fails, crash before the retry
# --------------------------------------------------------------------------- #
async def test_a_crash_after_a_failed_mark_write_is_applied_from_the_outbox(
    sessions: async_sessionmaker[AsyncSession],
    mem_cache: InMemoryCache,
    fast_retries: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (non-immediate) path, e.g. a password change."""
    sub = uuid.uuid4()
    old = _claims(sub)
    mem_cache.broken = True  # the first post-commit write fails ...

    async def _die(delay: float) -> None:  # ... and the process dies in the
        raise _Crash  # backoff before its retry

    with monkeypatch.context() as patch, pytest.raises(_Crash):
        patch.setattr(asyncio, "sleep", _die)
        async with committing_uow(sessions):
            await revocation.revoke_user_tokens_on_commit(sub, reason="password")
    # "restart": a fresh process, the cache is back
    mem_cache.broken = False
    assert await verification.is_token_revoked(old) is False  # the window is open

    pending = await _outbox(sessions, marks.TOKENS_REVOKED_CHANNEL)
    assert [e["payload"]["user_id"] for e in pending] == [str(sub)]
    # The relay re-sends it (at-least-once): identity's applier, twice.
    await apply_token_revocation(None, pending[0])
    await apply_token_revocation(None, pending[0])
    assert await verification.is_token_revoked(old) is True
    fresh = _claims(sub)
    await verification.ensure_token_not_revoked(None, fresh)  # re-login works


async def test_a_applier_redelivery_while_the_cache_is_still_down(
    sessions: async_sessionmaker[AsyncSession],
    mem_cache: InMemoryCache,
    fast_retries: None,
) -> None:
    sub = uuid.uuid4()
    mem_cache.broken = True
    async with committing_uow(sessions):
        await revocation.revoke_user_tokens_on_commit(sub, reason="password")
    (envelope,) = await _outbox(sessions, marks.TOKENS_REVOKED_CHANNEL)
    with pytest.raises(marks.RevocationUnavailableError):
        await apply_token_revocation(None, envelope)  # -> redelivered
    mem_cache.broken = False
    await apply_token_revocation(None, envelope)
    assert marks.subject_key(sub) in mem_cache.store


async def test_a_immediate_removal_needs_no_applier_after_a_crash(
    sessions: async_sessionmaker[AsyncSession],
    mem_cache: InMemoryCache,
    fast_retries: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Member removal (``require_immediate``): the mark is written before
    commit, so a crash after commit leaves NO window; the outbox row is only
    a durable duplicate (applied idempotently)."""
    org_id, owner_id, member_id = await _org_with_member(sessions)
    old = _claims(member_id)

    async def _die(delay: float) -> None:
        raise _Crash

    real_revoke = revocation.revoke_user_tokens
    calls = 0

    async def _fail_after_first(user_id: Any, *, valid_after: Any = None) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:  # the synchronous, pre-commit write succeeds
            return await real_revoke(user_id, valid_after=valid_after)
        return False  # the post-commit re-mark fails, then the process dies

    with monkeypatch.context() as patch, pytest.raises(_Crash):
        patch.setattr(revocation, "revoke_user_tokens", _fail_after_first)
        patch.setattr(asyncio, "sleep", _die)
        async with committing_uow(sessions) as db:
            await MembershipService.remove_member(
                org_id, member_id, db, actor_id=owner_id
            )
    assert await verification.is_token_revoked(old) is True  # no window at all
    (envelope,) = await _outbox(sessions, marks.TOKENS_REVOKED_CHANNEL)
    stored = mem_cache.store[marks.subject_key(member_id)]
    await apply_token_revocation(None, envelope)  # late duplicate: no-op
    assert mem_cache.store[marks.subject_key(member_id)] == stored


# --------------------------------------------------------------------------- #
# (b) concurrent revocations never move the mark backwards
# --------------------------------------------------------------------------- #
async def test_b_concurrent_revocations_on_real_redis_keep_the_highest_mark(
    sandloom_cache: Any,
) -> None:
    sub = str(uuid.uuid4())
    base = time.time()
    mark_values = [base + i / 1000 for i in range(40)]
    order = mark_values[:]
    random.Random(7).shuffle(order)
    results = await asyncio.gather(
        *(revocation.revoke_user_tokens(sub, valid_after=m) for m in order)
    )
    assert all(results)
    stored = await sandloom_cache.get(marks.subject_key(sub))
    assert marks._parse_mark_us(stored) == marks._seconds_to_us(max(mark_values))


class _InterleavingCache(InMemoryCache):
    """Pauses the FIRST writer between its read and its write until the second
    writer has finished — the race the PORTABLE ``set_max`` (add /
    compare-and-set loop, drivers without the fused op) must win."""

    async def set_max(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        return await portable_set_max(self, key, value, ttl_seconds=ttl_seconds)

    def __init__(self) -> None:
        super().__init__()
        self.read_done = asyncio.Event()
        self.other_done = asyncio.Event()
        self._paused = False

    async def get(self, key: str) -> str | None:
        value = await super().get(key)
        if not self._paused:
            self._paused = True
            self.read_done.set()
            await self.other_done.wait()
        return value


@pytest.mark.parametrize("seed", [None, "older"])
@pytest.mark.parametrize("first", ["low", "high"])
async def test_b_interleaved_revocations_never_regress(
    monkeypatch: pytest.MonkeyPatch, first: str, seed: str | None
) -> None:
    import sandloom.core.infra.access as access

    cache = _InterleavingCache()
    monkeypatch.setattr(access, "cache", lambda name="default": cache)
    sub = str(uuid.uuid4())
    base = time.time()
    if seed is not None:  # an existing older mark: the CAS (not add) branch
        cache.store[marks.subject_key(sub)] = marks._format_mark(
            marks._seconds_to_us(base - 10)
        )
    low, high = base + 0.1, base + 0.9
    mark_values = {"low": low, "high": high}
    second = "high" if first == "low" else "low"

    async def _first() -> bool:
        return await revocation.revoke_user_tokens(sub, valid_after=mark_values[first])

    async def _second() -> bool:
        await cache.read_done.wait()
        try:
            return await revocation.revoke_user_tokens(
                sub, valid_after=mark_values[second]
            )
        finally:
            cache.other_done.set()

    assert all(await asyncio.gather(_first(), _second()))
    stored = cache.store[marks.subject_key(sub)]
    assert marks._parse_mark_us(stored) == marks._seconds_to_us(high)


async def test_b_late_applier_after_a_newer_on_commit_revocation(
    sessions: async_sessionmaker[AsyncSession], mem_cache: InMemoryCache
) -> None:
    """Two revocations of one user commit; the OLDER one's durable event is
    applied last (redelivery): the newer mark stays."""
    sub = uuid.uuid4()
    async with committing_uow(sessions):
        await revocation.revoke_user_tokens_on_commit(
            sub, reason="first", valid_after=time.time()
        )
    async with committing_uow(sessions):
        await revocation.revoke_user_tokens_on_commit(
            sub, reason="second", valid_after=time.time() + 5
        )
    newest = mem_cache.store[marks.subject_key(sub)]
    for envelope in await _outbox(sessions, marks.TOKENS_REVOKED_CHANNEL):
        await apply_token_revocation(None, envelope)
    assert mem_cache.store[marks.subject_key(sub)] == newest


# --------------------------------------------------------------------------- #
# (c) removal racing a request that already passed the membership check
# --------------------------------------------------------------------------- #
async def test_c_in_flight_request_completes_and_the_next_one_is_rejected(
    real_uow_client: Any,
    sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOCTOU, precisely: authorization (token revocation mark + live
    membership) is evaluated when the request's guards run. A request whose
    guards passed BEFORE the removal committed finishes with the access it
    was granted (here: it lists the members, reading AFTER the removal).
    Every request whose guards run after the removal's commit is rejected —
    the mark was written before that commit (``require_immediate``)."""
    from sandloom_tenancy.services.listing import TenancyLists

    org_id, owner_id, member_id = await _org_with_member(sessions)
    _, headers = make_user_headers(member_id, tenant=False)
    original = TenancyLists.members_of
    removed = asyncio.Event()

    async def _members_of(*args: Any, **kwargs: Any) -> Any:
        # The guard already passed; the owner removes the member NOW (another
        # request: its own context, its own Unit of Work).
        if not removed.is_set():

            async def _owner_removes() -> None:
                async with committing_uow(sessions) as db:
                    await MembershipService.remove_member(
                        org_id, member_id, db, actor_id=owner_id
                    )

            await asyncio.create_task(_owner_removes(), context=contextvars.Context())
            removed.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(TenancyLists, "members_of", staticmethod(_members_of))
    in_flight = await real_uow_client.get(
        f"/api/v1/orgs/{org_id}/members", headers=headers
    )
    assert removed.is_set()
    assert in_flight.status_code == 200, in_flight.text  # the window
    assert str(member_id) not in {m["user_id"] for m in in_flight.json()}

    following = await real_uow_client.get(
        f"/api/v1/orgs/{org_id}/members", headers=headers
    )
    assert following.status_code == 401, following.text
    assert following.json()["detail"] == "Token has been revoked"


# --------------------------------------------------------------------------- #
# (d) member events: redelivery, reordering, rolling upgrade
# --------------------------------------------------------------------------- #
class _MemberProjection:
    """A reference consumer of the member events (what a read model does),
    following :func:`sandloom.shared.membership.member_event_action`."""

    def __init__(self, refresh: Callable[..., Any]) -> None:
        self.rows: dict[tuple[str, str], tuple[str | None, int]] = {}
        self.refresh = refresh
        self.history: list[int] = []

    async def handle(self, channel: str, payload: dict[str, Any]) -> str:
        key = (payload["organization_id"], payload["user_id"])
        stored = self.rows.get(key)
        stored_version = None if stored is None else stored[1]
        action = member_event_action(payload, stored_version)
        if action == "apply":
            role = None if channel == events.MEMBER_REMOVED_V1 else payload["role"]
            self.rows[key] = (role, payload["version"])
        elif action == "refresh":
            state: MemberState = await self.refresh(
                uuid.UUID(key[0]), uuid.UUID(key[1])
            )
            if refreshed_state_wins(state, stored_version):
                self.rows[key] = (state.role, state.version)
        if key in self.rows:
            self.history.append(self.rows[key][1])
        return action


_MEMBER_CHANNELS = (
    events.MEMBER_ADDED_V1,
    events.MEMBER_ROLE_CHANGED_V1,
    events.MEMBER_REMOVED_V1,
)


async def _lifecycle(
    db: AsyncSession, ambient_uow: Any
) -> tuple[uuid.UUID, uuid.UUID, list[tuple[str, dict[str, Any]]]]:
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    org = await OrganizationService.create(
        OrganizationCreateIn(name="Evt Co", slug=f"e-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=db,
    )
    await MembershipService.add_member(org.id, member_id, ROLE_MEMBER, db)
    await MembershipService.change_role(org.id, member_id, ROLE_ADMIN, db)
    await MembershipService.change_role(org.id, member_id, ROLE_MEMBER, db)
    await MembershipService.remove_member(org.id, member_id, db)
    await MembershipService.add_member(org.id, member_id, ROLE_ADMIN, db)
    published = [
        (c, p)
        for c, p, _ in ambient_uow.events
        if c in _MEMBER_CHANNELS and p["user_id"] == str(member_id)
    ]
    return org.id, member_id, published


@pytest.fixture
def no_revocation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(user_id: Any, *, valid_after: Any = None) -> bool:
        return True

    monkeypatch.setattr(revocation, "revoke_user_tokens", _ok)


@pytest.mark.parametrize("seed", range(6))
async def test_d_redelivered_and_reordered_versioned_events_never_regress(
    db: AsyncSession, ambient_uow: Any, no_revocation: None, seed: int
) -> None:
    org_id, member_id, published = await _lifecycle(db, ambient_uow)
    assert len(published) == 5
    deliveries = published + random.Random(seed).sample(published, 3)  # dupes
    random.Random(seed).shuffle(deliveries)

    async def _refresh(org: uuid.UUID, user: uuid.UUID) -> MemberState:
        raise AssertionError("versioned events never need a refresh")

    projection = _MemberProjection(_refresh)
    for channel, payload in deliveries:
        await projection.handle(channel, payload)
    assert projection.history == sorted(projection.history)  # never backwards
    key = (str(org_id), str(member_id))
    assert projection.rows[key] == (ROLE_ADMIN, published[-1][1]["version"])


async def _old_instance_role_change(
    db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> tuple[str, dict[str, Any]]:
    """What a PRE-0005 tenancy instance does: change the role WITHOUT bumping
    the member version and publish the event WITHOUT ``version``."""
    await db.execute(
        update(OrganizationMember)
        .where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
        .values(role=role)
    )
    await db.flush()
    payload = events.member_payload(org_id, user_id, role, None)
    payload.pop("version", None)
    return events.MEMBER_ROLE_CHANGED_V1, payload


@pytest.mark.parametrize("order", ["versioned_first", "versionless_first"])
async def test_d_rolling_upgrade_versionless_event_refreshes_from_the_owner(
    db: AsyncSession, ambient_uow: Any, no_revocation: None, order: str
) -> None:
    """NEW instance: v-event (role admin); THEN an OLD instance changes the
    role to member and publishes no version. Whatever the delivery order, the
    consumer ends on the latest change and never regresses afterwards."""
    owner_id, member_id = uuid.uuid4(), uuid.uuid4()
    org = await OrganizationService.create(
        OrganizationCreateIn(name="Mix Co", slug=f"m-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=db,
    )
    await MembershipService.add_member(org.id, member_id, ROLE_MEMBER, db)
    await MembershipService.change_role(org.id, member_id, ROLE_ADMIN, db)
    new_events = [
        (c, p)
        for c, p, _ in ambient_uow.events
        if c in _MEMBER_CHANNELS and p["user_id"] == str(member_id)
    ]
    old_event = await _old_instance_role_change(db, org.id, member_id, ROLE_MEMBER)
    assert "version" not in old_event[1]

    async def _refresh(org_id: uuid.UUID, user_id: uuid.UUID) -> MemberState:
        with bind_contract_session(db):  # the owner's read contract
            return await MembershipReadContract.get_member_state(org_id, user_id)

    projection = _MemberProjection(_refresh)
    deliveries = (
        [*new_events, old_event]
        if order == "versioned_first"
        else [old_event, *reversed(new_events)]
    )
    actions = [await projection.handle(c, p) for c, p in deliveries]
    assert "refresh" in actions
    key = (str(org.id), str(member_id))
    # The later (old-instance) change is NOT ignored ...
    assert projection.rows[key][0] == ROLE_MEMBER
    # ... and a redelivery of the earlier versioned event cannot regress it.
    for channel, payload in new_events:
        assert await projection.handle(channel, payload) == "skip"
    assert projection.rows[key][0] == ROLE_MEMBER
    assert projection.history == sorted(projection.history)
    # A NEW-instance change after the upgrade is applied normally.
    await MembershipService.change_role(org.id, member_id, ROLE_ADMIN, db)
    channel, latest = next(
        (c, p)
        for c, p, _ in reversed(ambient_uow.events)
        if c == events.MEMBER_ROLE_CHANGED_V1 and p["user_id"] == str(member_id)
    )
    assert await projection.handle(channel, latest) == "apply"
    assert projection.rows[key] == (ROLE_ADMIN, latest["version"])


def test_d_decision_table() -> None:
    assert member_event_action({"version": 3}, None) == "apply"
    assert member_event_action({"version": 3}, 2) == "apply"
    assert member_event_action({"version": 3}, 3) == "skip"
    assert member_event_action({"version": 2}, 3) == "skip"
    for missing in ({}, {"version": None}, {"version": 0}, {"version": True}):
        assert member_event_action(missing, 3) == "refresh"
        assert member_event_action(missing, None) == "refresh"


# --------------------------------------------------------------------------- #
# The commit -> access-stops window, measured (functional timing, no load)
# --------------------------------------------------------------------------- #
async def _accepted_until_rejected(claims: dict[str, Any], deadline: float) -> float:
    """Seconds until ``claims`` is rejected (polling every 10 ms)."""
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        if await verification.is_token_revoked(claims):
            return time.monotonic() - start
        await asyncio.sleep(0.01)
    return float("inf")


async def test_window_default_path_lasts_until_the_applier_runs(
    sessions: async_sessionmaker[AsyncSession],
    mem_cache: InMemoryCache,
    fast_retries: None,
) -> None:
    """BEFORE (default path): outage during the commit, cache back, applier
    delayed by ``delay`` (relay tick / consumer retry backoff / restart): an
    old token is ACCEPTED for the whole delay after recovery."""
    sub = uuid.uuid4()
    old = _claims(sub)
    mem_cache.broken = True
    async with committing_uow(sessions):
        await revocation.revoke_user_tokens_on_commit(sub, reason="password")
    mem_cache.broken = False  # recovery
    (envelope,) = await _outbox(sessions, marks.TOKENS_REVOKED_CHANNEL)
    delay = 0.3

    async def _applier_later() -> None:
        await asyncio.sleep(delay)
        await apply_token_revocation(None, envelope)

    task = asyncio.create_task(_applier_later())
    window = await _accepted_until_rejected(old, deadline=5.0)
    await task
    assert delay * 0.9 <= window < delay + 1.0, window


async def test_window_immediate_path_is_zero_or_the_change_is_refused(
    sessions: async_sessionmaker[AsyncSession],
    mem_cache: InMemoryCache,
    fast_retries: None,
) -> None:
    """AFTER (``require_immediate``, member removal): during the outage the
    removal is refused (503); once it commits, the very first check after the
    commit rejects the old token — no applier involved."""
    org_id, owner_id, member_id = await _org_with_member(sessions)
    old = _claims(member_id)
    mem_cache.broken = True
    with pytest.raises(marks.RevocationUnavailableError):
        async with committing_uow(sessions) as db:
            await MembershipService.remove_member(
                org_id, member_id, db, actor_id=owner_id
            )
    async with sessions() as db:  # refused: still a member
        assert await MembershipService.get_role(org_id, member_id, db) is not None

    mem_cache.broken = False
    async with committing_uow(sessions) as db:
        await MembershipService.remove_member(org_id, member_id, db, actor_id=owner_id)
    # The very first check after the commit rejects: asserted directly, not
    # as a wall-clock window (a loaded host made one check take > 5 ms).
    assert await verification.is_token_revoked(old) is True
