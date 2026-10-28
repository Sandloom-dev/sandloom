"""Identity module utilities for cryptography and tokens."""

import base64
import functools
import hashlib
import hmac
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any, TypedDict, cast
from uuid import UUID

import pyotp
from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from sandloom.sdk.infra import settings


class TokenPayload(TypedDict):
    data: dict[str, Any]
    expires_at: str


#: HKDF salt / info labels: domain-separate every key derived from a shared
#: secret (by default the JWT secret) so a Fernet key is never the raw secret
#: hashed — and two purposes never share a key.
_HKDF_SALT = b"sandloom_identity.v1"
_DEFAULT_CRYPTO_INFO = b"sandloom_identity.crypto.fernet.v1"


def identity_secret() -> str:
    """Identity's own symmetric secret (``JWT_SECRET``).

    Keys the emailed (itsdangerous) tokens and the secret fingerprints. In an
    asymmetric JWT deployment it is issuer-only — identity is the issuer — and
    never signs or verifies an access token; identity's critical startup
    check refuses to serve without it.
    """
    secret = settings.jwt_secret
    if not secret:
        raise RuntimeError(
            "identity requires JWT_SECRET (its emailed tokens and fingerprints "
            "are keyed with it; with an asymmetric JWT_ALGORITHM it is set on "
            "the issuer only)"
        )
    return secret


def _hkdf_fernet_key(secret: str, info: bytes) -> bytes:
    """Derive a urlsafe-base64 Fernet key from ``secret`` via HKDF-SHA256."""
    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALT, info=info
    ).derive(secret.encode())
    return base64.urlsafe_b64encode(derived)


class CryptoUtils:
    """Utilities for encryption and decryption.

    ``key`` may be one secret or a sequence of secrets (newest first) for key
    rotation: encryption always uses the first, decryption tries every key
    (``MultiFernet``). Keys are derived with HKDF and a purpose ``info`` label.
    """

    def __init__(
        self,
        key: str | Sequence[str] | None = None,
        *,
        info: bytes = _DEFAULT_CRYPTO_INFO,
    ) -> None:
        """Initialize crypto utils with one or more encryption keys.

        Without ``key`` the identity secret is read on first use, never at
        construction (the module-level instance is built at import).
        """
        keys: list[str] | None
        if key is None:
            keys = None
        elif isinstance(key, str):
            keys = [key]
        else:
            keys = [k for k in key if k]
        if keys is not None and not keys:
            raise ValueError("CryptoUtils requires at least one non-empty key")
        self._keys = keys
        self._info = info

    @functools.cached_property
    def _fernet(self) -> MultiFernet:
        keys = self._keys if self._keys is not None else [identity_secret()]
        return MultiFernet([Fernet(_hkdf_fernet_key(k, self._info)) for k in keys])

    def encrypt(self, data: str) -> str:
        """Encrypt data and return base64 encoded string."""
        encrypted = self._fernet.encrypt(data.encode())
        return base64.urlsafe_b64encode(encrypted).decode()

    def decrypt(self, encrypted_data: str) -> str:
        """Decrypt base64 encoded encrypted data."""
        try:
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted = self._fernet.decrypt(encrypted_bytes)
            return decrypted.decode()
        except Exception as e:
            raise ValueError("Failed to decrypt data") from e


class TokenUtils:
    """Utilities for generating and validating tokens."""

    def __init__(self, secret_key: str | None = None, salt: str = "identity") -> None:
        """Initialize token utils (without ``secret_key`` the identity secret
        is read on first use, never at construction)."""
        self._secret_key = secret_key
        self._salt = salt

    @functools.cached_property
    def _serializer(self) -> URLSafeTimedSerializer:
        secret_key = self._secret_key
        if secret_key is None:
            secret_key = identity_secret()
        return URLSafeTimedSerializer(secret_key, salt=self._salt)

    def generate_token(
        self,
        data: dict[str, Any],
        expires_in: int | None = None,
    ) -> str:
        """Generate a signed token."""
        if expires_in is None:
            expires_in = 3600  # 1 hour default

        token_data: TokenPayload = {
            "data": data,
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=expires_in)
            ).isoformat(),
        }
        return cast(str, self._serializer.dumps(token_data))

    def validate_token(
        self,
        token: str,
        max_age: int | None = None,
    ) -> dict[str, Any]:
        """Validate and decode a token."""
        try:
            if max_age is None:
                max_age = 3600  # 1 hour default

            token_data = self._serializer.loads(token, max_age=max_age)

            # Check expiration time
            expires_at = datetime.fromisoformat(token_data["expires_at"])
            if datetime.now(UTC) > expires_at:
                raise SignatureExpired("Token has expired")

            return token_data["data"]  # type: ignore
        except (BadSignature, SignatureExpired, KeyError, ValueError) as e:
            raise ValueError(f"Invalid token: {e}") from e


