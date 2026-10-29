# Sandloom

**Python backend framework for AI-driven development.**

[![PyPI alpha](https://img.shields.io/badge/PyPI-0.7.0a1-d7bd91)](https://pypi.org/project/sandloom/0.7.0a1/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab)](docs/getting-started/installation.md)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

## Keep shipping features. Keep architecture under control.

AI coding agents make implementing features faster. The harder part comes later:
changing one module without introducing hidden dependencies, breaking contracts
or losing control of data ownership.

Sandloom makes architectural boundaries executable. Developers and coding agents
build through a public Python SDK and generators, while automated checks detect
violations of the rules the framework enforces.

**Infrastructure-agnostic business modules** use capability interfaces instead
of vendor clients. Change infrastructure through configuration and compatible
drivers while keeping business logic. Start as a monolith and split into
services with the same modules. Add identity, organizations or billing as needed.

[Quickstart](#quickstart) · [See architecture checks](#see-architecture-checks-in-action) ·
[Documentation](docs/index.md) · [Examples](examples/README.md)

> **Alpha:** `0.7.0a1` is available on PyPI.
> [Compatibility and stability](docs/contributing/versioning.md).

## Why Sandloom

| When you need to… | Sandloom helps by… |
| --- | --- |
| Keep shipping features as the codebase grows | Enforcing module boundaries and detecting forbidden dependencies |
| Check compatibility when changing module interfaces | Checking typed, versioned contracts and event schemas |
| Give coding agents explicit architectural rules | Providing a public SDK, generated context and executable checks |
| Avoid rebuilding critical infrastructure for each feature | Providing transactions, outbox delivery, tenant isolation and deduplication |
| Change infrastructure without rewriting business modules | Separating capabilities from backend drivers and selecting providers through configuration |
| Split deployment as requirements change | Running the same business modules locally or across services through contracts |

### Who is Sandloom for?

Python backends with multiple business domains, evolving requirements and a long
development lifecycle—especially when coding agents regularly change features,
infrastructure may change, or modules may need independent deployment.
For a tiny API or one-off script, a lighter framework may be a better fit.

## Quickstart

You need **Python 3.11+** (3.12 recommended),
[uv 0.13.4+](https://docs.astral.sh/uv/) and **Docker with Compose**.

Create a backend with one module from the published alpha. This example uses
the framework without the optional identity, tenancy and billing packages:

<!-- readme:quickstart:start -->
```bash
uvx --from "sandloom==0.7.0a1" sandloom new myapp --no-batteries --module notes
cd myapp
uv sync
docker compose -f deploy/docker-compose.dev.yml up -d --wait
uv run sandloom migrate upgrade-all --apply
uv run uvicorn myapp.asgi:app --reload
```
<!-- readme:quickstart:end -->

Open [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health);
with the services healthy, it returns HTTP 200. Generated module routes require
a bearer token by default. For signup and login, use `--with identity` instead
of `--no-batteries` when generating the app; add `tenancy` and `billing` when
needed. See the
[installation guide](docs/getting-started/installation.md) and
[application options](docs/getting-started/building-a-product.md).

The generated Compose profile uses ports 5432/6379 for development and
5433/6380 for tests. If they are occupied, follow the port overrides in the
generated `myapp/README.md` before starting the services.

### Make your first change

In another terminal, from `myapp/`, add a second module and run its tests and
architecture gates:

<!-- readme:workflow:start -->
```bash
uv run sandloom add module reports
uv run sandloom generate
uv run sandloom test reports
uv run sandloom migrate upgrade-all --apply
```
<!-- readme:workflow:end -->

The test command finishes with `sandloom test: OK`. You have added an isolated
module, checked it and applied its migration without editing the first module.
Try the [forbidden-import example](#see-architecture-checks-in-action) below to
see a boundary violation caught before it becomes a dependency in your app.

## See architecture checks in action

From the generated project root (`myapp/`, containing `pyproject.toml`), create
`myapp/modules/reports/bad_import.py` with this deliberately invalid import.
The inner `myapp/` is the Python package; `notes` and `reports` were created above:

<!-- readme:boundary-import:start -->
```python
from myapp.modules.notes.services import create_item
```
<!-- readme:boundary-import:end -->

Run `uv run sandloom check`. Output excerpt from the published `0.7.0a1`:

<!-- readme:boundary-output:start -->
```text
sandloom check: 1 architecture violation(s)

  myapp/modules/reports/bad_import.py:1: error[SANDLOOM-C001 module-boundaries]: from myapp.modules.notes.services import ...
```
<!-- readme:boundary-output:end -->

The command exits with code 1. Cross-module work goes through
[contracts](docs/concepts/contracts.md) or [events](docs/concepts/events.md).
Remove the temporary file and check again:

<!-- readme:boundary-cleanup:start -->
```bash
rm myapp/modules/reports/bad_import.py
uv run sandloom check
```
<!-- readme:boundary-cleanup:end -->

```text
sandloom check: OK (2 module(s) under myapp.modules: notes, reports)
```

## What application code looks like

This use case comes from the [tutorial's tasks module](examples/tutorial/tasklist/modules/tasks).
Its model, schemas and event are defined alongside it; the complete application
and tests are in [examples/tutorial](examples/tutorial/README.md).

<!-- readme:example:start -->
```python
from uuid import UUID

from sandloom.sdk import db

from .events import ItemCreated
from .models.item import TasksItem
from .schemas import ItemIn, ItemOut


async def create_item(body: ItemIn, *, owner_id: UUID) -> ItemOut:
    item = TasksItem(title=body.title, owner_id=owner_id)
    db().add(item)
    await db().flush()
    await ItemCreated(item_id=item.id, tenant_id=item.tenant_id).publish()
    return ItemOut.model_validate(item)
```
<!-- readme:example:end -->

An authenticated [route](examples/tutorial/tasklist/modules/tasks/api.py) calls
this function inside a framework-owned unit. The row and event enter the same
database transaction; the outbox makes the event available for delivery after
commit. The model uses `TenantScoped` for the bound tenant. Application code
does not commit the session itself.

See [units of work](docs/concepts/units.md),
[tenant isolation](docs/concepts/multi-tenancy.md) and
[guarantees and their conditions](docs/concepts/guarantees.md).

## How Sandloom works

Each module owns its models, routes, migrations and tests. Typed contracts and
events connect modules; generated stubs and schema checks help you evolve their
interfaces. After changing a model, create its migration with
`sandloom migrate new <module> "<message>"`.

Generated apps include agent instructions. `sandloom docs <name>` shows an SDK
signature and example; `sandloom explain <code>` explains a failed check.
See the [SDK reference](docs/reference/sdk/index.md),
[testing guide](docs/reference/testing.md) and [module model](docs/concepts/modules.md).

## Change infrastructure without rewriting modules

Modules request capabilities such as a database, cache, object store or notifier.
Drivers implement those operations; configuration selects their instances.
Architecture checks reject backend SDKs and raw clients in business modules.

For example, move PostgreSQL and Redis from local containers to managed services
by changing connection settings. Select a notification driver with
`NOTIFIER_BACKEND`. Additional compatible drivers can be installed as plugins
and selected through `INFRA`; startup checks validate module requirements
against the configured capabilities and features.

The current production stack is **PostgreSQL + Redis**, with SMTP and local or
shared-volume files. Switching backend technology requires an implemented driver
with the required features; PostgreSQL is currently the only supported relational
backend. See the [capability model and driver support matrix](docs/concepts/capabilities-and-infrastructure.md).

## From one process to services

`SANDLOOM_MODULES` selects the modules a process hosts. `SERVICE_BINDINGS` tells
it where to call contracts hosted elsewhere. Typed contracts and durable events
connect the same module code in either topology.

Moving a module's existing data into its own database is a separate **offline
migration** with `sandloom migrate extract-module`. Deployment still requires
configuration, credentials and operations. See the
[deployment model](docs/concepts/deployment.md) and [deployment profiles](deploy/README.md).

## Explore

- [Tutorial](docs/getting-started/tutorial.md): a feature, an event consumer,
  a contract and tests.
- [Linkly](examples/linkly/README.md): a small backend without the standard modules.
- [Academy](examples/academy/README.md): workflows across modules with sagas.
- [Projecthub](examples/projecthub/README.md): projections across module boundaries.
- [Helpdesk](examples/helpdesk/README.md): a multi-tenant SaaS using identity,
  organizations and billing.
- [Module packages](packages/README.md): installable identity, tenancy and billing.
- [Admin console](admin/README.md): the optional owner console.
- [Brand assets](website/assets/brand/README.md): the caracal, wordmark, avatars and icons.

## Project status

Sandloom is in alpha. See [stability](docs/contributing/versioning.md) and the
[changelog](CHANGELOG.md) for compatibility and changes. Events use
**at-least-once delivery**; durable consumers deduplicate by envelope ID within
the documented retention conditions. Review the
[guarantee boundaries](docs/concepts/guarantees.md) and
[security model](docs/reference/security-model.md) for your application.

For framework development, start with [AGENTS.md](AGENTS.md) and
[CONTRIBUTING.md](CONTRIBUTING.md). Vulnerability reporting and its current
availability are described in [SECURITY.md](SECURITY.md).

## License

Created by **Vasyl Cherniavskyi**. Original Sandloom framework code, documentation
and examples are licensed under [Apache-2.0](LICENSE); see [NOTICE](NOTICE).
Third-party components and assets retain their own licenses and notices.
