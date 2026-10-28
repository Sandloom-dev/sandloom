"""Authentication service."""

import asyncio
import concurrent.futures
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, status
from sqlalchemy import and_, bindparam
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from sandloom.sdk import on_commit, record_audit
from sandloom.sdk.effects import mark_reauthenticated, sensitive_effect
from sandloom.sdk.infra import settings
from sandloom.sdk.security import (
    decode_token,
    ensure_user_not_blocked,
    rehash_password_if_needed,
    run_password_hashing,
)
from sandloom.sdk.security import password_hasher as configured_password_hasher
from sandloom_identity import lockout
from sandloom_identity.errors import CommittedHTTPError
from sandloom_identity.events import (
    EventActor,
    create_user_email_confirmed_event,
    create_user_logged_in_event,
    create_user_logged_out_event,
    create_user_password_reset_event,
    create_user_registered_event,
)
from sandloom_identity.integrations import email_sender
from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.models.twofa_secret import TwoFASecret
from sandloom_identity.models.user import User
from sandloom_identity.password_policy import password_policy_violation
from sandloom_identity.schemas.auth import LoginIn, RegisterIn, TokenPair
from sandloom_identity.services import twofa_service  # cycle: twofa -> reauth -> auth
from sandloom_identity.services.token_service import TokenService
from sandloom_identity.sessions import revoke_sessions
from sandloom_identity.utils import (
    RefreshTokenUtils,
    email_token_utils,
    secret_fingerprint,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.sdk.infra import CoreRuntime

logger = logging.getLogger("sandloom_identity.auth")


class _ConfiguredHasher:
    """Class attribute that builds the configured hasher on first access.

    It then replaces itself on the owning class with the plain hasher, so
    later reads (and test swaps via ``monkeypatch.setattr``) see an ordinary
    attribute.
    """

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name

    def __get__(self, obj: object, owner: type) -> PasswordHasher:
        hasher: PasswordHasher = configured_password_hasher()
        setattr(owner, self._name, hasher)
        return hasher


_BAD_CREDENTIALS = "Incorrect email or password"

#: Emails being delivered in the background (strong refs until done).
_PENDING_EMAILS: set["asyncio.Task[None]"] = set()


async def _email_after_commit(send: Callable[[], Awaitable[None]]) -> None:
    """Send an email once the UoW COMMITS, off the request/commit path.

    Never inline: the response time of registration / password reset /
    resend-verification must not depend on whether a mail goes out (i.e. on
    whether the account exists), and a change that rolls back must send no
    link. ``on_commit`` runs before the response, so it only SPAWNS the
    delivery; best-effort like the sender itself (failures are logged).
    """

    async def _deliver() -> None:
        try:
            await send()
        except Exception as exc:  # noqa: BLE001 - never surfaces to a request
            logger.warning("identity: email delivery failed (%s)", type(exc).__name__)

    def _spawn() -> None:
        task = asyncio.get_running_loop().create_task(_deliver())
        _PENDING_EMAILS.add(task)
        task.add_done_callback(_PENDING_EMAILS.discard)

    await on_commit(_spawn)


#: How long shutdown waits for in-flight emails before giving up (a hung mail
#: server must not hang the process stop; the rest are logged and dropped).
EMAIL_SHUTDOWN_DRAIN_SECONDS = 10.0


async def drain_pending_emails(timeout: float | None = None) -> bool:  # noqa: ASYNC109 - explicit per-call timeout is the API
    """Wait for the background email deliveries spawned so far.

    Returns False when ``timeout`` (seconds) elapsed with deliveries still in
    flight (they are left running, not cancelled); ``None`` waits for all.
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while _PENDING_EMAILS:
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            return False
        await asyncio.wait(list(_PENDING_EMAILS), timeout=remaining)
    return True


async def drain_emails_on_shutdown() -> None:
    """Identity shutdown task: let in-flight emails finish, bounded by
    :data:`EMAIL_SHUTDOWN_DRAIN_SECONDS`."""
    if not await drain_pending_emails(timeout=EMAIL_SHUTDOWN_DRAIN_SECONDS):
        logger.warning(
            "identity: shutdown drained emails for %.1fs; %d still in flight "
            "are dropped",
            EMAIL_SHUTDOWN_DRAIN_SECONDS,
            len(_PENDING_EMAILS),
        )


_BAD_OTP = "Invalid 2FA code"

#: Upper bound a password-pool thread waits for the login's account lookup
#: (:meth:`AuthService._verify_login_password`). The lookup is one indexed
#: query on an already checked-out connection; the bound only keeps a stuck
#: lookup from parking a pool thread forever (the login then fails closed).
_LOOKUP_HANDOFF_TIMEOUT_SECONDS = 30.0


#: The password login's lookup: the user by email + its ENABLED TOTP secret
#: (``None`` without 2FA), one statement, built once (bound ``email``).
_LOGIN_LOOKUP = (
    select(User, TwoFASecret)
    .outerjoin(
        TwoFASecret,
        and_(TwoFASecret.user_id == User.id, TwoFASecret.is_enabled.is_(True)),
    )
    .where(User.email == bindparam("email"))
)


def _discard_result(task: "asyncio.Future[bool]") -> None:
    """Retrieve an abandoned verify's outcome (no 'never retrieved' warning)."""
    if not task.cancelled():
        task.exception()


class AuthService:
    """Authentication service."""

    #: argon2id hasher with the CONFIGURED parameters (``PASSWORD_HASH_*``;
    #: default m=64 MiB, t=3, p=4, identical to the former passlib
    #: ``CryptContext(["argon2"])``). Stored hashes embed their parameters, so
    #: every one keeps verifying after a change; :meth:`login` re-hashes a
    #: hash made with other parameters. Tests may swap it for a cheap one.
    #: Built on first access, never at import (no settings read at import).
    password_hasher: PasswordHasher = _ConfiguredHasher()  # type: ignore[assignment]

    @classmethod
    def get_instance(cls) -> "AuthService":
        """Get service instance."""
        return cls()

    #: ``(hasher, hash)`` — the timing-equalizer hash of the current hasher.
    _DUMMY_HASH: tuple[PasswordHasher, str] | None = None

    @classmethod
    def _dummy_password_hash(cls) -> str:
        """Static hash used to equalize timing for unknown emails.

        Made by the CURRENT hasher, so verifying against it costs exactly what
        verifying a real stored hash costs.
        """
        cached = cls._DUMMY_HASH
        if cached is None or cached[0] is not cls.password_hasher:
            hasher = cls.password_hasher
            cached = (hasher, hasher.hash("timing-equalizer-dummy"))
            cls._DUMMY_HASH = cached
        return cached[1]

    @classmethod
    def _verify_against_dummy(cls, plain_password: str) -> bool:
        """Verify ``plain_password`` against the timing-equalizer hash.

        Runs inside the login's single password-pool call
        (:meth:`_verify_login_password`): the dummy hash is read (or, cold,
        made) in the same worker-thread call as the verify, so an unknown
        email queues on the bounded pool exactly once, like a known one. Two hops
        (fetch the hash, then verify) would make it wait about twice as long
        under a login burst — the timing side channel the dummy exists to
        close. Warm with :meth:`warm_dummy_password_hash` at startup so the
        first unknown email does not also pay the hash.
        """
        return cls.verify_password(plain_password, cls._dummy_password_hash())

    @classmethod
    def _verify_login_password(
        cls,
        plain_password: str,
        stored_hash: "concurrent.futures.Future[str | None]",
    ) -> bool:
        """The login's ONE password-pool call; runs before its hash is known.

        :meth:`login` submits this BEFORE it looks the account up, so the
        place a login takes in the bounded pool's queue is decided by work
        that is identical for every email. Submitted after the lookup, a
        burst of simultaneous logins queued in the order their lookups
        finished, and a real account's lookup (index hit, heap fetch, ORM row)
        is systematically a little slower than a miss: under a burst that
        sub-millisecond gap became a whole argon2 slot (~hundreds of ms) of
        extra queueing for known emails. ``stored_hash`` resolves to the
        account's hash, or ``None`` for an unknown email (verified against the
        dummy hash, same cost); cancelled or timed out -> mismatch.
        """
        try:
            hashed = stored_hash.result(timeout=_LOOKUP_HANDOFF_TIMEOUT_SECONDS)
        except (concurrent.futures.CancelledError, TimeoutError):
            return False
        if hashed is None:
            return cls._verify_against_dummy(plain_password)
        return cls.verify_password(plain_password, hashed)

    @classmethod
    async def warm_dummy_password_hash(cls) -> None:
        """Startup task: make the dummy hash off the event loop, once."""
        await run_password_hashing(cls._dummy_password_hash)

    @classmethod
    def verify_password(cls, plain_password: str, hashed_password: str) -> bool:
        """Verify password against hash.

        CPU- and memory-hard (argon2). Async callers MUST run it off the event
        loop via :meth:`averify_password` (the bounded password pool) so a
        burst of logins cannot stall the worker's loop for every other request.
        """
        try:
            return bool(cls.password_hasher.verify(hashed_password, plain_password))
        except VerificationError:
            return False  # wrong password
        except Exception:  # noqa: BLE001 - malformed hash = failed verification
            # Malformed/empty hash must behave like a failed verification,
            # taking comparable time.
            try:
                cls.password_hasher.verify(cls._dummy_password_hash(), plain_password)
            except Exception:  # noqa: BLE001, S110 - always a mismatch
                pass
            return False

    @classmethod
    def needs_rehash(cls, hashed_password: str) -> bool:
        """True when ``hashed_password`` was made with other argon2 parameters
        than the current hasher's (or is not an argon2 hash at all)."""
        try:
            return bool(cls.password_hasher.check_needs_rehash(hashed_password))
        except ValueError:  # InvalidHashError: not an argon2 hash
            return True

    @classmethod
    def get_password_hash(cls, password: str) -> str:
        """Hash password (argon2id). Offload from async code — see verify_password."""
        return str(cls.password_hasher.hash(password))

    @classmethod
    async def averify_password(cls, plain_password: str, hashed_password: str) -> bool:
        """Async wrapper: argon2 verify on the BOUNDED password pool
        (``PASSWORD_HASH_CONCURRENCY``), never the default executor."""
        return await run_password_hashing(
            cls.verify_password, plain_password, hashed_password
        )

    @classmethod
    async def ahash_password(cls, password: str) -> str:
        """Async wrapper: argon2 hashing on the bounded password pool."""
        return await run_password_hashing(cls.get_password_hash, password)

    # --- Registration -------------------------------------------------------

    @classmethod
    async def register(
        cls, user_data: RegisterIn, db: AsyncSession, actor: EventActor | None = None
    ) -> User | None:
        """Register a new user; ``None`` when the email is already taken.

        The HTTP layer answers both outcomes identically (no enumeration). For
        an existing address the owner is notified instead, and the password is
        still hashed so both paths take comparable time. A concurrent duplicate
        insert (unique violation) takes the same path instead of a 500.
        """
        result = await db.execute(select(User).where(User.email == user_data.email))
        existing = result.scalar_one_or_none()
        if existing is not None:
            await cls.ahash_password(user_data.password)  # timing equalizer
            await cls._notify_registration_attempt(existing)
            return None

        user = User(
            email=user_data.email,
            full_name=user_data.full_name,
            hashed_password=await cls.ahash_password(user_data.password),
            is_active=True,
            is_verified=False,
        )
        try:
            async with db.begin_nested():
                db.add(user)
                await db.flush()  # assign user ID; the request-scoped UoW commits
        except IntegrityError:
            # Lost a race with a concurrent registration of the same email.
            return None
        await db.refresh(user)

        await cls._send_verification_email(user.email, user.id, user.full_name)

        event = create_user_registered_event(
            user_id=user.id, actor=actor or EventActor()
        )
        await event.publish()

        return user

    @classmethod
    async def _notify_registration_attempt(cls, user: User) -> None:
        if not user.is_active:
            email = user.email
            await _email_after_commit(
                lambda: email_sender.send_security_notice(
                    email,
                    "A registration was requested for your deactivated account. "
                    "Contact your platform administrator to request reactivation.",
                )
            )
            return
        to_email, full_name = user.email, user.full_name
        await _email_after_commit(
            lambda: email_sender.send_registration_attempt_email(
                to_email=to_email,
                full_name=full_name,
                sign_in_url=f"{settings.frontend_url}/auth/login",
                reset_url=f"{settings.frontend_url}/auth/forgot-password",
            )
        )

    # --- Login --------------------------------------------------------------

    @classmethod
    async def _fail_credentials(
        cls,
        subject: str,
        user: User | None,
        reason: str,
        *,
        client_ip: str | None = None,
    ) -> NoReturn:
        """Count a failed credential check; audit (committed) when it locks.

        A lock is audited and committed whether or not the email has an
        account (``resource_type="login_subject"``, no id, for an unknown
        one): the audit INSERT + COMMIT is the slowest step of a failed
        login, so doing it only for real accounts would let the locking
        attempt reveal that the account exists. Bounded like the lock itself:
        one row per (subject, IP) lock.
        """
        detail = _BAD_CREDENTIALS if reason == "password" else _BAD_OTP
        locked_now = await lockout.register_failure(subject, client_ip=client_ip)
        if locked_now:
            record_audit(
                "identity.login.locked",
                actor_id=None,
                actor_type="system",
                resource_type="user" if user is not None else "login_subject",
                resource_id=str(user.id) if user is not None else None,
                data={"reason": reason},
                required=True,
            )
            # Committed: the audit row must survive the rejected request.
            raise CommittedHTTPError(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=detail
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    @classmethod
    async def login(  # noqa: C901
        cls,
        login_data: LoginIn,
        db: AsyncSession,
        actor: EventActor | None = None,
        core: "CoreRuntime | None" = None,
    ) -> tuple[User, TokenPair]:
        """Authenticate user and return tokens.

        Lockout (see :mod:`sandloom_identity.lockout`) covers both factors. The
        password: a HARD lock per (account, ``actor.ip``) plus a soft
        per-account delay, so an attacker cannot lock the owner out. The
        second factor: a hard lock per USER (any IP) — only a caller holding
        the password reaches it, so a leaked password cannot become a
        distributed TOTP brute force; unknown
        emails are throttled the same way so the lock does not reveal whether
        an account exists. Unverified
        accounts cannot sign in: a password registration only becomes usable
        once the mailbox owner confirms it (pre-registration hijack defence).
        """
        subject = lockout.subject_for_email(str(login_data.email))
        client_ip = actor.ip if actor is not None else None
        had_failures = await lockout.ensure_not_locked(subject, client_ip=client_ip)

        # No eager-loaded roles: claims are resolved by the contributor in
        # one query at mint time; the login itself never reads user.user_roles.
        # The ENABLED TOTP secret comes with the user (LEFT JOIN): one round
        # trip instead of a second query for "is 2FA on?" (and a third to
        # verify the code). It is only read here; nothing below uses it before
        # the password verified.
        #
        # Timing: the argon2 verify is queued on the bounded password pool
        # BEFORE the lookup (see _verify_login_password) and the lookup hands
        # it the hash, so a known and an unknown email take the same place in
        # the queue and do the same Redis, SQL and pool work afterwards. The
        # connection is checked out first so the pool thread waits for one
        # query round trip, never for a database pool checkout.
        await db.connection()
        stored_hash: concurrent.futures.Future[str | None] = concurrent.futures.Future()
        verify = asyncio.ensure_future(
            run_password_hashing(
                cls._verify_login_password, login_data.password, stored_hash
            )
        )
        await asyncio.sleep(0)  # let ``verify`` take its place in the queue now
        try:
            row = (await db.execute(_LOGIN_LOOKUP, {"email": login_data.email})).first()
        except BaseException:
            stored_hash.cancel()  # frees the pool thread at once
            verify.add_done_callback(_discard_result)
            raise
        user, twofa_secret = (row[0], row[1]) if row is not None else (None, None)
        stored_hash.set_result(None if user is None else (user.hashed_password or ""))

        # Unknown emails were verified against the dummy hash (same cost);
        # they fail here like a wrong password.
        if not await verify or user is None:
            await cls._fail_credentials(subject, user, "password", client_ip=client_ip)

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is not active"
            )

        if not user.is_verified:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Email address not verified. Use the link we emailed you "
                    "(or request a new one) before signing in."
                ),
            )

        # Operator-forced reset (``sandloom security force-password-reset`` after a
        # credential leak): the password — possibly cracked from a leaked
        # hash — no longer signs in; only the emailed reset link does. Checked
        # after the password verified, so it reveals nothing to a guesser.
        if user.password_reset_required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Password reset required. Use the password reset link "
                    "(request a new one) before signing in."
                ),
                headers={"X-Password-Reset-Required": "true"},
            )

        # Enforce 2FA when the user has it enabled
        if twofa_secret is not None:
            if not login_data.otp:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="2FA code required",
                    headers={"X-2FA-Required": "true"},
                )
            # The OTP phase is locked per USER across every address (the
            # subject other 2FA checks share): the caller already proved the
            # password, so a per-(email, IP) lock would only let a leaked
            # password fan TOTP guesses out over many IPs.
            otp_subject = lockout.subject_for_user(user.id, "2fa")
            otp_had_failures = await lockout.ensure_not_locked(otp_subject)
            if not await twofa_service.TwoFAService.verify_2fa_code(
                user, login_data.otp, db, secret=twofa_secret
            ):
                await cls._fail_credentials(otp_subject, user, "2fa")
            if otp_had_failures:
                await lockout.reset_failures(otp_subject)

        # Emergency block: refuse before any login side effect (lockout reset,
        # last_login_at, the logged-in event, token minting).
        await ensure_user_not_blocked(user.id)

        # Only a counter that existed needs clearing (read with the lock in
        # one round trip): no DEL for the common clean sign-in.
        if had_failures:
            await lockout.reset_failures(subject, client_ip=client_ip)

        # Transparent work-factor migration: a hash made with other argon2
        # parameters than the configured ones (stronger or weaker) is replaced
        # in this UoW. Never blocks the login (failure -> log + metric).
        new_hash = await rehash_password_if_needed(
            login_data.password, user.hashed_password or "", hasher=cls.password_hasher
        )

        tokens = await TokenService.create_tokens(user, db, core, block_checked=True)

        event = create_user_logged_in_event(
            user_id=user.id, method="password", actor=actor or EventActor()
        )
        await event.publish()

        # Write the user row LAST: the UPDATE row-locks it until commit, so
        # done before minting it held the lock across claims collection
        # (contract reads, RPC when split) and every concurrent login of the
        # same account queued behind it. No explicit flush: the UoW commit
        # writes it together with the outbox row. Same transaction, same
        # outcome.
        if new_hash is not None:
            user.hashed_password = new_hash
        user.last_login_at = datetime.now(UTC)

        return user, tokens

    @classmethod
    async def refresh_token(
        cls, refresh_token: str, db: AsyncSession, core: "CoreRuntime | None" = None
    ) -> TokenPair:
        """Refresh access token."""
        return await TokenService.refresh_token(refresh_token, db, core)

    @classmethod
    async def logout(
        cls, refresh_token: str, db: AsyncSession, actor: EventActor | None = None
    ) -> None:
        """Logout user by revoking refresh token."""
        user_id: UUID | None = None
        try:
            result = await db.execute(
                select(RefreshToken).where(
                    RefreshToken.token == RefreshTokenUtils.hash_token(refresh_token)
                )
            )
            token_obj = result.scalar_one_or_none()
            if token_obj:
                user_id = token_obj.user_id
        except Exception:  # noqa: BLE001, S110 - logout proceeds without the user id
            # Non-critical for logout; proceed
            pass

        await TokenService.revoke_token(refresh_token, db)

        if user_id is not None:
            event = create_user_logged_out_event(
                user_id=user_id, actor=actor or EventActor()
            )
            await event.publish()

    # --- Email verification -------------------------------------------------

    @classmethod
    async def confirm_email(
        cls, token: str, db: AsyncSession, actor: EventActor | None = None
    ) -> User:
        """Confirm user email with token (bound to the address it was sent to)."""
        invalid = HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token",
        )
        try:
            claims = email_token_utils.read_email_verification_token(token)
        except ValueError as e:
            raise invalid from e

        result = await db.execute(select(User).where(User.id == claims.user_id))
        user = result.scalar_one_or_none()

        if user is None:
            raise invalid
        if claims.email_fingerprint is not None and not hmac.compare_digest(
            claims.email_fingerprint, secret_fingerprint(user.email.lower(), "email")
        ):
            raise invalid

        if user.is_verified:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Email already verified"
            )

        user.is_verified = True
        user.email_verified_at = datetime.now(UTC)
        await db.flush()

        event = create_user_email_confirmed_event(
            user_id=user.id, actor=actor or EventActor()
        )
        await event.publish()

        return user

    @classmethod
    async def resend_verification(cls, email: str, db: AsyncSession) -> None:
        """Send a fresh verification link (silently a no-op otherwise)."""
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is not None and user.is_active and not user.is_verified:
            await cls._send_verification_email(user.email, user.id, user.full_name)

    @classmethod
    async def _send_verification_email(
        cls, email: str, user_id: UUID, full_name: str
    ) -> None:
        """Send email verification (token bound to this address)."""
        token = email_token_utils.generate_email_verification_token(
            user_id, email=email
        )
        verification_url = f"{settings.frontend_url}/auth/verify-email?token={token}"
        await _email_after_commit(
            lambda: email_sender.send_verification_email(
                to_email=email, full_name=full_name, verification_url=verification_url
            )
        )

    # --- Password reset / change --------------------------------------------

    @classmethod
    async def forgot_password(cls, email: str, db: AsyncSession) -> None:
        """Email a password reset link (one outstanding link per user)."""
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is not None and user.is_active:
            # Rotating the nonce invalidates every previously emailed link.
            nonce = secrets.token_urlsafe(24)
            user.password_reset_nonce = nonce
            await db.flush()
            token = email_token_utils.generate_password_reset_token(
                user.id, nonce=nonce, hashed_password=user.hashed_password
            )
            reset_url = f"{settings.frontend_url}/auth/reset-password?token={token}"
            to_email, full_name = user.email, user.full_name
            await _email_after_commit(
                lambda: email_sender.send_password_reset_email(
                    to_email=to_email, full_name=full_name, reset_url=reset_url
                )
            )

    @classmethod
    async def reset_password(
        cls,
        token: str,
        new_password: str,
        db: AsyncSession,
        actor: EventActor | None = None,
    ) -> User:
        """Reset password with a single-use token.

        The token must carry the user's current reset nonce (cleared here, so a
        link works once; rotated by every new request, so older links die) and
        the fingerprint of the current password hash (so it dies with any
        password change). Completing a reset proves control of the mailbox, so
        an unverified account becomes verified.
        """
        invalid = HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )
        try:
            claims = email_token_utils.read_password_reset_token(token)
        except ValueError as e:
            raise invalid from e

        # Serialize consumption of the reset nonce. A second concurrent request
        # must re-read the cleared nonce after the first transaction commits.
        result = await db.execute(
            select(User)
            .where(User.id == claims.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        user = result.scalar_one_or_none()

        if (
            user is None
            or not user.is_active
            or claims.nonce is None
            or user.password_reset_nonce is None
            or not hmac.compare_digest(claims.nonce, user.password_reset_nonce)
            or claims.password_fingerprint is None
            or not hmac.compare_digest(
                claims.password_fingerprint,
                secret_fingerprint(user.hashed_password, "password"),
            )
        ):
            raise invalid

        violation = password_policy_violation(new_password, user.email)
        if violation:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=violation
            )

        now = datetime.now(UTC)
        user.hashed_password = await cls.ahash_password(new_password)
        user.password_reset_nonce = None
        user.password_reset_required = False
        user.updated_at = now
        if not user.is_verified:
            user.is_verified = True
            user.email_verified_at = now
        await db.flush()

        await TokenService.revoke_all_user_tokens(user.id, db)
        await revoke_sessions(user.id, reason="password_reset")

        record_audit(
            "identity.password.reset",
            actor_id=user.id,
            resource_type="user",
            resource_id=str(user.id),
            required=True,
        )
        event = create_user_password_reset_event(
            user_id=user.id, actor=actor or EventActor()
        )
        await event.publish()

        return user

    @classmethod
    @sensitive_effect("security", step_up=300)
    async def change_password(
        cls,
        user_id: UUID,
        current_password: str,
        new_password: str,
        db: AsyncSession,
        actor: EventActor | None = None,
        core: "CoreRuntime | None" = None,
    ) -> TokenPair:
        """Change the password of an authenticated user.

        Requires the current password (password-less accounts use the reset
        flow), revokes every refresh token and outstanding access token of the
        user, and returns a fresh token pair for the calling client.
        """
        subject = lockout.subject_for_user(user_id, "pwchange")
        await lockout.ensure_not_locked(subject)

        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
            )
        if not user.hashed_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This account has no password; use the password reset flow",
            )
        if not await cls.averify_password(current_password, user.hashed_password):
            await lockout.register_failure(subject)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is incorrect",
            )
        mark_reauthenticated()  # the current password satisfies the step-up
        violation = password_policy_violation(new_password, user.email)
        if violation:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=violation
            )
        if await cls.averify_password(new_password, user.hashed_password):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="New password must differ from the current one",
            )
        await lockout.reset_failures(subject)

        user.hashed_password = await cls.ahash_password(new_password)
        user.password_reset_nonce = None
        user.password_reset_required = False
        user.updated_at = datetime.now(UTC)
        await db.flush()

        await TokenService.revoke_all_user_tokens(user.id, db)
        await revoke_sessions(
            user.id,
            reason="password_changed",
            actor_id=actor.user_id if actor else None,
        )

        record_audit(
            "identity.password.changed",
            actor_id=user.id,
            resource_type="user",
            resource_id=str(user.id),
            required=True,
        )
        return await TokenService.create_tokens(user, db, core)

    # --- Token → user -------------------------------------------------------

    @classmethod
    async def get_current_user(cls, token: str, db: AsyncSession) -> User | None:
        """Get current user from an ACCESS token."""
        try:
            payload = decode_token(token)
            if not payload or payload.get("token_type") != "access":
                return None

            user_id = payload.get("sub")
            if not user_id:
                return None

            result = await db.execute(select(User).where(User.id == UUID(user_id)))
            user = result.scalar_one_or_none()

            if user and user.is_active:
                return user
            return None
        except Exception:  # noqa: BLE001 - fail closed: unresolvable token = no user
            return None
