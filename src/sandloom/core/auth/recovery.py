"""Re-derive the security cache's subject-state records after a restore (H-20).

The security cache (``SECURITY_CACHE_INSTANCE``, else the default cache)
holds one record per subject that is NOT in any database: the subject-state
record ``sandloom:auth:subject:{user}`` (ADR-0009 item 13), either
``v1:blocked:<since>`` (no TTL, written by
:func:`sandloom.core.auth.blocks.block_user`) or ``v1:after:<mark>`` (the access
-token revocation of :func:`sandloom.core.auth.revocation.revoke_user_tokens`).

Restoring a Redis snapshot (RDB / AOF) taken BEFORE a later block silently
lifts that block: the record is absent or an older ``after`` value, which
means "not blocked". Likewise a revocation written after the snapshot is
lost, so access tokens it revoked pass again until they expire.

Both are recorded durably in the databases, in the same transaction as the
change that caused them:

* every block / unblock writes a ``required=True`` audit row
  (``auth.user.blocked`` / ``auth.user.unblocked``) — the audit log is never
  purged by the framework;
* every ``revoke_user_tokens_on_commit`` writes ``core.auth.tokens_revoked.v1``
  into the outbox (kept ``OUTBOX_RETENTION_HOURS`` after delivery).

:func:`reconcile_security_state` replays them onto the cache: the newest
audit event per user decides whether the user is blocked (a block and an
unblock at the same instant resolve to BLOCKED — the safe side), and every
revocation still inside the mark lifetime is re-applied with the monotonic
writer (a mark is never lowered). It is what ``sandloom backup reconcile-security``
runs; the runbook (``docs/operate/operator-reference.md``) makes it a gate
before traffic is admitted after a recovery.

A ``blocked`` record whose newest audit event is an UNBLOCK (a snapshot older
than the unblock) is reported as stale and only lifted (compare-and-set to an
``after`` value, never a delete) with ``lift_stale_blocks=True``: keeping a
user blocked is the safe direction.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sandloom.core.auth import blocks as _blocks
from sandloom.core.auth import revocation as _revocation
from sandloom.core.auth.marks import (
    TOKENS_REVOKED_CHANNEL,
    _format_blocked,
    _revocation_ttl_seconds,
    _seconds_to_us,
    state_covers,
    state_is_blocked,
    subject_key,
)
from sandloom.core.config.settings import settings
from sandloom.core.infra import access as _access

logger = logging.getLogger(__name__)

#: Audit actions written by ``block_user`` / ``unblock_user``.
BLOCKED_ACTION = "auth.user.blocked"
UNBLOCKED_ACTION = "auth.user.unblocked"

_PAGE = 500


@dataclass(frozen=True, slots=True)
class BlockEvent:
    """One durable block / unblock record (from an audit log or an export)."""

    user_id: str
    occurred_at: datetime  # aware, UTC
    blocked: bool
    source: str


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _event_from_row(row: Mapping[str, Any], source: str) -> BlockEvent | None:
    action = row.get("action")
    if action not in (BLOCKED_ACTION, UNBLOCKED_ACTION):
        return None
    if row.get("resource_type") not in (None, "user"):
        return None
    user = row.get("resource_id")
    occurred = row.get("occurred_at")
    if not user or occurred is None:
        return None
    if isinstance(occurred, str):
        occurred = datetime.fromisoformat(occurred)
    return BlockEvent(
        user_id=str(user),
        occurred_at=_aware(occurred),
        blocked=action == BLOCKED_ACTION,
        source=source,
    )


async def read_block_events(
    session: Any, audit_store: Any, *, source: str, page: int = _PAGE
) -> list[BlockEvent]:
    """Every block / unblock audit row of one database (keyset-paged)."""
    events: list[BlockEvent] = []
    for action in (BLOCKED_ACTION, UNBLOCKED_ACTION):
        before: tuple[datetime, Any] | None = None
        while True:
            rows = await audit_store.query_page(
                session, limit=page, before=before, action=action
            )
            for row in rows:
                event = _event_from_row(row, source)
                if event is not None:
                    events.append(event)
            if len(rows) < page:
                break
            last = rows[-1]
            before = (last["occurred_at"], last["id"])
    return events


def read_block_events_file(path: str | Path) -> list[BlockEvent]:
    """Block / unblock records of an ``sandloom security audit-export`` JSONL file.

    The off-site export is the source of truth when the DATABASE itself was
    restored to a point before a block (its audit row is gone too).
    """
    events: list[BlockEvent] = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: not a JSON line") from exc
            if isinstance(row, dict):
                event = _event_from_row(row, f"file:{path}")
                if event is not None:
                    events.append(event)
    return events


def latest_block_states(events: Iterable[BlockEvent]) -> dict[str, BlockEvent]:
    """Newest event per user; a block and an unblock at one instant -> blocked."""
    latest: dict[str, BlockEvent] = {}
    for event in events:
        held = latest.get(event.user_id)
        if (
            held is None
            or event.occurred_at > held.occurred_at
            or (event.occurred_at == held.occurred_at and event.blocked)
        ):
            latest[event.user_id] = event
    return latest


async def read_revocations(
    session: Any,
    outbox_store: Any,
    *,
    since: datetime,
    page: int = 1000,
) -> tuple[dict[str, float], datetime | None]:
    """``({user: newest valid_after}, oldest row seen)`` from the outbox."""
    marks: dict[str, float] = {}
    oldest: datetime | None = None
    after: tuple[datetime, Any] | None = None
    while True:
        rows = await outbox_store.list_channel_since(
            session,
            channel=TOKENS_REVOKED_CHANNEL,
            since=since,
            limit=page,
            after=after,
        )
        for row in rows:
            emitted = _aware(row["emitted_at"])
            if oldest is None or emitted < oldest:
                oldest = emitted
            payload = row.get("payload") or {}
            user, valid_after = payload.get("user_id"), payload.get("valid_after")
            try:
                mark = float(str(valid_after))
            except ValueError:
                logger.warning("tokens_revoked row %s without valid_after", row["id"])
                continue
            if user and mark > marks.get(str(user), 0.0):
                marks[str(user)] = mark
        if len(rows) < page:
            break
        last = rows[-1]
        after = (last["emitted_at"], last["id"])
    return marks, oldest


def revocation_window_start(now: datetime | None = None) -> datetime:
    """Oldest instant whose revocation can still matter (the mark lifetime)."""
    now = now or datetime.now(UTC)
    return now - timedelta(seconds=_revocation_ttl_seconds())


def revocation_coverage_gap() -> str | None:
    """Why the outbox may NOT hold every revocation that still matters.

    Access tokens live ``ACCESS_TOKEN_EXPIRE_MINUTES`` (+ the revocation clock
    skew): a revocation older than that guards only expired tokens. The
    outbox keeps delivered rows ``OUTBOX_RETENTION_HOURS`` (0 = forever).
    """
    retention_s = float(settings.outbox_retention_hours) * 3600
    if retention_s == 0:
        return None
    access_s = int(settings.access_token_expire_minutes) * 60 + float(
        settings.auth_revocation_clock_skew_seconds
    )
    if retention_s >= access_s:
        return None
    return (
        f"OUTBOX_RETENTION_HOURS={settings.outbox_retention_hours:g} is shorter "
        f"than the access-token lifetime ({access_s:g}s): revocations older than "
        "the retention cannot be re-derived — revoke every session "
        "(sandloom security revoke-sessions --all) or rotate the JWT key"
    )


@dataclass(slots=True)
class ReconcileReport:
    """What :func:`reconcile_security_state` found and (with ``apply``) did."""

    applied: bool
    blocked_users: int = 0
    missing_blocks: list[str] = field(default_factory=list)
    stale_blocks: list[str] = field(default_factory=list)
    lifted_stale_blocks: list[str] = field(default_factory=list)
    revocations_seen: int = 0
    missing_revocations: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    revocation_window_start: datetime | None = None
    oldest_revocation_record: datetime | None = None

    @property
    def clean(self) -> bool:
        """Nothing missing (or everything re-applied) and full coverage."""
        if self.failures or self.warnings:
            return False
        if self.applied:
            return True
        return not (self.missing_blocks or self.missing_revocations)

    def as_dict(self) -> dict[str, Any]:
        def _ts(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "applied": self.applied,
            "clean": self.clean,
            "blocked_users": self.blocked_users,
            "missing_blocks": self.missing_blocks,
            "stale_blocks": self.stale_blocks,
            "lifted_stale_blocks": self.lifted_stale_blocks,
            "revocations_seen": self.revocations_seen,
            "missing_revocations": self.missing_revocations,
            "failures": self.failures,
            "warnings": self.warnings,
            "revocation_window_start": _ts(self.revocation_window_start),
            "oldest_revocation_record": _ts(self.oldest_revocation_record),
        }


async def _get_many(provider: Any, keys: Sequence[str]) -> list[Any]:
    get_many = getattr(provider, "get_many", None)
    if get_many is not None:
        return list(await get_many(list(keys)))
    return [await provider.get(key) for key in keys]


def _chunks(items: Sequence[str], size: int = 200) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def reconcile_security_state(  # noqa: C901
    *,
    block_events: Iterable[BlockEvent],
    revocations: Mapping[str, float],
    apply: bool,
    lift_stale_blocks: bool = False,
    window_start: datetime | None = None,
    oldest_revocation_record: datetime | None = None,
) -> ReconcileReport:
    """Compare the security cache with the durable records; repair with ``apply``.

    Dry-run (``apply=False``) only reads the cache. With ``apply`` every
    missing ``blocked`` record is written (the block instant, like
    ``block_user``; it outranks any ``after`` value there), every revocation
    whose record does not cover it is re-written through
    :func:`sandloom.core.auth.revocation.revoke_user_tokens` (monotonic: never
    lowers a newer mark, never lifts a block), and — only with
    ``lift_stale_blocks`` — ``blocked`` records whose newest audit event is an
    unblock are swapped to an ``after`` value. Raises nothing for a single
    key: failures are listed in the report (the CLI exits non-zero).
    """
    report = ReconcileReport(
        applied=apply,
        revocation_window_start=window_start,
        oldest_revocation_record=oldest_revocation_record,
    )
    gap = revocation_coverage_gap()
    if gap is not None:
        report.warnings.append(gap)
    provider = _access.security_cache()
    states = latest_block_states(block_events)

    users = sorted(states)
    for chunk in _chunks(users):
        values = await _get_many(provider, [subject_key(u) for u in chunk])
        for user, raw in zip(chunk, values, strict=True):
            state = states[user]
            if state.blocked:
                report.blocked_users += 1
                if state_is_blocked(raw):
                    continue
                report.missing_blocks.append(user)
                if apply:
                    since = _seconds_to_us(state.occurred_at.timestamp())
                    try:
                        await _blocks._write_state(
                            provider, user, _format_blocked(since), None
                        )
                    except Exception as exc:  # noqa: BLE001 - reported
                        report.failures.append(f"block {user}: {exc}")
            elif state_is_blocked(raw):
                report.stale_blocks.append(user)
                if apply and lift_stale_blocks:
                    try:
                        await _blocks._swap_blocked_for_after(provider, user)
                        report.lifted_stale_blocks.append(user)
                    except Exception as exc:  # noqa: BLE001 - reported
                        report.failures.append(f"unblock {user}: {exc}")

    report.revocations_seen = len(revocations)
    revoked = sorted(revocations)
    for chunk in _chunks(revoked):
        values = await _get_many(provider, [subject_key(u) for u in chunk])
        for user, raw in zip(chunk, values, strict=True):
            target = revocations[user]
            if state_covers(raw, _seconds_to_us(target)):
                continue
            report.missing_revocations.append(user)
            if apply and not await _revocation.revoke_user_tokens(
                user, valid_after=target
            ):
                report.failures.append(f"revocation {user}: cache write failed")

    if apply and (report.missing_blocks or report.missing_revocations):
        logger.warning(
            "Security state reconciled: %d block mark(s) and %d revocation "
            "mark(s) re-applied",
            len(report.missing_blocks),
            len(report.missing_revocations),
        )
    return report


__all__ = [
    "BLOCKED_ACTION",
    "UNBLOCKED_ACTION",
    "BlockEvent",
    "ReconcileReport",
    "latest_block_states",
    "read_block_events",
    "read_block_events_file",
    "read_revocations",
    "reconcile_security_state",
    "revocation_coverage_gap",
    "revocation_window_start",
]
