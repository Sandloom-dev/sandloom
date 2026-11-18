"""A dev app without a frontend can finish the e-mailed steps through the API.

With ``NOTIFIER_LOG_LINKS`` the log notifier prints the e-mail's links, which
point at the frontend (``FRONTEND_URL``). Identity prints the same step as the
API request that completes it, marked ``[dev only]``, once the change commits.
It prints only when the mail goes through the log notifier: with another driver
or a custom sender a real mail goes out and the token stays off stderr.
Without the flag nothing is printed, and no flag verifies an account by itself.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.infra import notifier, settings
from sandloom.sdk.testing import run_on_commit
from sandloom_identity.api.auth import auth_router
from sandloom_identity.integrations import EmailSender
from sandloom_identity.schemas.auth import RegisterIn
from sandloom_identity.services import auth_service as auth_mod
from sandloom_identity.services.auth_service import AuthService

PASSWORD = "TestPassword123!"
_STEP = re.compile(r"\[dev only\] no frontend\? POST (\S+) (\{.*\})")


@pytest.fixture(autouse=True)
def _mail_goes_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _send(**kwargs: Any) -> None:
        return None

    for kind in ("send_verification_email", "send_password_reset_email"):
        monkeypatch.setattr(auth_mod.email_sender, kind, _send)


async def _commit(ambient_uow: Any) -> None:
    await run_on_commit(ambient_uow)
    await auth_mod.drain_pending_emails()


def test_the_paths_are_the_routes_of_the_auth_router() -> None:
    paths = {route.path for route in auth_router.routes}  # type: ignore[attr-defined]
    assert auth_mod.VERIFY_EMAIL_API_PATH in paths
    assert auth_mod.RESET_API_PATH in paths


@pytest.mark.asyncio
async def test_registering_prints_the_verify_request_that_verifies(
    test_db: AsyncSession,
    _ambient_uow: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "notifier_log_links", True)
    await AuthService.register(
        RegisterIn(email="dev-step@example.com", full_name="Dev", password=PASSWORD),
        test_db,
    )
    assert _STEP.search(capsys.readouterr().err) is None  # nothing before commit
    await _commit(_ambient_uow)

    printed = _STEP.search(capsys.readouterr().err)
    assert printed is not None
    assert printed.group(1) == "/auth/verify-email"
    body = json.loads(printed.group(2))
    assert list(body) == ["token"]
    # The printed request is the real step: the token verifies the account.
    user = await AuthService.confirm_email(body["token"], test_db)
    assert user.is_verified


@pytest.mark.asyncio
async def test_a_password_reset_prints_the_reset_request(
    test_db: AsyncSession,
    _ambient_uow: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "notifier_log_links", True)
    user = await make_user("dev-reset@example.com")
    await AuthService.forgot_password(user.email, test_db)
    await _commit(_ambient_uow)

    printed = _STEP.search(capsys.readouterr().err)
    assert printed is not None
    assert printed.group(1) == "/auth/reset-password"
    body = json.loads(printed.group(2))
    assert sorted(body) == ["new_password", "token"]
    # The printed request is the real step: the token resets the password.
    new_password = "AnotherPassword456!"
    reset = await AuthService.reset_password(body["token"], new_password, test_db)
    assert AuthService.verify_password(new_password, reset.hashed_password or "")
    assert not AuthService.verify_password(PASSWORD, reset.hashed_password or "")


@pytest.mark.asyncio
async def test_nothing_is_printed_without_the_flag(
    test_db: AsyncSession,
    _ambient_uow: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "notifier_log_links", False)
    user = await make_user("no-flag@example.com")
    unverified = await make_user("no-flag-unverified@example.com", verified=False)
    await AuthService.forgot_password(user.email, test_db)
    await AuthService.resend_verification(unverified.email, test_db)
    await _commit(_ambient_uow)

    printed = capsys.readouterr()
    assert "no frontend?" not in printed.err + printed.out
    assert "[dev only]" not in printed.err + printed.out


@pytest.mark.asyncio
async def test_nothing_is_printed_when_the_mail_does_not_go_through_the_log_driver(
    test_db: AsyncSession,
    _ambient_uow: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``NOTIFIER_LOG_LINKS`` with the ``smtp`` driver: a real mail goes out, and
    the live token must not also reach stderr (and the log pipeline)."""
    monkeypatch.setattr(settings, "notifier_log_links", True)
    monkeypatch.setattr(notifier(), "driver", "smtp")
    user = await make_user("smtp-driver@example.com")
    await AuthService.forgot_password(user.email, test_db)
    await _commit(_ambient_uow)

    printed = capsys.readouterr()
    assert "no frontend?" not in printed.err + printed.out
    assert "[dev only]" not in printed.err + printed.out


class _CustomSender(EmailSender):
    """A project's own mail adapter: it does not go through the notifier."""

    async def send_verification_email(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_password_reset_email(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_nothing_is_printed_with_a_custom_email_sender(
    test_db: AsyncSession,
    _ambient_uow: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "notifier_log_links", True)
    monkeypatch.setattr(auth_mod, "email_sender", _CustomSender())
    user = await make_user("custom-sender@example.com")
    await AuthService.forgot_password(user.email, test_db)
    await _commit(_ambient_uow)

    printed = capsys.readouterr()
    assert "no frontend?" not in printed.err + printed.out


class _BrokenStderr:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def write(self, _text: str) -> int:
        raise self.error

    def flush(self) -> None:
        raise self.error


@pytest.mark.asyncio
async def test_a_stderr_that_cannot_be_written_does_not_skip_the_delivery(
    _ambient_uow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered: list[str] = []

    async def _send() -> None:
        delivered.append("sent")

    monkeypatch.setattr(auth_mod.sys, "stderr", _BrokenStderr(BrokenPipeError()))
    await auth_mod._email_after_commit(_send, dev_step="[dev only] hint")
    await _commit(_ambient_uow)
    assert delivered == ["sent"]


def test_a_failure_of_the_print_that_is_not_an_io_error_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_mod.sys, "stderr", _BrokenStderr(RuntimeError("bug")))
    with pytest.raises(RuntimeError, match="bug"):
        auth_mod._print_dev_step("[dev only] hint")
