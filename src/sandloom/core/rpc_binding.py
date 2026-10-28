"""Response binding of internal RPC: every response is keyed to its request.

Guarantee (normative, docs/SERVICE_RPC.md "Response binding"): a caller
accepts a response only when it answers THE request it sent (never an
accidentally mixed-up one) and comes from the provider the binding pins —
the MAC key is a per-pair secret only the two services can derive. It is NOT
a substitute for TLS/mTLS (no confidentiality).

Wire
----
Request: ``X-SANDLOOM-RPC-Nonce`` (fresh per attempt). Response:
``X-SANDLOOM-RPC-Response-Binding: 2;alg=x25519;kid=<kid>;svc=<provider>`` and
``X-SANDLOOM-RPC-Response-MAC = HMAC-SHA256(K, "sandloom-rpc-response/2" | binding
header | nonce | request auth | status | sha256(body))``. Covering the
request's own authentication (its pair-MAC tag and command signature) means a
provider's answer to a REPLAYED request (an on-path party re-sending an
earlier request under a new nonce) never verifies for the new one.

Key (K): a per-PAIR static secret from DEDICATED X25519 key-agreement keys —
one per service, separate from its Ed25519 signing key (no cross-protocol key
reuse): ``RPC_DH_PRIVATE_KEY`` + ``RPC_DH_KEY_ID``, public halves in
``RPC_SERVICE_DH_KEYS``. ``X25519PrivateKey.exchange`` (the ``cryptography``
library; no custom curve code) feeds HKDF-SHA256 with info = direction +
caller svc/kid + provider svc/kid (a different salt and info than the request
MAC key of :mod:`sandloom.core.rpc_pair_mac`, i.e. an independent key). The
provider keys a response only for a caller whose request MAC VERIFIED (its
svc/kid from the verified header); a request that did not authenticate is
answered with the nonce alone, which the caller refuses (``unkeyed``). The
key never travels and is cached per (pair, kids), so a call costs one HMAC
(~1 µs) per side. The caller pins the provider with the binding's
``service_id`` (a response keyed by another service is ``service_mismatch``).

Caller verdict (:func:`verify`): accepted, or a rejection reason.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from sandloom.core import rpc_identity
from sandloom.core.signing_keys import (
    KeyConfigError,
    load_x25519_private_key,
    load_x25519_public_key,
    x25519_public_pem,
)

NONCE_HEADER = "X-SANDLOOM-RPC-Nonce"
MAC_HEADER = "X-SANDLOOM-RPC-Response-MAC"
#: Response: ``2;alg=x25519;kid=...;svc=...``.
BINDING_HEADER = "X-SANDLOOM-RPC-Response-Binding"
#: Request command-signature header covered by the response MAC.
SVC_SIG_HEADER = rpc_identity.SVC_SIG_HEADER

VERSION = "2"
ALG_PAIR = "x25519"

_V2_DOMAIN = b"sandloom-rpc-response/2"
_HKDF_SALT = b"sandloom-rpc-response-binding/v2"
_NONCE_MAX_LENGTH = 64
_NONCE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)
#: svc / kid values a binding header may carry (no separators, header-safe).
_PARAM = re.compile(r"^[A-Za-z0-9._:@+-]{1,128}$")

#: Every rejection reason (``sandloom_rpc_response_binding_rejected_total{reason}``).
REJECT_REASONS = (
    "unbound",  # no binding at all (not a Sandloom RPC endpoint)
    "unkeyed",  # the nonce echoed without a keyed MAC (request not verified)
    "nonce_mismatch",  # bound to ANOTHER request
    "mac_mismatch",  # body / status / request differ, or a forged MAC
    "malformed",  # an unparsable binding header
    "kid_unknown",  # the provider's kid is not in RPC_SERVICE_DH_KEYS
    "key_unavailable",  # this caller holds no usable X25519 key
    "service_mismatch",  # keyed by a service other than the pinned one
    "unpinned",  # the binding names no service_id to verify against
)


def new_nonce() -> str:
    return os.urandom(16).hex()


def valid_nonce(raw: str | None) -> str | None:
    """The request's nonce when well-formed (else the provider binds nothing)."""
    if raw and len(raw) <= _NONCE_MAX_LENGTH and _NONCE_CHARS.issuperset(raw):
        return raw
    return None


