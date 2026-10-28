"""Reconciliation of operation intents (#72, ADR-0003 open items 7, 13, 15).

The request path leaves two kinds of loose ends that no request will ever
close:

- a ``pending`` intent whose PSP answer was lost (timeout / 5xx / crash) and
  whose client never retried — the PSP session may exist (payable by whoever
  holds its URL, which billing never returned);
- a ``revoked`` intent whose compensation (expiring its checkout session)
  failed — the refusal stands, but the session stays payable.

The periodic reconciler (``billing.tasks.reconcile_operation_intents``) looks
the PSP session up by the intent marker the session carries, expires it and
closes the intent ONLY through the existing ``pending``-guarded transitions;
an ``issued`` intent is never touched. Every PSP failure is bounded: attempts
and the last error are persisted, and after the last attempt the intent is
flagged ``followup_at`` for an operator.
"""

import asyncio
import contextvars
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom_billing.models import (
    INTENT_FAILED,
    INTENT_ISSUED,
    INTENT_PENDING,
    INTENT_REVOKED,
    OperationIntent,
)
from sandloom_billing.psp import StripeApiError
from sandloom_billing.services.intent_reconciler import IntentReconciler
from sandloom_billing.tests.conftest import make_user_headers
from sandloom_billing.tests.test_operation_intents import (
    CHECKOUT,
    _audits,
    _demote,
    _idem,
    _intents,
    _org,
)

pytestmark = pytest.mark.asyncio

STALE = timedelta(minutes=30)


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


def _later(minutes: int = 31) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


async def _reconcile(psp: Any, *, minutes: int = 31, max_attempts: int = 3) -> Any:
    return await IntentReconciler.run(
        psp, now=_later(minutes), stale_after=STALE, max_attempts=max_attempts
    )


def _lose_the_psp_answer(
    monkeypatch: pytest.MonkeyPatch, psp: Any, *, create: bool = True
) -> None:
    """The PSP call is ambiguous (503 to the client): the session is created
    (``create=True``) or not, and the answer never reaches billing."""
    original = psp.create_checkout_session

    async def lost(**kwargs: Any) -> Any:
        if create:
            await original(**kwargs)
        raise TimeoutError("PSP answer lost")

    monkeypatch.setattr(psp, "create_checkout_session", lost)


async def _abandoned_pending(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
    *,
    create: bool = True,
) -> Any:
    _, headers = make_user_headers()
    _lose_the_psp_answer(monkeypatch, fake_psp, create=create)
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 503, resp.text
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_PENDING
    return intent


# --------------------------------------------------------------------------- #
# (a) stale pending intents
# --------------------------------------------------------------------------- #


