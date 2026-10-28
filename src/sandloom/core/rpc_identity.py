"""Service keys, the per-contract allowlist and command signatures of
internal RPC.

Internal RPC authenticates every request with a per-pair MAC
(:mod:`sandloom.core.rpc_pair_mac`, ADR-0004). This module holds what that model
needs besides the pair keys:

* every service's id (``RPC_SERVICE_ID``, default ``SANDLOOM_SERVICE_NAME``);
* the per-caller allowlist ``RPC_ALLOWED_CALLS`` (``{"<service>":
  ["<contract>", ...]}`` or ``{"<service>": {"<contract>": "*" |
  ["<method>", ...]}}``) — deny by default. A caller's entry is exactly the
  contracts its ``SERVICE_BINDINGS`` binds (a caller refuses to start when it
  binds a contract its own entry does not allow; ``sandloom keys allowlist``
  derives the map);
* the Ed25519 signing identity (``RPC_PRIVATE_KEY`` + ``RPC_KEY_ID``, public
  halves in ``RPC_SERVICE_KEYS`` — ``{"<service>": {"<kid>": "<PEM>"}}``,
  identical everywhere, several kids during that service's own rotation).
  A pair MAC proves "the caller or this provider"; a COMMAND call
  additionally carries the caller's Ed25519 signature over the request and
  its nonce (``RPC_COMMANDS_REQUIRE_SIGNATURE``), so a leaked PROVIDER
  X25519 key can forge reads at most, never effects. Providers hold only
  public keys, so a compromised provider cannot impersonate its callers.

Outcomes: a request that does not authenticate -> ``401``; a verified caller
that is not allowed the contract / method -> ``403``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sandloom.core.signing_keys import (
    KeyConfigError,
    load_private_key,
    load_public_key,
    same_public_key,
    sign_ed25519,
    verify_ed25519,
)

#: Detached Ed25519 signature of the caller's service identity.
SVC_SIG_HEADER = "X-SANDLOOM-RPC-Service-Signature"
_DOMAIN = b"sandloom-rpc-service-identity-v1"
_ALGORITHM = "EdDSA"


class ServiceIdentityError(Exception):
    """A caller identity that does not verify (-> 401)."""

    def __init__(self, code: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CallerIdentity:
    """This process's own Ed25519 signing identity (command signatures)."""

    service_id: str
    kid: str
    key: Any


@dataclass(frozen=True)
class AllowedCalls:
    """What one caller service may call: contract -> methods (``None`` = all)."""

    contracts: Mapping[str, frozenset[str] | None]

    def allows(self, contract: str, method: str | None) -> bool:
        if contract not in self.contracts:
            return False
        methods = self.contracts[contract]
        return methods is None or method is None or method in methods


# ---------------------------------------------------------------------------
# configuration parsing (shared by Settings validation and the runtime)
# ---------------------------------------------------------------------------


