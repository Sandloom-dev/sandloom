"""Internal-RPC keys of bench processes, as env vars (NEVER imports ``sandloom``).

Internal RPC has one authentication (ADR-0004 pair-MAC + keyed response
binding): every calling / serving process needs its own Ed25519 + X25519
keys, the public keys of its peers (``RPC_SERVICE_KEYS`` /
``RPC_SERVICE_DH_KEYS``) and the provider-side ``RPC_ALLOWED_CALLS``. The
stands build their env BEFORE ``sandloom`` is imported (settings are read at
import), so the keys are generated here with ``cryptography`` only — the
same shape as ``sandloom.core.contract_testing.rpc_env``. Keys are ephemeral and
cached per process: every env built in one process agrees on them, so a
parent can key the service processes it spawns.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Iterable, Mapping
from typing import Any

KID = "k1"


def _pems(key: Any) -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization

    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


@functools.cache
def keys(service_id: str) -> tuple[str, str, str, str]:
    """``(sign_private, sign_public, dh_private, dh_public)`` PEMs of a service."""
    from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

    sign = _pems(ed25519.Ed25519PrivateKey.generate())
    dh = _pems(x25519.X25519PrivateKey.generate())
    return (*sign, *dh)


def env(
    service_id: str,
    *,
    peers: Iterable[str] = (),
    allowed_calls: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, str]:
    """Env keying ``service_id`` (own keys + public keys of itself and
    ``peers``) with ``RPC_ALLOWED_CALLS`` = ``allowed_calls``."""
    sign_private, _, dh_private, _ = keys(service_id)
    services = list(dict.fromkeys([service_id, *peers]))
    return {
        "RPC_SERVICE_ID": service_id,
        "RPC_PRIVATE_KEY": sign_private,
        "RPC_KEY_ID": KID,
        "RPC_DH_PRIVATE_KEY": dh_private,
        "RPC_DH_KEY_ID": KID,
        "RPC_SERVICE_KEYS": json.dumps({s: {KID: keys(s)[1]} for s in services}),
        "RPC_SERVICE_DH_KEYS": json.dumps({s: {KID: keys(s)[3]} for s in services}),
        "RPC_ALLOWED_CALLS": json.dumps(
            {caller: sorted(c) for caller, c in (allowed_calls or {}).items()}
        ),
    }
