---
title: Modules, levels and discovery
description: What a module is, what it may import, how it talks to other modules, the three API levels and how define_module discovers a module's declarations.
type: concept
---

# Modules, levels and discovery

A **module** is one Python package that owns one slice of the business: its
tables, routes, events, contracts and tests. Modules are **atomic and
isolated**: you can work inside one module without reading or breaking any
other. That is what lets one codebase run as a monolith or as microservices by
configuration alone, and what keeps a change by a human or an agent contained
to one package.

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

Generate a module with `sandloom add module <name>` ([how-to](../guides/add-module.md)).
The generated package is the convention:

```text
<module>/
  __init__.py      # register = define_module(__name__, version="0.1.0")
  api.py           # router = module_router(): the routes
  services.py      # use cases; db() is the session of the running unit
  models/          # tables (TenantScoped or Base); table names start with <module>_
  schemas.py       # pydantic request and response shapes (no ORM)
  events.py        # Event classes of the module's own channels
  consumers.py     # views of other modules' channels and @on handlers
  contracts.py     # @contract providers
  sagas.py         # sagas the module owns
  projections.py   # read models the module builds
  tasks.py         # background tasks
  migrations/      # the module's own migration chain
  tests/           # the module's tests (fixtures from the sandloom.sdk.testing plugin)
```

Only `__init__.py` is required. A larger module may make any of these a
package; discovery walks it.

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
  `created_at: Mapped[CreatedAt]` (below).
- **Startup work** goes through `core.add_startup_task(fn)` in `setup`;
  `critical=True` when the process must not serve traffic if it fails.

## Timestamps, ids and list order

A list that pages or that a test asserts needs an order with no tie. Two
idioms give it; generated modules use both:

```python
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id
from sandloom.sdk.data import UpdatedAt  # sandloom: advanced last-write time


class Note(TenantScoped, Base):
    __tablename__ = "notes_notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]  # only on a row that changes


# newest first: the database clock, then the id
query = sa.select(Note).order_by(Note.created_at.desc(), Note.id.desc())
```

- **`CreatedAt` / `UpdatedAt`** are `timestamptz NOT NULL` columns the
  database stamps with `clock_timestamp()`, the time of the statement.
  `server_default=func.now()` is the trap: `now()` is the start of the
  transaction, so every row one transaction writes gets the same time. A
  request that inserts several rows, and a test (the `client` fixture runs a
  whole test in one transaction), would see them tie and come back in any
  order. `sandloom migrate new` renders `server_default=sa.text("clock_timestamp()")`.
- **`new_id()`** returns a UUIDv7: its first 48 bits are the time in
  milliseconds and a counter orders the ids one process makes in the same
  millisecond, so ids sort in creation order and B-tree inserts stay at the
  right edge of the index. Keep `uuid.uuid4` for an id that must not reveal
  when it was made (a credential, an unguessable link).
- **Order by `created_at`, then `id`.** `created_at` is one clock, the
  database's; `id` breaks the rare tie of two rows stamped in the same
  microsecond (a multi-row `INSERT`). `order_by(id)` alone is the creation
  order of one process; across processes it follows their clocks.
- Recipe: `uv run sandloom docs recipe list-order`.

## How modules talk to each other

| Need | Use | Page |
| --- | --- | --- |
| tell others something happened (fire and forget) | `await E(...).publish()` | [Events](events.md) |
| react to something another module did | `@on(View)` on a view of its channel | [Events](events.md) |
| a list that spans modules | a projection built from events, never a request-time cross-module join | [Projections](projections.md) |
| ask another module now (sync) | `Contract(REF)` and call its methods | [Contracts](contracts.md) |
| a process across modules with undo | a saga owned by one module | [Sagas](sagas.md) |

Other modules' rows are referenced by plain UUID, never by a foreign key. A
contract call runs in the provider's own unit on the provider's database,
locally or over internal RPC: local equals remote.

## The beginner tier

