"""Two-factor authentication service."""

import base64
import hashlib
import hmac
import io
import secrets
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pyotp
from fastapi import HTTPException, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import record_audit
from sandloom.sdk.effects import sensitive_effect
from sandloom.sdk.infra import settings
from sandloom_identity.events import (
    EventActor,
    TwoFABackupCodesRegeneratedEvent,
    TwoFADisabledEvent,
    TwoFAEnabledEvent,
)
from sandloom_identity.models.twofa_backup_code import TwoFABackupCode
from sandloom_identity.models.twofa_secret import TwoFASecret
from sandloom_identity.models.user import User
from sandloom_identity.schemas.twofa import (
    TwoFAInitResponse,
    TwoFAStatusOut,
)
from sandloom_identity.services.reauth import require_reauth
from sandloom_identity.utils import CryptoUtils, identity_secret

#: HKDF purpose label for TOTP secrets at rest (domain separation from any
#: other key derived from the same configured secret).
_TWOFA_CRYPTO_INFO = b"sandloom_identity.twofa-secret.v1"

#: Accepted clock drift, in TOTP steps, either side of "now".
TOTP_VALID_WINDOW = 1


class TwoFASecretDecryptionError(RuntimeError):
    """A stored TOTP secret could not be decrypted with any configured key."""


def _configured_twofa_keys() -> list[str]:
    configured = settings.twofa_encryption_key or ""
    return [k.strip() for k in configured.split(",") if k.strip()]


def twofa_configuration_error() -> str | None:
    """Why the 2FA key configuration must not serve traffic, or ``None``."""
    if settings.is_prod and not _configured_twofa_keys():
        return (
            "TWOFA_ENCRYPTION_KEY is required with ENVIRONMENT=prod (no JWT_SECRET "
            "fallback in production); generate one with openssl rand -hex 32"
        )
    return None


def _secret_crypto() -> CryptoUtils:
    """Crypto for TOTP secrets at rest.

    ``TWOFA_ENCRYPTION_KEY`` may hold a comma-separated list of keys (newest
    first) for rotation: new secrets are encrypted with the first, existing ones
    decrypt with any. Outside production an unset key falls back to the JWT
    secret, domain-separated by HKDF; in production the key is mandatory (the
    module refuses to start without it, and this raises as a backstop).
    """
    keys = _configured_twofa_keys()
    if keys:
        return CryptoUtils(key=keys, info=_TWOFA_CRYPTO_INFO)
    problem = twofa_configuration_error()
    if problem is not None:
        raise RuntimeError(problem)
    return CryptoUtils(key=identity_secret(), info=_TWOFA_CRYPTO_INFO)


def _encrypt_secret(secret: str) -> str:
    return _secret_crypto().encrypt(secret)


def _decrypt_secret(stored: str) -> str:
    """Decrypt a stored TOTP secret; fail loudly if it cannot be decrypted.

    Never fall back to treating the stored value as a plaintext secret: an
    undecryptable row means a key was lost/rotated away or the row was
    tampered with, and must not silently become a valid second factor.
    """
    try:
        return _secret_crypto().decrypt(stored)
    except ValueError as exc:
        raise TwoFASecretDecryptionError(
            "Stored 2FA secret cannot be decrypted with the configured key(s); "
            "check TWOFA_ENCRYPTION_KEY (keep old keys in the list while rotating)"
        ) from exc


def _match_totp_step(
    secret: str, code: str, window: int = TOTP_VALID_WINDOW
) -> int | None:
    """Return the TOTP time-step ``code`` is valid for, or ``None``."""
    code = code.strip()
    # ASCII digits only: str.isdigit() also accepts other scripts' digits,
    # on which compare_digest(str, str) below would raise TypeError (a 500).
    if len(code) != 6 or not (code.isascii() and code.isdigit()):
        return None
    totp = pyotp.TOTP(secret)
    now_step = int(time.time()) // totp.interval
    matched: int | None = None
    for step in range(now_step - window, now_step + window + 1):
        if hmac.compare_digest(totp.at(step * totp.interval), code):
            matched = step
    return matched


async def _resolve_user(user: User | UUID, db: AsyncSession) -> User:
    if isinstance(user, User):
        return user
    result = await db.execute(select(User).where(User.id == user))
    found = result.scalar_one_or_none()
    if found is None or not found.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )
    return found


