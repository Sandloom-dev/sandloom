"""Pair-MAC request authentication of internal RPC (ADR-0004).

Every internal RPC request is authenticated by an HMAC under a per-pair,
directional key derived from the two services' X25519 keys (no per-call
Ed25519 signature; commands also carry one, the KCI mitigation). Covered here: the key schedule (agreement, direction, domain
separation, low-order points), the canonical encoding (injective; fuzzed),
committed test vectors checked against an independent RFC 5869 HKDF, every
negative case of the provider (wrong audience, wrong direction / purpose key,
swapped ids, each MAC field tampered, truncated / non-canonical tags,
replayed nonces, expired / future issue times, unknown kids, downgrade
attempts), the replay guard (per process + the shared layer), error mapping
at the caller, key rotation, startup validation and the transport retry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import random
import struct
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import x25519
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from pydantic import ValidationError

from sandloom.core import rpc, rpc_binding, rpc_identity, rpc_pair_mac
from sandloom.core.app_factory import ConfigurationError, enforce_configuration
from sandloom.core.cli import keys as keys_cli
from sandloom.core.config.settings import Settings, settings
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import RemoteServiceProxy, RemoteServiceUnavailable
from sandloom.core.rpc import client as rpc_client
from sandloom.core.rpc import context as rpc_context
from sandloom.core.rpc import dispatch as rpc_dispatch
from sandloom.core.rpc import errors as rpc_errors
from sandloom.core.rpc import inbound_auth as rpc_inbound_auth
from sandloom.core.rpc import outbound as rpc_outbound
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.signing_keys import (
    generate_private_key,
    generate_x25519_key,
    load_private_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)

SERVICE = "demo.pair.v1"
VECTORS = Path(__file__).parent / "vectors" / "rpc_pair_mac_v1.json"

#: A configuration is refused at load (``Settings``) or at boot
#: (:func:`sandloom.core.app_factory.enforce_configuration`).
_REFUSED = (ValidationError, ConfigurationError)


def _dh_pair() -> tuple[str, str]:
    key = generate_x25519_key()
    return x25519_private_pem(key), x25519_public_pem(key)


def _ed_pair() -> tuple[str, str]:
    key = generate_private_key("EdDSA")
    return private_pem(key), public_pem(key)


DH = {
    n: _dh_pair()
    for n in ("billing", "billing-next", "tenancy", "tenancy-next", "audit", "rogue")
}
ED = {n: _ed_pair() for n in ("billing", "tenancy")}
SERVICE_DH_KEYS = {
    "billing": {"b1": DH["billing"][1]},
    "tenancy": {"t1": DH["tenancy"][1]},
    "audit": {"a1": DH["audit"][1]},
    "rogue": {"r1": DH["rogue"][1]},
}
SERVICE_KEYS = {
    "billing": {"e1": ED["billing"][1]},
    "tenancy": {"e1": ED["tenancy"][1]},
}
#: Every X25519 low-order point (RFC 7748 §6.1 / Bernstein's list), incl.
#: non-canonical encodings (>= p): the agreement must refuse all of them.
LOW_ORDER = [
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",
    "5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "cdeb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b880",
    "4c9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f11d7",
    "d9ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "daffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "dbffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
]


def _low_order_pem(hex_point: str) -> str:
    """A SubjectPublicKeyInfo PEM wrapping a raw (possibly invalid) point."""
    der = bytes.fromhex("302a300506032b656e032100") + bytes.fromhex(hex_point)
    b64 = base64.b64encode(der).decode()
    return f"-----BEGIN PUBLIC KEY-----\n{b64}\n-----END PUBLIC KEY-----\n"


def _rejected(reason: str) -> float:
    value = REGISTRY.get_sample_value(
        "sandloom_rpc_auth_rejected_total", {"reason": reason}
    )
    return float(value or 0.0)


@pytest.fixture(autouse=True)
def _fresh_state() -> Any:
    rpc_pair_mac.clear_key_cache()
    rpc_pair_mac._guard.clear()
    rpc_inbound_auth._auth_logged.clear()
    yield
    rpc_pair_mac.clear_key_cache()
    rpc_pair_mac._guard.clear()


# ---------------------------------------------------------------------------
# key schedule
# ---------------------------------------------------------------------------


def _key(own: str, peer: str, caller: str, ck: str, provider: str, pk: str) -> bytes:
    info = rpc_pair_mac.key_info(caller, ck, provider, pk)
    return rpc_pair_mac.derive_key(DH[own][0], DH[peer][1], info)


def test_both_sides_derive_the_same_directional_pair_key() -> None:
    at_caller = _key("billing", "tenancy", "billing", "b1", "tenancy", "t1")
    at_provider = _key("tenancy", "billing", "billing", "b1", "tenancy", "t1")
    assert at_caller == at_provider and len(at_caller) == 32
    # the reverse direction (tenancy calling billing) is another key
    reverse = _key("tenancy", "billing", "tenancy", "t1", "billing", "b1")
    assert reverse != at_caller
    # another pair / another kid is another key
    assert _key("billing", "audit", "billing", "b1", "audit", "a1") != at_caller
    assert _key("billing", "tenancy", "billing", "b1", "tenancy", "t2") != at_caller


def test_request_key_is_domain_separated_from_the_response_binding_key() -> None:
    k_req = _key("billing", "tenancy", "billing", "b1", "tenancy", "t1")
    k_resp = rpc_binding.pair_key(
        DH["billing"][0],
        DH["tenancy"][1],
        rpc_binding.pair_info("billing", "b1", "tenancy", "t1"),
    )
    assert k_req != k_resp
    # same IKM, but neither salt nor info coincide
    assert rpc_pair_mac._SALT != rpc_binding._HKDF_SALT
    assert rpc_pair_mac.key_info("billing", "b1", "tenancy", "t1") != (
        rpc_binding.pair_info("billing", "b1", "tenancy", "t1")
    )


@pytest.mark.parametrize("point", LOW_ORDER)
def test_low_order_points_never_yield_a_predictable_key(point: str) -> None:
    """The canonical low-order points (and their encodings >= p) are refused.

    The last five entries of the classic blacklist set the top bit; RFC 7748
    masks it, so they decode to points that are NOT of low order — the
    agreement then depends on our private key (two keys -> two secrets),
    which is all the check protects.
    """
    info = rpc_pair_mac.key_info("billing", "b1", "evil", "e1")
    pem = _low_order_pem(point)
    canonical_low_order = LOW_ORDER.index(point) < 7
    try:
        first = rpc_pair_mac.derive_key(DH["billing"][0], pem, info)
    except rpc_pair_mac.PairKeyError:
        return
    assert not canonical_low_order, "a low-order point produced a key"
    assert first != rpc_pair_mac.derive_key(DH["tenancy"][0], pem, info)


def test_all_zero_agreement_is_refused_even_if_the_library_returned_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Zero:
        def exchange(self, _peer: Any) -> bytes:
            return bytes(32)

    monkeypatch.setattr(rpc_pair_mac, "load_x25519_private_key", lambda _pem: Zero())
    with pytest.raises(rpc_pair_mac.PairKeyError, match="all-zero"):
        rpc_pair_mac.derive_key("x", DH["tenancy"][1], b"info")


# ---------------------------------------------------------------------------
# canonical encoding
# ---------------------------------------------------------------------------


def test_encoding_is_injective_on_classic_ambiguities() -> None:
    enc = rpc_pair_mac.encode_fields
    assert enc(b"a", b"bc") != enc(b"ab", b"c")
    assert enc(b"", b"x") != enc(b"x", b"")
    assert enc(b"a;b", b"c") != enc(b"a", b"b;c")
    # a field that looks like a length prefix + field cannot shift boundaries
    assert enc(b"\x00\x00\x00\x01a", b"") != enc(b"", b"\x00\x00\x00\x01a")


def test_encoding_round_trips_and_never_collides_fuzz() -> None:
    rng = random.Random(20261021)
    alphabet = [b"", b"\x00", b"\x00\x00\x00\x01", b"a", b";", b"=", b"\xff"]
    seen: dict[bytes, tuple[bytes, ...]] = {}
    for _ in range(20_000):
        arity = rng.randint(1, 11)
        fields = tuple(
            b"".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4)))
            if rng.random() < 0.7
            else rng.randbytes(rng.randint(0, 40))
            for _ in range(arity)
        )
        encoded = rpc_pair_mac.encode_fields(*fields)
        assert rpc_pair_mac.decode_fields(encoded, arity) == fields
        other = seen.setdefault(encoded, fields)
        assert other == fields, (other, fields)


def test_decoder_rejects_truncation_and_trailing_bytes() -> None:
    data = rpc_pair_mac.encode_fields(b"ab", b"c")
    for cut in range(len(data)):
        with pytest.raises(ValueError):
            rpc_pair_mac.decode_fields(data[:cut], 2)
    with pytest.raises(ValueError):
        rpc_pair_mac.decode_fields(data + b"\x00", 2)
    with pytest.raises(ValueError):
        rpc_pair_mac.decode_fields(data, 3)


def test_mac_input_fields_all_change_the_encoding() -> None:
    base: dict[str, Any] = {
        "caller": "billing",
        "caller_kid": "b1",
        "provider": "tenancy",
        "provider_kid": "t1",
        "http_method": "POST",
        "target": "c.v1/m",
        "nonce": "0" * 32,
        "context": b"ctx",
        "body": b"body",
    }
    reference = rpc_pair_mac.mac_input(**base)
    assert (
        len(rpc_pair_mac.decode_fields(reference, rpc_pair_mac.MAC_INPUT_ARITY)) == 11
    )
    for name, value in base.items():
        changed = {
            **base,
            name: (value + b"x") if isinstance(value, bytes) else value + "x",
        }
        assert rpc_pair_mac.mac_input(**changed) != reference, name


# ---------------------------------------------------------------------------
# committed test vectors (independent HKDF / HMAC / encoding)
# ---------------------------------------------------------------------------


def _rfc5869(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def _lp(*fields: bytes) -> bytes:
    return b"".join(struct.pack(">I", len(f)) + f for f in fields)


def test_committed_vectors() -> None:
    doc = json.loads(VECTORS.read_text())
    assert doc["version"] == rpc_pair_mac.VERSION
    for case in doc["cases"]:
        c = x25519.X25519PrivateKey.from_private_bytes(
            bytes.fromhex(case["caller_private_raw"])
        )
        p = x25519.X25519PrivateKey.from_private_bytes(
            bytes.fromhex(case["provider_private_raw"])
        )
        assert (
            cast(Any, c.public_key()).public_bytes_raw().hex()
            == case["caller_public_raw"]
        )
        z = c.exchange(p.public_key())
        assert z.hex() == case["shared_secret"]
        info = _lp(
            b"sandloom-rpc-request-key",
            b"1",
            b"caller->provider",
            case["caller"].encode(),
            case["caller_kid"].encode(),
            case["provider"].encode(),
            case["provider_kid"].encode(),
        )
        assert info.hex() == case["hkdf_info"]
        key = _rfc5869(z, case["hkdf_salt"].encode(), info)
        assert key.hex() == case["request_key"]
        # the module agrees (both sides)
        assert (
            rpc_pair_mac.derive_key(x25519_private_pem(c), x25519_public_pem(p), info)
            == key
        )
        assert (
            rpc_pair_mac.derive_key(x25519_private_pem(p), x25519_public_pem(c), info)
            == key
        )
        body = bytes.fromhex(case["body"])
        message = _lp(
            b"sandloom-rpc-request-mac",
            b"1",
            case["caller"].encode(),
            case["caller_kid"].encode(),
            case["provider"].encode(),
            case["provider_kid"].encode(),
            case["http_method"].encode(),
            case["target"].encode(),
            case["nonce"].encode(),
            case["context"].encode(),
            hashlib.sha256(body).digest(),
        )
        assert message.hex() == case["mac_input"]
        tag = hmac.new(key, message, hashlib.sha256).digest()
        assert base64.urlsafe_b64encode(tag).decode().rstrip("=") == case["tag"]
        own = {
            "rpc_service_id": case["caller"],
            "rpc_dh_private_key": x25519_private_pem(c),
            "rpc_dh_key_id": case["caller_kid"],
            "rpc_service_dh_keys": {
                case["provider"]: {case["provider_kid"]: x25519_public_pem(p)}
            },
        }
        caller_cfg = settings.model_copy(update=own)
        header = rpc_pair_mac.caller_key(caller_cfg, case["provider"]).header(
            http_method=case["http_method"],
            target=case["target"],
            nonce=case["nonce"],
            context=case["context"],
            body=body,
        )
        assert header == case["header"]


# ---------------------------------------------------------------------------
# provider-side verification (unit)
# ---------------------------------------------------------------------------


def _cfg(me: str, kid: str, **extra: Any) -> Any:
    return settings.model_copy(
        update={
            "rpc_service_id": me,
            "rpc_dh_private_key": DH[me][0],
            "rpc_dh_key_id": kid,
            "rpc_service_dh_keys": SERVICE_DH_KEYS,
            "rpc_dh_extra_private_keys": {},
            **extra,
        }
    )


CALLER = _cfg("billing", "b1")
PROVIDER = _cfg("tenancy", "t1")
NONCE = "a" * 32
CONTEXT = base64.urlsafe_b64encode(b'{"iat":1}').decode().rstrip("=")
BODY = b"\x80"


def _header(cfg: Any = CALLER, provider: str = "tenancy", **over: Any) -> str:
    args = {
        "http_method": "POST",
        "target": "c.v1/m",
        "nonce": NONCE,
        "context": CONTEXT,
        "body": BODY,
        **over,
    }
    return rpc_pair_mac.caller_key(cfg, provider).header(**args)


def _verify(header: str, cfg: Any = PROVIDER, **over: Any) -> Any:
    args = {
        "http_method": "POST",
        "target": "c.v1/m",
        "nonce": NONCE,
        "context": CONTEXT,
        "body": BODY,
        **over,
    }
    return rpc_pair_mac.verify_request(cfg, header, **args)


def _reason(header: str, cfg: Any = PROVIDER, **over: Any) -> str:
    with pytest.raises(rpc_pair_mac.AuthError) as exc:
        _verify(header, cfg, **over)
    # one stable client-facing code for every pre-verification failure
    assert exc.value.code == "rpc_auth_invalid" and exc.value.status_code == 401
    return exc.value.reason


def test_a_genuine_request_verifies() -> None:
    caller = _verify(_header())
    assert (caller.service, caller.kid) == ("billing", "b1")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("http_method", "GET"),
        ("target", "c.v1/other"),
        ("target", "other.v1/m"),
        ("nonce", "b" * 32),
        ("context", base64.urlsafe_b64encode(b'{"iat":2}').decode().rstrip("=")),
        ("body", b"\x81"),
        ("body", b""),
    ],
)
def test_each_tampered_mac_field_is_refused(field: str, value: Any) -> None:
    assert _reason(_header(), **{field: value}) == "mac_mismatch"


def _edit(header: str, name: str, value: str) -> str:
    parts = dict(p.split("=", 1) for p in header.split(";"))
    parts[name] = value
    return ";".join(f"{k}={v}" for k, v in parts.items())


def test_wrong_audience_is_refused() -> None:
    # a request for tenancy presented to audit (also knows billing)
    audit = _cfg("audit", "a1")
    assert _reason(_header(), audit) == "wrong_audience"
    # rewriting aud to audit: the tag was made under K(billing->tenancy)
    assert _reason(_edit(_header(), "aud", "audit"), audit) == "unknown_key"
    forged = _edit(_edit(_header(), "aud", "audit"), "pk", "a1")
    assert _reason(forged, audit) == "mac_mismatch"


def test_swapped_ids_and_wrong_direction_key_are_refused() -> None:
    # caller / audience swapped: tenancy "calling" billing with billing's tag
    swapped = _edit(_edit(_header(), "caller", "tenancy"), "aud", "billing")
    swapped = _edit(_edit(swapped, "ck", "t1"), "pk", "b1")
    assert _reason(swapped, _cfg("billing", "b1")) == "mac_mismatch"
    # a tag under the REVERSE direction key K(tenancy->billing)
    reverse = rpc_pair_mac.derive_key(
        DH["tenancy"][0],
        DH["billing"][1],
        rpc_pair_mac.key_info("tenancy", "t1", "billing", "b1"),
    )
    message = rpc_pair_mac.mac_input(
        caller="billing",
        caller_kid="b1",
        provider="tenancy",
        provider_kid="t1",
        http_method="POST",
        target="c.v1/m",
        nonce=NONCE,
        context=CONTEXT.encode(),
        body=BODY,
    )
    for key in (
        reverse,
        rpc_binding.pair_key(  # the response-binding key of the same pair
            DH["billing"][0],
            DH["tenancy"][1],
            rpc_binding.pair_info("billing", "b1", "tenancy", "t1"),
        ),
    ):
        tag = hmac.new(key, message, hashlib.sha256).digest()
        forged = _edit(_header(), "mac", rpc_pair_mac._b64(tag))
        assert _reason(forged) == "mac_mismatch"


def test_a_peer_cannot_forge_another_callers_request() -> None:
    # rogue knows its own key and every public key; it claims to be billing
    rogue = _cfg("rogue", "r1")
    tag_key = rpc_pair_mac.derive_key(
        DH["rogue"][0],
        DH["tenancy"][1],
        rpc_pair_mac.key_info("billing", "b1", "tenancy", "t1"),
    )
    message = rpc_pair_mac.mac_input(
        caller="billing",
        caller_kid="b1",
        provider="tenancy",
        provider_kid="t1",
        http_method="POST",
        target="c.v1/m",
        nonce=NONCE,
        context=CONTEXT.encode(),
        body=BODY,
    )
    tag = hmac.new(tag_key, message, hashlib.sha256).digest()
    forged = _edit(_header(), "mac", rpc_pair_mac._b64(tag))
    assert _reason(forged) == "mac_mismatch"
    # and its own genuine request names rogue, not billing
    assert _verify(_header(rogue)).service == "rogue"


def test_unknown_kids_are_refused() -> None:
    assert _reason(_edit(_header(), "ck", "b9")) == "unknown_key"
    assert _reason(_edit(_header(), "pk", "t9")) == "unknown_key"
    assert _reason(_edit(_header(), "caller", "nobody")) == "unknown_key"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h[: h.rindex("=") + 1] + h[h.rindex("=") + 1 : -1],  # 42 chars
        lambda h: h + "A",  # 44 chars
        lambda h: h + "=",  # padding
        lambda h: h[:-1] + ("B" if h[-1] == "A" else "A"),  # spare bits / other tag
        lambda h: h[: h.rindex("=") + 1] + "",  # empty tag
        lambda h: h.replace("v=1", "v=2"),
        lambda h: h.replace("v=1;", ""),
        lambda h: h + ";x=1",
        lambda h: h + ";v=1",
        lambda h: h.replace("caller=billing", "caller=bil ling"),
        lambda h: h.replace(";", ",", 1),
        lambda h: h + ";" + "x" * 1024,
        lambda h: "",
    ],
    ids=[
        "truncated-tag",
        "long-tag",
        "padded-tag",
        "altered-tag",
        "empty-tag",
        "unknown-version",
        "no-version",
        "unknown-param",
        "duplicate-param",
        "bad-id",
        "bad-separator",
        "oversized",
        "empty",
    ],
)
def test_malformed_headers_and_tags_are_refused(mutate: Callable[[str], str]) -> None:
    assert _reason(mutate(_header())) in {"malformed", "version", "mac_mismatch"}


def test_non_canonical_tag_encoding_is_refused() -> None:
    header = _header()
    tag_text = header.rsplit("=", 1)[1]
    raw = base64.urlsafe_b64decode(tag_text + "=")
    # set the two spare low bits of the last base64 char: same 32 bytes
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(tag_text[-1])
    twin = tag_text[:-1] + alphabet[last | 0b11]
    assert twin != tag_text
    assert base64.urlsafe_b64decode(twin + "=") == raw
    assert _reason(header.replace(tag_text, twin)) == "malformed"


@pytest.mark.parametrize("nonce", [None, "", "A" * 32, "a" * 31, "a" * 33, "g" * 32])
def test_nonce_must_be_128_bit_lowercase_hex(nonce: str | None) -> None:
    assert _reason(_header(), nonce=nonce) == "malformed"


def test_low_order_published_caller_key_is_refused_at_verification() -> None:
    cfg = _cfg(
        "tenancy",
        "t1",
        rpc_service_dh_keys={
            **SERVICE_DH_KEYS,
            "evil": {"e1": _low_order_pem(LOW_ORDER[2])},
        },
    )
    forged = _edit(_edit(_header(), "caller", "evil"), "ck", "e1")
    assert _reason(forged, cfg) == "bad_key"


def test_freshness_window_is_symmetric_and_strict() -> None:
    now = 1_000_000.0
    for iat in (1_000_000 - 60, 1_000_000, 1_000_000 + 60):
        assert rpc_pair_mac.check_fresh(iat, skew=60, now=now) == iat
    bad: list[Any] = [1_000_000 - 61, 1_000_000 + 61, True, "1000000", None, 1.0e6]
    for iat in bad:
        with pytest.raises(rpc_pair_mac.AuthError) as exc:
            rpc_pair_mac.check_fresh(iat, skew=60, now=now)
        assert exc.value.code == "rpc_auth_expired"


def test_replay_ttl_outlives_the_acceptance_window() -> None:
    for offset in (-60, -1, 0, 30, 60):
        now = 1_000_000.0
        iat = int(now) + offset
        ttl = rpc_pair_mac.replay_ttl(iat, skew=60, now=now)
        # the last instant the request still passes check_fresh
        assert now + ttl > iat + 60
        assert ttl <= 2 * 60 + 2


def test_replay_guard_detects_expires_and_fails_closed_when_full() -> None:
    guard = rpc_pair_mac.ReplayGuard(max_entries=3)
    assert guard.add("billing", NONCE, 10, now=100.0) == "ok"
    assert guard.add("billing", NONCE, 10, now=105.0) == "replay"
    assert guard.add("audit", NONCE, 10, now=105.0) == "ok"  # per caller
    assert guard.add("billing", "b" * 32, 10, now=105.0) == "ok"
    assert guard.add("billing", "c" * 32, 10, now=105.0) == "full"  # live entries
    assert guard.add("billing", NONCE, 10, now=110.0) == "replay"  # still live
    # after expiry the sweep frees room; an expired nonce is accepted again
    # (the request itself would fail the time window by then)
    assert guard.add("billing", "c" * 32, 10, now=112.0) == "ok"
    assert len(guard) == 3  # billing/NONCE (expired at 110) was swept
    assert guard.add("billing", NONCE, 10, now=112.0) == "full"
    assert guard.add("audit", NONCE, 10, now=116.0) == "ok"  # all swept


def test_context_with_duplicate_keys_is_refused() -> None:
    raw = base64.urlsafe_b64encode(b'{"iat":1,"tenant_id":"a","tenant_id":"b"}')
    with pytest.raises(rpc_pair_mac.AuthError):
        rpc_pair_mac.decode_context(raw.decode().rstrip("="))


# ---------------------------------------------------------------------------
# a real provider app, the proxy as caller
# ---------------------------------------------------------------------------


class _UoW:
    session: Any = None

    async def __aenter__(self) -> _UoW:
        self.session = type("S", (), {"info": {}})()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.session = None


class _Database:
    def uow_factory(self) -> Callable[[], _UoW]:
        return _UoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


#: Effects of the ``charge`` command (proves whether it ran).
CHARGED: list[int] = []


def _provider_app(cfg: Any) -> FastAPI:
    from sandloom.core.di import current_tenant_id_optional

    app = FastAPI()
    core = CoreFacade(app=app, settings_obj=cfg)
    app.state.core = core
    core.database = lambda name="default": _Database()  # type: ignore[method-assign]

    class Svc:
        @classmethod
        @contract_method
        async def echo(cls, value: str) -> str:
            return f"r-{value}"

        @classmethod
        @contract_method
        async def whose_tenant(cls) -> str:
            tid = current_tenant_id_optional()
            return str(tid) if tid else "none"

        @classmethod
        @contract_method
        async def admin_only(cls) -> str:
            return "admin"

        @classmethod
        @contract_method(command=True, idempotency_key="key")
        async def charge(cls, key: str, amount: int) -> int:
            CHARGED.append(amount)
            return amount

    core.register_service(SERVICE, Svc)
    rpc.install_service_rpc_routes(app, core)
    return app


def _provider_cfg(**extra: Any) -> Any:
    """tenancy, provider-only; allows billing everything but admin_only."""
    return settings.model_copy(
        update={
            "rpc_service_id": "tenancy",
            "rpc_private_key": None,
            "rpc_key_id": None,
            "rpc_dh_private_key": DH["tenancy"][0],
            "rpc_dh_key_id": "t1",
            "rpc_dh_extra_private_keys": {},
            "rpc_replay_guard": "local",
            "rpc_allowed_calls": {
                "billing": {SERVICE: ["echo", "whose_tenant", "charge"]},
                "rogue": [SERVICE],
            },
            **extra,
        }
    )


@pytest.fixture
def caller(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The process settings = the CALLER: billing."""
    for name, value in {
        "internal_api_token": "shared-secret",
        "internal_endpoints_open_in_dev": False,
        "rpc_service_id": "billing",
        "rpc_private_key": ED["billing"][0],
        "rpc_key_id": "e1",
        "rpc_service_keys": SERVICE_KEYS,
        "rpc_service_dh_keys": SERVICE_DH_KEYS,
        "rpc_dh_private_key": DH["billing"][0],
        "rpc_dh_key_id": "b1",
        "rpc_dh_extra_private_keys": {},
        "rpc_allowed_calls": {"billing": [SERVICE]},
    }.items():
        monkeypatch.setattr(settings, name, value)
    return settings


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []

    async def request(self, request: httpx.Request) -> None:
        await request.aread()
        self.requests.append(request)

    async def response(self, response: httpx.Response) -> None:
        self.responses.append(response)