Twenty-nine names cover CRUD, events and cross-module calls. Generated code
and the examples use only these (`sandloom test` checks it,
[SANDLOOM-T009](../reference/error-codes.md#sandloom-t009)); any other name is advanced
and its import line says why: `# sandloom: advanced <reason>`.

| Need | Names (`from sandloom.sdk import ...`) |
| --- | --- |
| a module | `define_module` |
| tables | `Base`, `TenantScoped`, `db`, `new_id`, `CreatedAt` |
| routes | `module_router`, `public`, `CurrentUser` |
| events | `Event`, `on` |
| contracts | `contract`, `command`, `Contract` |
| authorization | `require`, `Subscribed`, `SubscriptionActive`, `Credits`, `OrgRole`, `Owner`, `authorize_resource` |
| effects | `record_audit`, `on_commit` |
| tests (`sandloom.sdk.testing`) | `client` (fixture), `as_user`, `published`, `deliver`, `fake_contract`, `fake_access_fact` |

## Three API levels

| Level | What it is | Who uses it |
| --- | --- | --- |
| **L1, declarative** | `define_module` discovery, `module_router`, `Event` and `.publish()`, `on`, `contract`, `command`, `Contract`, `ContractRef`, `OptionalContract`, typed saga steps, `Projection`, `ProjectionView` | module authors and the generators |
| **L2, primitives** | the advanced names of the `sandloom.sdk` modules: `ModuleAPI`, `publish_event`, `contract_method`, `ContractRequirement`, `DbSession`, `idempotency_guard`, ... | modules with a need L1 does not cover; L1 itself |
| **L3, kernel** | the framework internals | kernel contributors only |

L1 is built only from L2: every name L1 uses is a public L2 name, and a
module registered through L1 produces exactly the registration a hand-written
L2 `register` would. So a module may drop to L2 for one feature without
losing any guarantee. The guarantees (deny-by-default, sessions, re-checks,
idempotency, write admission, labels) live in L3 and apply to every router,
including a plain FastAPI `APIRouter`.

??? example "The L2 form of a module"

    `define_module` builds this function. A module may write it by hand:

    ```python
    from sandloom.sdk.module import ContractRequirement, ModuleAPI, ModuleDescriptor  # sandloom: advanced L2 module


    def register(core: ModuleAPI) -> ModuleDescriptor:
        core.include_router(router)
        core.register_event_schema("notes.note.created.v1", "NoteCreated", schema)
        core.register_event_consumer(
            "stats.counter.reset.v1", handler, name="h", bind_tenant_from="tenant_id"
        )
        core.register_service("notes.lookup.v1", Lookup)
        return ModuleDescriptor(
            name=__name__,
            version="0.1.0",
            consumes=[ContractRequirement("stats.quota.v1", methods=("may_create_note",))],
        )
    ```

## Discovery

`define_module(__name__)` builds the module's `register(core)`. When the
application loads the module, it imports these submodules in this order, if
they exist: `models`, `api`, `events`, `consumers`, `contracts`, `sagas`,
`projections`, `tasks`. A package is walked recursively, sorted by name.

- **What is collected.** Classes and functions (`Event` subclasses, `@on`
  handlers, `@contract` classes) belong to the module when they are defined
  inside its package. Instances (`module_router()` routers, `Contract(...)`
  references, `Saga`, `Projection`) record the package that constructed them.
  Aliases and re-exports are counted once; objects owned by another package
  are ignored.
- **Misplaced declarations.** An L1 object outside the conventional
  submodules is refused with the module to move it to
  ([SANDLOOM-B006](../reference/error-codes.md#sandloom-b006)).
- **Plain routers keep their rule.** The `APIRouter` bound to
  `<pkg>.api.router`, else every `APIRouter` among `api.__all__` (or its
  public names), is mounted. `module_router()` routers anywhere in the `api`
  submodules join them.
- **Tables.** `<pkg>.models` is imported first. A mapped class whose module is
  inside the package belongs to the module; its table name starts with
  `<module>_`.
- **Atomic.** Discovery fills a registration draft, validates it, then
  applies it. A failure applies nothing and names the original error
  ([SANDLOOM-B001](../reference/error-codes.md#sandloom-b001)).
- **`setup(core)`** stays for what has no declaration: startup and shutdown
  tasks, claims contributors, planner operations. `router=` and `events=`
  override discovery; `consumes=` adds to the derived requirements, never
  weakening them.

The module list is discovered too. An application loads every subpackage of
`<app>.modules` whose `__init__.py` defines `register` (found statically, so
adding a module edits nothing else); installed module packages are found by
their entry point ([Module packages](#module-packages)). `SANDLOOM_MODULES`
still selects a subset, which is how one codebase deploys as several services
([Deployment](deployment.md)).

## Module packages

A module can also ship as its own distribution. Identity, tenancy and billing
do: `sandloom-identity`, `sandloom-tenancy` and `sandloom-billing`, import
packages `sandloom_identity`, `sandloom_tenancy` and `sandloom_billing`
(`packages/README.md` in the repository lists the distributions).

- **Entry point.** A module distribution registers itself in the
  `sandloom.modules` entry-point group, for example
  `identity = "sandloom_identity"` in its `pyproject.toml`.
- **Module name.** The entry-point name is the module name: it keys the
  tables (`identity_*`), the migration chain (`alembic_version_identity`), the
  event channels and contract names (`identity.*`). Any other module's name
  is the last segment of its import path (`app.modules.notes` is `notes`).
  `sandloom.core.descriptors.module_names` holds the rule.
- **Selection.** `SANDLOOM_MODULES`, `MODULE_REQUIRED` and deploy profiles
  name a module by entry-point name (`identity`) or import path
  (`sandloom_identity`). `sandloom.main:app` composes every installed entry
  point (sorted) when `SANDLOOM_MODULES` is unset; a generated app lists its
  modules. `MODULE_LOAD_ENTRYPOINTS=true` makes a loader also load every
  installed module package.
- **Ambiguity.** One name has one provider. The loader refuses, before
  anything loads, a requested module whose name two installed packages
  register, or that an installed package and an app-local module both carry
  (`app.modules.identity` next to `sandloom-identity`)
  ([SANDLOOM-B020](../reference/error-codes.md#sandloom-b020)). The message
  names both providers: uninstall the package or remove (rename) the local
  module.
- **Isolation.** An installed module package is another module: an app
  module importing it, in any spelling, is
  [SANDLOOM-C001](../reference/error-codes.md#sandloom-c001). Talk to it
  through its events and contracts. The standard modules' contract stubs, the
  generated copies of their DTOs and their event schemas live in
  `sandloom.shared` and ship with `sandloom` (SANDLOOM-C012: every caller
  process imports them).

To ship a module of your own:

1. Give it its own distribution with one `sandloom.modules` entry point
   (`<name> = "<import_package>"`). Lay the repository out like the framework
   tree: top-level module packages under `src/`, `modules_package: ""` in
   its `architecture_rules.yml`, so `sandloom check` applies.
2. Depend on a compatible framework range, `sandloom>=0.7.0a4,<0.8`, never an
   exact pin, and on no other module package.
3. Author its contract DTOs in its own `schemas` module and ship the stubs and
   DTO copies `sandloom generate` writes in a package every caller process
   imports (SANDLOOM-C012), listed in the app's `CONTRACT_DTO_PACKAGES`; an
   app composing the module also generates its own copies into `<app>.shared`.

Then `pip install` it next to an app and add its name to `SANDLOOM_MODULES`.

## Generated files

`sandloom generate [module ...]` writes, after a compatibility check against the
committed version (`sandloom add` runs it last; `sandloom test` only checks):

- `<module>/module.snapshot.json`: the module's declared surface (producer
  event schemas, consumers, consumed contracts, contracts, sagas,
  projections);
- `<module>/routes.snapshot.json`: every route and its resolved policy
  ([Route policy](route-policy.md#the-route-snapshot));
- `<module>/contracts.snapshot.json`: the describe documents of the contracts
  the module provides;
- the typed stub of every contract the module provides, in the shared DTO
  package (`<shared>/contracts/<x_y_vN>.py`), also for the installed module
  packages the application composes (the stubs stay with the consumer);
- a wire-shape copy of every DTO of the module's own package a provided
  contract signature names (`<shared>/contracts/<module>_dto.py`);
- the JSON schema of every event the module produces
  (`<shared>/events/<channel>.json`), so views in other repositories can be
  verified.

An incompatible change (a contract method removed, an event field changed on
the same `.vN`, a route that lost its authentication, a DTO the generator
cannot copy) fails before anything is written; `sandloom test` (dev and CI)
fails on a stale file: "generated files are stale: run `sandloom generate`".
A weaker route policy is approved only by `sandloom routes snapshot --write`. These generated files are
the one stated exception to "a module edits nothing outside its package".

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

## Design rationale

- [ADR-0002: the framework absorbs module boilerplate](../adr/0002-framework-absorbs-module-boilerplate.md)
- [ADR-0007: kernel layers](../adr/0007-kernel-layers.md)
- [ADR-0010: Public API 1.0](../adr/0010-public-api-1.0.md)

## Inspect before changing a module

Use [module context](../guides/module-context.md) to gather scoped rules, SDK and
wire interfaces, and the [declared architecture graph](../guides/architecture-graph.md)
to inspect contract/event connections. Both commands can be invoked by an agent
and read their index without booting the app.
