# Sandloom example applications

These are **real, standalone applications** generated with `sandloom new`, kept in
the framework repo as living proof that the framework is universal — they
consume the framework's public API, `sandloom.sdk` (Public API 1.0), exactly as
any external project would (and, for one of them, the installed module packages
`sandloom-identity`, `sandloom-tenancy` and `sandloom-billing`). Each
was scaffolded by the CLI and then filled in with a real domain. Agents copy
them, so they stay in the **beginner tier**: module code imports the 29
beginner names, and an advanced name carries `# sandloom: advanced <reason>` on
its import line (SANDLOOM-T009).

| App | Generated with | Stresses |
| --- | --- | --- |
| [`linkly/`](linkly) | `sandloom new linkly --no-batteries --module links` | minimal app, **no auth/identity**, `public(reason=...)` routes, cache-backed click counter, a typed producer event |
| [`helpdesk/`](helpdesk) | `sandloom new helpdesk --with identity,tenancy,billing --module tickets` | multi-tenant SaaS reusing the installed identity, tenancy and billing modules, cross-module **sync contract** (`tenancy.membership.v1`), the **organization billing gate** (`Subscribed(of=Account.org(...))`), RBAC, idempotency, events + audit |
| [`academy/`](academy) | `sandloom new academy --no-batteries --module enrollment` | **sagas** (ADR-0008) with **typed steps**: a course-enrollment flow across three modules — `@contract` / `@command` providers, local + command steps, a pivot, compensation, retries, the timer sweep — and a split-conformance case (monolith vs split over RPC, same terminal state) |
| [`projecthub/`](projecthub) | `sandloom new projecthub --no-batteries --module projects` | the **Cross-Module Query / Projection layer**: a `taskboard` read model folds `projects`+`tasks` events (read through **views**) into one indexed `project_task_query_index` (monotonic version-guarded upsert, exactly-once durable consumers, rebuild CLI through typed `Contract(...)` callers) — a cross-module list with **no runtime join** |

## What a module looks like

```python
# <app>/modules/notes/__init__.py
from sandloom.sdk import define_module
register = define_module(__name__, version="0.1.0")
```

Everything else is **discovered** from the conventional submodules
([spec §5.7](../tests/fixtures/specs/public-api-1.0-spec.md)):

- `api/` — `router = module_router()`: prefix `/api/v1/<module>`, every
  route authenticated (`user: CurrentUser`) and tenant-bound when the module
  has a `TenantScoped` table; `public(reason=...)` opts a route out.
- `events.py` — `class NoteCreated(Event, channel="notes.note.created.v1")`,
  published with `await NoteCreated(...).publish()`; a class of another
  module's channel is a **view** (projecthub's `taskboard`, `notifications`).
- `contracts.py` — `@contract("x.y.v1")` provider classes, commands marked
  `@command(idempotency_key=...)`; callers declare a module-level
  `Contract(X_Y_V1)` from the stub `sandloom generate` writes into the app's
  `<app>/shared/contracts/` (academy, projecthub). A contract DTO lives in the
  provider's `schemas.py`; its generated copy sits next to the stub
  (`<module>_dto.py`, helpdesk's `identity_dto` / `tenancy_dto`).
- `sagas.py` — `Saga(...)` with typed steps, `step("charge",
  call=charges.charge, ...)` (academy).
- `models/` — `Base` / `TenantScoped` tables; `migrations/` — the module's own
  chain (`run_module_migrations(__file__)`).
- What has no declaration (planner read operations, startup tasks, an L2
  projection) goes in a short `setup` with **top-level** imports: no module
  carries a function-local import (SANDLOOM-C021).
- `tests/` — **no conftest**: each app's `pyproject.toml` loads the framework
  harness with `addopts = [..., "-p", "sandloom.sdk.testing.plugin"]` (fixtures
  `client`, `db`, ...); tests use `as_user`, `published`, `deliver` and
  `fake_contract` from `sandloom.sdk.testing`.

`sandloom generate` (from an app's root) writes the generated files: each
module's `module.snapshot.json` / `routes.snapshot.json` /
`contracts.snapshot.json`, the contract stubs, the DTO copies and the exported
event schemas under `<app>/shared/`. `sandloom test` runs the tests and every
static gate and only checks them.

### Public SDK

These apps consume `sandloom.sdk`, their own package and generated contracts/DTOs.
The kernel is an internal implementation detail. See the module-author contract
for advanced APIs and the import annotation required when using one.

## Running them

Use an existing framework checkout. The canonical source is
[Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom), currently private
and awaiting source transfer. PyPI packages are not published yet. Keep the
examples beside `packages/`: their editable uv sources use relative paths.

From the repository root, start the **test** services once:

```bash
uv sync --locked
docker compose -f deploy/compose/test.yml up -d --wait
```

Test defaults are PostgreSQL `:5433` and Redis `:6380`. On a shared machine,
configure your own services through `SANDLOOM_TEST_DATABASE_URL`,
`SANDLOOM_TEST_CACHE_URL`, `SANDLOOM_TEST_BROKER_URL` and `SANDLOOM_TEST_OWNER`.
Never point the test harness at a development or production database.

For example, run Helpdesk in its own environment:

```bash
cd examples/helpdesk
uv sync
uv run sandloom test
cp .env.example .env
# Configure a separate development DATABASE_URL, CACHE_URL and BROKER_URL.
# Generate JWT_SECRET with: openssl rand -hex 32
uv run sandloom migrate upgrade-all --apply
uv run uvicorn helpdesk.asgi:app --reload
```

The example `.env.example` files expect their own development PostgreSQL database
on `:5432` and Redis on `:6379`; the test compose above does not provide those
endpoints. Provision them separately, or create a **separate development database**
on the local test PostgreSQL instance and adjust `.env` accordingly. Database
names used for tests must contain `test`; never reuse that database for the API.
The tutorial and Academy also include `deploy/docker-compose.dev.yml` for their
own development/test infrastructure; do not start conflicting copies on the same
ports.

The [tutorial](tutorial/README.md) is the smallest end-to-end starting point.
To test every example using the already installed contributor environment (as CI
does), run from the repository root:

```bash
framework_root="$PWD"
for app in examples/linkly examples/helpdesk examples/projecthub examples/academy examples/tutorial; do
  (cd "$app" && "$framework_root/.venv/bin/sandloom" test --full) || break
done
```

These source-checkout instructions do not install a root wheel: `sandloom-dev`
is not a release distribution. For an isolated wheel install, build the complete
required package set using the [package guide](../packages/README.md). Never
use `pip install -e .` to bypass the examples' uv source overrides while their
Sandloom dependencies are unpublished.

## Migrations and background work

Like any `sandloom new` project, the examples have no project-level Alembic chain:
the framework's core chain ships in the package and every module owns its
chain in `<app>/modules/<m>/migrations/`. `sandloom migrate upgrade-all --apply`
(the `migrate` service in each `deploy/docker-compose.yml`) applies both;
`sandloom migrate new <module> "<message>"` autogenerates a revision (with row-level
security for each new `TenantScoped` table). The `worker` / `beat` services
run the framework entrypoint `celery -A sandloom.core.worker`.
