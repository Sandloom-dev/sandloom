---
search:
  exclude: true
title: "ADR-0011: The driver API"
---

# ADR-0011: The driver API

- **Status:** Accepted (2026-10-31, by the owner)
- **Date:** 2026-10-31
- **Amends:** [ADR-0001](0001-framework-distribution.md) (how driver packages register)
  and [ADR-0010](0010-public-api-1.0.md) (a second public surface, not for modules)
- **Research:** `design/driver-api/research.md`
- **Amended:** 2026-11-05, the database capability (see the amendment at the end)

## Context

A contract has two readers. A module asks what it can call; a driver asks what it must
implement. They need different promises: a new abstract method does not touch a caller
and breaks an implementer. Sandloom showed both readers the same class and gave neither
a public home. Accessors returned `Any`, and a driver imported from `sandloom.core`.

Discovery had its own faults. Every installed package ran `register(registry)` at
startup in an order Python does not define, so a driver could lose to the package that
declares its capability. A failure was only logged, and a package could silently
replace a built-in driver, `sqlalchemy` included. Features were declared twice, so a
requirement could pass at startup and fail on first use.

## Decision

1. **Two doors, one class.** Modules get a Protocol of operations from
   `sandloom.sdk.infra` (`Cache`, `Notifier`, `ObjectStore`, `TaskQueue`), and accessors
   return it. Driver packages get the provider ABC, which subclasses that Protocol and
   adds the lifecycle, from `sandloom.driverkit`. A module may not import `driverkit`
   (C004) and the kernel never does (`lint-imports`).
2. **Find drivers by name, lazily.** An entry point
   `"<capability>.<driver>" = "pkg.driver:Class"` in `sandloom.drivers`, and
   `"<capability>" = "pkg.contracts:SPEC"` in `sandloom.capabilities`. Only what `INFRA`
   selects is imported. There is no `register` callable, so there is no order.
3. **Conflicts and failures stop startup.** A selected name with two providers, or a
   package claiming a built-in's name, is SANDLOOM-B021. A selected driver that cannot
   be used is SANDLOOM-B022. There is no override: another behaviour is another name.
4. **Declare once, on the class.** `supported_features`, or `features_for(options)` when
   a feature depends on an option. An instance that reports otherwise is refused. A
   required method is not a feature, and a test holds every spec to that.
5. **Required contract version.** `contract_version` on the driver class; a missing one,
   or another major version, is SANDLOOM-B022. Signatures are pinned per version by a
   test, and `sandloom.driverkit` is in the API baseline (T008).
6. **A contract is public only with a kit.** `NotifierProvider` has
   `NotifierConformance`, a pytest class with one test per obligation. A test fails a
   contract with no kit. The `cache`, `objectstore` and `taskqueue` contracts stay
   private until theirs exist. The kit is itself tested against broken drivers.
7. **`CapabilityRef[P]`, not `type[T]`.** `capability(REF, name) -> P`. mypy rejects an
   abstract class or a Protocol passed as `type[T]`; the key mirrors `ContractRef[P]`.
8. **Experimental means refused in production.** Configuring an experimental capability
   there needs the accepted risk `experimental_capability` (SANDLOOM-B023, ADR-0006). A
   documentation label alone does not hold: PEP 411 records that.
9. **A module cannot import a driver.** C004 reads installed `sandloom.drivers` entry
   points and refuses them and their parents, so a package keeps its reference and Protocol in a
   separate `contracts` module. `redis` and `celery` join the banned backend SDKs.

## Consequences

- Removed: `register(registry)`, `InfraRegistry` from the driver API, and the `s3`, `gcs`
  and `yc` placeholders. Selecting those names is the ordinary "no driver" error, naming
  what to install. Nothing is kept for compatibility: Sandloom is unreleased.
- `graph_store`, `workflow`, `search`, `lock` and `secrets` remain defined and not
  public. Each becomes public with its first driver and kit, as planned for a Temporal
  `workflow` driver.
- Rejected, with reasons in the research: eager loading in sorted order, an override
  flag, wrappers over providers, `TypeForm`, a `sandloom.experimental` namespace, and an
  experimental comment marker in modules, which a static check cannot resolve for a
  reference imported from another package.

## Amendment (2026-11-05): one SQL driver for several databases

