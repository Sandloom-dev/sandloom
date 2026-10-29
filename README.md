# Sandloom

**Python backend framework for AI-driven development.**

[![PyPI alpha](https://img.shields.io/badge/PyPI-0.7.0a1-d7bd91)](https://pypi.org/project/sandloom/0.7.0a1/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab)](docs/getting-started/installation.md)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

## Keep shipping as your backend grows

AI agents can generate a feature quickly. As a backend grows, each change needs
to respect module boundaries, interfaces and data ownership.

Sandloom makes those rules executable. Developers and coding agents work through
a public Python SDK and generators; architecture checks catch the violations
they cover. The framework provides transaction and messaging primitives so
application code can focus on the feature.

Start as a monolith. Split into services while keeping your business modules.
Add identity, organizations or billing when your backend needs them.

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
| Split deployment as requirements change | Running the same business modules locally or across services through contracts |

## See architecture checks in action

After the quickstart creates `notes` and `reports`, create
`myapp/modules/reports/bad_import.py` with this deliberately invalid import:

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
Try the [forbidden-import example](#see-architecture-checks-in-action) above to
see a boundary violation caught before it becomes a dependency in your app.

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

## From one process to services

`SANDLOOM_MODULES` selects the modules a process hosts. `SERVICE_BINDINGS` tells
it where to call contracts hosted elsewhere. Typed contracts and durable events
connect the same module code in either topology.

Moving a module's existing data into its own database is a separate **offline
migration** with `sandloom migrate extract-module`. Deployment still requires
configuration, credentials and operations. See the
[deployment model](docs/concepts/deployment.md) and [deployment profiles](deploy/README.md).

Production infrastructure currently targets **PostgreSQL and Redis**, with
local file storage and SMTP. Infrastructure capability interfaces support
extensions; individual adapters have their own implementation and support status.
See [capabilities and infrastructure](docs/concepts/capabilities-and-infrastructure.md).

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
