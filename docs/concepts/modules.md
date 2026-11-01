---
title: What a module is
description: A module is one Python package that owns one slice of the business. What it contains, how to create one, what it may import and how it talks to other modules.
type: concept
---

# What a module is

A **module** is one Python package that owns one slice of the business: its tables,
routes, events, contracts and tests. Orders, billing and notifications would each be a
module.

Modules are **atomic and isolated**: you can work inside one without reading or breaking
any other. That is what keeps a change by a person or an agent contained to one package,
and what lets one codebase run as a monolith or as separate services by configuration
alone.

## What a module looks like

```text
<module>/
  __init__.py      # register = define_module(__name__, version="0.1.0")
  api.py           # router = module_router(): the routes
  services.py      # use cases; db() is the session of the running unit
  models/          # tables; their names start with <module>_
  schemas.py       # pydantic request and response shapes (no ORM)
  events.py        # Event classes of the module's own channels
  consumers.py     # views of other modules' channels and @on handlers
  contracts.py     # @contract providers
  sagas.py         # sagas the module owns
  projections.py   # read models the module builds
  tasks.py         # background tasks
  migrations/      # the module's own migration chain
  tests/           # the module's tests
```

Only `__init__.py` is required. A larger module may make any of these a
package; discovery walks it.

Create one with `uv run sandloom add module <name>`; [Create a module](../guides/add-module.md)
walks through it. The application finds the new package by itself, so nothing else needs
editing.

## What you get by default, and what you declare

| You write | Sandloom does for you |
| --- | --- |
| `register = define_module(__name__)` in `__init__.py` | imports the module's conventional submodules and registers everything it finds there (routes, events, consumers, contracts, sagas, projections, tables) |
| tables in `models/` (`TenantScoped` or `Base`) | adds them to the module's own migration chain; `sandloom migrate new` renders the revision with row-level security |
| `router = module_router()` in `api.py` | prefix `/api/v1/<module>`, tags, authentication and tenant binding per route |
| `class E(Event, channel="m.e.v1")` | registers the schema of your own channel, snapshots it, exports it for other repositories |
| `@on(E)` | a durable consumer with exactly-once apply on the envelope id |
| `@contract("m.x.v1")` | registers the provider; `sandloom generate` writes the typed stub and DTO copies callers import |
| `Contract(REF)` at module level | derives the module's consumed contracts (`consumes`) |
| nothing | the module list: an application loads every subpackage of `<app>.modules` that defines `register` |

## What a module may import

A business module and its tests import only:

- public `sandloom.sdk` modules: the beginner names from `sandloom.sdk`, every other
  name from its themed module (`sandloom.sdk.web`, `sandloom.sdk.data`, ...; see the
  [Python API](../reference/sdk/index.md));
