"""Billing background tasks (framework worker: ``sandloom worker``).

``reconcile_operation_intents`` (#72, ADR-0003 open items 7 and 15) closes the
loose ends of the class-F operation intents that no request will close: stale
``pending`` intents (PSP answer lost, client never retried) and ``revoked``
intents whose expire compensation failed — see
:mod:`sandloom_billing.services.intent_reconciler`. The same run purges refund /
dispute payment states that never matched a credit grant after their
retention (``PAYMENT_STATE_RETENTION``, ADR-0003 item 12).

It is idempotent (every write is status- and owner-guarded, the PSP expire is
idempotent), so it is ``at_least_once``: a worker lost mid-run redelivers it
and the re-run finds the finished work done. Its schedule is the cron of
``@periodic``; the ``billing_reconciler`` health check derives its tolerance
from that declaration (``reconcile_operation_intents.max_interval``).
"""

from __future__ import annotations

import logging
from typing import Any

from sandloom.sdk.data import open_uow
from sandloom.sdk.tasks import periodic
from sandloom_billing.psp import active_psp_client
from sandloom_billing.services.intent_reconciler import IntentReconciler
from sandloom_billing.services.reconciler_health import record_reconciler_run
from sandloom_billing.services.webhook_service import purge_orphan_payment_states

logger = logging.getLogger("sandloom_billing.tasks")


async def reconcile_intents_once(psp: Any | None = None) -> dict[str, int]:
    """One reconciliation run with billing's PSP client; returns the counts.

    Its last unit records the completed run (the ``billing_reconciler``
    health check's input): a run that raises records nothing.
    """
    report = await IntentReconciler.run(psp if psp is not None else active_psp_client())
    counts = report.as_dict()
    # Housekeeping: refund / dispute states that never matched a grant.
    async with open_uow() as db:
        counts["purged_payment_states"] = await purge_orphan_payment_states(db)
        await record_reconciler_run(db)
    if report.still_failing or report.escalated:
        logger.warning("billing intent reconcile: %s", counts)
    elif report.reconciled or report.compensated:
        logger.info("billing intent reconcile: %s", counts)
    return counts


@periodic(
    cron="*/5 * * * *", id="billing.reconcile-operation-intents", at_least_once=True
)
async def reconcile_operation_intents() -> None:
    """Periodic: reconcile stale / uncompensated operation intents."""
    await reconcile_intents_once()
