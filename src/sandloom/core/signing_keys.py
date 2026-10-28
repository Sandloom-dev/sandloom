"""Asymmetric key material for JWTs and service identities (no settings import).

Pure helpers shared by :mod:`sandloom.core.config.settings` (startup validation),
:mod:`sandloom.core.auth.tokens` (minting / verifying access tokens),
:mod:`sandloom.core.rpc` (service-identity signatures) and the ``sandloom keys`` CLI.
Importing this module never builds the settings object.

Algorithm confusion is ruled out structurally: every asymmetric algorithm is
bound to ONE key type (``EdDSA`` -> Ed25519, ``RS256`` -> RSA >= 2048 bit,
``ES256`` -> EC P-256), keys are parsed into typed key objects at load, and a
key of the wrong type is a configuration error — so a public key can never be
fed to an HMAC verifier, and ``none`` / ``HS*`` are never in the accepted
algorithm list of an asymmetric deployment.
"""

from __future__ import annotations

import base64
import functools
import json
from typing import Any, Literal, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa, x25519

#: Symmetric (shared-secret) JWT algorithms.
HMAC_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})
#: Asymmetric JWT algorithms and the single key type each accepts.
ASYMMETRIC_ALGORITHMS = frozenset({"EdDSA", "RS256", "ES256"})
#: Smallest RSA modulus accepted for RS256.
MIN_RSA_BITS = 2048

AsymmetricAlgorithm = Literal["EdDSA", "RS256", "ES256"]
PrivateKey = ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey
PublicKey = ed25519.Ed25519PublicKey | rsa.RSAPublicKey | ec.EllipticCurvePublicKey


class KeyConfigError(ValueError):
    """A configured key is malformed or of the wrong type for its algorithm."""


def is_asymmetric(algorithm: str) -> bool:
    return algorithm in ASYMMETRIC_ALGORITHMS


def normalize_pem(value: str) -> str:
    """Accept a PEM whose newlines were escaped (``\\n``) in an env var."""
    text = value.strip()
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    return text + "\n"


def _check_type(key: object, algorithm: str, what: str) -> None:
    if algorithm == "EdDSA":
        ok = isinstance(key, ed25519.Ed25519PrivateKey | ed25519.Ed25519PublicKey)
        expected = "an Ed25519 key"
    elif algorithm == "RS256":
        ok = isinstance(key, rsa.RSAPrivateKey | rsa.RSAPublicKey)
        expected = f"an RSA key (>= {MIN_RSA_BITS} bit)"
        if ok and cast(Any, key).key_size < MIN_RSA_BITS:
            raise KeyConfigError(
                f"{what} is a {cast(Any, key).key_size}-bit RSA key; RS256 needs "
                f">= {MIN_RSA_BITS} bit"
            )
    elif algorithm == "ES256":
        ok = isinstance(
            key, ec.EllipticCurvePrivateKey | ec.EllipticCurvePublicKey
        ) and isinstance(cast(Any, key).curve, ec.SECP256R1)
        expected = "an EC P-256 key"
    else:
        raise KeyConfigError(f"{algorithm!r} is not an asymmetric JWT algorithm")
    if not ok:
        raise KeyConfigError(
            f"{what} is {type(key).__name__}, but {algorithm} requires {expected} "
            "(the key type is bound to the algorithm to rule out algorithm confusion)"
        )


@functools.lru_cache(maxsize=64)
def load_private_key(pem: str, algorithm: str) -> PrivateKey:
    """Parse a PEM private key and check it fits ``algorithm`` (cached)."""
    try:
        key = serialization.load_pem_private_key(
            normalize_pem(pem).encode(), password=None
        )
    except (ValueError, TypeError) as exc:
        raise KeyConfigError(
            "private key is not an unencrypted PEM private key"
        ) from exc
    _check_type(key, algorithm, "the private key")
    return cast(PrivateKey, key)


@functools.lru_cache(maxsize=256)
def load_public_key(pem: str, algorithm: str) -> PublicKey:
    """Parse a PEM public key and check it fits ``algorithm`` (cached)."""
    try:
        key = serialization.load_pem_public_key(normalize_pem(pem).encode())
    except (ValueError, TypeError) as exc:
        raise KeyConfigError("public key is not a PEM public key") from exc
    _check_type(key, algorithm, "the public key")
    return cast(PublicKey, key)


def public_pem(key: PrivateKey | PublicKey) -> str:
    """The SubjectPublicKeyInfo PEM of a key (or of a private key's public half)."""
    handle: Any = key
    public = handle.public_key() if hasattr(handle, "private_bytes") else handle
    pem: bytes = public.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem.decode()


