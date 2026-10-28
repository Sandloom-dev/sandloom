"""Per-account credential-guessing protection (failure counters + lock).

The IP rate limiter alone does not stop a distributed guessing attack against
one account, but a plain per-account HARD lock lets anyone lock a victim out
by failing on purpose (denial of service). Two tiers therefore apply to the
unauthenticated login (callers pass ``client_ip``):

* **Hard lock per (account, client IP).** Every failed credential check
  (password or second factor) bumps a counter keyed by the subject AND the
  caller's IP; from :data:`FAIL_THRESHOLD` failures inside
  :data:`FAIL_WINDOW_SECONDS` THAT (account, IP) pair is locked for an
  exponentially growing period (capped at :data:`MAX_LOCK_SECONDS`). An
  attacker locks only themself; the owner signing in from another address is
  unaffected.
* **Soft per-account throttle.** A per-account counter across ALL IPs; from
  :data:`GLOBAL_SOFT_THRESHOLD` failures every further attempt on the account
  is DELAYED (:func:`global_delay_for`, linear, capped at
  :data:`MAX_GLOBAL_DELAY_SECONDS`) — never refused — so a distributed attacker
  is slowed down without being able to lock the owner out.

Trade-off: a distributed attacker with many IPs gets ``FAIL_THRESHOLD`` guesses
per IP per window (bounded further by the IP rate limiter and the global
delay), in exchange for an attacker never being able to lock a victim's
account. Strong passwords + 2FA remain the real defence. Without a
``client_ip`` (second factor of an already-identified principal, password
change) the counter is per subject only, as before — those callers already
proved a first factor, so they cannot be used to lock an arbitrary account.

Subjects are opaque hashes (an email is never a cache key in clear). Unknown
emails are counted and locked exactly like real ones, so the lock response does
not reveal whether an account exists.

Cache outage: fail CLOSED in production (503 — a lock that silently stops
working is the vulnerability) and open elsewhere (dev/test without Redis).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from fastapi import HTTPException, status

from sandloom.sdk.infra import cache, settings

logger = logging.getLogger("sandloom_identity.lockout")

FAIL_THRESHOLD = 5
FAIL_WINDOW_SECONDS = 15 * 60
BASE_LOCK_SECONDS = 30
MAX_LOCK_SECONDS = 15 * 60

#: Per-account failures (all IPs, inside the window) before attempts are slowed.
GLOBAL_SOFT_THRESHOLD = 20
GLOBAL_DELAY_STEP_SECONDS = 0.5
MAX_GLOBAL_DELAY_SECONDS = 5.0

LOCKED_DETAIL = "Too many failed attempts. Try again later."

_FAIL_PREFIX = "identity:authfail:"
_LOCK_PREFIX = "identity:authlock:"
_GLOBAL_PREFIX = "identity:authfail_global:"


def _keyed(subject: str, client_ip: str | None) -> str:
    """The hard-lock key: per subject, or per (subject, client IP)."""
    if client_ip is None:
        return subject
    return f"{subject}:ip:{hashlib.sha256(client_ip.encode()).hexdigest()[:32]}"


def global_delay_for(failures: int) -> float:
    """Soft-throttle delay for an account with ``failures`` recent failures."""
    if failures < GLOBAL_SOFT_THRESHOLD:
        return 0.0
    steps = failures - GLOBAL_SOFT_THRESHOLD + 1
    return float(min(GLOBAL_DELAY_STEP_SECONDS * steps, MAX_GLOBAL_DELAY_SECONDS))


def subject_for_email(email: str) -> str:
    """Opaque throttle subject for a login email."""
    return "email:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()


def subject_for_user(user_id: object, purpose: str) -> str:
    """Opaque throttle subject for a per-user secondary check (e.g. 2FA)."""
    return f"{purpose}:{user_id}"


def lock_seconds_for(failures: int) -> int:
    """Lock duration after ``failures`` consecutive failures (0 = no lock)."""
    if failures < FAIL_THRESHOLD:
        return 0
    return int(
        min(BASE_LOCK_SECONDS * (2 ** (failures - FAIL_THRESHOLD)), MAX_LOCK_SECONDS)
    )


async def _get_keys(provider: Any, keys: list[str]) -> list[Any]:
    """Read ``keys`` — one round trip via ``get_many`` when the cache has it."""
    get_many = getattr(provider, "get_many", None)
    if get_many is not None:
        values = await get_many(keys)
        if isinstance(values, dict):
            return [values.get(key) for key in keys]
        return list(values)
    return [await provider.get(key) for key in keys]


async def _incr_two(provider: Any, first: str, second: str) -> tuple[int, int]:
    """Bump two failure counters — one round trip via ``incr_many``."""
    incr_many = getattr(provider, "incr_many", None)
    if incr_many is not None:
        counts = await incr_many([first, second], ttl_seconds=FAIL_WINDOW_SECONDS)
        return int(counts[0]), int(counts[1])
    return (
        await provider.incr(first, ttl_seconds=FAIL_WINDOW_SECONDS),
        await provider.incr(second, ttl_seconds=FAIL_WINDOW_SECONDS),
    )


def _unavailable(exc: Exception) -> None:
    if settings.is_prod:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication temporarily unavailable",
        ) from exc
    logger.warning("identity: lockout cache unavailable; failing open (non-prod)")


async def ensure_not_locked(subject: str, *, client_ip: str | None = None) -> bool:
    """Raise 429 while ``subject`` (from ``client_ip``) is locked.

    With ``client_ip`` the soft per-account throttle also applies: once the
    account crossed :data:`GLOBAL_SOFT_THRESHOLD` recent failures the attempt
    is delayed (never refused).

    Returns whether the (subject, ``client_ip``) failure counter exists, read
    in the same round trip as the lock: a sign-in that succeeds only has
    something for :func:`reset_failures` to clear when it does. ``True`` when
    unknown (cache outage, failing open outside production) so the caller
    still tries the reset. Skipping the reset when the counter was absent
    never weakens the lock: a failure that lands between this read and the
    success is simply kept (the old unconditional delete would have wiped it).
    """
    key = _keyed(subject, client_ip)
    try:
        provider = cache()
        keys = [_LOCK_PREFIX + key, _FAIL_PREFIX + key]
        if client_ip is not None:
            keys.append(_GLOBAL_PREFIX + subject)
        values = await _get_keys(provider, keys)
    except Exception as exc:  # noqa: BLE001 - cache outage: 503 in prod, fail open in dev
        _unavailable(exc)
        return True
    locked, failures = values[0], values[1]
    raw_global = values[2] if client_ip is not None else None
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=LOCKED_DETAIL
        )
    try:
        global_failures = int(raw_global) if raw_global is not None else 0
    except ValueError:
        global_failures = 0
    delay = global_delay_for(global_failures)
    if delay:
        await asyncio.sleep(delay)
    return failures is not None


async def register_failure(subject: str, *, client_ip: str | None = None) -> bool:
    """Count a failure; return True when this failure (newly) locks the key.

    The hard lock is per (subject, ``client_ip``); with ``client_ip`` the
    per-account soft-throttle counter is bumped too.
    """
    key = _keyed(subject, client_ip)
    try:
        provider = cache()
        if client_ip is None:
            failures = await provider.incr(
                _FAIL_PREFIX + key, ttl_seconds=FAIL_WINDOW_SECONDS
            )
        else:
            # Both counters in ONE round trip where the driver fuses it.
            failures, _global = await _incr_two(
                provider, _FAIL_PREFIX + key, _GLOBAL_PREFIX + subject
            )
        lock_for = lock_seconds_for(failures)
        if lock_for:
            await provider.set(_LOCK_PREFIX + key, "1", ttl_seconds=lock_for)
            return True
    except Exception as exc:  # noqa: BLE001 - cache outage: 503 in prod, fail open in dev
        _unavailable(exc)
    return False


async def reset_failures(subject: str, *, client_ip: str | None = None) -> None:
    """Clear the (subject, ``client_ip``) counter after a successful sign-in.

    The per-account soft-throttle counter is NOT reset (it only expires with
    the window): one success must not restart a distributed attack at full
    speed.
    """
    try:
        await cache().delete(_FAIL_PREFIX + _keyed(subject, client_ip))
    except Exception:  # noqa: BLE001 - best effort; the counter has a TTL
        logger.warning("identity: could not reset auth failure counter")


__all__ = [
    "FAIL_THRESHOLD",
    "GLOBAL_SOFT_THRESHOLD",
    "LOCKED_DETAIL",
    "ensure_not_locked",
    "global_delay_for",
    "lock_seconds_for",
    "register_failure",
    "reset_failures",
    "subject_for_email",
    "subject_for_user",
]
