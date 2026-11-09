"""The trusted view of a verified access token (ADR-0014 decisions 3 and 4).

:func:`apply_view` runs once per verified access token, inside
:func:`sandloom.core.auth.tokens.decode_token`, and is cached with it (the
policy's ``marker`` is part of the cache key). It removes every protected
claim the composition's map does not trust and lists each under
:data:`UNTRUSTED_KEY` with its reason, so every reader of the verified claims
(``UserContext``, ``Role``, ``Perm``, ``TenantScope``, the tenant allowlist,
claim-mode facts, the idempotency fingerprint and :func:`platform_admission`)
sees only the view.

A protected key K is kept only when one authority A of K satisfies, in all
three parts (the one condition of decision 3):

1. **allowed:** A is K's current authority in the verifier's map, or the one
   ``previous=`` authority the map declares for K;
2. **policy version:** the token's ``cpv`` entry for K equals
   ``cpv_digest(K, A's import path)``;
3. **verified source:** A's short name is in ``claims_sources`` and not in
   ``claims_sources_failed``.

A token without ``cpv`` is checked against parts 1 and 3 only while the
verifier runs the bounded compatibility mode (``legacy_tokens``, G-RISK-14):
its ``iat`` before ``until``, its ``exp - iat`` within
``ACCESS_TOKEN_EXPIRE_MINUTES``, and the time before the window end
(``until + ACCESS_TOKEN_EXPIRE_MINUTES + JWT_LEEWAY_SECONDS``).

Reasons (:data:`OUTDATED` and :data:`MISMATCH` are indeterminate, the others
a definite deny):

* :data:`OUTDATED` (401 ``token_policy_outdated``): the token has no ``cpv``
  outside the compatibility mode, or names for K a digest this map does not
  accept and was issued before the map took effect: one refresh fixes it;
* :data:`MISMATCH` (503 ``claims_policy_mismatch``, logged at ERROR): the
  token was issued after the map took effect and still names another digest:
  issuer and verifier disagree, a refresh would loop;
* :data:`UNASSIGNED` (403): no authority is assigned for K;
* :data:`SOURCE_ABSENT` (403): the authority the digest names did not run at
  the mint, or failed.

The kernel names no module: the map comes from the composition root.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable, Mapping
from functools import partial
from typing import Any

from fastapi import HTTPException, status

from sandloom.core.claims import (
    CPV_CLAIM,
    CPV_VERSION,
    TENANT_SELECTORS,
    ClaimAuthorities,
)

logger = logging.getLogger("sandloom.claims.view")

#: Key the view writes into a verified payload: ``{key: reason}`` of every
#: protected key it removed. Always dropped first: never from the token.
UNTRUSTED_KEY = "sandloom.untrusted"

OUTDATED = "token_policy_outdated"
MISMATCH = "claims_policy_mismatch"
UNASSIGNED = "unassigned"
SOURCE_ABSENT = "authority_absent"
#: The reasons a refresh or a retry may change (an indeterminate decision).
INDETERMINATE_REASONS: frozenset[str] = frozenset({OUTDATED, MISMATCH})

#: Every acceptance of a token without ``cpv`` in the compatibility mode.
LEGACY_METRIC = "claims_policy_legacy_tokens_total"

#: The kernel test authority (a test composition only, the pytest plugin):
#: its provenance name is not a valid module short name (a hyphen), so no
#: module can be stamped under it. A token whose ``cpv`` is
#: :data:`TEST_CPV` is read as issued under the verifier's own map: with
#: :data:`TEST_AUTHORITY` in ``claims_sources`` every protected key is the
#: test authority's; without it, parts 1 and 3 decide per key.
TEST_AUTHORITY = "sandloom-test"
TEST_CPV = "sandloom-test"

_test_composition: list[bool] = [False]
_INF = float("inf")


def legacy_metric_name(key: str) -> str:
    """The counter of compatibility-mode acceptances of ``key``."""
    return f'{LEGACY_METRIC}{{key="{key}"}}'


def enable_test_authority() -> None:
    """Mark this process as a test composition (the pytest plugin only).

    Refused outside a pytest process: the kernel test authority never
    exists in a running application.
    """
    if "_pytest" not in sys.modules:
        raise RuntimeError(
            "the kernel test authority exists only in a test composition "
            "(the sandloom pytest plugin)"
        )
    _test_composition[0] = True


def test_authority_enabled() -> bool:
    return _test_composition[0]


def _names(value: Any) -> frozenset[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(v for v in value if isinstance(v, str))
    return frozenset()


#: ``(key, digest)`` pairs already logged (once each; bounded).
_LOGGED: set[tuple[str, str, str]] = set()


def _log_once(kind: str, key: str, value: Any) -> None:
    label = value if isinstance(value, str) else "<none>"
    entry = (kind, key, label)
    if entry in _LOGGED or len(_LOGGED) > 256:
        return
    _LOGGED.add(entry)
    if kind == MISMATCH:
        logger.error(
            "claim policy mismatch: a token issued after this process's "
            "claim_authorities took effect names cpv %s=%s, which this map "
            "does not accept: issuer and verifier run different maps "
            "(claims_policy_mismatch, 503)",
            key,
            label,
        )
    elif kind == OUTDATED:
        logger.warning(
            "claim policy: a token minted under another policy names cpv "
            "%s=%s; the claim is untrusted until the client refreshes "
            "(token_policy_outdated, 401)",
            key,
            label,
        )
    else:
        logger.warning(
            "claim policy: a token without cpv was trusted for %r in the "
            "legacy_tokens compatibility mode (G-RISK-14)",
            key,
        )


def _source_kept(
    accepted: Iterable[tuple[str, str]],
    sources: frozenset[str],
    failed: frozenset[str],
) -> bool:
    return any(owner in sources and owner not in failed for owner, _d in accepted)


def _key_reason(
    key: str,
    *,
    policy: ClaimAuthorities,
    entries: Mapping[str, Any] | None,
    sources: frozenset[str],
    failed: frozenset[str],
    digestless: bool,
    issued_before: bool,
) -> str | None:
    """Why ``key`` is not trusted (``None``: trusted)."""
    accepted = policy.accepted.get(key)
    if not accepted:
        return UNASSIGNED
    if digestless:
        return None if _source_kept(accepted, sources, failed) else SOURCE_ABSENT
    if entries is None:
        return OUTDATED
    digest = entries.get(key)
    for owner, expected in accepted:
        if digest == expected:
            return None if owner in sources and owner not in failed else SOURCE_ABSENT
    reason = OUTDATED if issued_before else MISMATCH
    _log_once(reason, key, digest)
    return reason


def _time(payload: Mapping[str, Any], key: str) -> float:
    try:
        return float(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _reading(
    payload: Mapping[str, Any],
    policy: ClaimAuthorities,
    now: float,
    lifetime_seconds: float,
    leeway_seconds: float,
) -> tuple[Mapping[str, Any] | None, bool, float]:
    """How the token's ``cpv`` is read: ``(entries, digestless, valid_until)``.

    ``entries`` are the per-key digests of a ``cpv`` token (``None``: none);
    ``digestless`` means parts 1 and 3 decide alone (a test-kit token in a
    test composition, or a token without ``cpv`` inside the compatibility
    window, whose end is then ``valid_until``).
    """
    cpv = payload.get(CPV_CLAIM)
    if isinstance(cpv, dict) and cpv.get("v") == CPV_VERSION:
        found = cpv.get("k")
        return (found if isinstance(found, dict) else {}), False, _INF
    if _test_composition[0] and cpv == TEST_CPV:
        return None, True, _INF
    until = policy.legacy_until
    if until is None:
        return None, False, _INF
    window_end = until + lifetime_seconds + leeway_seconds
    iat, exp = _time(payload, "iat"), _time(payload, "exp")
    if now < window_end and iat < until and 0 < exp - iat <= lifetime_seconds:
        return None, True, window_end
    return None, False, _INF


def _test_authority_token(
    payload: Mapping[str, Any], sources: frozenset[str], failed: frozenset[str]
) -> bool:
    """A test-kit token vouched for by the kernel test authority, read only
    in a test composition."""
    return (
        _test_composition[0]
        and payload.get(CPV_CLAIM) == TEST_CPV
        and TEST_AUTHORITY in sources
        and TEST_AUTHORITY not in failed
    )


def _selector_reasons(
    payload: Mapping[str, Any],
    untrusted: dict[str, str],
    reason_of: Callable[[str], str | None],
) -> None:
    """The tenant a token binds stands or falls with its ``tenant_ids``
    grant (decision 1): an untrusted grant makes ``tenant_id`` / ``org_id``
    untrusted for the same reason."""
    selectors = [key for key in TENANT_SELECTORS if key in payload]
    if not selectors:
        return
    grant = untrusted.get("tenant_ids") or reason_of("tenant_ids")
    if grant is not None:
        untrusted.update(dict.fromkeys(selectors, grant))


def apply_view(
    payload: dict[str, Any],
    policy: ClaimAuthorities,
    *,
    now: float,
    lifetime_seconds: float,
    leeway_seconds: float,
) -> tuple[float, tuple[str, ...]]:
    """Reduce the verified ``payload`` to its trusted view, in place.

    Returns ``(valid_until, legacy_keys)``: the time from which the view must
    be recomputed (the compatibility window's end when it trusted a key, else
    infinity) and the keys it trusted in the compatibility mode (counted by
    the caller). A token other than an access token is left as it is.
    """
    payload.pop(UNTRUSTED_KEY, None)
    if payload.get("token_type") != "access":
        return _INF, ()
    sources = _names(payload.get("claims_sources"))
    failed = _names(payload.get("claims_sources_failed"))
    if _test_authority_token(payload, sources, failed):
        return _INF, ()
    entries, digestless, valid_until = _reading(
        payload, policy, now, lifetime_seconds, leeway_seconds
    )
    legacy = valid_until != _INF
    reason_of = partial(
        _key_reason,
        policy=policy,
        entries=entries,
        sources=sources,
        failed=failed,
        digestless=digestless,
        issued_before=_time(payload, "iat") < policy.effective_at,
    )
    untrusted: dict[str, str] = {}
    legacy_keys: list[str] = []
    for key in policy.protected:
        if key in payload:
            reason = reason_of(key)
            if reason is not None:
                untrusted[key] = reason
            elif legacy:
                legacy_keys.append(key)
                _log_once("legacy", key, None)
        elif entries is None and not digestless and key in policy.accepted:
            # An old token: a policy needing the key gets 401, never 403.
            untrusted[key] = OUTDATED
    _selector_reasons(payload, untrusted, reason_of)
    if untrusted:
        for key in untrusted:
            payload.pop(key, None)
        payload[UNTRUSTED_KEY] = untrusted
    return valid_until, tuple(legacy_keys)


def untrusted_reasons(claims: Mapping[str, Any]) -> Mapping[str, str]:
    """The view's ``{key: reason}`` of a verified payload (empty: none)."""
    value = claims.get(UNTRUSTED_KEY)
    return value if isinstance(value, dict) else {}


