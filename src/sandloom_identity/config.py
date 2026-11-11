"""Identity's declared settings (ADR-0015).

The module owns the TOTP encryption key (with its production strength check),
the sign-in provider credentials and redirect allowlist, and the refresh
session lifetimes. Env names are unchanged; the kernel no longer lists them.
``JWT_*``, ``ACCESS_TOKEN_EXPIRE_MINUTES`` and ``AUTH_REVOCATION_*`` stay
kernel settings (token mechanics every verifier shares).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, Field, SecretStr, ValidationInfo, model_validator

from sandloom.sdk.module import settings_of
from sandloom.sdk.security import secret_problem


class IdentitySettings(BaseModel):
    # Dedicated key(s) for encrypting TOTP secrets at rest (comma-separated,
    # newest first, for rotation). Required in production; outside production
    # an unset key falls back to one derived from JWT_SECRET.
    twofa_encryption_key: SecretStr | None = Field(
        default=None,
        alias="TWOFA_ENCRYPTION_KEY",
        description=(
            "Comma-separated TOTP encryption keys, newest first, for rotation; "
            "production identity requires explicit keys, while development can "
            "derive a fallback from JWT_SECRET."
        ),
    )

    refresh_token_expire_days: int = Field(
        default=14,
        alias="REFRESH_TOKEN_EXPIRE_DAYS",
        description=(
            "Lifetime, in days, of newly issued refresh tokens, bounded by the "
            "absolute session lifetime."
        ),
    )
    max_session_days: int = Field(
        default=30,
        ge=1,
        le=365,
        alias="MAX_SESSION_DAYS",
        description=(
            "Absolute session lifetime in days; refreshing a token does not "
            "extend this deadline."
        ),
    )

    # SSO
    google_client_id: str | None = Field(
        default=None,
        alias="GOOGLE_CLIENT_ID",
        description="OAuth client ID for Google sign-in.",
    )
    google_client_secret: SecretStr | None = Field(
        default=None,
        alias="GOOGLE_CLIENT_SECRET",
        description="OAuth client secret for Google sign-in.",
    )
    github_client_id: str | None = Field(
        default=None,
        alias="GITHUB_CLIENT_ID",
        description="OAuth client ID for GitHub sign-in.",
    )
    github_client_secret: SecretStr | None = Field(
        default=None,
        alias="GITHUB_CLIENT_SECRET",
        description="OAuth client secret for GitHub sign-in.",
    )
    microsoft_client_id: str | None = Field(
        default=None,
        alias="MICROSOFT_CLIENT_ID",
        description="OAuth client ID for Microsoft sign-in.",
    )
    microsoft_client_secret: SecretStr | None = Field(
        default=None,
        alias="MICROSOFT_CLIENT_SECRET",
        description="OAuth client secret for Microsoft sign-in.",
    )
    linkedin_client_id: str | None = Field(
        default=None,
        alias="LINKEDIN_CLIENT_ID",
        description="OAuth client ID for LinkedIn sign-in.",
    )
    linkedin_client_secret: SecretStr | None = Field(
        default=None,
        alias="LINKEDIN_CLIENT_SECRET",
        description="OAuth client secret for LinkedIn sign-in.",
    )
    telegram_bot_token: SecretStr | None = Field(
        default=None,
        alias="TELEGRAM_BOT_TOKEN",
        description=(
            "Telegram bot credential used by the Telegram sign-in integration."
        ),
    )

    # SSO Security: allowed redirect URIs (comma-separated)
    sso_allowed_redirect_uris: str = Field(
        default="http://localhost:3000,http://localhost:3000/auth/callback,https://app.example.com/auth/callback",
        alias="SSO_ALLOWED_REDIRECT_URIS",
        description=(
            "Comma-separated exact redirect URI allowlist for SSO callbacks and "
            "frontend redirects."
        ),
    )

    #: What the kernel's tools read from this declaration (ADR-0015 decision 11).
    #: The console's settings view lists the refresh-session lifetime.
    console: ClassVar[tuple[str, ...]] = ("refresh_token_expire_days",)
    #: Identical in every process that loads identity (deploy profile check
    #: SANDLOOM-T014): the issuer's refresh sessions have one lifetime.
    shared: ClassVar[tuple[str, ...]] = ("REFRESH_TOKEN_EXPIRE_DAYS",)
    #: ``(keep in the secret manager, what losing it costs)`` for the backup plan.
    backup: ClassVar[tuple[tuple[str, str], ...]] = (
        (
            "TWOFA_ENCRYPTION_KEY (every key still in the list; a database dump "
            "plus this key is readable TOTP secrets)",
            "losing TWOFA_ENCRYPTION_KEY makes every 2FA secret undecryptable: "
            "users must re-enrol (operator reset-2fa)",
        ),
    )
    #: The compose environment of a process that loads identity
    #: (``sandloom add deploy``).
    deploy: ClassVar[tuple[str, ...]] = (
        "# identity encrypts TOTP secrets with a DEDICATED key (no JWT_SECRET",
        "# fallback in prod); comma-separated, newest first, for rotation.",
        "TWOFA_ENCRYPTION_KEY: ${TWOFA_ENCRYPTION_KEY:?set TWOFA_ENCRYPTION_KEY "
        "(openssl rand -hex 32) - required when identity is loaded}",
        "# Refresh-session lifetime: identity's setting (the issuer's sessions).",
        "REFRESH_TOKEN_EXPIRE_DAYS: ${REFRESH_TOKEN_EXPIRE_DAYS:-14}",
    )
    #: A development / CI stack (``sandloom keys dev-env``) gets a fresh key.
    dev_env: ClassVar[Mapping[str, str | Callable[[], str]]] = {
        "TWOFA_ENCRYPTION_KEY": lambda: secrets.token_hex(32)
    }

    def twofa_keys(self) -> list[str]:
        """The configured TOTP keys, newest first (empty when unset)."""
        raw = (
            self.twofa_encryption_key.get_secret_value()
            if self.twofa_encryption_key
            else ""
        )
        return [k.strip() for k in raw.split(",") if k.strip()]

    @model_validator(mode="after")
    def _twofa_key_in_prod(self, info: ValidationInfo) -> IdentitySettings:
        """Production: the TOTP key is present and every key is strong."""
        context: dict[str, Any] = info.context or {}
        if context.get("environment") != "prod":
            return self
        keys = self.twofa_keys()
        if not keys:
            raise ValueError(
                "TWOFA_ENCRYPTION_KEY is required with ENVIRONMENT=prod (no "
                "JWT_SECRET fallback in production); generate one with openssl "
                "rand -hex 32"
            )
        for idx, key in enumerate(keys):
            problem = secret_problem(key)
            if problem:
                raise ValueError(
                    f"TWOFA_ENCRYPTION_KEY[{idx}] is {problem}; generate a random "
                    "one (e.g. openssl rand -hex 32)"
                )
        return self


def identity_settings() -> IdentitySettings:
    """Identity's validated settings of the running application."""
    return settings_of(IdentitySettings)
