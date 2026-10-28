"""Per-pair HMAC request authentication of internal RPC (ADR-0004).

The ONE request authentication of internal RPC: every call carries
``X-SANDLOOM-RPC-Auth`` — an HMAC-SHA256 tag under a key only the calling service
and the addressed provider can derive from their dedicated static X25519 keys
(``RPC_DH_PRIVATE_KEY``, public halves in ``RPC_SERVICE_DH_KEYS``); a per-call
Ed25519 signature would cost ≈ 126 µs per call on both sides, the MAC ≈ 11 µs
(ADR-0004). Commands additionally carry the caller's Ed25519 signature
(:mod:`sandloom.core.rpc_identity`, ``RPC_COMMANDS_REQUIRE_SIGNATURE``).

Key schedule (both sides compute the same key; nothing secret travels)::
    Z     = X25519(own private, peer public)    # cryptography; low-order -> error
    K_req = HKDF-SHA256(Z, salt="sandloom-rpc-pair-mac/v1",
                        info=LP("sandloom-rpc-request-key", "1", "caller->provider",
                                caller, caller kid, provider, provider kid))

Request MAC (verified over the RAW body before anything is decoded)::
    tag = HMAC-SHA256(K_req, LP("sandloom-rpc-request-mac", "1", caller, caller kid,
                                provider, provider kid, HTTP method, target,
                                nonce, raw context bytes, SHA-256(body)))
    X-SANDLOOM-RPC-Auth: v=1;caller=..;ck=..;aud=..;pk=..;mac=<base64url(tag)>

``LP`` is the canonical encoding: a fixed number of fields, each prefixed by
its 4-byte big-endian length (injective; :func:`decode_fields` inverts it).
The response direction keeps the existing keyed response binding
(:mod:`sandloom.core.rpc_binding`, a different salt and info, i.e. an independent
key). Replay: a freshness window (``RPC_AUTH_MAX_SKEW_SECONDS``) plus a
nonce guard (:class:`ReplayGuard`, and a shared ``SET NX`` layer in
:mod:`sandloom.core.rpc`). Threat model and rationale: docs/adr/0004.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import struct
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from sandloom.core import rpc_binding, rpc_identity
from sandloom.core.signing_keys import (
    KeyConfigError,
    load_private_key,
    load_x25519_private_key,
    load_x25519_public_key,
    x25519_public_pem,
)

#: Request header carrying the caller, audience, key ids and the tag.
AUTH_HEADER = "X-SANDLOOM-RPC-Auth"
VERSION = "1"

_SALT = b"sandloom-rpc-pair-mac/v1"
_KEY_LABEL = b"sandloom-rpc-request-key"
_MAC_LABEL = b"sandloom-rpc-request-mac"
_DIRECTION = b"caller->provider"
_HEADER_MAX_LENGTH = 1024
_NONCE = re.compile(r"[0-9a-f]{32}")
#: ids and kids (the response binding's alphabet: no separators, header-safe).
_PARAM = re.compile(r"[A-Za-z0-9._:@+-]{1,128}")

#: Every rejection reason (``sandloom_rpc_auth_rejected_total{reason}``). Only
#: ``expired`` / ``replayed`` / ``replay_unavailable`` / ``forbidden`` are
#: reachable after the tag verified; the client sees one code for the rest.
REJECT_REASONS = (
    "malformed",  # unparsable header / nonce / context
    "version",  # an unknown protocol version
    "wrong_audience",  # aud is not this provider
    "unknown_key",  # caller kid / provider kid unknown here
    "bad_key",  # a published key that cannot agree (low-order point)
    "mac_mismatch",  # tag does not verify
    "expired",  # iat outside the skew window (after the tag verified)
    "replayed",  # nonce already seen (after the tag verified)
    "replay_unavailable",  # nonce guard full / shared layer down (fail closed)
    "caller_quota",  # this caller holds its whole share of the nonce guard
    "required",  # mac-only provider, request not MAC-authenticated
    "signature_required",  # a command without the caller's Ed25519 signature
    "signature_invalid",  # a command whose Ed25519 signature does not verify
)


class PairKeyError(ValueError):
    """No usable pair key (missing / low-order / all-zero agreement)."""


class AuthError(Exception):
    """A request that does not authenticate (status, stable code, reason)."""

    def __init__(self, status_code: int, code: str, reason: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.reason = reason
        self.message = message


def _invalid(reason: str) -> AuthError:
    # One code and message for every pre-verification failure: an
    # unauthenticated party learns nothing about which check failed.
    return AuthError(
        401, "rpc_auth_invalid", reason, "RPC request authentication failed"
    )


# --- canonical encoding ----------------------------------------------------------


def encode_fields(*fields: bytes) -> bytes:
    """``LP``: each field as uint32 big-endian length + bytes (injective)."""
    out = bytearray()
    for field in fields:
        out += len(field).to_bytes(4, "big")
        out += field
    return bytes(out)


def decode_fields(data: bytes, arity: int) -> tuple[bytes, ...]:
    """Inverse of :func:`encode_fields` for exactly ``arity`` fields."""
    fields: list[bytes] = []
    pos = 0
    for _ in range(arity):
        if pos + 4 > len(data):
            raise ValueError("truncated length")
        size = int.from_bytes(data[pos : pos + 4], "big")
        pos += 4
        if pos + size > len(data):
            raise ValueError("truncated field")
        fields.append(data[pos : pos + size])
        pos += size
    if pos != len(data):
        raise ValueError("trailing bytes")
    return tuple(fields)


def key_info(caller: str, caller_kid: str, provider: str, provider_kid: str) -> bytes:
    """HKDF info of ``K_req(caller -> provider)`` (role order + direction)."""
    return encode_fields(
        _KEY_LABEL,
        VERSION.encode(),
        _DIRECTION,
        caller.encode(),
        caller_kid.encode(),
        provider.encode(),
        provider_kid.encode(),
    )


MAC_INPUT_ARITY = 11


def _pair_prefix(
    caller: str, caller_kid: str, provider: str, provider_kid: str
) -> bytes:
    """The first 6 MAC-input fields: fixed per (pair, kids)."""
    return encode_fields(
        _MAC_LABEL,
        VERSION.encode(),
        caller.encode(),
        caller_kid.encode(),
        provider.encode(),
        provider_kid.encode(),
    )


def _call_suffix(
    http_method: str, target: str, nonce: str, context: bytes, body: bytes
) -> bytes:
    """The last 5 MAC-input fields (per call), same encoding as the prefix."""
    method_b = http_method.upper().encode()
    target_b = target.encode()
    nonce_b = nonce.encode()
    return b"".join(
        (
            _U32(len(method_b)),
            method_b,
            _U32(len(target_b)),
            target_b,
            _U32(len(nonce_b)),
            nonce_b,
            _U32(len(context)),
            context,
            _U32_32,
            hashlib.sha256(body).digest(),
        )
    )


def mac_input(
    *,
    caller: str,
    caller_kid: str,
    provider: str,
    provider_kid: str,
    http_method: str,
    target: str,
    nonce: str,
    context: bytes,
    body: bytes,
) -> bytes:
    """The canonical bytes a request tag covers (11 fields).

    Equal to ``encode_fields`` of the 11 fields; computed as the per-pair
    prefix + the per-call suffix (the hot path feeds the prefix into the
    prepared HMAC once per key).
    """
    return _pair_prefix(caller, caller_kid, provider, provider_kid) + _call_suffix(
        http_method, target, nonce, context, body
    )


# --- keys --------------------------------------------------------------------------

_U32 = struct.Struct(">I").pack
_U32_32 = _U32(32)
_CACHE_MAX = 256
#: (own private PEM, peer public PEM, info) -> prepared HMAC context, already
#: fed with the pair's MAC-input prefix (HMAC is a stream: identical tags)
_templates: dict[tuple[str, str, bytes], Any] = {}
#: caller-side resolution: (provider, own pem, own kid, me, peer kid, peer pem)
_caller_keys: dict[tuple[str, ...], CallerKey] = {}
#: provider-side: (caller, caller kid, me, own kid, own pem, peer pem)
_provider_keys: dict[tuple[str, ...], Any] = {}


def clear_key_cache() -> None:
    """Forget every derived key (key rotation in a long-lived process)."""
    _templates.clear()
    _caller_keys.clear()
    _provider_keys.clear()


def derive_key(own_private_pem: str, peer_public_pem: str, info: bytes) -> bytes:
    """HKDF-SHA256 over the X25519 agreement (no caching; see :func:`_template`).

    Raises :class:`PairKeyError` for a peer key that cannot agree (the
    ``cryptography`` exchange refuses low-order points) or an all-zero
    shared secret (checked explicitly as well).
    """
    try:
        shared = load_x25519_private_key(own_private_pem).exchange(
            load_x25519_public_key(peer_public_pem)
        )
    except (ValueError, KeyConfigError) as exc:
        raise PairKeyError("X25519 agreement failed (low-order key?)") from exc
    if not any(shared):
        raise PairKeyError("X25519 agreement is all-zero")
    kdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=info)
    return kdf.derive(shared)


def _template(
    own_private_pem: str,
    peer_public_pem: str,
    caller: str,
    caller_kid: str,
    provider: str,
    provider_kid: str,
) -> Any:
    info = key_info(caller, caller_kid, provider, provider_kid)
    cache_key = (own_private_pem, peer_public_pem, info)
    template = _templates.get(cache_key)
    if template is None:
        key = derive_key(own_private_pem, peer_public_pem, info)
        template = hmac.new(key, digestmod=hashlib.sha256)
        template.update(_pair_prefix(caller, caller_kid, provider, provider_kid))
        if len(_templates) >= _CACHE_MAX:
            _templates.clear()
        _templates[cache_key] = template
    return template


def _tag(template: Any, suffix: bytes) -> str:
    mac = template.copy()  # never reuse MAC state across messages
    mac.update(suffix)
    return _b64(mac.digest())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def published_keys(cfg: Any, service: str) -> dict[str, str]:
    """``RPC_SERVICE_DH_KEYS[service]`` as {kid: PEM}, in configured order."""
    return rpc_binding.service_dh_keys(cfg).get(service, {})


def addressed_kid(cfg: Any, service: str) -> tuple[str, str] | None:
    """The kid callers address: ``RPC_SERVICE_DH_CURRENT_KIDS``, else the
    greatest published kid.

    JSON object order is not a rotation protocol. Operators should pin the
    current kid; the fallback is sorted so reordering a secret document
    cannot change the addressed key.
    """
    keys = published_keys(cfg, service)
    if not keys:
        return None
    current = getattr(cfg, "rpc_service_dh_current_kids", {}) or {}
    kid = current.get(service) or max(keys)
    if kid not in keys:
        raise ValueError(f"RPC_SERVICE_DH_CURRENT_KIDS[{service!r}] is not published")
    return kid, keys[kid]


def own_private_keys(cfg: Any) -> dict[str, str]:
    """This service's X25519 private keys by kid: current + rotation extras."""
    out: dict[str, str] = dict(getattr(cfg, "rpc_dh_extra_private_keys", {}) or {})
    own = rpc_binding.own_dh_key(cfg)
    if own is not None:
        out[own.kid] = own.private_pem
    return out


