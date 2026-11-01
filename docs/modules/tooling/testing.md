---
title: Testing modules
---

# Testing modules

Each module owns a `tests/` directory. Tests run against a real PostgreSQL
(from `.env.test`) through the real application. They are not mocked. The
harness (env loading, schema, sessions, HTTP client, tokens) belongs to the
**framework**, not to each module: it ships as the `sandloom.sdk.testing.plugin`
pytest plugin ([ADR-0002](../../adr/0002-framework-absorbs-module-boilerplate.md)).

This page explains how to run the tests and how the harness behaves. Every
fixture and helper, with copy-paste examples, is in the
[testing reference](../../reference/testing.md); exact signatures are in
[`sandloom.sdk.testing`](../../reference/sdk/testing.md) or `sandloom docs <name>`.

For the general test workflow, use this guide; for copy-paste fixtures, use
[Testing reference](../../reference/testing.md); for exact public helper signatures,
use [`sandloom.sdk.testing`](../../reference/sdk/testing.md).

## First run on a fresh checkout

You do not need a `.env` to run the tests. Start the test services and run
the gate:

```bash
docker compose -f deploy/compose/test.yml up -d --wait   # PostgreSQL :5433, Redis :6380
uv sync
uv run sandloom test
```

- **Framework suite.** Its root conftest (`conftest.py`) fills
  every setting the shell leaves unset from `.env.test` before the
  settings are first read.
  - `.env.test` beats a developer's `.env`, so tests never depend on dev
    values. `ENVIRONMENT` stays the dev default: the suite gates on
    `is_prod`, never on `is_test`.
  - A shell that exports `DATABASE_URL` itself (CI) is left alone.
- **Apps.** The plugin below loads `.env.test` instead.

`.env.example` is the template for running the **app** (`uvicorn`, `celery`,
`sandloom migrate`). Copying it to `.env` does not change the tests.

### Your own database and Redis (shared machines)

`SANDLOOM_TEST_DATABASE_URL`, `SANDLOOM_TEST_CACHE_URL` and `SANDLOOM_TEST_BROKER_URL` win
over `.env.test` on every load path. A plain shell `DATABASE_URL` /
`CACHE_URL` does not: `.env.test` overrides those for the test sessions.

- The database is created on first use.
- Pick two Redis indexes no one else uses (`tools/checks/test_resources.py list`).
- Set `SANDLOOM_TEST_OWNER` so the ownership guard knows the resources are yours.

```bash
export SANDLOOM_TEST_OWNER=me SANDLOOM_TEST_DATABASE_URL=postgresql+asyncpg://test_user:test_password@127.0.0.1:5433/sandloom_test_me
export SANDLOOM_TEST_CACHE_URL=redis://127.0.0.1:6380/120 SANDLOOM_TEST_BROKER_URL=redis://127.0.0.1:6380/121
```

The harness refuses to reset any database other than the one
`SANDLOOM_TEST_DATABASE_URL` names (or its pytest-xdist `_gw<N>` copies).

**`CACHE_URL` and `SANDLOOM_TEST_CACHE_URL`: do they have to differ?**

- **The old requirement.** A comment in `.github/workflows/ci.yml` says the
  settings-level `CACHE_URL` must name a different Redis database than the
  test cache. The reason given: the billing conftest pointed its idempotency
  cache at the test `CACHE_URL`, and the redis driver shares its process-wide
  client only when the two URLs are equal, so billing failed closed with 503.
- **It no longer reproduces.** Measured on 2026-10-25, with the snapshot
  `CACHE_URL` equal to `SANDLOOM_TEST_CACHE_URL` (what the fresh-checkout seeding
  produces):
  - the billing suite (307 passed);
  - the whole fast suite (3,783 passed).
- **CI still sets different values.** Keep them different if you run the
  slow suites with a hand-written `.env` and see 503s from billing.

## Activate the harness (once per application)

```toml
# pyproject.toml — `sandloom new` generates this
[tool.pytest.ini_options]
pythonpath = ["."]
asyncio_mode = "auto"
addopts = ["-p", "sandloom.sdk.testing.plugin"]
# optional, instead of SANDLOOM_COMPOSITION_ROOT in .env.test:
# sandloom_composition_root = "myapp.asgi"
```