class TwoFAService:
    """Service for Two-Factor Authentication operations."""

    @staticmethod
    async def is_2fa_enabled(user: User | UUID, db: AsyncSession) -> bool:
        """Lightweight check used by the login flows."""
        user_id = user.id if isinstance(user, User) else user
        result = await db.execute(
            select(TwoFASecret).where(
                TwoFASecret.user_id == user_id, TwoFASecret.is_enabled
            )
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    def _generate_backup_codes(count: int = 10) -> list[str]:
        """Generate backup codes for 2FA (80 bits of entropy each)."""
        return [secrets.token_hex(10) for _ in range(count)]

    @staticmethod
    def generate_backup_codes(count: int = 10) -> list[str]:
        """Generate backup codes for 2FA - public method."""
        return TwoFAService._generate_backup_codes(count)

    @staticmethod
    def _hash_backup_code(code: str) -> str:
        """Hash a backup code for secure storage."""
        return hashlib.sha256(code.encode()).hexdigest()

    @staticmethod
    async def _save_backup_codes(
        user: User, backup_codes: list[str], db: AsyncSession
    ) -> None:
        """Replace the user's backup codes."""
        await db.execute(
            delete(TwoFABackupCode).where(TwoFABackupCode.user_id == user.id)
        )
        for code in backup_codes:
            db.add(
                TwoFABackupCode(
                    user_id=user.id,
                    code_hash=TwoFAService._hash_backup_code(code),
                    is_used=False,
                )
            )

    @staticmethod
    async def get_existing_backup_codes(
        user: User | UUID, db: AsyncSession
    ) -> list[str]:
        """Return placeholders for the remaining (unused) backup codes."""
        user = await _resolve_user(user, db)
        result = await db.execute(
            select(func.count())
            .select_from(TwoFABackupCode)
            .where(
                TwoFABackupCode.user_id == user.id,
                ~TwoFABackupCode.is_used,
            )
        )
        count = int(result.scalar_one())
        return [f"****{i + 1:02d}" for i in range(count)]

    @staticmethod
    @sensitive_effect("security", step_up=300)
    async def regenerate_backup_codes(
        user: User | UUID,
        db: AsyncSession,
        actor: EventActor | None = None,
        *,
        otp: str,
        current_password: str | None = None,
        token_claims: dict[str, Any] | None = None,
    ) -> list[str]:
        """Regenerate backup codes (2FA enabled + re-auth + valid OTP)."""
        user = await _resolve_user(user, db)
        await require_reauth(user, current_password, token_claims=token_claims)
        if not await TwoFAService.is_2fa_enabled(user, db):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="2FA must be enabled to regenerate backup codes",
            )
        if not await TwoFAService.verify_2fa_code(user, otp, db):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid OTP code. Cannot regenerate backup codes.",
            )

        new_codes = TwoFAService._generate_backup_codes()
        await TwoFAService._save_backup_codes(user, new_codes, db)
        await db.flush()

        record_audit(
            "identity.twofa.backup_codes_regenerated",
            actor_id=actor.user_id if actor and actor.user_id else user.id,
            resource_type="user",
            resource_id=str(user.id),
            data={"count": len(new_codes)},
            required=True,
        )
        event = TwoFABackupCodesRegeneratedEvent(
            actor=actor or EventActor(user_id=user.id),
            payload={"user_id": str(user.id), "count": len(new_codes)},
        )
        await event.publish()
        return new_codes

    @staticmethod
    async def verify_backup_code(
        user: User | UUID, backup_code: str, db: AsyncSession
    ) -> bool:
        """Consume a backup code atomically (single use even under races)."""
        user_id = user.id if isinstance(user, User) else user
        code_hash = TwoFAService._hash_backup_code(backup_code)
        result = await db.execute(
            update(TwoFABackupCode)
            .where(
                TwoFABackupCode.user_id == user_id,
                TwoFABackupCode.code_hash == code_hash,
                TwoFABackupCode.is_used.is_(False),
            )
            .values(is_used=True, used_at=datetime.now(UTC))
            .returning(TwoFABackupCode.id)
            .execution_options(synchronize_session=False)
        )
        # Several identical hashes are impossible in practice (80-bit codes);
        # consuming them all at once is still the safe outcome.
        return len(result.scalars().all()) > 0

    @staticmethod
    def _generate_qr_code(
        secret: str, user_email: str, issuer: str = "Sandloom"
    ) -> str:
        """Generate QR code for TOTP setup."""
        totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(
            name=user_email, issuer_name=issuer
        )
        import qrcode  # sandloom: optional-import Pillow only while enrolling 2FA

        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(totp_uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, "PNG")
        img_str = base64.b64encode(buffer.getvalue()).decode()
        return f"data:image/png;base64,{img_str}"

    @staticmethod
    async def _consume_totp(
        twofa_secret: TwoFASecret, code: str, db: AsyncSession
    ) -> bool:
        """Accept ``code`` only for a time-step newer than the last accepted one.

        The step is claimed with a compare-and-set UPDATE, so the same code
        cannot be replayed (sequentially or concurrently) within its window.
        """
        step = _match_totp_step(_decrypt_secret(twofa_secret.secret), code)
        if step is None:
            return False
        result = await db.execute(
            update(TwoFASecret)
            .where(
                TwoFASecret.id == twofa_secret.id,
                or_(
                    TwoFASecret.last_used_step.is_(None),
                    TwoFASecret.last_used_step < step,
                ),
            )
            .values(last_used_step=step)
            .returning(TwoFASecret.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    @sensitive_effect("security", step_up=300)
    async def init_2fa(
        user: User | UUID,
        db: AsyncSession,
        *,
        current_password: str | None = None,
        token_claims: dict[str, Any] | None = None,
    ) -> TwoFAInitResponse:
        """Initialize 2FA setup for user (requires re-authentication)."""
        user = await _resolve_user(user, db)
        await require_reauth(user, current_password, token_claims=token_claims)

        result = await db.execute(
            select(TwoFASecret).where(TwoFASecret.user_id == user.id)
        )
        existing_secret = result.scalar_one_or_none()

        if existing_secret and existing_secret.is_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="2FA is already enabled for this user",
            )

        secret = pyotp.random_base32()
        qr_code = TwoFAService._generate_qr_code(
            secret=secret, user_email=user.email, issuer="Sandloom"
        )
        backup_codes = TwoFAService._generate_backup_codes()

        # Save or update secret (but keep disabled until confirmed)
        encrypted_secret = _encrypt_secret(secret)
        if existing_secret:
            existing_secret.secret = encrypted_secret
            existing_secret.is_enabled = False
            existing_secret.last_used_step = None
        else:
            db.add(
                TwoFASecret(user_id=user.id, secret=encrypted_secret, is_enabled=False)
            )

        await TwoFAService._save_backup_codes(user, backup_codes, db)
        await db.flush()

        return TwoFAInitResponse(
            secret=secret, qr_code=qr_code, backup_codes=backup_codes
        )

    @staticmethod
    @sensitive_effect("security", step_up=300)
    async def enable_2fa(
        user: User | UUID,
        otp_code: str,
        db: AsyncSession,
        actor: EventActor | None = None,
        *,
        current_password: str | None = None,
        token_claims: dict[str, Any] | None = None,
    ) -> TwoFAStatusOut:
        """Enable 2FA after re-authentication and verifying an OTP code."""
        user = await _resolve_user(user, db)
        await require_reauth(user, current_password, token_claims=token_claims)

        result = await db.execute(
            select(TwoFASecret).where(TwoFASecret.user_id == user.id)
        )
        twofa_secret = result.scalar_one_or_none()

        if not twofa_secret:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="2FA setup not found. Please initialize 2FA first.",
            )

        if twofa_secret.is_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="2FA is already enabled"
            )

        if not await TwoFAService._consume_totp(twofa_secret, otp_code, db):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP code"
            )

        twofa_secret.is_enabled = True
        twofa_secret.enabled_at = func.now()
        await db.flush()

        record_audit(
            "identity.twofa.enabled",
            actor_id=actor.user_id if actor and actor.user_id else user.id,
            resource_type="user",
            resource_id=str(user.id),
            data={"type": "totp"},
            required=True,
        )
        event = TwoFAEnabledEvent(
            actor=actor or EventActor(user_id=user.id),
            payload={"user_id": str(user.id), "type": "totp"},
        )
        await event.publish()

        return TwoFAStatusOut(enabled=True, has_backup_codes=True)

    @staticmethod
    @sensitive_effect("security", step_up=300)
    async def disable_2fa(
        user: User | UUID,
        otp_code: str,
        db: AsyncSession,
        actor: EventActor | None = None,
        *,
        current_password: str | None = None,
        token_claims: dict[str, Any] | None = None,
    ) -> TwoFAStatusOut:
        """Disable 2FA after re-authentication and verifying an OTP code."""
        user = await _resolve_user(user, db)
        await require_reauth(user, current_password, token_claims=token_claims)

        result = await db.execute(
            select(TwoFASecret).where(TwoFASecret.user_id == user.id)
        )
        twofa_secret = result.scalar_one_or_none()

        if not twofa_secret or not twofa_secret.is_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="2FA is not enabled for this user",
            )

        if not await TwoFAService.verify_2fa_code(user, otp_code, db):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP code"
            )

        await db.delete(twofa_secret)
        await db.execute(
            delete(TwoFABackupCode).where(TwoFABackupCode.user_id == user.id)
        )
        await db.flush()

        record_audit(
            "identity.twofa.disabled",
            actor_id=actor.user_id if actor and actor.user_id else user.id,
            resource_type="user",
            resource_id=str(user.id),
            required=True,
        )
        event = TwoFADisabledEvent(
            actor=actor or EventActor(user_id=user.id),
            payload={"user_id": str(user.id)},
        )
        await event.publish()
        return TwoFAStatusOut(enabled=False, has_backup_codes=False)

    @staticmethod
    async def reset_by_operator(
        user_id: UUID, db: AsyncSession, *, actor_id: UUID, reason: str
    ) -> None:
        """Operator recovery: remove the user's second factor (A-09).

        For a user who lost both the authenticator and every backup code
        (or whose secret became undecryptable after a lost
        ``TWOFA_ENCRYPTION_KEY``). Deletes the secret (enabled or pending)
        and every backup code; audited ``identity.twofa.reset`` with the
        operator and reason (required). The caller ends the user's sessions:
        a session opened with the lost factor must not survive its reset.
        404 when the user has no 2FA at all.
        """
        result = await db.execute(
            select(TwoFASecret).where(TwoFASecret.user_id == user_id)
        )
        twofa_secret = result.scalar_one_or_none()
        if twofa_secret is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="2FA is not set up for this user",
            )
        was_enabled = bool(twofa_secret.is_enabled)
        await db.delete(twofa_secret)
        await db.execute(
            delete(TwoFABackupCode).where(TwoFABackupCode.user_id == user_id)
        )
        await db.flush()
        record_audit(
            "identity.twofa.reset",
            actor_id=actor_id,
            resource_type="user",
            resource_id=str(user_id),
            data={"reason": reason, "was_enabled": was_enabled},
            required=True,
        )
        event = TwoFADisabledEvent(
            actor=EventActor(user_id=actor_id),
            payload={"user_id": str(user_id)},
        )
        await event.publish()

    @staticmethod
    async def get_2fa_status(user: User | UUID, db: AsyncSession) -> TwoFAStatusOut:
        """Get 2FA status for user."""
        user_id = user.id if isinstance(user, User) else user
        result = await db.execute(
            select(TwoFASecret).where(TwoFASecret.user_id == user_id)
        )
        twofa_secret = result.scalar_one_or_none()

        is_enabled = bool(twofa_secret and twofa_secret.is_enabled)

        if twofa_secret:
            backup_result = await db.execute(
                select(TwoFABackupCode.id)
                .where(
                    TwoFABackupCode.user_id == user_id,
                    ~TwoFABackupCode.is_used,
                )
                .limit(1)
            )
            has_backup_codes = backup_result.scalar_one_or_none() is not None
        else:
            has_backup_codes = False

        return TwoFAStatusOut(enabled=is_enabled, has_backup_codes=has_backup_codes)

    @staticmethod
    async def verify_2fa_code(
        user: User | UUID,
        code: str,
        db: AsyncSession,
        *,
        secret: TwoFASecret | None = None,
    ) -> bool:
        """Verify (and consume) a TOTP or backup code for user.

        TOTP codes are single-use per time-step; backup codes are single-use.
        ``secret``: the user's ENABLED secret row when the caller already
        loaded it in this session (the password login reads it together with
        the user), so it is not selected a second time.
        """
        user_id = user.id if isinstance(user, User) else user
        if secret is not None and secret.user_id == user_id and secret.is_enabled:
            twofa_secret: TwoFASecret | None = secret
        else:
            result = await db.execute(
                select(TwoFASecret).where(
                    TwoFASecret.user_id == user_id, TwoFASecret.is_enabled
                )
            )
            twofa_secret = result.scalar_one_or_none()

        if not twofa_secret:
            return False

        if await TwoFAService._consume_totp(twofa_secret, code, db):
            return True

        if TwoFAService.validate_backup_code(code):
            return await TwoFAService.verify_backup_code(user_id, code, db)

        return False

    @staticmethod
    def validate_backup_code(backup_code: str) -> bool:
        """Validate backup code format (20 hex characters)."""
        return len(backup_code) == 20 and all(
            c in "0123456789abcdef" for c in backup_code.lower()
        )
