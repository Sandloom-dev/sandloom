"""The HTTP edge: frontend / CORS, ops-endpoint credentials, client-IP trust
and rate limits.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HttpConfig(BaseModel):
    """The HTTP edge: frontend / CORS, ops-endpoint credentials, client-IP
    trust and rate limits.
    """

    # Frontend
    frontend_url: str = Field(
        default="http://localhost:3000",
        alias="FRONTEND_URL",
        description=(
            "Frontend base URL used by generated links and as the default CORS origin."
        ),
    )

    # CORS: allowed origins (JSON list). Empty -> [frontend_url].
    cors_origins: list[str] = Field(
        default_factory=list,
        alias="CORS_ORIGINS",
        description=(
            "JSON list of allowed CORS origins; an empty list uses FRONTEND_URL."
        ),
    )

    # Ops endpoints (/diagnostics, /contracts, /metrics, /health details).
    # Disabled (404) unless this token is configured; callers must send it in
    # the X-Internal-Token header. Prod requires >= 32 chars. It never
    # authenticates internal RPC (/_sandloom/rpc).
    internal_api_token: str | None = Field(
        default=None,
        alias="INTERNAL_API_TOKEN",
        description=(
            "Credential for operational diagnostics, contract metadata, health "
            "details and metrics through X-Internal-Token; never authenticates "
            "internal RPC."
        ),
    )

    # Explicit developer opt-in: outside prod, open the ops endpoints when no
    # INTERNAL_API_TOKEN is configured (never /_sandloom/rpc). Off by default (fail
    # closed), so an unset ENVIRONMENT (which defaults to dev) exposes nothing.
    internal_endpoints_open_in_dev: bool = Field(
        default=False,
        alias="INTERNAL_ENDPOINTS_OPEN_IN_DEV",
        description=(
            "Open operational endpoints without INTERNAL_API_TOKEN outside "
            "production only; has no effect in production or on internal RPC."
        ),
    )
    # MAC key of the planner's opaque keyset cursors (identical in every
    # instance of a service). Unset = INTERNAL_API_TOKEN, else JWT_SECRET.
    planner_cursor_secret: str | None = Field(
        default=None,
        alias="PLANNER_CURSOR_SECRET",
        description=(
            "HMAC secret for opaque planner pagination cursors; unset falls back "
            "to INTERNAL_API_TOKEN, then JWT_SECRET."
        ),
    )
    # Optional scrape-only credential for /metrics (sent as X-Internal-Token).
    # Lets Prometheus scrape without holding the token that unlocks the
    # diagnostics endpoints. /metrics also accepts INTERNAL_API_TOKEN.
    metrics_token: str | None = Field(
        default=None,
        alias="METRICS_TOKEN",
        description=(
            "Optional scrape-only X-Internal-Token credential for /metrics, "
            "without access to other operational endpoints."
        ),
    )

    # Trust X-Forwarded-For from the fronting proxy/gateway (client-IP
    # resolution for rate limiting etc.; see sandloom.core.ingress.client_ip).
    trust_proxy_headers: bool = Field(
        default=False,
        alias="TRUST_PROXY_HEADERS",
        description=(
            "Honor forwarded client-IP headers only under the configured "
            "trusted-proxy policy."
        ),
    )

    # CIDRs (JSON list) of the proxies whose X-Forwarded-For may be trusted.
    # XFF is honored ONLY if the immediate TCP peer is within one of these
    # ranges; the chain is then walked right-to-left skipping trusted hops and
    # the first untrusted hop is the client. When empty and trust_proxy_headers
    # is on, production ignores XFF (uses the peer) and dev uses the rightmost
    # hop; a warning is logged once.
    trusted_proxy_ips: list[str] = Field(
        default_factory=list,
        alias="TRUSTED_PROXY_IPS",
        description=(
            "JSON list of trusted proxy CIDRs; forwarded chains are walked from "
            "the immediate peer toward the first untrusted hop."
        ),
    )

    # Force rate limiting outside prod (it is always enforced in prod).
    rate_limit_enabled: bool = Field(
        default=False,
        alias="RATE_LIMIT_ENABLED",
        description=(
            "Enable route rate limiting outside production; production always "
            "enforces configured route limits."
        ),
    )
    # Opt-in global per-IP floor across EVERY route (0 = off, the default in
    # every environment): requests per RATE_LIMIT_GLOBAL_PERIOD_SECONDS from
    # one client IP, 429 above it, fail-open on a cache outage. Routes keep
    # their own rate_limiter on top. One cache round trip per request; the
    # primary floor belongs at the edge (gateway / load balancer).
    rate_limit_global_requests: int = Field(
        default=0,
        ge=0,
        alias="RATE_LIMIT_GLOBAL_REQUESTS",
        description=(
            "Global per-client-IP request allowance per configured period; zero "
            "disables this additional fail-open floor."
        ),
    )
    rate_limit_global_period_seconds: int = Field(
        default=60,
        ge=1,
        alias="RATE_LIMIT_GLOBAL_PERIOD_SECONDS",
        description=(
            "Window length in seconds for the global per-client-IP request allowance."
        ),
    )
    rate_limit_global_exempt_path_prefixes: list[str] = Field(
        default_factory=lambda: ["/health", "/ready", "/metrics", "/_sandloom/"],
        alias="RATE_LIMIT_GLOBAL_EXEMPT_PATH_PREFIXES",
        description=(
            "Request path prefixes excluded from the additional global rate limiter."
        ),
    )
