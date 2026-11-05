---
title: Database
description: The relational interface behind db() and the unit of work, its sqlalchemy driver for PostgreSQL and what another driver would have to provide.
type: concept
---

# Database

The database is your system of record: it backs the unit of work, the outbox and audit.

## What a module calls

`db()` is the session of the running unit on the module's database
([Units of work](../modules/database/units.md)). A named database is
`Annotated[AsyncSession, Database("x")]` in a route, or `open_uow("x")` in a block. Each
database has its own unit and its own outbox, and no transaction spans two of them. A route
that writes to several needs `writes(...)` or `allow_multi_db_write(...)`
([Write admission](../advanced/write-admission.md)).

## Drivers

### `sqlalchemy`

PostgreSQL through asyncpg, the relational backend. Set `DATABASE_URL`. The URL must
name its dialect and its async DBAPI, `postgresql+asyncpg://`; never rely on a bare
`postgresql://`. The same driver serves SQLite through aiosqlite, for development and tests
([SQLite](#sqlite-for-development-and-tests)): `sqlite+aiosqlite:///var/app.db`.

The settings check only the URL's shape: `<dialect>+<dbapi>://`. Startup
checks the rest before it connects. The dialect needs a renderer. The DBAPI
must be one SQLAlchemy knows, and async. Each failure stops it with
[SANDLOOM-B024](../reference/error-codes.md#sandloom-b024).

| Option | Meaning |
| --- | --- |
| `url` | the connection URL; falls back to `DATABASE_URL` |
| `tenant_rls` | turns on PostgreSQL row-level security ([Multi-tenancy](../modules/database/multi-tenancy.md)) |
| `system_url` | the separate login role for framework cross-tenant work; needs `tenant_rls` |
| `require_system_channel` | fails startup when no system channel is configured |

## The relational paradigm

`database` is the **relational** capability: the SQL-shaped system of record
that backs the unit of work, the outbox and audit. The framework abstracts
engine construction and pooling; the query dialect is SQLAlchemy, used only
in `models/` and `services/`. Sibling paradigms (`document_store`,
`graph_store`, ...) have their own contracts and weaker guarantees, so no
universal "database" abstraction has to span them. The relational guarantees
are features (`transactional_outbox`, `audit`), not assumptions: a driver
that cannot provide them does not advertise them. No distributed transaction
spans backends.

The relational backend is not freely swappable today: the only driver is
`sqlalchemy`, the unit of work, outbox, audit, tenancy and migrations are
SQLAlchemy code in the kernel, and the settings require a
`postgresql+asyncpg://` URL. In practice it is PostgreSQL.

Sandloom isolates business code from connecting to and operating infrastructure;
the full set of transactional guarantees is implemented and verified on
PostgreSQL through the SQLAlchemy driver. The unit guarantees (one writer
per request, SANDLOOM-R007; conservative write detection at the statement
boundary; refusal of an exited unit, SANDLOOM-R002; the lazy request unit;
unflushed changes counted as writes) are driver obligations, not kernel
code. A database driver advertises them as the `write_admission` feature
(with `transactions`), implements `LazyUnitOfWorkContract` (`open_lazily` /
`close`) and `DatabaseProvider.has_pending_changes`, and `create_app`
refuses (SANDLOOM-B010) a database that hosts request units without
`write_admission`. Another relational driver must implement these features
to host request units; a driver conformance suite for them is planned.

## Portable SQL forms

A module writes no SQL dialect ([SANDLOOM-C028](../reference/error-codes.md#sandloom-c028)).
For the few things a dialect used to give it, `sandloom.sdk.data` has portable forms
([Module tables](../modules/database/conventions.md#columns-and-statements-on-any-database)).
Each database's **SQL dialect renderer** writes them in that database's SQL. A
renderer belongs to the database's driver package. It names itself in the
`sandloom.sql_dialects` entry-point group under its SQLAlchemy dialect name. The
`sandloom` package ships the PostgreSQL and SQLite renderers.

| Form | PostgreSQL | SQLite (development and tests) |
| --- | --- | --- |
| `CaseInsensitiveText` | `citext` | `VARCHAR COLLATE NOCASE` (ASCII letters only) |
| `partial_index` | `CREATE INDEX ... WHERE` | `CREATE INDEX ... WHERE` |
| `search_index` | `pg_trgm` GIN index | not created; the search runs unindexed |
| `conflict_insert` | `INSERT ... ON CONFLICT` | `INSERT ... ON CONFLICT` |
| `transaction_lock` | `pg_advisory_xact_lock` | the unit's own write lock (`BEGIN IMMEDIATE`); refused in a read-only unit |
| `Array(item_type)` | `item_type[]` | refused (no `arrays`) |
| `fulltext_index`, `matches` | GIN index over `to_tsvector`, `@@ websearch_to_tsquery` | refused (no `fulltext_search`) |
| `json_path` | `#>>`, an index step only into an array | `json_extract`, booleans as `'true'` / `'false'`; the same text for strings, booleans and 64-bit integers |
| `json_contains` | `jsonb @>` | refused (no `json_contains`) |

The kernel's own locks come from the same renderer. Its `lock_statement` gives
the HTTP idempotency try-lock, the projection purge lock and the migration
locks, which are advisory locks on PostgreSQL. `transaction_lock` is built on it
too.

The outbox relay and the saga sweep are work queues. Where the renderer
declares `skip_locked`, as PostgreSQL's does, parallel workers split them with
`FOR UPDATE SKIP LOCKED`. The relay claims a whole batch in one statement when
the database also has `UPDATE ... RETURNING`.

Without `skip_locked`, a worker claims each row by compare-and-set: the outbox
row's lease (`locked_until`, `locked_by`) or the saga instance's `swept_at`. The
row is the worker's only when its `UPDATE` changed one row. Workers then run
safely in parallel but do not split the work: a claim waits for a row another
worker holds instead of skipping it. The outbox still delivers at least once.

No form falls back to other SQL. A statement the database cannot write refuses
with [SANDLOOM-R015](../reference/error-codes.md#sandloom-r015). Startup checks
three things and stops with
[SANDLOOM-B024](../reference/error-codes.md#sandloom-b024) or R015:

- each database's URL names a dialect its driver declares it serves (the
  `sql_dialects=` of the driver's registration; `sqlalchemy` serves PostgreSQL
  and SQLite);
- that dialect has a renderer;
- every `partial_index` has a condition there, so none would index every row.
  The migration environment checks the same before it writes or applies a
  revision.

A database instance advertises the driver's base features plus its renderer's:

| Renderer feature | PostgreSQL | SQLite |
| --- | --- | --- |
| `skip_locked` | yes | no |
| `transaction_locks` | yes | yes |
| `snapshot_reads` | yes | yes |
| `readonly_transactions` | yes | yes |
| `arrays` | yes | no |
| `fulltext_search` | yes | no |
| `json_path` | yes | yes |
| `json_contains` | yes | no |

### Forms that need a feature

`Array`, `fulltext_index` and `matches`, `json_path`, `json_contains` and
`transaction_lock` each need the feature of the same name (`arrays`,
`fulltext_search`, `json_path`, `json_contains`, `transaction_locks`). A
renderer writes them only when it declares the feature, through optional
members of its contract (`array_type`, `fulltext_index_options`,
`fulltext_document`, `fulltext_match`, `json_path`, `json_contains`). A
renderer that declares one of these features without its members is refused
when it loads ([SANDLOOM-B025](../reference/error-codes.md#sandloom-b025)).

The requirement belongs to the module and is checked three times:

- `sandloom check` finds a form whose feature the module does not declare
  in `CapabilityRequirement("database", features=...)`
  ([SANDLOOM-C030](../reference/error-codes.md#sandloom-c030));
- when the module loads, the kernel reads the forms its tables are built
  with from the models (`Array` columns, `fulltext_index` indexes). It
  compares them, and the declared features, with its database instance's
  features. A missing one stops startup and names the module, the form, the
  feature and the instance
  ([SANDLOOM-B033](../reference/error-codes.md#sandloom-b033));
- a statement or DDL compiled for a database without the feature refuses
  ([SANDLOOM-R015](../reference/error-codes.md#sandloom-r015)).

The renderer also says how a unit begins: its `begin_statement` text, and
`transaction_options`, the SQLAlchemy execution options for where SQLAlchemy
begins the transaction itself. The kernel asks once per engine. Without
`snapshot_reads`, a `SnapshotReads` scope is refused
([SANDLOOM-B027](../reference/error-codes.md#sandloom-b027)). Without
`readonly_transactions`, a read-only unit relies on the read-only scope and
the ORM read guard ([Read units](../modules/database/units.md#read-units-read-only-and-one-snapshot)). The first
connection refuses a server older than the renderer supports (PostgreSQL 14,
SQLite 3.38, whose JSON functions are built in) with
[SANDLOOM-B030](../reference/error-codes.md#sandloom-b030).

Migrations write the database's own type (`postgresql.CITEXT()`,
`postgresql.ARRAY(sa.String(length=40))`), since each
one is the DDL history of one database. The test suite builds every module's
and example's schema on SQLite, which catches a column type or index a second
database cannot write.

Row security comes from the renderer too (`row_security()`). It writes the
tenant policy that `enable_tenant_rls` creates and binds the tenant in each
transaction. The startup role checks are its SQL as well. SQLite has none, so `tenant_rls` there
stops startup ([SANDLOOM-B026](../reference/error-codes.md#sandloom-b026)) and
its tenant isolation is application-enforced
([Multi-tenancy](../modules/database/multi-tenancy.md#database-enforced-and-application-enforced-isolation)).

## SQLite, for development and tests

Install the `sqlite` extra (`pip install 'sandloom[sqlite]'`) and point a
database at a file: `sqlite+aiosqlite:///var/app.db`. The aiosqlite adapter
sets the database up the way the renderer's features promise:

- **Implicit transactions are off.** The driver emits each unit's `BEGIN`
  itself, so savepoints (`begin_nested()`) work.
- **Writers are serial.** Every unit not declared read-only begins
  `BEGIN IMMEDIATE` and holds the database's one write lock from its first
  statement, reads before its first write included. A second writer waits for
  it. That is why `transaction_lock`, `FOR UPDATE` and the claims hold
  without SQL of their own.
- **A read-only unit** begins deferred and turns `PRAGMA query_only` on until
  it ends: the database refuses its writes, and under WAL it reads one
  snapshot.
- **Each connection opens with** WAL, `foreign_keys` and `busy_timeout`. A
  waiting writer gives up with a lock error after `DB_STATEMENT_TIMEOUT_MS`:
  30 seconds by default, 5 seconds when it is set to 0.

A concurrent duplicate of an HTTP request with an `Idempotency-Key` waits at
`BEGIN IMMEDIATE` until the first one commits, then replays its answer. It
never answers busy as on PostgreSQL; past `busy_timeout` it gets the lock
error.

One task holds the write lock through one unit at a time. A local contract
call, a saga step or a child `open_uow()` opens a unit of its own on another
connection. If it writes while the caller's unit holds the lock, it would wait
for itself until `busy_timeout`. The driver refuses it at its `BEGIN`
instead ([SANDLOOM-R016](../reference/error-codes.md#sandloom-r016)). Do the
nested write in the caller's unit or after it commits.

An in-memory database such as `sqlite://` has no WAL, and every connection
sees another one. Startup refuses it with
[SANDLOOM-B032](../reference/error-codes.md#sandloom-b032), and so does a
missing aiosqlite. The driver also refuses a SQLite URL without `+aiosqlite`
when it builds the engine. Only a test engine on one shared connection may be
in memory. It advertises neither snapshot reads nor transaction locks, so its
`transaction_lock` refuses.

SQLite keeps no time zone offset. A time column is `UtcDateTime()`, which
reads back in UTC on every database and refuses a naive time
([SANDLOOM-C029](../reference/error-codes.md#sandloom-c029)).

### The schema comes from the models

The migration chains are PostgreSQL's DDL history: row-security policies,
`clock_timestamp()` defaults, trigram indexes. A SQLite file runs none of
them. Its renderer says `schema_from_models`, so the app builds the schema at
startup with `metadata.create_all` over every mapped table, the kernel's
included. Every SQLite instance gets every table.
`sandloom migrate` refuses such a database with
[SANDLOOM-B028](../reference/error-codes.md#sandloom-b028); only
`sandloom migrate databases`, which lists, still runs. Write and apply a
module's migrations against PostgreSQL.

`create_all` creates a missing table and never alters an existing one. After a
model change the file's tables lag the models. Startup compares the columns,
their nullability, the indexes and the unique constraints. On any difference
it stops with SANDLOOM-B028 and names each one. A worker runs the same check
before it forks.

To go on, move the file away or start the app with `sandloom dev run`. That
command sets `DB_RECREATE_ON_MODEL_CHANGE=true` for the app it serves. The app
then renames the file and its WAL to `<file>.stale-<UTC time>` and builds a
fresh file. Its warning names the old one.

No table is dropped, and the stale file still opens. `ENVIRONMENT` alone never
sets a file aside. Neither does a worker. Production ignores the setting.

### Not in production

SQLite is one file on one host: no replication, no row security, and no
migration path. Its renderer says `development_only`. With `ENVIRONMENT=prod`
startup stops with
[SANDLOOM-B029](../reference/error-codes.md#sandloom-b029), unless the
deployment accepts the risk
(`SANDLOOM_ACCEPTED_RISKS=["development_database_in_prod"]`). The strict
production profile refuses it even then.

SQLite has no roles, so there is no system channel to log in as.
`DATABASE_SYSTEM_URL` on a SQLite database stops startup with
[SANDLOOM-B026](../reference/error-codes.md#sandloom-b026), as the instance
options of row security do. Tenant isolation there is the ORM guard alone
(application-enforced). Startup says so when a `TenantScoped` table is mapped.

## The DBAPI adapter

The renderer knows the SQL of a database. The Python client library under
SQLAlchemy, its DBAPI, needs knowledge of its own. It reports a lost connection
in its own way, takes the server timeouts as its own connection arguments, and
has private state the fast paths drive. A **DBAPI adapter** holds it. The
`sqlalchemy` driver chooses one by the URL's DBAPI (`asyncpg` in
`postgresql+asyncpg://`) and registers the asyncpg adapter before it builds an
engine.

| Capability | What it gives | asyncpg |
| --- | --- | --- |
| `merged_begin` | the transaction's `BEGIN` and the tenant binding in one round trip; the `BEGIN` text is the renderer's `begin_statement` | yes |
| `raw_executor` | the planner's optimized executor, raw statements on the driver connection | yes |
| `read_only_policy` | `ReadOnlyDbSession` refuses writes in the client, before sending them | yes |
| `connection_recovery` | a dead connection answers 503, and a first message never sent is sent again on a new connection | yes |
| `reconnect_backoff` | connection attempts after an outage are admitted one at a time | yes |

Any other DBAPI, such as `psycopg`, takes the plain path. It is
slower and never wrong. The aiosqlite adapter claims no capability: it adds
the engine setup of [SQLite](#sqlite-for-development-and-tests) to the plain
path. The pool's idle probe and its check for a connection already closed are
the adapter's too (`ping`, `connection_closed`). Each fast path checks the private state it drives once
per process. On a mismatch it logs one warning and falls back.

`DB_READ_ONLY_SESSIONS=true` needs a guard on the default database: the
renderer's `readonly_transactions` or the adapter's `read_only_policy`. With
neither, startup stops with
[SANDLOOM-B031](../reference/error-codes.md#sandloom-b031).
[Write a driver](write-a-driver.md#add-a-dbapi-adapter) shows how a driver
package adds an adapter.

## What still needs PostgreSQL

The kernel's units and stores run on SQLite, from the outbox to the
projection watermarks. `CreatedAt` / `UpdatedAt` are stamped by each
database's clock (`clock_timestamp()` on PostgreSQL, UTC milliseconds on
SQLite). The platform's modules start on a SQLite file and serve requests.
Gaps remain. A nested write unit refuses
([SANDLOOM-R016](../reference/error-codes.md#sandloom-r016)). The test harness
and `sandloom new` do not yet run on SQLite. Identity's claims query uses
`array_agg` and `LATERAL`. Until those close, run applications on PostgreSQL.