def parse_service_keys(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``RPC_SERVICE_KEYS`` -> {service: {kid: Ed25519PublicKey}}."""
    out: dict[str, dict[str, Any]] = {}
    for service, keys in raw.items():
        if not isinstance(service, str) or not service.strip():
            raise ValueError("RPC_SERVICE_KEYS: a service id must be a string")
        if not isinstance(keys, Mapping) or not keys:
            raise ValueError(
                f"RPC_SERVICE_KEYS[{service!r}] must be a non-empty "
                '{"<kid>": "<Ed25519 PEM public key>"} object'
            )
        parsed: dict[str, Any] = {}
        for kid, pem in keys.items():
            if not isinstance(kid, str) or not kid.strip() or not isinstance(pem, str):
                raise ValueError(
                    f"RPC_SERVICE_KEYS[{service!r}]: kids and keys must be strings"
                )
            try:
                parsed[kid] = load_public_key(pem, _ALGORITHM)
            except KeyConfigError as exc:
                raise ValueError(
                    f"RPC_SERVICE_KEYS[{service!r}][{kid!r}]: {exc}"
                ) from exc
        out[service] = parsed
    return out


def parse_allowed_calls(raw: Mapping[str, Any]) -> dict[str, AllowedCalls]:
    """``RPC_ALLOWED_CALLS`` -> {caller service: AllowedCalls}."""
    out: dict[str, AllowedCalls] = {}
    for service, spec in raw.items():
        contracts: dict[str, frozenset[str] | None] = {}
        if isinstance(spec, list):
            for contract in spec:
                if not isinstance(contract, str) or not contract:
                    raise ValueError(
                        f"RPC_ALLOWED_CALLS[{service!r}]: contract names are strings"
                    )
                contracts[contract] = None
        elif isinstance(spec, Mapping):
            for contract, methods in spec.items():
                if methods == "*":
                    contracts[contract] = None
                elif isinstance(methods, list) and all(
                    isinstance(m, str) and m for m in methods
                ):
                    contracts[contract] = frozenset(methods)
                else:
                    raise ValueError(
                        f"RPC_ALLOWED_CALLS[{service!r}][{contract!r}] must be "
                        '"*" or a list of method names'
                    )
        else:
            raise ValueError(
                f"RPC_ALLOWED_CALLS[{service!r}] must be a list of contracts or a "
                '{"<contract>": "*" | ["<method>", ...]} object'
            )
        out[service] = AllowedCalls(contracts)
    return out


def service_id(cfg: Any) -> str | None:
    value = getattr(cfg, "rpc_service_id", None) or getattr(
        cfg, "sandloom_service_name", None
    )
    return str(value) if value else None


def configuration_problem(cfg: Any) -> str | None:
    """Why the signing-key / allowlist configuration cannot work, or ``None``.

    Malformed keys / allowlist, a private key that is not this service's
    published key, and a caller that binds (``SERVICE_BINDINGS``) a contract
    its own allowlist entry does not allow — every legitimately wired call
    must be allowed.
    """
    try:
        keys = parse_service_keys(getattr(cfg, "rpc_service_keys", {}) or {})
        allowed = parse_allowed_calls(getattr(cfg, "rpc_allowed_calls", {}) or {})
    except ValueError as exc:
        return str(exc)
    private_pem = getattr(cfg, "rpc_private_key", None)
    kid = getattr(cfg, "rpc_key_id", None)
    me = service_id(cfg)
    if private_pem:
        try:
            private = load_private_key(private_pem, _ALGORITHM)
        except KeyConfigError as exc:
            return f"RPC_PRIVATE_KEY: {exc} (service identity keys are Ed25519)"
        if not me:
            return (
                "RPC_PRIVATE_KEY needs a service id (RPC_SERVICE_ID or "
                "SANDLOOM_SERVICE_NAME)"
            )
        if not kid:
            return "RPC_KEY_ID is required with RPC_PRIVATE_KEY"
        published = keys.get(me, {}).get(kid)
        if published is None:
            return (
                f"RPC_SERVICE_KEYS[{me!r}][{kid!r}] is missing: providers could "
                "not verify this service's calls"
            )
        if not same_public_key(private, published):
            return (
                f"RPC_SERVICE_KEYS[{me!r}][{kid!r}] is not the public half of "
                "RPC_PRIVATE_KEY"
            )
    bindings: Mapping[str, Mapping[str, Any]] = getattr(cfg, "service_bindings", {})
    if not bindings:
        return None
    mine = allowed.get(me or "")
    missing = [c for c in sorted(bindings) if mine is None or c not in mine.contracts]
    if missing:
        return (
            f"SERVICE_BINDINGS of {me!r} bind {missing}, which RPC_ALLOWED_CALLS"
            f"[{me!r}] does not allow: providers would answer 403. Add them "
            "(`sandloom keys allowlist` derives the map from SERVICE_BINDINGS)"
        )
    return None


# ---------------------------------------------------------------------------
# runtime (cached per configuration object)
# ---------------------------------------------------------------------------

_identity_cache: dict[tuple[str, str, str], CallerIdentity] = {}
_trust_cache: list[Any] = [None, None, None, None]


def signing_identity(cfg: Any) -> CallerIdentity | None:
    """This service's Ed25519 identity, or ``None`` (no key configured).

    It signs COMMAND calls (and every call with
    ``RPC_COMMANDS_REQUIRE_SIGNATURE=all``): a leaked PROVIDER X25519 key,
    which derives every caller's pair key, can then never forge an effect.
    """
    pem = getattr(cfg, "rpc_private_key", None)
    kid = getattr(cfg, "rpc_key_id", None)
    me = service_id(cfg)
    if not pem or not kid or not me:
        return None
    cache_key = (pem, kid, me)
    identity = _identity_cache.get(cache_key)
    if identity is None:
        identity = CallerIdentity(me, kid, load_private_key(pem, _ALGORITHM))
        if len(_identity_cache) >= 8:
            _identity_cache.clear()
        _identity_cache[cache_key] = identity
    return identity


def _trust(cfg: Any) -> tuple[dict[str, dict[str, Any]], dict[str, AllowedCalls]]:
    raw_keys = getattr(cfg, "rpc_service_keys", {}) or {}
    raw_allowed = getattr(cfg, "rpc_allowed_calls", {}) or {}
    cached_keys, cached_allowed, keys, allowed = _trust_cache
    if cached_keys is raw_keys and cached_allowed is raw_allowed:
        return keys, allowed
    keys = parse_service_keys(raw_keys)
    allowed = parse_allowed_calls(raw_allowed)
    _trust_cache[:] = [raw_keys, raw_allowed, keys, allowed]
    return keys, allowed


def signing_message(http_method: str, target: str, body: bytes, context: str) -> bytes:
    return b"\n".join(
        [
            _DOMAIN,
            http_method.upper().encode(),
            target.encode(),
            hashlib.sha256(body).hexdigest().encode(),
            context.encode(),
        ]
    )


def sign(
    identity: CallerIdentity, http_method: str, target: str, body: bytes, context: str
) -> str:
    return sign_ed25519(
        identity.key, signing_message(http_method, target, body, context)
    )


def verify(
    cfg: Any,
    *,
    http_method: str,
    target: str,
    body: bytes,
    context: str | None,
    signature: str,
) -> tuple[str, dict[str, Any]]:
    """Verify a command signature; return ``(caller service id, context)``.

    The signature is checked against the keys of the service the context
    CLAIMS to be, so a service signing with its own key while claiming another
    id fails here (401). Freshness and the nonce are checked by the pair-MAC
    verification the signature rides on.
    """
    if not context:
        raise ServiceIdentityError(
            "rpc_identity_invalid", "service identity without a signed context"
        )
    try:
        decoded = json.loads(_unb64(context), object_pairs_hook=_no_duplicate_keys)
    except Exception as exc:  # malformed context
        raise ServiceIdentityError(
            "rpc_identity_invalid", "malformed RPC context"
        ) from exc
    if not isinstance(decoded, dict):
        raise ServiceIdentityError("rpc_identity_invalid", "malformed RPC context")
    caller = decoded.get("svc")
    kid = decoded.get("kid")
    if not isinstance(caller, str) or not isinstance(kid, str):
        raise ServiceIdentityError(
            "rpc_identity_invalid", "RPC context names no caller service / kid"
        )
    keys, _ = _trust(cfg)
    public = keys.get(caller, {}).get(kid)
    if public is None:
        raise ServiceIdentityError(
            "rpc_identity_unknown",
            f"unknown caller service key {caller!r}/{kid!r}",
        )
    if not verify_ed25519(
        public, signing_message(http_method, target, body, context), signature
    ):
        raise ServiceIdentityError(
            "rpc_identity_invalid", "RPC caller identity signature is invalid"
        )
    return caller, decoded


def authorize(cfg: Any, caller: str, contract: str, method: str | None) -> bool:
    """Deny by default: only an explicit RPC_ALLOWED_CALLS entry allows."""
    _, allowed = _trust(cfg)
    entry = allowed.get(caller)
    return entry is not None and entry.allows(contract, method)


def allowlist_from_bindings(
    bindings_by_service: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[str]]:
    """``RPC_ALLOWED_CALLS`` derived from each service's ``SERVICE_BINDINGS``."""
    return {
        service: sorted(bindings)
        for service, bindings in sorted(bindings_by_service.items())
        if bindings
    }


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """One interpretation of signed JSON (MAC and signature verifiers)."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate RPC context key")
        result[key] = value
    return result


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


__all__ = [
    "SVC_SIG_HEADER",
    "AllowedCalls",
    "CallerIdentity",
    "ServiceIdentityError",
    "allowlist_from_bindings",
    "authorize",
    "configuration_problem",
    "parse_allowed_calls",
    "parse_service_keys",
    "service_id",
    "sign",
    "signing_identity",
    "verify",
]
