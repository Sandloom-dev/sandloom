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

PostgreSQL through asyncpg, and the only relational backend. Set `DATABASE_URL`. The URL must
name its driver, `postgresql+asyncpg://`; never rely on a bare `postgresql://`.

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
| `transaction_lock` | `pg_advisory_xact_lock` | refuses: SQLite has no such lock |

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
  `sql_dialects=` of the driver's registration; `sqlalchemy` serves PostgreSQL);
- that dialect has a renderer;
- every `partial_index` has a condition there, so none would index every row.
  The migration environment checks the same before it writes or applies a
  revision.

A database instance advertises the driver's base features plus the ones its
renderer declares: on PostgreSQL `skip_locked`, `transaction_locks`,
`snapshot_reads` and `readonly_transactions`; on SQLite none yet.

The renderer also says how a unit begins: its `begin_statement` text, and
`transaction_options`, the SQLAlchemy execution options for where SQLAlchemy
begins the transaction itself. The kernel asks once per engine. Without
`snapshot_reads`, a `SnapshotReads` scope is refused
([SANDLOOM-B027](../reference/error-codes.md#sandloom-b027)). Without
`readonly_transactions`, a read-only unit relies on the read-only scope and
the ORM read guard ([Read units](../modules/database/units.md#read-units-read-only-and-one-snapshot)). The first
connection refuses a server older than the renderer supports (PostgreSQL 14,
SQLite 3.35) with
[SANDLOOM-B030](../reference/error-codes.md#sandloom-b030).

Migrations write the database's own type (`postgresql.CITEXT()`), since each
one is the DDL history of one database. The test suite builds every module's
and example's schema on SQLite, which catches a column type or index a second
database cannot write.

Row security comes from the renderer too (`row_security()`). It writes the
tenant policy that `enable_tenant_rls` creates and binds the tenant in each
transaction. The startup role checks are its SQL as well. SQLite has none, so `tenant_rls` there
stops startup ([SANDLOOM-B026](../reference/error-codes.md#sandloom-b026)) and
its tenant isolation is application-enforced
([Multi-tenancy](../modules/database/multi-tenancy.md#database-enforced-and-application-enforced-isolation)).

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

Any other DBAPI, such as `aiosqlite` or `psycopg`, takes the plain path. It is
slower and never wrong. Each fast path checks the private state it drives once
per process. On a mismatch it logs one warning and falls back.

`DB_READ_ONLY_SESSIONS=true` needs a guard on the default database: the
renderer's `readonly_transactions` or the adapter's `read_only_policy`. With
neither, startup stops with
[SANDLOOM-B031](../reference/error-codes.md#sandloom-b031).
[Write a driver](write-a-driver.md#add-a-dbapi-adapter) shows how a driver
package adds an adapter.

## What still needs PostgreSQL

The kernel itself still needs PostgreSQL: the outbox and
idempotency stores, and the projection upserts. `CreatedAt` / `UpdatedAt`
are stamped by each database's clock (`clock_timestamp()` on PostgreSQL, UTC
milliseconds on SQLite). Identity's claims query uses `array_agg` and `LATERAL`. Until those
move behind the driver too, run Sandloom on PostgreSQL.
