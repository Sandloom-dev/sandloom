# Research: reliability and isolation mechanisms across databases

Status: **research, 2026-11-04**, primary sources cited inline. **[inference]** marks reasoning or general knowledge not confirmed against a fetched source. Not covered: Wolverine/Marten, Django's migration locking, SQL Server's clock functions. Input to [design.md](design.md).

## 1. Outbox, inbox and idempotency

- **NServiceBus SQL persistence** (SQL Server, Oracle, MySQL, PostgreSQL).
  - Deduplication rests on a unique index on the outbox table, not on an upsert. In the default optimistic mode, "one of the transactions succeeds and the other fails due to a unique index constraint violation".
  - The pessimistic mode inserts the outbox row before the handlers run.
  - Sagas are created through a unique correlation index; updates use "SELECT ... FOR UPDATE or its dialect-specific equivalent".
  - Sources: [outbox](https://docs.particular.net/persistence/sql/outbox), [saga concurrency](https://docs.particular.net/persistence/sql/saga-concurrency).
- **MassTransit EF Core.**
  - `InboxState` keeps a `DuplicateDetectionWindow`, a time-bounded guarantee.
  - Pessimistic locks come from a per-provider `LockStatementProvider`.
  - The default isolation level is Serializable.
  - Source: [outbox](https://masstransit.massient.com/documentation/configuration/middleware/outbox).
- **Temporal SQL persistence.** Every plugin implements `IsDupEntryError(err) bool`; MySQL checks error 1062 ([interfaces.go](https://github.com/temporalio/temporal/blob/main/common/persistence/sql/sqlplugin/interfaces.go), [mysql db.go](https://github.com/temporalio/temporal/blob/main/common/persistence/sql/sqlplugin/mysql/db.go)). Duplicate-key classification is a driver capability.
- **Axon.**
  - The token store has owner-plus-timestamp claims, with `claimTimeout` 10 s.
  - A stolen claim can double-handle, so tokens belong in the same database as the projections.
  - Dialect table factories: Generic, Postgres, Oracle.
  - Source: [streaming processors](https://docs.axoniq.io/axon-framework-reference/4.10/events/event-processors/streaming/).
- **Eventuate Tram.**
  - A `RECEIVED_MESSAGES` table dedupes inside a managed transaction.
  - Relaying is log tailing on MySQL/PostgreSQL and polling elsewhere.
  - Source: [manual](https://eventuate.io/docs/manual/eventuate-tram/latest/getting-started-eventuate-tram.html).
- **Debezium outbox.** An append-only table; consumers dedupe by event id ([router](https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html)).

**The portable primitive** is a unique key plus a plain INSERT, with the duplicate-key error classified per driver.
- After an error, PostgreSQL leaves the transaction aborted until `ROLLBACK TO SAVEPOINT` ([transactions](https://www.postgresql.org/docs/current/tutorial-transactions.html)). The claim therefore needs a savepoint, or `ON CONFLICT DO NOTHING` on PostgreSQL and SQLite 3.24+ ([sqlite upsert](https://www.sqlite.org/lang_upsert.html)).
- MySQL's `INSERT IGNORE` also downgrades other errors ([mysql](https://dev.mysql.com/doc/refman/8.0/en/insert-on-duplicate.html)).
- SQLAlchemy does not normalise driver codes: "there is no guarantee that different DB-API implementations will raise the same exception type" ([exceptions](https://docs.sqlalchemy.org/en/20/core/exceptions.html)). **[inference]** Typical codes:
  - PostgreSQL: 23505, 40001/40P01;
  - MySQL: 1062, 1213/1205;
  - SQL Server: 2627/2601, 1205;
  - SQLite: `SQLITE_CONSTRAINT_UNIQUE`/`SQLITE_BUSY`.

**Relay.**
- MySQL 8 has `SKIP LOCKED`, but it is "not suitable for general transactional work" and unsafe for statement-based replication ([locking reads](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html)).
- Django raises `NotSupportedError` when it is missing, and is a no-op on SQLite.
- Alternatives: lease columns, a single relay holding a named lock, CDC.

| design | exactly-once effect | requires |
|---|---|---|
| INSERT + catch unique violation | yes, in the effect's transaction | unique index, dup-key classification, a savepoint (PostgreSQL) |
| ON CONFLICT DO NOTHING / INSERT IGNORE | yes | dialect support |
| time-windowed dedupe | only within the window | cleanup |
| lease / claim | at-least-once under a stolen claim | conditional UPDATE, an idempotent consumer |

## 2. Tenant isolation without PostgreSQL RLS

- **Hibernate.** Database, schema or discriminator multitenancy; `@TenantId` is filled automatically, and the partitioned approach must "alter each and every SQL statement" ([multitenancy](https://raw.githubusercontent.com/hibernate/hibernate-orm/6.6/documentation/src/main/asciidoc/userguide/chapters/multitenancy/MultiTenancy.adoc)).
- **acts_as_tenant.** `default_scope` plus `require_tenant`. It lists its own bypasses ([README](https://github.com/ErwinM/acts_as_tenant)):
  - `unscoped`;
  - raw SQL;
  - `update_columns`;
  - `upsert_all`, which "matches rows by primary key across all tenants".

  This is the threat model of an ORM-only filter.
- **django-tenants.** Schema per tenant via `search_path`, PostgreSQL only ([docs](https://django-tenants.readthedocs.io/en/latest/)).
- **Citus.** Primary and foreign keys "include the distribution column" ([multi-tenant](https://docs.citusdata.com/en/stable/use_cases/multi_tenant.html)). **[inference]** Composite `(tenant_id, id)` keys also block cross-tenant upserts by key.
- **SQL Server RLS.** Filter and block predicates, with `SESSION_CONTEXT` and `@read_only=1` per pooled connection ([RLS](https://learn.microsoft.com/en-us/sql/relational-databases/security/row-level-security)).
- **PostgreSQL RLS caveats** ([ddl-rowsecurity](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)):
  - superusers and BYPASSRLS roles bypass it;
  - owners bypass it unless FORCE is set;
  - with no policy, access is default-deny;
  - unique and foreign-key checks bypass it, which is a covert channel.
- **Pattern.** The ORM filter is the portable baseline, and database RLS is optional defense in depth.

## 3. Locks and migration serialisation

| database | named lock | scope |
|---|---|---|
| PostgreSQL | `pg_advisory_lock` / `pg_advisory_xact_lock` | session or transaction ([explicit locking](https://www.postgresql.org/docs/current/explicit-locking.html)) |
| MySQL | `GET_LOCK(name, timeout)` | session only; not released at commit; unsafe for statement-based replication ([locking functions](https://dev.mysql.com/doc/refman/8.0/en/locking-functions.html)) |
| SQL Server | `sp_getapplock` | transaction (default) or session ([sp_getapplock](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/sp-getapplock-transact-sql)) |
| SQLite | none; one writer, `BEGIN IMMEDIATE` | database ([transactions](https://www.sqlite.org/lang_transaction.html)) |

Migration tools:
- Flyway uses the native lock per database: [PostgreSQL](https://raw.githubusercontent.com/flyway/flyway/main/flyway-database/flyway-database-postgresql/src/main/java/org/flywaydb/database/postgresql/PostgreSQLAdvisoryLockTemplate.java), [MySQL](https://raw.githubusercontent.com/flyway/flyway/main/flyway-database/flyway-mysql/src/main/java/org/flywaydb/database/mysql/MySQLNamedLockTemplate.java), [SQL Server](https://raw.githubusercontent.com/flyway/flyway/main/flyway-database/flyway-sqlserver/src/main/java/org/flywaydb/database/sqlserver/SQLServerApplicationLockTemplate.java).
- Rails takes an advisory lock when `advisory_locks_enabled?` and reloads the applied versions after acquiring it ([migration.rb](https://raw.githubusercontent.com/rails/rails/main/activerecord/lib/active_record/migration.rb)).
- Liquibase uses a lock table that can be left locked after a crash ([lock table](https://docs.liquibase.com/concepts/tracking-tables/databasechangeloglock-table.html)).
- Alembic has no locking.

Lesson: use native named locks; a lock table needs a lease expiry.

## 4. Timestamps and ordering

- PostgreSQL: `now()` returns the transaction start, `statement_timestamp()` the statement start, and `clock_timestamp()` "changes even within a single SQL statement" ([datetime](https://www.postgresql.org/docs/current/functions-datetime.html)).
- MySQL: `NOW()` returns the statement start; `SYSDATE()` the execution time, but it is unsafe for replication ([date functions](https://dev.mysql.com/doc/refman/8.0/en/date-and-time-functions.html)).
- **[inference]** Wall-clock time is not commit order on any database: a row stamped earlier can commit later.
- UUIDv7: a 48-bit ms timestamp, monotonic per generator (RFC 9562 §6.2), approximate order across nodes ([RFC 9562](https://www.rfc-editor.org/rfc/rfc9562.html)).

## 5. Version-guarded upserts

| database | form | guard |
|---|---|---|
| PostgreSQL | `ON CONFLICT DO UPDATE ... WHERE` | atomic under concurrency ([insert](https://www.postgresql.org/docs/current/sql-insert.html)) |
| SQLite 3.24+ | same | `WHERE excluded.v > t.v` |
| MySQL | `ON DUPLICATE KEY UPDATE` | no WHERE; `IF()` per column, affected rows 0/1/2 |
| SQL Server / PostgreSQL 15+ | `MERGE` | needs HOLDLOCK on SQL Server ([merge](https://learn.microsoft.com/en-us/sql/t-sql/statements/merge-transact-sql)); PostgreSQL points to ON CONFLICT ([merge](https://www.postgresql.org/docs/current/sql-merge.html)) |

**[inference]** Portable emulation: guarded UPDATE; if no rows changed, INSERT; on a unique violation, retry the UPDATE once (with a savepoint on PostgreSQL). Django's `get_or_create` relies on the same uniqueness.

## Recommendations (from the research)

**Driver capabilities:**
- `classify_error` (unique violation, serialization failure, deadlock, lock timeout), required;
- `insert_or_skip`, required, with a savepoint fallback;
- `guarded_upsert`, required, with the emulation fallback;
- `named_lock(scope=transaction|session)`;
- `skip_locked`;
- `row_security: none|native`;
- `clock`.

**Per mechanism:**
- **Claims:** a unique-key insert in the effect's transaction; never a time window by default.
- **Relay order:** not from a DB clock; a single lock-holding relay or a lag. The label degrades, never silently.
- **Audit:** append-only, with application timestamps.
- **Sagas:** a unique correlation key, version-column updates, no multi-process SQLite.
- **Projections:** `guarded_upsert`, valid on every database.
- **Tenancy:** an ORM filter plus composite keys as the baseline, covering the bypass list; RLS optional, and refused only when configured but missing.
- **Locks:** native named locks; a lease table as the fallback.
- **Timestamps:** application UUIDv7 for order, the DB clock for display.

**Refuse to start:**
- no error classification or savepoints;
- configured DB-enforced tenancy without native RLS;
- a transaction-scoped lock needed on a session-only driver;
- SQLite with more than one worker process;
- MERGE upsert without serializable locking.