That is all: modules need **no `conftest.py`** and the app needs no root
conftest. The plugin is deliberately *not* a `pytest11` entry point:
installing the framework never changes another test suite (nor the
framework's own).

A module **in the framework tree** (`src/sandloom/<name>`, whose suite
does not load the plugin) gets a one-line conftest instead:

```python
from sandloom.sdk.testing.fixtures import *  # noqa: F403
```

What the plugin does:

- **Loads `.env.test` first.** The nearest `.env.test` walking up from the
  pytest rootdir (then from the test paths) is loaded with `override=True` in
  `pytest_load_initial_conftests`, before any conftest or test module can
  read the settings. No file found is a clear usage error.
- **Boots the app** through the composition root (`SANDLOOM_COMPOSITION_ROOT`, else
  the `sandloom_composition_root` ini option, else `sandloom.main`), which also
  registers every loaded module's models.
- **Guards the database.** `db_engine` refuses any `DATABASE_URL` whose
  database name lacks `test`, even with `ENVIRONMENT=test`. The schema (every
  model plus the core outbox / audit / consumer-inbox tables) is built once
  per process and emptied before each test: only the tables holding a row
  are emptied (`DELETE` children-first; a table with more than 1,000 rows is
  `TRUNCATE`d) and every used sequence is rewound. That leaves exactly what a
  `TRUNCATE … RESTART IDENTITY CASCADE` of every table leaves, at a fraction
  of the cost. The probe runs with `row_security = off`, so a row-level
  security policy can never hide a row from it; any error falls back to
  truncating everything.
- **Records events and resolves contracts.** `published(E)` lists the events
  the code under test published; `Contract(REF)` resolves through the test
  application, and `fake_contract` overrides one contract for a block.
- **Releases pools per test**, so loop-bound asyncpg connections never leak
  into the next test's event loop.

## What a test looks like

A test takes the `client` fixture, mints a user with `as_user`, and asserts on
the response and on `published(...)`:

```python
--8<-- "examples/tutorial/tasklist/modules/tasks/tests/test_tasks.py:tests"
```

A consumer is driven with `deliver`. The envelope id is stable across
`times`, so `times=2` is a redelivery; `session=db` runs the deliveries in the
test transaction:

```python
--8<-- "examples/tutorial/tasklist/modules/stats/tests/test_stats.py:tests"
```

## Fixtures and helpers in brief

The beginner kit is `client`, `as_user`, `published`, `deliver`,
`fake_contract` and `fake_access_fact`. The rest of the harness (`db`,
`real_uow_client`, `db_uow`, `ambient_uow`, `sandloom_cache`, `scratch_database`,
the migration, revocation and RPC helpers) is listed with examples in the
[testing reference](../../reference/testing.md). Two behaviours matter for
every test:

- **`client` never commits.** Every request runs on the test's one `db`
  session, bound as the Unit of Work. Events and audit rows land in the test
  transaction. A request that fails with a 4xx does **not** roll its writes
  back. Assert rollback behaviour with `real_uow_client`, which runs the real
  request Unit of Work (commit on success, rollback on any error).
- **Direct service calls need a Unit of Work.** `publish_event` and
  `record_audit` raise outside one. Use `db_uow` (the test session as the
  Unit of Work) or `ambient_uow` (an in-memory recording one).

### Unhandled errors are re-raised

Production answers an unhandled endpoint exception with an opaque
`500 {"code": "internal_error"}`. In a test that hides the cause, so
`client` and `real_uow_client` raise `UnhandledServerError` from the original
exception instead: route, exception type and message, and the catalogue fix:

```text
error[SANDLOOM-R001]: POST /api/v1/notes answered 500 internal_error because the endpoint raised:
  MissingTenantError: an ORM SELECT on TenantScoped model NotesNote ran with no tenant bound. ...
  fix: add a TenantScope() parameter to the endpoint ... (sandloom explain SANDLOOM-R001)
```

A test that asserts the 500 itself marks it
`@pytest.mark.sandloom_allow_server_errors`.

Keep **only module-specific fixtures** (seed data, stubs) in a module
`conftest.py`, built on the harness fixtures. See
`examples/helpdesk/.../tickets/tests/conftest.py`, and the bundled
`sandloom_tenancy/tests/conftest.py` / `sandloom_billing/tests/conftest.py` (a domain
fixture overriding `async_client` on top of `client`, for example to install
a fake payment provider, is the pattern).

## Deprecations fail the suite

Generated apps (and the example apps) set
`filterwarnings = ["error::DeprecationWarning"]`: a deprecated call fails the
tests now instead of breaking on the next release. Add targeted
`ignore::DeprecationWarning:<module>` entries only for third-party noise you
cannot fix. The framework's own suite errors on deprecations attributed to
`sandloom.*` code (`error::DeprecationWarning:sandloom`).

## Run the suites

`sandloom test` is the gate. It runs the tests in parallel, then the static gates:

```bash
uv run sandloom test <module>   # iterate: the module's tests, the fast suite, the gates
uv run sandloom test --full     # once, at the end: the full parallel suite (slow tests included) + gates
uv run sandloom test --no-gates # tests only;  sandloom test --plan prints the steps
```

Never re-run a serial `pytest -q` after each fix. Plain pytest still works
for a single test:

```bash
uv run pytest src/sandloom/<module>/tests/test_x.py::test_y --no-cov
```

**Fast suite.** `-m "not slow"` skips the multi-process and subprocess-heavy
suites: the split conformance matrix, the e2e split deployment, the
generated-project and migration-CLI tests, module data extraction, migration
chain walks, the real `pg_dump` round trip. The full suite (CI, the pre-push
hook) still runs everything; mark a new test `@pytest.mark.slow` when it
spawns servers / CLIs or walks migration chains.

**Parallel.** `pytest -n auto --dist loadfile` gives each xdist worker its own
database (`<db>_gw<N>`) and Redis indexes; `-n auto` is capped to the
connections the PostgreSQL server has free. On a Redis shared with other runs
set `SANDLOOM_TEST_REDIS_DB_STRIDE=auto` (`sandloom test` does): the controller then
claims free indexes for the workers before they start (this owner's indexes
from earlier runs first, then unmarked empty ones, each with an atomic
check-and-set) instead of the fixed `index + 16 × (N + 1)` layout, which
collides with other runs' markers. Too few free indexes abort the session
with exit code 4 (the pre-push hook then falls back to a serial run).

In the framework tree the benchmark security suite (`benchmarks/tests`) is a
separate CI job (`benchmarks-security`: it needs a superuser connection for its
RLS roles and runs about 6 minutes): `tools/ci/ci_local.sh <ref> --jobs
benchmarks-security`.

### Isolation guards

The harness protects concurrent runs on one machine from each other:

- **Resource ownership.** Every test database carries an owner marker (its
  comment, `sandloom-test-owner:<owner>`) and every test Redis index the key
  `sandloom:test:owner`. The owner is `SANDLOOM_TEST_OWNER`, else the checkout root (one
  per git worktree). An unmarked resource is claimed by the first run (a
  database the harness creates is stamped on creation); one marked by another
  owner aborts the session before any test runs (exit code 4, nothing
  truncated or flushed), under xdist for every worker's `_gw<N>` copy too.
  `SANDLOOM_TEST_TAKE_OWNERSHIP=1` takes a resource over deliberately. Empty a
  test Redis database with `flush_owned_redis(client)` (keeps the marker).
- **Self-connection.** A URL with `localhost` and a port from 32768 up is refused (exit
  code 4). A service published on `127.0.0.1` leaves `::1` with no listener, and a
  connection to that dead port can be given the same port as its source, so the socket
  connects to itself and reads its own request back. It takes thousands of connections,
  so it looks like a random failure. Use `127.0.0.1` or a lower port.
- **Stale markers.** Every claim also stamps when the resource was last used
  (Redis key `sandloom:test:last_used`, database setting `sandloom_test.last_used`; the
  owner marker keeps its format for older checkouts). Markers of finished runs
  otherwise pile up until no Redis index is free:

  ```bash
  uv run python tools/checks/test_resources.py list            # owner, age, connections
  uv run python tools/checks/test_resources.py prune --days 7  # dry run: what would go
  uv run python tools/checks/test_resources.py prune --days 7 --apply
  ```

  A resource is pruned only when nothing is connected to it and its last use
  is known and older than `--days` (at least 1; a run re-stamps its resources
  when it starts). Legacy markers without a stamp fall back to the youngest
  key idle time (Redis, read under `CLIENT NO-TOUCH`) or the database
  directory's modification time (superuser role); unknown is never pruned.
  A Redis index is emptied by an atomic check-and-`FLUSHDB` (skipped when its
  marker or stamp changed since the scan); a database is dropped with a plain
  `DROP DATABASE`, which PostgreSQL refuses while anything is connected.
  Unmarked databases (scratch copies leaked by killed runs) are only touched
  with `--include-unmarked`; this checkout's own owner is always protected.
- **Connection budget.** A test process gets small pools by default
  (`DB_POOL_SIZE=2`, `DB_MAX_OVERFLOW=3`, `CONTRACT_DB_POOL_SIZE=1`,
  `CONTRACT_DB_MAX_OVERFLOW=1`, `DB_POOL_OVERFLOW_LINGER_SECONDS=0`; an
  explicit value wins): at most 13 connections with the harness engine. The
  session starts only if the server's free connections (`max_connections` −
  reserved − in use) cover `workers × 13`, else it fails fast with the numbers
  instead of `too many clients` mid-run; `-n auto` is capped to what fits
  while keeping 40 free for the multi-process suites. The split conformance
  matrix boots in waves of at most 40 connections and checks each wave's
  room. `SANDLOOM_TEST_SKIP_CONNECTION_CHECK=1` skips the check.
