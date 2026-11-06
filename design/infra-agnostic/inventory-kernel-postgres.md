# Inventory: where the kernel depends on PostgreSQL

Status: **code inventory, 2026-11-04**, read only, branch `claude/modules-portable`. Kernel = `src/sandloom/core`, `src/sandloom/sdk`, `src/sandloom/shared`; business modules are already dialect-free (C028). Input to [design.md](design.md).

Categories: **(a)** SQL syntax a renderer could translate; **(b)** a capability other databases may lack or implement differently; **(c)** driver, connection, URL or error codes; **(d)** dev and test tooling. Approximate counts: (a) ~24, (b) ~22, (c) ~20, (d) ~25.

Searches that came back empty in the kernel runtime:
- LISTEN/NOTIFY;
- `gen_random_uuid` (ids are made in Python);
- JSONB (`sa.JSON` everywhere);
- `array_agg`, `LATERAL`, `DISTINCT ON` (outside identity), sequences;
- `CONCURRENTLY` in SQL;
- a kernel `CREATE ROLE`;
- handling of SQLSTATE 23505/40001/40P01 (no unique-violation, serialization or deadlock mapping anywhere).

## By guarantee

1. **Tenant isolation (RLS, system channel).**
   - (b) `sql_alchemy/rls.py` builds the policy from `current_setting('sandloom.tenant_id')`, a regex and a `::uuid` cast, then runs ENABLE/FORCE RLS, `CREATE POLICY ... AS PERMISSIVE` and `GRANT ... TO sandloom_system`.
   - (b+c) `sql_alchemy/tenant_rls.py`:
     - a merged `BEGIN; set_config(...)` built the way asyncpg builds it;
     - wraps the dialect's `do_begin`/commit/rollback;
     - refuses any database but PostgreSQL;
     - `pg_roles` checks (rolsuper, rolbypassrls) and `pg_policies` coverage queries.
   - (b+c) `planner_adapter/units.py:248`, `forms.py:183`: `set_config` with the asyncpg `$1` style.
   - (a) `tenant.py:31,58,504`: `postgresql.UUID` for `tenant_id`.
   - (b) `drivers/system_db.py`: a least-privilege login role that reaches other tenants only through the RLS system policy.
   - (b) `production_profile.py:125,229`: the strict profile requires `tenant_rls`, so production requires PostgreSQL.
   - (b) core migration `0010_saga_state.py`: `pg_roles`, `postgresql.UUID`, RLS.
2. **Exactly-once claims and idempotency** (command log, HTTP request log, durable inbox, dedupe).
   - (a) `dedupe_log.py`: chooses `pg_insert`/`sqlite_insert` by dialect, with `ON CONFLICT DO NOTHING | DO UPDATE ... WHERE expires_at <= now ... RETURNING`. `_generic_claim` is a savepoint fallback for other databases; the expiry extension runs on PostgreSQL only.
   - (c) `dedupe_log.py:53,61-68,234`: SQLSTATE `55P03` maps to `DedupeBusy`.
   - (b) `request_log.py:85,107`: `pg_try_advisory_xact_lock(ns, k)`, so a request never waits.
   - `command_log.py` and `durable_inbox.py` go through `dedupe_log`. `effects.ClaimSpec` is documented in ON CONFLICT terms.
3. **Transactional outbox and relay.**
   - (a) `outbox_store/statements.py`: `id = ANY(:ids)` with `ARRAY(UUID)`, and IN elsewhere.
   - (b, optimisation) `outbox_store/__init__.py`: `set_config('synchronous_commit','off',true)` and an AUTOCOMMIT view.
   - (b) `outbox_store/relay.py:120,146,420-430`: `FOR UPDATE SKIP LOCKED` in a CTE with UPDATE ... RETURNING, so concurrent relays split the backlog.
   - (a, already split) `effect_rows.py:160-182`: an `unnest(CAST(:x AS UUID[]/JSON[]/TIMESTAMP[]))` bulk insert, with `_write_portable` as the fallback.
   - (a) `models/outbox.py`, `consumer_inbox.py`, `audit.py`: `postgresql.UUID`; three partial indexes with `postgresql_where`. Core migrations 0001 and 0005 have the same partial indexes.