class TwoFAUtils:
    """Utilities for Two-Factor Authentication."""

    @staticmethod
    def generate_secret() -> str:
        """Generate a new TOTP secret."""
        return pyotp.random_base32()

    @staticmethod
    def generate_qr_code(
        secret: str,
        user_email: str,
        issuer_name: str = "Sandloom",
    ) -> str:
        """Generate QR code data URL for TOTP setup."""
        totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(
            name=user_email,
            issuer_name=issuer_name,
        )

        # Imported on demand: qrcode (and the Pillow image factory it loads)
        # is only needed while enrolling 2FA, never on the request hot path or
        # at process start.
        import qrcode  # sandloom: optional-import Pillow only while enrolling 2FA

        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(totp_uri)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")

        # Convert to base64 data URL
        buffer = BytesIO()
        img.save(buffer, "PNG")
        img_str = base64.b64encode(buffer.getvalue()).decode()

        return f"data:image/png;base64,{img_str}"

    @staticmethod
    def verify_totp(secret: str, token: str, window: int = 1) -> bool:
        """Verify a TOTP token."""
        totp = pyotp.TOTP(secret)
        return totp.verify(token, valid_window=window)

    @staticmethod
    def generate_backup_codes(count: int = 10) -> list[str]:
        """Generate backup codes for 2FA."""
        return [secrets.token_hex(10).upper() for _ in range(count)]


class RefreshTokenUtils:
    """Utilities for refresh token management."""

    @staticmethod
    def generate_token() -> str:
        """Generate a cryptographically secure refresh token."""
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash_token(token: str) -> str:
        """Hash a refresh token for storage."""
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def verify_token(token: str, token_hash: str) -> bool:
        """Verify a refresh token against its hash."""
        return hashlib.sha256(token.encode()).hexdigest() == token_hash


def secret_fingerprint(value: str | None, purpose: str) -> str:
    """Keyed, purpose-bound fingerprint of a secret-ish value (e.g. a hash).

    Embedded in emailed tokens so a token dies when the bound value changes
    (password hash, email). HMAC-keyed so the readable itsdangerous payload
    reveals nothing about the bound value.
    """
    key = hashlib.sha256(
        b"sandloom_identity.fingerprint.v1|" + identity_secret().encode()
    ).digest()
    msg = f"{purpose}|{value or ''}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class PasswordResetClaims:
    """Decoded password-reset token claims."""

    user_id: UUID
    nonce: str | None
    password_fingerprint: str | None


@dataclass(frozen=True)
class EmailVerificationClaims:
    """Decoded email-verification token claims."""

    user_id: UUID
    email_fingerprint: str | None


class EmailTokenUtils:
    """Utilities for email verification and password reset tokens.

    Password-reset tokens carry a per-user ``nonce`` (the single outstanding
    reset, stored on the user) and a fingerprint of the current password hash,
    so a link is single-use, superseded by a newer one, and dies with any
    password change. Verification tokens carry a fingerprint of the email they
    were issued for.
    """

    def __init__(self) -> None:
        """Initialize email token utils."""
        self._token_utils = TokenUtils(salt="email-verification")
        self._password_utils = TokenUtils(salt="password-reset")

    def generate_email_verification_token(
        self, user_id: UUID, email: str | None = None
    ) -> str:
        """Generate email verification token (optionally bound to ``email``)."""
        data: dict[str, Any] = {"user_id": str(user_id), "type": "email_verification"}
        if email is not None:
            data["ef"] = secret_fingerprint(email.lower(), "email")
        return self._token_utils.generate_token(data, expires_in=86400)  # 24 hours

    def read_email_verification_token(self, token: str) -> EmailVerificationClaims:
        """Validate a verification token and return its claims."""
        try:
            data = self._token_utils.validate_token(token, max_age=86400)
            if data.get("type") != "email_verification":
                raise ValueError("Invalid token type")
            return EmailVerificationClaims(
                user_id=UUID(data["user_id"]), email_fingerprint=data.get("ef")
            )
        except Exception as e:
            raise ValueError("Invalid email verification token") from e

    def validate_email_verification_token(self, token: str) -> UUID:
        """Validate email verification token and return user ID."""
        return self.read_email_verification_token(token).user_id

    def generate_password_reset_token(
        self,
        user_id: UUID,
        *,
        nonce: str | None = None,
        hashed_password: str | None = None,
    ) -> str:
        """Generate password reset token bound to ``nonce`` + password hash."""
        data: dict[str, Any] = {"user_id": str(user_id), "type": "password_reset"}
        if nonce is not None:
            data["n"] = nonce
            data["pf"] = secret_fingerprint(hashed_password, "password")
        return self._password_utils.generate_token(data, expires_in=3600)  # 1 hour

    def read_password_reset_token(self, token: str) -> PasswordResetClaims:
        """Validate a password reset token and return its claims."""
        try:
            data = self._password_utils.validate_token(token, max_age=3600)
            if data.get("type") != "password_reset":
                raise ValueError("Invalid token type")
            return PasswordResetClaims(
                user_id=UUID(data["user_id"]),
                nonce=data.get("n"),
                password_fingerprint=data.get("pf"),
            )
        except Exception as e:
            raise ValueError("Invalid password reset token") from e

    def validate_password_reset_token(self, token: str) -> UUID:
        """Validate password reset token and return user ID."""
        return self.read_password_reset_token(token).user_id


# Global instances
crypto_utils = CryptoUtils()
twofa_utils = TwoFAUtils()
refresh_token_utils = RefreshTokenUtils()
email_token_utils = EmailTokenUtils()
