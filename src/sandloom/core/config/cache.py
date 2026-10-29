"""Cache / broker URLs and the bounded Redis client."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CacheConfig(BaseModel):
    """Cache / broker URLs and the bounded Redis client."""

    # Cache / Broker
    cache_url: str = Field(
        alias="CACHE_URL",
        description=(
            "Connection URL of the default cache instance, normally Redis; named "
            "instances and overrides are declared through INFRA."
        ),
    )
    broker_url: str = Field(
        alias="BROKER_URL",
        description=(
            "Connection URL used to configure the default task queue broker and "
            "Redis-backed event transport."
        ),
    )
    # Redis cache client hardening (built-in ``redis`` cache driver). Every
    # cache operation is bounded so an unreachable / hung Redis RAISES quickly
    # and the callers' fail-open / fail-closed policies (rate limit, token
    # revocation, idempotency, login lockout) actually run instead of the
    # request hanging. The pool is bounded and BLOCKING: when all connections
    # are busy a caller waits up to CACHE_POOL_TIMEOUT_SECONDS, then errors.
    # (The event-bus transport uses its own client with a longer read timeout —
    # none for pub-sub listen, BLOCK window + margin for Streams XREADGROUP.)
    cache_socket_timeout_seconds: float = Field(
        default=0.5,
        gt=0,
        alias="CACHE_SOCKET_TIMEOUT_SECONDS",
        description=(
            "Maximum seconds to wait for a Redis cache command response; timeout "
            "failures are not retried."
        ),
    )
    cache_socket_connect_timeout_seconds: float = Field(
        default=0.5,
        gt=0,
        alias="CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS",
        description=("Maximum seconds to establish a Redis cache connection."),
    )
    cache_health_check_interval_seconds: int = Field(
        default=30,
        ge=0,
        alias="CACHE_HEALTH_CHECK_INTERVAL_SECONDS",
        description=(
            "Redis cache connection health-check interval in seconds; zero "
            "disables periodic connection checks."
        ),
    )
    cache_max_connections: int = Field(
        default=64,
        ge=1,
        alias="CACHE_MAX_CONNECTIONS",
        description=(
            "Maximum connections in the default Redis cache pool; callers wait "
            "within CACHE_POOL_TIMEOUT_SECONDS when the pool is busy."
        ),
    )
    cache_pool_timeout_seconds: float = Field(
        default=1.0,
        gt=0,
        alias="CACHE_POOL_TIMEOUT_SECONDS",
        description=(
            "Maximum seconds a Redis cache operation waits for a pooled "
            "connection before failing."
        ),
    )
    # Retries of a command that failed with a CONNECTION error (stale pooled
    # socket after a Redis restart). Timeouts are never retried, so the worst
    # case stays ~(1 + retries) x the socket timeout.
    cache_retry_attempts: int = Field(
        default=1,
        ge=0,
        le=5,
        alias="CACHE_RETRY_ATTEMPTS",
        description=(
            "Retries of Redis cache commands after connection errors. Command "
            "timeouts are never retried."
        ),
    )
