---
search:
  exclude: true
title: "ADR-0018: The tenant guard at the cursor"
---

# ADR-0018: The tenant guard at the cursor

- **Status:** Accepted (2026-11-21, by the owner, in the review of PR #342)
- **Date:** 2026-11-21
- **Origin:** three red-team rounds of the tenant guard, the last with 29 open bypasses
- **Implemented by:** the tm task `tenant-guard-read-paths` (PR #342, two fix rounds after review);
  decision 6 by `tenant-upsert-helper`

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
(`UnsafeTenantOperationError`, `MissingTenantError` when no tenant is bound), with three exceptions:

- the ORM guard approved this statement (decision 2);
- it is a flush write of rows the mapper events verified (decision 4);
- it is the one conflict insert `tenant_upsert` builds (decision 6).

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

### 6. The one conflict insert: `tenant_upsert`

A conflict insert (`INSERT ... ON CONFLICT`) on a tenant table stays refused like any statement the
ORM guard did not approve, and `conflict_insert` keeps working only inside `unscoped()`. The form
cannot be validated from what a caller builds. Its `DO UPDATE` can copy a column of the existing
row, its target can name a key that leaves out the tenant, and its slots (a CTE that carries DML,
`RETURNING`, a value of raw SQL, an index predicate) take any expression. An earlier attempt admitted
the form by checking each such slot; six review rounds each found another statement that reached
another tenant's row, as in the Context above.

So the caller builds no statement. `sandloom.sdk.data.tenant_upsert(Model, rows, update=[...])`
takes rows of plain Python values and the names of the columns to refresh, and builds the statement
itself:

- A row key is an attribute of the model, resolved through `mapper.column_attrs` to its Column. A
  value that is a SQL expression, `text()` or `literal_column()` is refused. The tenant column is
  never a row key: its value is the bound tenant, bound with the column's own type, so a
  `TypeDecorator` on it runs on the insert and on the update alike. A model whose tenant column is
  not named `tenant_id` in the database (`mapped_column("org_id")`), a model in an inheritance
  hierarchy, a model without a unique key that includes the tenant column, a model with a column that
  is not exactly `Column`, a model with a `sqlite_on_conflict*` option on a column or a constraint
  (SQLite's `REPLACE` deletes the row that holds the key before `ON CONFLICT` is asked, whichever
  tenant owns it) and a call whose statement would read a tenant table (a column default, `onupdate`
  or type `bind_expression` that selects from one) are refused with `SANDLOOM-R018`, before anything
  is sent and before the session is flushed.
- The conflict target is a unique key of the table that includes the tenant column. A row of another
  tenant is never the row that conflicts, so the update never touches it. That is the whole claim:
  every other key and every foreign key acts as on a plain insert (see the Consequences). A
  `DEFERRABLE` key is no candidate, because PostgreSQL cannot use it as the arbiter of `DO UPDATE`.
- `DO UPDATE SET <column> = excluded.<column>` for the listed columns, which are columns of the table
  other than the tenant column and the primary key, and `WHERE <tenant column> = <the bound tenant>`.
  There is no `RETURNING`, no CTE, no caller expression. The helper runs it on the unit's connection
  with `executemany`, so no value is rendered into the SQL. The dialect that renders it is the one of
  the bind the connection resolves for the model's mapper.

One function writes the statement: `canonical_tenant_upsert(dialect, table, target, update)` in
`sandloom.core.sql_alchemy.tenant_upsert`. The helper builds with it, and the cursor guard builds with
it again, so the guard checks the helper's statement against the helper's own builder and not against
a second description of it.

The cursor admits this statement and no other conflict insert on a tenant table. The helper marks
the statement it is about to run in a private weak set, `_UPSERTS`, and removes the mark when the call
ends, whether it succeeds or fails, so a captured statement cannot be run again. The mark alone
admits nothing. The guard reads the statement that ran (`context.invoked_statement`), never the compiled
object, which every statement of the same cache key shares: its reading is kept per marked statement
object and never on the compiled form. For a marked statement the cursor checks, on the statement and
on every parameter set:

- the parts. The statement is an `INSERT` over a `Table` that is, by identity, the `local_table` of a
  `TenantScoped` mapper (a second `Table` of the same name, whose tenant column is a `TypeDecorator`
  that rewrites its value, is none), made of plain `Column` objects, with every slot SQLAlchemy lists
  in its `_traverse_internals` empty but the table and the `ON CONFLICT DO UPDATE` clause, whose own
  slots are read the same way. The check reads SQLAlchemy's list of slots, not a list of ours, so a
  slot a later SQLAlchemy adds refuses until the check reads it. The target is columns of the table
  with the tenant column among them. `SET` writes `excluded.<column>` of the same table into a column
  that is neither the tenant column nor in the primary key. `WHERE` is one comparison, the tenant
  column equal to the bound parameter `sandloom_tenant_upsert__` typed as the column itself, a
  callable bind that reads the bound tenant when the statement runs;
- the text. The guard rebuilds the statement from those parts with `canonical_tenant_upsert`, compiles
  it as this execution compiled the statement (dialect, column keys, `executemany`), and admits only
  when the two SQL strings are equal and the statement, the clause and every `SET` value are of
  exactly the classes of the rebuild. A subclass of `Insert` or of a column, or an object that
  renders itself, that renders another SQL (a conflict target moved to the primary key, a `WHERE` that
  is always true, a sub-select in a `SET` value) is read where it lands: in the text;
- the values. The tenant column holds the bound tenant in every parameter set and so does the `WHERE`
  parameter, so an execution parameter that overrides either is refused. "Holds the bound tenant" is
  `type(value) is uuid.UUID and value == tenant`, never `value == tenant` alone: `==` runs the
  `__eq__` of the object the caller supplied, and a `uuid.UUID` subclass whose `__eq__` says yes is
  bound to the database as the UUID it was built from. The same helper decides every tenant
  comparison of the guard that reads a value a caller can supply (the flush insert and update, the
  criterion parameter). A tenant read from the database is a driver subclass (asyncpg's `UUID`):
  the bound tenant is read as a plain `uuid.UUID` of the same bits (`_bound_tenant`), and a row that
  carries such a subclass for the bound tenant is given the plain value at the flush, so nothing of
  the caller's object is bound to a statement; a subclass that disagrees with itself (its `__eq__`
  says yes, its bits say another tenant) is `TenantMismatchError`;
- the reach. The statement reads no tenant table and writes only the upserted one.

The mark counts only at the cursor. The same statement handed to `session.execute` is the conflict
insert it always was, and the ORM guard refuses it.

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
- `tenant_upsert` returns nothing and refreshes no instance the session already holds: an instance of
  an upserted row keeps its old values until it is refreshed. It flushes the session's pending changes
  first. A batch that names one key twice is the caller's to avoid: PostgreSQL refuses it. **Upgrade:**
  a module that ran `conflict_insert` on a tenant model inside `unscoped()` to upsert for one tenant
  moves to `tenant_upsert`, which needs a unique key that includes `tenant_id`; a model whose only
  key leaves the tenant out keeps `unscoped()` or gets such a key in a migration.
- The helper hides no key and no row that another tenant holds beyond the conflict target; the
  database answers as for a plain insert. A primary key (`id` alone) or another unique key that
  another tenant owns makes the insert fail on that key, not on the conflict target, with the
  database's integrity error, which shows that the key exists. A foreign key to a tenant table locks
  the row it references, and the row may be another tenant's. Use keys that are not reused across
  tenants, as for the stamp above, and make a foreign key composite, `(tenant_id, parent_id)`.
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
