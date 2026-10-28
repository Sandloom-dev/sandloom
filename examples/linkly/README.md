# linkly

A URL shortener built on the **Sandloom framework** (configurable monolith for SaaS).

The framework ships as the `sandloom` dependency. This project owns only
its business logic: its modules live under `linkly/modules/*` and import the
framework's public API, `sandloom.sdk` (Public API 1.0). The ASGI app
(`linkly/asgi.py`) builds the framework app from every module it finds under
`linkly.modules`; `SANDLOOM_MODULES` selects a subset.

## The links module

```
linkly/modules/links/
  __init__.py     register = define_module(__name__, version="0.1.0", setup=_setup)
  api/links.py    router = module_router(dependencies=[public(reason=...)])
  events.py       class LinkCreated(Event, channel="links.created.v1")
  models/         Shortlink(Base)
  services/       LinkService (cache-backed lookup, await LinkCreated(...).publish())
  tests/          client, published(LinkCreated)
```

- Everything is **discovered** by `define_module`: the `module_router()` of
  `api/`, the producer event of `events.py`, the table of `models/`. The
  only explicit registration is the planner's read operations, in a short
  `setup` with top-level imports.
- Every route is public (`public(reason=...)`, no identity module is loaded),
  so `module_router()` injects no authentication.
- `GET /api/v1/links/{code}` is a read-only route on the execution planner
  (`PlannedReads`): a cache hit touches no database at all.
- Module code imports only the beginner names of `sandloom.sdk`; an advanced name
  carries `# sandloom: advanced <reason>` on its import line (SANDLOOM-T009).

## Quick start

Run from `examples/linkly/` inside an existing Sandloom checkout. The local
package sources are declared in `pyproject.toml`; PyPI installation is pending
the first public release. Follow the [shared setup](../README.md#running-them)
for development database, Redis and signing-secret configuration.

```bash
uv sync
cp .env.example .env          # set your development DB, Redis and JWT_SECRET
uv run sandloom test          # requires the separate test services in .env.test
uv run sandloom migrate upgrade-all --apply
uv run uvicorn linkly.asgi:app --reload   # http://localhost:8000
```

`sandloom test` runs the tests and the static gates (ruff, format, mypy,
`sandloom check`, snapshots) and writes the generated files: each module's
`module.snapshot.json` and `routes.snapshot.json`, and the exported event
schemas under `linkly/shared/events/`.

## Add a module

```bash
uv run sandloom add module <name>     # a new package under linkly/modules; nothing else to edit
uv run sandloom test
```

## Migrations

Each module owns its own migration chain (`linkly/modules/<m>/migrations/`).
Apply them all with `sandloom migrate`:

```bash
export DATABASE_URL=postgresql+asyncpg://...      # your dev DB
uv run sandloom migrate upgrade-all --apply
uv run sandloom migrate new <module> "description"            # autogenerate a revision
```
