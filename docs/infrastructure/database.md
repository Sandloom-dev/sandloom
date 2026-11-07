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
It calls only the SQL functions every database answers alike, and marks its raw SQL
([SANDLOOM-C031](../reference/error-codes.md#sandloom-c031)).
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
| `aggregate_list` | `array_agg` | `json_group_array`, read back as the same list |
| `Array(item_type)` | `item_type[]` | refused (no `arrays`) |
| `fulltext_index`, `matches` | GIN index over `to_tsvector` with the text search configuration named like `language`, `@@ websearch_to_tsquery` | refused (no `fulltext_search`) |
| `json_path` | `#>>`, an index step only into an array | `json_extract`, booleans as `'true'` / `'false'`, numbers through `->` (the JSON text as written); the same text as PostgreSQL for every scalar |
| `json_contains` | `jsonb @>` | refused (no `json_contains`) |

`conflict_insert` names the key a row conflicts on and says to skip the row or update some columns. A database without `ON CONFLICT` gets a renderer that writes `MERGE` from the same calls. The full-text `language` is the text's language by its English name, such as `english` or `russian`; `simple` turns stemming off. Each renderer maps the name to its own configuration.

The kernel's own locks come from the same renderer. Its `lock_statement` gives
the HTTP idempotency try-lock, the projection purge lock and the migration
locks, which are advisory locks on PostgreSQL. `transaction_lock` is built on it
too. It returns a `LockStatement`: one statement per lock and key shape, with
the key in its parameters, so a lock taken per request builds no SQL.

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
| `concurrent_writers` (derived: not `serial_writers`) | yes | no |
| `skip_locked` | yes | no |
| `transaction_locks` | yes | yes |
| `snapshot_reads` | yes | yes |
| `readonly_transactions` | yes | yes |
| `arrays` | yes | no |
| `fulltext_search` | yes | no |
| `json_path` | yes | yes |
| `json_contains` | yes | no |
| `list_aggregates` | yes | yes |

### Forms that need a feature

`Array`, `fulltext_index` and `matches`, `json_path`, `json_contains`,
`transaction_lock` and `aggregate_list` each need their feature (`arrays`,
`fulltext_search`, `json_path`, `json_contains`, `transaction_locks`,
`list_aggregates`). A renderer writes them only when it declares the
feature, through optional members of its contract (`array_type`,
`fulltext_index_options`, `fulltext_document`, `fulltext_match`,
`json_path`, `json_contains`, `aggregate_list`, `aggregate_list_type`). A
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

### Nested units commit with their caller

Some code opens a unit inside another unit: a local contract call, a saga
step, a child `open_uow()`. On PostgreSQL the inner unit takes another
connection and commits on its own, before its caller. On SQLite that second
writer would wait for its own caller's write lock until `busy_timeout`.

The renderer says `serial_writers`. So the Unit of Work runs the inner unit on
the caller's connection, inside a `SAVEPOINT` (G-UOW-09). The caller is a unit
of the same task and database instance. It has already run a statement, so
it holds the write lock:

- **The inner unit's commit releases its savepoint.** It is not durable on its
  own: the caller's `COMMIT` commits it. The caller's rollback undoes its
  rows and outbox events, with its command-log record. Rolling back a
  savepoint around the call undoes it too, if the caller ran a statement
  before the call. The caller takes the write lock at its first statement (a
  test's `db` session at its first write). Opened before any statement,
  `begin_nested()` takes no lock. The call then commits on its own, as on
  PostgreSQL, and rolling that savepoint back does not undo it.
- **Its failure rolls back to the savepoint.** The caller goes on, as after
  an independent unit that failed.
- **Its post-commit stages wait for the caller's `COMMIT`.** The outbox
  dispatch, its `on_commit` callbacks and a command's amended record run
  after the caller commits, and never when it rolls back.
- **A retry runs the command again.** The command-log record went with the
  caller, and so did the command's effect. The record still commits exactly
  when the effect commits (G-CMD-01). For a saga step, the step's state joins
  them in that one transaction. A crash loses them together or keeps them
  together. The saga redrives a lost step with the same key and runs it once
  (G-SAGA-07).

This weakens contract atomicity (ADR-0005) on SQLite only. The guarantee
lattice labels it "commits with the caller". Startup logs one warning per
such database.

Some units keep their own transaction: one on another database instance, a
read-only one (a read contract's included), and a remote (RPC) provider's. A caller that has not written
yet holds no lock, even inside an early `begin_nested()`. The inner unit then
commits on its own, as on PostgreSQL.

The unit a request's local read contracts share is read-only on SQLite. It
reads without the write lock, and the request's own write units never wait
behind it.

Units of other tasks nest one task at a time: a second task waits until the
first one's nested units ended. A caller whose transaction failed, waiting
for its rollback, cannot host a unit: the inner unit raises
`PendingRollbackError`, as the caller's next statement would.

The driver still refuses at once what it cannot nest
([SANDLOOM-R016](../reference/error-codes.md#sandloom-r016)). Its task then
holds the lock outside its open units. That may be a bare session, a
connection outside a unit, or a second engine on the same file.

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
[SANDLOOM-B028](../reference/error-codes.md#sandloom-b028). Two sub-commands
still run: `sandloom migrate databases`, which lists, and `sandloom migrate
drift`, which says it skips. Write and apply a module's migrations against
PostgreSQL.

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

After a recreate, restart every other process on the file, such as a worker.
A process that held the old file open would go on writing to it under its new
name. So each process remembers the file it opened. Once the path names
another file, or none, its next connection fails with `DatabaseFileReplaced`
(SANDLOOM-B028), which says to restart it.

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

### Module tests on SQLite

Point the test harness at a file whose name contains `test`:

```bash
export SANDLOOM_TEST_DATABASE_URL=sqlite+aiosqlite:///$PWD/tmp/app_test.db
uv run pytest -n 4 --dist loadfile src/my_module/tests
```

- **Each xdist worker gets its own file** beside it, `app_test_gw<N>.db`.
- **The file carries its owner** in `app_test.db.owner`, so another run is
  refused before anything is reset.
- **The schema comes from the models**, built once per process and emptied
  between tests.
- **The harness's own sessions begin at their first write.** That covers the
  `db` fixture and the sessions a test opens on `db_engine`. Until then each
  read sees the last commit and holds no lock, as in a PostgreSQL test
  transaction. The first write takes the database's write lock. A `FOR
  UPDATE` read, a savepoint and `transaction_lock` take it too. The
  application's units then nest in the `db` session, as in production. That
  includes the `saga_harness`'s units in a test that also uses `client`. The
  outbox events of a nested unit are rows of the `db` transaction, which
  never commits. So they are neither published nor marked delivered, like
  the events of the `client` fixture's requests. Its `on_commit` callbacks
  run after the next successful request.
- **Migration tests and checks skip.** The `scratch_database` fixture skips:
  the chains are PostgreSQL's. So do the chain checks: the chain coverage of
  `sandloom test`'s "schema & deploy" step (SANDLOOM-T002) and the
  migrations-match-models tests (SANDLOOM-T006). Each says why.
- **A test needing a feature SQLite lacks** is marked
  `@skip_unless_db_feature("arrays")`
  ([testing reference](../reference/testing.md#tests-that-need-a-database-feature)).
  A race of two writers holding row locks is marked
  `@skip_unless_db_feature("concurrent_writers")`. SQLite has one writer, and the
  harness does not emulate PostgreSQL's concurrency.

### An application on SQLite

An application from `sandloom new` runs on a file. Its `uv sync` installs the
driver: the `sqlite` extra is in its `dev` dependencies. Set the database in
`.env`, outside the `sandloom dev` block. Redis stays: SQLite replaces
PostgreSQL only.

```bash
DATABASE_URL=sqlite+aiosqlite:///./data/app.db
CACHE_URL=redis://127.0.0.1:6379/0
BROKER_URL=redis://127.0.0.1:6379/1
```

`uv run sandloom dev run` reads that `DATABASE_URL` as the checkout's own
database. It starts no dev services and serves the app on the file. `sandloom
dev up` refuses and leaves `.env` as it is. The line must be the last
`DATABASE_URL` of the file, outside the block; a PostgreSQL URL there is
still `dev up`'s to manage. The
schema is built at startup. `sandloom migrate upgrade-all` refuses
(SANDLOOM-B028). To run the app's tests and gates on SQLite, set
`SANDLOOM_TEST_DATABASE_URL=sqlite+aiosqlite:///./data/app_test.db` for
`uv run sandloom test`. The migration checks skip there, each with its
reason. They are the chain coverage of the "schema & deploy" step
(SANDLOOM-T002), the migrations-match-models tests and the "migration drift"
step (SANDLOOM-T006).
A model or a column added on SQLite therefore passes without a revision.
Before production, write the revision against PostgreSQL
(`sandloom migrate new <module> "<message>"`), where the checks run.
`sandloom migrate upgrade-all` and `upgrade-core` refuse a chain that does
not create a model's table or index, before they apply anything
(SANDLOOM-T002). Production refuses SQLite (SANDLOOM-B029). The generated
README says the same.

### The CI lanes

PostgreSQL is the mandatory lane: `backend-tests` runs every test on
`postgres:16`. SQLite is the second lane, the job `second-db-test-lane`:

- the standard modules' suites on SQLite files;
- every example's tests on a file of its own;
- an application from `sandloom new` with the standard modules
  (`tools/smoke/quickstart_check.py --database sqlite --profile standard`).
  It is served on the README's `.env` lines without dev services. A user
  signs up through its API and verifies the email. After a login and a token
  refresh, the user creates an organization and finds it listed. Then the
  app's own `sandloom test` runs.

Each lane writes the tests it ran and skipped (`examples` writes the
examples' PostgreSQL ledgers)
([skip ledger](../reference/testing.md#the-skip-ledger)). The `skip-ledger`
job fails when a test was skipped on every lane. A test may skip on SQLite
for a missing feature; then it runs on PostgreSQL.

### Text order

Text comparisons and text order use the database's collation: `ORDER BY`,
`<`, `>`, `min`, `max`, `DISTINCT`. SQLite's is byte order. PostgreSQL's is
usually linguistic (glibc or ICU): `'a' < 'B'`, and punctuation is ignored at
the first level. A PostgreSQL built on musl libc sorts bytewise. The local
test services run such an Alpine image, so a local run cannot see the
difference. The CI lanes' `postgres:16` (Debian, glibc) can
(`core/tests/test_text_collation.py`). When byte order matters, write
`column.collate("C")` on PostgreSQL or sort in Python.

SQLite's `LIKE` folds ASCII case. The read planner's case-sensitive prefix
filter (`prefix=` without `prefix_ignore_case`) is therefore a `GLOB` there,
which keeps case; PostgreSQL's is `LIKE`. Both read the prefix literally.

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
Nested write units commit with their caller, and module test suites run on a
SQLite file. An application from `sandloom new` with the standard modules
runs on a file: sign-up, login, token refresh and organizations included
([An application on SQLite](#an-application-on-sqlite)). PostgreSQL is still
needed for:

- **Production.** SQLite is refused there
  ([Not in production](#not-in-production)).
- **Migrations.** The chains are written and applied against PostgreSQL. A
  model changed on SQLite needs its revision written there before a deploy.
  `sandloom migrate upgrade-all` refuses a chain that does not create a
  model's table or index
  ([SANDLOOM-T002](../reference/error-codes.md#sandloom-t002)).
- **Row security.** Tenant isolation on SQLite is the ORM guard alone.
- **Concurrent writers.** One unit writes at a time. Tests of two writers
  holding row locks skip there (`concurrent_writers`, `skip_locked`).
- **The forms SQLite lacks:** `arrays`, `fulltext_search` and
  `json_contains`. A module that declares one does not start on SQLite
  ([SANDLOOM-B033](../reference/error-codes.md#sandloom-b033)).