4. **Sagas** (single owner per instance, timer sweep).
   - (b) `saga_store.py`: `pg_insert` ON CONFLICT DO NOTHING/UPDATE, and `with_for_update(skip_locked=...)`.
   - `sagas/runtime.py`, `sweep.py` and `ops.py` depend on SKIP LOCKED ownership.
   - (a) `models/saga.py`: `postgresql.UUID` and a partial index.
5. **Projections** (monotonic idempotent apply; purge and rebuild serialised).
   - (a) `projections/upsert.py:40,117-122`: `pg_insert` ON CONFLICT DO UPDATE WHERE the stored version is older.
   - (a) `sdk/_decl/projections.py:45,580-590`: `pg_insert`, `excluded`, `func.greatest`, ON CONFLICT WHERE tenant. This is in the public SDK.
   - (a+b) `projection_watermarks.py`: `pg_insert` upserts, `FOR SHARE`, and `pg_advisory_xact_lock(hashtextextended(:key, 0))`.
6. **Ordered timestamps.**
   - (a) `columns.py:40,51-52`: `clock_timestamp()` as server default and onupdate.
   - (a) `cli/add.py:521` scaffolds it; `sdk/_prim/recipes.py:104-125` documents it.
7. **Read consistency and read-only units.**
   - (b) `di.py:259,357-372`: `postgresql_readonly=True` and REPEATABLE READ READ ONLY snapshots.
   - (b) `planner.py:85-95`: isolation names.
   - (b) `planner_adapter/__init__.py:120-150`: PostgreSQL-only, and a silent no-op elsewhere.
   - (c) `planner_adapter/compiler.py`: optimized executors only for `postgresql+asyncpg`, with a reported fallback elsewhere.
   - (c) `read_only_policy.py`: asyncpg only.
   - (b) `read_guard.py`: relies on server-side READ ONLY for raw `text()` writes.
   - (c) `driver_compat.py`: builds the BEGIN text and probes private asyncpg and SQLAlchemy-asyncpg internals.
8. **Request safety: timeouts, dead connections, reconnect.**
   - (c) `drivers/sqlalchemy_database.py`:
     - `SQLALCHEMY_SQL_DIALECTS = {"postgresql"}`;
     - asyncpg connect timeout;
     - `server_settings` for `statement_timeout`/`idle_in_transaction_session_timeout`;
     - contract pool `lock_timeout`.
   - (c) `dead_connection.py`: SQLSTATE `57P0x`/class `08`, and asyncpg exceptions and transaction internals.
   - (c) `reconnect.py:78`: `53300`.
   - (c) `facade/_claims.py`, `module_api/_read_scope.py`: `25P02`.
9. **Configuration.**
   - (c) `config/settings.py:250-263`: `DATABASE_URL` and `DATABASE_SYSTEM_URL` must be `postgresql+asyncpg://`. This is a hard gate.
   - (c) `migrations/urls.py`: rewrites async URLs to `psycopg2` and maps ssl to sslmode.
10. **Migration locking and provisioning.**
    - (b) `migrations/migrations.py`:
      - `pg_locks`/`pg_stat_activity`;
      - the run-wide `pg_try_advisory_lock`;
      - `CREATE DATABASE` under an advisory lock;
      - `CREATE EXTENSION citext, pg_trgm`.
    - (c) `migrations/migrations.py`: `42P04`, `-c statement_timeout`.
    - (b) `alembic_support.py`: `pg_advisory_xact_lock` when alembic is run directly. The partial-index refusal and `_render_item` are already dialect-aware.
    - (a) `planner_safety.py`: reads `dialect_options["postgresql"]` and `postgresql.CITEXT`.
    - (d) `migrations/backup.py`: psycopg2 catalog queries.