def _own_private(cfg: Any, kid: str) -> str | None:
    """This service's X25519 private key with ``kid`` (current or extra)."""
    pem = getattr(cfg, "rpc_dh_private_key", None)
    if pem and kid == getattr(cfg, "rpc_dh_key_id", None):
        return str(pem)
    extra = (getattr(cfg, "rpc_dh_extra_private_keys", None) or {}).get(kid)
    return str(extra) if extra else None


@dataclass(frozen=True)
class CallerKey:
    """A caller's prepared key towards one provider."""

    caller: str
    caller_kid: str
    provider: str
    provider_kid: str
    template: Any

    def header(
        self,
        *,
        http_method: str,
        target: str,
        nonce: str,
        context: str,
        body: bytes,
    ) -> str:
        tag = _tag(
            self.template,
            _call_suffix(http_method, target, nonce, context.encode(), body),
        )
        return (
            f"v={VERSION};caller={self.caller};ck={self.caller_kid};"
            f"aud={self.provider};pk={self.provider_kid};mac={tag}"
        )


def caller_key(cfg: Any, provider: str | None) -> CallerKey:
    """This service's key towards ``provider`` (raises :class:`PairKeyError`)."""
    pem = getattr(cfg, "rpc_dh_private_key", None)
    kid = getattr(cfg, "rpc_dh_key_id", None)
    me = rpc_identity.service_id(cfg)
    if not pem or not kid or not me:
        raise PairKeyError("this service has no RPC_DH_PRIVATE_KEY / RPC_DH_KEY_ID")
    if not provider:
        raise PairKeyError("the binding pins no provider service_id")
    addressed = addressed_kid(cfg, provider)
    if addressed is None:
        raise PairKeyError(f"RPC_SERVICE_DH_KEYS has no key for {provider!r}")
    peer_kid, peer_pem = addressed
    cache_key = (provider, pem, kid, me, peer_kid, peer_pem)
    key = _caller_keys.get(cache_key)
    if key is None:
        if not all(_PARAM.fullmatch(v) for v in (provider, kid, me)):
            raise PairKeyError("service ids / kids must match " + _PARAM.pattern)
        key = CallerKey(
            me,
            kid,
            provider,
            peer_kid,
            _template(pem, peer_pem, me, kid, provider, peer_kid),
        )
        if len(_caller_keys) >= _CACHE_MAX:
            _caller_keys.clear()
        _caller_keys[cache_key] = key
    return key