#: Request pair-MAC header (ADR-0004), covered by the response MAC.
REQ_MAC_HEADER = "X-SANDLOOM-RPC-Auth"


def request_auth(headers: Mapping[str, Any] | Any) -> bytes:
    """The request authentication the response MAC covers.

    The request's pair-MAC tag (and command signature, when sent): a response
    can never be re-bound to another request that shares its nonce (ADR-0004
    security review, finding 1).
    """
    svc = headers.get(SVC_SIG_HEADER) or ""
    mac = headers.get(REQ_MAC_HEADER) or ""
    return f"{svc}|{mac}".encode()


# --- MAC -----------------------------------------------------------------------


def mac_v2(
    key: bytes,
    descriptor: str,
    nonce: str,
    req_auth: bytes,
    status_code: int,
    body: bytes,
) -> str:
    mac = hmac.new(key, digestmod=hashlib.sha256)
    mac.update(
        b"\n".join(
            [
                _V2_DOMAIN,
                descriptor.encode(),
                nonce.encode(),
                hashlib.sha256(req_auth).digest(),
                str(status_code).encode(),
                hashlib.sha256(body).digest(),
            ]
        )
    )
    return mac.hexdigest()


def descriptor(kid: str, svc: str) -> str:
    return f"{VERSION};alg={ALG_PAIR};kid={kid};svc={svc}"


def parse_descriptor(raw: str) -> dict[str, str] | None:
    """``2;alg=x25519;kid=..;svc=..`` -> params (``None`` when malformed)."""
    version, *params = raw.split(";")
    if version.strip() != VERSION:
        return None
    out: dict[str, str] = {}
    for param in params:
        name, sep, value = param.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or name in out or not _PARAM.match(value):
            return None
        out[name] = value
    if out.get("alg") != ALG_PAIR or "kid" not in out or "svc" not in out:
        return None
    return out


# --- keys ----------------------------------------------------------------------

_CACHE_MAX = 256

#: (own private PEM, peer public PEM, info) -> pair key
_pair_keys: dict[tuple[str, str, bytes], bytes] = {}
#: cached parse of RPC_SERVICE_DH_KEYS: [raw map object, parsed]
_dh_trust: list[Any] = [None, {}]


def _hkdf(ikm: bytes, info: bytes) -> bytes:
    kdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALT, info=info)
    return kdf.derive(ikm)


def _remember(cache: dict[Any, Any], key: Any, value: Any, limit: int) -> None:
    """Insert into a bounded cache, evicting its OLDEST entry when full (a
    wholesale clear would drop every live key at once)."""
    if key not in cache and len(cache) >= limit:
        del cache[next(iter(cache))]
    cache[key] = value


