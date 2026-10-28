# projecthub

An application built on the **Sandloom framework** (configurable monolith for SaaS).
It demonstrates the **Cross-Module Query / Projection layer** (`sandloom.sdk.projections`):
how a UI list that logically spans several modules is served without a forbidden
cross-module SQL join.

The framework ships as the `sandloom` dependency. This project owns only
its business logic: its modules live under `projecthub/modules/*` and import the
framework's public API, `sandloom.sdk` (Public API 1.0), plus the generated contract
stubs of `projecthub.shared`. The ASGI app (`projecthub/asgi.py`) builds the
framework app from every module it finds under `projecthub.modules`;
`SANDLOOM_MODULES` selects a subset.

## The projection demo

Four modules, isolated (no cross-module imports — they talk through typed
events and typed contracts):

| Module | Owns | Role |
| --- | --- | --- |
| `projects` | `Project` | source module — `ProjectCreated` / `ProjectUpdated` producers (`projects.project.{created,updated}.v1`), the `@contract("projects.read.v1")` provider |
| `tasks` | `Task` | source module — `TaskCreated` / `TaskUpdated` producers (`tasks.task.{created,updated}.v1`), the `@contract("tasks.read.v1")` provider |
| `taskboard` | `project_task_query_index` | **read model** — owns no domain entity; declares views of the events above and builds a denormalised task×project index from them |
| `notifications` | `notifications_inapp` | **event reaction** — a durable consumer of a `TaskCreated` view writes an in-app record (tenant-scoped) and dispatches via the `notification` capability |

## The code (Public API 1.0)

- `projects` and `tasks` are one-line `define_module(...)` packages: their
  `module_router()` routes, producer events and `@contract` read providers are
  **discovered**. `sandloom generate` writes the typed stubs callers import
  (`projecthub/shared/contracts/projects_read_v1.py`, `tasks_read_v1.py`) and
  exports the event schemas (`projecthub/shared/events/`).
- `taskboard` calls the read contracts through module-level callers,
  `projects = Contract(PROJECTS_READ_V1)`; `define_module` derives the
  module's `consumes` (contract and methods) from those calls. Its consumers
  read **views** (`class TaskCreated(Event, channel="tasks.task.created.v1")`
  in `consumers.py`), verified against the producers by `sandloom check`.
- What has no declaration yet stays in a short L2 `setup` with top-level
  imports: the planner's read operations, the taskboard projection (a
  `Projection(...)` cannot express the project fan-out over task rows) and
  the notifications consumer (it writes in the delivery's session, which an
  `@on` handler reaches through `db()` once it lands).
- Tests sign in with `as_user()`, check `published(ProjectCreated)` and
  replace a contract with `fake_contract(PROJECTS_READ_V1, ...)`.
- An advanced name carries `# sandloom: advanced <reason>` on its import line
  (SANDLOOM-T009).

The cross-module join `tasks ⋈ projects` is moved out of request time: the
`taskboard` consumers fold task and project events into one indexed row at
*write* time, and `GET /api/v1/taskboard/tasks?task_status=active&project_status=active&project_prefix=Dev`
serves "active tasks in active Dev projects" from a single tenant-scoped read.

Source writes use `TenantScoped` models; `module_router()` authenticates
every route and binds the signed tenant. Create requests contain business fields only; `tenant_id` in a
request body is rejected. The signed tenant controls inserts and updates, and
cross-tenant IDs return 404. Both source module migration chains install forced
PostgreSQL RLS policies. Projection consumers bind the event's tenant before
calling the source contracts; tenant rebuilds bind their explicit tenant too.

Key properties (see `projecthub/modules/taskboard/`):

- **Monotonic, version-guarded writes** — each source carries its own version
  (`task_version`, `project_version`); `monotonic_upsert`/`monotonic_update`
  apply only newer data, so at-least-once / out-of-order delivery never rolls a
  row back. Project fields are copied from a local, version-guarded
  `taskboard_project_snapshot` row that both writers lock (project events
  upsert it, task events `SELECT … FOR UPDATE` it), so a project update racing
  a task insert can never leave the task row with stale project data.
- **Exactly-once apply** — durable consumer groups + a `UNIQUE(consumer, event_id)`
  inbox claim committed with the projection write.
- **Rebuildable** — the projection is a cache; `sandloom projection rebuild
  project_task_query_index [--tenant <id>]` reconstructs it via the source
  modules' public read contracts (never their tables) and re-fans the
  project snapshot over existing rows; run it as a job once the source
  services are up (not in `migrate`). `sandloom projection list`
  shows the registered projections and consumers.

See `projecthub/modules/taskboard/tests/test_projection.py` for the end-to-end
convergence test (duplicate + out-of-order deliveries included).

## Quick start

Run from `examples/projecthub/` inside an existing Sandloom checkout. The local
package sources are declared in `pyproject.toml`; PyPI installation is pending
the first public release. Follow the [shared setup](../README.md#running-them)
for development database, Redis and signing-secret configuration.

```bash
uv sync
cp .env.example .env          # set your development DB, Redis and JWT_SECRET
uv run sandloom test          # requires the separate test services in .env.test
uv run sandloom migrate upgrade-all --apply
uv run uvicorn projecthub.asgi:app --reload   # http://localhost:8000
```

## Add a module

```bash
uv run sandloom add module <name>     # a new package under projecthub/modules; nothing else to edit
uv run sandloom test
```

## Migrations

Each module owns its own migration chain (`projecthub/modules/<m>/migrations/`).
Apply them all with `sandloom migrate`:

```bash
export DATABASE_URL=postgresql+asyncpg://...      # your dev DB
uv run sandloom migrate upgrade-all --apply
uv run sandloom migrate new <module> "description"            # autogenerate a revision
```

## Event reactions & notifications

`notifications` shows the write-side counterpart of projections: rather than
build a read model, it *reacts* to an event with a side effect. It registers a
durable consumer of its `TaskCreated` view (`tasks.task.created.v1`);
the framework runs each delivery in a UoW with exactly-once claim, and the
handler persists an in-app record and calls `notifier().send(Notification(...))`.
The `notification` capability defaults to the zero-dependency `log` driver
(`NOTIFIER_BACKEND=smtp` + `SMTP_*` to send real email). The in-app table is
`TenantScoped`, so `GET /api/v1/notifications` (the router binds the tenant)
is auto-filtered to the caller's tenant.
