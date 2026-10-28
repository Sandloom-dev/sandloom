"""Prometheus metrics foundation (Phase G).

Centralizes metric object definitions so other core components can import
without causing circular dependencies. Internal metric names used previously
(`startup_task_failures`, `events_published`, etc.) are mapped to exported
Prometheus counters with conventional `_total` suffix.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from typing import Any, cast

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


def render_metrics() -> bytes:
    """Expose every Gunicorn worker, or the in-process registry outside it.

    Gunicorn sets the directory before importing the application. A fresh
    collector registry avoids exporting the scraping worker's metrics twice.
    """
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client.multiprocess import MultiProcessCollector  # noqa: PLC0415 - optional: only under PROMETHEUS_MULTIPROC_DIR (gunicorn multiprocess mode)

        registry = CollectorRegistry()
        cast(Callable[[CollectorRegistry], Any], MultiProcessCollector)(registry)
        return generate_latest(registry)
    return generate_latest()


# HTTP RED metrics (Rate / Errors / Duration). Labeled by the route TEMPLATE
# (e.g. /users/{id}) — never the raw path — to bound cardinality.
http_requests_total = cast(
    Any,
    Counter(
        "sandloom_http_requests_total",
        "HTTP requests handled",
        ["method", "path", "status"],
    ),
)
http_request_duration_seconds = cast(
    Any,
    Histogram(
        "sandloom_http_request_duration_seconds",
        "HTTP request latency in seconds",
        ["method", "path"],
    ),
)
http_requests_in_progress = cast(
    Any,
    Gauge(
        "sandloom_http_requests_in_progress",
        "In-flight HTTP requests",
        ["method"],
        multiprocess_mode="livesum",
    ),
)

"""Event bus metrics (labeled).

Labels rationale:
- channel: allows per-channel visibility and filtering.
- status (for delivered): success|failure aggregate (a delivery failure is
  ``events_delivered_total{status="failure"}``).
