"""Token management service."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NoReturn, cast
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import ColumnElement, and_, bindparam, delete, true, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from sandloom.sdk import record_audit
from sandloom.sdk.infra import settings
from sandloom.sdk.security import create_access_token, ensure_user_not_blocked
from sandloom_identity.claims import (
    claims_from_codes,
    contribute_identity_claims,
    identity_claims_lateral,
)
from sandloom_identity.errors import CommittedHTTPError
from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.models.user import User
from sandloom_identity.schemas.auth import TokenPair
from sandloom_identity.utils import RefreshTokenUtils

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.sdk.infra import CoreRuntime


#: The rotation's compare-and-set consume (``is_revoked = false`` → ``true``)
#: as a data-modifying CTE, joined to ``users``: the row comes back only when
#: THIS call flipped the flag, together with the user the new pair is for.
#: Built once; the token id and ``now`` are bound per call.
_consumed = (
    update(RefreshToken)
    .where(
        RefreshToken.id == bindparam("b_token_id"),
        RefreshToken.is_revoked.is_(False),
    )
    .values(is_revoked=True, revoked_at=bindparam("b_now"))
    .returning(RefreshToken.user_id)
    .cte("consumed_refresh_token")
)
#: Identity's claims (roles / permissions) come back in the same statement —
#: the contributor's own aggregate, LATERAL-joined to the user — and are handed
#: to claims collection as precomputed for ``contribute_identity_claims``.
_identity_claims = identity_claims_lateral(User.id)
_CONSUME_AND_LOAD_USER = (
    select(User, _identity_claims.c.role_codes, _identity_claims.c.permission_codes)
    .join(_consumed, User.id == _consumed.c.user_id)
    .join(_identity_claims, true())
)


class TokenService:
    """Token management service."""

    #: Grace window after a rotation in which presenting the just-rotated
    #: token is treated as a benign concurrent refresh (two tabs / a client
    #: retry after a lost response), not as theft. See :meth:`refresh_token`.
    ROTATION_GRACE_SECONDS = 10

    @classmethod
    async def create_tokens(
        cls,
        user: User,
        db: AsyncSession,
        core: "CoreRuntime | None" = None,
        *,
        family_id: UUID | None = None,
        auth_time: datetime | None = None,
        block_checked: bool = False,
        identity_claims: dict[str, Any] | None = None,
    ) -> TokenPair:
        """Create access and refresh tokens for user.

        When ``core`` is provided, every registered claims contributor runs and
        its claims (roles, permissions, subscription, …) are embedded in the
        access token. This is the cold path that makes claim-based
        authorization possible with zero per-request I/O.

        ``family_id`` continues an existing rotation family (refresh); omitted,
        a fresh family starts (a new login).

        ``auth_time`` is the instant of the primary authentication, embedded as
        the access token's ``auth_time`` claim (epoch seconds) and stored on
        the refresh token. A new family (a login) defaults it to now; a
        refresh passes the stored value, so it never moves forward — step-up
        re-authentication (:mod:`sandloom_identity.services.reauth`) relies on it.
        ``None`` while continuing a family without one omits the claim (that
        family is then refused at its next refresh: sign in again).

        Refuses an emergency-blocked user (``401 "Account blocked"``, live
        check) before anything is minted — the backstop for every issuance
        path (password / SSO / 2FA login, refresh, password change).
        ``block_checked=True``: the caller already made that live check in
        this request (saves a second cache round trip).

        ``identity_claims``: what ``contribute_identity_claims`` returns for
        ``user``, already read in this session (the refresh CAS) — claims
        collection then skips that query; identity is still stamped in
        ``claims_sources``.
        """
        if not block_checked:
            await ensure_user_not_blocked(user.id)
        if family_id is None and auth_time is None:
            auth_time = datetime.now(UTC)

        # Authorization claims (roles/permissions/entitlements) embedded at mint.
        # Identity's OWN contributors run in this (possibly uncommitted) session
        # — e.g. roles assigned during registration; every other module's
        # contributors run in that module's own Unit of Work, locally or over
        # RPC (SERVICE_BINDINGS ``<module>.claims.v1``). A critical contributor
        # failure raises ClaimsUnavailable (503) — never a reduced token.
        extra_claims = (
            await core.collect_claims(
                user.id,
                db,
                issuer="identity",
                precomputed=(
                    {contribute_identity_claims: identity_claims}
                    if identity_claims is not None
                    else None
                ),
            )
            if core
            else {}
        )

        # Create access token (token_type lets guards reject non-access JWTs).
        # ``email_verified`` lets other modules (e.g. tenancy invitation accept)
        # trust ``email`` only when the user proved ownership of the address.
        claims: dict[str, Any] = {
            **extra_claims,
            "sub": str(user.id),
            "email": user.email,
            "email_verified": user.is_verified is True,
            "token_type": "access",
        }
        if auth_time is not None:
            claims["auth_time"] = int(auth_time.timestamp())
        access_token = create_access_token(data=claims)

        # Create refresh token
        refresh_token_value = secrets.token_urlsafe(32)

        # Store only the hash; the raw value is returned to the client once.
        refresh_token = RefreshToken(
            user_id=user.id,
            family_id=family_id or uuid4(),
            auth_time=auth_time,
            token=RefreshTokenUtils.hash_token(refresh_token_value),
            expires_at=min(
                datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
                (auth_time or datetime.now(UTC))
                + timedelta(days=settings.max_session_days),
            ),
        )

        db.add(refresh_token)
        await db.flush()

        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token_value,
            token_type="bearer",  # noqa: S106 - OAuth token type, not a secret
            expires_in=settings.access_token_expire_minutes * 60,
        )

    @classmethod
    async def revoke_family(cls, family_id: UUID, db: AsyncSession) -> int:
        """Revoke every still-active token of a rotation family."""
        result = await db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.family_id == family_id,
                RefreshToken.is_revoked.is_(False),
            )
            .values(is_revoked=True, revoked_at=datetime.now(UTC))
        )
        await db.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    @classmethod
    async def refresh_token(
        cls, refresh_token: str, db: AsyncSession, core: "CoreRuntime | None" = None
    ) -> TokenPair:
        """Rotate a refresh token: one-time use with reuse detection.

        * The presented token is consumed with an atomic compare-and-set
          (``UPDATE … WHERE is_revoked = false RETURNING``), so two concurrent
          refreshes of the same token cannot both succeed.
        * **Rotation grace window.** Presenting a token that was rotated at
          most :attr:`ROTATION_GRACE_SECONDS` ago — or losing the CAS race to a
          concurrent refresh — while its family still has a live successor is
          a benign race (two tabs, a retry after a lost response): it fails
          with ``409`` ("refresh token already rotated; retry with the new
          token") WITHOUT revoking anything. No token is issued, so a thief
          gains nothing; the successor is never re-sent (only its hash is
          stored).
        * Beyond the window (or when the family has no live successor, e.g.
          after logout / a family revocation) a revoked token is reuse = token
          theft: the whole rotation family is revoked and the request fails
          with a ``CommittedHTTPError`` so the revocation commits even though
          the request is rejected. A thief that rotated first is therefore
          caught as soon as the legitimate client presents its old token after
          the window.
        """
        now = datetime.now(UTC)
        token_hash = RefreshTokenUtils.hash_token(refresh_token)

        result = await db.execute(
            select(RefreshToken).where(RefreshToken.token == token_hash)
        )
        token_obj = result.scalar_one_or_none()

        if token_obj is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )

        family_id = token_obj.family_id
        auth_time = token_obj.auth_time
        token_id = token_obj.id
        user_id = token_obj.user_id

        # An emergency-blocked user gets nothing — checked live BEFORE any
        # rotation / reuse-detection side effect touches the family.
        await ensure_user_not_blocked(user_id)

        # Sliding token expiry never extends the primary sign-in indefinitely.
        # A family without a trustworthy primary auth instant must sign in
        # again instead of acquiring a fresh lifetime on every refresh.
        if auth_time is None or now >= auth_time + timedelta(
            days=settings.max_session_days
        ):
            await db.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == family_id)
                .values(is_revoked=True, revoked_at=now)
            )
            raise CommittedHTTPError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session lifetime expired; sign in again",
            )

        if token_obj.is_revoked:
            await cls._reject_rotated(token_id, family_id, user_id, now, db)

        if token_obj.expires_at <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )

        # Atomic consume (CAS): only one concurrent caller can flip the flag.
        # The user the new pair is minted for comes back in the SAME
        # statement (the UPDATE is a data-modifying CTE joined to ``users``),
        # one round trip instead of the consume plus a user SELECT — and so do
        # the user's roles / permissions (identity's claims).
        consumed = (
            await db.execute(
                _CONSUME_AND_LOAD_USER, {"b_token_id": token_id, "b_now": now}
            )
        ).one_or_none()
        if consumed is None:
            # Lost the CAS race to a concurrent refresh of the same token.
            await cls._reject_rotated(token_id, family_id, user_id, now, db)
        user, role_codes, permission_codes = consumed
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is not active"
            )

        # Create new tokens in the same family (re-embeds fresh claims; the
        # primary authentication instant is carried over unchanged).
        return await cls.create_tokens(
            user,
            db,
            core,
            family_id=family_id,
            auth_time=auth_time,
            block_checked=True,
            identity_claims=claims_from_codes(role_codes, permission_codes),
        )

    @classmethod
    async def _reject_rotated(
        cls,
        token_id: UUID,
        family_id: UUID,
        user_id: UUID,
        now: datetime,
        db: AsyncSession,
    ) -> NoReturn:
        """A revoked token was presented: benign race (409) or reuse (revoke)."""
        if await cls._within_rotation_grace(token_id, family_id, now, db):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Refresh token already rotated; retry with the new token",
            )
        await cls._on_reuse(family_id, user_id, db)

    @classmethod
    async def _within_rotation_grace(
        cls, token_id: UUID, family_id: UUID, now: datetime, db: AsyncSession
    ) -> bool:
        """Rotated ≤ grace ago AND the same family still has a live successor."""
        revoked_at = (
            await db.execute(
                select(RefreshToken.revoked_at).where(RefreshToken.id == token_id)
            )
        ).scalar_one_or_none()
        if revoked_at is None:
            return False
        if now - revoked_at > timedelta(seconds=cls.ROTATION_GRACE_SECONDS):
            return False
        successor = (
            await db.execute(
                select(RefreshToken.id)
                .where(
                    RefreshToken.family_id == family_id,
                    RefreshToken.id != token_id,
                    RefreshToken.is_revoked.is_(False),
                    RefreshToken.expires_at > now,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return successor is not None

    @classmethod
    async def _on_reuse(
        cls, family_id: UUID, user_id: UUID, db: AsyncSession
    ) -> NoReturn:
        """Revoke the family, audit, and fail (committing the revocation)."""
        revoked = await cls.revoke_family(family_id, db)
        record_audit(
            "identity.refresh_token.reuse_detected",
            actor_id=user_id,
            resource_type="refresh_token_family",
            resource_id=str(family_id),
            data={"revoked": revoked},
            required=True,
        )
        raise CommittedHTTPError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    @classmethod
    async def revoke_token(cls, refresh_token: str, db: AsyncSession) -> None:
        """Revoke refresh token."""

        result = await db.execute(
            select(RefreshToken).where(
                RefreshToken.token == RefreshTokenUtils.hash_token(refresh_token)
            )
        )
        token_obj = result.scalar_one_or_none()

        if token_obj:
            token_obj.is_revoked = True
            token_obj.revoked_at = datetime.now(UTC)
            await db.flush()

    @classmethod
    async def revoke_all_user_tokens(cls, user_id: UUID, db: AsyncSession) -> None:
        """Revoke all refresh tokens for user."""

        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.is_revoked.is_(False))
            .values(is_revoked=True, revoked_at=datetime.now(UTC))
        )
        await db.flush()

    @classmethod
    async def cleanup_expired_tokens(
        cls, db: AsyncSession, *, limit: int | None = None
    ) -> int:
        """Remove expired refresh tokens.

        ``limit`` bounds one call to that many rows (``id IN (SELECT … LIMIT
        n)`` over ``ix_refresh_tokens_expires_at``) so the caller can purge in
        short transactions; ``None`` deletes every expired token at once.
        """
        return await cls._delete_tokens(
            db, RefreshToken.expires_at < datetime.now(UTC), limit
        )

    @classmethod
    async def cleanup_revoked_tokens_by_age(
        cls, db: AsyncSession, days_old: int = 30, *, limit: int | None = None
    ) -> int:
        """Clean up old revoked refresh tokens.

        Args:
            db: Database session
            days_old: Delete tokens revoked more than this many days ago
            limit: Delete at most this many rows in this call (``None``: all)

        Returns:
            Number of deleted tokens

        ``revoked_at`` has no index (rotation revokes a token on every refresh;
        an index would make that UPDATE non-HOT). A token is revoked after it
        was minted and expires ``REFRESH_TOKEN_EXPIRE_DAYS`` after minting, so
        ``revoked_at < cutoff`` implies ``expires_at < cutoff + TTL``: that
        bound lets ``ix_refresh_tokens_expires_at`` narrow the scan. It only
        ever excludes rows (a token minted under a longer TTL waits for the
        expired-token cleanup), never adds one.
        """
        cutoff_time = datetime.now(UTC) - timedelta(days=days_old)
        ttl = timedelta(days=settings.refresh_token_expire_days)
        return await cls._delete_tokens(
            db,
            and_(
                RefreshToken.revoked_at < cutoff_time,
                RefreshToken.expires_at < cutoff_time + ttl,
            ),
            limit,
        )

    @staticmethod
    async def _delete_tokens(
        db: AsyncSession, criterion: ColumnElement[bool], limit: int | None
    ) -> int:
        if limit is not None:
            if limit <= 0:
                return 0
            victims = (
                select(RefreshToken.id).where(criterion).limit(limit).scalar_subquery()
            )
            criterion = RefreshToken.id.in_(victims)
        result = cast(
            "CursorResult[Any]", await db.execute(delete(RefreshToken).where(criterion))
        )
        await db.flush()
        return int(result.rowcount)
