"""S2 auth for the bare apps: HS256 verify + one Redis GET (Sandloom's semantics).

Mirrors what Sandloom's ``current_context`` guard checks on an access token: a valid
HS256 signature with ``exp``/``nbf`` honoured, ``token_type == "access"``, an
``iat`` claim, and the subject's revocation mark (``tokens_valid_after``) read
from Redis — a token issued before the mark is rejected.
"""

from __future__ import annotations

from typing import Any

import jwt
import redis.asyncio as redis

from benchsuite import common


class AuthError(Exception):
    """Authentication failed (mapped to 401 by each framework)."""


def make_redis() -> redis.Redis:
    """A Redis client configured like Sandloom's default cache driver."""
    pool = redis.BlockingConnectionPool.from_url(
        common.cache_url(),
        max_connections=common.cache_max_connections(),
        timeout=5.0,
        encoding="utf-8",
        decode_responses=True,
    )
    return redis.Redis(connection_pool=pool)


_SECRET = common.jwt_secret()


def _decode(token: str) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(token, _SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise AuthError("invalid or expired token") from exc
    return payload


async def authenticate(authorization: str | None, client: redis.Redis) -> str:
    """Return the subject of a valid, unrevoked access token or raise."""
    if not authorization:
        raise AuthError("not authenticated")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("not authenticated")
    payload = _decode(token)
    if payload.get("token_type") != "access":
        raise AuthError("invalid token type")
    sub = payload.get("sub")
    iat = payload.get("iat")
    if not sub or not isinstance(iat, int | float):
        raise AuthError("invalid token claims")
    mark = await client.get(common.REVOCATION_KEY_PREFIX + str(sub))
    if mark is not None and float(iat) < float(mark):
        raise AuthError("token has been revoked")
    return str(sub)