- **Process-global state.** `create_app` binds its app as the process's
  active core / infra registry / event bus and fills the settings' module
  lists. The harness builds the composition-root app before the first test
  module and makes its binding the baseline: every module starts from it and
  gets it back afterwards, every test gets its starting bindings back, so an
  app a test builds never leaks into the next test, and a test that resolves
  the active runtime without asking for `sandloom_app` passes alone too. What a
  test registers on the bound core (an event schema, a service, a startup
  task, a declared operation, ...) is rewound after it
  (`CoreFacade.restore_registrations`).
- **Pre-push hook.** `backend-full-tests` refuses to run the full suite unless
  `SANDLOOM_TEST_DATABASE_URL` names this checkout's own database
  (`tools/checks/prepush_full_tests.sh`; `SKIP=backend-full-tests` skips
  it once).

### Test isolation

Every test must pass alone, in any order, serially or in parallel. A parallel
run with a fixed distribution hides a test that passes only because an
earlier one left a registry, a binding, a cache key or a row behind, so the
gates run the tests in a **seeded random order**
(`sandloom.core.testing.shuffle`: the files, then the tests inside each file; a
file's module fixtures are still set up once):

- **`sandloom test`** shuffles every test step with a fresh seed. It prints the
  seed before the steps and again after a failure:
  `sandloom test --seed <seed>` replays the order, `--no-shuffle` runs in file
  order.
- **pytest directly**: `--sandloom-shuffle=<seed>` (or `random`). The framework's
  `pyproject.toml` loads the plugin; an application adds
  `-p sandloom.core.testing.shuffle` (inert without the option). The seed is in
  the report header and the summary.
- **CI** runs the parallel suites (`backend-tests`, `examples`) with
  `--sandloom-shuffle=random`, and the job `backend-tests-isolation` runs the
  in-process framework tests **serially in one process** in a fresh order: the
  check a fixed xdist distribution cannot make. Locally:
  `tools/ci/ci_local.sh <ref> --jobs backend-tests-isolation`.

The serial job excludes `subprocess_probe`: package build/install checks and
CLI/plugin probes whose tested runtime lives entirely in a child process.
They cannot leak that runtime into the parent's next test and remain in both
unfiltered full suites (Python 3.11 and 3.12). Tests of the parent's globals,
registrations, caches and database state stay in the shuffled serial pass.
`--durations=25` reports the slowest setup/call/teardown phases in CI.

Replay a failure of the serial job in one process:

```bash
uv run pytest -p no:xdist --sandloom-shuffle=<seed>      # the whole run, same order
uv run pytest -p no:xdist src/sandloom/x/tests/test_y.py  # the failing file alone
```

What the harness resets for you after every test
(`sandloom.core.testing.isolation`): the process's runtime bindings and what a
test registered on the bound core (above), SQLAlchemy's class-level event
flags (`event.listen(Engine, ...)` sets `Engine._has_events` and
`event.remove` never clears it), and the RPC client's described-contract
caches (`PROCESS_CACHES`). The test database is emptied before every test that
uses `db_engine`, the cache by `sandloom_cache`. What a test must not do: rely on
another test's setup (register what you need in your own fixture), keep state
in module globals between tests, or read a module-level definition's derived
state before the harness registered it. A new process-wide cache or class
flag that tests warm belongs in `PROCESS_CACHES` / `CLASS_FLAGS`, not in a
per-test patch.

## Conventions

- **Tests import the framework only through `sandloom.sdk.testing`** (plus the
  public `sandloom.sdk` modules, contract stubs and DTOs). The `test-imports` rule
  of `sandloom check` (SANDLOOM-C013) is a whitelist: a module test may import those and
  its own package, not kernel internals and not another business module.
- **Mint users via JWT only.** Use `as_user` (or the `make_headers` fixture).
  Do not create identity rows from another module's tests. Tokens carry
  `token_type="access"`, `iat`, `tenant_id` and the signed `tenant_ids`
  allowlist, `email` / `email_verified`; extra keywords become claims,
  `tenant=False` mints a tenant-less token.
- **Unit tests without a database.** Run services against a mocked session
  under `recording_uow()` (its `.audits` / `.events` record what was written);
  `bind_test_uow(session)` binds a real session as the Unit of Work by hand;
  `run_on_commit(session)` treats the session as committed: it runs the
  queued `before_commit` checks first (a failing one propagates and no
  post-commit callback runs), then the post-commit callbacks;
  `InMemoryCache` is a full cache provider for idempotency / cache tests.
- **Contracts read the test's data.** On the `client` path the test session is
  bound for contract calls automatically. Elsewhere wrap the call in
  `bind_contract_session(db)` (from `sandloom.sdk.testing`).
- **In-memory bus delivery is asynchronous.** `publish` only enqueues on the
  dev/test `memory` bus. Drive a consumer directly with `deliver` instead of
  waiting for the bus.
- **Every versioned channel needs a schema** in every profile (SANDLOOM-R003). A
  typed `Event` class carries its own; a probe channel in a test is
  registered with `register_event_schemas`.
- **Module wiring is the module's own test.** The generated
  `tests/test_module_wiring.py` pins the approved route snapshot, the
  boots-alone outcome and the registration surface. A module's tests are
  collected without editing `testpaths` (`src/sandloom` in the framework, the app
  package in an app).
- **No driver resets.** The Redis cache driver's clients follow the running
  event loop, so tests never reset driver singletons.
- **Don't gate on `is_test`.** In the framework's own suite the settings
  snapshot `.env` (`ENVIRONMENT=dev`). Gate behaviour on `is_prod`, never on
  `is_test`.
- **Snapshot IDs before a savepoint rollback.** After a service method that may
  roll back a `begin_nested()` savepoint, snapshot `entity.id` before the call:
  rollback expires ORM instances on async sessions.
- **Sign webhooks.** Billing webhook tests sign payloads with
  `sandloom_billing.psp.sign_stripe_payload`.

## Cross-module test imports

Module tests are held to a whitelist (`sandloom check` rule `test-imports`): the
public `sandloom.sdk` modules, the contract stubs and DTOs, the testing kit
`sandloom.sdk.testing` and the module's own package. Another business module's
code (its services, models, schemas) and kernel internals are out.

Build another module's state the way a client would: through its HTTP API on
the shared `client` (rows land in the test `db`), its contracts
(`fake_contract`, or the real provider through the test app) or its events
(`deliver`); carry its facts in the token
(`as_user(tenant_id=..., roles=[...])`) or answer them with
`fake_access_fact`. See `sandloom_billing/tests/tenancy_api.py` (organizations and
memberships seeded through tenancy's routes) and
`examples/helpdesk/.../tickets/tests/conftest.py`. Take the app and its core
from the `sandloom_app` / `sandloom_core` fixtures, never from the composition root
module. A test that is really about kernel behaviour belongs in the kernel's
own suite; a missing seam belongs in `sandloom.sdk.testing`.

## The quality gates

One command runs everything, with one summary and a non-zero exit when any
step fails (each failing step names its `sandloom explain` code):

```bash
sandloom test                 # fast parallel suite (-m "not slow" -n auto) + static gates
sandloom test billing         # billing's own tests first, then the same
sandloom test --full          # the full parallel suite (slow tests included) + gates
sandloom test --no-gates      # tests only;  sandloom test --plan prints the steps
```

The static gates are `ruff check`, `ruff format --check`, `mypy`, `sandloom check`,
the architecture tests, `lint-imports` (when the project has a
`.importlinter`) and the contract and route snapshot checks (SANDLOOM-T004 /
SANDLOOM-T001). Pre-commit runs the fast ones on every commit.

Applications run the same isolation rules with the `sandloom check` CLI
(generated projects wire it into CI and pre-commit):

```bash
sandloom check                                   # ./architecture_rules.yml
sandloom check --rules path/to/architecture_rules.yml
sandloom check --only module-boundaries,orm-leakage
sandloom check --list-rules
```

Each rule has an `SANDLOOM-Cnnn` code and `sandloom explain <code>`;
`sandloom check --list-rules` lists them, and the
[error catalogue](../../reference/error-codes.md) explains each. A violation
prints as `path:N: error[SANDLOOM-Cnnn rule]: message`; waive one deliberately on
its line with `# sandloom: allow <rule-id> <reason>`. Exit status `0` clean, `1`
violations, `2` bad manifest / usage. It is purely static: no application
import, no environment needed. `sandloom check --boot` additionally builds the app
through `create_app` (no server) and reports every startup refusal (SANDLOOM-B0xx)
and stale route snapshots in the same format.

## Inspect before changing a module

Use [module context](module-context.md) to gather scoped rules, SDK and
wire interfaces, and the [declared architecture graph](architecture-graph.md)
to inspect contract/event connections. Both commands can be invoked by an agent
and read their index without booting the app.
