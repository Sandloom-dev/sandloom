---
title: Building a product
---

# Building a product on Sandloom

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
| `sandloom.shared` — generated standard-module contract stubs and DTO copies; authentication and authorization are exposed through `sandloom.sdk` | `myapp/shared/` — your generated contract stubs, event schemas and DTOs |
| `sandloom_identity`, `sandloom_tenancy`, `sandloom_billing` — opt-in module packages (`sandloom-identity`, ...) | `myapp/asgi.py` — `create_app()` from `sandloom.sdk.app` |
| `sandloom.main` — composes every installed module package | `.env`, `deploy/`, tests, CI + pre-commit (`sandloom check`) |

The kernel is **namespace-agnostic**: your modules live in `myapp.modules.*`
and never inside `sandloom.*`. The generated `asgi.py` loads every module of
`myapp.modules` plus the module packages you chose (by module name:
`identity`, `tenancy`, `billing`), so adding a module edits nothing there;
`SANDLOOM_MODULES` only selects a subset. Your modules never import a module
package: they call it through the contract stubs `sandloom generate` writes in
`myapp/shared/`.

## `sandloom new` — start a project

`sandloom new <app>` is the `django-admin startproject` of Sandloom — it generates a
runnable, testable standalone project. From an empty directory, use the published alpha:

```bash
uvx --from "sandloom[standard]==0.7.0a4" sandloom new helpdesk --with identity,tenancy,billing --module tickets
uvx --from "sandloom==0.7.0a4" sandloom new linkly --no-batteries --module links
```

| Flag | Meaning |
| --- | --- |
| `--with a,b,c` | standard modules to install and load (`identity`, `tenancy`, `billing`): the app depends on `sandloom[redis,celery]>=0.7.0a4,<0.8` plus `sandloom-<module>>=0.7.0a4,<0.8` for each |
| `--no-batteries` | install no standard module (minimal app: depends on `sandloom[redis,celery]` only) |
| `--module <name>` | name of the initial sample module |
| `--dir <path>` | parent directory (default: cwd) |
| `--framework-source path --framework-path <repo>` | depend on a local framework checkout (the repository root containing `packages/`): editable uv sources for exactly the distributions the app uses |

The generated tree boots immediately: `sandloom new` prints the next steps, the
[quickstart](index.md#quickstart) (`uv sync`, `uv run sandloom generate`, the dev services,
`uv run sandloom add module <name>`, `uv run sandloom test`, the migrations,
`uv run uvicorn <app>.asgi:app --reload`), and the generated `README.md`
repeats them. After dependencies are installed, `uv run sandloom generate`
writes module/route snapshots, stubs, DTO copies and event schemas. A standalone
CLI may defer that work until the selected batteries are installed. The app's
own CI gate, `sandloom test --ci`, only checks those files. For email verification
and tokens, follow [authentication and email](authentication.md).

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
uv run sandloom add module notifications
uv run sandloom add event notifications NotificationQueued --field message:str
uv run sandloom test notifications
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
uv run sandloom migrate upgrade-all --apply
```

See [Migrations & Upgrades](../guides/migrations.md) for the full per-module,
per-database model.

## Develop against framework source

Application development normally uses published distributions. Contributors
can consume a local checkout with
`--framework-source path --framework-path <repository-root>`; these are editable
uv sources, so the app runs the current checkout. The root `sandloom-dev` project
is a contributor environment and is not a release package.

To build wheels yourself, use `uv build --wheel packages/sandloom` and build
the selected `packages/sandloom-*` distributions too. The kernel wheel ships
its core migration chain; each battery owns its package and migration chain.
See [package layout](https://github.com/Sandloom-dev/sandloom/tree/main/packages)
and [publishing](../guides/publishing.md).