"""

events_published_total = cast(
    Any,
    Counter(
        "sandloom_events_published_total",
        "Events successfully published",
        ["channel"],
    ),
)
events_publish_failures_total = cast(
    Any,
    Counter(
        "sandloom_events_publish_failures_total",
        "Event publish failures",
        ["channel"],
    ),
)
events_delivered_total = cast(
    Any,
    Counter(
        "sandloom_events_delivered_total",
        "Events delivered to callbacks (labeled by status)",
        ["channel", "status"],  # status=success|failure
    ),
)
subscriptions_total = cast(
    Any,
    Counter(
        "sandloom_subscriptions_total",
        "Total subscription registrations",
        ["channel"],
    ),
)
redis_reconnect_attempts_total = cast(
    Any,
    Counter(
        "sandloom_redis_reconnect_attempts_total",
        "Redis reconnect attempts for event bus",
    ),
)

# Phase J: schema validation counters
schema_validation_success_total = cast(
    Any,
    Counter(
        "sandloom_schema_validation_success_total",
        "Event payload schema validation successes",
    ),
)
schema_validation_failures_total = cast(
    Any,
    Counter(
        "sandloom_schema_validation_failures_total",
        "Event payload schema validation failures",
    ),
)

events_malformed_total = cast(
    Any,
    Counter(
        "sandloom_events_malformed_total",
        "Event bus messages dropped as malformed (unparseable transport frame)",
        ["channel"],
    ),
)

# Phase K: guardrails
slow_event_callbacks_total = cast(
    Any,
    Counter(
        "sandloom_slow_event_callbacks_total",
        "Event bus callbacks exceeding configured time budget",
        ["channel"],
    ),
)
event_payload_rejected_total = cast(
    Any,
    Counter(
        "sandloom_event_payload_rejected_total",
        "Events rejected due to payload size guard",
        ["channel"],
    ),
)

# Idempotency: a terminal record (succeeded marker / failed-key release) could
# not be persisted to the cache after the operation ran. A succeeded marker lost
# this way lets the pending reservation expire so a later retry may re-execute
# the completed operation — alert on this and rely on durable_inbox for
# exactly-once effects on money / irreversible work.
idempotency_terminal_persist_failures_total = cast(
    Any,
    Counter(
        "sandloom_idempotency_terminal_persist_failures_total",
        "Idempotency terminal records that could not be persisted after retries",
    ),
)

# Durable streams transport reliability
streams_messages_claimed_total = cast(
    Any,
    Counter(
        "sandloom_streams_messages_claimed_total",
        "Stream entries reclaimed from a stale/dead consumer via XAUTOCLAIM",
        ["stream"],
    ),
)
streams_messages_dlq_total = cast(
    Any,
    Counter(
        "sandloom_streams_messages_dlq_total",
        "Stream entries dead-lettered after exceeding the delivery budget",
        ["stream"],
    ),
)
streams_dlq_append_failures_total = cast(
    Any,
    Counter(
        "sandloom_streams_dlq_append_failures_total",
        "Failed attempts to append a poison entry to its dead-letter stream "
        "(the source entry is left un-acked for reclaim)",
        ["stream"],
    ),
)
# Best-effort stream maintenance that failed (operation: xautoclaim | xtrim):
# reclaim of a dead consumer's pending or the age-based trim is skipped, so a
# sustained rate means stranded backlog / an unbounded stream.
streams_maintenance_failures_total = cast(
    Any,
    Counter(
        "sandloom_streams_maintenance_failures_total",
        "Failed best-effort Streams maintenance calls (XAUTOCLAIM reclaim, "
        "XTRIM MINID age trim); the step is skipped and retried next interval",
        ["stream", "operation"],
    ),
)

# Cross-Module Query / Projection counters
projection_events_processed_total = cast(
    Any,
    Counter(
        "sandloom_projection_events_processed_total",
        "Projection consumer events processed (result=applied|skipped)",
        ["projection", "consumer", "result"],
    ),
)
projection_events_failed_total = cast(
    Any,
    Counter(
        "sandloom_projection_events_failed_total",
        "Projection consumer handler failures (event left for redelivery)",
        ["projection", "consumer"],
    ),
)
event_consumer_events_processed_total = cast(
    Any,
    Counter(
        "sandloom_event_consumer_events_processed_total",
        "Generic event consumer events processed (result=applied|skipped)",
        ["consumer", "result"],
    ),
)
event_consumer_events_failed_total = cast(
    Any,
    Counter(
        "sandloom_event_consumer_events_failed_total",
        "Generic event consumer handler failures (event left for redelivery)",
        ["consumer"],
    ),
)
consumer_subscription_attempts_total = cast(
    Any,
    Counter(
        "sandloom_consumer_subscription_attempts_total",
        "Side-effect/projection subscription attempts by outcome",
        ["consumer", "kind", "result"],
    ),
)
consumer_subscription_restarts_total = cast(
    Any,
    Counter(
        "sandloom_consumer_subscription_restarts_total",
        "Side-effect/projection subscriptions restarted after terminal failure",
        ["consumer", "kind"],
    ),
)
notifications_sent_total = cast(
    Any,
    Counter(
        "sandloom_notifications_sent_total",
        "Outbound notifications delivered by the notification capability",
        ["channel", "result"],
    ),
)

# Lifecycle task counters
startup_task_failures_total = cast(
    Any, Counter("sandloom_startup_task_failures_total", "Startup task failures")
)
startup_task_timeouts_total = cast(
    Any, Counter("sandloom_startup_task_timeouts_total", "Startup task timeouts")
)
startup_task_cancelled_total = cast(
    Any, Counter("sandloom_startup_task_cancelled_total", "Startup tasks cancelled")
)
shutdown_task_failures_total = cast(
    Any, Counter("sandloom_shutdown_task_failures_total", "Shutdown task failures")
)
shutdown_task_timeouts_total = cast(
    Any, Counter("sandloom_shutdown_task_timeouts_total", "Shutdown task timeouts")
)
shutdown_task_cancelled_total = cast(
    Any, Counter("sandloom_shutdown_task_cancelled_total", "Shutdown tasks cancelled")
)

# Module loader counters
module_load_failures_total = cast(
    Any,
    Counter(
        "sandloom_module_load_failures_total",
        "Module load failures (non-fatal + fatal)",
    ),
)

# DB driver fast paths (sandloom.core.sql_alchemy.driver_compat): a probe found the
# private asyncpg / SQLAlchemy internals changed → the plain path is used.
db_driver_fast_path_disabled_total = cast(
    Any,
    Counter(
        "sandloom_db_driver_fast_path_disabled_total",
        "DB fast paths disabled because a driver-internals probe failed",
        ["probe"],
    ),
)
# A pooled connection found dead (sandloom.core.sql_alchemy.dead_connection):
# replaced at checkout (``checkout``) or at the first message of a fresh
# transaction, retried once on a new connection (``begin`` / ``merged_begin``
# / ``tenant_sync`` / ``planner``).
db_dead_connection_replaced_total = cast(
    Any,
    Counter(
        "sandloom_db_dead_connection_replaced_total",
        "Dead pooled connections replaced before any statement of the transaction ran",
        ["phase"],
    ),
)
# non_tenant_statements() scope saw a statement outside its allowlist (prod:
# the tenant setting is synced anyway; dev/test: the statement is refused).
tenant_rls_non_tenant_violations_total = cast(
    Any,
    Counter(
        "sandloom_tenant_rls_non_tenant_violations_total",
        "Statements in a non_tenant_statements() scope outside its table allowlist",
    ),
)

# Startup duration
startup_duration_seconds = cast(
    Any,
    Gauge(
        "sandloom_startup_duration_seconds",
        "Application startup duration in seconds",
        multiprocess_mode="livemax",
    ),
)

# Configuration tiers (sandloom.core.config.tiers): 1 per active override at
# startup — ``kind`` weaker (a semantic setting off its profile value) or
# accepted_risk (an SANDLOOM_ACCEPTED_RISKS entry). Alert on any series in prod.
config_override_active = cast(
    Any,
    Gauge(
        "sandloom_config_override_active",
        "Active profile overrides (weaker, accepted_risk)",
        ["setting", "kind"],
        multiprocess_mode="livemax",
    ),
)

# Accepted risks (sandloom.core.config.accepted_risks): 1 per risk id this process
# accepts
# (SANDLOOM_ACCEPTED_RISKS), set at startup. Every series is
# a deliberate weakening — review each one; alert on new ones.
accepted_risk_active = cast(
    Any,
    Gauge(
        "sandloom_accepted_risk_active",
        "Weaker guarantees this process accepts (SANDLOOM_ACCEPTED_RISKS)",
        ["risk"],
        multiprocess_mode="livemax",
    ),
)

# Facade dynamic metrics gauge (label by metric name)
facade_metric = cast(
    Any,
    Gauge(
        "sandloom_facade_metric",
        "Values from CoreFacade internal metrics registry",
        ["name"],
        multiprocess_mode="livemax",
    ),
)

# Mapping from internal metric key -> Prometheus counter for lifecycle helpers
PROM_COUNTER_MAP = {
    "startup_task_failures": startup_task_failures_total,
    "startup_task_timeouts": startup_task_timeouts_total,
    "startup_task_cancelled": startup_task_cancelled_total,
    "shutdown_task_failures": shutdown_task_failures_total,
    "shutdown_task_timeouts": shutdown_task_timeouts_total,
    "shutdown_task_cancelled": shutdown_task_cancelled_total,
}

_FACADE_METRIC_NAMES: set[str] = set()
_FACADE_METRIC_NAMES_LOCK = threading.Lock()
_MAX_FACADE_METRIC_NAMES = 1024


def set_facade_metric(name: str, value: float) -> bool:
    """Bound label allocation across all facades in this process."""
    with _FACADE_METRIC_NAMES_LOCK:
        if name not in _FACADE_METRIC_NAMES:
            if len(_FACADE_METRIC_NAMES) >= _MAX_FACADE_METRIC_NAMES:
                return False
            _FACADE_METRIC_NAMES.add(name)
    facade_metric.labels(name=name).set(value)
    return True


def record_startup_duration(seconds: float) -> None:
    """Record total startup duration in gauge."""
    try:  # defensive; avoid surfacing metrics issues during boot
        startup_duration_seconds.set(seconds)
    except Exception:  # pragma: no cover  # noqa: BLE001, S110 - metrics must never break startup
        pass


def update_facade_metrics(  # pragma: no cover - best effort
    metrics: Mapping[str, Any],
) -> None:
    """Export a snapshot of the facade's internal metric registry to Prometheus.

    Accepts the metrics mapping (``core.list_metrics()``) rather than the facade
    itself, so this observability module carries no dependency back onto the
    kernel — the caller (lifecycle) owns the facade and passes the snapshot.
    """
    try:
        for key, val in metrics.items():
            if isinstance(val, int | float):
                set_facade_metric(key, float(val))
    except Exception:  # noqa: BLE001 - metrics must never break startup
        return


"""Internal-RPC client pool metrics (``sandloom.core.rpc_transport``).

