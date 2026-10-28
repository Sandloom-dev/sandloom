"""TwoFAService against a real database.

Covers setup/enable/disable with step-up re-auth, TOTP replay protection
(last accepted time-step), atomic single-use backup codes, fail-loud secret
decryption, and key rotation.
"""

import asyncio
import time
from unittest.mock import patch

import pyotp
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from sandloom_identity.events import BaseEvent, EventActor
from sandloom_identity.models.twofa_backup_code import TwoFABackupCode
from sandloom_identity.models.twofa_secret import TwoFASecret
from sandloom_identity.services import twofa_service as twofa_mod
from sandloom_identity.services.twofa_service import (
    TwoFASecretDecryptionError,
    TwoFAService,
    _decrypt_secret,
    _encrypt_secret,
)

PASSWORD = "TestPassword123!"


def _code_for_step(secret: str, step: int) -> str:
    totp = pyotp.TOTP(secret)
    return totp.at(step * totp.interval)


async def _enabled_user(test_db, make_user, email="2fa@example.com"):
    user = await make_user(email)
    init = await TwoFAService.init_2fa(user, test_db, current_password=PASSWORD)
    now_step = int(time.time()) // 30
    # Use the previous step so "now" stays available for the test body.
    await TwoFAService.enable_2fa(
        user,
        _code_for_step(init.secret, now_step - 1),
        test_db,
        current_password=PASSWORD,
    )
    await test_db.commit()
    return user, init


class TestCodes:
    def test_generate_backup_codes(self) -> None:
        codes = TwoFAService.generate_backup_codes(5)
        assert len(codes) == 5
        assert all(len(c) == 20 for c in codes)
        assert len(set(codes)) == 5

    def test_validate_backup_code(self) -> None:
        assert TwoFAService.validate_backup_code("a" * 20)
        assert not TwoFAService.validate_backup_code("ABCDEF12")  # 8-hex: gone
        assert not TwoFAService.validate_backup_code("xyz")
        assert not TwoFAService.validate_backup_code("g" * 20)


class TestReauth:
    @pytest.mark.asyncio
    async def test_init_requires_current_password(self, test_db, make_user) -> None:
        user = await make_user("reauth-init@example.com")
        with pytest.raises(HTTPException) as exc:
            await TwoFAService.init_2fa(user, test_db)
        assert exc.value.status_code == 401
        with pytest.raises(HTTPException):
            await TwoFAService.init_2fa(user, test_db, current_password="wrong-pass")

    @pytest.mark.asyncio
    async def test_init_ok_with_password(self, test_db, make_user) -> None:
        user = await make_user("init-ok@example.com")
        out = await TwoFAService.init_2fa(user, test_db, current_password=PASSWORD)
        assert out.secret and out.qr_code.startswith("data:image/png;base64,")
        assert len(out.backup_codes) == 10
        row = (
            await test_db.execute(
                select(TwoFASecret).where(TwoFASecret.user_id == user.id)
            )
        ).scalar_one()
        assert row.is_enabled is False
        assert row.secret != out.secret  # encrypted at rest

    @pytest.mark.asyncio
    async def test_passwordless_user_needs_recent_sign_in(
        self, test_db, make_user
    ) -> None:
        user = await make_user("sso-only@example.com", password=None)
        with pytest.raises(HTTPException) as exc:
            await TwoFAService.init_2fa(
                user,
                test_db,
                token_claims={"iat": time.time(), "auth_time": time.time() - 3600},
            )
        assert "Recent sign-in" in exc.value.detail
        out = await TwoFAService.init_2fa(
            user, test_db, token_claims={"auth_time": time.time() - 10}
        )
        assert out.secret

    @pytest.mark.asyncio
    async def test_disable_and_regenerate_require_reauth(
        self, test_db, make_user
    ) -> None:
        user, init = await _enabled_user(test_db, make_user, "reauth-dis@example.com")
        code = pyotp.TOTP(init.secret).now()
        with pytest.raises(HTTPException) as exc:
            await TwoFAService.disable_2fa(user, code, test_db)
        assert exc.value.status_code == 401
        with pytest.raises(HTTPException) as exc:
            await TwoFAService.regenerate_backup_codes(user, test_db, otp=code)
        assert exc.value.status_code == 401


