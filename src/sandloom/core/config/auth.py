"""Access tokens (JWT), token revocation and password hashing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AuthConfig(BaseModel):
    """Access tokens (JWT), token revocation and password hashing."""

    # Security / JWT
    # Symmetric (HS256/384/512, the monolith/dev default): JWT_SECRET signs AND
    # verifies, so every process that verifies tokens can also MINT them — fine
    # for one process / one trust domain. Asymmetric (EdDSA / RS256 / ES256):
    # only the issuer (the process loading a token-issuing module, identity)
    # holds JWT_PRIVATE_KEY; every other service verifies with the public keys
    # in JWT_PUBLIC_KEYS (``{"<kid>": "<PEM>"}``, several during a rotation)
    # and cannot mint. Asymmetric mode never accepts an HS token: the key type
    # is bound to the algorithm (sandloom.core.signing_keys) and only
    # JWT_ALGORITHM is in the accepted list (``none`` never is). In that mode
    # JWT_SECRET is issuer-only (identity keys its emailed tokens with it; it
    # never signs or verifies a JWT) and a verify-only process must not hold it.
    jwt_secret: str | None = Field(default=None, alias="JWT_SECRET")
    jwt_algorithm: Literal["HS256", "HS384", "HS512", "EdDSA", "RS256", "ES256"] = (
        Field(default="HS256", alias="JWT_ALGORITHM")
    )
    # Issuer-only PEM private key (SECRETS_DIR-aware: a file ``jwt_private_key``;
    # ``\n``-escaped newlines are accepted in an env var). Its kid is JWT_KID.
    jwt_private_key: str | None = Field(default=None, alias="JWT_PRIVATE_KEY")
    # Verification keys by kid (JSON object of PEM public keys), identical in
    # every process. A token must carry a ``kid`` header naming one of them.
    jwt_public_keys: dict[str, str] = Field(
        default_factory=dict, alias="JWT_PUBLIC_KEYS"
    )
    access_token_expire_minutes: int = Field(
        default=15, alias="ACCESS_TOKEN_EXPIRE_MINUTES"
    )
    refresh_token_expire_days: int = Field(
        default=14, alias="REFRESH_TOKEN_EXPIRE_DAYS"
    )
    max_session_days: int = Field(default=30, ge=1, le=365, alias="MAX_SESSION_DAYS")
    tenant_claims_max_organizations: int = Field(
        default=128, ge=1, le=10000, alias="TENANT_CLAIMS_MAX_ORGANIZATIONS"
    )
    # Lifetime of a token-revocation ("tokens valid after") mark. It must
    # outlive every access token minted before it by ANY service — not only the
    # writer's own ACCESS_TOKEN_EXPIRE_MINUTES (a split deployment where
    # tenancy revokes and identity mints). None = max(own access TTL + skew,
    # 24h); an explicit value is never allowed below own access TTL + skew.
    auth_revocation_mark_ttl_seconds: int | None = Field(
        default=None, gt=0, alias="AUTH_REVOCATION_MARK_TTL_SECONDS"
    )
    # Optional issuer/audience binding. When set, every minted token carries the
    # claim and decode_token VERIFIES it (a token for another issuer/audience is
    # rejected). Left unset (default) they are neither emitted nor verified, so
    # existing single-app deployments are unaffected; set them when multiple
    # services/audiences share a secret and must not accept each other's tokens.
    jwt_issuer: str | None = Field(default=None, alias="JWT_ISSUER")
    jwt_audience: str | None = Field(default=None, alias="JWT_AUDIENCE")
    # Clock-skew tolerance (seconds) applied when verifying time-based claims
    # (exp / nbf / iat), so minor clock drift between minting and verifying
    # processes does not spuriously reject a valid token.
    jwt_leeway_seconds: int = Field(default=10, alias="JWT_LEEWAY_SECONDS")
    # Key rotation. JWT_PREVIOUS_SECRETS (JSON list) holds retired signing
    # secrets that are still ACCEPTED for verification (never used to mint), so
    # rotating JWT_SECRET does not log everybody out: move the old secret here,
    # keep it for at least ACCESS_TOKEN_EXPIRE_MINUTES, then drop it. JWT_KID
    # (optional) is stamped as the ``kid`` header of every minted token; a token
    # whose ``kid`` matches it is verified with JWT_SECRET only, any other token
    # is tried against JWT_SECRET then each previous secret. Asymmetric mode:
    # JWT_KID (required with JWT_PRIVATE_KEY) names the signing key's entry in
    # JWT_PUBLIC_KEYS; JWT_PREVIOUS_SECRETS must be empty.
    jwt_previous_secrets: list[str] = Field(
        default_factory=list, alias="JWT_PREVIOUS_SECRETS"
    )
    jwt_kid: str | None = Field(default=None, alias="JWT_KID")
    # Framework-wide access-token revocation (sandloom.core.auth.revocation): every
    # token
    # check rejects a token whose ``iat`` predates the subject's revocation mark
    # in the cache. When the cache is unreachable the check cannot run: fail
    # CLOSED (503) or OPEN (accept the token). Unset = closed in prod, open
    # elsewhere. The same knob governs the emergency-block check
    # (sandloom.core.auth.blocks.block_user), which runs on every request.
    auth_revocation_fail_closed: bool | None = Field(
        default=None, alias="AUTH_REVOCATION_FAIL_CLOSED"
    )
    # Clock-skew margin of a revocation mark (sandloom.core.auth.marks): a mark is
    # written at ``now + margin`` on the REVOKING process's clock, so a token
    # minted shortly BEFORE the revocation by an issuer whose clock runs up to
    # ``margin`` seconds ahead is still rejected. Trade-off: a token minted
    # within the margin AFTER the revocation by a path that did not read the
    # mark first is rejected too (the client refreshes again). The framework's
    # issuers (identity login / refresh / password change) read the mark in
    # their issuance check and stamp the new token after it, so they never pay
    # it. Keep it identical in every service and >= the worst NTP drift you
    # tolerate; 0 disables it.
    auth_revocation_clock_skew_seconds: float = Field(
        default=2.0, ge=0, le=60, alias="AUTH_REVOCATION_CLOCK_SKEW_SECONDS"
    )
    # After-commit revocation mark writes (revoke_user_tokens_on_commit) are
    # retried this many times (exponential backoff from the base delay) before
    # the revocation is deferred to the durable applier
    # (core.auth.tokens_revoked.v1, auth_revocation_deferred_total).
    auth_revocation_write_attempts: int = Field(
        default=3, ge=1, le=10, alias="AUTH_REVOCATION_WRITE_ATTEMPTS"
    )
    auth_revocation_retry_base_seconds: float = Field(
        default=0.05, ge=0, le=5, alias="AUTH_REVOCATION_RETRY_BASE_SECONDS"
    )
    # Max concurrent argon2 hash/verify operations per process. Each argon2
    # call holds ~64 MiB of memory for its duration, so an unbounded burst of
    # logins (the default executor allows min(32, cpus + 4) at once) inflates
    # RSS and starves the CPU. None = min(4, available CPUs).
    password_hash_concurrency: int | None = Field(
        default=None, ge=1, le=64, alias="PASSWORD_HASH_CONCURRENCY"
    )
    # argon2id work factor (sandloom.core.config.password_params). PROFILE picks a base
    # —
    # ``strong`` (default; m=65536 KiB, t=3, p=4, today's hashes),
    # ``owasp_min`` (OWASP Password Storage Cheat Sheet minimum: m=19456 KiB,
    # t=2, p=1) or ``custom`` (all three values explicit); an explicit value
    # overrides its field of the preset. Stored hashes embed their own
    # parameters, so they keep verifying after a change and are re-hashed to
    # the configured ones on the user's next successful login. Below the OWASP
    # minimum: refused in prod unless SANDLOOM_ACCEPTED_RISKS has
    # "weak_password_hash" (logged loudly at every start); allowed with a
    # warning elsewhere (fast tests).
    password_hash_profile: Literal["strong", "owasp_min", "custom"] = Field(
        default="strong", alias="PASSWORD_HASH_PROFILE"
    )
    password_hash_time_cost: int | None = Field(
        default=None, ge=1, le=100, alias="PASSWORD_HASH_TIME_COST"
    )
    password_hash_memory_kib: int | None = Field(
        default=None, ge=8, le=4 * 1024 * 1024, alias="PASSWORD_HASH_MEMORY_KIB"
    )
    password_hash_parallelism: int | None = Field(
        default=None, ge=1, le=64, alias="PASSWORD_HASH_PARALLELISM"
    )
