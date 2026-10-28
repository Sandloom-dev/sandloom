"""Commit hooks: ``on_commit`` / ``before_commit`` and their runners.

The queues a UoW session carries until its commit (each entry tagged with the
savepoint it was registered in, so a rolled-back ``begin_nested()`` drops its
callbacks), the request's captured commit checks, and the runners of stages 1
and 4 of
:data:`sandloom.core.unit_of_work.COMMIT_PIPELINE`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from typing import TYPE_CHECKING, Any

import sandloom.core.unit_of_work.risk as _risk
from sandloom.core.tenant_context import (
    _current_tenant,
    bind_tenant,
)
from sandloom.core.unit_of_work.request import (
    INTENT_UNIT_KEY,
    RECHECKED_KEY,
    SCOPE_KEY,
    SELF_REVOKED_FINAL_KEY,
    SELF_REVOKED_KEY,
    WRITES_KEY,
    admission_get,
)
from sandloom.core.unit_of_work.savepoints import savepoint_token, token_survived

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Annotation-only: the engine is paradigm-agnostic and imports no SQLAlchemy
    # at runtime (relational specifics stay behind the lazy sql_alchemy imports),
    # so it sits OUTSIDE the relational-paradigm boundary ratchet.
    from sqlalchemy.ext.asyncio import AsyncSession


from sandloom.core.unit_of_work.scope import (
    _OUTBOX_PENDING_KEY,
    _PENDING_CHECK_KEY,
    OnCommitCallback,
    _refuse_in_read_only_scope,
    _resolve_scoped_session,
)

logger = logging.getLogger("sandloom.core.unit_of_work")


#: session.info key holding ``(callback, savepoint_token)`` pairs from on_commit.
_ON_COMMIT_KEY = "sandloom_on_commit"


def _savepoint_token(session: AsyncSession) -> Any:
    """Opaque marker of the innermost transaction (relational sessions only)."""
    if not hasattr(session, "sync_session"):
        return None
    try:
        return savepoint_token(session)
    except Exception:  # noqa: BLE001 - untracked => runs iff the UoW commits
        return None


def _token_survived(session: AsyncSession, token: Any) -> bool:
    if token is None:
        return True
    try:
        return token_survived(session, token)
    except Exception:  # noqa: BLE001 - never drop a callback on inspection error
        return True


async def _call(callback: OnCommitCallback) -> None:
    result = callback()
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        await result


async def _call_in_tenant(
    callback: OnCommitCallback, tenant: _uuid.UUID | None
) -> None:
    """Run a queued callback under the tenant bound when it was queued.

    Commit-time callbacks run after the UoW's (and often the request's
    ``TenantScope``) tenant scope closed on the production path, but inside
    it on the test-override path; binding the registration-time tenant makes
    ``current_tenant_id()`` behave identically on both.
    """
    if tenant is None or _current_tenant.get() == tenant:
        await _call(callback)
        return
    with bind_tenant(tenant):
        await _call(callback)


async def on_commit(callback: OnCommitCallback, *, database: str | None = None) -> None:
    """Run ``callback`` only after the current Unit of Work COMMITS.

    For side effects that live OUTSIDE the transaction (a cache write such as
    ``revoke_user_tokens``, a notification) and must not happen if the business
    change rolls back. ``callback`` takes no arguments and may be sync or async
    (use ``functools.partial`` / a closure to bind arguments).

    * Inside a UoW (request ``DbSession``, ``db_session()``, a contract call):
      queued on that UoW's session and run right after its commit — never on
      rollback, and never when it was registered inside a ``begin_nested()``
      savepoint that rolled back. ``database=`` disambiguates when several
      databases are active (same rule as ``publish_event``).
    * Outside any UoW (a script, a test that owns its own transaction): there is
      no commit to wait for, so it runs immediately.

    Callbacks run after the outbox's post-commit dispatch was handed off (on
    the request path the events may still be on their way to a networked
    broker — never order a callback against event delivery), in registration
    order. A failing callback is logged and does NOT fail the already-committed
    request (the change is durable; make the effect retryable if it matters).
    A callback runs under the tenant that was bound when it was queued (the
    UoW's / request's tenant scope has already closed by then).
    """
    _refuse_in_read_only_scope("on_commit")
    session = _resolve_scoped_session(database, op="on_commit")
    if session is None:
        await _call(callback)
        return
    queued = session.info.setdefault(_ON_COMMIT_KEY, [])
    queued.append((callback, _savepoint_token(session), _current_tenant.get()))


#: session.info key holding ``(callback, savepoint_token)`` pairs from
#: before_commit.
_BEFORE_COMMIT_KEY = "sandloom_before_commit"


async def before_commit(
    callback: OnCommitCallback, *, database: str | None = None
) -> None:
    """Run ``callback`` right BEFORE the current Unit of Work commits.

    A last-moment check whose failure must abort the transaction: the callback
    (no arguments, sync or async) runs after the handler finished and before
    ``COMMIT``; if it raises, the UoW ROLLS BACK and the exception propagates
    (a request answers with it — e.g. an ``HTTPException(401)``). Used to
    re-validate an authorization input that lives in ANOTHER service, where no
    row lock can make the check atomic with the commit (ADR-0003 G2):
    the commit-time re-check (spec §4.5) re-reads the caller's subject-state
    record here, after the queued callbacks. It runs BEFORE the commit, not
    atomically with it:
    a change landing between the check and ``COMMIT`` is not seen (the check
    narrows a window to that gap; it does not close it).

    * Inside a UoW (request ``DbSession``, ``db_session()``, a contract
      command): queued on its session, run in registration order before the
      commit; skipped when registered inside a ``begin_nested()`` savepoint
      that rolled back (like :func:`on_commit`). ``database=`` disambiguates
      several active databases.
    * Outside any UoW there is no commit to precede: it runs immediately.

    Keep callbacks short and side-effect free (a read): they run while the
    transaction still holds its locks.
    """
    _refuse_in_read_only_scope("before_commit")
    session = _resolve_scoped_session(database, op="before_commit")
    if session is None:
        await _call(callback)
        return
    queue_before_commit(session, callback)


def queue_before_commit(session: AsyncSession, callback: OnCommitCallback) -> None:
    """Queue ``callback`` on ``session``'s before-commit stage (kernel use).

    :func:`before_commit` for a caller that already holds the UoW session
    (the durable ``idempotency_guard``): no scope resolution, so several
    active databases are never ambiguous.
    """
    queued = session.info.setdefault(_BEFORE_COMMIT_KEY, [])
    queued.append((callback, _savepoint_token(session), _current_tenant.get()))


async def _run_before_commit_callbacks(session: AsyncSession) -> None:
    """Run (and clear) the queued before-commit checks; the first failure
    propagates — the caller then rolls the UoW back instead of committing."""
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return
    queued: list[tuple[OnCommitCallback, Any, _uuid.UUID | None]] = list(
        info.get(_BEFORE_COMMIT_KEY, [])
    )
    info[_BEFORE_COMMIT_KEY] = []
    for callback, token, tenant in queued:
        if not _token_survived(session, token):
            continue
        await _call_in_tenant(callback, tenant)

    scope = info.get(SCOPE_KEY)
    if scope is not None:
        await _commit_recheck(session, info, scope, bool(queued))


def _pending_orm_changes(session: Any) -> bool:
    """The driver's answer (``DatabaseProvider.has_pending_changes``, stashed
    when the unit was bound); no driver answer: no deferred writes."""
    check = session.info.get(_PENDING_CHECK_KEY)
    return bool(check(session)) if check is not None else False


async def _commit_recheck(
    session: Any, info: dict[str, Any], scope: Any, ran_callbacks: bool
) -> None:
    """The commit-time re-check of the request's subject (spec §4.5) and what
    risk triggers before the COMMIT (ADR-0009 items 10-12).

    Runs before EVERY commit of a unit opened in a request: the common read
    unit exits after a handful of dictionary reads (no allocation).
    """
    survivors = _survivors(session, info) if info.get(SELF_REVOKED_KEY) else None
    if survivors is None:
        info.pop(SELF_REVOKED_FINAL_KEY, None)
    policy = scope.policy
    risky = scope.risk is not None or (
        policy is not None and bool(getattr(policy, "risk", None))
    )
    check = scope.check
    if not risky and check is None:
        return
    # ``wrote``: a write statement reached the cursor, or unflushed ORM
    # changes (COMMIT flushes them)
    wrote = bool(info.get(WRITES_KEY)) or _pending_orm_changes(session)
    if risky and info.get(INTENT_UNIT_KEY) is None:
        _risk_before_commit(session, scope, wrote)
    if check is None or not _recheck_due(scope, info, wrote, ran_callbacks):
        return
    if info.get(RECHECKED_KEY) is scope:
        return  # this transaction's commit was already checked
    subject = scope.subject
    if subject is not None and (
        (survivors is not None and subject in survivors)
        or subject in scope.self_revoked
    ):
        return  # self-revocation: "check, then revoke" (ADR-0009 item 3)
    await check()
    info[RECHECKED_KEY] = scope


def _survivors(session: Any, info: dict[str, Any]) -> set[str]:
    """The subjects this unit self-revoked in savepoints that survived, fixed
    right before COMMIT (moved to the request scope when the unit commits)."""
    marks = info.get(SELF_REVOKED_KEY) or ()
    survivors = {subject for subject, token in marks if _token_survived(session, token)}
    info[SELF_REVOKED_FINAL_KEY] = survivors
    return survivors


def _risk_before_commit(session: Any, scope: Any, wrote: bool) -> None:
    """What risk triggers, in the request's own units (not a contract /
    consumer unit's fresh admission scope)."""
    if scope.admission is admission_get():
        _risk.before_commit(session, scope, wrote)


