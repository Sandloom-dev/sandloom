---
title: Operator reference
description: Authentication, keys, incidents, backups, migrations, streams and operational command flags with dry-run and production safeguards.
---

# Operator reference

Every operational command, flag, metric and alert: secrets, migrations, events,
sagas, backups, incidents, retention, audit and observability. To find a procedure
by situation, start with the [runbook](runbook.md). To set up production, see
[Deploy an application](../guides/deployment.md).

## Secrets management

Secrets are loaded by `pydantic-settings` from, in priority order: process
environment → `.env` file → a **secrets directory** (`SECRETS_DIR`).

- **Docker/Kubernetes secrets (recommended in prod).** Set `SECRETS_DIR`
  (e.g. `/run/secrets`) and mount each secret as a file named after the
  setting field: `jwt_secret`, `database_url`, `stripe_secret_key`, … The
  value is the file contents. This keeps secrets out of the process
  environment and out of compose YAML.
  - Docker Compose: `secrets:` + `SECRETS_DIR=/run/secrets`.
  - Kubernetes: mount a `Secret` as a volume at `/run/secrets`.
- **External secret managers** (Vault, AWS Secrets Manager, GCP Secret
  Manager): inject into the environment or render to the `SECRETS_DIR` mount
  via your platform's CSI driver / sidecar. No application code change is
  required — the framework only sees env vars / files.
- Production fail-fast (`ENVIRONMENT=prod`): `JWT_SECRET`, `JWT_PREVIOUS_SECRETS`,
  `TWOFA_ENCRYPTION_KEY`, `INTERNAL_API_TOKEN` and `METRICS_TOKEN` must be
  ≥ 32 chars and not a placeholder, and `METRICS_TOKEN` must differ from
  `INTERNAL_API_TOKEN`; identity refuses to start without
  `TWOFA_ENCRYPTION_KEY`; `BILLING_PROVIDER=stripe` requires
  `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET`, and `fake` is refused unless
  `SANDLOOM_ACCEPTED_RISKS=["fake_psp_in_prod"]` (demo stacks only). Every accepted
  risk is logged as a WARNING at each start, exported as
  `sandloom_accepted_risk_active{risk}` and listed by `/diagnostics/core` and
  `sandloom doctor`; an unknown id fails startup.
- **Rotation.** JWT: set the new `JWT_SECRET`, move the old one into
  `JWT_PREVIOUS_SECRETS` (still verified) and optionally set `JWT_KID`; drop it
  after the access-token lifetime. Asymmetric JWT (`EdDSA`/`RS256`/`ES256`):
  publish the new kid in `JWT_PUBLIC_KEYS` everywhere, switch the issuer's
  `JWT_PRIVATE_KEY` + `JWT_KID`, drop the old kid after the access-token
  lifetime (runbook: `docs/reference/security-model.md`). 2FA: `TWOFA_ENCRYPTION_KEY` is a
  comma-separated list, newest first — keep old keys until every secret is
  re-encrypted. Internal token: rotate on every service together (RPC
  signatures are verified with the provider's own token).
- **Revoking sessions.** `revoke_user_tokens(user_id)` invalidates a subject's
  already-issued access tokens (an `after` value in the subject record
  `sandloom:auth:subject:{sub}`). With `AUTH_REVOCATION_FAIL_CLOSED`
  (default: on in prod) a cache outage makes token checks answer `503` rather
  than accept possibly-revoked tokens.
- **Revocation precision, skew and durability.** A token is revoked iff its
  issue instant (`iat_us`, microseconds; a token without `iat_us` is revoked
  by any mark of its subject) is `<=` the mark; the mark (`seconds.micros`) is written at
  `now + AUTH_REVOCATION_CLOCK_SKEW_SECONDS` (default `2`, max `60`; keep it
  >= your worst NTP drift and identical everywhere). Trade-off: a token minted
  within the margin AFTER a revocation by an issuer that did not read the mark
  first is rejected once (framework issuers read it and are unaffected).
  `revoke_user_tokens_on_commit` writes `core.auth.tokens_revoked.v1` into the
  revoking transaction, retries the after-commit mark write
  (`AUTH_REVOCATION_WRITE_ATTEMPTS`=3, backoff from
  `AUTH_REVOCATION_RETRY_BASE_SECONDS`=0.05) and, if it still fails, leaves it
  to identity's durable consumer `sessions@tokens_revoked` (redelivered until
  the cache is back; idempotent). Alerts:
  `increase(auth_revocation_deferred_total[5m]) > 0` (warning: revocations
  pending on the applier — a cache outage), and
  `increase(auth_revocation_applier_failures_total[10m]) > 0` sustained, or a
  growing `sessions@tokens_revoked` pending count / DLQ entry on
  `core.auth.tokens_revoked.v1` (critical: revoked tokens still accepted). A
  deployment without identity has no applier — the retries are its only
  backstop. Revocations made with `require_immediate=True` (every bundled
  security event) never depend on it: their mark is written before commit,
  or the change is refused with `503` —
  `increase(auth_revocation_immediate_refused_total[5m]) > 0` means changes
  are being refused because the cache is unreachable;
  `auth_revocation_remark_overtook_floor_total` counts such changes whose
  UoW committed later than one skew after the call (that request's fresh
  tokens were revoked too — users re-authenticate; look for a slow step after
  the revocation call). A
  `auth_revocation_corrupt_mark_total` increase means some writer stored an
  unparsable mark: that user is locked out (fail-closed) until their next
  login repairs it — investigate the writer.
- **Stuck / dead-lettered revocations (runbook).** identity's
  `sessions@tokens_revoked` and `sessions@user_blocked` consumers are
  `dead_letter=False`: a delivery that fails (cache or database down) is
  retried forever with capped backoff (≤ 60 s), never parked. Alert on
  `auth_revocation_pending_seconds > 60` (warning) / `> 300` (critical: a
  revoked token may still be accepted wherever the cache is reachable); the
  affected identity replica answers `/ready` 503 (`revocations_pending`) until
  it drains, and logs `Durable revocation of subject … still pending` at ERROR.
  Fix the cache; nothing else to do. A CRITICAL `Malformed durable revocation`
  log / `increase(auth_revocation_malformed_total[5m]) > 0` / a new entry with
  reason `poison_event` in `sandloom streams dlq-list core.auth.tokens_revoked.v1`
  means a producer wrote a payload that can never apply: find the producer
  (the entry keeps the envelope), revoke the user by hand
  (`revoke_user_tokens(user_id)` or `block_user`) and, only once the producer
  is fixed and the payload corrected upstream, drop the entry. `sandloom streams
  dlq-replay` would re-deliver the same malformed payload and dead-letter it
  again.
- Logs redact keys containing sensitive substrings and scrub token-like values
  inside messages (`sandloom.core.observability.guardrails`); the same scrubber is
  Sentry's `before_send`.

## Uncertain task enqueues

`taskqueue.enqueue(..., idempotency_key=...)` stores reservations in
`SECURITY_CACHE_INSTANCE` under `sandloom:task-enqueue:*`. Use persistent Redis with
`noeviction`, shared by every producer of the queue. Successful records expire
after the driver's `dedupe_ttl_seconds` replay window; unresolved pending
reservations deliberately do not expire.

If enqueue returns `TaskEnqueueUncertain`, do not delete the reservation or
retry with another key. Inspect the reservation's `id` and reconcile that task
ID with broker, worker and application records. Stop the affected publisher
before reconciliation so its in-flight send cannot race the decision. Only
after establishing the outcome may an operator atomically replace that exact
pending record with `state=sent` and the confirmed task ID, or compare-and-delete
it if the task was definitely never published. If the outcome remains unknown,
leave the reservation pending and reconcile the business effect. A broker
redelivery still requires handler-side DB-atomic idempotency.

