"""A dev app without a frontend can finish the e-mailed steps through the API.

With ``NOTIFIER_LOG_LINKS`` the log notifier prints the e-mail's links, which
point at the frontend (``FRONTEND_URL``). Identity prints the same step as the
API request that completes it, marked ``[dev only]``, once the change commits.
Without the flag nothing is printed, and no flag verifies an account by itself.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.infra import settings
from sandloom.sdk.testing import run_on_commit
from sandloom_identity.api.auth import auth_router
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
    assert auth_mod.RESET_PASSWORD_API_PATH in paths


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
    assert body["token"]


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
