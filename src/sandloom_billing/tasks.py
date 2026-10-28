"""Billing background tasks (framework worker: ``celery -A sandloom.core.worker``).

``reconcile_operation_intents`` (#72, ADR-0003 open items 7 and 15) closes the
loose ends of the class-F operation intents that no request will close: stale
``pending`` intents (PSP answer lost, client never retried) and ``revoked``
intents whose expire compensation failed — see
:mod:`sandloom_billing.services.intent_reconciler`. The same run purges refund /
dispute payment states that never matched a credit grant after their
retention (``PAYMENT_STATE_RETENTION``, ADR-0003 item 12).

It is idempotent (every write is status- and owner-guarded, the PSP expire is
idempotent), so it runs ``acks_late`` + ``reject_on_worker_lost``: a worker
lost mid-run redelivers it and the re-run finds the finished work done.
Async work runs through ``run_async`` (the worker's persistent loop), never
``asyncio.run``.
"""

from __future__ import annotations

import logging
from typing import Any

from sandloom.sdk.data import open_uow
from sandloom.sdk.effects import run_async
from sandloom.sdk.tasks import shared_task
from sandloom_billing.psp import active_psp_client
from sandloom_billing.services.intent_reconciler import IntentReconciler
from sandloom_billing.services.webhook_service import purge_orphan_payment_states

logger = logging.getLogger("sandloom_billing.tasks")

#: How often beat runs the reconciler (seconds). The staleness threshold is
#: ``BILLING_INTENT_RECONCILE_AFTER_MINUTES``; this only bounds the delay.
RECONCILE_INTERVAL_SECONDS = 300.0


async def reconcile_intents_once(psp: Any | None = None) -> dict[str, int]:
    """One reconciliation run with billing's PSP client; returns the counts."""
    report = await IntentReconciler.run(psp if psp is not None else active_psp_client())
    counts = report.as_dict()
    # Housekeeping: refund / dispute states that never matched a grant.
    async with open_uow() as db:
        counts["purged_payment_states"] = await purge_orphan_payment_states(db)
    if report.still_failing or report.escalated:
        logger.warning("billing intent reconcile: %s", counts)
    elif report.reconciled or report.compensated:
        logger.info("billing intent reconcile: %s", counts)
    return counts


@shared_task(
    name="sandloom_billing.tasks.reconcile_operation_intents",
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_operation_intents() -> dict[str, int]:
    """Periodic: reconcile stale / uncompensated operation intents."""
    return run_async(reconcile_intents_once())


def setup_periodic_tasks(sender: Any, **kwargs: Any) -> None:
    """Register billing's periodic tasks on the worker app (beat hook)."""
    sender.add_periodic_task(
        RECONCILE_INTERVAL_SECONDS,
        reconcile_operation_intents.s(),
        name="billing_reconcile_operation_intents",
    )