## Preflight and effective configuration

Two read-only commands answer "what is this process configured with?" and
"will it work?" before (or instead of) starting it. Both resolve configuration
exactly like the app (process env > `.env` > `SECRETS_DIR` files > defaults)
and never print secrets.

```bash
sandloom config dump                    # every setting, redacted, with its source
sandloom config dump --only JWT_,RPC_   # name prefixes
sandloom config dump --non-default --json
sandloom doctor                         # preflight report; exit 0 / 1 warn / 2 fail
sandloom doctor --json --timeout 10 --skip consumers
```

`sandloom config dump` prints `NAME = value  [env|dotenv|secrets_dir|default]`.
Credentials are redacted by name (`*SECRET*`, `*PASSWORD*`, `*TOKEN`,
`*PRIVATE*`, `*_KEY`, `*DSN`, …, recursively inside `INFRA` /
`SERVICE_BINDINGS`) and `user:password@` is masked in every URL; an unset
secret stays visible as `null`. `--show-secrets` prints them in clear and is
refused with `ENVIRONMENT=prod`.

`sandloom doctor` boots the composition root (`SANDLOOM_COMPOSITION_ROOT`, like the
worker and `sandloom migrate`; no lifespan, no consumers, no relay) and reports:

| Section | Checks |
|---|---|
| `config` | settings validate; `ENVIRONMENT` set explicitly; prod: ops token, docs; `multi_tenant_strict` requirements |
| `modules` | the composition root loads; every consumed contract is local or bound in `SERVICE_BINDINGS` |
| `infra` | every configured instance (databases, caches, object stores, task queue, event bus) answers its health probe in time — the `/diagnostics/infra` probes |
| `migrations` | per database: applied revision of the core chain and each module chain vs the code's heads (`ok` / `behind` / `missing` / `unknown` = migrated by a newer release); read-only connections |
| `consumers` | each durable consumer's Streams group exists; pending and lag (`--lag-warn`, default 1000) |
| `rls` | `tenant_rls` databases: the app role cannot escape RLS; strict profile: every tenant table covered |

Exit codes: `0` all ok, `1` warnings only, `2` at least one failure (or a
usage error). It writes nothing and runs in production as is — use it as a
deploy gate after `migrate` and before shifting traffic.

## Database migrations

Run before serving traffic. The compose profiles do this automatically via a
one-shot `migrate` service that the app waits on.

Fresh scaffold baselines enable and force tenant RLS when their target
database is configured with `tenant_rls: true` in `INFRA`. The migration runner
matches the effective target URL, including named instances. With the option
off, the baseline leaves RLS off so ordinary non-superuser deployments keep
working with ORM tenant scoping. When enabling RLS for an already migrated
database, add a new module revision calling `enable_tenant_rls` on its tenant
tables; changing runtime configuration alone does not alter existing tables.

```bash
sandloom migrate databases                       # list configured DB instances
sandloom migrate upgrade-all --apply             # core chain -> every hosting DB +
                                            # each module's chain -> its DB
sandloom migrate upgrade-module billing --apply  # one module's chain
sandloom migrate upgrade-core --database analytics --apply   # core chain -> named DB
```

**Extensions.** The identity chain creates `citext` (case-insensitive emails)
and `pg_trgm` (the admin user search's trigram index, identity 0006). Both are
trusted extensions: the migration role needs `CREATE` on the database, or a
superuser creates them beforehand (`deploy/postgres/init-databases.sh` does,
for every service database). This holds under
`PRODUCTION_PROFILE=multi_tenant_strict` as well: there the migration role
owns the tables and the extensions, and the application role needs nothing
extra (index use and the `pg_trgm` functions require no grant). Some indexes
are built `CONCURRENTLY` in an autocommit block (no write lock; a failed build
leaves an invalid index, which `DROP INDEX CONCURRENTLY` plus a re-run fixes).

`upgrade-all` exits non-zero on failure and holds a per-database advisory
lock, so concurrent migrate one-shots serialise. A runner that finds the lock
taken waits at most `--lock-timeout SECONDS` (`upgrade-all` / `upgrade-core` /
`upgrade-module`; default `MIGRATION_LOCK_TIMEOUT_SECONDS`, 300) and logs
every 5 s WHO holds it — pid, `application_name`, user, client address and
query start, from `pg_locks` + `pg_stat_activity` (migration runners connect
as `application_name='sandloom-migrate'`). The timeout error carries the same <!-- stale-ok: runtime application_name -->
line. A stuck holder is usually a crashed CI job's open session: let it
finish, or `SELECT pg_terminate_backend(<pid>)` after checking it is not a
live migration. `sandloom migrate ensure-outbox` is
a dev/repair tool (refused in prod without `--force` plus the accepted risk
`unversioned_ddl_repair` in `SANDLOOM_ACCEPTED_RISKS`). Each database that
publishes events owns its own transactional outbox; the audit log lives in the
database of the active Unit of Work.

## Events: outbox relay and dead letters

- The relay (in-process, `OUTBOX_RELAY_ENABLED=auto` = prod; under gunicorn
  exactly one elected worker runs it, the others run with `delegated`; `off`
  in a prod app process is refused unless `SANDLOOM_ACCEPTED_RISKS` lists
  `relay_disabled`) retries a failed row with exponential backoff
  (`OUTBOX_RELAY_BACKOFF_BASE_SECONDS` / `OUTBOX_RELAY_BACKOFF_MAX_SECONDS`)
  and dead-letters it after `OUTBOX_RELAY_MAX_ATTEMPTS` (`dead_at` set,
  `outbox_dead_total`). A broker outage opens a circuit
  (`OUTBOX_RELAY_CIRCUIT_BASE_SECONDS` / `OUTBOX_RELAY_CIRCUIT_MAX_SECONDS`)
  that pauses dispatch without consuming attempts
  (`outbox_relay_broker_unavailable_total`).
- Inspect / requeue dead outbox rows:
  `sandloom outbox list-dead [--database x] [--channel c]`,
  `sandloom outbox requeue --id <id> | --all`.
- Post-commit dispatch runs OFF the request path: a request only enqueues its
  committed events; a per-process background coalescer publishes them in
  commit order and marks them delivered in one batched UPDATE (<= 50 ms).
  Each round takes everything already queued (<= 64 UoWs / ~512 events) and
  publishes it as ONE ordered batch (Redis Streams: one `MULTI`/`EXEC`
  round trip); a batch that fails or times out marks nothing delivered and
  is left to the relay (duplicates possible, never loss).
  On shutdown the app drains it for up to 5 s; anything left (or dropped on
  queue overflow, `outbox_post_commit_dropped_total`) is an undelivered row
  the relay delivers after its 3 s grace. Workers/scripts using
  `open_uow()` still publish in line.
- The relay claims a batch of 50 due rows in one statement
  (`FOR UPDATE SKIP LOCKED`, per-batch lease token) and settles it in one
  transaction; a full batch is followed by the next one without sleeping, so
  a backlog drains at broker speed. On Redis Streams the claimed batch is
  published in one `MULTI`/`EXEC` round trip; a broker outage parks the
  first row (no attempt counted), any other batch failure is retried row by
  row so attempts and dead-lettering stay per row. Several relays (one per
  container) partition the backlog safely.
- **Retention**: delivered rows are deleted by the relay once older than
  `OUTBOX_RETENTION_HOURS` (default 168; 0 = keep), at most 10 000 rows per
  database every ~60 ticks (`outbox_purged_total`); dead-lettered and pending
  rows are never purged. On demand / for a large backlog:
  `sandloom outbox purge [--database x] [--hours H] [--batch N]`.
- **Durable consumer inbox** (`consumer_inbox`) claims are NOT pruned
  automatically: a claim must outlive every possible redelivery of its event
  (Streams retention `EVENT_STREAM_RETENTION_SECONDS`, an operator `requeue`
  of an old dead row) or a late duplicate would apply its effect twice. Prune
  explicitly with a cutoff well past that window:
  `sandloom outbox purge --inbox-days 30`.
- Streams consumers dead-letter poison entries to `<channel>.dlq` (with reason
  and consumer group): `sandloom streams dlq-list <channel>`,
  `sandloom streams dlq-replay <channel> [--id <entry>] [--group <g>]`. A replay is
  delivered ONLY to the group that dead-lettered the entry; every other group
  acks it unprocessed (no repeated side effects elsewhere).
- Consumer groups are `<module>:<name>`. `sandloom streams groups <channel>` shows
  each group's lag / pending (also `sandloom_streams_group_lag` /
  `sandloom_streams_group_pending` and `/diagnostics/consumers`);
  `sandloom streams consumers [--all]` lists the cross-service subscription
  inventory (stream, group, module, `SANDLOOM_SERVICE_NAME`, consumer id;
  heartbeated every 30 s, stale after 5 min). Set `SANDLOOM_SERVICE_NAME` per
  deployment so these listings say which service a consumer belongs to.
