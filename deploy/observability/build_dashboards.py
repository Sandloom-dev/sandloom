#!/usr/bin/env python3
# ruff: noqa: E501 - PromQL strings read better unwrapped
"""Generate the provisioned Grafana dashboards (``grafana/dashboards/*.json``).

The JSON files are build output, committed so Grafana can provision them with
no build step. Edit THIS file, then regenerate:

    python3 deploy/observability/build_dashboards.py          # write
    python3 deploy/observability/build_dashboards.py --check  # CI: fail if stale

Standard library only. Every metric a query names must exist in the kernel's
Prometheus registry — ``src/sandloom/core/tests/test_observability_bundle.py``
checks it, so a renamed metric fails CI instead of silently blanking a panel.

Conventions:
- ``$datasource`` / ``$job`` template variables; every query filters
  ``job=~"$job"``.
- A metric label named ``instance`` (``sandloom_cache_operation_errors_total``)
  collides with Prometheus' target label and is scraped as
  ``exported_instance`` (default ``honor_labels: false``).
- Under Gunicorn the app exports one aggregated view of its workers
  (``PROMETHEUS_MULTIPROC_DIR``): no per-worker series, no ``process_*``
  metrics. The Celery worker exports none.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent / "grafana" / "dashboards"
DS = {"type": "prometheus", "uid": "${datasource}"}
J = 'job=~"$job"'

# HTTP paths that are probes / scrapes, not user traffic.
NOT_USER = 'path!~"/health.*|/ready|/metrics|<unmatched>"'
READ = 'method=~"GET|HEAD"'
WRITE = 'method=~"POST|PUT|PATCH|DELETE"'


class Board:
    """Accumulates panels on a 24-column grid, two (or N) per row."""

    def __init__(self, uid: str, title: str, description: str, tags: list[str]):
        self.uid, self.title, self.description = uid, title, description
        self.tags = ["sandloom", *tags]
        self.panels: list[dict[str, Any]] = []
        self.extra_vars: list[dict[str, Any]] = []
        self._y = 0
        self._x = 0
        self._row_h = 0
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _place(self, w: int, h: int) -> dict[str, int]:
        if self._x + w > 24:
            self._y += self._row_h
            self._x, self._row_h = 0, 0
        pos = {"x": self._x, "y": self._y, "w": w, "h": h}
        self._x += w
        self._row_h = max(self._row_h, h)
        return pos

    def row(self, title: str) -> None:
        if self._x:
            self._y += self._row_h
            self._x, self._row_h = 0, 0
        self.panels.append(
            {
                "type": "row",
                "id": self._next_id(),
                "title": title,
                "collapsed": False,
                "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1},
                "panels": [],
            }
        )
        self._y += 1

    def _targets(self, queries: list[tuple[str, str]]) -> list[dict[str, Any]]:
        return [
            {
                "datasource": DS,
                "expr": expr,
                "legendFormat": legend,
                "refId": chr(ord("A") + i),
            }
            for i, (expr, legend) in enumerate(queries)
        ]

    def ts(
        self,
        title: str,
        queries: list[tuple[str, str]],
        *,
        unit: str = "short",
        desc: str = "",
        w: int = 12,
        h: int = 8,
        stack: bool = False,
        thresholds: list[tuple[float, str]] | None = None,
    ) -> None:
        custom: dict[str, Any] = {"lineWidth": 1, "fillOpacity": 10}
        if stack:
            custom["stacking"] = {"mode": "normal", "group": "A"}
        defaults: dict[str, Any] = {"unit": unit, "custom": custom}
        if thresholds:
            custom["thresholdsStyle"] = {"mode": "line"}
            defaults["thresholds"] = _thresholds(thresholds)
        self.panels.append(
            {
                "type": "timeseries",
                "id": self._next_id(),
                "title": title,
                "description": desc,
                "datasource": DS,
                "gridPos": self._place(w, h),
                "fieldConfig": {"defaults": defaults, "overrides": []},
                "options": {
                    "legend": {
                        "displayMode": "table",
                        "placement": "bottom",
                        "calcs": ["lastNotNull", "max"],
                    },
                    "tooltip": {"mode": "multi", "sort": "desc"},
                },
                "targets": self._targets(queries),
            }
        )

    def stat(
        self,
        title: str,
        expr: str,
        *,
        unit: str = "short",
        desc: str = "",
        w: int = 6,
        h: int = 4,
        thresholds: list[tuple[float, str]] | None = None,
    ) -> None:
        self.panels.append(
            {
                "type": "stat",
                "id": self._next_id(),
                "title": title,
                "description": desc,
                "datasource": DS,
                "gridPos": self._place(w, h),
                "fieldConfig": {
                    "defaults": {
                        "unit": unit,
                        "thresholds": _thresholds(thresholds or [(0, "green")]),
                        "color": {"mode": "thresholds"},
                    },
                    "overrides": [],
                },
                "options": {
                    "reduceOptions": {
                        "calcs": ["lastNotNull"],
                        "fields": "",
                        "values": False,
                    },
                    "colorMode": "background",
                    "graphMode": "area",
                },
                "targets": self._targets([(expr, "")]),
            }
        )

    def text(self, content: str, *, w: int = 24, h: int = 3) -> None:
        self.panels.append(
            {
                "type": "text",
                "id": self._next_id(),
                "title": "",
                "gridPos": self._place(w, h),
                "options": {"mode": "markdown", "content": content},
            }
        )

    def query_var(
        self, name: str, label: str, query: str, *, multi: bool = True
    ) -> None:
        self.extra_vars.append(
            {
                "name": name,
                "label": label,
                "type": "query",
                "datasource": DS,
                "query": {"query": query, "refId": f"{name}-var"},
                "definition": query,
                "refresh": 2,
                "multi": multi,
                "includeAll": True,
                "allValue": ".*",
                "current": {"selected": True, "text": "All", "value": "$__all"},
                "sort": 1,
            }
        )

    def render(self) -> dict[str, Any]:
        variables: list[dict[str, Any]] = [
            {
                "name": "datasource",
                "label": "Data source",
                "type": "datasource",
                "query": "prometheus",
                "current": {},
                "hide": 0,
            },
            {
                "name": "job",
                "label": "Job",
                "type": "query",
                "datasource": DS,
                "query": {
                    "query": "label_values(sandloom_http_requests_total, job)",
                    "refId": "job-var",
                },
                "definition": "label_values(sandloom_http_requests_total, job)",
                "refresh": 2,
                "multi": True,
                "includeAll": True,
                "allValue": ".*",
                "current": {"selected": True, "text": "All", "value": "$__all"},
                "sort": 1,
            },
            *self.extra_vars,
        ]
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "editable": True,
            "graphTooltip": 1,
            "schemaVersion": 39,
            "version": 1,
            "refresh": "30s",
            "time": {"from": "now-6h", "to": "now"},
            "timepicker": {},
            "timezone": "",
            "annotations": {"list": []},
            "links": [
                {
                    "type": "dashboards",
                    "tags": ["sandloom"],
                    "asDropdown": True,
                    "title": "Sandloom dashboards",
                    "includeVars": True,
                    "keepTime": True,
                }
            ],
            "templating": {"list": variables},
            "panels": self.panels,
        }


def _thresholds(steps: list[tuple[float, str]]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for i, (value, color) in enumerate(steps):
        out.append({"color": color, "value": None if i == 0 else value})
    return {"mode": "absolute", "steps": out}


def q99(metric: str, by: str, sel: str = J, q: str = "0.99") -> str:
    """``histogram_quantile`` of ``metric`` grouped by ``by`` ("" = overall)."""
    group = f"le, {by}" if by else "le"
    return (
        f"histogram_quantile({q}, sum by ({group}) "
        f"(rate({metric}_bucket{{{sel}}}[$__rate_interval])))"
    )


# --------------------------------------------------------------------------- #
# Dashboards
# --------------------------------------------------------------------------- #


def http_red() -> Board:
    b = Board(
        "sandloom-http-red",
        "Sandloom / HTTP RED",
        "Rate, errors and duration per route template (sandloom_http_*). Reads = "
        "GET/HEAD, writes = POST/PUT/PATCH/DELETE: the ADR-0003 operation "
        "class is a route declaration, not a metric label.",
        ["http", "red"],
    )
    b.query_var(
        "route",
        "Route",
        'label_values(sandloom_http_requests_total{job=~"$job"}, path)',
    )
    rs = f'{J}, path=~"$route", {NOT_USER}'
    b.row("Overview")
    b.stat(
        "Requests / s",
        f"sum(rate(sandloom_http_requests_total{{{rs}}}[$__rate_interval]))",
        unit="reqps",
    )
    b.stat(
        "5xx ratio",
        f'sum(rate(sandloom_http_requests_total{{{rs}, status=~"5.."}}[$__rate_interval])) '
        f"/ sum(rate(sandloom_http_requests_total{{{rs}}}[$__rate_interval]))",
        unit="percentunit",
        thresholds=[(0, "green"), (0.001, "yellow"), (0.01, "red")],
    )
    b.stat(
        "Read p99",
        q99("sandloom_http_request_duration_seconds", "", f"{rs}, {READ}"),
        unit="s",
        thresholds=[(0, "green"), (0.25, "yellow"), (0.5, "red")],
    )
    b.stat(
        "Write p99",
        q99("sandloom_http_request_duration_seconds", "", f"{rs}, {WRITE}"),
        unit="s",
        thresholds=[(0, "green"), (0.5, "yellow"), (1.0, "red")],
    )
    b.row("Rate")
    b.ts(
        "Requests / s by route",
        [
            (
                f"sum by (path) (rate(sandloom_http_requests_total{{{rs}}}[$__rate_interval]))",
                "{{path}}",
            )
        ],
        unit="reqps",
        stack=True,
    )
    b.ts(
        "Requests / s by status",
        [
            (
                f"sum by (status) (rate(sandloom_http_requests_total{{{rs}}}[$__rate_interval]))",
                "{{status}}",
            )
        ],
        unit="reqps",
        stack=True,
    )
    b.ts(
        "Reads vs writes",
        [
            (
                f"sum(rate(sandloom_http_requests_total{{{rs}, {READ}}}[$__rate_interval]))",
                "reads",
            ),
            (
                f"sum(rate(sandloom_http_requests_total{{{rs}, {WRITE}}}[$__rate_interval]))",
                "writes",
            ),
        ],
        unit="reqps",
    )
    b.ts(
        "In-flight requests",
        [(f"sum by (method) (sandloom_http_requests_in_progress{{{J}}})", "{{method}}")],
        desc="Summed over the live Gunicorn workers (livesum).",
    )
    b.row("Errors")
    b.ts(
        "5xx ratio by route",
        [
            (
                f'sum by (path) (rate(sandloom_http_requests_total{{{rs}, status=~"5.."}}[$__rate_interval])) '
                f"/ sum by (path) (rate(sandloom_http_requests_total{{{rs}}}[$__rate_interval]))",
                "{{path}}",
            )
        ],
        unit="percentunit",
    )
    b.ts(
        "4xx / s by route and status",
        [
            (
                f'sum by (path, status) (rate(sandloom_http_requests_total{{{rs}, status=~"4.."}}[$__rate_interval]))',
                "{{status}} {{path}}",
            )
        ],
        unit="reqps",
        desc="401/403/404/409/422 are usually client behaviour; 429 is the rate limiter.",
    )
    b.ts(
        "429 / s by route (rate limiter)",
        [
            (
                f"sum by (route) (rate(sandloom_rate_limit_rejected_total{{{J}}}[$__rate_interval]))",
                "{{route}}",
            )
        ],
        unit="reqps",
    )
    b.ts(
        "X-Forwarded-For while untrusted",
        [
            (
                f"sum(rate(sandloom_http_untrusted_forwarded_headers_total{{{J}}}[$__rate_interval]))",
                "requests/s",
            )
        ],
        unit="reqps",
        desc="Non-zero: a proxy forwards XFF but TRUST_PROXY_HEADERS is off.",
    )
    b.row("Duration")
    for q, name in (("0.5", "p50"), ("0.95", "p95"), ("0.99", "p99")):
        b.ts(
            f"{name} by route",
            [(q99("sandloom_http_request_duration_seconds", "path", rs, q), "{{path}}")],
            unit="s",
            w=8,
        )
    b.ts(
        "Read latency quantiles",
        [
            (q99("sandloom_http_request_duration_seconds", "", f"{rs}, {READ}", q), f"p{q}")
            for q in ("0.5", "0.95", "0.99")
        ],
        unit="s",
    )
    b.ts(
        "Write latency quantiles",
        [
            (q99("sandloom_http_request_duration_seconds", "", f"{rs}, {WRITE}", q), f"p{q}")
            for q in ("0.5", "0.95", "0.99")
        ],
        unit="s",
    )
    return b


def outbox() -> Board:
    b = Board(
        "sandloom-outbox-relay",
        "Sandloom / Outbox & relay",
        "Transactional outbox → broker. The age gauge is exported only by the "
        "process running the relay (OUTBOX_RELAY_ENABLED, one Gunicorn leader).",
        ["events", "outbox"],
    )
    b.row("Backlog")
    b.stat(
        "Oldest pending (max)",
        f"max(sandloom_outbox_oldest_pending_age_seconds{{{J}}})",
        unit="s",
        thresholds=[(0, "green"), (10, "yellow"), (60, "red")],
    )
    b.stat(
        "Broker circuit",
        f"max(outbox_relay_circuit_open{{{J}}})",
        desc="1 = broker unavailable, relay cooling down.",
        thresholds=[(0, "green"), (1, "red")],
    )
    b.stat(
        "Since last relay batch",
        f"time() - max(outbox_relay_last_run_timestamp{{{J}}})",
        unit="s",
        thresholds=[(0, "green"), (60, "yellow"), (300, "red")],
    )
    b.stat(
        "Dead-lettered (24h)",
        f"sum(increase(outbox_dead_total{{{J}}}[24h]))",
        thresholds=[(0, "green"), (1, "red")],
    )
    b.ts(
        "Oldest pending outbox row age",
        [
            (
                f"max by (database) (sandloom_outbox_oldest_pending_age_seconds{{{J}}})",
                "{{database}}",
            )
        ],
        unit="s",
        thresholds=[(0, "green"), (10, "yellow"), (60, "red")],
    )
    b.ts(
        "Relay throughput",
        [
            (
                f"sum(rate(outbox_relay_processed_total{{{J}}}[$__rate_interval]))",
                "delivered/s",
            ),
            (
                f"sum(rate(outbox_relay_failed_total{{{J}}}[$__rate_interval]))",
                "failed attempts/s",
            ),
            (
                f"sum(rate(outbox_relay_batches_total{{{J}}}[$__rate_interval]))",
                "batches/s",
            ),
        ],
        unit="ops",
    )
    b.ts("Relay batch size", [(f"max(outbox_relay_inflight_messages{{{J}}})", "rows")])
    b.ts(
        "Relay trouble",
        [
            (
                f"sum(increase(outbox_dead_total{{{J}}}[$__rate_interval]))",
                "dead-lettered",
            ),
            (
                f"sum(increase(outbox_relay_broker_unavailable_total{{{J}}}[$__rate_interval]))",
                "broker unavailable",
            ),
            (
                f"sum by (database) (increase(outbox_relay_database_errors_total{{{J}}}[$__rate_interval]))",
                "db errors {{database}}",
            ),
        ],
    )
    b.row("Producers")
    b.ts(
        "Published / s by channel",
        [
            (
                f"sum by (channel) (rate(sandloom_events_published_total{{{J}}}[$__rate_interval]))",
                "{{channel}}",
            )
        ],
        unit="ops",
        stack=True,
    )
    b.ts(
        "Publish problems",
        [
            (
                f"sum by (channel) (rate(sandloom_events_publish_failures_total{{{J}}}[$__rate_interval]))",
                "failure {{channel}}",
            ),
            (
                f"sum by (channel) (rate(sandloom_event_payload_rejected_total{{{J}}}[$__rate_interval]))",
                "payload too large {{channel}}",
            ),
            (
                f"sum(rate(outbox_post_commit_dropped_total{{{J}}}[$__rate_interval]))",
                "post-commit left to relay",
            ),
        ],
        unit="ops",
        desc="post-commit left to relay: the dispatcher queue was full or the broker "
        "failing; the relay delivers those rows (latency, not loss).",
    )
    b.ts(
        "Retention purge",
        [
            (
                f"sum(increase(outbox_purged_total{{{J}}}[$__rate_interval]))",
                "rows purged",
            )
        ],
    )
    b.ts(
        "Schema validation",
        [
            (
                f"sum(rate(sandloom_schema_validation_failures_total{{{J}}}[$__rate_interval]))",
                "failures/s",
            ),
            (
                f"sum(rate(sandloom_schema_validation_success_total{{{J}}}[$__rate_interval]))",
                "ok/s",
            ),
        ],
        unit="ops",
    )
    return b


def consumers() -> Board:
    b = Board(
        "sandloom-consumers-streams",
        "Sandloom / Consumers & streams",
        "Durable consumers on Redis Streams: group lag, end-to-end latency, "
        "failures and dead letters. Group gauges come from the 30 s heartbeat.",
        ["events", "consumers"],
    )
    b.query_var(
        "consumer",
        "Consumer",
        'label_values(sandloom_event_end_to_end_latency_seconds_count{job=~"$job"}, consumer)',
    )
    c = f'{J}, consumer=~"$consumer"'
    b.row("Lag")
    b.ts(
        "Oldest unacked entry age by group",
        [
            (
                f"max by (stream, group) (sandloom_streams_group_oldest_unacked_age_seconds{{{J}}})",
                "{{group}} @ {{stream}}",
            )
        ],
        unit="s",
        thresholds=[(0, "green"), (60, "yellow"), (300, "red")],
    )
    b.ts(
        "Undelivered entries (lag) by group",
        [
            (
                f"max by (stream, group) (sandloom_streams_group_lag{{{J}}})",
                "{{group}} @ {{stream}}",
            )
        ],
    )
    b.ts(
        "Delivered, unacked (pending) by group",
        [
            (
                f"max by (stream, group) (sandloom_streams_group_pending{{{J}}})",
                "{{group}} @ {{stream}}",
            )
        ],
    )
    b.ts(
        "End-to-end latency p99 by consumer",
        [(q99("sandloom_event_end_to_end_latency_seconds", "consumer", c), "{{consumer}}")],
        unit="s",
        desc="Outbox emitted_at → successful handler commit; includes relay, "
        "broker and redelivery time (and producer/consumer clock skew).",
    )
    b.ts(
        "End-to-end latency p50 by consumer",
        [
            (
                q99("sandloom_event_end_to_end_latency_seconds", "consumer", c, "0.5"),
                "{{consumer}}",
            )
        ],
        unit="s",
    )
    b.ts(
        "Handler duration p99 by consumer",
        [
            (
                q99("sandloom_event_consumer_handler_duration_seconds", "consumer", c),
                "{{consumer}}",
            )
        ],
        unit="s",
        desc="One successful delivery including its UoW commit.",
    )
    b.row("Throughput & failures")
    b.ts(
        "Processed / s by consumer and result",
        [
            (
                f"sum by (consumer, result) (rate(sandloom_event_consumer_events_processed_total{{{c}}}[$__rate_interval]))",
                "{{consumer}} {{result}}",
            )
        ],
        unit="ops",
        stack=True,
    )
    b.ts(
        "Handler failures / s by consumer",
        [
            (
                f"sum by (consumer) (rate(sandloom_event_consumer_events_failed_total{{{c}}}[$__rate_interval]))",
                "{{consumer}}",
            )
        ],
        unit="ops",
    )
    b.ts(
        "Projection events / s",
        [
            (
                f"sum by (projection, result) (rate(sandloom_projection_events_processed_total{{{J}}}[$__rate_interval]))",
                "{{projection}} {{result}}",
            ),
            (
                f"sum by (projection) (rate(sandloom_projection_events_failed_total{{{J}}}[$__rate_interval]))",
                "{{projection}} failed",
            ),
        ],
        unit="ops",
    )
    b.ts(
        "Deliveries by status (bus)",
        [
            (
                f"sum by (status) (rate(sandloom_events_delivered_total{{{J}}}[$__rate_interval]))",
                "{{status}}",
            )
        ],
        unit="ops",
    )
    b.ts(
        "Subscriptions",
        [
            (
                f'sum by (consumer) (increase(sandloom_consumer_subscription_attempts_total{{{J}, result!="ok"}}[$__rate_interval]))',
                "attempt not ok {{consumer}}",
            ),
            (
                f"sum by (consumer) (increase(sandloom_consumer_subscription_restarts_total{{{J}}}[$__rate_interval]))",
                "restart {{consumer}}",
            ),
        ],
        desc="A required consumer that keeps restarting fails /ready.",
    )
    b.ts(
        "Slow callbacks / malformed frames",
        [
            (
                f"sum by (channel) (increase(sandloom_slow_event_callbacks_total{{{J}}}[$__rate_interval]))",
                "slow {{channel}}",
            ),
            (
                f"sum by (channel) (increase(sandloom_events_malformed_total{{{J}}}[$__rate_interval]))",
                "malformed {{channel}}",
            ),
        ],
    )
    b.row("Streams transport")
    b.ts(
        "Dead-lettered / reclaimed",
        [
            (
                f"sum by (stream) (increase(sandloom_streams_messages_dlq_total{{{J}}}[$__rate_interval]))",
                "dlq {{stream}}",
            ),
            (
                f"sum by (stream) (increase(sandloom_streams_dlq_append_failures_total{{{J}}}[$__rate_interval]))",
                "dlq append failed {{stream}}",
            ),
            (
                f"sum by (stream) (increase(sandloom_streams_messages_claimed_total{{{J}}}[$__rate_interval]))",
                "reclaimed {{stream}}",
            ),
        ],
    )
    b.ts(
        "Operator replays / group recreation",
        [
            (
                f"sum by (stream) (increase(streams_dlq_replayed_total{{{J}}}[$__rate_interval]))",
                "replayed {{stream}}",
            ),
            (
                f"sum by (stream) (increase(streams_targeted_replay_skipped_total{{{J}}}[$__rate_interval]))",
                "replay skipped {{stream}}",
            ),
            (
                f"sum by (stream) (increase(streams_group_recreated_total{{{J}}}[$__rate_interval]))",
                "group recreated {{stream}}",
            ),
            (
                f"sum by (stream) (increase(streams_group_recreated_at_tail_total{{{J}}}[$__rate_interval]))",
                "recreated at tail (gap!) {{stream}}",
            ),
        ],
    )
    b.ts(
        "Published without consumers / version starvation",
        [
            (
                f"sum by (channel) (increase(sandloom_events_published_without_consumers_total{{{J}}}[$__rate_interval]))",
                "no consumers {{channel}}",
            ),
            (
                f"sum by (channel, starved) (increase(sandloom_events_version_starvation_total{{{J}}}[$__rate_interval]))",
                "{{channel}} starves {{starved}}",
            ),
        ],
    )
    b.ts(
        "Event-bus reconnects",
        [
            (
                f"sum(increase(sandloom_redis_reconnect_attempts_total{{{J}}}[$__rate_interval]))",
                "reconnect attempts",
            )
        ],
    )
    return b


def security() -> Board:
    b = Board(
        "sandloom-security",
        "Sandloom / Revocation & security",
        "Token revocation / block checks, the security cache, rate limiting and "
        "idempotency. Most panels should be flat zero.",
        ["security", "auth"],
    )
    b.row("Revocation")
    b.stat(
        "Revocations pending (max age)",
        f"max(auth_revocation_pending_seconds{{{J}}})",
        unit="s",
        thresholds=[(0, "green"), (60, "yellow"), (300, "red")],
    )
    b.stat(
        "Deferred (1h)",
        f"sum(increase(auth_revocation_deferred_total{{{J}}}[1h]))",
        thresholds=[(0, "green"), (1, "yellow")],
    )
    b.stat(
        "Malformed (24h)",
        f"sum(increase(auth_revocation_malformed_total{{{J}}}[24h]))",
        thresholds=[(0, "green"), (1, "red")],
    )
    b.stat(
        "Immediate refused (1h)",
        f"sum(increase(auth_revocation_immediate_refused_total{{{J}}}[1h]))",
        thresholds=[(0, "green"), (1, "red")],
    )
    b.ts(
        "Revocation check latency",
        [
            (
                q99("sandloom_auth_revocation_check_duration_seconds", "mode", J, q),
                f"p{q} {{{{mode}}}}",
            )
            for q in ("0.5", "0.99")
        ],
        unit="s",
        thresholds=[(0, "green"), (0.01, "red")],
        desc="Every authenticated request pays it (strict = live read, budgeted = "
        "revocation_freshness). Memo hits are not timed.",
    )
    b.ts(
        "Revocation checks / s",
        [
            (
                f"sum by (mode) (rate(sandloom_auth_revocation_check_duration_seconds_count{{{J}}}[$__rate_interval]))",
                "{{mode}}",
            )
        ],
        unit="ops",
    )
    b.ts(
        "Revocation write path",
        [
            (
                f"sum by (path) (increase(auth_revocation_applied_total{{{J}}}[$__rate_interval]))",
                "applied {{path}}",
            ),
            (
                f"sum(increase(auth_revocation_deferred_total{{{J}}}[$__rate_interval]))",
                "deferred",
            ),
            (
                f"sum(increase(auth_revocation_applier_failures_total{{{J}}}[$__rate_interval]))",
                "applier failures",
            ),
            (
                f"sum(increase(auth_revocation_immediate_refused_total{{{J}}}[$__rate_interval]))",
                "immediate refused (503)",
            ),
            (
                f"sum(increase(auth_revocation_corrupt_mark_total{{{J}}}[$__rate_interval]))",
                "corrupt mark",
            ),
            (
                f"sum(increase(auth_revocation_malformed_total{{{J}}}[$__rate_interval]))",
                "malformed (DLQ)",
            ),
        ],
    )
    b.ts(
        "Oldest pending durable revocation",
        [(f"max(auth_revocation_pending_seconds{{{J}}})", "seconds")],
        unit="s",
        thresholds=[(0, "green"), (60, "yellow"), (300, "red")],
    )
    b.row("Cache & rate limiting")
    b.ts(
        "Cache operation errors / s",
        [
            (
                f"sum by (exported_instance, operation) (rate(sandloom_cache_operation_errors_total{{{J}}}[$__rate_interval]))",
                "{{exported_instance}} {{operation}}",
            )
        ],
        unit="ops",
        desc="The metric's own `instance` label (cache instance name) is scraped as "
        "`exported_instance`.",
    )
    b.ts(
        "Rate limiter without a cache answer",
        [
            (
                f"sum by (outcome) (rate(sandloom_rate_limit_unavailable_total{{{J}}}[$__rate_interval]))",
                "{{outcome}}",
            )
        ],
        unit="ops",
        desc="open = allowed unlimited, closed = answered 503.",
    )
    b.ts(
        "429 / s by route",
        [
            (
                f"sum by (route) (rate(sandloom_rate_limit_rejected_total{{{J}}}[$__rate_interval]))",
                "{{route}}",
            )
        ],
        unit="ops",
    )
    b.ts(
        "Idempotency / password / RLS",
        [
            (
                f"sum(increase(sandloom_idempotency_terminal_persist_failures_total{{{J}}}[$__rate_interval]))",
                "idempotency terminal persist failed",
            ),
            (
                f"sum by (outcome) (increase(sandloom_password_rehash_total{{{J}}}[$__rate_interval]))",
                "password rehash {{outcome}}",
            ),
            (
                f"sum(increase(sandloom_tenant_rls_non_tenant_violations_total{{{J}}}[$__rate_interval]))",
                "non-tenant statement violations",
            ),
        ],
    )
    b.ts(
        "Login / refresh traffic",
        [
            (
                f'sum by (path, status) (rate(sandloom_http_requests_total{{{J}, path=~"/auth/login|/auth/refresh|/auth/2fa/.*|/auth/sso/.*"}}[$__rate_interval]))',
                "{{status}} {{path}}",
            )
        ],
        unit="reqps",
        w=24,
        desc="401 bursts on /auth/login with 429s: credential stuffing.",
    )
    return b


def db_pools() -> Board:
    b = Board(
        "sandloom-db-pools",
        "Sandloom / Database pools",
        "SQLAlchemy pools, sampled every 5 s per process and summed over the "
        "service's workers. pool = main (requests/UoWs) or contract (in-process "
        "contract calls).",
        ["database"],
    )
    b.query_var(
        "database",
        "Database",
        'label_values(sandloom_db_pool_max_connections{job=~"$job"}, database)',
    )
    d = f'{J}, database=~"$database"'
    b.row("Utilisation")
    b.ts(
        "Checked-out / capacity",
        [
            (
                f'sum by (database, pool) (sandloom_db_pool_connections{{{d}, state="checked_out"}}) '
                f"/ sum by (database, pool) (sandloom_db_pool_max_connections{{{d}}})",
                "{{database}}/{{pool}}",
            )
        ],
        unit="percentunit",
        thresholds=[(0, "green"), (0.9, "red")],
    )
    b.ts(
        "Checkout waiters (pool exhausted)",
        [
            (
                f"sum by (database, pool) (sandloom_db_pool_checkout_waiters{{{d}}})",
                "{{database}}/{{pool}}",
            )
        ],
        desc="A 5 s sample: short bursts show in request latency first.",
    )
    b.ts(
        "Connections by state",
        [
            (
                f"sum by (database, pool, state) (sandloom_db_pool_connections{{{d}}})",
                "{{database}}/{{pool}} {{state}}",
            ),
            (
                f"sum by (database, pool) (sandloom_db_pool_max_connections{{{d}}})",
                "{{database}}/{{pool}} max",
            ),
        ],
    )
    b.ts(
        "Overflow connections",
        [
            (
                f"sum by (database, pool) (sandloom_db_pool_overflow{{{d}}})",
                "{{database}}/{{pool}}",
            )
        ],
    )
    b.row("Driver health")
    b.ts(
        "Dead connections replaced",
        [
            (
                f"sum by (phase) (increase(sandloom_db_dead_connection_replaced_total{{{J}}}[$__rate_interval]))",
                "{{phase}}",
            )
        ],
        desc="Bursts follow a PostgreSQL restart / failover; steady growth = a "
        "proxy or firewall closing idle connections.",
    )
    b.ts(
        "Driver fast paths disabled",
        [
            (
                f"sum by (probe) (increase(sandloom_db_driver_fast_path_disabled_total{{{J}}}[$__rate_interval]))",
                "{{probe}}",
            )
        ],
        desc="An asyncpg / SQLAlchemy internals probe failed after an upgrade "
        "(OPERATIONS.md → Upgrading asyncpg / SQLAlchemy).",
    )
    return b


def rpc() -> Board:
    b = Board(
        "sandloom-rpc",
        "Sandloom / Internal RPC",
        "Contract calls to other services (SERVICE_BINDINGS). Empty in the "
        "monolith: every contract call is in-process.",
        ["rpc"],
    )
    b.query_var(
        "peer",
        "Peer",
        'label_values(sandloom_rpc_client_request_duration_seconds_count{job=~"$job"}, peer)',
    )
    p = f'{J}, peer=~"$peer"'
    b.row("Calls")
    b.ts(
        "Latency p99 by peer (ok)",
        [
            (
                q99(
                    "sandloom_rpc_client_request_duration_seconds",
                    "peer",
                    f'{p}, outcome="ok"',
                ),
                "{{peer}}",
            )
        ],
        unit="s",
        thresholds=[(0, "green"), (0.25, "red")],
    )
    b.ts(
        "Attempts / s by peer and outcome",
        [
            (
                f"sum by (peer, outcome) (rate(sandloom_rpc_client_request_duration_seconds_count{{{p}}}[$__rate_interval]))",
                "{{peer}} {{outcome}}",
            )
        ],
        unit="ops",
        desc="timeout: the call may have committed at the provider — retry with "
        "the same operation key.",
    )
    b.ts(
        "Pool wait p99",
        [(q99("sandloom_rpc_client_pool_wait_seconds", "peer", p), "{{peer}}")],
        unit="s",
    )
    b.ts(
        "Calls waiting for a pooled connection",
        [(f"sum by (peer) (sandloom_rpc_client_pool_waiting{{{p}}})", "{{peer}}")],
    )
    b.ts(
        "Connections opened / closed",
        [
            (
                f"sum by (peer) (rate(sandloom_rpc_client_connections_opened_total{{{p}}}[$__rate_interval]))",
                "opened {{peer}}",
            ),
            (
                f"sum by (peer, reason) (rate(sandloom_rpc_client_connections_closed_total{{{p}}}[$__rate_interval]))",
                "closed {{peer}} {{reason}}",
            ),
        ],
        unit="ops",
        desc="Opened ≈ calls means keep-alive is not working (pool churn).",
    )
    b.ts(
        "Pooled connections",
        [
            (
                f"sum by (peer, state) (sandloom_rpc_client_connections{{{p}}})",
                "{{peer}} {{state}}",
            )
        ],
        desc="Set through a gauge callback: absent under Gunicorn multiprocess "
        "mode (single-process / Uvicorn only).",
    )
    b.row("Authentication & binding")
    b.ts(
        "Provider-side auth rejections",
        [
            (
                f"sum by (reason) (increase(sandloom_rpc_auth_rejected_total{{{J}}}[$__rate_interval]))",
                "{{reason}}",
            ),
            (
                f"sum by (caller) (increase(sandloom_rpc_auth_clock_skew_total{{{J}}}[$__rate_interval]))",
                "clock skew from {{caller}}",
            ),
        ],
    )
    b.ts(
        "Response binding",
        [
            (
                f"sum by (reason) (increase(sandloom_rpc_response_binding_rejected_total{{{J}}}[$__rate_interval]))",
                "rejected {{reason}}",
            ),
        ],
        desc="Responses refused because they are not keyed to their request "
        "by the pinned provider; any increase is a misrouted, replayed or "
        "forged response (or a provider without its X25519 key).",
    )
    b.ts(
        "Transport fallbacks / describe failures",
        [
            (
                f"sum by (reason) (increase(sandloom_rpc_transport_fallback_total{{{J}}}[$__rate_interval]))",
                "fallback to httpx {{reason}}",
            ),
            (
                f"sum by (peer) (increase(sandloom_rpc_describe_failures_total{{{J}}}[$__rate_interval]))",
                "describe failed {{peer}}",
            ),
        ],
        w=24,
    )
    return b


def runtime() -> Board:
    b = Board(
        "sandloom-runtime",
        "Sandloom / Runtime & GC",
        "Cyclic GC pauses (stop the event loop), lifecycle tasks and module "
        "loading. Celery workers export no metrics yet (review map H-09).",
        ["runtime", "gc"],
    )
    b.row("Garbage collector")
    b.ts(
        "GC pause p99 by generation",
        [(q99("sandloom_gc_pause_seconds", "generation"), "gen {{generation}}")],
        unit="s",
        desc="Expect generation 2 well under 10 ms after the post-startup freeze.",
    )
    b.ts(
        "Collections / s by generation",
        [
            (
                f"sum by (generation) (rate(sandloom_gc_pause_seconds_count{{{J}}}[$__rate_interval]))",
                "gen {{generation}}",
            )
        ],
        unit="ops",
        desc="Summed over workers. Above ~10/s per worker for gen 0: try "
        "GC_THRESHOLDS=10000,10,100 (OPERATIONS.md → Garbage collector tuning).",
    )
    b.ts(
        "Time in GC (share of one core)",
        [
            (
                f"sum by (generation) (rate(sandloom_gc_pause_seconds_sum{{{J}}}[$__rate_interval]))",
                "gen {{generation}}",
            )
        ],
        unit="percentunit",
        stack=True,
    )
    b.ts(
        "Collected objects / s",
        [
            (
                f"sum by (generation) (rate(sandloom_gc_collected_objects_total{{{J}}}[$__rate_interval]))",
                "gen {{generation}}",
            )
        ],
        unit="ops",
    )
    b.ts(
        "Frozen objects (per worker, max)",
        [(f"max(sandloom_gc_frozen_objects{{{J}}})", "frozen")],
    )
    b.ts(
        "Startup duration",
        [(f"max(sandloom_startup_duration_seconds{{{J}}})", "seconds")],
        unit="s",
    )
    b.row("Lifecycle")
    b.ts(
        "Startup / shutdown task problems",
        [
            (
                f"sum(increase(sandloom_startup_task_failures_total{{{J}}}[$__rate_interval]))",
                "startup failed",
            ),
            (
                f"sum(increase(sandloom_startup_task_timeouts_total{{{J}}}[$__rate_interval]))",
                "startup timeout",
            ),
            (
                f"sum(increase(sandloom_startup_task_cancelled_total{{{J}}}[$__rate_interval]))",
                "startup cancelled",
            ),
            (
                f"sum(increase(sandloom_shutdown_task_failures_total{{{J}}}[$__rate_interval]))",
                "shutdown failed",
            ),
            (
                f"sum(increase(sandloom_shutdown_task_timeouts_total{{{J}}}[$__rate_interval]))",
                "shutdown timeout",
            ),
            (
                f"sum(increase(sandloom_shutdown_task_cancelled_total{{{J}}}[$__rate_interval]))",
                "shutdown cancelled",
            ),
        ],
    )
    b.ts(
        "Module load failures / notifications",
        [
            (
                f"sum(increase(sandloom_module_load_failures_total{{{J}}}[$__rate_interval]))",
                "module load failures",
            ),
            (
                f"sum by (channel, result) (increase(sandloom_notifications_sent_total{{{J}}}[$__rate_interval]))",
                "notification {{channel}} {{result}}",
            ),
        ],
    )
    b.ts(
        "Scrape health",
        [(f"up{{{J}}}", "{{instance}}")],
        w=24,
        desc="0 = Prometheus cannot scrape /metrics (down, or METRICS_TOKEN wrong).",
    )
    return b


def slo() -> Board:
    b = Board(
        "sandloom-slo",
        "Sandloom / SLOs",
        "Example SLOs from docs/reference/slo.md on the recording rules in "
        "prometheus/rules/sandloom-slo.yml. Targets are starting points, not promises.",
        ["slo"],
    )
    b.text(
        "Targets on this board are the **examples** from `docs/reference/slo.md` "
        "(2 vCPU monolith, Oracle qualification 2026-10-22). Change the targets in "
        "`deploy/observability/prometheus/rules/sandloom-slo.yml` and here together.",
        h=2,
    )
    slos = [
        ("API read availability", "sandloom:sli_read_errors:ratio_rate", 0.999),
        ("API write availability", "sandloom:sli_write_errors:ratio_rate", 0.999),
        ("API read latency (< 250 ms)", "sandloom:sli_read_slow:ratio_rate", 0.99),
        ("API write latency (< 500 ms)", "sandloom:sli_write_slow:ratio_rate", 0.99),
        ("Login availability", "sandloom:sli_login_errors:ratio_rate", 0.999),
        ("Login latency (< 2.5 s)", "sandloom:sli_login_slow:ratio_rate", 0.99),
        ("Event delivery (< 5 s end-to-end)", "sandloom:sli_event_slow:ratio_rate", 0.99),
    ]
    b.row("Error budget remaining (30 days)")
    for title, rec, target in slos:
        budget = 1 - target
        b.stat(
            title,
            f"1 - (avg_over_time({rec}1h{{{J}}}[30d]) / {budget:g})",
            unit="percentunit",
            w=6,
            thresholds=[(0, "red"), (0.25, "yellow"), (0.5, "green")],
            desc=f"Target {target:.1%}. 1 = untouched budget, ≤ 0 = SLO missed over "
            "30 days. Averages hourly ratios (traffic-unweighted).",
        )
    b.stat(
        "Outbox age within 10 s (30 days)",
        f"avg_over_time(sandloom:sli_outbox_fresh:bool{{{J}}}[30d])",
        unit="percentunit",
        w=6,
        thresholds=[(0, "red"), (0.99, "yellow"), (0.999, "green")],
        desc="Share of evaluations with the oldest pending outbox row < 10 s. "
        "Target 99.9 %.",
    )
    b.row("Burn rate (1 = spending exactly the budget)")
    for title, rec, target in slos:
        budget = 1 - target
        b.ts(
            f"{title} burn rate",
            [
                (f"{rec}5m{{{J}}} / {budget:g}", "5m"),
                (f"{rec}1h{{{J}}} / {budget:g}", "1h"),
                (f"{rec}6h{{{J}}} / {budget:g}", "6h"),
            ],
            w=8,
            thresholds=[(0, "green"), (6, "yellow"), (14.4, "red")],
        )
    b.ts(
        "Consumer group oldest unacked age",
        [
            (
                f"max by (stream, group) (sandloom_streams_group_oldest_unacked_age_seconds{{{J}}})",
                "{{group}}",
            )
        ],
        unit="s",
        w=8,
        thresholds=[(0, "green"), (60, "yellow"), (300, "red")],
    )
    return b


DASHBOARDS = {
    "sandloom-http-red.json": http_red,
    "sandloom-outbox-relay.json": outbox,
    "sandloom-consumers-streams.json": consumers,
    "sandloom-security.json": security,
    "sandloom-db-pools.json": db_pools,
    "sandloom-rpc.json": rpc,
    "sandloom-runtime.json": runtime,
    "sandloom-slo.json": slo,
}


def render_all() -> dict[str, str]:
    return {
        name: json.dumps(build().render(), indent=2, sort_keys=True) + "\n"
        for name, build in DASHBOARDS.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if a committed dashboard differs from the generator",
    )
    args = parser.parse_args(argv)
    rendered = render_all()
    stale = [
        n
        for n, text in rendered.items()
        if not (OUT / n).is_file() or (OUT / n).read_text() != text
    ]
    extra = sorted(p.name for p in OUT.glob("*.json") if p.name not in rendered)
    if args.check:
        for name in stale:
            print(f"stale: {OUT / name}", file=sys.stderr)
        for name in extra:
            print(f"not generated: {OUT / name}", file=sys.stderr)
        if stale or extra:
            print(
                "run: python3 deploy/observability/build_dashboards.py", file=sys.stderr
            )
            return 1
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in rendered.items():
        (OUT / name).write_text(text)
    for name in extra:
        (OUT / name).unlink()
    print(f"wrote {len(rendered)} dashboards to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
