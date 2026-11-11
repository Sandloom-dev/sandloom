"""Verify the external issuer's id_tokens and back-channel logout tokens.

Discovery (``<issuer>/.well-known/openid-configuration``) gives the JWKS
URI; the keys are cached for ``ACME_IDP_JWKS_TTL_SECONDS`` and refetched once
on an unknown ``kid`` (key rotation). Only the sign-in and the logout
endpoints call this: the cold path, never a guarded request.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt

from .config import AcmeIdpSettings

#: The ``events`` member a back-channel logout token carries.
BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"
_ALGORITHMS = ["RS256", "ES256", "EdDSA"]


class ExternalTokenRejected(ValueError):
    """The external token is not one the configured issuer signed for us."""


class _Keys:
    """The issuer's JWKS, cached in process memory."""

    def __init__(self) -> None:
        self.keys: dict[str, Any] = {}
        self.fetched_at = 0.0
        self.source = ""

    async def get(self, cfg: AcmeIdpSettings, kid: str) -> Any:
        fresh = time.monotonic() - self.fetched_at < cfg.jwks_ttl_seconds
        if self.source != cfg.issuer or not fresh or kid not in self.keys:
            await self._fetch(cfg)
        try:
            return self.keys[kid]
        except KeyError:
            raise ExternalTokenRejected(f"unknown signing key {kid!r}") from None

    async def _fetch(self, cfg: AcmeIdpSettings) -> None:
        base = cfg.issuer.rstrip("/")
        async with httpx.AsyncClient(timeout=5) as client:
            meta = (await client.get(f"{base}/.well-known/openid-configuration")).json()
            if meta.get("issuer") != cfg.issuer:
                raise ExternalTokenRejected("discovery names another issuer")
            jwks = (await client.get(meta["jwks_uri"])).json()
        keyset = jwt.PyJWKSet.from_dict(jwks)
        self.keys = {k.key_id: k for k in keyset.keys if k.key_id}
        self.fetched_at = time.monotonic()
        self.source = cfg.issuer


_KEYS = _Keys()


async def verify(
    cfg: AcmeIdpSettings, token: str, *, require: list[str]
) -> dict[str, Any]:
    """The verified claims of ``token`` (else :class:`ExternalTokenRejected`)."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise ExternalTokenRejected("not a JWT") from exc
    kid = header.get("kid")
    if not isinstance(kid, str) or header.get("alg") not in _ALGORITHMS:
        raise ExternalTokenRejected("no kid or an unsupported algorithm")
    key = await _KEYS.get(cfg, kid)
    try:
        return jwt.decode(
            token,
            key.key,
            algorithms=[header["alg"]],
            audience=cfg.client_id,
            issuer=cfg.issuer,
            options={"require": ["iss", "sub", "aud", "iat", "exp", *require]},
        )
    except jwt.PyJWTError as exc:
        raise ExternalTokenRejected(str(exc)) from exc


async def verify_id_token(cfg: AcmeIdpSettings, token: str) -> dict[str, Any]:
    """An id_token's claims: signature, issuer, audience, lifetime, email."""
    claims = await verify(cfg, token, require=["email"])
    if "events" in claims:
        raise ExternalTokenRejected("a logout token is not an id_token")
    return claims


async def verify_logout_token(cfg: AcmeIdpSettings, token: str) -> dict[str, Any]:
    """A back-channel logout token's claims (OIDC Back-Channel Logout 1.0)."""
    claims = await verify(cfg, token, require=["jti", "events"])
    events = claims.get("events")
    if not isinstance(events, dict) or BACKCHANNEL_LOGOUT_EVENT not in events:
        raise ExternalTokenRejected("not a back-channel logout token")
    if "nonce" in claims:
        raise ExternalTokenRejected("a logout token carries no nonce")
    return claims
