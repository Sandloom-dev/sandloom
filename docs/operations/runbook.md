---
title: Production Runbook
---

# Production Runbook

Operational guidance for running Sandloom in production. Pairs with the engineering
contract in `AGENTS.md`.

## Secrets

Secrets are loaded by `pydantic-settings` in priority order: process environment
→ `.env` file → a **secrets directory** (`SECRETS_DIR`).

- **Docker / Kubernetes secrets (recommended in prod).** Set `SECRETS_DIR`
  (e.g. `/run/secrets`) and mount each secret as a file named after the setting
  field: `jwt_secret`, `database_url`, `stripe_secret_key`, … The file contents
  are the value. This keeps secrets out of the process environment and out of
  compose YAML.
    - Docker Compose: `secrets:` + `SECRETS_DIR=/run/secrets`.
    - Kubernetes: mount a `Secret` as a volume at `/run/secrets`.
- **External secret managers** (Vault, AWS Secrets Manager, GCP Secret Manager):
  inject into the environment or render to the `SECRETS_DIR` mount via your
  platform's CSI driver / sidecar — no application code change required.
- **Fail-fast in prod:** `JWT_SECRET`, `JWT_PREVIOUS_SECRETS`,
  `TWOFA_ENCRYPTION_KEY` and (when set) `INTERNAL_API_TOKEN` / `METRICS_TOKEN`
  must be long and random — placeholders are rejected; `METRICS_TOKEN` must
  differ from `INTERNAL_API_TOKEN`. `TWOFA_ENCRYPTION_KEY` is required when
  identity is loaded. `BILLING_PROVIDER=stripe` requires `STRIPE_SECRET_KEY` +
  `STRIPE_WEBHOOK_SECRET`; `BILLING_PROVIDER=fake` is refused unless
  `SANDLOOM_ACCEPTED_RISKS=["fake_psp_in_prod"]`.
- **Rotation.** JWT: move the old secret to `JWT_PREVIOUS_SECRETS`, keep it for
  at least `ACCESS_TOKEN_EXPIRE_MINUTES`, then drop it (asymmetric keys: add
  the new kid to `JWT_PUBLIC_KEYS` everywhere, switch the issuer, drop the old
  kid — see the security model). TOTP: prepend the new
  key to `TWOFA_ENCRYPTION_KEY` (comma list, newest first) and keep old keys
  until secrets are re-encrypted — losing every key makes enrolled TOTP secrets
  undecryptable.
- Logs redact sensitive keys and scrub credential-shaped text; Sentry events
  pass the same redaction.

## Database migrations

Run before serving traffic. The compose profiles do this automatically via a
one-shot `migrate` service that the app waits on
(`depends_on: service_completed_successfully`).

```bash
sandloom migrate databases                                   # list configured DB instances
sandloom migrate upgrade-all --apply                         # core chain → every DB + each module's chain → its DB
sandloom migrate upgrade-core --database analytics --apply   # core chain → a named DB
sandloom migrate upgrade-module identity --apply             # one module's chain → its DB(s)
sandloom migrate ensure-outbox --all-databases --apply       # DEV/REPAIR: per-DB shared infra tables
```

`sandloom migrate` holds a PostgreSQL advisory lock per target database, so
concurrent migrate jobs (replicas, retried CI steps) serialise safely. Each
database that publishes events owns its own transactional outbox; the audit log
lives in the database of the active Unit of Work. See the
[Migrations guide](../guides/migrations.md).

## Dead-lettered events

Two terminal states need an explicit operator action once the cause (a bug, a
long broker outage, a missing schema) is fixed. Both keep the stable envelope
id, so consumers that already applied an event dedupe the redelivery.

```bash
# Outbox rows that exhausted OUTBOX_RELAY_MAX_ATTEMPTS (dead_at set)
sandloom outbox list-dead [--database NAME] [--channel CH] [--limit N]
sandloom outbox requeue   [--database NAME] (--id UUID ... | --all) [--channel CH]

# Streams entries a consumer group kept failing (moved to <channel>.dlq)
sandloom streams dlq-list   <channel> [--limit N]
sandloom streams dlq-replay <channel> [--id DLQ_ID ...] [--group G] [--limit N]
# Consumer groups (lag / pending), cross-service inventory, owner release
sandloom streams groups <channel>
sandloom streams consumers [--all]
sandloom streams release-group <channel> <group>
```

