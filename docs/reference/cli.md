---
title: CLI, Settings & Local CI
---

# CLI, settings and local CI

Every `sandloom` command, the configuration tiers, accepted risks, the key settings
and the local CI runner. Run the commands from the application root (or
the framework repository root) with `uv run sandloom ...`. Every command
has `--help`.

## Project and code

| Command | What it does |
| --- | --- |
| `sandloom new <app> [--with identity,tenancy,billing \| --no-batteries] [--module <name>] [--dir <path>]` | Generate a standalone, runnable application. The app depends on `sandloom[redis,celery]>=0.7.0a2,<0.8` plus `sandloom-<module>>=0.7.0a2,<0.8` per `--with` module. `--framework-req`, `--framework-source {pypi,git,path}` and `--framework-path` choose where the framework comes from (`path`: editable sources for exactly those distributions). |
| `sandloom add module <name>` | A new module under the app's `modules` package. It is discovered: nothing else to edit. |
| `sandloom add event <module> <name> [--field F]` | A typed `Event` in the module's `events.py`. |
| `sandloom add consumer <module> <channel> [--field F]` | An `@on` consumer of a channel (another module's event). |
| `sandloom add contract <module> <name>` | A `@contract` class and its DTO in the module's `schemas.py`; the stub and the DTO copy are generated. |
| `sandloom add saga <module> <name>` | A saga with a typed command step, plus tests. |
| `sandloom add projection <module> <name>` | A projection skeleton. |
| `sandloom add deploy <module>... [--service S] [--force]` / `--refresh` / `--check [--env-file F]` | Split into services: create (first run) or update the microservices profile and its gateway, derived from what the modules provide and consume; `--check` runs the split invariants (SANDLOOM-T014) and boots every service alone. [Split an app into services](../guides/split-into-services.md). |
| `sandloom docs [<name>] [--write \| --check]` | Print a public `sandloom.sdk` name's exact signature, tier and docstring; `--write` / `--check` refresh or verify the generated reference blocks. |
| `sandloom explain <code> [--list \| --markdown]` | Explain an `SANDLOOM-xxxx` error code or a check rule: why, and the fix. |
| `sandloom version` | Print the framework version. |

Every generator but `deploy` runs `sandloom generate` last, so the generated
files are current after scaffolding.
`sandloom add --root <path>` runs a generator against another project root. Each
generator has a how-to: [Add a module](../guides/add-module.md),
[Add an event](../guides/add-event.md),
[Add a consumer](../guides/add-consumer.md),
[Add a contract](../guides/add-contract.md),
[Add a saga](../guides/add-saga.md),
[Add a projection](../guides/add-projection.md).

## Tests and gates

| Command | What it does |
| --- | --- |
| `sandloom generate [<module> ...]` | Writes every generated file: each module's `module.snapshot.json`, `routes.snapshot.json` and `contracts.snapshot.json`, the contract stubs and the DTO copies (`<shared>/contracts/<module>_dto.py`, a wire-shape copy of each provider DTO a contract signature names) and the exported event schemas. Every file is compared with the committed one first: an incompatible change (SANDLOOM-T001, T004, T005, T007, C012) fails and nothing is written. A weaker route policy is written only by `sandloom routes snapshot --write`. Module names keep those modules' files only. Loads the project's `.env.test` like `sandloom test`; connects to no database. Every `sandloom add` generator runs it last, and `sandloom new` runs it once. |
| `sandloom test [<module>] [--full] [--no-gates] [-n N] [--plan] [--ci] [--seed N \| --no-shuffle]` | The gate: the module's tests first, then the fast suite in parallel (`-m "not slow" -n auto --dist loadfile`, `SANDLOOM_TEST_REDIS_DB_STRIDE=auto`; `--full` includes slow tests), then ruff, ruff format, mypy, `sandloom check`, the architecture tests, `lint-imports` and the contract and route snapshots. A failing step names its code. It never writes a generated file: a stale one fails with "generated files are stale: run `sandloom generate`" and the list. `--ci` also runs `ruff check --no-fix`. The test steps run in a seeded random order (printed; `--seed N` replays it, `--no-shuffle` keeps file order): [Test isolation](../guides/testing.md#test-isolation). |
| `sandloom check [--rules FILE] [--only rule,...] [--list-rules] [--boot]` | Static architecture analyzers against `./architecture_rules.yml` (exit `0` ok, `1` violations, `2` usage). `--boot` also builds the app through `create_app` and reports startup refusals and stale route snapshots. Outside this repository it needs `pip install "sandloom[check]"`. |
| `sandloom routes snapshot [--write \| --check] [--module M]` | Each module's route snapshot (`routes.snapshot.json`: method, path, authentication, resolved policy). `--check` fails on a stale file (SANDLOOM-T001); after a route change run `--write` and review the diff. `sandloom generate` never writes a weaker policy (spec §5.8): `--write` is its explicit approval and names each weakening it approves. |
| `sandloom routes reference` | Print the generated route reference. |
| `sandloom contracts snapshot [--write \| --check] [--module M]` | Each module's contract snapshot (SANDLOOM-T004). |
| `sandloom contracts check --against <snapshot\|dir\|url> [--as provider\|consumer] [--service S] [--timeout T]` | Rolling-upgrade compatibility of this build's contracts against a snapshot or a running provider. |

Never re-run a serial `pytest -q` after each fix; `sandloom test` runs the suite in
parallel. The harness and its isolation guards are described in
[Testing Modules](../guides/testing.md); every fixture in the
[testing reference](testing.md).

```bash
docker compose -f deploy/compose/test.yml up -d --wait   # test PostgreSQL :5433, Redis :6380
uv run python tools/checks/test_resources.py list|prune [--days 7] [--apply]
                            # shared test DBs / Redis indexes: owner, last use,
                            # connections; prune is a dry run without --apply and
                            # never touches a connected or recently used one
SANDLOOM_TEST_DATABASE_URL=postgresql+asyncpg://…/sandloom_test_mine uv run sandloom test
                            # run on YOUR OWN database: SANDLOOM_TEST_DATABASE_URL /
                            # SANDLOOM_TEST_CACHE_URL / SANDLOOM_TEST_BROKER_URL win over
                            # .env.test on every load path (a plain shell
                            # DATABASE_URL does NOT — .env.test overrides it)
uv run pytest src/sandloom/<module>/tests/test_x.py::test_y --no-cov   # one test
```

Parallel workers get database `<db>_gw<N>` and their own Redis indexes, which
needs a Redis started with `--databases 256`. In tests, reload `.env.test`
with `reload_test_env` (from `sandloom.sdk.testing`), never a bare
`load_dotenv(override=True)`. CI runs the slow suites on Python 3.11 and 3.12
only (3.13 / 3.14: `-m "not slow"`); the pre-push hook runs everything.

## Migrations

Each module owns its own chain; the core chain holds the shared
infrastructure tables. See [Migrations](../concepts/migrations.md) and
[Write a migration](../guides/write-migration.md).

| Command | What it does |
| --- | --- |
| `sandloom migrate new <module> "<message>" [--database D] [--rev-id R]` | Autogenerate a revision in ONE module's chain and add row-level security for each new `TenantScoped` table. The module's database must be at its chain head. |
| `sandloom migrate revision <module> -m "<message>" [--autogenerate] [--database D] [--rev-id R]` | A revision in one module's own chain (its `migrations/`, version table `alembic_version_<chain>`, its database). |
| `sandloom migrate upgrade-all [--apply] [--lock-timeout S] [--create-databases]` | Core chain → every hosting database, then each module's chain → its database (exit 1 on failure). Waits up to `S` (default `MIGRATION_LOCK_TIMEOUT_SECONDS`, 300) for another runner's advisory lock and logs the holder. `--create-databases` first creates a missing PostgreSQL database (role with `CREATEDB`; the microservices one-shots). |
| `sandloom migrate upgrade-core --database D --apply` | The core chain → a named database. |
| `sandloom migrate upgrade-module <module> --apply` | One module's chain → its database(s). |
| `sandloom migrate databases` | List the configured database instances. |
| `sandloom migrate plan` | Show the migration plan (dry run). |
| `sandloom migrate ensure-outbox --all-databases --apply` | DEV/REPAIR only: create the shared core tables (outbox, audit, consumer inbox, command log, HTTP idempotency log) per database. Refused in prod without `--force` (no drift detection) and the accepted risk `unversioned_ddl_repair`; production uses `upgrade-core`. |
| `sandloom migrate extract-module <module> --from <src> --to <dst> [--apply --i-have-stopped-writes] [--verify \| --drop-source \| --unfence [--force]]` | Move ONE module's existing data out of a shared database: fence, copy, verify, outbox/inbox hand-off, consumer-group owner transfer. Dry run by default; offline. Runbook: `deploy/README.md`. |

## Operations

| Command | What it does |
| --- | --- |
| `sandloom outbox list-dead \| requeue \| purge [--database D] [--channel C] [--id ID \| --all] [--limit N]` | Inspect dead-lettered outbox events, return them to pending, or delete delivered events older than the retention window. |
| `sandloom streams dlq-list \| dlq-replay <channel> [--id ID] [--group G]` | List or re-publish a channel's Streams dead-letter entries. |
| `sandloom streams groups <channel> \| consumers [--all] \| release-group <channel> <group>` | Consumer groups of a channel, the subscription inventory, or forget a group's owner fingerprint. |
| `sandloom projection list \| rebuild <name> [--tenant <id>]` | List registered projections and consumers; rebuild one from its source. |
| `sandloom sagas list [--all] \| show <type> <id> \| retry <type> <id> \| park <type> <id> \| resolve <type> <id> --step S --as applied\|skipped` | Operate saga instances. Dry run unless `--apply --reason R` (prod: also `--confirm-production`). |
| `sandloom security revoke-sessions \| force-password-reset (--all \| --tenant ORG \| --user ID) [--operation-id ID]` | Incident response: end every session of the targets, or require a password reset. Dry run unless `--apply --reason R` (prod: also `--confirm-production`); `--operation-id` resumes an interrupted run. |
| `sandloom security audit-export --since T [--until T] [--action-prefix P] --output FILE` | Export an audit-log window as JSONL (sanitized metadata). |
| `sandloom backup plan \| verify <dump> \| reconcile-security` | What to back up per database and Redis endpoint; restore a dump into a scratch database and check it; re-derive blocks and revocation marks after a security-cache restore. |
| `sandloom keys generate [--alg EdDSA\|RS256\|ES256] [--purpose jwt\|rpc\|rpc-dh] [--service S] [--kid K] [--out-dir DIR]` | Generate an access-token signing pair, or a service's internal-RPC keys. |
| `sandloom keys allowlist` | Derive `RPC_ALLOWED_CALLS` from `SERVICE_BINDINGS`. |
| `sandloom keys dev-env [--deploy-dir D] [--out F]` | DEVELOPMENT / CI ONLY: a compose `--env-file` with fresh keys for every variable the microservices profile requires. |
| `sandloom config dump [--json] [--only PREFIXES] [--non-default] [--show-secrets]` | The effective settings with their source (env, `.env`, `SECRETS_DIR`, default); secrets redacted unless `--show-secrets` (refused in prod). |
| `sandloom doctor [--json] [--skip S] [--timeout T] [--lag-warn N]` | Read-only deployment preflight: config, modules, infrastructure reachability, migration heads, consumer lag, RLS, sagas (exit `0` ok, `1` warnings, `2` failures). |
| `sandloom admin export --output FILE [--check]` | Compile the static admin UI manifest; `--check` fails on drift. |

Runbooks: [Operations](../operations/runbook.md), `docs/operations/operator-reference.md`.

```bash
uv run uvicorn <app>.asgi:app --reload                         # API on :8000 (needs .env)
uv run celery -A sandloom.core.worker worker|beat --loglevel=INFO   # background tasks
```

## Framework dependency sources

Generate apps from an existing checkout with
`uv run sandloom new myapp --framework-source path --framework-path .`.
The path is the repository root containing `packages/`. Editable host paths
work locally; they are not portable Docker or CI dependencies.
`--framework-source pypi` installs published distributions and is the default
for the installed CLI. `--framework-source git` is disabled; use a local
checkout for source development. The canonical repository is
[Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom). See
[getting started](../getting-started/index.md).

## Local CI (GitHub Actions stand-in)

Run from the repository root. It exports the ref with `git archive` (never
your working tree), runs the real `ci.yml` / `docs.yml` /
`architecture-check.yml` jobs under `act` where safe and the service-backed
jobs (backend-tests ×4, benchmarks-security, examples, microservices-smoke)
in its own fallback runner, each with fresh run-id-named Postgres/Redis on an
isolated network (never the shared `sandloom_*_test` containers). backend-tests
runs the full suite on Python 3.11 and 3.12 (coverage on 3.12); 3.13 and 3.14
run `-m "not slow"`. It holds `/tmp/sandloom-ci-local.lock` while running (CPU
benchmarks wait for it); logs and summary in `.ci-local/runs/<run-id>/`;
exit 0 = green. Details: [Local CI runner](../contributing/local-ci.md).

```bash
tools/ci/ci_local.sh <git-ref> [--fetch] [--jobs a,b] [--python 3.11,3.13] [--parallel N]
tools/ci/ci_local.sh pr:<N>                      # a PR head from origin
tools/ci/ci_local.sh <git-ref> --rerun-failed    # only the jobs that failed last
                            # time for that ref (fail-fast loop; never a merge gate)
# jobs run in parallel by default (min(3, docker CPUs//3, mem//2.5GB));
# --parallel 1 = sequential
tools/ci/ci_local.sh --list | --check            # jobs + engines | workflow drift check
# --report-pr <N> posts the summary on the PR via gh (only when asked)
```

`uv sync` installs the `dev` dependency group (ruff, mypy, pytest — pinned
exactly in `pyproject.toml` and locked in `uv.lock`); CI runs
`uv sync --frozen` and the hooks `uv run --locked`, so a stale `uv.lock` fails
instead of re-resolving. Bump a tool by editing its pin, `uv lock`, and fixing
its new findings in the same change.

## Configuration tiers and accepted risks

Every `Settings` field is classified in the kernel's tier table
(`src/sandloom/core/config/tiers.py`): *semantic* (changes a guarantee —
its value is chosen per profile dev / test / prod / prod +
`multi_tenant_strict` in one table, see
[Configuration Tiers](configuration-tiers.md)) or *ops* (capacity /
operations). A new setting must be classified
(`src/sandloom/core/tests/test_config_tiers.py`); prefer an ops knob or a
profile-chosen value over a new semantic flag — the semantic count is
ratcheted (ceiling 15) and a new one needs an ADR. Overrides of the profile
are logged at startup and listed by `/diagnostics/core` and `sandloom doctor`.

**Every production weakening is an accepted risk, never a new boolean**
([ADR-0006](../adr/0006-accepted-risks.md)): `SANDLOOM_ACCEPTED_RISKS` (JSON or
comma list) names stable ids (`fake_psp_in_prod`, `log_notifier_in_prod`,
`local_storage_in_prod`, `volatile_event_bus`, `unversioned_ddl_repair`,
`weak_password_hash`, `shared_hs_secret_split`, `relay_disabled`,
`command_dedupe_below_retry_horizon`, `saga_horizon_beyond_command_dedupe`).
An unknown id fails startup, and each accepted risk is logged at every start,
exported as `sandloom_accepted_risk_active{risk}` and listed by
`/diagnostics/core` / `sandloom doctor`. Removed settings (among them the former
`*_ALLOW_*` booleans, `RPC_AUTH_MODE`, `RPC_RESPONSE_BINDING` and the former
per-route revocation settings) fail startup unless set to their inert value.

## Key settings

Every knob is a `Settings` field (validated at startup, `.env` /
`SECRETS_DIR`-aware); the full list with defaults is the
[Configuration Reference](configuration.md). Framework code never reads
configuration with `os.getenv` (`SANDLOOM_COMPOSITION_ROOT` is read from the
process env first, then from `Settings` / `.env`; only `SANDLOOM_WORKER_TASKQUEUE`
is raw env).

- **Modules.** `SANDLOOM_MODULES` (JSON list) selects the modules a process loads;
  without it the composition root loads its defaults (an `sandloom new` app: every
  module of its `modules` package plus the chosen module packages;
  `sandloom.main:app`: every installed `sandloom.modules` entry point). An entry
  is a module name (`identity`) or an import path (`sandloom_identity`).
  `MODULE_REQUIRED` (load failure aborts startup; when empty, every module
  listed EXPLICITLY in `SANDLOOM_MODULES` is required — list the required subset in
  `MODULE_REQUIRED` instead). See
  [Loader Configuration](../guides/loader-configuration.md).
- **Services.** `SERVICE_BINDINGS` (strictly validated; `"required": true`
  makes `/ready` — never `/health` — fail while that peer is unreachable;
  `SERVICE_PROBE_TIMEOUT_SECONDS` caps each concurrent probe).
- **Events.** `EVENT_BUS_BACKEND` (auto/memory/redis/streams; auto = durable
  `streams` in prod, `memory` elsewhere; the volatile `memory` / `redis`
  (Pub/Sub) drivers are refused in prod unless the risk `volatile_event_bus`
  is accepted; required/durable consumers on Pub/Sub also need it outside
  prod), `OUTBOX_RELAY_ENABLED` (auto/on/off/delegated; auto = prod only;
  under gunicorn only one elected worker runs it, the others get `delegated`;
  `off` in a prod app process is refused unless the risk `relay_disabled` is
  accepted), `OUTBOX_RELAY_MAX_ATTEMPTS`,
  `OUTBOX_RELAY_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` (per-row retry backoff),
  `OUTBOX_RELAY_CIRCUIT_BASE_SECONDS` / `_MAX_SECONDS` (broker-down cool-down
  that does not burn attempts), `OUTBOX_RETENTION_HOURS` (the relay purges
  delivered rows; `sandloom outbox purge` on demand),
  `CONSUMER_STARTUP_TIMEOUT_SECONDS`, `CONSUMER_RETRY_BASE_SECONDS` and
  `CONSUMER_RETRY_MAX_SECONDS` (required-consumer startup/reconnect policy),
  `EVENT_STREAM_READ_COUNT` (Streams entries per consumed batch, default 64).
- **Contracts.** `CONTRACT_READ_SCOPE_ENABLED` / `CONTRACT_READ_SCOPE_IDLE_MS`
  (request-scoped read connection + memo for local read contracts; opt out of
  the memo per scope with `contract_read_scope(memo=False)` from
  `sandloom.sdk.contracts`), `CONTRACT_DB_POOL_SIZE` / `CONTRACT_DB_MAX_OVERFLOW` /
  `CONTRACT_DB_LOCK_TIMEOUT_MS` (dedicated pool of in-process contract calls),
  `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` (command replay window, at most 7
  days).
- **Internal endpoints.** `INTERNAL_API_TOKEN` (unlocks `/metrics`,
  `/diagnostics/*`, `/contracts/*` and `/health` details — never `/_sandloom/rpc`;
  without it they are closed, unless `INTERNAL_ENDPOINTS_OPEN_IN_DEV=true`
  outside prod), `METRICS_TOKEN` (scrape-only `/metrics` credential; must
  differ from the internal token), `HEALTH_CACHE_TTL_SECONDS`.
- **Edge.** `RATE_LIMIT_ENABLED` (per-route `rate_limiter` limits; always on in
  prod), `RATE_LIMIT_GLOBAL_REQUESTS` / `RATE_LIMIT_GLOBAL_PERIOD_SECONDS` /
  `RATE_LIMIT_GLOBAL_EXEMPT_PATH_PREFIXES` (opt-in per-IP floor over every
  route incl. 404s, 0 = off everywhere, fail-open; the primary floor is the
  edge gateway), `TRUST_PROXY_HEADERS` + `TRUSTED_PROXY_IPS` (CIDR list;
  `X-Forwarded-For` is walked right-to-left from a trusted peer only —
  `get_client_ip` in `sandloom.sdk.web`), `CORS_ORIGINS`, `MAX_UPLOAD_SIZE_BYTES`,
  `MAX_REQUEST_BODY_BYTES` (global body cap → 413), `DOCS_ENABLED` (gate
  `/docs` etc.; off in prod by default).
- **Tokens and sessions.** `JWT_ALGORITHM` (HS256/384/512 shared secret —
  monolith/dev default — or EdDSA/RS256/ES256 asymmetric: the issuer — the
  process loading a module declared `issues_tokens=True`, i.e. identity —
  holds `JWT_PRIVATE_KEY`, every service verifies with `JWT_PUBLIC_KEYS`
  `{"<kid>": "<PEM>"}` and cannot mint; a critical startup check refuses a
  private key or `JWT_SECRET` on a verify-only process and, in prod, a shared
  HS secret in a split process unless the risk `shared_hs_secret_split` is
  accepted; `sandloom keys generate` makes a pair), `JWT_PREVIOUS_SECRETS` +
  `JWT_KID` (key rotation; asymmetric: rotate kids in `JWT_PUBLIC_KEYS`),
  `AUTH_REVOCATION_FAIL_CLOSED`, `AUTH_REVOCATION_CLOCK_SKEW_SECONDS` /
  `AUTH_REVOCATION_WRITE_ATTEMPTS` / `AUTH_REVOCATION_RETRY_BASE_SECONDS`
  (revocation mark margin and retries; identical in every service),
  `TWOFA_ENCRYPTION_KEY` (required in prod with identity; comma list =
  rotation), `SECURITY_CACHE_INSTANCE` (named cache for the subject-state
  records and — by default — idempotency records),
  `IDEMPOTENCY_CACHE_INSTANCE` (opt-in separate cache for idempotency
  records; empty = the security cache). Every authenticated route reads the
  session record live; there is no per-route revocation setting
  ([Route policy](../concepts/route-policy.md)).
- **Passwords.** `PASSWORD_HASH_CONCURRENCY` (argon2 pool; default
  min(4, CPUs)), `PASSWORD_HASH_PROFILE` (`strong` default = m=65536 KiB/t=3/p=4
  | `owasp_min` = m=19456/t=2/p=1 | `custom`) + `PASSWORD_HASH_TIME_COST` /
  `PASSWORD_HASH_MEMORY_KIB` / `PASSWORD_HASH_PARALLELISM` (explicit values
  override the preset; build hashers with `password_hasher()` from
  `sandloom.sdk.security`, never hand-pinned parameters; a successful login
  re-hashes a hash with other parameters; prod refuses parameters below the
  OWASP argon2id minimum unless the risk `weak_password_hash` is accepted).
- **Infrastructure.** `INFRA` (JSON declaring additional named infrastructure
  instances / driver overrides — see
  [Capabilities & Infrastructure](../concepts/capabilities-and-infrastructure.md)),
  `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_TIMEOUT` (default-engine pool
  sizing), `DB_POOL_PING_IDLE_SECONDS` (checkout ping only after that much
  idle time; no `pool_pre_ping`), `DB_POOL_OVERFLOW_LINGER_SECONDS` (overflow
  connections linger instead of churning connect/close; 0 = stock
  QueuePool), `DB_POOL_FAIR_BARGE_MS` (fair checkout: a waiter older than
  this is served before newcomers; 0 = strict FIFO handoff),
  `DB_STATEMENT_TIMEOUT_MS` / `DB_IDLE_IN_TX_TIMEOUT_MS` (server-side guards,
  0 = off), `CACHE_SOCKET_TIMEOUT_SECONDS` /
  `CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS` / `CACHE_MAX_CONNECTIONS` /
  `CACHE_POOL_TIMEOUT_SECONDS` / `CACHE_RETRY_ATTEMPTS` /
  `CACHE_HEALTH_CHECK_INTERVAL_SECONDS` (bounded Redis cache client: a dead
  Redis raises fast so fail-open/closed policies run), `GC_FREEZE_AFTER_STARTUP`
  / `GC_THRESHOLDS` / `GC_METRICS_ENABLED` (cyclic GC of gunicorn / Celery
  workers: freeze the startup heap, thresholds `2000,10,10`, pause histogram;
  applied by the process entry points, never by `create_app`).
- **Observability.** `LOG_FORMAT` (auto/json/text; auto = JSON in prod),
  `LOG_LEVEL`, `SENTRY_DSN` (+ `SENTRY_TRACES_SAMPLE_RATE`; no-op when
  unset), `TRACING_URL` (OTLP endpoint).
- **Billing and notifications.** `BILLING_PROVIDER=stripe` fails fast at
  startup unless `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` are set; prod
  refuses `fake` unless the risk `fake_psp_in_prod` is accepted.
  `NOTIFIER_BACKEND` (log/smtp; default log) + `SMTP_*`
  (host/port/username/password/from/use_tls for the email driver).
- **Secrets.** In prod, secrets and tokens must be at least 32 characters and
  not placeholders. `SECRETS_DIR` (Docker/K8s file-based secrets — a setting
  may be supplied as a file named after its field).

Operations runbook (secrets, backups, retention/PII, audit, observability):
`docs/operations/operator-reference.md` and [Operations](../operations/runbook.md).
