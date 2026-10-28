from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sandloom_identity.tasks import (
    CLEANUP_BATCH_SIZE,
    audit_user_activity,
    cleanup_expired_refresh_tokens,
    cleanup_revoked_refresh_tokens,
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
def test_cleanup_expired_refresh_tokens_task_calls_service(
    mock_cleanup_service: AsyncMock,
):
    """
    Tests that the Celery task calls the correct service method and returns
    the correct format.

    Mocks only dependencies, without testing the business logic itself.
    """
    mock_cleanup_service.return_value = (
        5  # Simulate the service having deleted 5 tokens
    )

    result = cleanup_expired_refresh_tokens()

    # 1. Verify the service was called
    mock_cleanup_service.assert_awaited_once()

    # 2. Verify the format of the return value
    assert isinstance(result, dict)
    assert result["task"] == "cleanup_expired_refresh_tokens"
    assert result["deleted_count"] == 5
    assert "timestamp" in result


@patch("sandloom_identity.tasks.open_uow", new=_fake_db_session)
@patch(
    "sandloom_identity.tasks.TokenService.cleanup_revoked_tokens_by_age",
    new_callable=AsyncMock,
)
def test_cleanup_revoked_refresh_tokens_with_params(
    mock_cleanup_service: AsyncMock,
):
    """Tests that the Celery task passes parameters to the service."""
    mock_cleanup_service.return_value = 3

    result = cleanup_revoked_refresh_tokens(days_old=7)

    # 1. Verify the service was called with the correct arguments
    mock_cleanup_service.assert_awaited_once_with(_SESSION, 7, limit=CLEANUP_BATCH_SIZE)

    # 2. Verify the result
    assert result["deleted_count"] == 3
    assert result["days_old"] == 7


def test_placeholder_tasks_format():
    """Tests that placeholder tasks return the expected format."""
    tasks_to_test = [send_email_verification_reminder, audit_user_activity]

    for task_func in tasks_to_test:
        result = task_func()

        assert isinstance(result, dict)
        assert "task" in result
        assert "status" in result
        assert "timestamp" in result
        assert result["status"] == "placeholder"

        # Verify the timestamp has the correct format
        datetime.fromisoformat(result["timestamp"])


def test_setup_periodic_tasks_configures_schedule() -> None:
    """The beat hook registers every periodic task on the SENDER (the worker
    app the framework worker resolved) — identity owns no Celery app."""
    from sandloom_identity.tasks import setup_periodic_tasks

    mock_sender = MagicMock()
    setup_periodic_tasks(mock_sender)

    assert mock_sender.add_periodic_task.call_count == 2
    names = {c.kwargs["name"] for c in mock_sender.add_periodic_task.call_args_list}
    assert names == {
        "cleanup_expired_refresh_tokens_hourly",
        "cleanup_revoked_refresh_tokens_daily",
    }