Watch `outbox_dead_total` and the DLQ streams. Relay retry pacing is tuned with
`OUTBOX_RELAY_BACKOFF_BASE_SECONDS` / `OUTBOX_RELAY_BACKOFF_MAX_SECONDS`
(per-row backoff) and `OUTBOX_RELAY_CIRCUIT_BASE_SECONDS` /
`OUTBOX_RELAY_CIRCUIT_MAX_SECONDS` (broker-down cool-down, which does not burn
row attempts).

## Background workers

Run the framework Celery entrypoint (`celery -A sandloom.core.worker worker` and
exactly **one** `celery -A sandloom.core.worker beat` per broker). The worker boots
`SANDLOOM_COMPOSITION_ROOT` (default `sandloom.main`) with the process's `SANDLOOM_MODULES`
and loads each loaded module's `tasks.py`. `sandloom migrate`, `sandloom outbox`,
`sandloom streams` and `sandloom projection` resolve the same composition root, so set
`SANDLOOM_COMPOSITION_ROOT` for them too when operating a generated app. See
[Deployment](../guides/deployment.md#background-work-celery-worker-beat).

## Backups & restore (PostgreSQL)

The framework does not manage backups — wire them into your platform. Baseline:

- **Logical backup (small/medium):**
  `pg_dump --format=custom --dbname="$DATABASE_URL" --file=sandloom-$(date +%F).dump`.
  Restore with `pg_restore --clean --if-exists --dbname="$TARGET_URL" sandloom-….dump`.
- **Continuous / PITR (production):** enable WAL archiving, or use managed
  Postgres with automated backups + point-in-time recovery.
- **Verify restores** on a schedule into a scratch database — an untested backup
  is not a backup.
- Back up **every** configured database instance (`sandloom migrate databases`), not
  just the default.
- The compose `pgdata` volume is dev-only; production must use managed/replicated
  storage with a backup policy.

## Data retention & PII

- **TOTP secrets** are encrypted at rest; **refresh tokens** are stored hashed.
- **Soft delete:** tenancy organizations soft-delete; rows persist until a purge
  job removes them. Define a retention window per data class and run a scheduled
  purge (Celery beat) — there is no automatic purge yet.
- **GDPR / right-to-erasure:** implement per-module erasure that removes or
  anonymizes PII for a subject id; the audit log retains the *fact* of actions
  without sensitive payloads — keep PII out of `record_audit(..., data=...)`.
- **Audit log retention:** append-only; size it and archive/prune on a policy.

## Audit log

Security-sensitive actions are recorded via `record_audit()`. Entries store
actor, action, resource, `request_id` and `trace_id`, and commit within the
audited transaction. Query the `audit_log` table for who-did-what investigations.
See [Audit](../concepts/audit.md).

## Observability

- **Logs:** JSON in production (`LOG_FORMAT=auto`); each line carries
  `request_id` and `trace_id`. Ship to your log pipeline.
- **Metrics:** Prometheus at `/metrics` (token-gated: give the scraper the
  scrape-only `METRICS_TOKEN`, not `INTERNAL_API_TOKEN`). HTTP RED metrics
  (`sandloom_http_requests_total`, `sandloom_http_request_duration_seconds`) plus event
  bus, outbox relay, lifecycle, and schema metrics.
- **Dashboards & alerts:** `deploy/observability/` ships provisioned Grafana
  dashboards, Prometheus alert + SLI recording rules and an optional compose
  overlay (Prometheus + Grafana) for the monolith profile. Example SLOs and
  error-budget arithmetic: [SLOs](../reference/slo.md).
- **Traces:** set `TRACING_URL` to your OTLP endpoint (TLS auto for `https`).
- **Errors:** set `SENTRY_DSN` to enable Sentry reporting.
- **Probes:** use `/health/simple` for process liveness: a database or cache
  outage must not trigger a restart loop. Use `/ready` for readiness and routing
  (`503` when degraded); `/health` reports operational health and can also
  return `503` when a dependency is unavailable. Readiness includes
  health + required modules + every required event consumer actively
  subscribed. Probe results are cached for `HEALTH_CACHE_TTL_SECONDS`
  (default 3; `0` disables). Without `X-Internal-Token` callers see statuses
  only — no check details, module or consumer names.
- **Diagnostics** (token-gated): `/diagnostics/infra` (capabilities, drivers,
  stability, instances + per-instance health), `/diagnostics/consumers`
  (per-consumer live state), `/diagnostics/services`, `/diagnostics/core`.
  Without `INTERNAL_API_TOKEN` these answer `404` (set
  `INTERNAL_ENDPOINTS_OPEN_IN_DEV=true` for local development).
