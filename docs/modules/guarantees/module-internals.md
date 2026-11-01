---
title: Module internals
description: The beginner names, the three API levels, how define_module discovers declarations, module packages and generated files.
type: concept
---

# Module internals

You do not need this page to write a module. It explains what happens underneath
[What a module is](../index.md), for when you drop below the generated code, ship a module
as its own package or read a discovery error.

## The beginner tier

Twenty-nine names cover CRUD, events and cross-module calls. Generated code
and the examples use only these (`sandloom test` checks it,
[SANDLOOM-T009](../../reference/error-codes.md#sandloom-t009)); any other name is advanced
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
  ([SANDLOOM-B006](../../reference/error-codes.md#sandloom-b006)).
- **Plain routers keep their rule.** The `APIRouter` bound to
  `<pkg>.api.router`, else every `APIRouter` among `api.__all__` (or its
  public names), is mounted. `module_router()` routers anywhere in the `api`
  submodules join them.
- **Tables.** `<pkg>.models` is imported first. A mapped class whose module is
  inside the package belongs to the module; its table name starts with
  `<module>_`.
- **Atomic.** Discovery fills a registration draft, validates it, then
  applies it. A failure applies nothing and names the original error
  ([SANDLOOM-B001](../../reference/error-codes.md#sandloom-b001)).
- **`setup(core)`** stays for what has no declaration: startup and shutdown
  tasks, claims contributors, planner operations. `router=` and `events=`
  override discovery; `consumes=` adds to the derived requirements, never
  weakening them.

The module list is discovered too. An application loads every subpackage of
`<app>.modules` whose `__init__.py` defines `register` (found statically, so
adding a module edits nothing else); installed module packages are found by
their entry point ([Module packages](#module-packages)). `SANDLOOM_MODULES`
still selects a subset, which is how one codebase deploys as several services
([Deployment](../../operate/topologies.md)).

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
  ([SANDLOOM-B020](../../reference/error-codes.md#sandloom-b020)). The message
  names both providers: uninstall the package or remove (rename) the local
  module.
- **Isolation.** An installed module package is another module: an app
  module importing it, in any spelling, is
  [SANDLOOM-C001](../../reference/error-codes.md#sandloom-c001). Talk to it
  through its events and contracts. The standard modules' contract stubs, the
  generated copies of their DTOs and their event schemas live in
  `sandloom.shared` and ship with `sandloom` (SANDLOOM-C012: every caller
  process imports them).

To ship a module of your own:

1. Give it its own distribution with one `sandloom.modules` entry point
   (`<name> = "<import_package>"`). Lay the repository out like the framework
   tree: top-level module packages under `src/`, `modules_package: ""` in
   its `architecture_rules.yml`, so `sandloom check` applies.
2. Depend on a compatible framework range, `sandloom>=0.7.0a5,<0.8`, never an
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
  ([Route policy](../http/route-policy.md#the-route-snapshot));
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

## Design rationale

- [ADR-0002: the framework absorbs module boilerplate](../../adr/0002-framework-absorbs-module-boilerplate.md)
- [ADR-0007: kernel layers](../../adr/0007-kernel-layers.md)
- [ADR-0010: Public API 1.0](../../adr/0010-public-api-1.0.md)
