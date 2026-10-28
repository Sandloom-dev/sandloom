---
title: Multi-tenancy
description: Fail-closed tenant scoping - TenantScoped tables, the tenant binding, automatic filtering, unscoped(), single-tenant mode and PostgreSQL row-level security.
type: concept
---

# Multi-tenancy

Sandloom provides **fail-closed tenant scoping** so you never hand-write
`where(Model.tenant_id == ...)` on every query: forget it once and you leak data
across tenants. The framework filters automatically.

## What you get by default, and what you declare

| You write | Sandloom does for you |
| --- | --- |
| a model that inherits `TenantScoped` | an indexed `tenant_id` column, filled from the bound tenant on insert; every read, update and delete filtered to the bound tenant; fails closed with no tenant bound |
| `router = module_router()` in a module with a `TenantScoped` model | binds the tenant on every route from the signed token (`400 tenant context required` when missing) |
| `sandloom migrate new <module> "<msg>"` | `enable_tenant_rls` / `disable_tenant_rls` in the revision for every new `TenantScoped` table |
| `with unscoped():` | lifts the ORM guard for deliberate cross-tenant system work (never PostgreSQL RLS) |
| a global table | a `tenant_scope_exempt` entry with a written reason in `architecture_rules.yml` |

## Opt a model into scoping

A tenant-owned model inherits `TenantScoped`, which declares the indexed
`tenant_id` column and opts into automatic filtering:

```python
from sandloom.sdk import Base, TenantScoped


class TicketsTicket(TenantScoped, Base):
    __tablename__ = "tickets_tickets"
    ...
```