# --- provider ----------------------------------------------------------------------

#: The ONE canonical header form (fixed parameter order, as :class:`CallerKey`
#: writes it). The tag is 43 base64url chars whose last char leaves the two
#: spare bits zero, so exactly one string encodes a given 32-byte tag.
_HEADER = re.compile(
    r"v=([0-9]{1,3});caller=([A-Za-z0-9._:@+-]{1,128});ck=([A-Za-z0-9._:@+-]{1,128});"
    r"aud=([A-Za-z0-9._:@+-]{1,128});pk=([A-Za-z0-9._:@+-]{1,128});"
    r"mac=([A-Za-z0-9_-]{42}[AEIMQUYcgkosw048])"
)


def parse_header(raw: str) -> dict[str, str]:
    """Strictly parse ``X-SANDLOOM-RPC-Auth`` (raises :class:`AuthError`)."""
    if len(raw) > _HEADER_MAX_LENGTH:
        raise _invalid("malformed")
    match = _HEADER.fullmatch(raw)
    if match is None:
        if raw.startswith("v=") and not raw.startswith(f"v={VERSION};"):
            raise _invalid("version")
        raise _invalid("malformed")
    version, caller, ck, aud, pk, mac = match.groups()
    if version != VERSION:
        raise _invalid("version")
    return {"v": version, "caller": caller, "ck": ck, "aud": aud, "pk": pk, "mac": mac}