def same_public_key(private: PrivateKey, public: PublicKey) -> bool:
    return public_pem(private) == public_pem(public)


def generate_private_key(algorithm: str) -> PrivateKey:
    """A fresh private key for ``algorithm`` (``sandloom keys generate``)."""
    if algorithm == "EdDSA":
        return ed25519.Ed25519PrivateKey.generate()
    if algorithm == "RS256":
        return rsa.generate_private_key(public_exponent=65537, key_size=3072)
    if algorithm == "ES256":
        return ec.generate_private_key(ec.SECP256R1())
    raise KeyConfigError(f"{algorithm!r} is not an asymmetric algorithm")


def private_pem(key: PrivateKey) -> str:
    handle: Any = key
    pem: bytes = handle.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem.decode()


# ---------------------------------------------------------------------------
# Service identity (internal RPC): Ed25519 detached signatures
# ---------------------------------------------------------------------------


def sign_ed25519(key: PrivateKey, message: bytes) -> str:
    """Detached Ed25519 signature, base64url without padding."""
    if not isinstance(key, ed25519.Ed25519PrivateKey):  # pragma: no cover
        raise KeyConfigError("service identity keys must be Ed25519")
    return base64.urlsafe_b64encode(key.sign(message)).decode().rstrip("=")


def verify_ed25519(key: PublicKey, message: bytes, signature: str) -> bool:
    if not isinstance(key, ed25519.Ed25519PublicKey):  # pragma: no cover
        return False
    try:
        raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        key.verify(raw, message)
    except Exception:  # noqa: BLE001 - any malformed / wrong signature
        return False
    return True


# ---------------------------------------------------------------------------
# Response-binding key agreement (internal RPC): dedicated X25519 keys
# ---------------------------------------------------------------------------
# A service's X25519 key is SEPARATE from its Ed25519 signing key (no
# cross-protocol key reuse); everything below is the `cryptography` library.


def generate_x25519_key() -> x25519.X25519PrivateKey:
    return x25519.X25519PrivateKey.generate()


@functools.lru_cache(maxsize=64)
def load_x25519_private_key(pem: str) -> x25519.X25519PrivateKey:
    """Parse a PEM (PKCS#8) X25519 private key (cached)."""
    try:
        key = serialization.load_pem_private_key(
            normalize_pem(pem).encode(), password=None
        )
    except (ValueError, TypeError) as exc:
        raise KeyConfigError(
            "private key is not an unencrypted PEM private key"
        ) from exc
    if not isinstance(key, x25519.X25519PrivateKey):
        raise KeyConfigError(f"the private key is {type(key).__name__}, not X25519")
    return key


@functools.lru_cache(maxsize=256)
def load_x25519_public_key(pem: str) -> x25519.X25519PublicKey:
    """Parse a PEM (SubjectPublicKeyInfo) X25519 public key (cached)."""
    try:
        key = serialization.load_pem_public_key(normalize_pem(pem).encode())
    except (ValueError, TypeError) as exc:
        raise KeyConfigError("public key is not a PEM public key") from exc
    if not isinstance(key, x25519.X25519PublicKey):
        raise KeyConfigError(f"the public key is {type(key).__name__}, not X25519")
    return key


def x25519_private_pem(key: x25519.X25519PrivateKey) -> str:
    # (the pinned types-cryptography stub mistypes the serialization enums)
    handle: Any = key
    pem: bytes = handle.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem.decode()


def x25519_public_pem(key: x25519.X25519PrivateKey | x25519.X25519PublicKey) -> str:
    public: Any = key.public_key() if isinstance(key, x25519.X25519PrivateKey) else key
    pem: bytes = public.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem.decode()


def keys_json(kid: str, pem: str) -> str:
    """``{"<kid>": "<pem>"}`` as one-line JSON (an env-var friendly snippet)."""
    return json.dumps({kid: pem})


__all__ = [
    "ASYMMETRIC_ALGORITHMS",
    "HMAC_ALGORITHMS",
    "KeyConfigError",
    "generate_private_key",
    "generate_x25519_key",
    "is_asymmetric",
    "load_private_key",
    "load_public_key",
    "load_x25519_private_key",
    "load_x25519_public_key",
    "normalize_pem",
    "private_pem",
    "public_pem",
    "same_public_key",
    "sign_ed25519",
    "verify_ed25519",
    "x25519_private_pem",
    "x25519_public_pem",
]
