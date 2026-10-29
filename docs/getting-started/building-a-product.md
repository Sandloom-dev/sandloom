---
title: Building a Product
---

# Building a Product on Sandloom

You build a product **on top of** the framework rather than forking it. The
framework ships as the `sandloom` distribution and the standard modules as their
own packages (`sandloom-identity`, `sandloom-tenancy`, `sandloom-billing`); your
application is a standalone project that depends on them and keeps its modules
in its own namespace.

## The framework vs your application

| The framework (`sandloom` and the module packages) | Your application (you generate it) |
| --- | --- |
| `sandloom.sdk` — the public API your modules and tests import | your own namespace, e.g. `myapp/` |
| `sandloom.core` — the kernel behind it: UoW, event bus + outbox, infra plugin layer, contracts, CLIs (never imported by modules) | `myapp/modules/*` — your business modules |
| `sandloom.shared` — framework DTOs (`UserContext`, `UserContact`, `MemberState`), guards, authz | `myapp/shared/` — your generated contract stubs, event schemas and DTOs |
| `sandloom_identity`, `sandloom_tenancy`, `sandloom_billing` — opt-in module packages (`sandloom-identity`, ...) | `myapp/asgi.py` — `create_app()` from `sandloom.sdk.app` |
| `sandloom.main` — composes every installed module package | `.env`, `deploy/`, tests, CI + pre-commit (`sandloom check`) |

The kernel is **namespace-agnostic**: your modules live in `myapp.modules.*`
and never inside `sandloom.*`. The generated `asgi.py` loads every module of
`myapp.modules` plus the module packages you chose (by module name:
`identity`, `tenancy`, `billing`), so adding a module edits nothing there;
`SANDLOOM_MODULES` only selects a subset. Your modules never import a module
package: they call it through the contract stubs `sandloom generate` writes in
`myapp/shared/`.

## Build distribution artifacts

For normal local app development, the [quickstart](index.md#quickstart) uses
editable packages from the checkout; no manual wheel build is needed. The root
`sandloom-dev` project is a contributor environment, not a release package.
To build one distribution from the repository root:

```bash
uv build --wheel packages/sandloom
```

Build the module packages you use (`packages/sandloom-identity`, ...) too
before installing wheels in a clean environment. See the
[package layout and full validation](https://github.com/Sandloom-dev/sandloom/tree/main/packages).
The initial alpha is also available from PyPI:
`pip install "sandloom[standard]==0.7.0a2"`.

The `sandloom` wheel ships `sandloom.core` (including the core migration
chain, packaged as `sandloom/core/migrations/core_chain`), `sandloom.sdk`,
`sandloom.shared` and `sandloom.main`; the standard modules ship as their own
wheels with their own chains and a `sandloom.modules` entry point (see
[Publishing](../guides/publishing.md)). The framework provides the `sandloom` console script (`sandloom new`, `sandloom add`,
`sandloom migrate`, `sandloom test`, `sandloom check` and the operations commands; see the
[CLI reference](../reference/cli.md)).

## `sandloom new` — start a project

`sandloom new <app>` is the `django-admin startproject` of Sandloom — it generates a
runnable, testable standalone project. Run these from the framework checkout root:

```bash
uv run sandloom new helpdesk --with identity,tenancy,billing --module tickets --framework-source path --framework-path .
uv run sandloom new linkly --no-batteries --module links --framework-source path --framework-path .
```

| Flag | Meaning |
| --- | --- |
| `--with a,b,c` | standard modules to install and load (`identity`, `tenancy`, `billing`): the app depends on `sandloom[redis,celery]>=0.7.0a2,<0.8` plus `sandloom-<module>>=0.7.0a2,<0.8` for each |
| `--no-batteries` | install no standard module (minimal app: depends on `sandloom[redis,celery]` only) |
| `--module <name>` | name of the initial sample module |
| `--dir <path>` | parent directory (default: cwd) |
| `--framework-source path --framework-path <repo>` | depend on a local framework checkout (the repository root containing `packages/`): editable uv sources for exactly the distributions the app uses |

The generated tree boots immediately: `sandloom new` prints the next steps, the
[quickstart](index.md#quickstart) (`uv sync`, `uv run sandloom generate`, the dev services,
`uv run sandloom add module <name>`, `uv run sandloom test`, the migrations,
`uv run uvicorn <app>.asgi:app --reload`), and the generated `README.md`
repeats them. It also runs `sandloom generate` (module and route snapshots,
event schemas), so a fresh app passes its own CI gate, `sandloom test --ci`,
which only checks them.

The generated project also carries a `Dockerfile`, a monolith compose profile
(`deploy/docker-compose.yml`: migrate, app, Celery worker and beat via
`sandloom.core.worker`), a CI workflow and a pre-commit config that run `sandloom check`.

## How module loading works

`create_app()` loads the modules the composition root names: every module of
the app's `modules` package (found statically) plus the module packages it
chose. Each
module's `define_module(...)` call discovers what it declares (routes, events,
consumers, contracts). `SANDLOOM_MODULES` (a JSON list) selects a subset:

```bash
SANDLOOM_MODULES='["identity", "helpdesk.modules.tickets"]'
```

A module package is named by its entry-point name (`identity`) or its import
path (`sandloom_identity`). An app module with the same name as an installed
module package (`helpdesk.modules.identity` next to `sandloom-identity`) is
refused at startup ([SANDLOOM-B020](../reference/error-codes.md#sandloom-b020)).

To split an app, configure the service processes, `SANDLOOM_MODULES`,
`SERVICE_BINDINGS`, gateway routes, database assignments and RPC keys. Module
business code stays the same. Moving existing module data to its own database
is a separate offline extraction step. See [Deployment](../guides/deployment.md).

## Add more modules

```bash
sandloom add module notifications                       # helpdesk/modules/notifications
sandloom add event notifications <event>                # more generators: sandloom add --help
sandloom test notifications
```

Nothing else to edit: the module is discovered. Generated modules use
**relative imports** for their own code and only absolute-import `sandloom.sdk`,
so the same skeleton is valid in the framework tree or any downstream
namespace. Step by step: [Add a module](../guides/add-module.md).

## Migrations

There is no project-level Alembic tree: the framework ships the core chain and
each module owns its own chain (`sandloom migrate new` writes a revision in it). Apply the
core chain plus every loaded module's chain with the framework migrator (it
boots the composition root named by `SANDLOOM_COMPOSITION_ROOT` — default
`sandloom.main`, which loads the same `SANDLOOM_MODULES`; the generated compose sets it
to `<app>.asgi`):

```bash
export DATABASE_URL=postgresql+asyncpg://...
uv run sandloom migrate upgrade-all --apply
```

See [Migrations & Upgrades](../guides/migrations.md) for the full per-module,
per-database model.