@dataclass(frozen=True)
class VerifiedCaller:
    service: str
    kid: str


def _provider_template(cfg: Any, caller: str, ck: str, me: str, pk: str) -> Any:
    own_pem = _own_private(cfg, pk)
    peer_pem = published_keys(cfg, caller).get(ck)
    if own_pem is None or peer_pem is None:
        raise _invalid("unknown_key")
    cache_key = (caller, ck, me, pk, own_pem, peer_pem)
    template = _provider_keys.get(cache_key)
    if template is None:
        try:
            template = _template(own_pem, peer_pem, caller, ck, me, pk)
        except PairKeyError as exc:
            raise _invalid("bad_key") from exc
        if len(_provider_keys) >= _CACHE_MAX:
            _provider_keys.clear()
        _provider_keys[cache_key] = template
    return template


def verify_request(
    cfg: Any,
    header: str,
    *,
    http_method: str,
    target: str,
    nonce: str | None,
    context: str | None,
    body: bytes,
) -> VerifiedCaller:
    """Authenticate a request's tag; return the verified caller.

    Order: parse -> audience -> key lookup -> tag (constant time). Freshness
    and replay are checked by the caller of this function AFTER it returns.
    """
    params = parse_header(header)
    if nonce is None or context is None or not _NONCE.fullmatch(nonce):
        raise _invalid("malformed")
    me = rpc_identity.service_id(cfg)
    caller, ck, pk = params["caller"], params["ck"], params["pk"]
    if not me or params["aud"] != me:
        raise _invalid("wrong_audience")
    template = _provider_template(cfg, caller, ck, me, pk)
    expected = _tag(
        template, _call_suffix(http_method, target, nonce, context.encode(), body)
    )
    # Both are 43-char canonical base64url strings (the header regex admits
    # only the canonical encoding): equal strings <=> equal 256-bit tags.
    if not hmac.compare_digest(params["mac"], expected):
        raise _invalid("mac_mismatch")
    return VerifiedCaller(caller, ck)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    return rpc_identity._no_duplicate_keys(pairs)


def decode_context(context: str) -> dict[str, Any]:
    """The (already authenticated) context JSON, strictly (no duplicate keys)."""
    try:
        raw = base64.urlsafe_b64decode(context + "=" * (-len(context) % 4))
        decoded = json.loads(raw, object_pairs_hook=_no_duplicates)
    except (ValueError, binascii.Error) as exc:
        raise _invalid("malformed") from exc
    if not isinstance(decoded, dict):
        raise _invalid("malformed")
    return decoded