- **Decided by:** the owner, 2026-11-04 (the spec's base decisions) and 2026-11-05
  (nesting, `func.now()`, the schema of SQLite, the CI lanes, the saga marker)
- **Spec:** `design/infra-agnostic/spec.md`, each slice with its "as built" section
- **Research:** `design/infra-agnostic/research-nested-writes-sqlite.md`,
  `design/infra-agnostic/research-sqlite-risks.md`, and the inputs the spec lists

### Context of the amendment

The `database` capability had one driver, `sqlalchemy`, and it served only
PostgreSQL. The kernel's stores wrote PostgreSQL SQL: `ON CONFLICT`, advisory
locks, `SKIP LOCKED`, row security, `clock_timestamp()`. Modules imported
`sqlalchemy.dialects.postgresql`.

The owner decided to complete the existing capability, driver and feature
design instead of adding a new one. Database drivers are equal and differ only
in what they declare. The second database is SQLite, for development and tests.
What only some databases can do comes as `sandloom.sdk.data` forms, each a
feature of the `database` capability; there is no namespace per database.

### Decisions of the amendment

1. **Renderer contract 2.** Each SQL database has one `SqlDialect`, found by
   name in the `sandloom.sql_dialects` entry-point group and exported from
   `sandloom.driverkit`. It returns SQL and facts. Connections, pools and
   sessions stay in the driver.
   - **Required:** `contract_version` (2), `name`, `minimum_server_version`
     (checked at the first connection, SANDLOOM-B030), `features()` and
     `classify_error`. Also required: the forms the kernel's guarantees need
     (`REQUIRED_MEMBERS`, such as `conflict_insert` and `wall_clock`).
   - **Optional, in whole groups:** the members of one feature
     (`FEATURE_FORM_MEMBERS`), the two prefix members, and four facts read as
     `False` when absent (`schema_from_models`, `development_only`,
     `serial_writers`, `aware_times`).
   - **Locks:** `lock_statement` and `unlock_statement` return a
     `LockStatement` (the statement, built once per key shape, and the
     parameters carrying the key), exported from `sandloom.driverkit`. A lock
     taken per request then builds no statement.
   - **Versioning:** the loader refuses another major version, a missing
     required member, an unknown feature, and a feature or group with members
     missing (SANDLOOM-B025).

   A group may be absent because its form then refuses (SANDLOOM-R015) and a
   module that needs it is refused at load (SANDLOOM-B033). The optional
   members changed neither the Protocol nor the `sandloom.driverkit` baseline.
2. **Instance features and the kit.** An instance has the driver's base
   features, the features its renderer declares from a fixed list, and
   `tenant_rls` when the option is on. The list: `skip_locked`,
   `transaction_locks`, `snapshot_reads`, `readonly_transactions`, `arrays`,
   `fulltext_search`, `json_path`, `json_contains`, `list_aggregates`.
   Features follow from the options and the URL's dialect, never from the
   server version, so the build-free preflight stays exact. Row security is not
   a feature, since no module would require it; `tenant_rls` without it stops
   startup (SANDLOOM-B026). As decision 6 requires, renderers have a kit:
   `SqlDialectConformance` in `sandloom.driverkit.testing`.
3. **DBAPI adapters.** What the driver knows about one DBAPI is a
   `DbapiAdapter` in `core/drivers/`, chosen by the URL's DBAPI. For asyncpg
   that is the merged `BEGIN`, the planner's raw executor, the client read-only
   policy, connection recovery and reconnect backoff. Any other DBAPI takes
   the plain path. Three capabilities are kernel-only (`merged_begin`,
   `raw_executor`, `connection_recovery`): they drive asyncpg's API,
   SQLAlchemy's private state or the kernel's own reconnect. A driver package's
   adapter may claim only `read_only_policy` and `reconnect_backoff`
   (SANDLOOM-B025). So `core/sql_alchemy` names no database, and a new SQL
   database needs a renderer, plus an adapter only for speed.
4. **Transaction locks are a database feature, not the `lock` capability.**
   `transaction_lock(key)` holds a lock until the unit commits or rolls back.
   It guards rows written in that same transaction. The private `lock`
   capability (`LockProvider`, a Redis or etcd lock with a TTL) lives outside
   any transaction. A call or a timeout releases it, never the commit. It
   would leave a gap between release and commit, and it would need a second
   backend for what the database already gives. So the feature is
   `transaction_locks` of `database`, written by
   `lock_statement(scope="transaction")`, run with its parameters.
   - **PostgreSQL:** an advisory lock. A string key is still hashed on the
     server, so old and new processes exclude each other during a rolling
     deploy.
   - **SQLite:** the one writer. A unit that may write already holds the
     database from `BEGIN IMMEDIATE`; a read-only unit refuses (R015).

   `lock` stays private.
5. **Serial writers: a nested unit is a SAVEPOINT of its caller.** On SQLite
   every unit that may write begins `BEGIN IMMEDIATE`, so a file has one
   writer. Some units open inside a writing unit of the same task: a local
   contract call, a saga step's unit, a child `open_uow()`. Such a unit would
   wait for its own caller. The research compared four answers. They were a
   refusal (SANDLOOM-R016), a deferred `BEGIN`, a savepoint and a file per
   module. The owner chose the savepoint. With the refusal, the standard modules would not run on SQLite,
   because they call contracts inside units. With a file per module, nesting
   inside one module would still be refused.

   The inner unit runs as a SAVEPOINT on the caller's connection when the
   renderer states `serial_writers` and the caller has already run a
   statement. It commits with the caller, and its outbox dispatch and
   `on_commit` callbacks wait for the outer COMMIT. This weakens atomicity on
   SQLite only. It carries the computed label "commits with the caller" and a
   startup warning (G-UOW-09). G-CMD-01 and G-SAGA-07 are stated for both
   databases. The policy is chosen once per engine (`core/sql_alchemy/nesting.py`),
   so the unit of work has no SQLite branch.

   A saga step whose provider commits elsewhere (over RPC, or on another
   instance) first commits its started marker in a transition of its own
   (owner decision). Joined to the transition, the marker would be lost with
   it.
6. **Database feature forms.** `sandloom.sdk.data` has `Array`,
   `fulltext_index` with `matches`, `json_path`, `json_contains` and
   `aggregate_list`. A module that uses one declares its feature in
   `CapabilityRequirement("database", features=...)`. It states what it needs,
   never which database.
   - `sandloom check` finds a missing declaration (SANDLOOM-C030).
   - Module load refuses an instance without the feature, also derived from
     the module's tables (SANDLOOM-B033).
   - A statement compiled without the feature refuses (SANDLOOM-R015).

   An undeclared `transaction_lock` is a warning for one minor release, since
   modules used it before the rule. `concurrent_writers` is derived: an
   instance has it unless its renderer states `serial_writers`, and a renderer
   that lists it is refused (B025). Race tests skip on it, and the fact and the
   feature cannot disagree.
7. **Portable SQL.** SANDLOOM-C031 checks modules and their tests. It reports a
   `func.<name>` outside the allowlist, raw SQL without
   `# sandloom: raw-sql <reason>`, and code that reads the database's name. It
   is a new code beside C028 (dialect imports): it asks another question and
   also reads tests. A name joins the allowlist only when a test runs it on
   both databases with the same value and Python type. The list: `abs`,
   `char_length`, `coalesce`, `count`, `length`, `max`, `min`, `nullif`, `sum`. A test shows
   that `lower`, `upper` and `avg` differ, so they stay out.

   `func.now()` is an error (owner decision). Its replacement is the existing
   `WallClock()`, a required renderer member: the database's clock at the
   statement, stored as the same text on both databases. It is statement time,
   because SQLite has no transaction clock.
8. **SQLite takes its schema from the models.** SQLite never runs the
   migration chains, which are PostgreSQL's DDL history (owner decision). A
   renderer stating `schema_from_models` gets `create_all` at startup. A file
   that differs from the models stops startup, and `sandloom migrate` refuses
   SQLite (both SANDLOOM-B028).

   In exchange, `assert_migrations_match_models(module)` proves on PostgreSQL
   that each chain builds the schema its models declare. It compares columns,
   types, server defaults, and the indexes as the database reflects them. So a
   chain equals its models on PostgreSQL, and the models equal the SQLite
   schema by construction. A production process on SQLite needs the accepted
   risk `development_database_in_prod`. The strict profile refuses it always
   (SANDLOOM-B029, ADR-0006).
9. **Two CI lanes and a skip ledger (owner decision).** PostgreSQL
   (`backend-tests`) is the mandatory lane. SQLite (`second-db-test-lane`) is
   the second: the standard modules, the examples, an application from
   `sandloom new`, and the kernel's unit, saga and command-log suites. The
   SQLite harness does not imitate PostgreSQL's concurrency; a race test skips
   there by feature. Each lane records the tests it ran and skipped. The
   `skip-ledger` job fails on a test that ran on no lane, unless an allowlist
   entry names it with a reason. A skip by feature never hides a test nobody
   runs.

### Consequences of the amendment

- The kernel names no database outside its drivers, the core migration chain
  and two files of the migration runner. An architecture test counts the
  sites, and the count may only shrink.
- A new SQL database is a renderer that passes the kit. It needs an adapter
  only where its DBAPI is slow on the plain path.
- On PostgreSQL the SQL, the DDL and the migrations are unchanged, pinned by
  tests. The ops tools (`migrate extract-module`, backups, the dev services)
  stay PostgreSQL-only and say so.
- Application authors meet the changes listed in
  [Upgrade to portable SQL and SQLite](../operate/upgrade-sqlite-and-portable-sql.md).
- Not done, with the reasons in the spec and the research:
  - a namespace per database;
  - a renderer that runs connections;
  - portable migration chains;
  - PostgreSQL's concurrency imitated on SQLite;
  - a file per module on SQLite.
