"""Identity module background tasks.

Declared with ``@task`` / ``@periodic`` of ``sandloom.sdk.tasks``: the
framework worker (``sandloom worker``) imports ``<module>.tasks`` for every
loaded module and runs what it declares; the scheduler (``sandloom beat``)
sends the ``@periodic`` ones at their cron slots (UTC). This module never builds
a broker app itself. The bodies are ``async def``: the worker runs them on its
one persistent loop, and the engine pools are loop-bound.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from sandloom.sdk.data import open_uow
from sandloom.sdk.tasks import periodic, task
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


#: Revoked refresh tokens are purged after this many days.
REVOKED_RETENTION_DAYS = 30


async def purge_expired_refresh_tokens() -> int:
    """Delete expired refresh tokens in batches; returns how many."""
    return await _purge_in_batches(
        lambda sess: TokenService.cleanup_expired_tokens(sess, limit=CLEANUP_BATCH_SIZE)
    )


async def purge_revoked_refresh_tokens(days_old: int = REVOKED_RETENTION_DAYS) -> int:
    """Delete refresh tokens revoked more than ``days_old`` days ago, in
    batches; returns how many."""
    return await _purge_in_batches(
        lambda sess: TokenService.cleanup_revoked_tokens_by_age(
            sess, days_old, limit=CLEANUP_BATCH_SIZE
        )
    )


@periodic(cron="0 * * * *", id="identity.cleanup-expired-refresh-tokens")
async def cleanup_expired_refresh_tokens() -> None:
    """Hourly: clean up expired refresh tokens."""
    await purge_expired_refresh_tokens()


@periodic(cron="0 3 * * *", id="identity.cleanup-revoked-refresh-tokens")
async def cleanup_revoked_refresh_tokens() -> None:
    """Daily at 03:00 UTC: clean up old revoked refresh tokens."""
    await purge_revoked_refresh_tokens()


# The two tasks below are placeholders: they stay importable, and beat does not
# schedule jobs that perform no verification or security work.
@task
async def send_email_verification_reminder() -> None:
    """Send reminder to users who haven't verified their email (placeholder).

    The implementation would depend on the specific email service used."""


@task
async def audit_user_activity() -> None:
    """Audit user activity for security monitoring (placeholder).

    The implementation would analyse login patterns and suspicious activity."""