def max_skew(cfg: Any) -> int:
    return int(getattr(cfg, "rpc_auth_max_skew_seconds", 60) or 60)


def check_fresh(iat: Any, *, skew: int, now: float | None = None) -> int:
    """``iat`` when ``|now - iat| <= skew`` (raises :class:`AuthError` 401)."""
    now = time.time() if now is None else now
    if (
        isinstance(iat, bool)
        or not isinstance(iat, int)
        or not 0 <= iat < _IAT_MAX  # bounded before any float arithmetic
        or abs(now - iat) > skew
    ):
        raise AuthError(
            401, "rpc_auth_expired", "expired", "RPC request is outside the time window"
        )
    return iat


_IAT_MAX = 2**40  # far beyond any real epoch second


def replay_ttl(iat: int, *, skew: int, now: float | None = None) -> int:
    """Seconds a nonce must be remembered: until ``iat`` can no longer pass
    on THIS clock (the per-process guard)."""
    now = time.time() if now is None else now
    return max(1, int(iat + skew - now) + 2)


def shared_replay_ttl(skew: int) -> int:
    """TTL of a nonce in the SHARED layer: constant, never derived from one
    replica's clock.

    A request is accepted by a replica whose clock reads within ``skew`` of
    its ``iat``. Two replicas whose clocks differ by ``d`` accept it during a
    real-time span of at most ``2 * skew + d`` from its first acceptance; with
    the deployment rule that replica clocks agree within ``skew`` (the same
    bound callers must meet), ``d <= skew`` and ``3 * skew`` covers it.
    """
    return 3 * skew


class ReplayGuard:
    """Per-process ``(caller, nonce)`` table, bounded, never evicting live entries.

    Entries are bucketed by their expiry second and swept lazily; a full
    table of live entries makes :meth:`add` answer ``"full"`` (the provider
    fails closed) instead of forgetting a nonce that could still be replayed.
    """

    def __init__(self, max_entries: int, per_caller: int | None = None) -> None:
        self.max_entries = max_entries
        self.per_caller = per_caller or max_entries
        self._seen: dict[str, int] = {}
        self._buckets: dict[int, list[str]] = {}
        self._per_caller: dict[str, int] = {}
        self._swept = 0

    def __len__(self) -> int:
        return len(self._seen)

    def _sweep(self, now_s: int) -> None:
        if now_s <= self._swept:
            return
        self._swept = now_s
        for second in [b for b in self._buckets if b < now_s]:
            for key in self._buckets.pop(second):
                if self._seen.get(key) == second:
                    del self._seen[key]
                    owner = key.partition("\x00")[0]
                    left = self._per_caller.get(owner, 1) - 1
                    if left > 0:
                        self._per_caller[owner] = left
                    else:
                        self._per_caller.pop(owner, None)

    def add(self, caller: str, nonce: str, ttl: int, now: float | None = None) -> str:
        """``"ok"`` (first sight), ``"replay"``, ``"full"`` (the table) or
        ``"quota"`` (this caller's share: one flooding peer refuses only
        itself, never the other callers)."""
        now_s = int(time.time() if now is None else now)
        self._sweep(now_s)
        key = f"{caller}\x00{nonce}"  # one str per entry (memory)
        expiry = self._seen.get(key)
        if expiry is not None and expiry >= now_s:
            return "replay"
        if len(self._seen) >= self.max_entries:
            return "full"
        held = self._per_caller.get(caller, 0)
        if held >= self.per_caller:
            return "quota"
        expiry = now_s + ttl
        self._seen[key] = expiry
        self._per_caller[caller] = held + 1
        self._buckets.setdefault(expiry, []).append(key)
        return "ok"


_guard: list[ReplayGuard] = []