Labels: ``transport`` (aiohttp|lean), ``peer`` (``host:port`` of a
SERVICE_BINDINGS base URL — bounded by the bindings), ``state`` (active|idle),
``reason`` (why a pooled connection closed: peer_close, idle_eviction,
connection_close, timeout, cancelled, oversize, error, shutdown).
"""

rpc_client_connections = cast(
    Any,
    Gauge(
        "sandloom_rpc_client_connections",
        "Internal RPC client connections per peer (active = in use or connecting)",
        ["transport", "peer", "state"],
        multiprocess_mode="livesum",
    ),
)
rpc_client_pool_waiting = cast(
    Any,
    Gauge(
        "sandloom_rpc_client_pool_waiting",
        "Internal RPC calls waiting for a free pooled connection",
        ["transport", "peer"],
        multiprocess_mode="livesum",
    ),
)
rpc_client_pool_wait_seconds = cast(
    Any,
    Histogram(
        "sandloom_rpc_client_pool_wait_seconds",
        "Time an internal RPC call waited for a free pooled connection",
        ["transport", "peer"],
        buckets=(0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
    ),
)
rpc_describe_failures_total = cast(
    Any,
    Counter(
        "sandloom_rpc_describe_failures_total",
        "Internal RPC service-description probes that failed",
        ["peer"],
    ),
)
rpc_client_connections_opened_total = cast(
    Any,
    Counter(
        "sandloom_rpc_client_connections_opened_total",
        "New TCP (+TLS) connections opened by the internal RPC client",
        ["transport", "peer"],
    ),
)
rpc_client_connections_closed_total = cast(
    Any,
    Counter(
        "sandloom_rpc_client_connections_closed_total",
        "Internal RPC client connections closed, by reason",
        ["transport", "peer", "reason"],
    ),
)
# RPC_TRANSPORT=aiohttp disabled by its internals probe
# (sandloom.core.rpc_transport_compat): the shared client falls back to httpx.
rpc_transport_fallback_total = cast(
    Any,
    Counter(
        "sandloom_rpc_transport_fallback_total",
        "Internal RPC transport fell back to httpx (aiohttp internals probe failed)",
        ["reason"],
    ),
)
# Response binding (sandloom.core.rpc): a response not bound to its own request
# (``reason``: sandloom.core.rpc_binding.REJECT_REASONS) was rejected — never
# returned to the caller; the connection pool was discarded.
rpc_response_binding_rejected_total = cast(
    Any,
    Counter(
        "sandloom_rpc_response_binding_rejected_total",
        "Internal RPC responses rejected because they are not bound to their request",
        ["reason"],
    ),
)
# Pair-MAC request authentication (sandloom.core.rpc_pair_mac, ADR-0004): a
# provider refused a request (``reason``: rpc_pair_mac.REJECT_REASONS, plus
# ``fail_open`` = shared nonce layer down outside prod, request accepted).
rpc_auth_rejected_total = cast(
    Any,
    Counter(
        "sandloom_rpc_auth_rejected_total",
        "Internal RPC requests refused by pair-MAC request authentication",
        ["reason"],
    ),
)
# Pair-MAC: an ACCEPTED request whose iat was more than half of
# RPC_AUTH_MAX_SKEW_SECONDS off this provider's clock (``caller`` = service
# id, bounded by the allowlist). Alert on any increase: replica / caller
# clocks must agree within the skew (docs/adr/0004, deploy/README.md).
rpc_auth_clock_skew_total = cast(
    Any,
    Counter(
        "sandloom_rpc_auth_clock_skew_total",
        "Accepted internal RPC requests with an issue time beyond half the window",
        ["caller"],
    ),
)
"""Operational metrics (review map B-07, H-09, C-07, H-14).

