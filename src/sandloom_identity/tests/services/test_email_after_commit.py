"""Identity emails go out after the UoW commits, off the response path.

Sending synchronously inside the request made the response time depend on
whether a mail was sent — i.e. on whether the account exists (registration /
reset / resend are answered identically on purpose) — and sent links for
changes that then rolled back. Emails are now queued with ``on_commit`` and
delivered in the background: nothing goes out before the commit, nothing on a
rollback, and the commit path never waits for the mail server.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.testing import run_on_commit
from sandloom_identity.schemas.auth import RegisterIn
from sandloom_identity.services import auth_service as auth_mod
from sandloom_identity.services.auth_service import AuthService

PASSWORD = "TestPassword123!"


class _BlockingSender:
    """Records sends; each send blocks until ``release`` is set."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.release = asyncio.Event()

    def method(self, kind: str) -> Callable[..., Any]:
        async def send(**kwargs: Any) -> None:
            await self.release.wait()
            self.sent.append(kind)

        return send


@pytest.fixture
def sender(monkeypatch: pytest.MonkeyPatch) -> _BlockingSender:
    fake = _BlockingSender()
    for kind in (
        "send_verification_email",
        "send_password_reset_email",
        "send_registration_attempt_email",
    ):
        monkeypatch.setattr(auth_mod.email_sender, kind, fake.method(kind))
    return fake


async def _fast(awaitable: Any) -> Any:
    """The service call must not wait for the mail server (fail, never hang)."""
    return await asyncio.wait_for(awaitable, timeout=2)


async def _committed(session: Any, sender: _BlockingSender) -> None:
    """Commit the ambient UoW: the queued sends are spawned, NOT awaited."""
    await asyncio.wait_for(run_on_commit(session), timeout=1)
    assert sender.sent == []  # the commit path did not wait for the mail
    sender.release.set()
    await auth_mod.drain_pending_emails()


@pytest.mark.asyncio
async def test_register_sends_nothing_before_commit(
    test_db: AsyncSession, _ambient_uow: Any, sender: _BlockingSender, make_user: Any
) -> None:
    await _fast(
        AuthService.register(
            RegisterIn(
                email="new-after-commit@example.com", full_name="New", password=PASSWORD
            ),
            test_db,
        )
    )
    existing = await make_user("taken-after-commit@example.com")
    await _fast(
        AuthService.register(
            RegisterIn(email=existing.email, full_name="Taken", password=PASSWORD),
            test_db,
        )
    )
    assert sender.sent == []
    await _committed(_ambient_uow, sender)
    assert sorted(sender.sent) == [
        "send_registration_attempt_email",
        "send_verification_email",
    ]


@pytest.mark.asyncio
async def test_reset_and_resend_send_after_commit_only(
    test_db: AsyncSession, _ambient_uow: Any, sender: _BlockingSender, make_user: Any
) -> None:
    user = await make_user("reset-after-commit@example.com")
    unverified = await make_user("resend-after-commit@example.com", verified=False)
    await _fast(AuthService.forgot_password(user.email, test_db))
    await _fast(AuthService.resend_verification(unverified.email, test_db))
    assert sender.sent == []
    await _committed(_ambient_uow, sender)
    assert sorted(sender.sent) == [
        "send_password_reset_email",
        "send_verification_email",
    ]


@pytest.mark.asyncio
async def test_rolled_back_change_sends_nothing(
    test_db: AsyncSession, _ambient_uow: Any, sender: _BlockingSender, make_user: Any
) -> None:
    user = await make_user("rollback-reset@example.com")
    await _fast(AuthService.forgot_password(user.email, test_db))
    # The UoW rolls back: its on_commit queue is never run.
    sender.release.set()
    await auth_mod.drain_pending_emails()
    assert sender.sent == []


def test_identity_drains_emails_on_shutdown() -> None:
    from sandloom.sdk.testing import registration_surface
    from sandloom_identity import register

    surface = registration_surface(register)
    assert (
        "add_shutdown_task",
        "sandloom_identity.services.auth_service.drain_emails_on_shutdown",
    ) in surface.of("add_shutdown_task")


@pytest.mark.asyncio
async def test_shutdown_drain_finishes_deliveries_but_is_bounded(
    test_db: AsyncSession,
    _ambient_uow: Any,
    sender: _BlockingSender,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await make_user("shutdown-drain@example.com")
    await _fast(AuthService.forgot_password(user.email, test_db))
    await asyncio.wait_for(run_on_commit(_ambient_uow), timeout=1)

    # A hung mail server cannot hang shutdown: the drain gives up in time.
    monkeypatch.setattr(auth_mod, "EMAIL_SHUTDOWN_DRAIN_SECONDS", 0.2)
    await asyncio.wait_for(auth_mod.drain_emails_on_shutdown(), timeout=2)
    assert sender.sent == []

    # An in-flight delivery that completes in time is awaited, not cancelled.
    sender.release.set()
    await asyncio.wait_for(auth_mod.drain_emails_on_shutdown(), timeout=2)
    assert sender.sent == ["send_password_reset_email"]