def replay_guard(cfg: Any) -> ReplayGuard:
    """This process's guard (sized by ``RPC_REPLAY_CACHE_MAX_ENTRIES``)."""
    size = int(getattr(cfg, "rpc_replay_cache_max_entries", 200_000) or 200_000)
    if not _guard:
        _guard.append(ReplayGuard(size))
    # Never rebuilt (that would forget live nonces): only the bounds follow
    # the configuration.
    _guard[0].max_entries = size
    _guard[0].per_caller = min(
        size,
        int(getattr(cfg, "rpc_replay_cache_per_caller_max_entries", 0) or size // 2),
    )
    return _guard[0]


def commands_require_signature(cfg: Any) -> bool:
    """Do COMMAND calls need the caller's Ed25519 signature on top of the MAC?

    The KCI mitigation (ADR-0004): a leaked provider X25519 key derives every
    caller's pair key, so the MAC alone authenticates no effect against it.
    """
    value = getattr(cfg, "rpc_commands_require_signature", "auto")
    if value in ("on", "all"):
        return True
    if value == "off":
        return False
    return bool(getattr(cfg, "is_prod", False))


def all_calls_require_signature(cfg: Any) -> bool:
    """``RPC_COMMANDS_REQUIRE_SIGNATURE=all``: reads are signed too."""
    return getattr(cfg, "rpc_commands_require_signature", "auto") == "all"


def stale_extra_kids(cfg: Any) -> list[str]:
    """Kids in RPC_DH_EXTRA_PRIVATE_KEYS no longer published for this service.

    A retired key kept here still lets its holder (and anyone who leaked it)
    derive every caller's pair key towards this provider: drop it once the
    rotation's overlap window is over.
    """
    me = rpc_identity.service_id(cfg)
    published = published_keys(cfg, me) if me else {}
    extras = getattr(cfg, "rpc_dh_extra_private_keys", {}) or {}
    return sorted(kid for kid in extras if kid not in published)


def shared_replay_key(provider: str, caller: str, nonce: str) -> str:
    """Collision-free key (ids may contain ``:``): a digest of LP(fields)."""
    digest = hashlib.sha256(
        encode_fields(provider.encode(), caller.encode(), nonce.encode())
    ).hexdigest()
    return f"sandloom:rpc:nonce:{digest}"


# --- configuration -----------------------------------------------------------------


def _x25519_raw(pem: str) -> bytes:
    handle: Any = load_x25519_private_key(pem)
    raw: bytes = handle.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return raw


def _ed25519_seed(pem: str) -> bytes | None:
    try:
        handle: Any = load_private_key(pem, "EdDSA")
    except KeyConfigError:
        return None
    raw: bytes = handle.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return raw


def configuration_problem(cfg: Any) -> str | None:  # noqa: C901
    """Why this process's pair-MAC configuration cannot work, or ``None``.

    Always: ``RPC_DH_EXTRA_PRIVATE_KEYS`` parses (X25519, distinct kids,
    matching its published half if published); no two services publish the
    same X25519 key; the X25519 key is not the Ed25519 signing seed; the
    replay-guard limits are coherent. A CALLER (``SERVICE_BINDINGS``) also
    needs its own X25519 key (published-half match is
    :func:`sandloom.core.rpc_binding.configuration_problem`), every binding pinned
    to a provider whose addressed X25519 key agrees with ours (derived here:
    low-order keys fail at startup, not on the first call), and — with
    command signatures on — its Ed25519 signing key.
    """
    try:
        published = rpc_binding.parse_service_dh_keys(
            getattr(cfg, "rpc_service_dh_keys", {}) or {}
        )
    except ValueError as exc:
        return str(exc)
    current = getattr(cfg, "rpc_service_dh_current_kids", {}) or {}
    for service, kid in current.items():
        if kid not in published.get(service, {}):
            return f"RPC_SERVICE_DH_CURRENT_KIDS[{service!r}] is not a published kid"
    seen: dict[str, tuple[str, str]] = {}
    for service, keys in published.items():
        for kid, pem in keys.items():
            normal = x25519_public_pem(load_x25519_public_key(pem))
            other = seen.setdefault(normal, (service, kid))
            if other[0] != service:
                return (
                    f"RPC_SERVICE_DH_KEYS: {other[0]}/{other[1]} and {service}/{kid} "
                    "are the same X25519 key — two services sharing a key can "
                    "impersonate each other"
                )
    me = rpc_identity.service_id(cfg)
    own = rpc_binding.own_dh_key(cfg)
    if own is not None and me in current and current[me] not in own_private_keys(cfg):
        return (
            "RPC_SERVICE_DH_CURRENT_KIDS selects a local kid whose private key "
            "is unavailable; install it before directing callers to it"
        )
    extras = getattr(cfg, "rpc_dh_extra_private_keys", {}) or {}
    if not isinstance(extras, Mapping):
        return 'RPC_DH_EXTRA_PRIVATE_KEYS must be a {"<kid>": "<PEM>"} object'
    for kid, pem in extras.items():
        if (
            not isinstance(kid, str)
            or not _PARAM.fullmatch(kid)
            or not isinstance(pem, str)
        ):
            return f"RPC_DH_EXTRA_PRIVATE_KEYS: kid {kid!r} / key must be strings"
        if own is not None and kid == own.kid:
            return f"RPC_DH_EXTRA_PRIVATE_KEYS[{kid!r}] repeats RPC_DH_KEY_ID"
        try:
            private = load_x25519_private_key(pem)
        except KeyConfigError as exc:
            return f"RPC_DH_EXTRA_PRIVATE_KEYS[{kid!r}]: {exc}"
        mine = published.get(me or "", {}).get(kid)
        if mine is not None and x25519_public_pem(
            load_x25519_public_key(mine)
        ) != x25519_public_pem(private):
            return (
                f"RPC_SERVICE_DH_KEYS[{me!r}][{kid!r}] is not the public half of "
                f"RPC_DH_EXTRA_PRIVATE_KEYS[{kid!r}]"
            )
    signing = getattr(cfg, "rpc_private_key", None)
    if own is not None and signing:
        seed = _ed25519_seed(signing)
        try:
            dh_raw = _x25519_raw(own.private_pem)
        except KeyConfigError:
            dh_raw = None
        if seed is not None and seed == dh_raw:
            return (
                "RPC_DH_PRIVATE_KEY reuses the RPC_PRIVATE_KEY seed: "
                "a key-agreement key must never be a signing key "
                "(`sandloom keys generate --purpose rpc`)"
            )
    if getattr(cfg, "is_prod", False) and (
        getattr(cfg, "rpc_replay_guard", "commands") == "local"
    ):
        return (
            "RPC_REPLAY_GUARD=local in prod: a replay to another worker / "
            "replica would go undetected; use commands (the default) or all"
        )
    if getattr(cfg, "rpc_replay_cache_per_caller_max_entries", 0) > getattr(
        cfg, "rpc_replay_cache_max_entries", 1
    ):
        return (
            "RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES exceeds "
            "RPC_REPLAY_CACHE_MAX_ENTRIES"
        )
    bindings: Mapping[str, Mapping[str, Any]] = (
        getattr(cfg, "service_bindings", {}) or {}
    )
    if not bindings:
        return None
    if own is None:
        return (
            "SERVICE_BINDINGS: internal RPC authenticates every call with a "
            "per-pair key — set RPC_DH_PRIVATE_KEY + RPC_DH_KEY_ID and "
            "RPC_SERVICE_ID (`sandloom keys generate --purpose rpc-dh --service <id>`)"
        )
    unpinned = sorted(n for n, b in bindings.items() if not b.get("service_id"))
    if unpinned:
        return (
            "internal RPC derives each call's key from the provider's identity: "
            f'SERVICE_BINDINGS {unpinned} must name the provider service ("service_id")'
        )
    for name, binding in sorted(bindings.items()):
        provider = str(binding["service_id"])
        try:
            caller_key(cfg, provider)
        except PairKeyError as exc:
            return f"SERVICE_BINDINGS[{name!r}] -> {provider!r}: {exc}"
    if commands_require_signature(cfg) and not signing:
        return (
            "SERVICE_BINDINGS with RPC_COMMANDS_REQUIRE_SIGNATURE on (the "
            "prod default): a caller signs its COMMAND calls with its Ed25519 "
            "identity — set RPC_PRIVATE_KEY + RPC_KEY_ID and publish it in "
            "RPC_SERVICE_KEYS (`sandloom keys generate --purpose rpc`)"
        )
    return None


__all__ = [
    "AUTH_HEADER",
    "REJECT_REASONS",
    "VERSION",
    "AuthError",
    "CallerKey",
    "PairKeyError",
    "ReplayGuard",
    "VerifiedCaller",
    "addressed_kid",
    "caller_key",
    "check_fresh",
    "clear_key_cache",
    "all_calls_require_signature",
    "commands_require_signature",
    "configuration_problem",
    "decode_context",
    "decode_fields",
    "derive_key",
    "encode_fields",
    "key_info",
    "mac_input",
    "max_skew",
    "own_private_keys",
    "parse_header",
    "published_keys",
    "replay_guard",
    "replay_ttl",
    "shared_replay_ttl",
    "shared_replay_key",
    "stale_extra_kids",
    "verify_request",
]
