"""Auth hardening against a real database + test Redis.

Single-use / superseded / password-bound reset tokens, email-bound
verification tokens, per-account lockout (no enumeration), registration
enumeration + race, password policy, change-password, PII-free events.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from sandloom_identity import lockout
from sandloom_identity.errors import CommittedHTTPError
from sandloom_identity.events import create_user_registered_event
from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.schemas.auth import LoginIn, PasswordChangeIn, RegisterIn
from sandloom_identity.services import auth_service as auth_mod
from sandloom_identity.services.auth_service import AuthService
from sandloom_identity.services.token_service import TokenService
from sandloom_identity.tests.utils.emails import deliver_emails
from sandloom_identity.utils import email_token_utils

PW = "TestPassword123!"


async def _issue_reset_token(test_db, user) -> str:
    with patch.object(
        auth_mod.email_sender, "send_password_reset_email", new=AsyncMock()
    ) as send:
        await AuthService.forgot_password(user.email, test_db)
        await test_db.commit()
        await deliver_emails()  # sent after commit, in the background
    url = send.await_args.kwargs["reset_url"]
    return url.split("token=", 1)[1]


class TestPasswordResetTokens:
    @pytest.mark.asyncio
    async def test_token_is_single_use(self, test_db, make_user) -> None:
        user = await make_user("reset1@example.com")
        token = await _issue_reset_token(test_db, user)
        await AuthService.reset_password(token, "BrandNewPass#1", test_db)
        await test_db.commit()
        with pytest.raises(HTTPException) as exc:
            await AuthService.reset_password(token, "AnotherPass#22", test_db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_newer_token_invalidates_older(self, test_db, make_user) -> None:
        user = await make_user("reset2@example.com")
        old = await _issue_reset_token(test_db, user)
        new = await _issue_reset_token(test_db, user)
        with pytest.raises(HTTPException):
            await AuthService.reset_password(old, "BrandNewPass#1", test_db)
        await AuthService.reset_password(new, "BrandNewPass#1", test_db)

    @pytest.mark.asyncio
    async def test_token_dies_with_password_change(self, test_db, make_user) -> None:
        user = await make_user("reset3@example.com")
        token = await _issue_reset_token(test_db, user)
        # Password changed through another path (nonce left in place).
        user.hashed_password = AuthService.get_password_hash("ChangedElsewhere#9")
        await test_db.commit()
        with pytest.raises(HTTPException):
            await AuthService.reset_password(token, "BrandNewPass#1", test_db)

    @pytest.mark.asyncio
    async def test_legacy_unbound_token_rejected(self, test_db, make_user) -> None:
        user = await make_user("reset4@example.com")
        await _issue_reset_token(test_db, user)  # user now has a nonce
        legacy = email_token_utils.generate_password_reset_token(user.id)
        with pytest.raises(HTTPException):
            await AuthService.reset_password(legacy, "BrandNewPass#1", test_db)

    @pytest.mark.asyncio
    async def test_reset_revokes_sessions_and_verifies(
        self, test_db, make_user
    ) -> None:
        user = await make_user("reset5@example.com", verified=False)
        await TokenService.create_tokens(user, test_db)
        token = await _issue_reset_token(test_db, user)
        with patch.object(auth_mod, "record_audit") as audit:
            await AuthService.reset_password(token, "BrandNewPass#1", test_db)
        await test_db.commit()
        test_db.expire_all()
        from sqlalchemy import select

        rows = (await test_db.execute(select(RefreshToken))).scalars().all()
        assert rows and all(r.is_revoked for r in rows)
        await test_db.refresh(user)
        assert user.is_verified is True  # reset proves mailbox control
        assert audit.call_args.args[0] == "identity.password.reset"


class TestEmailVerificationTokens:
    @pytest.mark.asyncio
    async def test_token_bound_to_email(self, test_db, make_user) -> None:
        user = await make_user("verify@example.com", verified=False)
        other = email_token_utils.generate_email_verification_token(
            user.id, email="someone-else@example.com"
        )
        with pytest.raises(HTTPException):
            await AuthService.confirm_email(other, test_db)
        good = email_token_utils.generate_email_verification_token(
            user.id, email=user.email
        )
        confirmed = await AuthService.confirm_email(good, test_db)
        assert confirmed.is_verified is True
        # Single use in effect: the second confirmation is refused.
        with pytest.raises(HTTPException):
            await AuthService.confirm_email(good, test_db)


class TestLockout:
    @pytest.mark.asyncio
    async def test_lock_after_threshold_even_with_correct_password(
        self, test_db, make_user
    ) -> None:
        await make_user("lock@example.com")
        for _ in range(lockout.FAIL_THRESHOLD):
            with pytest.raises(HTTPException):
                await AuthService.login(
                    LoginIn(email="lock@example.com", password="wrong-password"),
                    test_db,
                )
        with pytest.raises(HTTPException) as exc:
            await AuthService.login(
                LoginIn(email="lock@example.com", password=PW), test_db
            )
        assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_unknown_email_locks_identically(self, test_db) -> None:
        """Lock behaviour must not reveal whether the account exists."""
        for _ in range(lockout.FAIL_THRESHOLD):
            with pytest.raises(HTTPException) as exc:
                await AuthService.login(
                    LoginIn(email="ghost@example.com", password="x" * 9), test_db
                )
            assert exc.value.status_code == 401
        with pytest.raises(HTTPException) as exc:
            await AuthService.login(
                LoginIn(email="ghost@example.com", password="x" * 9), test_db
            )
        assert exc.value.status_code == 429
        assert exc.value.detail == lockout.LOCKED_DETAIL

    @pytest.mark.asyncio
    async def test_lock_is_audited_and_committed(self, test_db, make_user) -> None:
        user = await make_user("audit-lock@example.com")
        raised = []
        with patch.object(auth_mod, "record_audit") as audit:
            for _ in range(lockout.FAIL_THRESHOLD):
                try:
                    await AuthService.login(
                        LoginIn(email=user.email, password="wrong-password"), test_db
                    )
                except HTTPException as exc:
                    raised.append(exc)
        # The locking failure is a CommittedHTTPError so its audit row persists.
        assert isinstance(raised[-1], CommittedHTTPError)
        assert audit.call_args.args[0] == "identity.login.locked"
        assert audit.call_args.kwargs["resource_id"] == str(user.id)
        assert "email" not in str(audit.call_args.kwargs.get("data"))

    @pytest.mark.asyncio
    async def test_success_resets_counter(self, test_db, make_user) -> None:
        await make_user("reset-counter@example.com")
        for _ in range(lockout.FAIL_THRESHOLD - 1):
            with pytest.raises(HTTPException):
                await AuthService.login(
                    LoginIn(email="reset-counter@example.com", password="nope-nope"),
                    test_db,
                )
        await AuthService.login(
            LoginIn(email="reset-counter@example.com", password=PW), test_db
        )
        for _ in range(lockout.FAIL_THRESHOLD - 1):
            with pytest.raises(HTTPException) as exc:
                await AuthService.login(
                    LoginIn(email="reset-counter@example.com", password="nope-nope"),
                    test_db,
                )
            assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_attacker_cannot_lock_out_the_owner_from_another_ip(
        self, test_db, make_user
    ) -> None:
        """Hard lock is per (account, client IP): no lockout DoS of a victim."""
        from sandloom_identity.events import EventActor

        await make_user("victim@example.com")
        attacker = EventActor(ip="203.0.113.7")
        for _ in range(lockout.FAIL_THRESHOLD):
            with pytest.raises(HTTPException) as exc:
                await AuthService.login(
                    LoginIn(email="victim@example.com", password="wrong-password"),
                    test_db,
                    actor=attacker,
                )
            assert exc.value.status_code == 401
        # The attacker's own (account, IP) pair is locked …
        with pytest.raises(HTTPException) as exc:
            await AuthService.login(
                LoginIn(email="victim@example.com", password=PW),
                test_db,
                actor=attacker,
            )
        assert exc.value.status_code == 429
        # … the owner signing in from elsewhere is not.
        _user, tokens = await AuthService.login(
            LoginIn(email="victim@example.com", password=PW),
            test_db,
            actor=EventActor(ip="198.51.100.20"),
        )
        assert tokens.access_token

    @pytest.mark.asyncio
    async def test_distributed_failures_slow_the_account_down_without_locking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays: list[float] = []

        async def _sleep(seconds: float) -> None:
            delays.append(seconds)

        monkeypatch.setattr(lockout.asyncio, "sleep", _sleep)
        subject = lockout.subject_for_email("spread@example.com")
        for i in range(lockout.GLOBAL_SOFT_THRESHOLD):
            await lockout.register_failure(subject, client_ip=f"10.0.{i}.1")
        # A fresh IP is slowed down (soft), not refused (hard).
        await lockout.ensure_not_locked(subject, client_ip="10.9.9.9")
        assert delays and delays[-1] == lockout.global_delay_for(
            lockout.GLOBAL_SOFT_THRESHOLD
        )
        assert lockout.global_delay_for(10_000) == lockout.MAX_GLOBAL_DELAY_SECONDS
        assert lockout.global_delay_for(lockout.GLOBAL_SOFT_THRESHOLD - 1) == 0

    @pytest.mark.asyncio
    async def test_check_reports_whether_a_failure_counter_exists(self) -> None:
        """The lock check reads the failure counter in the same round trip,
        so a clean sign-in skips the reset; a recorded failure is reset."""
        subject = lockout.subject_for_email("counter@example.com")
        ip = "198.51.100.33"
        assert await lockout.ensure_not_locked(subject, client_ip=ip) is False
        await lockout.register_failure(subject, client_ip=ip)
        assert await lockout.ensure_not_locked(subject, client_ip=ip) is True
        await lockout.reset_failures(subject, client_ip=ip)
        assert await lockout.ensure_not_locked(subject, client_ip=ip) is False
        # per-subject (second factor) keys work the same way
        otp = lockout.subject_for_user("u-1", "2fa")
        assert await lockout.ensure_not_locked(otp) is False
        await lockout.register_failure(otp)
        assert await lockout.ensure_not_locked(otp) is True

    def test_exponential_lock_durations(self) -> None:
        t = lockout.FAIL_THRESHOLD
        assert lockout.lock_seconds_for(t - 1) == 0
        assert lockout.lock_seconds_for(t) == lockout.BASE_LOCK_SECONDS
        assert lockout.lock_seconds_for(t + 1) == 2 * lockout.BASE_LOCK_SECONDS
        assert lockout.lock_seconds_for(t + 50) == lockout.MAX_LOCK_SECONDS

    @pytest.mark.asyncio
    async def test_cache_outage_fails_closed_in_prod(self) -> None:
        from sandloom.sdk.infra import settings

        broken = AsyncMock(side_effect=ConnectionError("redis down"))
        with (
            patch.object(
                lockout, "cache", return_value=type("C", (), {"get": broken})()
            ),
            patch.object(type(settings), "is_prod", new=property(lambda self: True)),
        ):
            with pytest.raises(HTTPException) as exc:
                await lockout.ensure_not_locked("email:x")
        assert exc.value.status_code == 503


class TestRegistration:
    @pytest.mark.asyncio
    async def test_existing_email_notifies_owner_instead(
        self, test_db, make_user
    ) -> None:
        await make_user("taken@example.com")
        with patch.object(
            auth_mod.email_sender, "send_registration_attempt_email", new=AsyncMock()
        ) as notify:
            result = await AuthService.register(
                RegisterIn(email="taken@example.com", full_name="X", password=PW),
                test_db,
            )
            notify.assert_not_awaited()  # never inline: only after commit
            await deliver_emails()
        assert result is None
        notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_insert_is_generic(self, test_db) -> None:
        """The check-then-insert race (unique violation) is not a 500."""
        real_flush = test_db.flush
        calls = {"n": 0}

        async def flaky_flush(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("INSERT", {}, Exception("duplicate key"))
            return await real_flush(*a, **kw)

        with patch.object(test_db, "flush", side_effect=flaky_flush):
            result = await AuthService.register(
                RegisterIn(email="race@example.com", full_name="R", password=PW),
                test_db,
            )
        assert result is None

    def test_registered_event_carries_no_email(self) -> None:
        from uuid import uuid4

        from sandloom_identity.events import EventActor

        event = create_user_registered_event(user_id=uuid4(), actor=EventActor())
        assert "email" not in event.payload


class TestPasswordPolicy:
    def test_common_password_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RegisterIn(email="a@example.com", full_name="A", password="password123")

    def test_email_as_password_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RegisterIn(
                email="someone@example.com",
                full_name="A",
                password="someone@example.com",
            )
        with pytest.raises(ValidationError):
            RegisterIn(
                email="longername@example.com", full_name="A", password="longername"
            )

    def test_change_schema_applies_policy(self) -> None:
        with pytest.raises(ValidationError):
            PasswordChangeIn(current_password="x", new_password="qwerty123")


class TestChangePassword:
    @pytest.mark.asyncio
    async def test_requires_current_password(self, test_db, make_user) -> None:
        user = await make_user("chg1@example.com")
        with pytest.raises(HTTPException) as exc:
            await AuthService.change_password(
                user.id, "wrong-one", "BrandNewPass#1", test_db
            )
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_change_revokes_other_sessions(self, test_db, make_user) -> None:
        user = await make_user("chg2@example.com")
        other_device = await TokenService.create_tokens(user, test_db)
        await test_db.commit()
        with patch.object(auth_mod, "record_audit") as audit:
            fresh = await AuthService.change_password(
                user.id, PW, "BrandNewPass#1", test_db
            )
        await test_db.commit()
        assert audit.call_args.args[0] == "identity.password.changed"
        with pytest.raises(HTTPException):
            await TokenService.refresh_token(other_device.refresh_token, test_db)
        await test_db.rollback()
        assert await TokenService.refresh_token(fresh.refresh_token, test_db)
        assert AuthService.verify_password("BrandNewPass#1", user.hashed_password)

    @pytest.mark.asyncio
    async def test_email_equal_password_rejected(self, test_db, make_user) -> None:
        user = await make_user("chg3@example.com")
        with pytest.raises(HTTPException) as exc:
            await AuthService.change_password(user.id, PW, "chg3@example.com", test_db)
        assert exc.value.status_code == 422
