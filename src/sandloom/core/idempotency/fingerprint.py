"""Shared idempotency fingerprinting: who is asking, for which tenant, with
which ``Idempotency-Key`` and which request body.

Both guard forms — the cache guard (:mod:`sandloom.core.idempotency`) and the
durable guard (:mod:`sandloom.core.idempotency.durable`) — scope a key by the same
caller identity and tenant, validate it the same way and compare the same
request fingerprint, so a key means the same thing in either store. Also the
stored-record format both read.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import orjson
from fastapi import HTTPException, Request

import sandloom.core.auth.bearer as _bearer
import sandloom.core.auth.verification as _verification
import sandloom.core.ingress.client_ip as _client_ip
import sandloom.core.tenant as _tenant
from sandloom.core import effects

logger = logging.getLogger("sandloom.core.idempotency")


_MAX_KEY_LEN = 200
_DEFAULT_TTL_SECONDS = effects.RECORD_TTL_SECONDS
# Hop-by-hop / length headers that must not be replayed verbatim. ``set-cookie``
# is stripped too: an auth-sensitive cookie (e.g. a rotated session) set by the
# original response should not be re-issued on every later replay.
_SKIP_REPLAY_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "date",
    "set-cookie",
}


def _caller_identity(request: Request) -> str:
    """Stable identity of the caller for idempotency scoping.

    Prefers the authenticated subject (``sub`` of a verified ACCESS bearer
    token, via :func:`sandloom.core.auth.bearer.bearer_claims`) so the same user
    retrying
    after a token refresh still maps to one identity — binding to the raw
    bearer token would make every rotation look like a different caller and
    wrongly reject the retry with ``422``. Falls back to a hash of the raw
    Authorization header (distinct credentials stay distinct).

    For an UNAUTHENTICATED caller there is no stable principal, so identity is
    scoped by source IP (``anon:<ip>``) — best-effort isolation so two different
    anonymous callers cannot replay each other's stored response by guessing the
    same Idempotency-Key. This is defense-in-depth, not a guarantee (NAT / shared
    egress collapses distinct users into one IP): place ``idempotency_guard``
    AFTER authentication on any route that handles per-user data.
    """

    claims = _bearer.bearer_claims(request)
    sub = claims.get("sub") if claims else None
    if sub:
        return f"u:{sub}"
    auth = request.headers.get("authorization", "")
    if auth:
        return "h:" + hashlib.sha256(auth.encode()).hexdigest()[:32]
    # The real client behind trusted proxies (never the gateway's address, which
    # would collapse every anonymous caller into one namespace).
    return f"anon:{_client_ip.get_client_ip(request)}"


def _canonical_tenant_id(value: object) -> str | None:
    """Return one stable representation for header, claim and bound tenant IDs."""
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return str(UUID(raw))
    except ValueError:
        # TenantScope will reject an invalid UUID separately. Keeping the raw
        # value here still prevents it from sharing an idempotency namespace.
        return raw


def _tenant_identity(request: Request) -> str:
    """Resolve the effective tenant using the same precedence as TenantScope."""

    bound = _tenant.current_tenant_id_optional()
    if bound is not None:
        return f"t:{bound}"

    header_tenant = _canonical_tenant_id(request.headers.get("X-Tenant-Id", ""))
    if header_tenant is not None:
        return f"t:{header_tenant}"

    claims = _bearer.bearer_claims(request)
    if claims:
        claim_tenant = _canonical_tenant_id(
            claims.get("tenant_id") or claims.get("org_id") or ""
        )
        if claim_tenant is not None:
            return f"t:{claim_tenant}"
    return "t:none"


async def _request_fingerprint(
    request: Request, caller: str | None = None, tenant: str | None = None
) -> str:
    """Stable fingerprint of (method, path, query, caller, tenant, body).

    Reusing one Idempotency-Key for a DIFFERENT request is client misuse and
    must be rejected rather than silently treated as the original. Starlette
    caches ``request.body()`` so reading it here does not consume it for the
    endpoint.
    """
    body = await request.body()
    if caller is None:
        caller = _caller_identity(request)
    if tenant is None:
        tenant = _tenant_identity(request)
    # Sorting pairs makes semantically identical query strings stable while
    # preserving repeated parameters.
    query = orjson.dumps(sorted(request.query_params.multi_items()))
    raw = b"|".join(
        [
            request.method.encode(),
            request.url.path.encode(),
            query,
            caller.encode(),
            tenant.encode(),
            body,
        ]
    )
    return hashlib.sha256(raw).hexdigest()


class _CacheUnavailable(Exception):
    """Raised internally when the idempotency store cannot be reached."""


@dataclass
class IdempotencyReservation:
    """An owned ``pending`` reservation of one idempotency key.

    ``pending_value`` is the EXACT cache value written by the reservation; it
    embeds a random owner token, so every later transition of the key
    (finalise / release) is a compare-and-set against it. A request whose
    reservation expired and was re-reserved by a retry can therefore never
    delete or overwrite that retry's reservation (which would let a third
    request run concurrently — a double execution).
    """

    full_key: str
    fingerprint: str
    ttl_seconds: int
    pending_value: str
    # Unauthenticated callers have no stable principal (identity falls back to
    # the source IP); their responses are never stored for body replay.
    anonymous: bool = False
    # ``idempotency_guard(store_response=False)``: dedupe IN-FLIGHT requests
    # only — a success releases the key like a failure (nothing is stored or
    # replayed); the endpoint owns durable idempotency itself.
    in_flight_only: bool = False


# Attribute name used to pass the armed key from the dependency to the
# middleware via ``request.state``.
_ARMED_ATTR = "_sandloom_idempotency"

_CORRUPT: dict[str, Any] = {"state": "corrupt"}


def _parse_record(raw: str) -> dict[str, Any]:
    """Decode a stored record; an unparseable one reads as ``corrupt``."""
    try:
        loaded = orjson.loads(raw)
    except (TypeError, ValueError):
        return dict(_CORRUPT)
    if not isinstance(loaded, dict):
        return dict(_CORRUPT)
    return loaded


async def _request_key(request: Request, required: bool) -> str | None:
    """The validated ``Idempotency-Key`` (``None``: absent and optional).

    A replay short-circuits later FastAPI dependencies, so the bearer's live
    security state is authenticated BEFORE reserving or replaying, wherever
    the endpoint placed its ordinary auth guard (which then shares
    ``ensure_token_not_revoked``'s per-request memo).
    """
    key = request.headers.get("Idempotency-Key")
    if not key:
        if required:
            raise HTTPException(
                status_code=400, detail="Idempotency-Key header is required"
            )
        return None
    if len(key) > _MAX_KEY_LEN or not key.isprintable():
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key")

    claims = _bearer.bearer_claims(request)
    if claims is not None:
        await _verification.ensure_token_not_revoked(request, claims)
    return key
