"""What crosses a signed RPC call, both ways: one design (local == remote).

**Out — the caller context** every request carries under its pair MAC
(tenant + issue time + signer / audience) and, when the calling code runs
for an end user, the **caller facts** (``usr``): everything the provider's
checks read off the request scope of a LOCAL call —

* ``sub``: the end user (billing's personal account, the command dedupe
  scope, the audit actor);
* ``iat_us``: the token's issue instant (the live session check);
* ``auth_time`` and ``ra`` (re-authenticated in this request): step-up;
* ``rc``: the commit-time re-check reaches command units (spec §4.2);
* ``op`` and ``ik``: the request's operation key and whether the client sent
  an ``Idempotency-Key`` (money dedupe);
* ``sr``: a committed unit of the request revoked its own subject ("check,
  then revoke"), so the re-check of a later unit skips it, as locally.

The provider binds them as the call's request scope
(:func:`_caller_scope`), so its checks run exactly as for a local call and a
nested call carries them on, hop after hop. A call without facts is a system
call (no end user anywhere up the chain), never "a user call whose facts got
lost": a call made for an end user ALWAYS carries them.

**Back — the call effects** (``fx``) every response the provider produced
after running the call carries, success AND error, MAC-bound with the body
(:func:`_call_effects`, merged by :func:`_merge_call_effects` before the
caller decodes the result or raises): every risk the execution observed
(``rk``), its remote calls an attempt of which got no answer, so their
provider unit may still run (``id``: the caller's intent windows refuse, at
any depth), the subjects its committed units self-revoked (``sr``), a
re-authentication (``ra``) and the step-ups it left pending (``su``) — what
the same call made locally leaves on the caller's request scope. A command
replay re-applies its first execution's effects (kept with its dedupe
record) except ``ra`` and ``id``.
"""

from __future__ import annotations

import json
import time
from functools import partial
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

import sandloom.core.auth.verification as _verification
import sandloom.core.unit_of_work.risk as _risk
from sandloom.core import rpc_identity
from sandloom.core.rpc.wire import _b64
from sandloom.core.tenant import current_tenant_id_optional
from sandloom.core.unit_of_work.request import RequestScope, current_request_scope


def _encode_context(payload: dict[str, Any]) -> str:
    return _b64(json.dumps(payload, separators=(",", ":")).encode())


def _context_payload(
    identity: rpc_identity.CallerIdentity | None = None,
    audience: str | None = None,
) -> dict[str, Any]:
    """The caller context of one call before encoding.

    It carries the bound tenant and the issue time; with a signing
    ``identity`` (a signed command) also the
    signer (``svc`` + ``kid``); ``audience`` (the binding's pinned provider)
    as ``aud`` — a provider refuses a context addressed to another service;
    the caller facts (``usr``) of the end user the call runs for.
    """
    tenant_id = current_tenant_id_optional()
    payload: dict[str, Any] = {
        "tenant_id": str(tenant_id) if tenant_id is not None else None,
        "iat": int(time.time()),
    }
    if identity is not None:
        payload["svc"] = identity.service_id
        payload["kid"] = identity.kid
    if audience:
        payload["aud"] = audience
    facts = _caller_facts(current_request_scope())
    if facts is not None:
        payload["usr"] = facts
    return payload


def _caller_facts(scope: RequestScope | None) -> dict[str, Any] | None:
    """The caller facts of the request scope (``None``: no end user)."""
    if scope is None or scope.claims is None or not scope.subject:
        return None
    claims = scope.claims
    facts: dict[str, Any] = {"sub": scope.subject}
    for name in ("iat_us", "auth_time"):
        value = claims.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            facts[name] = value
    if scope.check is not None and (scope.intent is None or scope.at_commit):
        # the re-check reaches a command unit here (an operation-intent route
        # re-checks in release_check, not at its units' commits)
        facts["rc"] = 1
    state = scope.risk
    if state is not None:
        if state.reauthenticated:
            facts["ra"] = 1
        if state.operation_key:
            facts["op"] = state.operation_key
    if _risk.client_sent_key(scope):
        facts["ik"] = 1
    if scope.subject in scope.self_revoked:
        # a committed unit of the request revoked its own subject ("check,
        # then revoke"): the provider's re-check honours it like a local
        # unit's (spec §4.5 item 3)
        facts["sr"] = 1
    return facts


def _number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _caller_scope(core: Any, facts: Any) -> RequestScope | None:
    """The request scope a served call runs in: the caller facts bound
    exactly as a local call sees its request's (``None``: a system call)."""
    if not isinstance(facts, dict):
        return None
    subject = facts.get("sub")
    if not isinstance(subject, str) or not subject:
        return None
    claims: dict[str, Any] = {"sub": subject}
    for name in ("iat_us", "auth_time"):
        value = _number(facts.get(name))
        if value is not None:
            claims[name] = value
    scope = RequestScope(core=core, policy=None)
    scope.subject = subject
    scope.claims = claims
    if facts.get("rc"):
        # the provider re-checks the caller's subject before the COMMIT of
        # the call's unit, like the caller before a local command's (§4.2)
        scope.check = partial(_verification.assert_token_valid_now, claims)
        scope.at_commit = True
    if facts.get("sr"):
        scope.self_revoked = frozenset((subject,))
    op = facts.get("op")
    if facts.get("ra") or facts.get("ik") or (isinstance(op, str) and op):
        state = _risk.risk_state(scope)
        state.reauthenticated = bool(facts.get("ra"))
        state.client_key = bool(facts.get("ik"))
        if isinstance(op, str) and op:
            state.operation_key = op
    return scope


def _call_effects(
    scope: RequestScope | None, observed: _risk.ObservedEffects
) -> dict[str, Any] | None:
    """What a served call leaves on its caller's request (``fx``)."""
    fx = _risk.call_effects(observed)
    if scope is not None:
        if scope.self_revoked:
            fx["sr"] = sorted(scope.self_revoked)
        state = scope.risk
        if state is not None:
            if state.reauthenticated:
                fx["ra"] = 1
            if state.step_up:
                fx["su"] = sorted(set(state.step_up))
    return fx or None


def _merge_call_effects(fx: Any) -> bool:
    """Mark the caller's request with what the remote execution left on it
    (``fx``, MAC-bound with the response; ``None``: nothing), exactly as the
    same call made locally would have
    (:func:`sandloom.core.unit_of_work.risk.apply_call_effects`: risks, calls in
    doubt, committed self-revocations, pending step-ups), and a
    re-authentication the execution performed in it.

    ``False``: ``fx`` is malformed -- not a dict, an unknown field, or one
    entry of any field of the wrong type or beyond its bound
    (:func:`sandloom.core.unit_of_work.risk.well_formed_call_effects`) -- so what
    the call left is not fully known: the proxy counts such a call in doubt,
    as one that got no answer. Its well-formed risks still mark the request
    (fail closed); its re-authentication never does."""
    if fx is None:
        return True
    if not isinstance(fx, dict):
        return False
    _risk.apply_call_effects(fx)
    if not _risk.well_formed_call_effects(fx):
        return False
    if fx.get("ra"):
        scope = current_request_scope()
        if scope is not None:
            _risk.risk_state(scope).reauthenticated = True
    return True


def _parse_rpc_tenant(raw: str | None) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid tenant id",
        ) from exc