class TestEnableDisable:
    @pytest.mark.asyncio
    async def test_enable_then_status(self, test_db, make_user) -> None:
        user, _ = await _enabled_user(test_db, make_user)
        status = await TwoFAService.get_2fa_status(user, test_db)
        assert status.enabled is True and status.has_backup_codes is True
        assert await TwoFAService.is_2fa_enabled(user.id, test_db)

    @pytest.mark.asyncio
    async def test_enable_without_setup(self, test_db, make_user) -> None:
        user = await make_user("nosetup@example.com")
        with pytest.raises(HTTPException) as exc:
            await TwoFAService.enable_2fa(
                user, "123456", test_db, current_password=PASSWORD
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_enable_invalid_otp(self, test_db, make_user) -> None:
        user = await make_user("badotp@example.com")
        await TwoFAService.init_2fa(user, test_db, current_password=PASSWORD)
        with pytest.raises(HTTPException) as exc:
            await TwoFAService.enable_2fa(
                user, "000000", test_db, current_password=PASSWORD
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_init_when_enabled_conflicts(self, test_db, make_user) -> None:
        user, _ = await _enabled_user(test_db, make_user, "dup@example.com")
        with pytest.raises(HTTPException) as exc:
            await TwoFAService.init_2fa(user, test_db, current_password=PASSWORD)
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_disable_success_audits(self, test_db, make_user) -> None:
        user, init = await _enabled_user(test_db, make_user, "disable@example.com")
        with patch.object(twofa_mod, "record_audit") as audit:
            out = await TwoFAService.disable_2fa(
                user,
                pyotp.TOTP(init.secret).now(),
                test_db,
                EventActor(user_id=user.id),
                current_password=PASSWORD,
            )
        assert out.enabled is False
        assert audit.call_args.args[0] == "identity.twofa.disabled"
        assert not await TwoFAService.is_2fa_enabled(user, test_db)

    @pytest.mark.asyncio
    async def test_disable_not_enabled(self, test_db, make_user) -> None:
        user = await make_user("notenabled@example.com")
        with pytest.raises(HTTPException) as exc:
            await TwoFAService.disable_2fa(
                user, "123456", test_db, current_password=PASSWORD
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_enable_publishes_and_audits(self, test_db, make_user) -> None:
        user = await make_user("events@example.com")
        init = await TwoFAService.init_2fa(user, test_db, current_password=PASSWORD)
        published: list[str] = []

        async def fake_publish(
            event: BaseEvent, *, database: str | None = None
        ) -> None:
            published.append(event.channel)

        with (
            patch.object(twofa_mod, "record_audit") as audit,
            patch.object(BaseEvent, "publish", fake_publish),
        ):
            await TwoFAService.enable_2fa(
                user,
                pyotp.TOTP(init.secret).now(),
                test_db,
                current_password=PASSWORD,
            )
        assert audit.call_args.args[0] == "identity.twofa.enabled"
        assert published == ["identity.twofa_enabled.v1"]


class TestTOTPReplay:
    @pytest.mark.asyncio
    async def test_same_code_cannot_be_used_twice(self, test_db, make_user) -> None:
        user, init = await _enabled_user(test_db, make_user, "replay@example.com")
        code = pyotp.TOTP(init.secret).now()
        assert await TwoFAService.verify_2fa_code(user, code, test_db) is True
        assert await TwoFAService.verify_2fa_code(user, code, test_db) is False

    @pytest.mark.asyncio
    async def test_preloaded_secret_is_not_selected_again(
        self, test_db, make_user
    ) -> None:
        from sqlalchemy import event, select

        from sandloom_identity.models.twofa_secret import TwoFASecret

        user, init = await _enabled_user(test_db, make_user, "preload@example.com")
        secret = (
            await test_db.execute(
                select(TwoFASecret).where(TwoFASecret.user_id == user.id)
            )
        ).scalar_one()
        seen: list[str] = []

        def _record(_c, _cur, statement, *_a) -> None:  # type: ignore[no-untyped-def]
            seen.append(statement)

        engine = test_db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", _record)
        try:
            code = pyotp.TOTP(init.secret).now()
            assert await TwoFAService.verify_2fa_code(
                user, code, test_db, secret=secret
            )
            # Still single-use with a preloaded row (the CAS consume ran).
            assert not await TwoFAService.verify_2fa_code(
                user, code, test_db, secret=secret
            )
        finally:
            event.remove(engine, "before_cursor_execute", _record)
        assert not any(s.startswith("SELECT twofa_secrets") for s in seen), seen

    @pytest.mark.asyncio
    async def test_older_step_rejected_after_newer_used(
        self, test_db, make_user
    ) -> None:
        user, init = await _enabled_user(test_db, make_user, "older@example.com")
        now_step = int(time.time()) // 30
        assert await TwoFAService.verify_2fa_code(
            user, _code_for_step(init.secret, now_step), test_db
        )
        # The previous step's code is still inside the drift window but is
        # older than the last accepted step -> rejected.
        assert not await TwoFAService.verify_2fa_code(
            user, _code_for_step(init.secret, now_step - 1), test_db
        )

    @pytest.mark.asyncio
    async def test_concurrent_replay_only_one_accepted(
        self, test_engine: AsyncEngine, test_db, make_user
    ) -> None:
        user, init = await _enabled_user(test_db, make_user, "race2fa@example.com")
        code = pyotp.TOTP(init.secret).now()
        maker = async_sessionmaker(test_engine, expire_on_commit=False)

        async def attempt() -> bool:
            async with maker() as sess:
                ok = await TwoFAService.verify_2fa_code(user.id, code, sess)
                await sess.commit()
                return ok

        results = await asyncio.gather(attempt(), attempt())
        assert sorted(results) == [False, True]


class TestBackupCodes:
    @pytest.mark.asyncio
    async def test_backup_code_single_use(self, test_db, make_user) -> None:
        user, init = await _enabled_user(test_db, make_user, "backup@example.com")
        code = init.backup_codes[0]
        assert await TwoFAService.verify_2fa_code(user, code, test_db) is True
        assert await TwoFAService.verify_2fa_code(user, code, test_db) is False
        remaining = await TwoFAService.get_existing_backup_codes(user, test_db)
        assert len(remaining) == 9

    @pytest.mark.asyncio
    async def test_concurrent_backup_code_use(
        self, test_engine: AsyncEngine, test_db, make_user
    ) -> None:
        user, init = await _enabled_user(test_db, make_user, "racebk@example.com")
        code = init.backup_codes[1]
        maker = async_sessionmaker(test_engine, expire_on_commit=False)

        async def attempt() -> bool:
            async with maker() as sess:
                ok = await TwoFAService.verify_backup_code(user.id, code, sess)
                await sess.commit()
                return ok

        results = await asyncio.gather(attempt(), attempt())
        assert sorted(results) == [False, True]
        used = (
            (
                await test_db.execute(
                    select(TwoFABackupCode).where(TwoFABackupCode.is_used.is_(True))
                )
            )
            .scalars()
            .all()
        )
        assert len(used) == 1

    @pytest.mark.asyncio
    async def test_regenerate(self, test_db, make_user) -> None:
        user, init = await _enabled_user(test_db, make_user, "regen@example.com")
        with patch.object(twofa_mod, "record_audit") as audit:
            new_codes = await TwoFAService.regenerate_backup_codes(
                user,
                test_db,
                otp=pyotp.TOTP(init.secret).now(),
                current_password=PASSWORD,
            )
        assert len(new_codes) == 10 and set(new_codes).isdisjoint(init.backup_codes)
        assert audit.call_args.args[0] == "identity.twofa.backup_codes_regenerated"
        # Old codes are gone.
        assert not await TwoFAService.verify_backup_code(
            user, init.backup_codes[2], test_db
        )

    @pytest.mark.asyncio
    async def test_regenerate_requires_enabled(self, test_db, make_user) -> None:
        user = await make_user("regen-off@example.com")
        with pytest.raises(HTTPException) as exc:
            await TwoFAService.regenerate_backup_codes(
                user, test_db, otp="123456", current_password=PASSWORD
            )
        assert exc.value.status_code == 400


class TestSecretCrypto:
    def test_undecryptable_secret_fails_loudly(self) -> None:
        with pytest.raises(TwoFASecretDecryptionError):
            _decrypt_secret("JBSWY3DPEHPK3PXP")  # legacy plaintext is NOT accepted

    def test_key_rotation(self) -> None:
        from sandloom.sdk.infra import settings

        with patch.object(settings, "twofa_encryption_key", "old-key"):
            stored = _encrypt_secret("SECRET")
        with patch.object(settings, "twofa_encryption_key", "new-key,old-key"):
            assert _decrypt_secret(stored) == "SECRET"
            rotated = _encrypt_secret("SECRET")
        with patch.object(settings, "twofa_encryption_key", "new-key"):
            assert _decrypt_secret(rotated) == "SECRET"
            with pytest.raises(TwoFASecretDecryptionError):
                _decrypt_secret(stored)

    def test_sha256_derived_values_do_not_decrypt(self) -> None:
        """Only HKDF-derived keys decrypt (no plain SHA-256 derivation)."""
        import base64
        import hashlib

        from cryptography.fernet import Fernet

        from sandloom.sdk.infra import settings

        with patch.object(settings, "twofa_encryption_key", "some-key"):
            sha = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"some-key").digest()))
            stored = base64.urlsafe_b64encode(sha.encrypt(b"OLD")).decode()
            with pytest.raises(TwoFASecretDecryptionError):
                _decrypt_secret(stored)
