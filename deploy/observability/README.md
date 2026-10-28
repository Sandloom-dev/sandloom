# Sandloom observability bundle

Ready-to-use Grafana dashboards, Prometheus alert rules and SLI recording
rules for the metrics the Sandloom kernel exports, plus a compose overlay that runs
Prometheus + Grafana next to the monolith profile.

```
deploy/docker-compose.observability.yml   # overlay (profile: observability)
deploy/observability/
  build_dashboards.py                     # generates grafana/dashboards/*.json
  check.sh                                # dashboards + promtool config/rules/tests
  prometheus/prometheus.yml               # scrape config (METRICS_TOKEN header)
  prometheus/rules/sandloom-alerts.yml         # alert rules — thresholds live HERE
  prometheus/rules/sandloom-slo.yml            # SLI recording rules + burn-rate alerts
  prometheus/tests/sandloom-rules.test.yml     # promtool rule unit tests
  grafana/provisioning/                   # datasource + dashboard provider
  grafana/dashboards/*.json               # generated, provisioned into folder "Sandloom"
```

## Run it with the monolith

```bash
export METRICS_TOKEN=$(openssl rand -hex 32) GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 12)
docker compose -f deploy/compose/monolith.yml -f deploy/docker-compose.observability.yml --profile observability up -d
```

(plus the monolith's own required variables — `JWT_SECRET`,
`TWOFA_ENCRYPTION_KEY`, `BILLING_PROVIDER`; see `deploy/README.md`). Without
`--profile observability` the overlay starts nothing. Grafana listens on
`127.0.0.1:3000` (folder **Sandloom**), Prometheus on `127.0.0.1:9090`
(`/alerts`, `/rules`). No Alertmanager ships: add an `alerting:` block to
`prometheus/prometheus.yml` that points at yours.

Prometheus scrapes `app:2222/metrics` with `X-Internal-Token: $METRICS_TOKEN`
(handed over as a compose secret). Under Gunicorn one scrape returns the
aggregate of all live workers (`PROMETHEUS_MULTIPROC_DIR`), so scrape the
service, never individual workers; `process_*` metrics are not exported in
that mode. The Celery worker and beat export no metrics yet.

For the **microservices** profile, add one scrape target per service with its
own `job` (rules and dashboards aggregate `by (job)`), and run the overlay's
two services on the profile's network. In your own Prometheus/Grafana, load
the rule files and import the dashboard JSON as they are — every query uses a
`$datasource` and a `$job` variable.

## Dashboards

| Dashboard | Shows |
|---|---|
| Sandloom / HTTP RED | Rate, 5xx ratio, p50/p95/p99 per route template; reads (GET/HEAD) vs writes; 4xx and 429 by route; in-flight requests; untrusted `X-Forwarded-For`. |
| Sandloom / Outbox & relay | Oldest pending outbox age per database, broker circuit, time since the last relay batch, delivered / failed / dead-lettered, publishes and publish failures per channel, post-commit drops, retention purge, schema validation. |
| Sandloom / Consumers & streams | Per group: oldest unacked age, lag, pending; per consumer: end-to-end latency p50/p99, handler p99, processed / failed, projections; DLQ, reclaims, replays, group recreation (gap), published-without-consumers, version starvation. |
| Sandloom / Revocation & security | Revocation check p50/p99 by mode, deferred / applier failures / immediate refused / corrupt / malformed, pending durable revocations, cache errors per instance, rate limiter blind spots, 429s, idempotency persist failures, password rehash, RLS allowlist violations, login/refresh traffic. |
| Sandloom / Database pools | Checked-out / capacity per database and pool (main, contract), checkout waiters, connections by state, overflow, dead connections replaced, driver fast paths disabled. |
| Sandloom / Internal RPC | Per peer: p99 of successful attempts, attempts by outcome (ok/timeout/error), pool wait, waiting calls, connection churn; pair-MAC auth rejections, clock skew, response binding (rejected / unbound / unkeyed), transport fallbacks. Empty in the monolith. |
| Sandloom / Runtime & GC | GC pause p99 and collections per generation, time in GC, collected / frozen objects, startup duration, lifecycle task failures, module load failures, notifications, scrape health (`up`). |
| Sandloom / SLOs | Remaining 30-day error budget and 5m/1h/6h burn rates of the example SLOs (`docs/reference/slo.md`), outbox freshness, consumer lag. |

## Changing things

- **Dashboards**: edit `build_dashboards.py`, run it, commit the JSON
  (`--check` fails CI when they differ). Grafana's UI edits are not saved
  (`allowUiUpdates: false`).
- **Alerts**: edit `prometheus/rules/sandloom-alerts.yml`; a new or renamed alert
  also goes into the index in `docs/operations/operator-reference.md` → Alerts (same name and
  severity) and, when its behaviour matters, into
  `prometheus/tests/sandloom-rules.test.yml`.
- **Validate**: `deploy/observability/check.sh` (Docker) and
  `uv run pytest src/sandloom/core/tests/test_observability_bundle.py` (from
  `backend/`): every metric and label used must exist in the kernel's
  registry, so renaming a metric fails CI until the bundle follows.
