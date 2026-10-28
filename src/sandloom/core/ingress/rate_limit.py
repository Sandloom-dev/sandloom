"""Reusable rate limiting dependency for FastAPI endpoints (core-level).

The limiter is backed by the operation-based ``CacheProvider``
(``sandloom.core.infra.access.cache``): the atomic INCR+EXPIRE primitive lives in
the provider, so this module stays infrastructure-agnostic and is plug-
compatible with any cache backend.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from sandloom.core.config.settings import settings
from sandloom.core.infra.access import cache
from sandloom.core.ingress.client_ip import get_client_ip
from sandloom.core.observability.metrics import (
    rate_limit_rejected_total,
    rate_limit_unavailable_total,
)

logger = logging.getLogger("sandloom.core.http.rate_limit")


def rate_limiter(
    requests: int, period: int, *, fail_closed: bool = False
) -> Callable[[Request], Awaitable[None]]:
    """Create a dependency that enforces a sliding-window-counter rate limit.

    A plain fixed window lets a client send ``requests`` at the very end of one
    window and ``requests`` again at the start of the next — a 2x burst across
    the boundary. The sliding-window counter weights the previous window's
    count by how much of it still overlaps the trailing ``period``::

        estimate = previous * (1 - elapsed_fraction) + current

    using only the operation-based cache contract (``incr_window`` — one
    round trip on Redis, ``incr`` + ``get`` elsewhere), so it stays
    backend-agnostic. Each window counter lives for two periods so the
    previous one is still readable while the current one fills.

    Args:
        requests: Max allowed requests within the (sliding) window.
        period: Window size in seconds.
        fail_closed: Behavior when the rate-limit cache is unavailable.
            ``False`` (default) fails OPEN (allow + warn) — right for ordinary
            endpoints where a transient cache blip should not 500 every request.
            ``True`` fails CLOSED (``503``) — use on admission-control-sensitive
            endpoints (login, password reset, payment, irreversible mutations)
            where a cache outage must not silently lift the limit.

    Returns:
        An async dependency callable that raises HTTP 429 if limit is exceeded.
    """

    async def dependency(request: Request) -> None:
        # Enforced in prod (and anywhere RATE_LIMIT_ENABLED=1); bypassed in
        # dev/test to avoid a hard Redis dependency in unit tests.
        if not (settings.is_prod or settings.rate_limit_enabled):
            return None

        ip = get_client_ip(request, config=settings)
        # Per-route buckets so one endpoint cannot exhaust another's budget.
        # Key on the matched route *template* (``/items/{id}``), not the raw
        # path: otherwise every distinct path param is its own bucket and an
        # attacker enumerating ids never trips the limit (and the cache fills
        # with unbounded keys).
        base = f"rate_limit:{ip}:{_route_key(request)}"

        # On a cache outage the behavior is governed by fail_closed: ordinary
        # endpoints fail open (a transient blip should not 500 every request);
        # admission-control endpoints fail closed (503) so the limit can never
        # be silently lifted.
        estimate = await _estimate_or_none(base, period)
        if estimate is None:
            if fail_closed:
                rate_limit_unavailable_total.labels("closed").inc()
                logger.error(
                    "Rate-limit cache unavailable for %s; failing closed (503)",
                    request.url.path,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Rate limiter unavailable; retry later.",
                ) from None
            rate_limit_unavailable_total.labels("open").inc()
            logger.warning(
                "Rate-limit cache unavailable for %s; failing open (allowing)",
                request.url.path,
            )
            return None
        if estimate > requests:
            rate_limit_rejected_total.labels(_route_label(request)).inc()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded.",
                headers={"Retry-After": str(period)},
            )

    return dependency


async def _sliding_window_estimate(base: str, period: int) -> float:
    """Count one hit on ``base`` and return the sliding-window estimate.

    ``previous * (1 - elapsed_fraction) + current`` over two fixed windows
    whose counters live two periods. ``incr_window`` (one round trip on Redis)
    atomically increments and sets the TTL on the first hit, so no counter can
    leak without an expiry; duck-typed providers without it use incr + get.
    """
    now = time.time()
    window = int(now) // period
    elapsed_fraction = (now - window * period) / period
    key = f"{base}:{window}"
    previous_key = f"{base}:{window - 1}"
    provider = cache()
    incr_window = getattr(provider, "incr_window", None)
    if incr_window is not None:
        count, previous_raw = await incr_window(
            key, previous_key, ttl_seconds=2 * period
        )
    else:
        count = await provider.incr(key, ttl_seconds=2 * period)
        previous_raw = await provider.get(previous_key)
    try:
        previous = int(previous_raw) if previous_raw is not None else 0
    except (TypeError, ValueError):
        previous = 0
    return previous * (1.0 - elapsed_fraction) + float(count)


async def _estimate_or_none(base: str, period: int) -> float | None:
    """:func:`_sliding_window_estimate`, or ``None`` when the cache is down."""
    try:
        return await _sliding_window_estimate(base, period)
    except Exception:  # noqa: BLE001 - cache outage: the caller fails open/closed
        return None


class GlobalRateLimitMiddleware:
    """Opt-in per-IP floor across EVERY route (``RATE_LIMIT_GLOBAL_REQUESTS``).

    ``rate_limiter`` limits only the routes that declare it, so unknown paths
    (404 scans) and ordinary routes are otherwise unlimited at the app layer.
    This coarse floor counts every request of a client IP (one sliding window,
    the same algorithm) and answers ``429`` above it. Off by default in every
    environment: it costs one cache round trip per request, and a misconfigured
    ``TRUSTED_PROXY_IPS`` would put every client behind one IP — the primary
    floor belongs at the edge (gateway / load balancer). It fails OPEN on a
    cache outage (DoS mitigation, not admission control) and skips
    ``RATE_LIMIT_GLOBAL_EXEMPT_PATH_PREFIXES`` (probes, metrics, internal RPC).
    Per-route limits still apply on top of it.
    """

    def __init__(
        self, app: Any, *, requests: int, period: int, exempt: tuple[str, ...]
    ) -> None:
        self.app = app
        self._requests, self._period, self._exempt = requests, period, exempt

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or str(scope.get("path", "")).startswith(
            self._exempt
        ):
            await self.app(scope, receive, send)
            return
        ip = get_client_ip(Request(scope), config=settings)
        estimate = await _estimate_or_none(f"rate_limit:global:{ip}", self._period)
        if estimate is None:
            rate_limit_unavailable_total.labels("open").inc()
            logger.warning("Rate-limit cache unavailable; global floor failing open")
            await self.app(scope, receive, send)
            return
        if estimate <= self._requests:
            await self.app(scope, receive, send)
            return
        rate_limit_rejected_total.labels("global").inc()
        await JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Rate limit exceeded.", "code": "rate_limited"},
            headers={"Retry-After": str(self._period)},
        )(scope, receive, send)


def _route_key(request: Request) -> str:
    """Matched route template for the rate-limit bucket, or the raw path.

    Mirrors the metric label policy: the template keeps the key space bounded
    so a per-route limit is genuinely per-route, not per-(route, path-param).
    A dependency only runs inside a matched route (FastAPI sets
    ``scope["route"]``, mounted FastAPI sub-apps included), so the raw-path
    fallback serves only request stand-ins (unit tests); unmatched requests
    (404s) never reach a ``rate_limiter`` — see ``GlobalRateLimitMiddleware``.
    """
    scope = getattr(request, "scope", None)
    route = scope.get("route") if isinstance(scope, dict) else None
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return request.url.path


def _route_label(request: Request) -> str:
    """Matched route template, or ``unmatched`` (bounded metric label)."""
    scope = getattr(request, "scope", None)
    route = scope.get("route") if isinstance(scope, dict) else None
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else "unmatched"