- Group ownership: the first process to join a group records
  `sandloom:group-owner:<stream>:<group>` (module + physical consumer database).
  A process of another module / database joining that group fails startup —
  two deployments would split the group's events. When a module deliberately
  moves to another database (e.g. monolith → per-service DB), drain the group
  (lag and pending 0) on the old deployment, then
  `sandloom streams release-group <channel> <group>` before starting the new one.
- Upgrading from bare-name groups (≤ 0.3): a consumer's new `<module>:<name>`
  group is created at the old `<name>` group's last-delivered position, so no
  history is re-applied. Entries still PENDING in the old group are not moved —
  drain them first (temporarily pin `group="<name>"`), then
  `XGROUP DESTROY <stream> <name>`.
- Retention: each stream is trimmed to `EVENT_STREAM_RETENTION_SECONDS`
  (default 7 days, `XTRIM MINID ~`) and, if set, `EVENT_STREAM_MAXLEN`
  (`MAXLEN ~`). A consumer offline longer than retention misses the trimmed
  events; recover them from the outbox (`sandloom outbox requeue`) or a projection
  rebuild. `EVENT_STREAM_PREFIX` namespaces every Streams key when several
  environments share one Redis.
- v1 → v2 channel rollouts: dual-publish both versions
  (`publish_event_versions`) until `sandloom streams consumers` shows no live `.v1`
  group; `sandloom_events_version_starvation_total` /
  `sandloom_events_published_without_consumers_total` (+ a warning log) flag a
  producer that stopped feeding live consumers.
- Projection rebuilds (`sandloom projection rebuild <name>`) run the startup tasks
  only — no consumer runtime, no outbox relay — and read the source modules
  over their read contracts (`SERVICE_BINDINGS` in a split deployment). Run
  them as one-off jobs once the source services are up; never put them in the
  `migrate` one-shot (the sources are not running yet).
- Run the durable `streams` bus in production (`EVENT_BUS_BACKEND=auto` picks
  it); the volatile drivers are refused in prod unless the deployment accepts
  the risk `volatile_event_bus` (`SANDLOOM_ACCEPTED_RISKS`).

## Sagas

The saga runtime (ADR-0008, `docs/concepts/sagas.md`) runs in every process
that hosts a saga-owning module: one advance consumer per module
(`<module>:sagas@advance`, `durable=False`, `dead_letter=False`) and the
lease-guarded timer sweep (one holder per database, independent of the outbox
relay).

