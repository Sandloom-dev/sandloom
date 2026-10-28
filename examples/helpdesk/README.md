# helpdesk

Multi-tenant ticketing built on the **Sandloom framework** (configurable monolith
for SaaS), reusing the installed identity, tenancy and billing modules.

The framework ships as the `sandloom` dependency and the three modules as the
`sandloom-identity`, `sandloom-tenancy` and `sandloom-billing` packages. This
project owns only its business logic: its modules live under
`helpdesk/modules/*` and import the framework's public API, `sandloom.sdk`
(Public API 1.0), plus the contract stubs generated into `helpdesk.shared`. The
ASGI app (`helpdesk/asgi.py`) loads the installed modules (by name: `identity`,
`tenancy`, `billing`) plus every module it finds under `helpdesk.modules`;
`SANDLOOM_MODULES` selects a subset.

## The tickets module

```
helpdesk/modules/tickets/
  __init__.py      register = define_module(__name__, setup=_setup, consumes=[...])
  api/tickets.py   router = module_router(prefix="/api/v1/orgs"); user: CurrentUser
  events.py        TicketCreated, TicketStatusChanged (Event producers)
  models/          Ticket(Base)
  services/        TicketService: membership check, .publish(), record_audit(...)
  tests/           as_user, published(TicketCreated), seeded organizations
```

- **Discovered** by `define_module`: the `module_router()` routes, the two
  producer events, the table. The planner's read operations are registered
  in a short `setup` with top-level imports.
- **Authentication**: `module_router()` authenticates every route; the
  handlers take `user: CurrentUser`.
- **Sync contract**: organization membership is checked through
  `tenancy.membership.v1`, never by importing tenancy. Tenancy is an L2
  provider without a generated stub yet, so the route resolves it with
  `get_core` and the module states the optional requirement in `consumes`.
- **Billing gate**: `GET /orgs/{organization_id}/tickets/export` is gated by
  the **organization's** subscription,
  `require(Subscribed(of=Account.org(param="organization_id")))`; a personal
  subscription does not open an organization's export (402).
- **RBAC**: only `admin`/`owner` may change a ticket's status; status changes
  publish `TicketStatusChanged` and record an audit entry.
- **Idempotency**: `create` takes `idempotency_guard(scope="tickets")`, so a
  repeated `Idempotency-Key` replays the original response.
- Module code imports only the beginner names of `sandloom.sdk`; an advanced name
  carries `# sandloom: advanced <reason>` on its import line (SANDLOOM-T009).

## Quick start

Run from `examples/helpdesk/` inside an existing Sandloom checkout. The local
package sources are declared in `pyproject.toml`; PyPI installation is pending
the first public release. Follow the [shared setup](../README.md#running-them)
for development database, Redis and signing-secret configuration.

```bash
uv sync
cp .env.example .env          # set your development DB, Redis and JWT_SECRET
uv run sandloom test          # requires the separate test services in .env.test
uv run sandloom migrate upgrade-all --apply
uv run uvicorn helpdesk.asgi:app --reload   # http://localhost:8000
```

`sandloom generate` writes the generated files: each module's
`module.snapshot.json` and `routes.snapshot.json`, the stubs and DTO copies of
the contracts it calls (`helpdesk/shared/contracts/`, `identity_dto.py`,
`tenancy_dto.py`), and the exported event schemas under
`helpdesk/shared/events/`. `sandloom test` only checks them.

## Add a module

```bash
uv run sandloom add module <name>     # a new package under helpdesk/modules; nothing else to edit
uv run sandloom test
```

## Migrations

Each module owns its own migration chain (`helpdesk/modules/<m>/migrations/`).
Apply them all with `sandloom migrate`:

```bash
export DATABASE_URL=postgresql+asyncpg://...      # your dev DB
uv run sandloom migrate upgrade-all --apply
uv run sandloom migrate new <module> "description"            # autogenerate a revision
```
