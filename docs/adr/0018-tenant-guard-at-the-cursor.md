---
search:
  exclude: true
title: "ADR-0018: The tenant guard at the cursor"
---

# ADR-0018: The tenant guard at the cursor

- **Status:** Accepted (2026-11-21, by the owner, in the review of PR #342)
- **Date:** 2026-11-21
- **Origin:** three red-team rounds of the tenant guard, the last with 29 open bypasses
- **Implemented by:** the tm task `tenant-guard-read-paths` (PR #342, two fix rounds after review)

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
per dialect by `install_reach_recorder`). It sees every `visit_table`, the target of an INSERT,
UPDATE or DELETE, the string a custom `@compiles` element returns and every bare word of a
`text()` or `literal_column()` fragment. SQLAlchemy caches the compiled object. An execution of a
statement that names no tenant table costs two set checks.

A fragment is read from its raw text and no quoting rule is applied, so nothing a dialect's
quoting hides is skipped: dollar quotes, an `E'...'` string with an escaped quote, a comment, a
quoted identifier. A PostgreSQL `U&"..."` identifier spells its name in escapes no scan can read, so
it counts as reaching every tenant table. A statement the recorder did not compile (a `DDL()`
element, a compiled object of a dialect without the recorder, a compiler a driver swaps in) has its
rendered string read the same way, and is refused when it names a tenant table.

Outside `unscoped()`, a statement that names a tenant table is refused
(`UnsafeTenantOperationError`, `MissingTenantError` when no tenant is bound), with two exceptions:

- the ORM guard approved this statement (decision 2);
- it is a flush write of rows the mapper events verified (decision 4).

Tables are matched by name, not by object. A second `Table` of the same name, `table("name")` and a
`text()` fragment are the same table. The match ignores the schema, so a same-named table in
another schema is refused. The scan is a superset, and it fails closed: a literal that spells a
tenant table's name counts as reaching it.

Schema work names tenant tables too. `metadata.create_all`, `DDL(...)` and an index of a tenant
table run inside `unscoped()`; `ensure_model_schema` does so at startup, and so do the test
fixtures that build tables.

### 2. The approval

The `do_orm_execute` guard records the statement it checked in a private weak map, `_APPROVED`,
with one of two kinds: `criterion`, when it attached the tenant criterion, and `trusted`, when it
verified a column load or saw a `text()` the caller sent. The cursor admits a statement only when
`context.invoked_statement` is in the map. Nothing is written to the statement or to its execution
options. A statement a later `do_orm_execute` listener swaps in, a follow-up loader statement
(`selectinload`, `subqueryload`, `immediateload`) and a copy of the options carry no approval, so
each is checked on its own. A later listener that rewrites a tenant statement is therefore refused.

The approval is not single-use. The same statement object, run again, is approved again, and the
guard re-checks it. A `criterion` approval alone admits nothing: it needs the criterion's bind
parameter at the cursor. `TENANT_CRITERIA_PARAM` is the one bind parameter of the criterion. An
execution whose caller parameters, or a later listener, set it to another value is refused, and so
is a statement that lost it. The cursor re-checks it for every compiled parameter set, including
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

A flush write is an allowlist. The statement is admitted only when every part of it is accounted
for:

- a flush is running, a tenant is bound, and the statement is the plain Core INSERT, UPDATE or
  DELETE the persistence layer builds. A statement a column default or a listener wrote on the
  flush connection is admitted only when it passes every check below: a listener's UPDATE or DELETE
  that pins, by primary key, a row the flush verified is admitted without `unscoped()`;
- nothing it renders reads a tenant table, and it names none that no scan can read;
- it is written over a `Table` object (by identity, not by name) of a mapper whose event verified a
  row of this flush. A second `Table` of the same name, with other columns or another primary key,
  is none of them;
- an INSERT writes a table that a verified insert of this flush writes, and carries an explicit
  `tenant_id` equal to the bound tenant in every parameter set. A missing or computed value is no
  value;
- an UPDATE or DELETE pins its rows by primary key (`pk = :param`, the parameter typed as the
  column itself), and every parameter set names a `(table, primary key)` the mapper events
  verified in this flush, the key listed in the table's own column order. An UPDATE never sets
  another tenant's `tenant_id`;
- an INSERT or UPDATE never carries values of its own that name the `tenant_id` column
  (`.values(tenant_id=...)`, a SQL expression for it, a key that spells another column's name, a
  positional row). The persistence layer sets that column only through the execution parameters the
  checks above read, so a statement a listener built with its own value is refused whatever its
  parameters say.

A column load of an instance, and the flush UPDATE or DELETE of one, needs the stamp of the
instance: the `tenant_id` the row carried when it was read from the database, or when a flush
inserted it. The `load` and `refresh` events record it in `InstanceState.info`. A flush records
the stamp of an inserted instance only after the flush succeeded, and the stamp stays only once
the ROOT transaction committed. The guard keeps a record of each stamped insert per session
transaction, the savepoint or the root that was innermost when the flush ran. `after_commit`
hands the records of a released savepoint to its parent and drops those of the root, whose rows
now exist. `after_transaction_end` clears the stamps of every record the ending transaction still
owns, because it ended without committing: a rollback, an exception, or a `close()`, which rolls
the database back, an open savepoint included, without a soft rollback. A savepoint closed
without release therefore loses its stamps even when the root commits later, and a record
clears a stamp only while it still names the tenant the record made. A failed or abandoned insert
leaves nothing to re-attach with.

`merge(load=False)`, unpickling and `make_transient_to_detached` give no stamp. Flushing or
refreshing such an instance is refused. So is a deferred column load of an instance a `load_only()`
or `defer()` projection left without its `tenant_id`: the load is refused until the projection
includes the column. `Session.get`, `Query.get`, `get_one` and a many-to-one lazy load answer from
the identity map only for a stamped instance of the bound tenant (`Session._identity_lookup`). For
any other they run the scoped query. An expired instance of another tenant raises the refusal
where an unexpired one runs the scoped query: nothing crosses either way. Column defaults are
judged per rendered column: a column the statement sets itself (`values()`, `ordered_values()`,
the target list of an `INSERT ... FROM SELECT`) renders no default. SQLAlchemy leaves the
`include_insert_from_select_defaults` flag out of a statement's cache key, so the verdict of an
`INSERT ... FROM SELECT` that renders no defaults is never cached under that shared key.

### 5. What a module may write

The cursor rule needs every connection to come through the framework's engine, and no bypass to
outlive its block. Three `sandloom check` rules say it for business modules.

- **SANDLOOM-C036.** `unscoped` appears only as the direct context expression of a `with` item
  (`with unscoped():`, also in an `async def`: `unscoped()` is a synchronous context manager), with
  no `yield` inside the block, including the context expression of a later item of the same `with`.
  There is no decorator exemption: a `@contextmanager` wrapper hands the bypass to its caller, as do
  an alias bound to a variable, `ExitStack.enter_context`, `functools.partial` and `getattr`. An
  imported alias used directly as the `with` item is followed. The `as` target of the `unscoped()`
  item is evaluated inside the block, so a `yield` there is reported. The kernel's own files are
  not scanned.
- **SANDLOOM-C037.** The rule lists capabilities, not spellings. A module may not build an engine
  or a pool, import `sqlalchemy.pool` or most of `sqlalchemy.engine`, star-import from `sqlalchemy`,
  import a DBAPI driver, or call `raw_connection()`. A name bound to a constant dynamic import of
  `sqlalchemy` (`sa = importlib.import_module("sqlalchemy")`) counts as `sqlalchemy`, and so does a
  name assigned from such a name (`other = sa`, however long the chain). `CursorResult`, `Row`,
  `URL` and the other result and URL types stay allowed.
- **SANDLOOM-C015.** The `unsafe-tenant-sql` rule flags `DDL(...)` and `literal_column(...)`
  outside `with unscoped():` the way it flags `text()`, because a `DDL()` string that names a
  tenant table is refused at the cursor and a `literal_column()` text is rendered as written into
  any statement. The portability marker `# sandloom: raw-sql` reviews dialect text, not tenant
  reach, so it never lifts this rule. The attribute spelling `X.unscoped()` lifts it only when `X`
  (or the root of a dotted `X`) is a name that, in the scope of the use, an import of a `sandloom`
  module binds; a parameter, a local object or another package's module named `tc` does not, and
  one function's parameter named `tc` does not take the import from another function. C037 follows
  an alias the same way: `alias = sa` in one function makes `alias` a `sqlalchemy` name in that
  function only.

## Consequences

- Every compiled statement that names a tenant table, and that the ORM guard did not approve
  or the unit of work did not verify, is refused at the cursor, on SQLite and on PostgreSQL, with
  or without row-level security. The seams below are the exceptions. The statement walk stays: it
  decides what the guard scopes, and the cursor decides what runs.
- Some statements that were harmless are refused: a same-named table in another schema, a
  non-tenant polymorphic base with a tenant subclass, a mapper with raw tenant SQL, a literal or a
  comment that spells a tenant table's name, `DDL()` and `metadata.create_all` over a tenant table
  outside `unscoped()`. Each has a recorded reason and a way out: `unscoped()`, or the ORM entity.
  **Upgrade:** schema DDL that names a tenant table runs in `unscoped()`. `ensure_model_schema` does
  it for the app; test fixtures that create or drop the tables of a tenant model do it themselves.
- A flush writes a loaded or newly inserted instance as before. An instance built by
  `merge(load=False)` or by unpickling needs a load first, or `unscoped()`.
- The guard registers two session events, `after_commit` and `after_transaction_end`, which keep
  the stamp of an inserted instance exactly as long as its row can exist, and two mapper events
  on `TenantScoped`, `load` and `refresh`, which record the stamp of a loaded instance. The
  planner's custom-listener probe (`planner_safety.policy_reason`) treats a session or mapper
  listener as custom and takes the general executor; it exempts the guard's own
  `after_transaction_end` hook and its `load` / `refresh` stampers, which touch only the `info`
  of an instance a Session holds and so have nothing to do for the fused path, so a session with
  only the kernel's listeners keeps it.
- The test harness's schema reset (`sandloom.core.testing.database.reset_schema`) names every
  table of a disposable test database in its probe, `DELETE`, `TRUNCATE` and sequence rewind. It
  runs inside `unscoped()`. A custom `@compiles` element is read for the fragment it returns, not
  for the statement around it, so a flush `UPDATE` whose `onupdate` is such an element still names
  only its own target.
- The mapper events add a listener call to each flushed instance, and the load events one
  dictionary write to each loaded instance.
- Some seams stay with the caller or with the database, in the order they are named:
  - a hidden raw reference inside a criterion-approved statement that the ORM walk cannot see;
  - `Connection.exec_driver_sql`, which has no compiled statement;
  - a `text()` passed straight to `session.execute`, which the ORM guard approves as the caller's:
    `scoped()`, the `unsafe-tenant-sql` check and row-level security hold it;
  - a column load of a stamped instance when no tenant is bound: the instance was read under a
    tenant, and the load goes to the same row;
  - SQL the database builds on its own, from text no scan of the statement can read: PostgreSQL
    dynamic SQL (`query_to_xml('select ... ' || name, ...)` with a concatenated name,
    `table_to_xml(oid::regclass)`, a `DO $$ ... EXECUTE ... $$` block), and a view, function or
    trigger over a tenant table, which a statement reaches by naming only the view or the function.
    The cursor reads the SQL it is sent, not what the database runs on its behalf: row-level
    security is the boundary, and only for objects that run under a role its policy applies to. A
    view owned by a superuser or another role that bypasses row-level security, and a
    `SECURITY DEFINER` function owned by one, read the rows with the owner's rights. Create views
    with `security_invoker = true`, write functions as `SECURITY INVOKER`, or give them an owner
    the policy applies to;
  - a primary key reused by another tenant while an instance of the old row is still held: the
    stamp is the `tenant_id` the row carried when it was read or inserted, it does not re-read the
    row, and nothing keeps a key from being reused. A deferred column load or a flush UPDATE or
    DELETE of that instance passes the stamp check and reaches the new row. Use keys that are not
    reused across tenants; row-level security is the boundary;
  - the stamp of an inserted instance after a rollback the session never sees: a session bound to
    a connection whose OUTER transaction the caller rolls back after `session.commit()`. The
    session saw the commit and keeps the stamp, and the database later discards the row. Re-attaching
    such an instance after another tenant took its primary key is the case the stamp lifecycle
    cannot close; row-level security is the boundary;
  - private APIs: the guard's own state (the `sandloom.tenant_id` key of `InstanceState.info`,
    `sandloom.core.tenant._APPROVED`) and a private SQLAlchemy API are reachable by code that
    imports or writes them. No cursor rule stops the code that edits the guard itself.
- Some C037 cases are not seen: a dynamic import of a non-constant name, `getattr` with a variable
  name on `sqlalchemy`, and a name bound to `sqlalchemy` by anything but a plain assignment of a
  name that holds it (unpacking, a container, a call result).
- C015 and C037 resolve a name by lexical scope, not by control flow. A class body that reads a
  module alias before it assigns the same name is not reported for that read, and an alias that a
  function re-assigns to an unrelated value is still reported. The runtime cursor guard is the
  boundary; these rules are a static layer.

## Out of scope

- Raw `text()` under `unscoped()`. The block lifts the guard on purpose.
- Row-level security is the strong boundary. This guard is a second layer for databases without
  it, and for the paths RLS does not see. On a `tenant_rls` database the database still scopes every
  statement to the bound tenant.

## Related

- [Multi-tenancy](../modules/database/multi-tenancy.md): how modules use `TenantScoped`, `bind_tenant` and `unscoped()`.
- [ADR-0011: driver API](0011-driver-api.md): the `tenant_rls` database option.
