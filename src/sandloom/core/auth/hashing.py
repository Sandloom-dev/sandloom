"""Password hashing: argon2id with configured cost parameters, run on a
bounded executor so a login burst never starves the event loop."""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar, cast

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from prometheus_client import Counter

from sandloom.core.config.password_params import PasswordHashParams
from sandloom.core.config.settings import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Password hashing (argon2id via argon2-cffi)
# --------------------------------------------------------------------------- #
# The DEFAULT parameters (``PASSWORD_HASH_PROFILE=strong``) are exactly the
# ones the former passlib ``CryptContext(schemes=["argon2"])`` produced (it
# delegated to argon2-cffi's defaults), pinned so an argon2-cffi upgrade cannot
# silently change the work factor: ``$argon2id$v=19$m=65536,t=3,p=4`` with a
# 16-byte salt and a 32-byte digest. The cost parameters are configurable
# (``PASSWORD_HASH_PROFILE`` / ``_TIME_COST`` / ``_MEMORY_KIB`` /
# ``_PARALLELISM``, see :mod:`sandloom.core.config.password_params`); salt and digest
# length are not. Existing hashes (any argon2 variant/parameters) verify; a
# hash with other parameters reports :func:`password_needs_rehash` and is
# re-hashed by :func:`rehash_password_if_needed` after a successful login.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST_KIB = 65536
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16

password_rehash_total = Counter(
    "sandloom_password_rehash_total",
    "Stored password hashes re-hashed to the configured argon2 parameters "
    "after a successful login (outcome=rehashed|failed)",
    ["outcome"],
)


def configured_password_hash_params(cfg: Any = None) -> PasswordHashParams:
    """The effective argon2id parameters of ``cfg`` (default: settings)."""
    return cast(PasswordHashParams, (cfg or settings).password_hash_params)


def build_password_hasher(params: PasswordHashParams | None = None) -> PasswordHasher:
    """An argon2id hasher for ``params`` (default: the configured ones), with
    the framework's fixed 16-byte salt / 32-byte digest."""
    p = params or configured_password_hash_params()
    return PasswordHasher(
        time_cost=p.time_cost,
        memory_cost=p.memory_kib,
        parallelism=p.parallelism,
        hash_len=ARGON2_HASH_LEN,
        salt_len=ARGON2_SALT_LEN,
    )


_DEFAULT_HASHER: list[PasswordHasher] = []
_DEFAULT_HASHER_LOCK = threading.Lock()


def default_password_hasher() -> PasswordHasher:
    """The framework's password hasher (argon2id, configured parameters).

    Built on first use, never at import (spec §2.4: importing reads no
    settings); identity's AuthService uses one built from the same settings.
    """
    if not _DEFAULT_HASHER:
        with _DEFAULT_HASHER_LOCK:
            if not _DEFAULT_HASHER:
                _DEFAULT_HASHER.append(build_password_hasher())
    return _DEFAULT_HASHER[0]


def hash_password(password: str, *, hasher: PasswordHasher | None = None) -> str:
    """Hash a password (argon2id). CPU/memory-hard — offload from async code
    with :func:`run_password_hashing`."""
    return str((hasher or default_password_hasher()).hash(password))


def verify_password(
    plain_password: str,
    hashed_password: str,
    *,
    hasher: PasswordHasher | None = None,
) -> bool:
    """Verify a password against an argon2 hash.

    ``False`` on a mismatch; a string that is not an argon2 hash at all raises
    ``ValueError`` (``argon2.exceptions.InvalidHashError``), as before.
    """
    try:
        return bool(
            (hasher or default_password_hasher()).verify(
                hashed_password, plain_password
            )
        )
    except VerificationError:  # mismatch / undecodable parameters
        return False


def password_needs_rehash(
    hashed_password: str, *, hasher: PasswordHasher | None = None
) -> bool:
    """True when the hash was made with other parameters than the current ones
    (or is not an argon2 hash): re-hash it after the next successful verify."""
    try:
        return bool(
            (hasher or default_password_hasher()).check_needs_rehash(hashed_password)
        )
    except (InvalidHashError, ValueError):
        return True


def _available_cpus() -> int:
    """CPUs this process may use: affinity mask capped by a cgroup v2 quota."""
    try:
        cpus = len(os.sched_getaffinity(0))  # type: ignore[attr-defined,unused-ignore]
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    try:
        with open("/sys/fs/cgroup/cpu.max", encoding="ascii") as fh:
            quota, period = fh.read().split()[:2]
        if quota != "max":
            cpus = min(cpus, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return max(1, cpus)


def password_hash_concurrency() -> int:
    """Max concurrent argon2 operations (``PASSWORD_HASH_CONCURRENCY``)."""
    configured = getattr(settings, "password_hash_concurrency", None)
    if configured:
        return int(configured)
    raw_workers = os.environ.get("SANDLOOM_WORKER_COUNT") or os.environ.get(
        "WEB_CONCURRENCY", "1"
    )
    try:
        workers = max(1, int(raw_workers))
    except ValueError:
        workers = 1
    return max(1, min(4, _available_cpus() // workers))


_hash_executor: ThreadPoolExecutor | None = None
_hash_executor_lock = threading.Lock()


def _password_executor() -> ThreadPoolExecutor:
    global _hash_executor
    if _hash_executor is None:
        with _hash_executor_lock:
            if _hash_executor is None:
                _hash_executor = ThreadPoolExecutor(
                    max_workers=password_hash_concurrency(),
                    thread_name_prefix="sandloom-argon2",
                )
    return _hash_executor


_T = TypeVar("_T")


async def run_password_hashing(fn: Callable[..., _T], *args: Any) -> _T:
    """Run a password hash/verify ``fn(*args)`` off the event loop, BOUNDED.

    A dedicated pool of :func:`password_hash_concurrency` threads (not the
    loop's default executor, which allows ``min(32, cpus + 4)``): each argon2
    call holds ~64 MiB for its duration, so a login burst queues here instead
    of multiplying the process RSS and starving every other request's CPU.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _password_executor(), functools.partial(fn, *args)
    )


async def ahash_password(password: str) -> str:
    """:func:`hash_password` on the bounded password executor."""
    return await run_password_hashing(hash_password, password)


async def averify_password(plain_password: str, hashed_password: str) -> bool:
    """:func:`verify_password` on the bounded password executor."""
    return await run_password_hashing(verify_password, plain_password, hashed_password)


async def rehash_password_if_needed(
    plain_password: str,
    stored_hash: str,
    *,
    hasher: PasswordHasher | None = None,
) -> str | None:
    """A new hash of ``plain_password`` when ``stored_hash`` was made with
    other parameters than the configured ones (stronger OR weaker), else
    ``None``.

    Call it ONLY after ``plain_password`` verified against ``stored_hash``,
    and store the result in the same Unit of Work. Never raises: a failure is
    logged (no secret material) and counted
    (``sandloom_password_rehash_total{outcome="failed"}``) and returns ``None`` —
    a rehash must never block the login it rides on.
    """
    h = hasher or default_password_hasher()
    try:
        if not password_needs_rehash(stored_hash, hasher=h):
            return None
        new_hash = await run_password_hashing(
            functools.partial(hash_password, hasher=h), plain_password
        )
    except Exception as exc:  # noqa: BLE001 - never block the login
        password_rehash_total.labels(outcome="failed").inc()
        logger.warning(
            "password rehash failed (%s); keeping the stored hash",
            type(exc).__name__,
        )
        return None
    password_rehash_total.labels(outcome="rehashed").inc()
    return new_hash
