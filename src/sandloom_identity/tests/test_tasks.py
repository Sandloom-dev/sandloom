from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sandloom_identity.tasks import (
    CLEANUP_BATCH_SIZE,
    REVOKED_RETENTION_DAYS,
    audit_user_activity,
    cleanup_expired_refresh_tokens,
    cleanup_revoked_refresh_tokens,
    purge_revoked_refresh_tokens,
    send_email_verification_reminder,
)

_SESSION = MagicMock(name="uow_session")


@asynccontextmanager
async def _fake_db_session(name: str = "default"):
    """Stand-in for the unit (``open_uow()``) the tasks open."""
    yield _SESSION


@patch("sandloom_identity.tasks.open_uow", new=_fake_db_session)
@patch(
    "sandloom_identity.tasks.TokenService.cleanup_expired_tokens",
    new_callable=AsyncMock,
)
async def test_cleanup_expired_refresh_tokens_task_calls_service(
    mock_cleanup_service: AsyncMock,
):
    """The task runs the service in batches (what the worker drives).

    Mocks only dependencies, without testing the business logic itself.
    """
    mock_cleanup_service.return_value = 5  # fewer than a batch: one pass

    assert await cleanup_expired_refresh_tokens() is None

    mock_cleanup_service.assert_awaited_once_with(_SESSION, limit=CLEANUP_BATCH_SIZE)


@patch("sandloom_identity.tasks.open_uow", new=_fake_db_session)
@patch(
    "sandloom_identity.tasks.TokenService.cleanup_revoked_tokens_by_age",
    new_callable=AsyncMock,
)
async def test_cleanup_revoked_refresh_tokens_with_params(
    mock_cleanup_service: AsyncMock,
):
    """The purge passes the retention to the service; the task uses the default."""
    mock_cleanup_service.return_value = 3

    assert await purge_revoked_refresh_tokens(days_old=7) == 3
    mock_cleanup_service.assert_awaited_once_with(_SESSION, 7, limit=CLEANUP_BATCH_SIZE)

    mock_cleanup_service.reset_mock()
    await cleanup_revoked_refresh_tokens()
    mock_cleanup_service.assert_awaited_once_with(
        _SESSION, REVOKED_RETENTION_DAYS, limit=CLEANUP_BATCH_SIZE
    )


async def test_placeholder_tasks_run_and_do_nothing():
    assert await send_email_verification_reminder() is None
    assert await audit_user_activity() is None


def test_the_cleanups_are_periodic_and_the_placeholders_are_not() -> None:
    """Every interval maps to cron: hourly, and daily in a fixed slot."""
    expired, revoked = cleanup_expired_refresh_tokens, cleanup_revoked_refresh_tokens
    assert (expired.id, expired.cron) == (
        "identity.cleanup-expired-refresh-tokens",
        "0 * * * *",
    )
    assert (revoked.id, revoked.cron) == (
        "identity.cleanup-revoked-refresh-tokens",
        "0 3 * * *",
    )
    assert cleanup_expired_refresh_tokens.max_interval == timedelta(hours=1)
    assert cleanup_revoked_refresh_tokens.max_interval == timedelta(days=1)
    for placeholder in (send_email_verification_reminder, audit_user_activity):
        assert placeholder.name.startswith("sandloom_identity.tasks.")
        assert not hasattr(placeholder, "cron")