- generated contract stubs and DTO copies (`sandloom.shared.contracts.*`, or
  the application's shared package `<app>.shared` / `CONTRACT_DTO_PACKAGES`);
- its own package.

A module never imports another business module (an installed module package
such as `sandloom_identity` included), a kernel internal, or a
private `sandloom.sdk._*` layer. Any spelling counts: absolute, relative,
`importlib`, `__import__`, `getattr` on a module, `sys.modules`. A module also
never reaches a kernel handle by attribute (`.event_bus`, `.infra`,
`.service_registry`, `request.app.state.core`, `.raw_client()`), and imports
no backend SDK (Redis, boto3, Celery), driver or `sandloom.driverkit` directly:
infrastructure comes from
capabilities ([Capabilities and infrastructure](capabilities-and-infrastructure.md)).

Business modules carry no function-local imports
([SANDLOOM-C021](../reference/error-codes.md#sandloom-c021)); the one exception is a
marked optional dependency (`# sandloom: optional-import <reason>`).

## How modules talk to each other

Through [events](events.md) when something has happened, and through
[contracts](contracts.md) when one module needs an answer now, never through an import.
[Choose how modules talk](between-modules.md) compares every option.

Other modules' rows are referenced by plain UUID, never by a foreign key. A contract call
runs in the provider's own unit on the provider's database, locally or over internal RPC:
local equals remote.

## Conventions inside a module

- **Layers.** SQLAlchemy and ORM models belong to `models/` and `services.py`
  (or a `services/` package).
  Routes (`api.py`) and `schemas.py` stay persistence-free: a route calls a
  service and returns a pydantic DTO
  ([SANDLOOM-C009](../reference/error-codes.md#sandloom-c009),
  [SANDLOOM-C010](../reference/error-codes.md#sandloom-c010),
  [SANDLOOM-C011](../reference/error-codes.md#sandloom-c011)).
- **Event channels are versioned**: `<module>.<entity>.<verb>.vN`
  ([Events](events.md)).
- **Bound list endpoints**: cap `limit` with `Query(le=...)`.
- **Ids and timestamps** come from the framework: `default=new_id` and
  `created_at: Mapped[CreatedAt]` ([details](data-conventions.md)).
- **Startup work** goes through `core.add_startup_task(fn)` in `setup`;
  `critical=True` when the process must not serve traffic if it fails.

## What breaks it

| Code | What it catches |
| --- | --- |
| [SANDLOOM-C001](../reference/error-codes.md#sandloom-c001) | an import of another business module |
| [SANDLOOM-C002](../reference/error-codes.md#sandloom-c002) | an import outside the public API |
| [SANDLOOM-C003](../reference/error-codes.md#sandloom-c003), [SANDLOOM-C004](../reference/error-codes.md#sandloom-c004), [SANDLOOM-C005](../reference/error-codes.md#sandloom-c005) | a raw provider, a backend SDK, a kernel handle |
| [SANDLOOM-C009](../reference/error-codes.md#sandloom-c009), [SANDLOOM-C010](../reference/error-codes.md#sandloom-c010), [SANDLOOM-C011](../reference/error-codes.md#sandloom-c011) | endpoints outside the api layer, ORM in endpoints, ORM in schemas |
| [SANDLOOM-C021](../reference/error-codes.md#sandloom-c021) | a function-local import |
| [SANDLOOM-B001](../reference/error-codes.md#sandloom-b001), [SANDLOOM-B006](../reference/error-codes.md#sandloom-b006) | a module that fails to load or registers something invalid |
| [SANDLOOM-T009](../reference/error-codes.md#sandloom-t009) | an advanced name in generated code or an example without a marker |

`sandloom check --list-rules` lists every static rule; `sandloom test` runs them with
the architecture tests and the import contracts.

??? note "Guarantees, precisely"

    - Imports are restricted to `sandloom.sdk` public modules, the DTO packages and
      the module's own package (G-MODULE-02, G-MODULE-13).
    - Discovery keeps today's plain-router rule; a refused module registers
      nothing (G-MODULE-14, C-MODULE-04).
    - L1 registration equals the hand-written L2 registration, and every L1
      dependency is a public L2 name (G-SDK-03, G-SDK-05).
    - Generated code and examples stay in the beginner tier (G-SDK-04).
    - Importing `sandloom.sdk` reads no environment and performs no I/O (G-SDK-01).
    - An application derives its module list (G-MODULE-19).

    The ledger with the enforcing tests: [Guarantees & capabilities](../reference/guarantees.md).

## Inspect before changing a module

Use [module context](../guides/module-context.md) to gather scoped rules, SDK and
wire interfaces, and the [declared architecture graph](../guides/architecture-graph.md)
to inspect contract/event connections. Both commands can be invoked by an agent
and read their index without booting the app.

## Go deeper

- [Module internals](module-internals.md): the beginner names, the API levels, discovery,
  module packages and generated files.
- [ADR-0002: the framework absorbs module boilerplate](../adr/0002-framework-absorbs-module-boilerplate.md)