def _recheck_due(
    scope: Any, info: dict[str, Any], wrote: bool, ran_callbacks: bool
) -> bool:
    """Whether the re-check runs before this unit's COMMIT (spec §4.5): every
    unit under ``at_commit()``; never automatic on an operation-intent route
    (``release_check``); a unit committing a write or a queued effect on an
    armed (write) route; a unit that wrote on a read route."""
    if scope.at_commit:
        return True
    if scope.intent is not None:
        return False
    if wrote:
        return True
    return bool(
        scope.armed
        and (ran_callbacks or info.get(_ON_COMMIT_KEY) or info.get(_OUTBOX_PENDING_KEY))
    )


def queued_on_commit(session: Any) -> list[OnCommitCallback]:
    """The ``on_commit`` callbacks ``session``'s unit will run after its
    COMMIT (those of a rolled-back savepoint excluded), in order."""
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return []
    return [
        callback
        for callback, token, _tenant in info.get(_ON_COMMIT_KEY, ())
        if _token_survived(session, token)
    ]


async def _run_on_commit_callbacks(session: AsyncSession) -> None:
    info = getattr(session, "info", None)
    if not isinstance(info, dict):  # a bare test double: nothing was queued
        return
    queued: list[tuple[OnCommitCallback, Any, _uuid.UUID | None]] = list(
        info.get(_ON_COMMIT_KEY, [])
    )
    if not queued:
        return
    info[_ON_COMMIT_KEY] = []
    for callback, token, tenant in queued:
        if not _token_survived(session, token):
            continue
        try:
            await _call_in_tenant(callback, tenant)
        except Exception:  # the transaction already committed
            logger.error(
                "on_commit callback %r failed after commit",
                getattr(callback, "__qualname__", callback),
                exc_info=True,
            )
