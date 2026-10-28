"""Identity module background tasks.

Registered on the FRAMEWORK worker (``celery -A sandloom.core.worker worker``): the
worker imports ``<module>.tasks`` for every loaded module, and ``@shared_task``
binds these functions to whatever app the worker resolved from the taskqueue
capability — this module never builds (or imports) a Celery app itself.
Async work runs through ``run_async`` (one persistent loop per worker
process), never ``asyncio.run`` — the engine pools are loop-bound.
``setup_periodic_tasks`` is the beat hook the worker connects to
``on_after_finalize``.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sandloom.sdk.data import open_uow
from sandloom.sdk.effects import run_async
from sandloom.sdk.tasks import shared_task
from sandloom_identity.services.token_service import TokenService

#: Rows per DELETE: each batch is its own short Unit of Work, so a large
#: backlog never becomes one long transaction (WAL burst, vacuum held back).
CLEANUP_BATCH_SIZE = 5000


async def _purge_in_batches(purge: Callable[[Any], Awaitable[int]]) -> int:
    total = 0
    while True:
        async with open_uow() as sess:
            deleted = await purge(sess)
        total += deleted
        if deleted < CLEANUP_BATCH_SIZE:
            return total


@shared_task(name="sandloom_identity.tasks.cleanup_expired_refresh_tokens")
def cleanup_expired_refresh_tokens() -> dict[str, Any]:
    """Clean up expired refresh tokens."""

    async def _run_cleanup() -> int:
        return await _purge_in_batches(
            lambda sess: TokenService.cleanup_expired_tokens(
                sess, limit=CLEANUP_BATCH_SIZE
            )
        )

    deleted_count = run_async(_run_cleanup())

    return {
        "task": "cleanup_expired_refresh_tokens",
        "deleted_count": deleted_count,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@shared_task(name="sandloom_identity.tasks.cleanup_revoked_refresh_tokens")
def cleanup_revoked_refresh_tokens(days_old: int = 30) -> dict[str, Any]:
    """Clean up old revoked refresh tokens."""

    async def _run_cleanup() -> int:
        return await _purge_in_batches(
            lambda sess: TokenService.cleanup_revoked_tokens_by_age(
                sess, days_old, limit=CLEANUP_BATCH_SIZE
            )
        )

    deleted_count = run_async(_run_cleanup())

    return {
        "task": "cleanup_revoked_refresh_tokens",
        "days_old": days_old,
        "deleted_count": deleted_count,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@shared_task(name="sandloom_identity.tasks.send_email_verification_reminder")
def send_email_verification_reminder() -> dict[str, Any]:
    """Send reminder to users who haven't verified their email."""
    # This is a placeholder for email reminder functionality
    # Implementation would depend on the specific email service used

    return {
        "task": "send_email_verification_reminder",
        "status": "placeholder",
        "timestamp": datetime.now(UTC).isoformat(),
    }


@shared_task(name="sandloom_identity.tasks.audit_user_activity")
def audit_user_activity() -> dict[str, Any]:
    """Audit user activity for security monitoring."""
    # This is a placeholder for security auditing functionality
    # Implementation would involve analyzing login patterns, suspicious activities, etc.

    return {
        "task": "audit_user_activity",
        "status": "placeholder",
        "timestamp": datetime.now(UTC).isoformat(),
    }


def setup_periodic_tasks(sender: Any, **kwargs: Any) -> None:
    """Register identity's periodic tasks on the worker app (beat hook)."""
    # Clean up expired refresh tokens every hour
    sender.add_periodic_task(
        3600.0,  # Every hour
        cleanup_expired_refresh_tokens.s(),
        name="cleanup_expired_refresh_tokens_hourly",
    )

    # Clean up old revoked tokens daily
    sender.add_periodic_task(
        86400.0,  # 24 hours
        cleanup_revoked_refresh_tokens.s(),
        name="cleanup_revoked_refresh_tokens_daily",
    )

    # Placeholder task names remain importable for compatibility, but beat must
    # not schedule jobs that perform no verification or security work.