def outdated_or_mismatch(reason: str | None) -> HTTPException | None:
    """The HTTP error of an indeterminate trust reason, else ``None``."""
    if reason == OUTDATED:
        return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=OUTDATED)
    if reason == MISMATCH:
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=MISMATCH)
    return None


def platform_admission(roles: Any, untrusted: Mapping[str, str]) -> None:
    """The one platform admission (``require_platform_admin`` and
    ``platform_operator``): the platform role ``admin``, trusted only when
    ``roles`` is in the view. No module name is trusted: the view decides
    which authority vouches for ``roles``.

    Raises 401 ``token_policy_outdated`` / 503 ``claims_policy_mismatch``
    when ``roles`` is untrusted for that reason, else 403.
    """
    if isinstance(roles, (list, tuple, set, frozenset)) and "admin" in roles:
        return
    error = outdated_or_mismatch(untrusted.get("roles"))
    if error is not None:
        raise error
    raise HTTPException(
        status.HTTP_403_FORBIDDEN, "Platform administrator role required"
    )


__all__ = [
    "INDETERMINATE_REASONS",
    "LEGACY_METRIC",
    "MISMATCH",
    "OUTDATED",
    "SOURCE_ABSENT",
    "TEST_AUTHORITY",
    "TEST_CPV",
    "UNASSIGNED",
    "UNTRUSTED_KEY",
    "apply_view",
    "enable_test_authority",
    "legacy_metric_name",
    "outdated_or_mismatch",
    "platform_admission",
    "test_authority_enabled",
    "untrusted_reasons",
]