def _client(app: FastAPI, rec: _Recorder | None = None) -> AsyncClient:
    hooks: dict[str, list[Callable[..., Any]]] = (
        {"request": [rec.request], "response": [rec.response]} if rec else {}
    )
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://tenancy", event_hooks=hooks
    )


def _proxy(
    app: FastAPI, rec: _Recorder | None = None, service_id: str | None = "tenancy"
) -> RemoteServiceProxy:
    binding: dict[str, object] = {"base_url": "http://tenancy"}
    if service_id:
        binding["service_id"] = service_id
    return RemoteServiceProxy(
        service_name=SERVICE,
        binding=binding,
        default_timeout_seconds=5.0,
        client=_client(app, rec),
    )


async def _send(app: FastAPI, request: httpx.Request, **header_edits: Any) -> Any:
    headers = {
        k: v for k, v in request.headers.items() if k.lower() != "content-length"
    }
    for name, value in header_edits.items():
        name = name.replace("_", "-")
        headers = {k: v for k, v in headers.items() if k.lower() != name.lower()}
        if value is not None:
            headers[name] = value
    async with _client(app) as client:
        return await client.request(
            request.method, request.url.path, content=request.content, headers=headers
        )


def _code(response: Any) -> str:
    return str(rpc_wire._unpack(response.content)["error"]["code"])


