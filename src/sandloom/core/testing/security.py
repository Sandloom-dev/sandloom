"""Test seams over the kernel's token-security state and environment.

Module tests may not import kernel internals (``sandloom check`` rule
``test-imports``); these helpers are the sanctioned way for a module's
security tests to reach what they legitimately need:

* :func:`use_security_cache` — make the subject-state records (access-token
  revocations and emergency blocks) live in a given cache provider (e.g. an
  :class:`~sandloom.core.testing.InMemoryCache`, ``broken=True`` for an outage);
* :func:`revocation_mark_key` / :func:`block_mark_key` — the cache key of a
  subject's state record (ADR-0009: one record holds both), to read, age or
  write it in a test; :func:`revoked_after_value` / :func:`blocked_value`
  build the two values (``block_user`` from ``sandloom.core.di`` is the real API);
* :func:`record_revocation_checks` — record every access-token revocation
  check (one per request when the per-request memo works);
* :func:`set_environment` — run the rest of a test as ``dev`` / ``test`` /
  ``prod``.

The helpers that change process state take pytest's ``monkeypatch``: the
change is undone when the test ends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from sandloom.core.auth import verification
from sandloom.core.auth.marks import (
    _format_blocked,
    _format_mark,
    _seconds_to_us,
    subject_key,
)
from sandloom.core.config.common import Environment
from sandloom.core.config.settings import (
    settings,
)
from sandloom.core.infra import access

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pytest

    from sandloom.core.infra.contracts import CacheProvider


def use_security_cache(
    monkeypatch: pytest.MonkeyPatch, provider: CacheProvider
) -> CacheProvider:
    """Resolve the kernel's revocation / block state to ``provider``.

    The revocation marks and the emergency block marks are read and written
    through ``sandloom.core.infra.access.cache`` at call time; for the rest of the
    test every such lookup (any instance name) returns ``provider``.
    """
    monkeypatch.setattr(access, "cache", lambda name="default": provider)
    return provider


def revocation_mark_key(user_id: Any) -> str:
    """The cache key of ``user_id``'s subject-state record.

    A revocation is the value ``v1:after:<epoch seconds>`` (see
    :func:`revoked_after_value`): the subject's access tokens issued at or
    before that instant are revoked. The same key holds a block
    (:func:`block_mark_key`).
    """
    return subject_key(user_id)


def block_mark_key(user_id: Any) -> str:
    """The cache key of ``user_id``'s subject-state record (the block lives there).

    Writing :func:`blocked_value` to it blocks the subject on the very next
    authenticated request, exactly like a committed ``block_user`` (minus its
    audit / revocation / event); deleting it unblocks (``unblock_user`` never
    deletes: it swaps the value for a revocation).
    """
    return subject_key(user_id)


def revoked_after_value(epoch_seconds: float) -> str:
    """The record value revoking every token issued at or before ``epoch_seconds``."""
    return _format_mark(_seconds_to_us(epoch_seconds))


def blocked_value(epoch_seconds: float) -> str:
    """The record value of a subject blocked since ``epoch_seconds``."""
    return _format_blocked(_seconds_to_us(epoch_seconds))


def record_revocation_checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the ``sub`` of every access-token revocation check.

    Wraps the kernel's single revocation lookup (the one every framework
    token guard reaches through the per-request memo): the returned list
    grows by one subject per actual cache lookup, so a request whose guards
    share the memo adds exactly one entry.
    """
    calls: list[str] = []
    original = verification.token_access_status

    async def recording(claims: dict[str, Any], **kwargs: Any) -> str:
        calls.append(str(claims.get("sub")))
        return await original(claims, **kwargs)

    monkeypatch.setattr(verification, "token_access_status", recording)
    return calls


def set_environment(
    monkeypatch: pytest.MonkeyPatch, name: Literal["dev", "test", "prod"]
) -> None:
    """Run the rest of the test with ``settings.environment == name``.

    Behaviour gated on ``settings.is_prod`` / ``is_dev`` (startup guards,
    fail-closed defaults) follows; other settings are left as they are.
    """
    monkeypatch.setattr(settings, "environment", Environment(name))


__all__ = [
    "block_mark_key",
    "blocked_value",
    "record_revocation_checks",
    "revocation_mark_key",
    "revoked_after_value",
    "set_environment",
    "use_security_cache",
]