A model (or Core `Table`) that declares its own `tenant_id` without
`TenantScoped` fails `sandloom check`
([SANDLOOM-C014](../reference/error-codes.md#sandloom-c014)); a deliberately global
table goes under `tenant_scope_exempt` in `architecture_rules.yml` with a
written `reason` (stale entries fail too).

## Bind the tenant

`module_router()` binds the tenant on every route of a module that declares a
`TenantScoped` model (`tenant="derive"`; a route's own `tenant=` overrides).
A signed `tenant_id` / `org_id` access-token claim binds directly.
`X-Tenant-Id` is only a selector: the requested id must also be in the token's
signed `tenant_ids` allowlist, otherwise the request is rejected with `403`.
A missing tenant answers `400 tenant context required`
([SANDLOOM-R001](../reference/error-codes.md#sandloom-r001)).

```python
from sandloom.sdk import CurrentUser, db, module_router

router = module_router()          # the tenant is bound on every route


@router.get("/tickets")
async def list_tickets(user: CurrentUser) -> list[TicketOut]:
    rows = await db().scalars(select(TicketsTicket))   # filtered to the bound tenant
    return [TicketOut.model_validate(r) for r in rows]
```

A route that needs the tenant id takes `TenantId` (advanced, `sandloom.sdk.web`).
A plain router binds with `TenantScope()` (`sandloom.sdk.web`). Workers, consumers
and scripts bind explicitly with `bind_tenant(id)` (`sandloom.sdk.tenant`); a
consumer of an event with a `tenant_id: UUID` field gets it bound
automatically ([Events](events.md)).

## How filtering works

Every ORM `select`, update, or delete that reaches a `TenantScoped` table fails
if no tenant is bound. Once bound, it is auto-filtered (a `do_orm_execute`
hook). The guard looks at the **whole statement**, not only the top-level
entity: joins (including relationship joins), `select_from`, `EXISTS` / `IN` /
scalar subqueries, eager-load options, and relationship lazy/`selectin` loads.
`Session.get()` is covered too — an identity-map hit for another tenant's row
is refused. Normal ORM inserts inherit the current tenant and reject a
conflicting `tenant_id`. Bulk inserts and bulk ownership changes are reserved
for explicit `unscoped()` system work.

Raw `text()` and Core statements carry no ORM criteria. Core SQL on a
recognised tenant table is refused when it cannot be safely scoped, and
runtime raw SQL in a business module needs an explicit `unscoped()` admission
([SANDLOOM-C015](../reference/error-codes.md#sandloom-c015)). Use the explicit helpers
there, or enable [row-level security](#postgresql-row-level-security-optional).
For explicit, hard-to-misuse reads:

```python
from sandloom.sdk.tenant import current_tenant_id, scoped, tenant_filter  # sandloom: advanced explicit scope

await db().execute(scoped(select(TicketsTicket), TicketsTicket))   # explicit scope
tenant_filter(TicketsTicket)                                        # the filter clause
current_tenant_id()                                                 # the bound tenant id
```

All of these **fail closed** — they raise `MissingTenantError` if no tenant is
bound.

## Deliberate cross-tenant access

Admin or system code that genuinely must cross tenants does so explicitly:

```python
from sandloom.sdk.tenant import unscoped  # sandloom: advanced system maintenance

with unscoped():
    ...  # the ORM guard is lifted for this block
```

`unscoped()` disables the ORM guard only. On a database with
[Row-Level Security](#postgresql-row-level-security-optional) the database still
scopes every statement to the bound tenant; cross-tenant work there runs on
the framework's audited system channel.

## Single-tenant deployments

Single-tenant is the strict one-tenant case of the same model, not an isolation
bypass:

```dotenv
TENANCY_MODE=single
SINGLE_TENANT_ID=00000000-0000-0000-0000-000000000001
```

Request and worker UoWs automatically bind that canonical tenant. Migrations
and deliberate cross-tenant maintenance must use `unscoped()` (with
`tenant_rls`, the system channel — see below). Every projection
row also carries `tenant_id`; filter on it first.

## PostgreSQL Row-Level Security (optional)

For defense in depth the same invariant can be enforced **inside the
database**. Enable it per database instance:

```json
{"database": {"default": {"tenant_rls": true}}}
```

(the `INFRA` setting). The framework then keeps the transaction-local setting
`sandloom.tenant_id` in sync with the bound tenant. Add the policy to each
tenant-owned table from the module's migration:

`sandloom migrate new` renders these calls for every new `TenantScoped` table
(`system_access=True` only for a model with `__system_access__ = True`):

```python
from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  # sandloom: advanced RLS


def upgrade() -> None:
    op.create_table("notes_notes", ...)   # has a tenant_id uuid column
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        enable_tenant_rls(op, "notes_notes")
        # only for tables that privileged system jobs must reach across tenants:
        # enable_tenant_rls(op, "notes_notes", system_access=True)


def downgrade() -> None:
    if op.get_context().config.attributes.get("sandloom_tenant_rls", False):
        disable_tenant_rls(op, "notes_notes")
    op.drop_table("notes_notes")
```

The policy is fail-closed and has **no bypass**: the application role sees and
writes only rows of the tenant in `sandloom.tenant_id`; no tenant, an empty or a
malformed value → no rows, refused writes. `unscoped()` lifts only the ORM
guard — on a `tenant_rls` database it does **not** open other tenants' rows
(a warning is logged when it runs with no tenant bound). Cross-tenant work goes
through the framework's *system channel* (below).

### Roles

RLS only confines a role that cannot step outside it. Use three roles:

| Role | Used by | Must be |
|---|---|---|
| migration/owner role | `sandloom migrate` (owns the tables) | anything; never the app role |
| application role | `DATABASE_URL`: every request, worker and contract unit | `NOSUPERUSER NOBYPASSRLS`, not the owner, not a member of the owner/system/superuser roles; DML grants only |
| system role (`sandloom_system`) | `DATABASE_SYSTEM_URL` — framework system channel only | `NOSUPERUSER NOBYPASSRLS`; DML grants only on `system_access` tables, `INSERT` on `audit_log` |

```sql
CREATE ROLE sandloom_app    LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE sandloom_system LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sandloom_app;
GRANT INSERT ON audit_log TO sandloom_system;   -- system sessions are audited
-- per-table system grants come from enable_tenant_rls(..., system_access=True)
```

At startup the API process verifies the application role (superuser,
`BYPASSRLS`, ownership of an RLS table, membership in a privileged or the
system role) and the system role (superuser,
`BYPASSRLS`) and refuses to start on any violation, in every environment.
Instance options: `system_url` (the `default` instance falls back to
`DATABASE_SYSTEM_URL`, SECRETS_DIR-aware), `require_system_channel` (fail fast
when no system DSN is configured). The role checks always run on a
`tenant_rls` instance (the former `verify_roles` option is removed; setting it
fails startup). The Celery worker runs the same checks at boot.

If the app role still owns the tables, transfer ownership (`ALTER TABLE ...
OWNER TO <migration role>`) and re-grant it DML.

### Threat model

RLS guards against a *forgotten* tenant filter (raw SQL included) and removes
every privileged bypass from the application role. It does **not**
authenticate the tenant: code that can send arbitrary SQL as the application
role can still set `sandloom.tenant_id` to another tenant. See
[the RLS threat model](../reference/security-model.md#tenant-rls-threat-model)
for the exact guarantees, residual risks and the architectural options.

## Production profile

`PRODUCTION_PROFILE=multi_tenant_strict` (production only) makes the trust
model non-optional: `tenant_rls` and role verification on every database, the
tenant policy on every `TenantScoped` table (a startup check), fail-closed
revocation, `SECURITY_CACHE_INSTANCE` on a separate Redis, and the contract
compatibility check when split. See
[the security model](../reference/security-model.md).

**Trust model.** Business modules are trusted code; trust comes from
admission (architecture checks and review) before execution, never
automatically, AI-generated code included. Sandloom protects against developer
mistakes, not against malicious Python or SQL inside its process.

## What breaks it

| Code | What it catches |
| --- | --- |
| [SANDLOOM-C014](../reference/error-codes.md#sandloom-c014) | a per-tenant table that is not `TenantScoped` |
| [SANDLOOM-C015](../reference/error-codes.md#sandloom-c015) | runtime raw SQL or Core table access that can bypass the tenant guard |
| [SANDLOOM-C024](../reference/error-codes.md#sandloom-c024) | a raw query on a non-relational store outside `unscoped()` |
| [SANDLOOM-R001](../reference/error-codes.md#sandloom-r001) | no tenant bound where one is required (`400`) |

??? note "Guarantees, precisely"

    - Fail-closed scoping of every ORM path, inserts inheriting the bound
      tenant (G-TENANT-01 to G-TENANT-08 and related entries).
    - A missing tenant is `400 tenant context required`, also when the error
      reaches the request boundary (G-TENANT-12).
    - RLS policies are fail-closed, forced and without bypass; the role checks
      run at startup (G-RLS-*).

    The ledger with the enforcing tests: [Guarantees & capabilities](../reference/guarantees.md).