@pytest.mark.asyncio
async def test_service_mac_end_to_end_without_any_ed25519(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_ed25519(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("service_mac must not sign or verify Ed25519")

    rec = _Recorder()
    app = _provider_app(_provider_cfg())
    # Once described, READS are MAC-only (a not-yet-described method may be a
    # command and is signed too: see test_kci_*).
    await _proxy(app)._remote_contract("")
    monkeypatch.setattr(rpc_identity, "sign", no_ed25519)
    monkeypatch.setattr(rpc_identity, "verify", no_ed25519)
    from sandloom.core.tenant import bind_tenant

    tenant = "7d5b7c38-0a2a-4c0e-9d34-2b9f1e0d4a11"
    with bind_tenant(UUID(tenant)):
        assert await _proxy(app, rec).whose_tenant() == tenant
    assert await _proxy(app, rec).echo("v") == "r-v"
    for request in rec.requests:
        assert rpc_pair_mac.AUTH_HEADER in request.headers
        assert rpc_identity.SVC_SIG_HEADER not in request.headers
        assert "X-Internal-Token" not in request.headers
    for response in rec.responses:  # strict: pair-keyed by the pinned provider
        assert response.headers[rpc_binding.BINDING_HEADER] == (
            "2;alg=x25519;kid=t1;svc=tenancy"
        )


@pytest.mark.asyncio
async def test_captured_request_replayed_to_the_same_provider_is_refused(
    caller: Any,
) -> None:
    rec = _Recorder()
    app = _provider_app(_provider_cfg())
    assert await _proxy(app, rec).echo("v") == "r-v"
    call = next(r for r in rec.requests if r.method == "POST")
    before = _rejected("replayed")
    replay = await _send(app, call)
    assert replay.status_code == 401 and _code(replay) == "rpc_auth_replayed"
    assert _rejected("replayed") == before + 1
    # a replay under a fresh nonce no longer verifies (the nonce is MACed)
    renonced = await _send(app, call, X_SANDLOOM_RPC_Nonce="f" * 32)
    assert renonced.status_code == 401 and _code(renonced) == "rpc_auth_invalid"


@pytest.mark.asyncio
async def test_captured_request_replayed_to_another_service_is_refused(
    caller: Any,
) -> None:
    """The review finding: same contract, another provider, inside the window."""
    rec = _Recorder()
    assert await _proxy(_provider_app(_provider_cfg()), rec).echo("v") == "r-v"
    call = next(r for r in rec.requests if r.method == "POST")
    other = _provider_app(
        _provider_cfg(
            rpc_service_id="audit",
            rpc_dh_private_key=DH["audit"][0],
            rpc_dh_key_id="a1",
        )
    )
    response = await _send(other, call)
    assert response.status_code == 401 and _code(response) == "rpc_auth_invalid"


@pytest.mark.asyncio
async def test_downgrades_are_refused(caller: Any) -> None:
    rec = _Recorder()
    mac_app = _provider_app(_provider_cfg())
    assert await _proxy(mac_app, rec).echo("v") == "r-v"
    call = next(r for r in rec.requests if r.method == "POST")
    # an Ed25519-signed request without the MAC
    identity = rpc_identity.CallerIdentity(
        "billing", "e1", load_private_key(ED["billing"][0], "EdDSA")
    )
    context = rpc_context._encode_context(
        rpc_context._context_payload(identity, "tenancy")
    )
    signature = rpc_identity.sign(
        identity, "POST", f"{SERVICE}/echo", call.content, context
    )
    ed_only = await _send(
        mac_app,
        call,
        X_SANDLOOM_RPC_Auth=None,
        X_SANDLOOM_RPC_Context=context,
        X_SANDLOOM_RPC_Service_Signature=signature,
        X_SANDLOOM_RPC_Nonce="1" * 32,
    )
    assert ed_only.status_code == 401 and _code(ed_only) == "rpc_auth_required"
    # The recorded call may carry a service signature too: the client signs
    # a method it has not described yet (it may write), and whether it has
    # described it depends on what ran before in the process. Strip every
    # credential so "alone" / "nothing" mean exactly that.
    no_credential = {
        "X_SANDLOOM_RPC_Auth": None,
        "X_SANDLOOM_RPC_Context": None,
        "X_SANDLOOM_RPC_Service_Signature": None,
        "X_SANDLOOM_RPC_Nonce": None,
    }
    # the shared token alone opens nothing (hidden)
    token_only = await _send(
        mac_app, call, **no_credential, X_Internal_Token="shared-secret"
    )
    assert token_only.status_code == 404
    # nothing at all: hidden
    bare = await _send(mac_app, call, **no_credential)
    assert bare.status_code == 404
    # a bad MAC never falls back to a valid signature riding along
    bad_mac = _edit(call.headers[rpc_pair_mac.AUTH_HEADER], "mac", "A" * 43)
    both = await _send(
        mac_app,
        call,
        X_SANDLOOM_RPC_Auth=bad_mac,
        X_SANDLOOM_RPC_Service_Signature=signature,
    )
    assert both.status_code == 401 and _code(both) == "rpc_auth_invalid"


@pytest.mark.asyncio
async def test_auth_failures_are_503_at_the_caller_with_a_stable_code(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # provider does not know billing's key
    app = _provider_app(
        _provider_cfg(rpc_service_dh_keys={"tenancy": SERVICE_DH_KEYS["tenancy"]})
    )
    with pytest.raises(RemoteServiceUnavailable) as exc:
        await _proxy(app).echo("v")
    assert exc.value.status_code == 503 and exc.value.code == "rpc_auth_invalid"
    # forbidden method: 403 at the provider, 503 at the caller
    with pytest.raises(RemoteServiceUnavailable) as forbidden:
        await _proxy(_provider_app(_provider_cfg())).admin_only()
    assert forbidden.value.code == "rpc_caller_forbidden"
    # the provider's clock is 10 minutes ahead of the caller's
    real = time.time
    monkeypatch.setattr(
        rpc_pair_mac,
        "time",
        type("Clock", (), {"time": staticmethod(lambda: real() + 600)}),
    )
    with pytest.raises(RemoteServiceUnavailable) as stale:
        await _proxy(_provider_app(_provider_cfg())).echo("v")
    assert stale.value.code == "rpc_auth_expired"


@pytest.mark.asyncio
async def test_caller_never_falls_back_without_a_pair_key(caller: Any) -> None:
    rec = _Recorder()
    app = _provider_app(_provider_cfg())
    with pytest.raises(RemoteServiceUnavailable) as exc:
        await _proxy(app, rec, service_id=None).echo("v")
    assert exc.value.code == "rpc_auth_unavailable"
    with pytest.raises(RemoteServiceUnavailable):
        await _proxy(app, rec, service_id="nobody").echo("v")
    assert rec.requests == []  # nothing unauthenticated ever left


@pytest.mark.asyncio
async def test_transport_retry_is_not_a_replay(caller: Any) -> None:
    app = _provider_app(_provider_cfg())
    served: list[str] = []

    class FlakyTransport(httpx.AsyncBaseTransport):
        """Delivers attempt 1 to the provider, then loses its response."""

        def __init__(self) -> None:
            self.inner = ASGITransport(app=app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await self.inner.handle_async_request(request)
            if request.method == "POST":
                served.append(request.headers[rpc_wire._NONCE_HEADER])
                if len(served) == 1:
                    raise httpx.ReadTimeout("lost", request=request)
            return response

    proxy = RemoteServiceProxy(
        service_name=SERVICE,
        binding={"base_url": "http://tenancy", "service_id": "tenancy"},
        default_timeout_seconds=5.0,
        client=AsyncClient(transport=FlakyTransport(), base_url="http://tenancy"),
    )
    assert await proxy.echo("v") == "r-v"
    assert len(served) == 2 and served[0] != served[1]


@pytest.mark.asyncio
async def test_shared_nonce_layer_catches_a_replay_on_another_process(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.infra import access
    from sandloom.core.testing import InMemoryCache

    shared = InMemoryCache()
    monkeypatch.setattr(access, "security_cache", lambda: shared)
    rec = _Recorder()
    app = _provider_app(_provider_cfg(rpc_replay_guard="all"))
    assert await _proxy(app, rec).echo("v") == "r-v"
    call = next(r for r in rec.requests if r.method == "POST")
    rpc_pair_mac._guard.clear()  # "another worker": empty local table
    replay = await _send(app, call)
    assert replay.status_code == 401 and _code(replay) == "rpc_auth_replayed"


@pytest.mark.asyncio
async def test_shared_nonce_layer_outage_fails_closed_in_prod_only(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.infra import access

    class Down:
        async def add(self, *_a: Any, **_k: Any) -> bool:
            raise ConnectionError("redis down")

    monkeypatch.setattr(access, "security_cache", lambda: Down())
    dev = _provider_app(_provider_cfg(rpc_replay_guard="all"))
    before = _rejected("fail_open")
    assert await _proxy(dev).echo("v") == "r-v"
    assert _rejected("fail_open") > before
    prod_cfg = _provider_cfg(rpc_replay_guard="all")
    monkeypatch.setattr(type(prod_cfg), "is_prod", property(lambda self: True))
    with pytest.raises(RemoteServiceUnavailable) as exc:
        await _proxy(_provider_app(prod_cfg)).echo("v")
    assert exc.value.code == "rpc_replay_unavailable"


@pytest.mark.asyncio
async def test_commands_mode_uses_the_shared_layer_for_commands_only(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.infra import access

    calls: list[str] = []

    class Spy:
        async def add(self, key: str, *_a: Any, **_k: Any) -> bool:
            calls.append(key)
            return True

    monkeypatch.setattr(access, "security_cache", lambda: Spy())
    app = _provider_app(_provider_cfg(rpc_replay_guard="commands"))
    assert await _proxy(app).echo("v") == "r-v"  # a read
    assert calls == []
    core = app.state.core
    assert rpc_dispatch._is_command(core, SERVICE, "echo") is False
    assert rpc_dispatch._is_command(core, SERVICE, "missing") is True
    assert rpc_dispatch._is_command(core, "no.such.v1", "echo") is True


@pytest.mark.asyncio
async def test_rejections_log_ids_only_never_key_material(
    caller: Any, caplog: pytest.LogCaptureFixture
) -> None:
    rec = _Recorder()
    app = _provider_app(_provider_cfg())
    assert await _proxy(app, rec).echo("v") == "r-v"
    call = next(r for r in rec.requests if r.method == "POST")
    with caplog.at_level(logging.DEBUG, logger="sandloom.core.rpc"):
        await _send(app, call)  # replay
        await _send(
            app,
            call,
            X_SANDLOOM_RPC_Auth=_edit(
                call.headers[rpc_pair_mac.AUTH_HEADER], "mac", "A" * 43
            ),
        )
    text = caplog.text
    tag = call.headers[rpc_pair_mac.AUTH_HEADER].rsplit("=", 1)[1]
    for secret in (tag, call.headers[rpc_wire._CTX_HEADER], "PRIVATE KEY"):
        assert secret not in text
    assert "replayed" in text and "mac_mismatch" in text


# ---------------------------------------------------------------------------
# rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_rotation_has_no_failed_call_at_any_step(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1, t2 = DH["tenancy"], DH["tenancy-next"]

    async def call(provider_cfg: Any, published: dict[str, str]) -> str:
        keys = {**SERVICE_DH_KEYS, "tenancy": published}
        monkeypatch.setattr(settings, "rpc_service_dh_keys", keys)
        cfg = provider_cfg.model_copy(update={"rpc_service_dh_keys": keys})
        rpc_pair_mac.clear_key_cache()
        rec = _Recorder()
        assert await _proxy(_provider_app(cfg), rec).echo("v") == "r-v"
        return rpc_pair_mac.parse_header(
            rec.requests[-1].headers[rpc_pair_mac.AUTH_HEADER]
        )["pk"]

    step0 = _provider_cfg()
    assert await call(step0, {"t1": t1[1]}) == "t1"
    # 1) P holds t2 as an extra key (not yet published)
    step1 = _provider_cfg(rpc_dh_extra_private_keys={"t2": t2[0]})
    assert await call(step1, {"t1": t1[1]}) == "t1"
    # 2) t2 appended LAST everywhere: callers address t2, P accepts it
    assert await call(step1, {"t1": t1[1], "t2": t2[1]}) == "t2"
    # 3) P switches its current key, keeps t1 as an extra
    step3 = _provider_cfg(
        rpc_dh_private_key=t2[0],
        rpc_dh_key_id="t2",
        rpc_dh_extra_private_keys={"t1": t1[0]},
    )
    assert await call(step3, {"t1": t1[1], "t2": t2[1]}) == "t2"
    # 4) t1 unpublished; 5) dropped from P
    assert await call(step3, {"t2": t2[1]}) == "t2"
    step5 = _provider_cfg(rpc_dh_private_key=t2[0], rpc_dh_key_id="t2")
    assert await call(step5, {"t2": t2[1]}) == "t2"


@pytest.mark.asyncio
async def test_a_request_to_a_dropped_provider_kid_is_refused(caller: Any) -> None:
    rec = _Recorder()
    assert await _proxy(_provider_app(_provider_cfg()), rec).echo("v") == "r-v"
    call = next(r for r in rec.requests if r.method == "POST")
    rotated = _provider_app(
        _provider_cfg(rpc_dh_private_key=DH["tenancy-next"][0], rpc_dh_key_id="t2")
    )
    response = await _send(rotated, call)
    assert response.status_code == 401 and _code(response) == "rpc_auth_invalid"


# ---------------------------------------------------------------------------
# startup validation
# ---------------------------------------------------------------------------

_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://h:6379/0",
    "BROKER_URL": "redis://h:6379/1",
    "JWT_SECRET": "x" * 40,
    "JWT_PRIVATE_KEY": "",
}
PINNED = {"tenancy.membership.v1": {"base_url": "http://t", "service_id": "tenancy"}}


def _settings(**extra: Any) -> Settings:
    env = {
        **_BASE_ENV,
        "RPC_SERVICE_ID": "billing",
        "RPC_DH_PRIVATE_KEY": DH["billing"][0],
        "RPC_DH_KEY_ID": "b1",
        "RPC_SERVICE_DH_KEYS": SERVICE_DH_KEYS,
        "RPC_ALLOWED_CALLS": {"billing": ["tenancy.membership.v1"]},
        "SERVICE_BINDINGS": PINNED,
        **extra,
    }
    cfg = Settings(_env_file=None, **env)
    enforce_configuration(cfg)  # the checks a process runs at boot
    return cfg


def test_a_consistent_configuration_needs_no_signing_key_outside_prod() -> None:
    cfg = _settings()
    assert cfg.rpc_private_key is None
    assert rpc_identity.signing_identity(cfg) is None  # commands unsigned (dev)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"RPC_DH_PRIVATE_KEY": "", "RPC_DH_KEY_ID": ""}, "RPC_DH_PRIVATE_KEY"),
        (
            {"SERVICE_BINDINGS": {"tenancy.membership.v1": {"base_url": "http://t"}}},
            "service_id",
        ),
        (
            {
                "SERVICE_BINDINGS": {
                    "tenancy.membership.v1": {"base_url": "http://t", "service_id": "x"}
                },
            },
            "no key for 'x'",
        ),
        (
            {
                "RPC_SERVICE_DH_KEYS": {
                    **SERVICE_DH_KEYS,
                    "tenancy": {"t1": _low_order_pem(LOW_ORDER[2])},
                },
            },
            "low-order",
        ),
        (
            {
                "RPC_SERVICE_DH_KEYS": {
                    **SERVICE_DH_KEYS,
                    "copy": {"c1": DH["tenancy"][1]},
                }
            },
            "same X25519 key",
        ),
        ({"RPC_COMMANDS_REQUIRE_SIGNATURE": "on"}, "RPC_PRIVATE_KEY"),
        ({"RPC_DH_EXTRA_PRIVATE_KEYS": {"b1": DH["billing-next"][0]}}, "repeats"),
        (
            {
                "RPC_DH_EXTRA_PRIVATE_KEYS": {"b2": DH["rogue"][0]},
                "RPC_SERVICE_DH_KEYS": {
                    **SERVICE_DH_KEYS,
                    "billing": {"b1": DH["billing"][1], "b2": DH["billing-next"][1]},
                },
            },
            "not the public half",
        ),
        ({"RPC_DH_EXTRA_PRIVATE_KEYS": {"b2": ED["billing"][0]}}, "not X25519"),
        ({"RPC_ALLOWED_CALLS": {"billing": []}}, "does not allow"),
    ],
    ids=[
        "no-own-key",
        "unpinned-binding",
        "provider-without-keys",
        "low-order-provider-key",
        "duplicate-public-key",
        "signed-commands-need-signing-key",
        "extra-repeats-current",
        "extra-mismatch",
        "extra-not-x25519",
        "allowlist-must-cover-bindings",
    ],
)
def test_startup_refuses_unusable_configurations(
    extra: dict[str, Any], message: str
) -> None:
    with pytest.raises(_REFUSED, match=message):
        _settings(**{"INTERNAL_API_TOKEN": "t" * 40, **extra})


def test_startup_refuses_a_dh_key_that_is_the_signing_seed() -> None:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    seed = bytes(range(32))
    ed = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    dh = x25519.X25519PrivateKey.from_private_bytes(seed)
    with pytest.raises(_REFUSED, match="reuses the RPC_PRIVATE_KEY seed"):
        _settings(
            RPC_PRIVATE_KEY=private_pem(ed),
            RPC_KEY_ID="e1",
            RPC_SERVICE_KEYS={"billing": {"e1": public_pem(ed)}},
            RPC_DH_PRIVATE_KEY=x25519_private_pem(dh),
            RPC_SERVICE_DH_KEYS={
                **SERVICE_DH_KEYS,
                "billing": {"b1": x25519_public_pem(dh)},
            },
        )


def test_keys_generate_rpc_dh(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert keys_cli.main(["generate", "--purpose", "rpc-dh", "--service", "svc"]) == 0
    out = capsys.readouterr().out
    assert "RPC_DH_PRIVATE_KEY" in out and "RPC_PRIVATE_KEY —" not in out
    entry = json.loads(out.strip().splitlines()[-1])
    assert list(entry) == ["svc"]
    assert (
        keys_cli.main(
            [
                "generate",
                "--purpose",
                "rpc-dh",
                "--service",
                "svc",
                "--out-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert (tmp_path / "rpc_dh_private_key").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "rpc_private_key").exists()


@pytest.fixture(autouse=True)
async def _close_shared_client() -> AsyncIterator[None]:
    yield
    await rpc.close_rpc_client()


# ---------------------------------------------------------------------------
# both transports, a real socket (uvicorn), plus a golden wire request
# ---------------------------------------------------------------------------


@pytest.fixture
async def served_provider() -> AsyncIterator[str]:
    import asyncio
    import socket

    import uvicorn

    app = _provider_app(_provider_cfg())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="off", access_log=False)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["aiohttp", "httpx"])
async def test_both_transports_authenticate_and_refuse_raw_replays(
    caller: Any, served_provider: str, kind: str
) -> None:
    from sandloom.core.rpc_transport import CLIENTS

    client = rpc_client._new_rpc_client("httpx") if kind == "httpx" else CLIENTS[kind]()
    proxy = RemoteServiceProxy(
        service_name=SERVICE,
        binding={"base_url": served_provider, "service_id": "tenancy"},
        default_timeout_seconds=5.0,
        client=client,
    )
    sent: list[dict[str, str]] = []
    real_post = client.post

    async def spy(url: str, **kw: Any) -> Any:
        sent.append(dict(kw["headers"]) | {"__body__": kw["content"].hex()})
        return await real_post(url, **kw)

    client.post = spy
    try:
        assert await proxy.echo("v") == "r-v"
        assert await proxy.echo("w") == "r-w"
    finally:
        await client.aclose()
    first = sent[0]
    assert first[rpc_pair_mac.AUTH_HEADER] != sent[1][rpc_pair_mac.AUTH_HEADER]
    # the exact bytes again, over a fresh plain socket: refused as a replay
    async with httpx.AsyncClient() as raw:
        body = bytes.fromhex(first.pop("__body__"))
        replay = await raw.post(
            f"{served_provider}/_sandloom/rpc/{SERVICE}/echo",
            content=body,
            headers=first,
        )
    assert replay.status_code == 401 and _code(replay) == "rpc_auth_replayed"


#: The pair-MAC wire request for fixed keys, clock, nonce and tenant: any
#: change to the encoding, key schedule or header format breaks this test.
#: (Same keys as the first committed vector; tag cross-checked with an
#: independent RFC 5869 / length-prefix implementation.)
GOLDEN_AUTH = (
    "v=1;caller=billing;ck=b1;aud=tenancy;pk=t1;"
    "mac=vVvCcA6YstJOXOFDJ2uGb-63Urrujlv6muDc5skW-L4"
)


@pytest.mark.asyncio
async def test_golden_pair_mac_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.tenant import bind_tenant

    c = x25519.X25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    p = x25519.X25519PrivateKey.from_private_bytes(bytes(range(101, 133)))
    for name, value in {
        "rpc_service_id": "billing",
        "rpc_private_key": None,
        "rpc_key_id": None,
        "rpc_dh_private_key": x25519_private_pem(c),
        "rpc_dh_key_id": "b1",
        "rpc_service_dh_keys": {"tenancy": {"t1": x25519_public_pem(p)}},
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(
        rpc_context, "time", type("T", (), {"time": staticmethod(lambda: 1760000000.0)})
    )
    monkeypatch.setattr(
        rpc_proxy, "_new_nonce", lambda: "0123456789abcdef0123456789abcdef"
    )
    seen: list[httpx.Request] = []
    provider = settings.model_copy(
        update={
            "rpc_service_id": "tenancy",
            "rpc_dh_private_key": x25519_private_pem(p),
            "rpc_dh_key_id": "t1",
            "rpc_service_dh_keys": {"billing": {"b1": x25519_public_pem(c)}},
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        from sandloom.core.contract_testing import rpc_response_headers

        seen.append(request)
        body = rpc_wire._pack({"ok": True, "result": "x"})
        headers = rpc_response_headers(provider, request.headers, 200, body)
        return httpx.Response(200, content=body, headers=headers)

    proxy = RemoteServiceProxy(
        service_name="tenancy.membership.v1",
        binding={"base_url": "http://t", "service_id": "tenancy"},
        default_timeout_seconds=2,
        client=AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t"),
    )
    with bind_tenant(UUID(int=7)):
        assert await proxy.get_role(UUID(int=1)) == "x"
    request = seen[0]
    assert request.headers["x-sandloom-rpc-context"] == GOLDEN_CONTEXT
    assert request.headers["x-sandloom-rpc-auth"] == GOLDEN_AUTH
    assert "x-sandloom-rpc-service-signature" not in request.headers
    assert "x-internal-token" not in request.headers


GOLDEN_CONTEXT = (  # {"tenant_id": <UUID 7>, "iat": 1760000000, "aud": "tenancy"}
    "eyJ0ZW5hbnRfaWQiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDciLCJpYXQiOjE3"
    "NjAwMDAwMDAsImF1ZCI6InRlbmFuY3kifQ"
)


@pytest.mark.asyncio
async def test_contract_conformance_holds_under_service_mac(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local == remote with every call pair-MAC authenticated (a self-pair:
    the conformance harness serves the core in-process)."""
    from sandloom.core.contract_testing import ContractCase, assert_contract_conformance

    monkeypatch.setattr(settings, "rpc_service_id", "tenancy")
    monkeypatch.setattr(settings, "rpc_dh_private_key", DH["tenancy"][0])
    monkeypatch.setattr(settings, "rpc_dh_key_id", "t1")
    monkeypatch.setattr(settings, "rpc_replay_guard", "local")
    monkeypatch.setattr(settings, "rpc_allowed_calls", {"tenancy": [SERVICE]})
    app = _provider_app(settings)
    core = app.state.core
    await assert_contract_conformance(
        core,
        SERVICE,
        [
            ContractCase("echo", ("v",)),
            ContractCase("echo", (), {"value": "kw"}),
            ContractCase("echo", ()),  # missing argument: same 422 both ways
            ContractCase("whose_tenant", ()),
        ],
    )


# ---------------------------------------------------------------------------
# regressions of the adversarial security review (docs/adr/0004 "Security review")
# ---------------------------------------------------------------------------


class _Drop(httpx.AsyncBaseTransport):
    """An on-path party: captures each request and never delivers it."""

    def __init__(self) -> None:
        self.captured: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.captured.append(request)
        raise httpx.ConnectError("dropped", request=request)


async def _capture(value: str) -> httpx.Request:
    drop = _Drop()
    proxy = RemoteServiceProxy(
        service_name=SERVICE,
        binding={"base_url": "http://tenancy", "service_id": "tenancy"},
        default_timeout_seconds=5.0,
        client=AsyncClient(transport=drop, base_url="http://tenancy"),
    )
    with pytest.raises(RemoteServiceUnavailable):
        await proxy.echo(value)
    return next(r for r in drop.captured if r.method == "POST")


@pytest.mark.asyncio
async def test_r1_a_response_cannot_be_rebound_to_another_request(caller: Any) -> None:
    """Finding 1: an old captured request R1 sent with a SECOND nonce header
    (the victim's R2 nonce) is refused, never bound, and a pair-keyed
    response covers the request MAC, so it cannot answer R2 either."""
    app = _provider_app(_provider_cfg())
    r1 = await _capture("ATTACKER-CHOSEN-OLD-CALL")
    r2 = await _capture("victim-new-call")
    n2 = r2.headers[rpc_wire._NONCE_HEADER]
    raw = [(k, v) for k, v in r1.headers.raw if k.lower() not in (b"content-length",)]
    raw.append((b"x-sandloom-rpc-nonce", n2.encode()))
    async with _client(app) as client:
        response = await client.request(
            "POST", r1.url.path, content=r1.content, headers=raw
        )
    assert response.status_code == 401 and _code(response) == "rpc_auth_invalid"
    assert rpc_wire._NONCE_HEADER not in response.headers  # not bound at all
    # and R1 delivered normally: its keyed response does not verify for R2
    async with _client(app) as client:
        served = await client.request(
            "POST",
            r1.url.path,
            content=r1.content,
            headers=[
                (k, v) for k, v in r1.headers.raw if k.lower() != b"content-length"
            ],
        )
    assert served.status_code == 200
    forged_headers = httpx.Headers(served.headers)
    forged_headers[rpc_wire._NONCE_HEADER] = n2  # the on-path party re-labels it
    verdict = rpc_binding.verify(
        settings,
        forged_headers,
        nonce=n2,
        sent=dict(r2.headers),
        expect_service="tenancy",
        status_code=200,
        body=served.content,
    )
    assert verdict == "mac_mismatch"


def test_r1_request_auth_covers_the_request_mac() -> None:
    base = rpc_binding.request_auth({})
    assert rpc_binding.request_auth({"X-SANDLOOM-RPC-Auth": "v=1;x"}) != base
    assert rpc_binding.request_auth({"X-SANDLOOM-RPC-Auth": "v=1;y"}) != (
        rpc_binding.request_auth({"X-SANDLOOM-RPC-Auth": "v=1;x"})
    )


@pytest.mark.asyncio
async def test_r2_a_signed_command_cannot_be_stripped_of_its_mac(
    caller: Any, commands_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Ed25519 signature never authenticates a request on its own: a
    command stripped of its MAC (to dodge the nonce guard) is refused."""
    monkeypatch.setattr(settings, "rpc_commands_require_signature", "on")
    app = _provider_app(
        _provider_cfg(
            rpc_service_keys=SERVICE_KEYS, rpc_commands_require_signature="on"
        )
    )
    rec = _Recorder()
    assert await _proxy(app, rec).charge("r2", 1) == 1
    call = next(r for r in rec.requests if r.method == "POST")
    assert call.headers.get(rpc_identity.SVC_SIG_HEADER)
    stripped = await _send(app, call, X_SANDLOOM_RPC_Auth=None)
    assert stripped.status_code == 401 and _code(stripped) == "rpc_auth_required"


@pytest.mark.asyncio
async def test_r3_an_unauthorized_peer_cannot_fill_the_replay_guard(
    caller: Any,
) -> None:
    cfg = _provider_cfg(rpc_replay_cache_max_entries=1000)
    app = _provider_app(cfg)
    audit = _cfg("audit", "a1")  # holds a published key, no allowlist entry
    ctx = rpc_context._encode_context(rpc_context._context_payload(None, "tenancy"))
    assert ctx is not None
    async with _client(app) as client:
        for i in range(1001):
            nonce = f"{i:032x}"
            header = rpc_pair_mac.caller_key(audit, "tenancy").header(
                http_method="POST",
                target=f"{SERVICE}/echo",
                nonce=nonce,
                context=ctx,
                body=b"",
            )
            response = await client.post(
                f"/_sandloom/rpc/{SERVICE}/echo",
                content=b"",
                headers={
                    "X-SANDLOOM-RPC-Auth": header,
                    "X-SANDLOOM-RPC-Nonce": nonce,
                    "X-SANDLOOM-RPC-Context": ctx,
                },
            )
            assert response.status_code == 403
    assert len(rpc_pair_mac.replay_guard(cfg)) == 0
    assert await _proxy(app).echo("v") == "r-v"


def test_r4_shared_layer_ttl_is_clock_independent() -> None:
    assert rpc_pair_mac.shared_replay_ttl(60) == 180  # 2 * skew + drift <= skew


@pytest.mark.parametrize("iat", [10**400, -1, 2**63])
def test_r5_out_of_range_issue_times_are_expired_not_500(iat: int) -> None:
    with pytest.raises(rpc_pair_mac.AuthError) as exc:
        rpc_pair_mac.check_fresh(iat, skew=60)
    assert exc.value.code == "rpc_auth_expired"


def test_r6_rejection_logs_are_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    rpc_inbound_auth._auth_logged.clear()
    with caplog.at_level(logging.WARNING, logger="sandloom.core.rpc"):
        for _ in range(50):
            with pytest.raises(rpc_errors._RpcContextError):
                rpc_inbound_auth._auth_reject(rpc_pair_mac._invalid("mac_mismatch"))
    assert caplog.text.count("Refused an internal RPC request") == 1


def test_r7_shared_replay_keys_never_collide() -> None:
    nonce = "0" * 32
    assert rpc_pair_mac.shared_replay_key("a:b", "c", nonce) != (
        rpc_pair_mac.shared_replay_key("a", "b:c", nonce)
    )


def test_r8_resizing_never_forgets_live_nonces() -> None:
    small = settings.model_copy(update={"rpc_replay_cache_max_entries": 1000})
    big = settings.model_copy(update={"rpc_replay_cache_max_entries": 5000})
    assert rpc_pair_mac.replay_guard(small).add("billing", NONCE, 60) == "ok"
    assert rpc_pair_mac.replay_guard(big).add("billing", NONCE, 60) == "replay"


# ---------------------------------------------------------------------------
# second review: key-compromise impersonation (KCI) and the command signature
# ---------------------------------------------------------------------------


@pytest.fixture
def commands_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commands on the fake database dedupe through the (in-memory) cache."""
    from sandloom.core.idempotency import cache_store as idempotency_cache_store
    from sandloom.core.testing import InMemoryCache

    mem = InMemoryCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": mem)
    CHARGED.clear()


def _forged_by_provider_key(
    *, caller: str, ck: str, method: str, body: bytes, nonce: str, context: str
) -> str:
    """What a holder of tenancy's LEAKED X25519 key computes for ANY caller:
    X25519 is symmetric, so p * C_pub == c * P_pub (key-compromise
    impersonation)."""
    template = rpc_pair_mac._template(
        DH["tenancy"][0], SERVICE_DH_KEYS[caller][ck], caller, ck, "tenancy", "t1"
    )
    tag = rpc_pair_mac._tag(
        template,
        rpc_pair_mac._call_suffix("POST", method, nonce, context.encode(), body),
    )
    return f"v=1;caller={caller};ck={ck};aud=tenancy;pk=t1;mac={tag}"


async def _forge(app: FastAPI, method: str, args: list[Any]) -> Any:
    body = rpc_wire._pack({"args": args, "kwargs": {}})
    ctx = rpc_context._encode_context(rpc_context._context_payload(None, "tenancy"))
    assert ctx is not None
    nonce = rpc_binding.new_nonce()
    header = _forged_by_provider_key(
        caller="billing",
        ck="b1",
        method=f"{SERVICE}/{method}",
        body=body,
        nonce=nonce,
        context=ctx,
    )
    async with _client(app) as client:
        return await client.post(
            f"/_sandloom/rpc/{SERVICE}/{method}",
            content=body,
            headers={
                "X-SANDLOOM-RPC-Auth": header,
                "X-SANDLOOM-RPC-Nonce": nonce,
                "X-SANDLOOM-RPC-Context": ctx,
                "Content-Type": "application/msgpack",
            },
        )


@pytest.mark.asyncio
async def test_kci_a_leaked_provider_key_forges_reads_but_never_commands(
    caller: Any, commands_cache: None
) -> None:
    """Pinned property (ADR-0004, KCI): whoever holds a PROVIDER's X25519
    private key derives every caller's pair key towards it — the MAC alone
    cannot tell them apart. Reads: forgeable (documented). Commands: refused,
    they also need the caller's Ed25519 signature."""
    app = _provider_app(
        _provider_cfg(
            rpc_commands_require_signature="on", rpc_service_keys=SERVICE_KEYS
        )
    )
    read = await _forge(app, "echo", ["forged"])
    assert read.status_code == 200  # the documented residual risk
    command = await _forge(app, "charge", ["k-1", 100])
    assert command.status_code == 401
    assert _code(command) == "rpc_auth_signature_required"
    assert CHARGED == []  # no effect


@pytest.mark.asyncio
async def test_kci_a_genuine_caller_signs_its_commands_and_they_run(
    caller: Any, commands_cache: None
) -> None:
    app = _provider_app(
        _provider_cfg(
            rpc_commands_require_signature="on", rpc_service_keys=SERVICE_KEYS
        )
    )
    rec = _Recorder()
    proxy = _proxy(app, rec)
    assert await proxy.charge(key="k-2", amount=7) == 7
    assert CHARGED == [7]
    post = next(r for r in rec.requests if r.url.path.endswith("/charge"))
    assert rpc_identity.SVC_SIG_HEADER in post.headers  # signed
    assert rpc_pair_mac.AUTH_HEADER in post.headers  # and MACed
    # a described READ is MAC-only (no per-call Ed25519 on the hot path)
    rec.requests.clear()
    assert await proxy.echo("v") == "r-v"
    read = next(r for r in rec.requests if r.url.path.endswith("/echo"))
    assert rpc_identity.SVC_SIG_HEADER not in read.headers


@pytest.mark.asyncio
async def test_kci_a_command_signed_by_another_service_is_refused(
    caller: Any, commands_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid Ed25519 signature of ANOTHER service (here tenancy's own, as a
    provider insider might hold) does not authorize billing's command."""
    app = _provider_app(
        _provider_cfg(
            rpc_commands_require_signature="on", rpc_service_keys=SERVICE_KEYS
        )
    )
    body = rpc_wire._pack({"args": ["k-3", 5], "kwargs": {}})
    identity = rpc_identity.CallerIdentity(
        "tenancy", "e1", load_private_key(ED["tenancy"][0], "EdDSA")
    )
    ctx = rpc_context._encode_context(rpc_context._context_payload(identity, "tenancy"))
    assert ctx is not None
    nonce = rpc_binding.new_nonce()
    header = _forged_by_provider_key(
        caller="billing",
        ck="b1",
        method=f"{SERVICE}/charge",
        body=body,
        nonce=nonce,
        context=ctx,
    )
    async with _client(app) as client:
        response = await client.post(
            f"/_sandloom/rpc/{SERVICE}/charge",
            content=body,
            headers={
                "X-SANDLOOM-RPC-Auth": header,
                "X-SANDLOOM-RPC-Nonce": nonce,
                "X-SANDLOOM-RPC-Context": ctx,
                rpc_identity.SVC_SIG_HEADER: rpc_identity.sign(
                    identity, "POST", f"{SERVICE}/charge", body, ctx
                ),
            },
        )
    # the context names tenancy as svc while the MAC names billing
    assert response.status_code == 401 and _code(response) == "rpc_auth_invalid"
    assert CHARGED == []


def test_commands_require_signature_defaults_and_startup_rule() -> None:
    dev = settings.model_copy(update={"rpc_commands_require_signature": "auto"})
    assert rpc_pair_mac.commands_require_signature(dev) is bool(dev.is_prod)
    with pytest.raises(_REFUSED, match="RPC_COMMANDS_REQUIRE_SIGNATURE"):
        _settings(INTERNAL_API_TOKEN="t" * 40, RPC_COMMANDS_REQUIRE_SIGNATURE="on")
    cfg = _settings(
        RPC_COMMANDS_REQUIRE_SIGNATURE="on",
        RPC_PRIVATE_KEY=ED["billing"][0],
        RPC_KEY_ID="e1",
        RPC_SERVICE_KEYS=SERVICE_KEYS,
    )
    assert rpc_identity.signing_identity(cfg) is not None  # signs commands only


def test_strict_profile_refuses_unsigned_commands_and_stale_extra_keys() -> None:
    from sandloom.core.production_profile import config_problems

    base = settings.model_copy(
        update={
            "rpc_service_id": "tenancy",
            "rpc_dh_private_key": DH["tenancy"][0],
            "rpc_dh_key_id": "t1",
            "rpc_service_dh_keys": SERVICE_DH_KEYS,
        }
    )
    errors, _ = config_problems(
        base.model_copy(
            update={
                "rpc_commands_require_signature": "off",
                "rpc_dh_extra_private_keys": {"t0": DH["tenancy-next"][0]},
            }
        )
    )
    joined = " ".join(errors)
    assert "RPC_COMMANDS_REQUIRE_SIGNATURE=off" in joined
    assert "['t0'] are no longer published" in joined


# ---------------------------------------------------------------------------
# second review: smaller items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_x_tenant_id_is_refused_on_the_mac_path(caller: Any) -> None:
    rec = _Recorder()
    app = _provider_app(_provider_cfg())
    assert await _proxy(app, rec).echo("v") == "r-v"
    call = next(r for r in rec.requests if r.method == "POST")
    response = await _send(
        app,
        call,
        X_Tenant_Id="7d5b7c38-0a2a-4c0e-9d34-2b9f1e0d4a11",
        X_SANDLOOM_RPC_Nonce=None,
    )
    assert response.status_code == 400 and _code(response) == "rpc_unsigned_tenant"


@pytest.mark.asyncio
async def test_an_unpublished_extra_kid_is_still_accepted_and_flagged(
    caller: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Rotation step 4 done (t1 unpublished) but step 5 not (t1 still in the
    extras): a request addressed to t1 still verifies — which is exactly why
    a stale extra key is a warning (an error under multi_tenant_strict)."""
    from sandloom.core.app_factory import warn_pair_mac_rpc

    rec = _Recorder()
    assert await _proxy(_provider_app(_provider_cfg()), rec).echo("v") == "r-v"
    call = next(r for r in rec.requests if r.method == "POST")
    late = _provider_cfg(
        rpc_dh_private_key=DH["tenancy-next"][0],
        rpc_dh_key_id="t2",
        rpc_dh_extra_private_keys={"t1": DH["tenancy"][0]},
        rpc_service_dh_keys={
            **SERVICE_DH_KEYS,
            "tenancy": {"t2": DH["tenancy-next"][1]},
        },
    )
    rpc_pair_mac._guard.clear()  # "the late provider": a fresh process
    response = await _send(_provider_app(late), call)
    assert response.status_code == 200
    assert rpc_pair_mac.stale_extra_kids(late) == ["t1"]
    with caplog.at_level(logging.WARNING):
        warnings = warn_pair_mac_rpc(late, logging.getLogger("t"))
    assert any("no longer published" in w for w in warnings)


@pytest.mark.asyncio
async def test_replica_clock_drift_within_the_skew_is_covered_by_the_shared_ttl(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replica A (on time) records the nonce; replica B runs ``skew`` behind,
    so it accepts the request up to 2 * skew + skew after A first saw it. The
    shared key must still be there: TTL = 3 * skew."""
    skew = 60
    accepted_by_a_at = 1_000_000.0
    iat = int(accepted_by_a_at) - skew  # oldest iat A accepts
    ttl = rpc_pair_mac.shared_replay_ttl(skew)
    # B (clock behind by `skew`) accepts until its clock reads iat + skew,
    # i.e. real time iat + 2 * skew
    b_last_real = iat + 2 * skew
    assert accepted_by_a_at + ttl >= b_last_real
    # and in the worst case where A saw it first at iat - skew (A ahead):
    assert (iat - skew) + ttl >= b_last_real


@pytest.mark.asyncio
async def test_head_on_the_status_route_is_not_served(caller: Any) -> None:
    app = _provider_app(_provider_cfg())
    headers, signer = rpc_outbound._get_auth(
        SERVICE, settings, rpc_pair_mac.caller_key(settings, "tenancy"), "tenancy"
    )
    nonce = headers[rpc_wire._NONCE_HEADER] = rpc_binding.new_nonce()
    signer.apply(headers, nonce)
    async with _client(app) as client:
        head = await client.head(f"/_sandloom/rpc/{SERVICE}", headers=headers)
        get = await client.get(f"/_sandloom/rpc/{SERVICE}", headers=headers)
    assert head.status_code == 405
    assert get.status_code == 200  # the nonce was not consumed by the HEAD


def test_one_caller_cannot_take_the_whole_nonce_table() -> None:
    guard = rpc_pair_mac.ReplayGuard(max_entries=100, per_caller=10)
    for i in range(10):
        assert guard.add("flood", f"{i:032x}", 60, now=100.0) == "ok"
    assert guard.add("flood", "f" * 32, 60, now=100.0) == "quota"
    assert guard.add("billing", "f" * 32, 60, now=100.0) == "ok"
    # the quota frees with the entries
    assert guard.add("flood", "e" * 32, 60, now=200.0) == "ok"


def test_local_replay_guard_is_refused_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(_REFUSED, match="RPC_REPLAY_GUARD=local in prod"):
        _settings(
            ENVIRONMENT="prod",
            JWT_SECRET="pR0d-grade-jwt-secret-9f3a7c1e5b2d8046",
            INTERNAL_API_TOKEN="Int3rnal-ops-token-4b8e2a6c0d1f9735xyz",
            RPC_REPLAY_GUARD="local",
            RPC_PRIVATE_KEY=ED["billing"][0],
            RPC_KEY_ID="e1",
            RPC_SERVICE_KEYS=SERVICE_KEYS,
        )


def test_startup_warns_that_the_shared_guard_fails_open_outside_prod() -> None:
    from sandloom.core.app_factory import warn_pair_mac_rpc

    cfg = _provider_cfg(rpc_replay_guard="commands")
    assert not cfg.is_prod
    warnings = warn_pair_mac_rpc(cfg, logging.getLogger("t"))
    assert any("FAILS OPEN" in w for w in warnings)


@pytest.mark.asyncio
async def test_all_mode_signs_and_requires_reads_too(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RPC_COMMANDS_REQUIRE_SIGNATURE=all: KCI-resistant reads (Ed25519 cost)."""
    monkeypatch.setattr(settings, "rpc_commands_require_signature", "all")
    app = _provider_app(
        _provider_cfg(
            rpc_commands_require_signature="all", rpc_service_keys=SERVICE_KEYS
        )
    )
    rec = _Recorder()
    assert await _proxy(app, rec).echo("v") == "r-v"
    assert all(rpc_identity.SVC_SIG_HEADER in r.headers for r in rec.requests)
    forged = await _forge(app, "echo", ["forged"])
    assert forged.status_code == 401
    assert _code(forged) == "rpc_auth_signature_required"


# ---------------------------------------------------------------------------
# third review: the command signature covers the nonce; quota; clock skew
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kci_a_captured_signed_command_cannot_be_re_maced_under_a_new_nonce(
    caller: Any, commands_cache: None
) -> None:
    """A holder of the provider's X25519 key captures a GENUINE signed
    command and re-MACs it under fresh nonces: the signed context names the
    original nonce (``n``), so every re-MAC is refused before any effect."""
    app = _provider_app(
        _provider_cfg(
            rpc_commands_require_signature="on", rpc_service_keys=SERVICE_KEYS
        )
    )
    rec = _Recorder()
    assert await _proxy(app, rec).charge(key="k-9", amount=3) == 3
    assert CHARGED == [3]
    call = next(r for r in rec.requests if r.url.path.endswith("/charge"))
    ctx = call.headers[rpc_wire._CTX_HEADER]
    signed = json.loads(base64.urlsafe_b64decode(ctx + "=="))
    assert signed["n"] == call.headers[rpc_wire._NONCE_HEADER]
    for _ in range(3):
        nonce = rpc_binding.new_nonce()
        header = _forged_by_provider_key(
            caller="billing",
            ck="b1",
            method=f"{SERVICE}/charge",
            body=call.content,
            nonce=nonce,
            context=ctx,
        )
        response = await _send(
            app, call, X_SANDLOOM_RPC_Auth=header, X_SANDLOOM_RPC_Nonce=nonce
        )
        assert response.status_code == 401
        assert _code(response) == "rpc_auth_invalid"
    assert CHARGED == [3]  # no second effect, independent of command dedupe


@pytest.mark.asyncio
async def test_a_signed_command_survives_the_transport_retry(
    caller: Any, commands_cache: None
) -> None:
    """Attempt 1 reaches the provider and its answer is lost; attempt 2 is
    re-signed with ITS nonce and accepted (the durable dedupe replays)."""
    app = _provider_app(
        _provider_cfg(
            rpc_commands_require_signature="on", rpc_service_keys=SERVICE_KEYS
        )
    )
    seen: list[str] = []

    class Flaky(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.inner = ASGITransport(app=app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await self.inner.handle_async_request(request)
            if request.url.path.endswith("/charge"):
                seen.append(request.headers[rpc_identity.SVC_SIG_HEADER])
                if len(seen) == 1:
                    raise httpx.ReadTimeout("lost", request=request)
            return response

    proxy = RemoteServiceProxy(
        service_name=SERVICE,
        binding={"base_url": "http://tenancy", "service_id": "tenancy"},
        default_timeout_seconds=5.0,
        client=AsyncClient(transport=Flaky(), base_url="http://tenancy"),
    )
    assert await proxy.charge(key="k-10", amount=4) == 4
    assert len(seen) == 2 and seen[0] != seen[1]  # re-signed per attempt
    assert CHARGED == [4]  # executed once


def test_per_caller_quota_defaults_to_half_the_table() -> None:
    cfg = settings.model_copy(update={"rpc_replay_cache_max_entries": 200_000})
    assert (
        Settings.model_fields["rpc_replay_cache_per_caller_max_entries"].default
        == 100_000
    )
    guard = rpc_pair_mac.replay_guard(
        cfg.model_copy(update={"rpc_replay_cache_per_caller_max_entries": 0})
    )
    assert guard.per_caller == 100_000


def test_clock_skew_beyond_half_the_window_is_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def skew() -> float:
        value = REGISTRY.get_sample_value(
            "sandloom_rpc_auth_clock_skew_total", {"caller": "billing"}
        )
        return float(value or 0.0)

    rpc_inbound_auth._skew_logged.clear()
    before = skew()
    now = int(time.time())
    rpc_inbound_auth._observe_clock_skew("billing", now - 10, 60)  # in sync: nothing
    assert skew() == before
    with caplog.at_level(logging.WARNING, logger="sandloom.core.rpc"):
        rpc_inbound_auth._observe_clock_skew(
            "billing", now - 40, 60
        )  # accepted, drifting
        rpc_inbound_auth._observe_clock_skew("billing", now + 45, 60)
    assert skew() == before + 2
    assert caplog.text.count("clock is") == 1  # rate-limited log


@pytest.mark.asyncio
async def test_a_signed_mac_call_is_ed25519_signed_once_per_attempt(
    caller: Any, commands_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nonce-less context is never signed (its signature was overwritten
    before sending): one signature per sent request, always over the context
    carrying that attempt's nonce — describe GETs and commands alike."""
    app = _provider_app(_provider_cfg(rpc_service_keys=SERVICE_KEYS))
    contexts: list[dict[str, Any]] = []
    real_sign = rpc_identity.sign

    def counting_sign(
        identity: Any, method: str, target: str, body: bytes, ctx: str
    ) -> str:
        contexts.append(json.loads(rpc_wire._unb64(ctx)))
        return real_sign(identity, method, target, body, ctx)

    monkeypatch.setattr(rpc_identity, "sign", counting_sign)
    rec = _Recorder()
    assert await _proxy(app, rec).charge(key="k-once", amount=1) == 1
    signed = [r for r in rec.requests if rpc_identity.SVC_SIG_HEADER in r.headers]
    assert signed and len(contexts) == len(signed)
    assert all(ctx.get("n") for ctx in contexts)
    sent = [
        json.loads(rpc_wire._unb64(r.headers[rpc_wire._CTX_HEADER])) for r in signed
    ]
    assert sent == contexts