11. **Shipped ops CLI (d, PostgreSQL by design).**
    - `cli/extract.py`: psycopg2, `pg_class`/`pg_attribute`/`pg_trigger`, md5 row hashing, ON CONFLICT, `FOR UPDATE`, `SET LOCAL lock_timeout`, `55P03`, BYPASSRLS checks.
    - `cli/migrate_drift.py`: `CREATE`/`DROP DATABASE WITH (FORCE)`.
    - `cli/backup.py`: `pg_dump`/`pg_restore`.
    - Scaffolds hard-code `postgresql+asyncpg` and `postgres:16`.
12. **Dev and test harness (d).**
    - `core/testing`: `connections`, `session_checks`, `slots`, `migrations`, `workers`, `database`, `resources`, `ownership`. These cover `CREATE DATABASE`/`EXTENSION`, `pg_database`, `shobj_description` and `DROP ... WITH (FORCE)`.
    - `cli/dev/services.py`, `commands.py`: the database lifecycle through psycopg2.

## Already abstracted (build on these)

- **The module-facing SQL forms seam:** `sql_alchemy/portable.py` (`SqlDialect`, the `sandloom.sql_dialects` registry, `UnsupportedSqlForm`), with renderers in `drivers/sql_dialects.py`. The kernel's own `pg_insert`, advisory-lock and partial-index sites do not use it yet.
- **Capability features:** `DATABASE_CAPABILITY.features`, declared by the driver in `SQLALCHEMY_DB_*_FEATURES`. These are transactions, write_admission, savepoints, transactional_outbox, audit, tenant_rls, command_idempotency and http_idempotency.
- **Store contracts on `DatabaseProvider`:** `OutboxStore`, `AuditStore`, `CommandLogStore` (`CommandLogBusy`), `RequestLogStore` (`RequestLogBusy`), `execution_adapter()` (`infra/contracts.py:120-670`).
- **Write admission is close to dialect-free.**
- **Fallbacks already written:**
  - `dedupe_log._generic_claim`;
  - `effect_rows._write_portable`;
  - `statements._id_in`;
  - the planner's stated fallback;
  - the migration env's partial-index refusal;
  - C028.

## The five hardest items

1. **RLS tenant isolation:** policies, FORCE RLS, role attributes, a transaction-local GUC, a separate login role. MySQL and SQLite have no equivalent. The strict production profile requires it, so another database means either an ORM-only mode taken as an accepted risk, or a refusal. The guarantee has to be re-specified.
2. **SKIP LOCKED work partitioning** for the outbox relay (a CTE with UPDATE ... RETURNING) and saga ownership. SQLite has no row locks, and MySQL 8 has SKIP LOCKED but no UPDATE ... RETURNING. The fallback changes the concurrency model: lease columns with a compare-and-set UPDATE, or one worker per database.
3. **Non-blocking claims through advisory locks:** `pg_try_advisory_xact_lock` in the request log, the projection purge lock, the migration and provisioning locks, and the `55P03` lock_timeout-to-Busy mapping. A driver needs a lock primitive with try and transaction-scope variants, a portable key hash and its own error taxonomy.
4. **Driver internals coupling:** the merged `BEGIN; set_config`, `driver_compat`'s private probes, `dead_connection`'s asyncpg exceptions and SQLSTATEs, `server_settings`, the planner's raw `$1` executors. These are hot-path optimisations measured with cachegrind. They need a per-driver connection adapter rather than a renderer, and must keep their numbers.
5. **Hard gates and isolation semantics:** the `postgresql+asyncpg` URL gate, `SQLALCHEMY_SQL_DIALECTS={"postgresql"}`, REPEATABLE READ READ ONLY snapshots and `postgresql_readonly`. Lifting the gate exposes silent downgrades; each needs a feature flag (`snapshot_reads`, `readonly_tx`) and an explicit refusal.

The easier group, category (a): UUID columns, `clock_timestamp`, the `pg_insert` upserts, `ANY(:ids)` and the `unnest` bulk insert. These can move behind the existing renderers and `sa.Uuid`, and several already have fallbacks.