def parse_service_dh_keys(raw: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Validate ``RPC_SERVICE_DH_KEYS`` (every PEM an X25519 public key)."""
    out: dict[str, dict[str, str]] = {}
    for service, keys in raw.items():
        if not isinstance(service, str) or not service.strip():
            raise ValueError("RPC_SERVICE_DH_KEYS: a service id must be a string")
        if not isinstance(keys, Mapping) or not keys:
            raise ValueError(
                f"RPC_SERVICE_DH_KEYS[{service!r}] must be a non-empty "
                '{"<kid>": "<X25519 PEM public key>"} object'
            )
        parsed: dict[str, str] = {}
        for kid, pem in keys.items():
            if not isinstance(kid, str) or not _PARAM.match(kid):
                raise ValueError(
                    f"RPC_SERVICE_DH_KEYS[{service!r}]: kid {kid!r} must match "
                    f"{_PARAM.pattern}"
                )
            if not isinstance(pem, str):
                raise ValueError(f"RPC_SERVICE_DH_KEYS[{service!r}][{kid!r}]: a PEM")
            try:
                load_x25519_public_key(pem)
            except KeyConfigError as exc:
                raise ValueError(
                    f"RPC_SERVICE_DH_KEYS[{service!r}][{kid!r}]: {exc}"
                ) from exc
            parsed[kid] = pem
        out[service] = parsed
    return out


def service_dh_keys(cfg: Any) -> dict[str, dict[str, str]]:
    """``RPC_SERVICE_DH_KEYS`` parsed (cached per map object, order kept)."""
    raw = getattr(cfg, "rpc_service_dh_keys", {}) or {}
    if _dh_trust[0] is not raw:
        _dh_trust[:] = [raw, parse_service_dh_keys(raw)]
    parsed: dict[str, dict[str, str]] = _dh_trust[1]
    return parsed


def _dh_public(cfg: Any, svc: str, kid: str) -> str | None:
    """A service's published X25519 public key (PEM) by kid."""
    return service_dh_keys(cfg).get(svc, {}).get(kid)


@dataclass(frozen=True)
class OwnDhKey:
    service_id: str
    kid: str
    private_pem: str


def own_dh_key(cfg: Any) -> OwnDhKey | None:
    """This service's X25519 key (``RPC_DH_PRIVATE_KEY`` + ``RPC_DH_KEY_ID``)."""
    pem = getattr(cfg, "rpc_dh_private_key", None)
    kid = getattr(cfg, "rpc_dh_key_id", None)
    me = rpc_identity.service_id(cfg)
    if not pem or not kid or not me or not _PARAM.match(kid) or not _PARAM.match(me):
        return None
    return OwnDhKey(me, kid, pem)


def pair_info(
    caller_svc: str, caller_kid: str, provider_svc: str, provider_kid: str
) -> bytes:
    """HKDF info: direction (provider -> caller responses) + both identities."""
    return b"\x00".join(
        [
            b"response",
            caller_svc.encode(),
            caller_kid.encode(),
            provider_svc.encode(),
            provider_kid.encode(),
        ]
    )


def pair_key(own_private_pem: str, peer_public_pem: str, info: bytes) -> bytes:
    """HKDF-SHA256(X25519(own, peer)) for one direction of one pair (cached).

    ``cryptography`` raises ``ValueError`` for a low-order peer key (an
    all-zero shared secret); callers treat it as an unusable key.
    """
    cache_key = (own_private_pem, peer_public_pem, info)
    cached = _pair_keys.get(cache_key)
    if cached is not None:
        return cached
    shared = load_x25519_private_key(own_private_pem).exchange(
        load_x25519_public_key(peer_public_pem)
    )
    key = _hkdf(shared, info)
    _remember(_pair_keys, cache_key, key, _CACHE_MAX)
    return key


# --- provider ------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderKey:
    descriptor: str
    key: bytes


def provider_key(cfg: Any, caller: tuple[str, str] | None) -> ProviderKey | None:
    """The key this provider binds a response with (``None``: unkeyed).

    ``caller``: the (svc, X25519 kid) of a caller whose request MAC VERIFIED
    (never a merely claimed one).
    """
    if caller is None:
        return None
    own = own_dh_key(cfg)
    peer = _dh_public(cfg, caller[0], caller[1]) if own is not None else None
    if own is None or peer is None:
        return None
    info = pair_info(caller[0], caller[1], own.service_id, own.kid)
    try:
        key = pair_key(own.private_pem, peer, info)
    except (ValueError, KeyConfigError):
        return None
    return ProviderKey(descriptor(own.kid, own.service_id), key)


def response_headers(
    *,
    nonce: str,
    key: ProviderKey | None,
    req_auth: bytes,
    status_code: int,
    body: bytes,
) -> list[tuple[str, str]]:
    """The binding headers of one provider response (the nonce alone when
    the request did not authenticate: the caller refuses it)."""
    if key is None:
        return [(NONCE_HEADER, nonce)]
    return [
        (NONCE_HEADER, nonce),
        (BINDING_HEADER, key.descriptor),
        (
            MAC_HEADER,
            mac_v2(key.key, key.descriptor, nonce, req_auth, status_code, body),
        ),
    ]


# --- caller --------------------------------------------------------------------


def _mac_equal(presented: Any, expected: str) -> bool:
    """Constant-time MAC check on BYTES: a non-ASCII (latin-1 decoded)
    header value is a mismatch, never a ``TypeError`` from
    ``compare_digest(str, str)``."""
    return hmac.compare_digest(
        str(presented).encode("utf-8", "surrogatepass"), expected.encode()
    )


def verify(  # noqa: C901 - one early return per rejection reason
    cfg: Any,
    response_headers_: Any,
    *,
    nonce: str,
    sent: Mapping[str, str],
    expect_service: str | None,
    status_code: int,
    body: bytes,
) -> str | None:
    """Check a response against the request that sent ``nonce``.

    ``None`` when it is bound to that request by the pinned
    ``expect_service``; else the rejection reason (:data:`REJECT_REASONS`).
    ``sent``: the request headers (its MAC tag and command signature).
    """
    echoed = response_headers_.get(NONCE_HEADER)
    mac = response_headers_.get(MAC_HEADER)
    raw = response_headers_.get(BINDING_HEADER)
    if echoed is None and mac is None and raw is None:
        return "unbound"
    if echoed != nonce:
        return "nonce_mismatch"
    if raw is None:
        return "unkeyed"
    if mac is None:
        return "mac_mismatch"
    params = parse_descriptor(str(raw))
    if params is None:
        return "malformed"
    svc, kid = params["svc"], params["kid"]
    if expect_service is None:
        return "unpinned"
    if svc != expect_service:
        return "service_mismatch"
    own = own_dh_key(cfg)
    if own is None:
        return "key_unavailable"
    peer = _dh_public(cfg, svc, kid)
    if peer is None:
        return "kid_unknown"
    try:
        key = pair_key(
            own.private_pem, peer, pair_info(own.service_id, own.kid, svc, kid)
        )
    except (ValueError, KeyConfigError):
        return "key_unavailable"
    expected = mac_v2(key, str(raw), nonce, request_auth(sent), status_code, body)
    if not _mac_equal(mac, expected):
        return "mac_mismatch"
    return None


# --- configuration -------------------------------------------------------------


def configuration_problem(cfg: Any) -> str | None:
    """Why the X25519 key configuration cannot work, or ``None``.

    ``RPC_SERVICE_DH_KEYS`` parses; a configured ``RPC_DH_PRIVATE_KEY`` has a
    service id + ``RPC_DH_KEY_ID`` and is published (matching) under that kid.
    (A caller's own key and pinned bindings:
    :func:`sandloom.core.rpc_pair_mac.configuration_problem`.)
    """
    try:
        dh_keys = parse_service_dh_keys(getattr(cfg, "rpc_service_dh_keys", {}) or {})
    except ValueError as exc:
        return str(exc)
    private_pem = getattr(cfg, "rpc_dh_private_key", None)
    if private_pem:
        try:
            private = load_x25519_private_key(private_pem)
        except KeyConfigError as exc:
            return f"RPC_DH_PRIVATE_KEY: {exc} (response-binding keys are X25519)"
        me = rpc_identity.service_id(cfg)
        kid = getattr(cfg, "rpc_dh_key_id", None)
        if not me:
            return "RPC_DH_PRIVATE_KEY needs a service id (RPC_SERVICE_ID)"
        if not kid or not _PARAM.match(kid):
            return (
                "RPC_DH_KEY_ID is required with RPC_DH_PRIVATE_KEY (and must "
                f"match {_PARAM.pattern})"
            )
        published = dh_keys.get(me, {}).get(kid)
        if published is None:
            return (
                f"RPC_SERVICE_DH_KEYS[{me!r}][{kid!r}] is missing: peers could "
                "not verify this service's keyed responses"
            )
        if x25519_public_pem(load_x25519_public_key(published)) != (
            x25519_public_pem(private)
        ):
            return (
                f"RPC_SERVICE_DH_KEYS[{me!r}][{kid!r}] is not the public half "
                "of RPC_DH_PRIVATE_KEY"
            )
    return None


__all__ = [
    "ALG_PAIR",
    "BINDING_HEADER",
    "MAC_HEADER",
    "NONCE_HEADER",
    "REJECT_REASONS",
    "OwnDhKey",
    "ProviderKey",
    "configuration_problem",
    "mac_v2",
    "new_nonce",
    "own_dh_key",
    "pair_key",
    "parse_service_dh_keys",
    "provider_key",
    "request_auth",
    "response_headers",
    "service_dh_keys",
    "valid_nonce",
    "verify",
]