async def test_stale_pending_intent_session_is_found_by_marker_and_expired(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """#72: the client never retried. The reconciler finds the PSP session by
    the intent marker (billing never learned its id), expires it and revokes
    the intent (audited)."""
    intent = await _abandoned_pending(
        async_client, catalog, fake_psp, monkeypatch, sessions
    )
    [session] = fake_psp.checkout_sessions

    report = await _reconcile(fake_psp)

    [closed] = await _intents(sessions)
    assert closed.id == intent.id and closed.status == INTENT_REVOKED
    assert closed.psp_session_id == session["id"]
    assert closed.psp_expired_at is not None
    assert fake_psp.expired_sessions == [session["id"]]
    assert await _audits(sessions, "billing.intent.revoked") == 1
    assert report.reconciled == 1 and report.still_failing == 0


async def test_fresh_pending_intent_is_left_alone(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Younger than the threshold: its request (or retry) may still be
    running — the reconciler neither calls the PSP nor touches it."""
    await _abandoned_pending(async_client, catalog, fake_psp, monkeypatch, sessions)

    async def must_not_be_called(**_: Any) -> Any:
        raise AssertionError("no PSP lookup for a fresh intent")

    monkeypatch.setattr(fake_psp, "list_checkout_sessions", must_not_be_called)
    report = await _reconcile(fake_psp, minutes=1)

    [intent] = await _intents(sessions)
    assert intent.status == INTENT_PENDING
    assert report.reconciled == 0


async def test_stale_pending_intent_without_a_psp_session_fails(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The PSP never created the session: nothing to expire, the intent is
    ``failed`` (frees its request key, the gate refuses it)."""
    await _abandoned_pending(
        async_client, catalog, fake_psp, monkeypatch, sessions, create=False
    )

    report = await _reconcile(fake_psp)

    [intent] = await _intents(sessions)
    assert intent.status == INTENT_FAILED
    assert fake_psp.expired_sessions == []
    assert report.reconciled == 1


async def test_stale_pending_intent_paid_meanwhile_is_flagged_for_an_operator(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The session was completed although its URL was never handed out: the
    intent is revoked (the webhook gate refuses the completion) and flagged
    for an operator (refund is manual)."""
    await _abandoned_pending(async_client, catalog, fake_psp, monkeypatch, sessions)
    fake_psp.checkout_sessions[0]["status"] = "complete"

    await _reconcile(fake_psp)

    [intent] = await _intents(sessions)
    assert intent.status == INTENT_REVOKED
    assert intent.followup_at is not None
    assert fake_psp.expired_sessions == []


async def test_reconciler_never_touches_an_issued_intent(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, headers = make_user_headers()
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200, resp.text

    async def must_not_be_called(*_: Any, **__: Any) -> Any:
        raise AssertionError("an issued intent is never reconciled")

    monkeypatch.setattr(fake_psp, "list_checkout_sessions", must_not_be_called)
    monkeypatch.setattr(fake_psp, "expire_checkout_session", must_not_be_called)
    await _reconcile(fake_psp, minutes=60 * 48)

    [intent] = await _intents(sessions)
    assert intent.status == INTENT_ISSUED and intent.followup_at is None


async def test_psp_lookup_outage_is_bounded_then_left_for_an_operator(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """An ambiguous PSP failure leaves the intent pending, counts the attempt
    and keeps the last error; after the last attempt it is flagged
    ``followup_at`` and no longer retried."""
    await _abandoned_pending(async_client, catalog, fake_psp, monkeypatch, sessions)
    lookups: list[int] = []

    async def down(**_: Any) -> Any:
        lookups.append(1)
        raise StripeApiError("Stripe 5xx: 503")

    monkeypatch.setattr(fake_psp, "list_checkout_sessions", down)
    first = await _reconcile(fake_psp, max_attempts=2)
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_PENDING
    assert intent.reconcile_attempts == 1 and "503" in (intent.reconcile_error or "")
    assert intent.followup_at is None
    assert first.still_failing == 1 and first.escalated == 0

    second = await _reconcile(fake_psp, max_attempts=2)
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_PENDING and intent.reconcile_attempts == 2
    assert intent.followup_at is not None
    assert second.escalated == 1

    await _reconcile(fake_psp, max_attempts=2)
    assert len(lookups) == 2  # exhausted: never retried again


# --------------------------------------------------------------------------- #
# (b) revoked intents whose compensation failed
# --------------------------------------------------------------------------- #


async def _revoked_with_failed_expire(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> Any:
    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    create, expire = fake_psp.create_checkout_session, fake_psp.expire_checkout_session

    async def demoted_meanwhile(**kwargs: Any) -> Any:
        # another actor, concurrently: its own context (spec §4.6)
        await asyncio.create_task(
            _demote(async_client, test_db, org_id, admin_id),
            context=contextvars.Context(),
        )
        return await create(**kwargs)

    async def psp_down(session_id: str) -> None:
        raise ConnectionError("PSP unreachable")

    monkeypatch.setattr(fake_psp, "create_checkout_session", demoted_meanwhile)
    monkeypatch.setattr(fake_psp, "expire_checkout_session", psp_down)
    resp = await async_client.post(
        CHECKOUT,
        json={"org_id": str(org_id), "price_id": catalog["pro_month"].id},
        headers=_idem(admin_headers),
    )
    assert resp.status_code == 403, resp.text
    monkeypatch.setattr(fake_psp, "expire_checkout_session", expire)
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_REVOKED
    return intent


async def test_failed_compensation_is_persisted_and_retried(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    intent = await _revoked_with_failed_expire(
        async_client, test_db, catalog, fake_psp, monkeypatch, sessions
    )
    assert intent.reconcile_attempts == 1
    assert "ConnectionError" in (intent.reconcile_error or "")
    assert intent.psp_expired_at is None
    [session] = fake_psp.checkout_sessions

    report = await _reconcile(fake_psp, minutes=0)

    [retried] = await _intents(sessions)
    assert retried.status == INTENT_REVOKED
    assert retried.psp_expired_at is not None
    assert fake_psp.expired_sessions == [session["id"]]
    assert report.compensated == 1


async def test_failed_compensation_retry_is_bounded(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _revoked_with_failed_expire(
        async_client, test_db, catalog, fake_psp, monkeypatch, sessions
    )

    async def still_down(session_id: str) -> None:
        raise StripeApiError("Stripe unreachable after retries")

    monkeypatch.setattr(fake_psp, "expire_checkout_session", still_down)
    report = await _reconcile(fake_psp, minutes=0, max_attempts=2)

    [intent] = await _intents(sessions)
    assert intent.reconcile_attempts == 2
    assert intent.followup_at is not None and intent.psp_expired_at is None
    assert report.escalated == 1
    again = await _reconcile(fake_psp, minutes=0, max_attempts=2)
    assert again.compensated == again.still_failing == again.escalated == 0


async def test_successful_compensation_is_recorded_and_never_retried(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom_billing.tests.test_operation_intents import _revoked_checkout

    intent = await _revoked_checkout(
        async_client,
        test_db,
        catalog["pro_month"].id,
        fake_psp,
        monkeypatch,
        sessions,
    )
    assert intent.psp_expired_at is not None and intent.reconcile_attempts == 0

    report = await _reconcile(fake_psp, minutes=0)
    assert report.compensated == 0
    assert len(fake_psp.expired_sessions) == 1


# --------------------------------------------------------------------------- #
# ADR-0003 open item 15: ambiguous PSP failure + authority loss
# --------------------------------------------------------------------------- #


async def test_ambiguous_psp_failure_then_authority_loss_is_reconciled(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The PSP call is ambiguous (503: the session exists, its id unknown),
    then the admin is demoted, so the retry is refused before it reaches the
    intent (revoked token 401 / authorize 403). The reconciler finds the session by the intent
    marker, expires it and revokes the intent."""
    org_id, (admin_id, admin_headers) = await _org(async_client, test_db)
    _lose_the_psp_answer(monkeypatch, fake_psp)
    body = {"org_id": str(org_id), "price_id": catalog["pro_month"].id}
    request_headers = _idem(admin_headers, "ambiguous-then-demoted")
    first = await async_client.post(CHECKOUT, json=body, headers=request_headers)
    assert first.status_code == 503, first.text

    await _demote(async_client, test_db, org_id, admin_id)
    retry = await async_client.post(CHECKOUT, json=body, headers=request_headers)
    # The demotion also revoked the admin's tokens: 401 (or 403 at authorize).
    assert retry.status_code in (401, 403), retry.text
    [pending] = await _intents(sessions)
    assert pending.status == INTENT_PENDING
    [session] = fake_psp.checkout_sessions
    assert fake_psp.expired_sessions == []

    await _reconcile(fake_psp)

    [intent] = await _intents(sessions)
    assert intent.status == INTENT_REVOKED
    assert fake_psp.expired_sessions == [session["id"]]


# --------------------------------------------------------------------------- #
# ADR-0003 open item 13: an issued checkout whose session expired
# --------------------------------------------------------------------------- #


async def test_issued_intent_stores_the_session_expiry(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, headers = make_user_headers()
    resp = await async_client.post(
        CHECKOUT, json={"price_id": catalog["pro_month"].id}, headers=_idem(headers)
    )
    assert resp.status_code == 200, resp.text
    [intent] = await _intents(sessions)
    [session] = fake_psp.checkout_sessions
    assert intent.psp_session_expires_at is not None
    assert int(intent.psp_session_expires_at.timestamp()) == session["expires_at"]


async def test_retry_of_an_issued_checkout_after_its_session_expired_is_410(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
    sandloom_cache: Any,
) -> None:
    """A late retry must not get a dead URL: ``410`` (start a new checkout
    with a new key), no URL, no PSP call, the intent unchanged."""
    _, headers = make_user_headers()
    body = {"price_id": catalog["pro_month"].id}
    resp = await async_client.post(
        CHECKOUT, json=body, headers=_idem(headers, "late-retry")
    )
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    [intent] = await _intents(sessions)
    async with sessions() as db:
        await db.execute(
            update(OperationIntent)
            .where(OperationIntent.id == intent.id)
            .values(psp_session_expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await db.commit()
    client = await sandloom_cache.raw_client()
    await client.flushdb()

    retry = await async_client.post(
        CHECKOUT, json=body, headers=_idem(headers, "late-retry")
    )

    assert retry.status_code == 410, retry.text
    assert url not in retry.text
    [unchanged] = await _intents(sessions)
    assert unchanged.status == INTENT_ISSUED
    assert len(fake_psp.checkout_sessions) == 1


async def test_retry_of_a_legacy_issued_checkout_uses_the_psp_default_lifetime(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    sessions: async_sessionmaker[AsyncSession],
    sandloom_cache: Any,
) -> None:
    """An intent issued before the expiry was stored (NULL) counts as expired
    24 h (Stripe's default session lifetime) after it was created."""
    _, headers = make_user_headers()
    body = {"price_id": catalog["pro_month"].id}
    resp = await async_client.post(
        CHECKOUT, json=body, headers=_idem(headers, "legacy-retry")
    )
    assert resp.status_code == 200, resp.text
    async with sessions() as db:
        await db.execute(
            text(
                "UPDATE billing_operation_intents SET psp_session_expires_at = NULL, "
                "created_at = now() - interval '25 hours'"
            )
        )
        await db.commit()
    client = await sandloom_cache.raw_client()
    await client.flushdb()

    retry = await async_client.post(
        CHECKOUT, json=body, headers=_idem(headers, "legacy-retry")
    )
    assert retry.status_code == 410, retry.text


# --------------------------------------------------------------------------- #
# The periodic task
# --------------------------------------------------------------------------- #


def test_reconcile_task_is_scheduled_and_acks_late() -> None:
    from sandloom_billing import tasks

    scheduled: list[tuple[float, Any, str]] = []

    class _Sender:
        def add_periodic_task(self, interval: float, sig: Any, name: str) -> None:
            scheduled.append((interval, sig, name))

    tasks.setup_periodic_tasks(_Sender())

    [(interval, sig, _name)] = scheduled
    assert sig.task == "sandloom_billing.tasks.reconcile_operation_intents"
    assert 0 < interval <= 15 * 60
    task = tasks.reconcile_operation_intents
    assert task.acks_late is True and task.reject_on_worker_lost is True


async def test_reconcile_task_body_reports_counts(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The task body (what ``run_async`` drives in the worker) uses billing's
    PSP client and the settings' threshold."""
    from sandloom.sdk.infra import settings
    from sandloom_billing import tasks

    await _abandoned_pending(async_client, catalog, fake_psp, monkeypatch, sessions)
    monkeypatch.setattr(settings, "billing_intent_reconcile_after_minutes", 30)
    async with sessions() as db:
        await db.execute(
            text(
                "UPDATE billing_operation_intents "
                "SET created_at = now() - interval '31 minutes'"
            )
        )
        await db.commit()

    result = await tasks.reconcile_intents_once(fake_psp)

    assert result["reconciled"] == 1
    [intent] = await _intents(sessions)
    assert intent.status == INTENT_REVOKED


async def test_reconciler_scopes_every_write_to_the_intents_customer(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A PSP session carrying this intent's marker but listed for ANOTHER PSP
    customer is never acted on (the lookup is by the intent's own customer)."""
    await _abandoned_pending(async_client, catalog, fake_psp, monkeypatch, sessions)
    fake_psp.checkout_sessions[0]["customer"] = f"cus_other_{uuid.uuid4().hex[:6]}"

    await _reconcile(fake_psp)

    [intent] = await _intents(sessions)
    assert intent.status == INTENT_FAILED  # no session of ITS customer
    assert fake_psp.expired_sessions == []


async def test_late_finalize_after_the_reconciler_failed_the_intent_expires(
    async_client: AsyncClient,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A request stuck past the threshold: the reconciler saw no session and
    failed the intent; the request's PSP call then created one. Its finalize
    cannot issue (``WHERE status='pending'``) and expires that session — it
    is not issued anywhere."""
    from starlette.requests import Request

    from sandloom_billing.services.operation_intents import (
        OperationIntentService,
        _Reserved,
    )
    from sandloom_billing.services.tenant_resolver import TenantContext

    await _abandoned_pending(
        async_client, catalog, fake_psp, monkeypatch, sessions, create=False
    )
    await _reconcile(fake_psp)
    [failed] = await _intents(sessions)
    assert failed.status == INTENT_FAILED
    monkeypatch.undo()
    late = await fake_psp.create_checkout_session(
        customer_id="cus_x",
        price_external_id="price_pro_month",
        success_url="https://s",
        cancel_url="https://c",
        idempotency_key=failed.psp_idempotency_key,
    )
    reserved = _Reserved(
        intent_id=failed.id,
        customer_id=failed.customer_id,
        tenant_type=failed.tenant_type,
        tenant_id=failed.tenant_id,
        external_customer=None,
        plan=None,
    )
    ctx = TenantContext(
        tenant_type="user", tenant_id=failed.actor_id, user_id=failed.actor_id
    )
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await OperationIntentService._finalize(
            None, request, ctx, reserved, fake_psp, session=late
        )

    assert exc.value.status_code == 409
    assert fake_psp.expired_sessions == [late.id]
    [after] = await _intents(sessions)
    assert after.status == INTENT_FAILED


async def test_concurrent_successful_expire_is_not_counted_as_a_failure(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Another run expired the session (``psp_expired_at`` set) while this
    run's expire failed: the failure is not recorded, nothing is escalated."""
    from sandloom_billing.services.operation_intents import record_expire_outcome

    intent = await _revoked_with_failed_expire(
        async_client, test_db, catalog, fake_psp, monkeypatch, sessions
    )

    async def lost_the_race(session_id: str) -> None:
        await record_expire_outcome(intent.id, intent.customer_id, error=None)
        raise StripeApiError("already being expired")

    monkeypatch.setattr(fake_psp, "expire_checkout_session", lost_the_race)
    report = await _reconcile(fake_psp, minutes=0, max_attempts=2)

    [after] = await _intents(sessions)
    assert after.psp_expired_at is not None
    assert after.reconcile_attempts == 1 and after.followup_at is None
    assert report.escalated == 0 and report.still_failing == 0


async def test_a_failure_recorded_after_a_success_is_ignored(
    async_client: AsyncClient,
    test_db: AsyncSession,
    catalog: dict[str, Any],
    fake_psp: Any,
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom_billing.services.operation_intents import record_expire_outcome

    intent = await _revoked_with_failed_expire(
        async_client, test_db, catalog, fake_psp, monkeypatch, sessions
    )
    await record_expire_outcome(intent.id, intent.customer_id, error=None)
    await record_expire_outcome(intent.id, intent.customer_id, error="late failure")

    [after] = await _intents(sessions)
    assert after.psp_expired_at is not None
    assert after.reconcile_attempts == 1 and after.reconcile_error is None


def test_billing_migrations_autogenerate_scope_covers_every_billing_table() -> None:
    """``migrations/env.py`` limits autogenerate to ``table_names``: every
    billing model table must be listed, or a revision would miss it."""
    import ast
    from pathlib import Path

    import sandloom_billing.models  # registers the tables
    from sandloom.sdk import Base

    env = Path(sandloom_billing.models.__file__).parents[1] / "migrations" / "env.py"
    listed = {
        node.value
        for node in ast.walk(ast.parse(env.read_text()))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("billing_")
    }
    models = {t for t in Base.metadata.tables if t.startswith("billing_")}
    assert models <= listed, sorted(models - listed)
