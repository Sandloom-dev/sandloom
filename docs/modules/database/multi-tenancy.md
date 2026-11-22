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
| `router = module_router()` in a module with a `TenantScoped` model | binds the tenant on every route from the signed token (`400 tenant_required` when missing) |
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
([SANDLOOM-C014](../../reference/error-codes.md#sandloom-c014)); a deliberately global
table goes under `tenant_scope_exempt` in `architecture_rules.yml` with a
written `reason` (stale entries fail too).

## Bind the tenant

`module_router()` binds the tenant per route with `tenant="derive"`:
`required` when the module declares a `TenantScoped` model, else `none`. A
`tenant=` of `"required"`, `"optional"` or `"none"` on the router or the route
overrides it.

`TenantScope` picks the tenant in this order. The header (`X-Tenant-Id`, or the
`TenantScope(header=...)` the route names) is only a selector. Its id is
accepted when the token's signed `tenant_ids` allowlist lists it or when it is
the token's signed `tenant_id` or `org_id` claim. Any other id is rejected with
`403 tenant_not_authorized`. Without the header the `tenant_id` claim, then the
`org_id` claim, binds directly. Without either claim a token that lists
exactly one tenant in `tenant_ids` binds it. A token that lists several and no
header gets `400 tenant_required`; only `tenant_ids` decides that `400`. A
grant the claim policy removed answers `401 token_policy_outdated` or `503
claims_policy_mismatch` before these answers. With `TENANCY_MODE=single` the
configured tenant binds. A header or token tenant that names another one
answers `403 tenant_mismatch`.

The `403 tenant_not_authorized` `detail` names the header the route reads. It
tells the user to refresh the access token or log in again when the
organization was joined or created after the token was issued.

With no tenant to select the answer is `400 tenant_required`. Its `detail`
names the header and says the value is an organization id that the access token
lists. Both bodies carry the `code` and the HTTP statuses are unchanged. The
`detail` text is part of the API. A client branches on `code`. See
[Upgrading to 0.7.0a16](../../operate/upgrade.md#upgrading-to-070a16-tenant-selection-errors-carry-a-code).
A `MissingTenantError` that reaches the request boundary answers the same `400`
([SANDLOOM-R001](../../reference/error-codes.md#sandloom-r001)). The route bound
no tenant there, which is a server-side fault, so its text names no header: it
says the route needs a tenant scope.

The binding scopes data only and never chooses a billing account
([Authorization](../http/authorization.md#billing-subjects-of-is-required)).

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
automatically ([Events](../between/events.md)).

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

The legacy `Session.bulk_save_objects` and `Session.bulk_insert_mappings` plant
rows. `Session.bulk_update_mappings` rewrites them by primary key. None of the
three runs the ORM-execute guard or the flush ownership check. On a
`TenantScoped` model, or any mapper over its table, each is refused outside
`unscoped()`, with or without a bound tenant. On other models they are
untouched.

The guard also reads SQL that a statement carries inside itself, which
`get_children()` does not list:

- `with_loader_criteria(...)` and the extra criteria of a relationship option
  such as `selectinload(Parent.children.and_(...))`. A subquery in them is
  scoped to the bound tenant like any other.
- `insert(Other).values(...)` in every form: dict, keyword arguments, a list of
  rows. A subquery reading a `TenantScoped` model is scoped. One reading its raw
  `Table` is refused.
- `with_expression(...)`. SQLAlchemy strips the annotation the tenant criterion
  needs from its expression. A subquery that reaches a `TenantScoped` table is
  refused outside `unscoped()`, whichever tenant's row it reads.
- A bare `func.f(...)` executable, which is none of select, insert, update or
  delete. It is refused when it reaches a `TenantScoped` table.
- A SQL-expression column `default=` or `onupdate=` that reads a `TenantScoped`
  table, on a table that is not itself tenant-scoped. It is refused on an
  INSERT or UPDATE statement and, at `before_flush`, on a flushed object. Set
  such a value in Python, or run the write inside `unscoped()`.

Each refusal is `UnsafeTenantOperationError` under a bound tenant. With none
bound, the statement raises `MissingTenantError` or
`UnsafeTenantOperationError`.

Raw `text()` and Core statements carry no ORM criteria. Core SQL on a
recognised tenant table is refused when it cannot be safely scoped. Runtime
raw SQL in a business module (`text()`, `DDL()`, `literal_column()`)
needs an explicit `unscoped()` admission
([SANDLOOM-C015](../../reference/error-codes.md#sandloom-c015)). The portability
marker `# sandloom: raw-sql` reviews dialect text, not tenant reach, so it does
not lift that check. The attribute spelling `tc.unscoped()` counts only when `tc`
is an import of a `sandloom` module. Use the explicit helpers
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

`unscoped()` lasts for its block. A task created inside the block
(`asyncio.create_task`, `gather`) copies the context, but its bypass ends with
the block. A task still running after the block exited is guarded again and
fails closed without a bound tenant. Await such a task inside the block, or give
it its own `unscoped()`. Nested blocks hold the bypass while any of them is
open: a task created in an inner block keeps it until the outer block exits
too. An event consumer's delivery always runs guarded, even when the bus task
that runs it was first started inside an open `unscoped()`.

In a module `unscoped` is written one way: as the direct context expression of
a `with` item, with no `yield` inside the block. It is a synchronous context
manager, so write `with unscoped():` inside an `async def` too. A generator
that yields inside `with unscoped():` keeps the guard off for its caller until
the generator closes, because the block stays open across the `yield`.

Other spellings let the bypass outlive its block too: a `@contextmanager`
wrapper, an alias, a variable, `ExitStack.enter_context(unscoped())`,
`functools.partial(unscoped)`. A `@contextmanager` function is not exempt.
Collect the rows in the block and yield them after it. `sandloom check` reports
each of these
([SANDLOOM-C036](../../reference/error-codes.md#sandloom-c036)). It scans
modules, not the kernel's own code.

The ORM guard does not scope everything a statement can reach. Its tenant
criteria apply to the SELECTs the ORM builds from a mapped class. A write carried
as a CTE is outside it. So is a core `Table` built by name, a hand-written
clause, a column load or a bulk statement. So the cursor of every engine the
framework builds checks the compiled SQL, the text about to be sent
([ADR-0018](../../adr/0018-tenant-guard-at-the-cursor.md)). It does not matter
whether a session or a bare connection ran the statement. Nor does it matter
where the tree hangs the reference: a loader option or a custom `@compiles`
element still ends up in the compiled SQL.

Outside `unscoped()`, a compiled statement that reaches a tenant table is
refused. Two kinds pass: a statement the ORM guard approved, and a flush write
of rows the unit of work verified. A refusal is `UnsafeTenantOperationError`
under a bound tenant and `MissingTenantError` with none.

A tenant table is matched by name. A `Table` of another `MetaData`, a
`table("name")` and `text("select ... from name")` reach it as much as the
mapped class does. The match ignores the schema, so a same-named table in
another schema is refused too.

Raw fragments are read word by word, whatever their quoting. A literal or a
comment that spells a tenant table's name counts. So does a PostgreSQL
`U&"..."` identifier, for every tenant table at once, because its name is
written in escapes. The cursor reads a `DDL()` element whole the same way.
Schema work over a tenant table therefore runs in `unscoped()`. That covers
`metadata.create_all`, `DDL(...)` and index creation. The framework's
`ensure_model_schema` does this. A test fixture that creates the tables of a
tenant model wraps its DDL too.

A data-modifying CTE is refused whichever table it targets, because the
statement does not tell reliably which one. SELECT-only CTEs that reach no
tenant table stay allowed.

What follows from it:

- The ORM guard approves the statement it checked in a private map, so a caller
  cannot forge the approval. A statement that a later `do_orm_execute` listener
  replaced carries none, and neither does the follow-up statement of a
  `selectinload`. The tenant criterion parameter must reach the cursor holding
  the bound tenant. A value of the caller's, in the parameters or from a later
  listener, is refused.
- The approval covers the mapper's own table and its plain aliases. A tenant
  table reached any other way inside that statement is refused: a raw
  `secondary`, or a hand-written subquery.
- A flush writes through an allowlist, over the `Table` objects of the mappers
  the unit of work verified in that flush. A second `Table` of the same name is
  none of them. An INSERT carries an explicit `tenant_id` equal to the bound
  tenant. An UPDATE or DELETE names a row, by primary key, that the unit of work
  verified in that flush. A statement that a column default or a listener wrote
  on the flush connection is refused unless it passes these same checks: a
  listener's UPDATE or DELETE that pins, by primary key, a row verified in that
  flush is admitted without `unscoped()`. An INSERT or UPDATE that sets
  `tenant_id` through values of its own is refused: `.values(tenant_id=...)`, an
  expression, a positional row.
- The load of a deferred or expired column, and the flush UPDATE or DELETE of an
  instance, need a stamp of the bound tenant. The stamp is the `tenant_id` read
  from the database, or the one a successful flush inserted the instance with.
  An insert keeps its stamp only once the root transaction committed. The
  session tracks the inserts per transaction: a released savepoint hands its
  inserts to its parent, and a savepoint or root transaction that ends without
  committing clears the stamps of the inserts it owned. A failed or rolled back
  insert leaves none, and neither does one the session or a savepoint closed
  without committing. An instance from `merge(load=False)` or from unpickling
  is refused at that point, and so is one that went through
  `make_transient_to_detached`. The same holds for a deferred column of an
  instance that a `load_only()` or `defer()` projection loaded without its
  `tenant_id`. Include the column in the projection.
- `Session.get`, `get_one` and a many-to-one lazy load answer from the identity
  map only for such an instance. For any other they run the scoped query.
- `unscoped()` lifts all of it for its block.

Cases the check does not see.

- A `nesting=True` CTE below the top level is not in the compiled list, and
  PostgreSQL itself refuses a data-modifying statement there.
- A raw reference to a tenant table hidden inside a statement the ORM guard
  approved, which the ORM walk cannot see.
- `Connection.exec_driver_sql` sends a string with no compiled statement.
- SQL the database builds from text no scan can read. That covers PostgreSQL
  dynamic SQL: `query_to_xml('select ... ' || name, ...)` with a concatenated
  name, `table_to_xml(oid::regclass)`, a `DO $$ ... EXECUTE ... $$` block. It
  also covers a view, function or trigger over a tenant table, reached by naming
  only the view or the function. The cursor reads the SQL it is sent.
  [Row-Level Security](#postgresql-row-level-security-optional) holds these only
  when they run under a role the tenant policy applies to. A view owned by a
  superuser or another role that bypasses row-level security, and a
  `SECURITY DEFINER` function owned by one, read the rows with the owner's
  rights. Create views with `security_invoker = true`, write functions as
  `SECURITY INVOKER`, or give them an owner the policy applies to.
- A `text()` passed straight to `session.execute` is the caller's
  responsibility. Write it with `scoped()`, or let the `unsafe-tenant-sql` check
  and [Row-Level Security](#postgresql-row-level-security-optional) hold it.
- The guard's private state, the `sandloom.tenant_id` key of
  `InstanceState.info`, and private SQLAlchemy APIs are open to code that writes
  or imports them.

Two more cases concern the stamp. The load of a column of a stamped instance
with no tenant bound is not refused. The instance was read under a tenant, and
the load goes to its own row. A rollback the session never sees keeps the stamp
of an inserted instance. This happens when the session is bound to a connection
whose outer transaction the caller rolls back after `session.commit()`. The
database discards the row, and Row-Level Security holds that case.

A stamp is the `tenant_id` the row carried when the instance was read or
inserted. It does not re-read the row, and no rule keeps a primary key from being
reused. If a row is deleted and another tenant takes its primary key while an
instance of the old row is still held, a deferred column load or a flush UPDATE
or DELETE of that instance still passes the stamp check and reaches the new row.
Use primary keys that are not reused across tenants (UUIDs, or sequences that do
not recycle values). Row-Level Security holds this case too.

The `sandloom check` rules are static and miss some spellings. SANDLOOM-C037
does not see a dynamic import of a name that is not a constant, or `getattr` with
a variable name on `sqlalchemy`. It does not follow a name bound to `sqlalchemy`
by unpacking, a container or a call result either. It does follow a plain
assignment of a name that holds it, however long the chain.

Cross-tenant work uses `unscoped()`.

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

## Database-enforced and application-enforced isolation

Tenant isolation holds at one of two strengths on each database instance:

| Isolation | Where | What confines a statement |
| --- | --- | --- |
| **database-enforced** | `tenant_rls: true` on a database with row security (PostgreSQL) | the ORM guard **and** the database's policies, which confine raw SQL too |
| **application-enforced** | every other instance, a database without row security (SQLite) included | the ORM guard only; raw SQL is not confined |

The SQL of row security belongs to the database's SQL dialect renderer
(`row_security()`). It writes the policy DDL and binds the tenant in each
transaction. It also checks the roles at startup. A renderer without it, such
as SQLite's, cannot enforce isolation in the database. On such an instance:

- `tenant_rls`, `system_url` and `require_system_channel` stop startup with
  [SANDLOOM-B026](../../reference/error-codes.md#sandloom-b026): nothing could
  provide what they promise.
- When the application maps a `TenantScoped` table, startup logs one warning
  per such instance on the `sandloom.app` logger. It names the instance and
  "application-enforced tenant isolation". The guarantee lattice computes the
  same label from the instance's features.
- The strict production profile refuses it, since it requires `tenant_rls`
  on every instance (see [Production profile](#production-profile)).

A PostgreSQL instance without `tenant_rls` is application-enforced too. That
is the operator's choice, and startup logs no warning for it.

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
[the RLS threat model](../../advanced/security-model.md#tenant-rls-threat-model)
for the exact guarantees, residual risks and the architectural options.

## Production profile

`PRODUCTION_PROFILE=multi_tenant_strict` (production only) makes the trust
model non-optional: `tenant_rls` and role verification on every database, the
tenant policy on every `TenantScoped` table (a startup check), fail-closed
revocation, `INFRA.atomic_store.security` on a separate Redis, and the contract
compatibility check when split. See
[the security model](../../advanced/security-model.md).

**Trust model.** Business modules are trusted code; trust comes from
admission (architecture checks and review) before execution, never
automatically, AI-generated code included. Sandloom protects against developer
mistakes, not against malicious Python or SQL inside its process.

## What breaks it

| Code | What it catches |
| --- | --- |
| [SANDLOOM-C014](../../reference/error-codes.md#sandloom-c014) | a per-tenant table that is not `TenantScoped` |
| [SANDLOOM-C015](../../reference/error-codes.md#sandloom-c015) | runtime raw SQL or Core table access that can bypass the tenant guard |
| [SANDLOOM-C024](../../reference/error-codes.md#sandloom-c024) | a raw query on a non-relational store outside `unscoped()` |
| [SANDLOOM-C036](../../reference/error-codes.md#sandloom-c036) | `unscoped` used other than as a `with` item, or a `yield` inside that block, so the bypass outlives its block |
| [SANDLOOM-C037](../../reference/error-codes.md#sandloom-c037) | a module that builds an engine or a pool, or opens a driver connection, around the framework's engine and its cursor check |
| [SANDLOOM-R001](../../reference/error-codes.md#sandloom-r001) | no tenant bound where one is required (`400`) |
| [SANDLOOM-B026](../../reference/error-codes.md#sandloom-b026) | `tenant_rls` or the system channel on a database without row security |

??? note "Guarantees, precisely"

    - Fail-closed scoping of every ORM path, inserts inheriting the bound
      tenant (G-TENANT-01 to G-TENANT-08 and related entries).
    - The legacy `Session.bulk_*` methods are refused on tenant models, and
      SQL hidden in loader options, `values()` or column defaults is scoped or
      refused (G-TENANT-21, G-TENANT-22).
    - A missing tenant is `400 tenant_required`, also when the error
      reaches the request boundary (G-TENANT-12).
    - RLS policies are fail-closed, forced and without bypass; the role checks
      run at startup (G-RLS-*).

    The ledger with the enforcing tests: [Guarantees & capabilities](../../reference/guarantees.md).
