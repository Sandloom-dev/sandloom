# Sandloom

**Python backend framework for AI-driven development.**

[![PyPI alpha](https://img.shields.io/badge/PyPI-0.7.0a1-d7bd91)](https://pypi.org/project/sandloom/0.7.0a1/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab)](docs/getting-started/installation.md)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

Build APIs and domain workflows with coding agents and developers. Sandloom
provides a public Python SDK, module generators and executable architecture
rules. Keep changes inside isolated modules while the framework supplies
transactions, tenant isolation and durable messaging.

Start with a monolith and split modules into services by configuration, keeping
your business logic. Add identity, organizations or billing when your backend
needs them.

[Quickstart](#quickstart) · [Documentation](docs/index.md) ·
[Examples](examples/README.md) · [Architecture](docs/concepts/index.md)

> **Alpha:** `0.7.0a1` is available on PyPI. Review the
> [stability policy](docs/contributing/versioning.md) before adopting it.

## Why Sandloom

| You want to… | Sandloom provides… |
| --- | --- |
| Develop with a coding agent | A public SDK, generated module scaffolding, agent instructions and checks with actionable error codes |
| Keep changes local as the backend grows | Enforced module boundaries; typed contracts and events for work across modules |
| Avoid rebuilding correctness mechanisms for each feature | Framework-owned transactions, tenant isolation, transactional outbox, durable consumers and command deduplication |
| Change deployment topology without rewriting business logic | Local contract calls or internal RPC behind the same interface, selected by configuration |

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

## Develop with an agent or on your own

Run these commands from the generated application. The new module is discovered
automatically; adding it requires no edits to another module.

<!-- readme:workflow:start -->
```bash
uv run sandloom add module reports
uv run sandloom generate
uv run sandloom test reports
uv run sandloom migrate upgrade-all --apply
```
<!-- readme:workflow:end -->

After changing a model, use `sandloom migrate new <module> "<message>"` to create
its migration. `sandloom docs <name>` prints an SDK signature and example;
`sandloom explain <code>` explains a failed check and its fix.

Generated apps include their own agent instructions. For work on the framework,
start with [AGENTS.md](AGENTS.md); the [SDK reference](docs/reference/sdk/index.md)
and [testing guide](docs/reference/testing.md) cover application development.

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

## Status and contributing

Sandloom is in alpha. Public API compatibility and migration policies are
documented in [Versioning & Stability](docs/contributing/versioning.md);
changes are recorded in the [changelog](CHANGELOG.md).
Events use **at-least-once delivery**; durable consumers deduplicate deliveries
by envelope ID. Review the [guarantee boundaries](docs/concepts/guarantees.md)
and [security model](docs/reference/security-model.md) for your application.

Documentation links currently open source files in this repository. Public
hosting at `sandloom.dev` is being prepared.

For framework setup, questions and contributions, see
[CONTRIBUTING.md](CONTRIBUTING.md). Vulnerability reporting and its current
availability are described in [SECURITY.md](SECURITY.md).

## License

Created by **Vasyl Cherniavskyi**. Original Sandloom framework code, documentation
and examples are licensed under [Apache-2.0](LICENSE); see [NOTICE](NOTICE).
Third-party components and assets retain their own licenses and notices.
