---
search:
  exclude: true
title: "ADR-0018: The tenant guard at the cursor"
---

# ADR-0018: The tenant guard at the cursor

- **Status:** Accepted (2026-11-21, by the owner, in the review of PR #342)
- **Date:** 2026-11-21
- **Origin:** three red-team rounds of the tenant guard, the last with 29 open bypasses
- **Implemented by:** the tm task `tenant-guard-read-paths`

## Context

The tenant guard has two halves. The first is a `do_orm_execute` listener. It attaches the tenant
criteria to every `TenantScoped` entity of an ORM statement, after a walk over the statement
tree that refuses a reference the criteria cannot reach. The second sits at the cursor of the
engine. It refuses a statement with a data-modifying CTE outside `unscoped()`.

The walk reads what `get_children()` lists. Three red-team rounds found that SQL reaches the
cursor from places the walk never sees:

- expressions the tree hides: `with_expression`, loader criteria, `Load` options, lambdas with an
  untracked closure, `column_property`, a relationship's `secondary`;
- statements that bypass the listener: `session.connection().execute`, a `ctx.connection` call in a
  column default, a statement built over a `Table` of another `MetaData`, a `@compiles` element;
- the persistence layer: a flush UPDATE or DELETE of an instance built by `merge(load=False)`, a
  column load of a forged instance, an identity-map lookup that skips SQL altogether;
- a parameter that overrides the tenant criterion of a guarded statement.

Each fix closed one spelling and the next round found a variant. The statement walk cannot be
closed, because it asks how the statement was built. The question that does not change is what
SQL is about to be sent.

## Decision

### 1. One rule at the cursor

`install_write_admission` already wraps `do_execute`, `do_executemany` and
`do_execute_no_params` of every engine the framework builds. The same hook now asks of every
compiled statement: which tables does it name?

The engine's statement compiler records the table names it renders (`ReachRecorder`, installed
per dialect by `install_reach_recorder`). It sees every `visit_table`, the identifiers of a
`text()` or `literal_column()` fragment, the target of an INSERT, UPDATE or DELETE and the
string a custom `@compiles` element returns. SQLAlchemy caches the compiled object. An execution
of a statement that names no tenant table costs two set checks.

Outside `unscoped()`, a statement that names a tenant table is refused
(`UnsafeTenantOperationError`, `MissingTenantError` when no tenant is bound), with two exceptions:

- the ORM guard stamped the execution (decision 2);
- it is a flush write of rows the mapper events verified (decision 4).

Tables are matched by name, not by object. A second `Table` of the same name, `table("name")` and a
`text()` fragment are the same table. The match ignores the schema, so a same-named table in
another schema is refused. That is a superset, and it fails closed.

### 2. The scoped stamp

The `do_orm_execute` guard writes the execution option `sandloom_tenant_scoped` with a private
token only after it attached the criteria, verified a column load, or saw a `text()` the caller
sent. A caller cannot forge it: the same option with another value is no stamp.

A stamped statement must still carry the bound tenant. `TENANT_CRITERIA_PARAM` is the one bind
parameter of the criterion. Any execution whose caller parameters, or a later listener, set it to
another value is refused. The cursor re-checks it for every compiled parameter set, including
`executemany` and streaming.

### 3. What the ORM annotation protects

The annotation of `with_loader_criteria` protects the mapper's own table and its plain aliases.
A tenant table reached any other way inside a stamped statement is refused: a raw `secondary`, a
`column_property` over a raw `Table`, or a subquery written by hand. The loader criteria resolve
against every mapper of the statement, not the first. An untracked-closure lambda gets no cached
verdict.

A mapper that carries such SQL refuses any bound statement over it that also reaches a tenant
table. That is a documented superset: the walk cannot say which statement the SQL ends up in.

### 4. Flush writes and column loads need a stamped instance

A flush UPDATE or DELETE, and the load of a deferred or expired column, are admitted only for an
instance whose `tenant_id` was read from the database under the bound tenant. The `load` and
`refresh` events record it in `InstanceState.info`. A verified flush INSERT records it too.

`merge(load=False)`, unpickling and `make_transient_to_detached` give no stamp. Flushing or
refreshing such an instance is refused. `Session.get`, `Query.get`, `get_one` and a many-to-one lazy
load answer from the identity map only for a stamped instance of the bound tenant
(`Session._identity_lookup`). For any other they run the scoped query. Column defaults are judged
per rendered column.

### 5. What a module may write

The cursor rule needs every connection to come through the framework's engine, and no bypass to
outlive its block. Two `sandloom check` rules say it for business modules.

- **SANDLOOM-C036.** `unscoped` appears only as the direct context expression of a `with` or
  `async with` item, with no `yield` inside the block. There is no decorator exemption: a
  `@contextmanager` wrapper hands the bypass to its caller, as do an alias, a variable,
  `ExitStack.enter_context`, `functools.partial` and `getattr`. The kernel's own files are not scanned.
- **SANDLOOM-C037.** The rule lists capabilities, not spellings. A module may not build an engine
  or a pool, import `sqlalchemy.pool` or most of `sqlalchemy.engine`, star-import from `sqlalchemy`,
  import a DBAPI driver, or call `raw_connection()`. `CursorResult`, `Row`, `URL` and the other
  result and URL types stay allowed.

## Consequences

- Every statement that reaches a tenant table without the ORM guard is refused at the cursor, on
  SQLite and on PostgreSQL, with or without row-level security. The statement walk stays: it
  decides what the guard scopes, and the cursor decides what runs.
- Some statements that were harmless are refused: a same-named table in another schema, a
  non-tenant polymorphic base with a tenant subclass, a mapper with raw tenant SQL. Each has a
  recorded reason and a way out: `unscoped()`, or the ORM entity.
- A flush writes a loaded instance as before. An instance built by `merge(load=False)` or by
  unpickling needs a load first, or `unscoped()`.
- The test harness's schema reset (`sandloom.core.testing.database.reset_schema`) names every
  table of a disposable test database in its probe, `DELETE`, `TRUNCATE` and sequence rewind. It
  runs inside `unscoped()`. A custom `@compiles` element is read for the fragment it returns, not
  for the statement around it, so a flush `UPDATE` whose `onupdate` is such an element still names
  only its own target.
- The mapper events add a listener call to each flushed instance, and the load events one
  dictionary write to each loaded instance.
- Some seams stay with the caller or with the database, in the order they are named:
  - a hidden raw reference inside a stamped statement that the ORM walk cannot see;
  - `Connection.exec_driver_sql`, which has no compiled statement;
  - a `text()` passed straight to `session.execute`, which the ORM guard stamps as the caller's;
  - a private SQLAlchemy API, which stays reachable but yields an unstamped statement the
    cursor refuses;
  - a column load of a stamped instance when no tenant is bound.
- Two C037 cases are not seen: a dynamic import of a non-constant name, and `getattr` with a
  variable name on `sqlalchemy`.

## Out of scope

- Raw `text()` under `unscoped()`. The block lifts the guard on purpose.
- Row-level security is the strong boundary. This guard is a second layer for databases without
  it, and for the paths RLS does not see. On a `tenant_rls` database the database still scopes every
  statement to the bound tenant.

## Related

- [Multi-tenancy](../modules/database/multi-tenancy.md): how modules use `TenantScoped`, `bind_tenant` and `unscoped()`.
- [ADR-0011: driver API](0011-driver-api.md): the `tenant_rls` database option.