- **Sizing: one pooled connection per in-flight step.** An advance holds a
  connection of the default pool and the instance row lock from its
  `SELECT … FOR UPDATE SKIP LOCKED` to its `COMMIT`: one step call (a local
  step's savepoint, or a command on the contract pool / over RPC — bounded by
  `SERVICE_RPC_TIMEOUT_SECONDS` + one transport retry, or the contract pool's
  `CONTRACT_DB_LOCK_TIMEOUT_MS`). A command step also checks out one
  contract-pool connection for its own UoW. Budget
  `consumer concurrency × 1` default-pool connections (+ the same on the
  contract pool for local command steps) on top of request traffic; the
  connections are never on the request path. The saga rung
  (`benchmarks/ladder/saga_rung.py`, `ladder/STANDS.md` → C) measured on a
  shared dev machine: the start adds 3 statements (≈ 2.3 ms) to the caller's
  transaction; a local step's transition is 5 statements (≈ 5 ms), a command
  step 7 (≈ 7 ms, the provider's command-log claim + result update included).
  Re-measure on your hardware before sizing from it.
- **Retries and deadlines are the sweep's.** A transient failure schedules
  `next_attempt_at` (full-jitter backoff, `SAGA_RETRY_BASE_SECONDS` /
  `SAGA_RETRY_MAX_SECONDS`) and publishes no nudge: the sweep re-drives it at
  most once per `SAGA_SWEEP_INTERVAL_SECONDS` (default 5 s), claiming at
  most `SAGA_SWEEP_BATCH` instances per tick (`FOR UPDATE SKIP LOCKED`).
  Retry latency is therefore `max(backoff, sweep interval)`. The sweep lease
  lives in `BEAT_LEASE_CACHE_INSTANCE` (needs `cas`; without it every process
  sweeps — safe, only redundant).
- **Under `tenant_rls`** the sweep and `sandloom sagas` reach other tenants'
  instances only through the system channel (`DATABASE_SYSTEM_URL`, the
  `sandloom_system` role, which must exist before the core migration `0010` so
  the saga tables get system access). Startup refuses a saga database with
  `tenant_rls` and no system channel. An idle tick rolls its system session
  back (no audit row); a tick that nudged instances is audited as
  `sandloom.system_db.session`. Raise the interval if those log lines are noisy.
- **Windows.** `(step deadline, else saga deadline) + compensation_deadline`
  (`SAGA_COMPENSATION_DEADLINE_SECONDS`, default 1 h, per definition
  overridable) must fit `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`
  (`saga_horizon_beyond_command_dedupe`: refused in prod, a warning
  elsewhere).
- **Retention.** The sweep holder purges completed / compensated instances
  older than `SAGA_RETENTION_DAYS` (default 30, 0 = keep) about once an hour;
  parked instances are never purged.
- **Moving a module out.** `sandloom migrate extract-module` copies its saga
  instances (`saga_type` = `<module>.<name>`) and step rows with its command
  records; the new service continues every in-flight saga.

### Runbook: a parked saga

```bash
sandloom sagas list                                   # parked, compensating, overdue
sandloom sagas show enrollment.enroll <id>            # timeline, why parked, next step
sandloom sagas retry enrollment.enroll <id> --apply --reason "provider fixed"
sandloom sagas resolve enrollment.enroll <id> --step charge --as applied \
    --apply --reason "refund issued by hand, ticket 123"
sandloom sagas park enrollment.enroll <id> --apply --reason "hold during the incident"
```

Every mutation is a dry run without `--apply --reason`, needs
`--confirm-production` in prod and is audited (`core.saga.retried` /
`core.saga.parked` / `core.saga.step_resolved`). `show` prints the fixed
remediation of each park reason:

| reason | meaning | do |
|---|---|---|
| `retriable_refused` | a step after the pivot refused | fix the provider / arguments, `retry`; not needed → `resolve --as skipped` |
| `compensation_refused` | a compensation refused | undo by hand at the provider (search the step's operation key), `resolve --as applied` |
| `compensation_deadline_exceeded` | the compensation kept failing | fix the provider, `retry` |
| `deadline_exceeded` | past the pivot and past the deadline | fix the failing step's provider, `retry` (fresh deadline) |
| `nondeterministic_args` | the provider saw the key with other arguments | fix the args function in a new version; resolve the step by hand |
| `result_not_replayable` | the step ran but its result was too large to replay | establish the outcome, `resolve --as applied` |
| `misconfigured` | 401 / argument binding / a missing contract | fix the wiring, deploy, `retry` |
| `unknown_definition` | the (type, version) is not registered here | deploy that version alongside the new one, `retry` |
| `outcome_unknown_after_ttl` | the last attempt was ambiguous and its dedupe record may be gone | establish the outcome at the provider, `resolve --as applied|skipped` |
| `operator` | parked by `sandloom sagas park` | `retry` when it may continue |

**Retry after the dedupe window.** `retry` refuses (exit 1) while the current
step's outcome is unknown and its first attempt is older than
`CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` minus one sweep interval and the RPC
timeout: a re-call could run the step twice. Establish the outcome at the
provider (`show` prints the operation key) and record it with `resolve`.

## Upgrading asyncpg / SQLAlchemy

Two round-trip fast paths drive PRIVATE driver state: merged BEGIN
(`DB_RLS_MERGED_BEGIN`, `tenant_rls` databases) and the autocommit outbox
mark-delivered; the dead-connection retry and the pool's non-blocking
checkout (`LingeringQueuePool`, probe `pool_fast_checkout`; fallback: SQLAlchemy's blocking `wait_for(queue.get())` checkout) do too.
`sandloom.core.sql_alchemy.driver_compat`
probes those internals once per process (names, kinds, and an I/O-free
self-test against asyncpg's real `Transaction` and an in-memory SQLAlchemy
`Session`). If anything changed, the process logs ONE warning
(`event=db.driver_fast_path_disabled`), increments
`sandloom_db_driver_fast_path_disabled_total{probe=...}` and uses the plain path
(separate `BEGIN` + `set_config`; a normal transactional mark-delivered).
Requests never fail because of it — alert on the metric, it costs round trips.

The verified versions are pinned as ranges in `pyproject.toml` and
`packages/sandloom/pyproject.toml` (upper bound = the next minor
not yet verified) and mirrored in `VERIFIED_DRIVER_RANGES`; a test fails when
the installed version leaves the range or the two disagree. To verify an
upgrade (from the repository root, on your own test database):

1. Bump the pin (and the range's upper bound) in both pyprojects and in
   `VERIFIED_DRIVER_RANGES`, then `uv lock && uv sync`.
2. `uv run pytest src/sandloom/core/tests/test_driver_compat.py -q --no-cov` — the
   probes must pass (a failure names the changed internal).
3. `uv run pytest tests/perf_budgets src/sandloom/core/tests/test_tenant_rls*.py
   src/sandloom/core/tests/test_post_commit_dispatcher.py
   src/sandloom/core/tests/test_outbox_relay_batch.py -q --no-cov` — round-trip
   budgets (3 per merged-BEGIN read, 1 per mark-delivered), cancellation /
   failure cleanliness of the merged BEGIN, RLS isolation and the fallbacks.
4. The full suite. Only then widen the range.

## Upgrading aiohttp

`RPC_TRANSPORT=aiohttp` (the default internal-RPC client) subclasses aiohttp
internals to observe its pool and to close a pooled connection that receives
bytes while idle. `sandloom.core.rpc_transport_compat` probes them (names and
kinds when the client is built; at startup of a process with
`SERVICE_BINDINGS`, also a loopback self-test of the idle-connection guard).
If anything changed the process logs ONE warning
(`event=rpc.transport_fallback`), increments
`sandloom_rpc_transport_fallback_total{reason}` and builds the shared RPC client on
httpx — calls keep working at httpx's CPU cost; alert on the metric. Mixed-up
responses are excluded independently of the client by the response binding
([Response binding](../guides/service-rpc.md#response-binding)).

The verified range is pinned in `pyproject.toml` and
`packages/sandloom/pyproject.toml` and mirrored in
`VERIFIED_AIOHTTP_RANGE`; a test fails when the installed version leaves the
range or the pins disagree. To verify an upgrade (from the repository root):

1. Bump the range in both pyprojects and `VERIFIED_AIOHTTP_RANGE`, then
   `uv lock && uv sync`.
2. `uv run pytest src/sandloom/core/tests/test_rpc_transport_compat.py -q --no-cov`
   — static checks and the self-test must pass (a failure names the change).
3. `uv run pytest src/sandloom/core/tests/test_rpc_transports.py
   src/sandloom/core/tests/test_rpc_response_binding.py -q --no-cov` — the fault
   suites on both transports.
4. The full suite incl. the split conformance matrix. Only then widen the range.

## Backups & restore

The framework does not run backups — wire them in your platform — but it tells
you what to back up and proves a backup restores:

```bash
sandloom backup plan [--json]          # per database instance + per Redis endpoint
sandloom backup verify default-20261022T0300Z.dump --database default \
    --server-url postgresql+asyncpg://owner:…@scratch-pg:5432/postgres
```

- **`sandloom backup plan`** lists every configured database instance (with the
  migration chains it hosts), every Redis endpoint with its roles (cache,
  **security cache**, event-bus streams, task broker), the object store and the
  secrets, each with backup and restore steps. Passwords are masked.
- **PostgreSQL:** `pg_dump --format=custom --no-owner --no-privileges` per
  instance, run as the **owner / migration role** — under `tenant_rls` the app
  role sees one tenant at a time. Production: WAL archiving / managed PITR plus
  periodic base backups.
- **`sandloom backup verify <dump>`** restores the dump into a throw-away database
  (`sandloom_restore_verify_*`) on `--server-url` (default: the instance's own
  server; in prod pass a scratch server or `--confirm-production`), checks that
  every chain the instance hosts is at its head, that the core tables exist,
  that indexes / constraints are valid and foreign keys hold, reports the
  newest audit row (your effective RPO) and the outbox backlog, then drops the
  database (`--keep` keeps it). Exit 0 = the backup restores and is current.
  The dump is piped on stdin, so a containerised client works:
  `--pg-restore "docker exec -i pg pg_restore" --restore-url
  postgresql://owner:…@localhost:5432/x`. Schedule it and alert on failure.
- **Redis (security cache):** `appendonly yes`, `appendfsync always` (`everysec`
  can lose the last second of revocation marks / blocks on a crash — run
  `sandloom backup reconcile-security` after such a restart if you accept it), RDB
  snapshots copied off-host, `maxmemory-policy noeviction`, a separate instance
  in prod (`SECURITY_CACHE_INSTANCE`). It holds the revocation marks and the
  emergency blocks — state that exists nowhere else live.
- **Redis (idempotency cache, opt-in `IDEMPOTENCY_CACHE_INSTANCE`):** the
  same persistence, eviction policy and backups as the security cache — never
  weaker. It holds the idempotency reservations / replay records and task
  enqueue dedupe; restore it with the security cache.
- **Secrets** (JWT keys, `TWOFA_ENCRYPTION_KEY` — every key still in the list —
  RPC keys, PSP keys) live in the secret manager, never next to the dumps: a
  dump plus the 2FA key is a readable set of TOTP secrets.

### Restoring: the order, and the traffic gate

1. Stop writers (app, worker, beat at 0) — or keep the gateway closed.
2. Restore PostgreSQL (`pg_restore` into an empty database / PITR), then
   `sandloom migrate upgrade-all --apply`.
3. Restore Redis if it was lost.
4. **`sandloom backup reconcile-security`** (dry-run), then
   `sandloom backup reconcile-security --apply --reason "restore INC-…"`. A Redis
   snapshot OLDER than a later `block_user` silently unblocks that user (an
   absent block mark means "not blocked"), and a lost revocation mark lets the
   revoked access tokens pass until they expire. The command re-derives both
   from the durable records: the newest `auth.user.blocked` /
   `auth.user.unblocked` audit row per user across every database (a tie is
   resolved as blocked), and every `core.auth.tokens_revoked.v1` outbox row
   inside the mark lifetime (re-applied monotonically: a newer mark is never
   lowered). A block mark whose newest record is an unblock is only reported —
   `--lift-stale-blocks` removes it; staying blocked is the safe side.
   If the DATABASE was restored to a point before a block, its audit row is gone
   too: pass the off-site export with `--audit-export audit.jsonl` (see below).
5. **Admit traffic only when step 4 exits 0** ("clean"). Exit 1 means marks are
   missing (dry-run), a write failed, or the outbox does not cover the
   access-token lifetime (`OUTBOX_RETENTION_HOURS` shorter than
   `ACCESS_TOKEN_EXPIRE_MINUTES`): then end every session
   (`sandloom security revoke-sessions --all --apply …`) or rotate the JWT key before
   opening the gateway.
6. Outbox rows pending at the snapshot are re-published by the relay
   (at-least-once; consumers dedupe on the envelope id). Events delivered to a
   Streams snapshot newer than the restored Redis are gone from the stream:
   rebuild projections (`sandloom projection rebuild`) and check consumers with
   side effects by hand.

Ship `sandloom security audit-export --since <yesterday> --action-prefix auth.
--apply --reason "daily off-site copy" --output …` off-site daily: it is the
block record that survives a database restore.

## Security incident runbook (database leak / credential leak / key compromise)

Every command below is a **dry-run by default**; `--apply` acts and needs
`--reason` (and `--operator`, default your login); in production also
`--confirm-production`. Each applied action is audited (per user by identity
with `actor_type="operator"`, or `core.security.*` for the export /
reconciliation). Re-running an interrupted bulk command with the same
`--operation-id` replays finished batches (durable command dedupe) instead of
repeating them. The session commands call identity's `identity.security.v1`
contract — run them in a process that loads identity (or binds it via
`SERVICE_BINDINGS` with an `RPC_ALLOWED_CALLS` grant); `--tenant` also needs
`tenancy.membership.v1`.

**0. Contain and record.** Open an incident, freeze deploys, then export the
audit window you will reason about:
`sandloom security audit-export --since 2026-10-20T00:00Z --output inc-audit.jsonl
--apply --reason "INC-42 evidence"` (sanitized metadata, newest first, one JSON
object per line; the SHA-256 is recorded in `core.security.audit_exported`).

**1. One account compromised.** Owner console or API: `POST
/api/v1/users/{id}/block` (emergency stop, ends sessions), investigate, then
`/unblock`. A user who lost the second factor: `POST
/api/v1/users/{id}/reset-2fa` (admin risk, admin only, never on yourself, audited
`identity.twofa.reset`, ends every session) — verify the person out of band
first: afterwards the password alone signs in until they re-enrol.

**2. Database leak** (dump, replica, backup copy exposed). Password hashes
(argon2id), encrypted TOTP secrets, hashed refresh tokens, PII and the audit log
are out.
1. `sandloom security force-password-reset --all` (dry-run), then `--apply --reason
   "INC-42 db leak"`: a password sign-in answers `403` (`X-Password-Reset-Required:
   true`) until the emailed reset flow — or a password change — clears it, and
   every session ends. Per tenant: `--tenant <org id>`; individuals: `--user`.
2. If `TWOFA_ENCRYPTION_KEY` may have leaked with it (same host / secret
   store), the TOTP secrets are readable: rotate the key (prepend a new key —
   `TWOFA_ENCRYPTION_KEY=new,old` decrypts old secrets and encrypts new ones) AND
   have users re-enrol (the leaked secrets stay valid until then; operators can
   `reset-2fa`).
3. Refresh tokens are stored hashed; sessions were ended in step 1 anyway.
4. Rotate the database credentials and every secret that was in the same
   environment as the dump.

**3. Credential leak** (one secret exposed: an `.env`, a CI variable).
- `DATABASE_URL` / `CACHE_URL` passwords: rotate at the server, redeploy.
- `JWT_SECRET` (HS*): anyone can mint tokens. Set a new `JWT_SECRET` + new
  `JWT_KID` and do NOT keep the old one in `JWT_PREVIOUS_SECRETS` — every
  outstanding access token is rejected at once (that is the point). Asymmetric
  (`JWT_PRIVATE_KEY`): `sandloom keys generate`, publish the new public key under a
  new kid in `JWT_PUBLIC_KEYS` on every service, remove the compromised kid,
  then switch the issuer. Afterwards `sandloom security revoke-sessions --all --apply`
  so refresh tokens minted meanwhile die too. Note `JWT_SECRET` also keys
  identity's emailed reset / verification links: rotating it invalidates
  outstanding links (users request new ones).
- `INTERNAL_API_TOKEN` / `METRICS_TOKEN`: rotate, redeploy every service
  (identical everywhere). The internal token guards the ops endpoints only;
  it is never an RPC credential.
- RPC keys (`RPC_PRIVATE_KEY`, `RPC_DH_PRIVATE_KEY`): `sandloom keys generate
  --purpose rpc` / `--purpose rpc-dh` for the affected service, publish the new
  public key in `RPC_SERVICE_KEYS` / `RPC_SERVICE_DH_KEYS` everywhere (new key
  id), redeploy, then remove the old id. A leaked PROVIDER X25519 key derives
  every caller's pair key — commands stay safe only with
  `RPC_COMMANDS_REQUIRE_SIGNATURE` on.
- `TWOFA_ENCRYPTION_KEY`: as in 2.2.
- `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET`: roll in the Stripe dashboard,
  redeploy.

**4. Tenant-scoped compromise.** `sandloom security revoke-sessions --tenant <org>
--apply --reason …` (pages the members through `tenancy.membership.v1`), plus
`force-password-reset --tenant` if their credentials are suspect.

**5. Close.** `sandloom backup reconcile-security` must report clean; export the
final audit window for the incident record; schedule the follow-ups (key
rotation drills, a restore drill with `sandloom backup verify`).

## Data retention & PII

- **TOTP secrets** are encrypted at rest (`TWOFA_ENCRYPTION_KEY`); **refresh
  tokens** are stored hashed.
- **Soft delete**: tenancy organizations soft-delete (`DELETE`, owner only,
  sets `is_active=false` and publishes `tenancy.organization_deleted.v1`);
  soft-deleted rows persist
  until a purge job removes them. Define a retention window per data class and
  run a scheduled purge (Celery beat) — there is no automatic purge yet.
- **GDPR / right-to-erasure**: implement per-module erasure that removes or
  anonymizes PII for a subject id; the audit log retains the *fact* of actions
  (action, actor, timestamp) without sensitive payloads — keep PII out of
  `record_audit(..., data=...)`.
- **Audit log retention**: append-only; size it and archive/prune on a policy
  (e.g. ship to cold storage after N months). The framework never deletes
  audit rows itself — retention is a compliance decision (unlike the outbox,
  whose delivered rows `OUTBOX_RETENTION_HOURS` purges).

## Audit log

Security-sensitive actions are recorded via `record_audit()` (see AGENTS.md →
"Audit"). Entries store actor, action, resource, request_id and trace_id, and
commit within the audited transaction; security audits use `required=True`, so
a missing UoW is an error rather than a dropped row. Query the `audit_log`
table for who-did-what investigations.

## Redis cache: timeouts, pool, revocation cache

- Every cache client is bounded: `CACHE_SOCKET_TIMEOUT_SECONDS` /
  `CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS` (0.5 s), keepalive, a 30 s health
  check, a blocking pool of `CACHE_MAX_CONNECTIONS` (64) per process with
  `CACHE_POOL_TIMEOUT_SECONDS` (1 s) and one retry of connection errors. A
  dead or hung Redis therefore makes cache operations fail in about a second,
  and each caller's policy applies: the rate limiter fails open (or 503 with
  `fail_closed=True`), token revocation follows `AUTH_REVOCATION_FAIL_CLOSED`,
  the idempotency guard follows its `fail_closed`, and the login lockout
  answers 503 in production. Size `CACHE_MAX_CONNECTIONS` x workers below the
  Redis `maxclients`. The event-bus transport uses its own client without the
  read timeout, because blocking stream reads wait longer than that.
- Token revocation applies on the next request everywhere: every
  authenticated request reads the subject's session record
  (`sandloom:auth:subject:{id}`) live from Redis, one `GET` memoised per request.
  There is no per-route freshness setting and no per-process copy
  (ADR-0009).
- **Emergency block** of a compromised account: `block_user(user_id,
  reason=..., actor_id=...)` inside a UoW (audited). It is read live on every
  authenticated request, so it applies on the next request everywhere — also
  to tokens minted later — until `unblock_user(...)`. Login, SSO and refresh
  are refused while blocked, and the block revokes the user's access and
  refresh tokens, so after unblocking the user signs in again. A blocked
  subject record (`sandloom:auth:subject:{user_id}`) has no TTL: run Redis with a `noeviction` (or
  `volatile-*`) policy so it cannot be evicted. With Redis down, prod answers
  503 on every authenticated route (the block cannot be confirmed).

## Password hashing: work factor & capacity planning

Passwords are hashed with argon2id (16-byte salt, 32-byte digest — fixed).
The cost parameters are configuration:

| Setting | Default | Meaning |
|---|---|---|
| `PASSWORD_HASH_PROFILE` | `strong` | preset: `strong` (m=65536 KiB, t=3, p=4 — the historical default), `owasp_min` (m=19456 KiB, t=2, p=1), `custom` (all three values below required) |
| `PASSWORD_HASH_TIME_COST` | preset | iterations `t` (overrides the preset) |
| `PASSWORD_HASH_MEMORY_KIB` | preset | memory `m` in KiB (overrides the preset; ≥ 8 × p) |
| `PASSWORD_HASH_PARALLELISM` | preset | lanes `p` (overrides the preset) |
| `SANDLOOM_ACCEPTED_RISKS` | `[]` | `weak_password_hash` is the prod opt-out of the OWASP floor (logged loudly at every start) |
| `PASSWORD_HASH_CONCURRENCY` | min(4, CPUs) | concurrent hash/verify per process (bounded pool) |

**Floor.** `owasp_min` is the OWASP Password Storage Cheat Sheet argon2id
minimum — "19 MiB of memory, an iteration count of 2, and 1 degree of
parallelism" — and the cheat sheet lists equal-defence equivalents
(m=47104/t=1, m=12288/t=3, m=9216/t=4, m=7168/t=5, all p=1;
<https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html>).
A configuration passes when it is at least one of them in both memory and
iterations. Below it, a production process refuses to start (settings
validation error naming the fix) unless
`SANDLOOM_ACCEPTED_RISKS=["weak_password_hash"]`;
outside prod weak values are allowed with a startup warning (fast tests).

**CPU per login** (one argon2id verify; in-process, single caller, 200
verifies, Apple M1 Max — measure on your hardware before sizing):

| Preset | Params | CPU / verify | Wall / verify | Peak memory / op |
|---|---|---|---|---|
| `strong` (default) | m=65536, t=3, p=4 | ~120 ms | ~32 ms (4 lanes in parallel) | 64 MiB |
| `owasp_min` | m=19456, t=2, p=1 | ~21 ms | ~21 ms | 19 MiB |
| custom (OWASP equivalent) | m=47104, t=1, p=1 | ~26 ms | ~26 ms | 46 MiB |

Registration, password change/reset and the timing equalizer for unknown
emails cost the same as one verify. On the integration stand `strong` was
~53% of the mixed profile's CPU and ~75% of the write-heavy one: a login-heavy
deployment sizes CPU for `logins/s × CPU per verify`; `owasp_min` cuts that
~5–6×. `PASSWORD_HASH_CONCURRENCY` bounds how many run at once (and so the
memory: concurrency × m).

**Changing the setting is a rolling, transparent migration.** Every stored
hash embeds its own parameters, so it keeps verifying. After a successful
login (password, 2FA and block checks passed) identity re-hashes a hash whose
parameters differ from the configured ones — stronger or weaker — and stores
it in the same Unit of Work; dormant accounts keep their old hash until they
sign in. A rehash failure never blocks the login: it is logged (no secret
material) and counted in `sandloom_password_rehash_total{outcome="failed"}`
(successes: `outcome="rehashed"`). Keep the setting identical in every
process that runs identity.

## Garbage collector tuning

A full cyclic collection (generation 2) traverses every tracked object in
the process and stops the event loop while it runs. In a Sandloom worker about
200k of those objects are the startup heap: modules, classes, SQLAlchemy
and pydantic metadata, the DI graph. Under load, request objects that live
across awaits get promoted, so full collections arrive every few seconds.
Before tuning, the Oracle 2-vCPU stand spent 6–14% of CPU in GC, at
~136 ms per full collection. Measurements and sources:
`docs/design/gc-tuning-2026-10-23.md` (internal research, retained in the original private repository).

| Setting | Default | Meaning |
|---|---|---|
| `GC_FREEZE_AFTER_STARTUP` | `true` | `gc.collect(); gc.freeze()` once per process after startup: the startup heap is never traversed again. A full collection then costs ~2 ms of CPU instead of ~35 ms on the local stand |
| `GC_THRESHOLDS` | `2000,10,10` | `gc.set_threshold(...)` at the same moment (`a,b,c` or a JSON list; empty = interpreter default). 2000 is CPython 3.13+'s own default; 3.12's 700 is crossed by every burst of in-flight request objects. Bounds: threshold0 100–1,000,000, others 1–10,000. Ignored with a warning on CPython 3.14.0–3.14.4 (incremental GC) |
| `GC_METRICS_ENABLED` | `true` | `sandloom_gc_pause_seconds{generation}` (histogram; `_count` = collections), `sandloom_gc_collected_objects_total{generation}`, `sandloom_gc_frozen_objects` |

**Where it applies.**
- **gunicorn worker:** applied by the `post_worker_init` hook (`sandloom.core.cli.gunicorn_conf`)
  when the app reports `lifespan.startup.complete`, so the freeze includes
  pools and consumers and happens before the worker accepts a connection.
- **Celery worker:** applied by `sandloom.core.worker` in the parent before the pool
  forks (`worker_init`); children inherit the frozen heap and the thresholds.
- **Never in `create_app`:** tests and a plain `uvicorn sandloom.main:app` run
  untuned. A custom server entry point applies it with
  `sandloom.core.gc_tuning.GcAfterStartup(app, GcConfig.from_settings())`.

**Tuning.**
- With the defaults, GC is ~0.2% of worker CPU on the local stand. On slower
  cores, more requests are in flight per worker at the same rate. If
  `rate(sandloom_gc_pause_seconds_count{generation="0"}[1m])` stays above ~10/s per
  worker, try `GC_THRESHOLDS=10000,10,100`.
- Accept it only if worker memory (`container_memory_working_set_bytes`)
  stays flat over a 30-minute soak. Higher thresholds keep cyclic garbage
  alive up to one cycle longer, and an application that stores exceptions
  (traceback → frames → locals) retains more per cycle.
- Do not disable automatic collection or add periodic `gc.collect()`: with
  the freeze it only adds work.

**Signals.**
- `histogram_quantile(0.99, sum by (le, generation) (rate(sandloom_gc_pause_seconds_bucket[5m])))`:
  expect generation 2 well under 10 ms after the freeze.
- `sandloom_gc_frozen_objects` is set once per worker. It is roughly constant; it
  is not updated when frozen objects die.
- A growing worker RSS with flat request mix means a leak. Retained cycles
  show up as `sandloom_gc_collected_objects_total` rising with the thresholds
  raised. Bound workers with gunicorn `max_requests` + `max_requests_jitter`
  and Celery `WORKER_MAX_MEMORY_PER_CHILD` as usual.

**Kill switch:** `GC_FREEZE_AFTER_STARTUP=false` and `GC_THRESHOLDS=` (empty)
restore the interpreter's own behaviour exactly.

## Observability

- **Logs**: JSON in production (`LOG_FORMAT=auto`), each line carries
  `request_id` and `trace_id`. Ship to your log pipeline.
- **Internal endpoints** (`/metrics`, `/diagnostics/*`, `/contracts/*`,
  health details) require `X-Internal-Token: $INTERNAL_API_TOKEN`
  and are closed when no token is configured (`INTERNAL_ENDPOINTS_OPEN_IN_DEV`
  opens them outside prod only). Give Prometheus `METRICS_TOKEN`, a
  scrape-only credential. `/_sandloom/rpc` is never opened by either: it accepts
  only pair-MAC authenticated requests (ADR-0004).
- **Metrics**: Prometheus at `/metrics` (token-gated). HTTP RED metrics
  (`sandloom_http_requests_total`, `sandloom_http_request_duration_seconds`) plus event
  bus, outbox relay, lifecycle, and schema metrics. Operational metrics and
  what to alert on: [Operational metrics](#operational-metrics) and
  [Alerts](#alerts) below.
- **Dashboards, alert rules, SLOs**: `deploy/observability/` ships
  provisioned Grafana dashboards (HTTP RED, outbox & relay, consumers &
  streams, revocation & security, DB pools, internal RPC, runtime & GC, SLOs),
  the Prometheus alert and SLI recording rules, and a compose overlay that runs
  Prometheus + Grafana next to the monolith profile (`--profile
  observability`, not started by default) — see `deploy/observability/README.md`.
  Import the JSON / rule files into your own stack the same way. Example SLOs
  with error-budget arithmetic and starting targets measured on a 2 vCPU
  stand: `docs/reference/slo.md`. The Celery worker exports no metrics yet.
- **Traces**: set `TRACING_URL` to your OTLP endpoint (TLS auto for https).
- **Errors**: set `SENTRY_DSN` to enable Sentry reporting.
- **Health**: `/health` (operational health, 503 when degraded),
  `/health/simple` (process liveness), `/ready` (health + required modules +
  required consumer subscriptions; topology shown only with the internal token),
  `/diagnostics/infra` (capabilities, stability, drivers, per-instance health),
  `/diagnostics/consumers` and `/diagnostics/services` (token-gated). Results are cached for
  `HEALTH_CACHE_TTL_SECONDS` (default 3s) so anonymous probes cannot fan out
  infra checks.
- **Client IP**: behind a proxy set `TRUST_PROXY_HEADERS=true` and
  `TRUSTED_PROXY_IPS` (CIDRs); `X-Forwarded-For` is then walked right-to-left
  from a trusted peer. Rate limiting (sliding window) is always on in prod.

### Operational metrics

Under Gunicorn every metric is aggregated across the live workers
(`PROMETHEUS_MULTIPROC_DIR`, see `sandloom.core.cli.gunicorn_conf`): counters and
histograms are summed; a gauge one process owns (the relay leader, a Streams
group heartbeat) is `livemostrecent`; per-process quantities (pool
connections) are `livesum` — the service total. A dead worker's gauges
disappear with it. None of these metrics adds work to the request path beyond
one histogram observation (revocation check, RPC call); the pool gauges are
sampled every 5 s by a background task, the outbox age by the relay every
10 s, the Streams ages by the 30 s group heartbeat.

| Metric | Type, labels | Meaning |
|---|---|---|
| `sandloom_outbox_oldest_pending_age_seconds` | gauge, `database` | Age of the oldest undelivered, non-dead outbox row (0 = none). One indexed `min(emitted_at)` on `ix_outbox_messages_relay` per relay pass, also while the broker circuit is open. Exported only where the relay runs. |
| `sandloom_streams_group_oldest_unacked_age_seconds` | gauge, `stream`, `group` | Age of the oldest entry the group has not acknowledged: its oldest pending entry, else the first undelivered one (from the entry id; one `XPENDING` or `XRANGE … COUNT 1` per heartbeat). |
| `sandloom_streams_group_lag` / `sandloom_streams_group_pending` | gauge, `stream`, `group` | Entries not yet delivered / delivered but unacked (existing). |
| `sandloom_event_end_to_end_latency_seconds` | histogram, `consumer` | `emitted_at` (the outbox row's timestamp, carried exactly in the envelope) → the consumer handler's successful commit. Covers outbox wait, relay, broker, redelivery and the handler. Clock skew between the producer's database and the consumer host adds to it. |
| `sandloom_event_consumer_handler_duration_seconds` | histogram, `consumer` | One successful delivery, its UoW included. |
| `sandloom_db_pool_connections` | gauge, `database`, `pool`, `state` | `checked_out` / `idle` connections; `pool` = `main` or `contract`. |
| `sandloom_db_pool_max_connections` | gauge, `database`, `pool` | `pool_size + max_overflow` (summed over workers). |
| `sandloom_db_pool_overflow` | gauge, `database`, `pool` | Open connections beyond `pool_size`. |
| `sandloom_db_pool_checkout_waiters` | gauge, `database`, `pool` | Checkouts queued at the pool's fair gate — the pool is exhausted. A 5 s sample: short bursts show in request latency first. |
| `sandloom_cache_operation_errors_total` | counter, `instance`, `operation` | Redis cache operations that failed (connection, timeout, server error); caller errors are not counted. `instance` includes `SECURITY_CACHE_INSTANCE`. |
| `sandloom_auth_revocation_check_duration_seconds` | histogram, `mode` | The request's live session-record check (`mode` is always `strict`); per-request memo hits are not timed. |
| `sandloom_rate_limit_rejected_total` | counter, `route` | `429` answers of `rate_limiter` (route template, `unmatched` for 404s). |
| `sandloom_rate_limit_unavailable_total` | counter, `outcome` | Rate-limit checks without a cache answer: `open` (allowed) / `closed` (503). |
| `sandloom_rpc_client_request_duration_seconds` | histogram, `peer`, `outcome` | One internal RPC attempt (`ok` = an answer arrived, whatever its status; `timeout`; `error`). `peer` is the binding's base URL. The retry delay is not counted. |
| `sandloom_multi_store_partial_commit_total` | counter, `route`, `store` | An `allow_multi_db_write` request left partially committed (best-effort 1PC): `store` committed, then a later commit of the request failed. `route` is the route template; the `uow.partial_commit` warning names the committed stores and the failing one. A retry with the same `Idempotency-Key` converges it ([write admission](../concepts/write-admission.md) rule 4). Counted only on a failed commit. |
| `sandloom_http_untrusted_forwarded_headers_total` | counter | Requests whose client IP was resolved while carrying `X-Forwarded-For` with `TRUST_PROXY_HEADERS` off (the warning is logged once; this counts every one). |

### Alerts

The alert rules ship as Prometheus rule files in
`deploy/observability/prometheus/rules/` — **they are the single source of
truth for every expression and threshold**; this table is the index from an
alert to its runbook (each rule's `runbook_url` points to the section named
here). `src/sandloom/core/tests/test_observability_bundle.py` keeps the
table and the rules in step (same names, same severities) and checks every
metric and label they use exists in the kernel. Thresholds are starting
points: tune them in the rule file to your traffic and SLOs.

Severity: **page** — someone acts now; **warning** — a ticket for business
hours; **info** — context for an investigation. Rules aggregate `by (job)`,
so the same file serves the monolith and every service of the microservices
profile. The cache instance label is scraped as `exported_instance`
(`honor_labels: false`); `SandloomSecurityCacheErrors` matches
`exported_instance="security"` — rename it if your `SECURITY_CACHE_INSTANCE`
differs.

| Alert | Severity | Fires when | First step / runbook |
|---|---|---|---|
| `SandloomOutboxBacklogAgeing` | page | Oldest undelivered outbox row > 60 s for 5m | Broker down (`outbox_relay_circuit_open == 1`), relay not running (`outbox_relay_last_run_timestamp` stale), or a backlog it cannot drain. [Events](#events-outbox-relay-and-dead-letters). |
| `SandloomOutboxBacklogAgeingWarning` | warning | Same, > 10 s for 5m | Early warning for the above. |
| `SandloomOutboxRelayAbsent` | warning | A job reports `sandloom_startup_duration_seconds` but no outbox age for 10m | No process runs the relay (`OUTBOX_RELAY_ENABLED`, Gunicorn leader election). |
| `SandloomOutboxDeadLettered` | warning | Any outbox row dead-lettered in 15m | `sandloom outbox list-dead` / `requeue`. [Events](#events-outbox-relay-and-dead-letters). |
| `SandloomConsumerGroupBehind` | page | A group's oldest unacked entry > 300 s for 10m | A consumer is down, failing or too slow; stream retention (`EVENT_STREAM_RETENTION_SECONDS`) will drop what it has not read. `sandloom streams consumers`. |
| `SandloomEventDeliverySlow` | warning | p99 end-to-end event latency > 30 s for 10m | Compare the outbox age (producer side) with the group age (consumer side) to locate the lag. |
| `SandloomConsumerFailures` | warning | A consumer's handler fails continuously for 10m | Redeliveries pile up; poison entries end in `<channel>.dlq`. |
| `SandloomStreamsDeadLettered` | warning | Any Streams entry dead-lettered in 15m | `sandloom streams dlq-list`, then a targeted `dlq-replay` once fixed. |
| `SandloomDbPoolSaturated` | warning | Checked-out / capacity > 90 % for 5m | Raise `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` (mind PostgreSQL `max_connections`), or find long transactions. |
| `SandloomDbCheckoutsQueueing` | warning | Checkout waiters seen in every 5m window for 10m | Requests wait for a connection (write p99 grows; `DB_POOL_TIMEOUT` → 500s next). |
| `SandloomSecurityCacheErrors` | page | Security cache errors > 0.1/s | Revocation / block / idempotency state unreachable: 503s (fail-closed) or unchecked revocation (fail-open). [Redis cache](#redis-cache-timeouts-pool-revocation-cache). |
| `SandloomCacheErrors` | warning | Other cache instances' errors > 0.1/s | Redis unreachable or timing out; rate limiting falls back to its policy. [Redis cache](#redis-cache-timeouts-pool-revocation-cache). |
| `SandloomRevocationCheckSlow` | warning | p99 revocation + block check > 10 ms for 10m | Every authenticated request pays it; check the security Redis (latency, `appendfsync`). |
| `SandloomRevocationsDeferred` | warning | `auth_revocation_deferred_total` grew in 5m | Post-commit revocation writes failing (cache outage); the durable applier catches up. [Secrets management](#secrets-management). |
| `SandloomRevocationApplierFailing` | page | The durable applier fails for 10m | Revoked tokens may still be accepted. Fix the security cache. [Secrets management](#secrets-management). |
| `SandloomRevocationImmediateRefused` | warning | A `require_immediate` change was refused (503) in 5m | The cache is unreachable for security changes. [Secrets management](#secrets-management). |
| `SandloomRevocationCorruptMark` | warning | An unparsable revocation mark in 15m | That user is locked out until the next login; investigate the writer. |
| `SandloomRevocationPending` | warning | A durable revocation pending > 60 s | The identity replica answers `/ready` 503 until it drains. [Secrets management](#secrets-management). |
| `SandloomRevocationPendingCritical` | page | Pending > 300 s | A revoked token may still be accepted. [Secrets management](#secrets-management). |
| `SandloomRevocationMalformed` | page | A revocation event that can never apply (5m) | Find the producer, revoke by hand, drop the DLQ entry only after the fix. [Secrets management](#secrets-management). |
| `SandloomIdempotencyPersistFailures` | warning | An idempotency terminal record was lost in 15m | A retry may re-execute a completed operation; exactly-once effects need `durable_inbox`. |
| `SandloomMultiStorePartialCommit` | warning | A multi-store request partially committed in 15m | The `uow.partial_commit` log names the route, the committed stores and the failing store; the client's keyed retry converges it, an unretried request leaves the secondary's write orphaned. [Write admission](../concepts/write-admission.md) rule 4. |
| `SandloomTenantRlsNonTenantViolations` | warning | A statement outside a `non_tenant_statements()` allowlist in 15m | Find the statement in the logs; fix the allowlist or the code. |
| `SandloomRateLimiterBlind` | warning | A rate-limit check without a cache answer in 5m | Limits not enforced (`open`) or endpoints answer 503 (`closed`). |
| `Sandloom429Spike` | info | > 1 rejected request/s on a route | Abuse or a too-tight limit; with untrusted forwarding every client may share a proxy's bucket. |
| `SandloomProxyHeadersUntrusted` | warning | `X-Forwarded-For` seen with `TRUST_PROXY_HEADERS` off in 1h | Rate limits key on the proxy's address. Set `TRUST_PROXY_HEADERS=true` + `TRUSTED_PROXY_IPS`, or strip the header at the edge. |
| `SandloomTargetDown` | page | `up == 0` for 5m | Process down, or `METRICS_TOKEN` mismatch (`/metrics` answers 404). [Observability](#observability). |
| `SandloomRpcPeerSlow` | warning | p99 of successful RPC attempts to a peer > 250 ms for 10m | A provider service is slow. |
| `SandloomRpcTimeouts` | warning | RPC timeouts to a peer for 5m | A timed-out command may have committed; callers retry with the same operation key. |
| `SandloomRpcAuthClockSkew` | warning | An accepted RPC request beyond half the auth window in 15m | Fix NTP before skew exceeds `RPC_AUTH_MAX_SKEW_SECONDS` and calls are refused. |
| `SandloomRpcResponseBindingRejected` | warning | An RPC response rejected as unbound to its request in 15m | Mixed-up connection, key misconfiguration or tampering. |
| `SandloomSagaParked` | warning | A saga instance parked in 15m | `sandloom sagas list`, then `sandloom sagas show <type> <id>` (why + the fixed remediation). [Sagas](#sagas). |
| `SandloomGcGen2PausesLong` | info | Full-GC p99 pause > 25 ms for 30m | [Garbage collector tuning](#garbage-collector-tuning). |
| `SandloomApiReadAvailabilityBurnFast` | page | Read 5xx ratio > 14.4 × the example 0.1 % budget over 1h and 5m | `docs/reference/slo.md` → Burn-rate alerts. |
| `SandloomApiReadAvailabilityBurnSlow` | warning | > 6 × the budget over 6h and 30m | Same. |
| `SandloomApiWriteAvailabilityBurnFast` | page | Write 5xx ratio > 14.4 × budget over 1h and 5m | Same. |
| `SandloomApiWriteAvailabilityBurnSlow` | warning | > 6 × the budget over 6h and 30m | Same. |