Alert thresholds: ``docs/operations/operator-reference.md`` → "Alerts".
Multiprocess (Gunicorn) rules: a gauge one process owns
(the relay leader, a group heartbeat) is
``livemostrecent``; a per-process quantity summed into the service total (pool
connections) is ``livesum``. Hot-path observations use children resolved once
(no ``labels()`` lookup per call).
"""

# -- B-07: delivery lag -------------------------------------------------------
# Age of the oldest undelivered, non-dead outbox row, per database. Set by the
# relay (one indexed ``min(emitted_at)`` every _AGE_INTERVAL_SECONDS, also while
# its broker circuit is open). 0 = nothing pending. Only the relay leader sets
# it: absent when no process in the service runs the relay.
outbox_oldest_pending_age_seconds = cast(
    Any,
    Gauge(
        "sandloom_outbox_oldest_pending_age_seconds",
        "Age of the oldest undelivered outbox row (0 = none pending)",
        ["database"],
        multiprocess_mode="livemostrecent",
    ),
)
# emitted_at (the outbox row's commit-side timestamp, carried exactly in the
# envelope) -> the consumer handler's successful completion. Includes outbox,
# relay, broker, redelivery and handler time.
_LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
    1800.0,
)
event_end_to_end_latency_seconds = cast(
    Any,
    Histogram(
        "sandloom_event_end_to_end_latency_seconds",
        "Event emitted_at to successful consumer handler completion",
        ["consumer"],
        buckets=_LATENCY_BUCKETS,
    ),
)
event_consumer_handler_duration_seconds = cast(
    Any,
    Histogram(
        "sandloom_event_consumer_handler_duration_seconds",
        "Duration of a successful consumer delivery (its UoW included)",
        ["consumer"],
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0, 30.0),
    ),
)

# -- C-07 / H-09: database pools ----------------------------------------------
# Sampled every few seconds by a per-process background task (zero cost on the
# checkout path); summed over the service's live processes. ``pool`` = main
# (requests / UoWs) or contract (in-process contract calls).
db_pool_connections = cast(
    Any,
    Gauge(
        "sandloom_db_pool_connections",
        "Pooled database connections by state (checked_out|idle)",
        ["database", "pool", "state"],
        multiprocess_mode="livesum",
    ),
)
db_pool_max_connections = cast(
    Any,
    Gauge(
        "sandloom_db_pool_max_connections",
        "Pool capacity: pool_size + max_overflow",
        ["database", "pool"],
        multiprocess_mode="livesum",
    ),
)
db_pool_overflow = cast(
    Any,
    Gauge(
        "sandloom_db_pool_overflow",
        "Open connections beyond pool_size",
        ["database", "pool"],
        multiprocess_mode="livesum",
    ),
)
db_pool_checkout_waiters = cast(
    Any,
    Gauge(
        "sandloom_db_pool_checkout_waiters",
        "Checkouts queued at the pool's fair gate (pool exhausted)",
        ["database", "pool"],
        multiprocess_mode="livesum",
    ),
)

# -- H-09: cache, revocation, rate limit, RPC --------------------------------
cache_operation_errors_total = cast(
    Any,
    Counter(
        "sandloom_cache_operation_errors_total",
        "Cache operations that raised (connection, timeout, server error)",
        ["instance", "operation"],
    ),
)
_FAST_BUCKETS = (0.0002, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5)
auth_revocation_check_duration_seconds = cast(
    Any,
    Histogram(
        "sandloom_auth_revocation_check_duration_seconds",
        "Revocation + block check of a request (memo hits excluded)",
        ["mode"],  # strict (the only mode: every check is live, ADR-0009)
        buckets=_FAST_BUCKETS,
    ),
)
rate_limit_rejected_total = cast(
    Any,
    Counter(
        "sandloom_rate_limit_rejected_total",
        "Requests refused with 429 by rate_limiter (route template)",
        ["route"],
    ),
)
rate_limit_unavailable_total = cast(
    Any,
    Counter(
        "sandloom_rate_limit_unavailable_total",
        "Rate-limit checks without a cache answer (outcome=open|closed)",
        ["outcome"],
    ),
)
rpc_client_request_duration_seconds = cast(
    Any,
    Histogram(
        "sandloom_rpc_client_request_duration_seconds",
        "Internal RPC call latency per peer and attempt outcome",
        ["peer", "outcome"],  # ok | timeout | error
        buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0),
    ),
)

# -- H-14: forwarding headers without TRUST_PROXY_HEADERS ---------------------
http_untrusted_forwarded_headers_total = cast(
    Any,
    Counter(
        "sandloom_http_untrusted_forwarded_headers_total",
        "Requests carrying X-Forwarded-For while TRUST_PROXY_HEADERS is off",
    ),
)
# Cyclic GC (sandloom.core.gc_tuning, gc.callbacks). A collection stops the whole
# event loop: a full one on a large heap is a tail-latency event for every
# in-flight request, invisible in CPU profiles. The histogram's _count is the
# collection count per generation. Labels: generation 0 / 1 / 2.
gc_pause_seconds = cast(
    Any,
    Histogram(
        "sandloom_gc_pause_seconds",
        "Elapsed time of one cyclic garbage collection",
        ["generation"],
        buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    ),
)
gc_collected_objects_total = cast(
    Any,
    Counter(
        "sandloom_gc_collected_objects_total",
        "Unreachable objects found by cyclic garbage collection",
        ["generation"],
    ),
)
gc_frozen_objects = cast(
    Any,
    Gauge(
        "sandloom_gc_frozen_objects",
        "Objects moved to the permanent generation by the post-startup gc.freeze()",
        multiprocess_mode="livemax",
    ),
)

# Sagas (sandloom.core.sagas, ADR-0008 §8). ``sandloom_saga_instances`` is refreshed by
# the timer sweep's holder from a bounded GROUP BY (one row per type x status);
# the step / park series are counted where the transition happens. Alert on
# ``sandloom_saga_parked_total``: a parked instance never moves without an operator.
saga_instances = cast(
    Any,
    Gauge(
        "sandloom_saga_instances",
        "Saga instances per type and status (sweep holder's last count)",
        ["saga_type", "status"],
        multiprocess_mode="livemax",
    ),
)
saga_step_duration_seconds = cast(
    Any,
    Histogram(
        "sandloom_saga_step_duration_seconds",
        "Duration of one saga step call (command or local step)",
        ["saga_type", "direction"],
    ),
)
saga_step_attempts_total = cast(
    Any,
    Counter(
        "sandloom_saga_step_attempts_total",
        "Saga step attempts by outcome class",
        ["saga_type", "outcome"],
    ),
)
saga_parked_total = cast(
    Any,
    Counter(
        "sandloom_saga_parked_total",
        "Saga instances parked (they wait for an operator)",
        ["saga_type", "reason"],
    ),
)
saga_started_total = cast(
    Any,
    Counter(
        "sandloom_saga_started_total",
        "Saga instances created by start_saga",
        ["saga_type"],
    ),
)
