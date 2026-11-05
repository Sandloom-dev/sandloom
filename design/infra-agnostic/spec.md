# Spec: complete the database capability

Status: **v2, reviewed, 2026-11-04**.
- tm: `infra-agnostic-spec`, then the slices of `infra-agnostic`.
- Supersedes the framing of [design.md](design.md), which stays as the research summary.
- v1 was reviewed independently: 4 blockers, 12 should-fix. The [changes from the review](#changes-from-the-review) are at the end.
- Inputs: [inventory](inventory-kernel-postgres.md), [ORM research](research-orm-backends.md), [reliability research](research-reliability-mechanisms.md), [interface review](review-relational-interface.md).

## Decisions this spec rests on (owner, 2026-11-04)

1. **No new architecture.** This completes the existing capability, driver and feature architecture for the `database` capability.
2. **Database drivers are equal.** No database has a special status in code; databases differ only in what their driver declares.
3. **Tenant isolation without database row security** runs application-enforced (the ORM guard) and is said aloud. The strict production profile keeps requiring database-enforced isolation.
4. **The second database is SQLite**, for development and tests.
5. **Database-specific power** comes as `sandloom.sdk.data` forms that are features of the `database` capability. A module requires them through `CapabilityRequirement`. There is no vendor namespace.

## What exists and stays

- **The `database` capability**:
  - its features: `transactions`, `savepoints`, `transactional_outbox`, `audit`, `tenant_rls`, `command_idempotency`, `http_idempotency`, `write_admission`;
  - `DatabaseProvider` (`core/infra/contracts.py:607`), whose docstring already says the kernel imports no SQLAlchemy;
  - the registry's build-free `features_for(options)`, checked against the built instance (`registry.py:633`).
- **The one relational driver, `sqlalchemy`.** Its implementation is `core/sql_alchemy/`, the driver's own code: unit of work, stores, RLS, planner adapter.
- **The SQL dialect renderer from this wave**:
  - `SqlDialect` with six forms, including `conflict_insert`;
  - the `sandloom.sql_dialects` entry points, `PostgresqlSql` and `SqliteSql`;
  - the R015/B024 refusals, `SqlDialectConformance`, and the SQLite schema lane.

## The design in one paragraph

The `sqlalchemy` driver serves every SQL database that has a **dialect renderer**.
- **The renderer** is the one place that describes a database: its SQL forms, error codes, lock statements, row-security SQL and features. It returns SQL and facts. It never runs connections or sessions; those stay in the driver.
- **An instance's features** come from options and the URL's dialect only, so the build-free preflight stays exact. The server's version is a minimum checked when the instance opens, and never changes the feature set.
- **The driver's code** (`core/sql_alchemy/`) asks the renderer instead of branching on `dialect.name`.
- **The rest of the kernel** asks features and `DatabaseProvider`.
- **A guarantee the instance cannot give** refuses at startup. Or, where the guarantee lattice already computes labels from mechanisms, it shows the weaker label. It is never weakened silently.

## 1. Renderer contract v2

`SqlDialect` (`core/sql_alchemy/portable.py`, exported from `sandloom.driverkit`):

```python
class SqlDialect(Protocol):
    contract_version: int        # REQUIRED, 2. Missing means 1; the loader refuses another major.
    name: str                    # REQUIRED, the SQLAlchemy dialect name

    # REQUIRED facts
    def features(self) -> frozenset[str]: ...            # renderer-declarable features (section 2)
    minimum_server_version: tuple[int, ...] | None      # checked when the instance opens
    def classify_error(self, exc: BaseException) -> ErrorKind: ...
    #   ErrorKind = Literal["unique_violation", "serialization_failure", "deadlock",
    #                       "lock_not_available", "connection_lost", "other"]

    # forms; None = this database cannot (v1 forms unchanged)
    def case_insensitive_text(self, length) -> TypeEngine | None: ...
    def partial_index_options(self, where) -> dict | None: ...
    def search_index_options(self) -> dict | None: ...
    def search_operand(self, column: str, case_insensitive: bool) -> str | None: ...
    def conflict_insert(self, table) -> ConflictInsert | None: ...
    def greatest(self, *exprs) -> ColumnElement: ...       # PostgreSQL greatest(), SQLite max()
    def id_in(self, column, ids) -> ColumnElement[bool]: ...
    def wall_clock(self) -> str: ...                      # SQL of "now, at the call"

    # SQL the driver runs (sync or async, its choice)
    def lock_statement(self, key: str | int, *, scope: Literal["transaction", "session"],
                       wait: bool) -> Executable | None: ...
    def begin_statement(self, *, write: bool, read_only: bool, snapshot: bool) -> str | None: ...
    def row_security(self) -> RowSecurity | None: ...    # policy DDL, binding SQL, role checks
```

- **The v1 `transaction_lock` form** becomes the kernel helper built on `lock_statement(scope="transaction", wait=True)`. Its public signature stays.
- **`lock_statement` keys.**
  - PostgreSQL hashes a string key on the server with `hashtextextended`, exactly as `projection_watermarks.py:55` does today, so old and new processes exclude each other during a rolling deploy.
  - Integer keys and `(ns, k)` pairs keep their current SQL.
- **`RowSecurity`** carries the policy DDL, the per-transaction binding SQL, and the role and coverage check queries. The binding *mechanics* stay in `sql_alchemy/tenant_rls.py`: the merged `BEGIN; set_config` on asyncpg is a measured hot path.
- **Versioning sits in `portable._load_entry_points`.** It refuses a renderer whose `contract_version` major differs (a new code, B025), and refuses a renderer that declares a feature outside section 2.
- **v2 is pinned by tests.** Its names join the `sandloom.driverkit` API baseline (ADR-0011 §5, T008).

## 2. Features

**Instance features:**
- start from the driver's base: `transactions`, `savepoints`, `transactional_outbox`, `audit`, `command_idempotency`, `http_idempotency`, `write_admission`;
- add the renderer's declared features, limited to the renderer-feature set below;
- add `tenant_rls` when the option is on.

Every renderer-declarable feature is in the driver's static declaration, `SQLALCHEMY_DB_FEATURES`, because the registry intersects with it (`registry.py:108`).

| renderer-declarable feature | meaning | PostgreSQL | SQLite |
|---|---|---|---|
| `skip_locked` | concurrent workers split a queue without blocking | yes | no |
| `transaction_locks` | a named lock held until commit or rollback | yes (advisory) | yes (one writer, `BEGIN IMMEDIATE`, section 6) |
| `snapshot_reads` | a read-only unit sees one snapshot | yes | yes (a deferred read transaction in WAL is a snapshot) |
| `readonly_transactions` | the database refuses writes in a read-only unit | yes | no (the ORM read guard only) |
| `arrays` | `Array` columns | yes | no |
| `fulltext_search` | `fulltext_index` and `matches()` | yes | no |
| `json_path` | JSON path extraction | yes | yes (JSON1) |
| `json_contains` | JSON containment | yes | no |

**Not features:**
- **Row security.** No module would require it. `tenant_rls=True` on an instance whose renderer has no `row_security()` stops startup (B026), and so do `system_url` and `require_system_channel` without it.
- **`search_index`.** It degrades on purpose, to unindexed search.

**`sandloom check`** gains a rule: a module that uses a feature form (`Array`, `fulltext_index`, the JSON forms, `transaction_lock`) declares the matching `CapabilityRequirement("database", features=...)`.

## 3. Each kernel site

Sites per [the inventory](inventory-kernel-postgres.md), including those the review found.

| guarantee | sites | change | without the feature |
|---|---|---|---|
| inbox, command and HTTP claims | `dedupe_log.py` (`pg_insert`/`sqlite_insert` by dialect; the expiry extension at :265-276, PostgreSQL only), `command_log.py`, `durable_inbox.py` | `conflict_insert` from the renderer; the expiry computed in Python; `55P03`/SQLITE_BUSY → `classify_error` → Busy. Busy always ends the unit with a rollback (PostgreSQL aborts the transaction after `55P03`). | same guarantee on both databases |
| HTTP "busy, never wait" | `request_log.py:85,107` | `lock_statement(scope="transaction", wait=False)` | same on both |
| outbox writes | `models/outbox.py`, `statements.py` `ANY(:ids)`, `effect_rows.py` `unnest` | `sa.Uuid`; `id_in`; the `unnest` fast path only where the renderer says so, `_write_portable` otherwise | same |
| outbox relay | `relay.py` (SKIP LOCKED CTE; `with_for_update` at :120,146,420; text with UUID binds at :240) | with `skip_locked`: as today. Without: claim each row by compare-and-set on the existing `locked_until`/`locked_by` columns (`UPDATE ... WHERE id=:id AND (locked_until IS NULL OR locked_until < :now)`, rowcount = 1). Typed bindparams. | at-least-once and parallel relays kept |
| sagas | `saga_store.py` (`pg_insert` :127,195,213; `with_for_update` :164,296), `sagas/ops.py:120` | `conflict_insert`; the sweep claims by compare-and-set as the relay does; the instance row lock stays (serial writers on SQLite) | same |
| projections | `projections/upsert.py`, `sdk/_decl/projections.py:580-590` (`func.greatest`), `projection_watermarks.py` (`pg_insert`, FOR SHARE, `hashtextextended` lock) | `conflict_insert` + `greatest`; purge lock through `lock_statement` | same |
| ordered timestamps | `columns.py`, `cli/add.py:521`, recipes | `WallClock()` server default compiled through `wall_clock()`; PostgreSQL renders `clock_timestamp()`, so no migration and no drift | same `(created_at, id)` order. SQLite clocks have millisecond resolution; ties are broken by the UUIDv7 id, which is monotonic per process. |
| tenant isolation | `rls.py`, `tenant_rls.py`, `system_db.py`, `planner_adapter` `set_config`, `production_profile.py`, core migration 0010 | RLS DDL, binding SQL and role checks from `row_security()`; mechanics stay in the driver | ORM guard only; startup warns "application-enforced"; the strict profile refuses (as today) |
| `tenant_id` column | `tenant.py` `postgresql.UUID` | `sa.Uuid` | same |
| read-only and snapshot units | `di.py`, `planner.py`, `planner_adapter`, `read_guard.py` | `begin_statement(...)`, precomputed per `(write, read_only, snapshot)` | a unit requiring `snapshot_reads` on an instance without it refuses (B027) instead of today's silent no-op; read-only without `readonly_transactions` relies on the ORM read guard |
| errors and connections | `dead_connection.py`, `reconnect.py`, `facade/_claims.py`, `module_api/_read_scope.py` (SQLSTATEs) | `classify_error`; asyncpg-only fast paths stay in the driver, behind a check of the DBAPI | same |
| configuration | `settings.py:250-263` gate; `migrations/urls.py` | the `Settings` validator accepts any async SQLAlchemy URL (`core.config` is a leaf and cannot ask renderers, ADR-0007); B024 at boot keeps "a renderer exists" | B024 |
| migrations | advisory locks in `migrations.py`/`alembic_support.py` (sync psycopg2 engines); `CREATE EXTENSION`; `-c statement_timeout` | `lock_statement(scope="session")` executed on the sync connection; extensions as renderer-declared setup DDL. **SQLite does not run chains** (section 6). | `sandloom migrate` refuses SQLite (B028) |

**Outside this spec:**
- **The ops tools** (`cli/extract.py`, `cli/backup.py`, `migrations/backup.py`, `migrate_drift.py`'s `CREATE DATABASE`, dev services) stay PostgreSQL-only and refuse another database with a clear message.
- **The relational interface review's P0s** have their own tasks.

## 4. The kernel names no database

An architecture test extends the C028 analyzer to the framework.
- **Allowed only in `core/drivers/**` and the core migration chain:** a dialect import, a `dialect.name`/`.driver` comparison, or a PostgreSQL-only `func.*` name.
- **A ratchet.** Today's sites start in an allowlist that may only shrink. Each slice removes its rows. The work is done when only the ops tools remain.

## 5. Database-specific forms as features (decision 5)

`sandloom.sdk.data` gains:
- `Array(item_type)`;
- `fulltext_index(name, *columns, language=)` with `matches(column, query)`;
- `json_path(column, path)` and `json_contains(column, value)`.

**Contract:** each form is rendered by the renderer and needs its feature. A module declares the requirement; the check rule of section 2 makes it impossible to forget. Startup refuses the module where the feature is missing.

**Public API:** the forms join the `sdk.data` advanced tier and the baseline.

## 6. SQLite, the second database

- **Connection.** `sqlite+aiosqlite://`, with `aiosqlite` in a new `sqlite` extra of the `sandloom` package.
- **Transactions.**
  - The driver turns pysqlite's implicit transactions off and emits `BEGIN` itself (the SQLAlchemy recipe), so SAVEPOINT works: `begin_nested()` and the rolled-back test fixture.
  - A unit that may write begins `BEGIN IMMEDIATE`.
  - WAL is on, and `busy_timeout` comes from the statement timeout.
  - Every write transaction is therefore serial: `transaction_lock`, `FOR UPDATE` (a no-op that is now correct) and the claims hold.
  - SQLITE_BUSY/LOCKED classify as `lock_not_available`.
- **Several processes** (API, Celery worker, relay) work through WAL, `BEGIN IMMEDIATE`, `busy_timeout` and the row compare-and-set leases of section 3.
- **Schema.** SQLite does not run the migration chains, which are PostgreSQL's DDL history (RLS, `clock_timestamp`, trigram indexes).
  - The application's schema is built from the models by `metadata.create_all`, through the existing test harness path and a `sandloom dev` path.
  - `sandloom migrate` refuses SQLite (B028).
  - Making chains portable is out of scope.
- **Types.**
  - `DateTime(timezone=True)` comes back naive on SQLite, so the driver's SQLite path attaches UTC on load (a type decorator in the renderer's types).
  - `sa.Uuid` is CHAR(32), so raw `text()` with UUID binds uses typed bindparams.
- **Production.** SQLite outside development and tests needs an accepted risk (ADR-0006, B029); the strict profile refuses it.
- **Done when:**
  - an application generated by `sandloom new` with the standard modules starts on SQLite, with its schema from `create_all`;
  - its module tests pass there, and tests needing a missing feature skip through `skip_unless_db_feature`;
  - this runs as the CI lane `second-db-test-lane`.

## 7. Guarantees and labels

Labels are **computed, not declared** (ADR-0010 §6, `guarantee_lattice.py`):
- the `Mechanism` gains fields read from instance features;
- application-enforced isolation is a security-model statement plus a startup warning;
- the G-TENANT and RLS entries in `guarantees.yml` are qualified, with tests, through the ledger process (a `removed:`/changed entry, owner-approved). The ledger never weakens silently.

## 8. Error codes

| code | refusal |
|---|---|
| B024 | a configured database without a renderer, or one its driver does not serve (exists) |
| B025 | a renderer of another contract major, or one that declares an unknown feature |
| B026 | `tenant_rls`/system channel on a database without row security |
| B027 | a unit requires `snapshot_reads` and the instance lacks it |
| B028 | `sandloom migrate` on a database without a migration path (SQLite) |
| B029 | SQLite outside development and tests without its accepted risk |
| B030 | a server below the renderer's minimum version |
| R015 | a form the database cannot write (exists) |

Each code gets its explain entry and its docs section.

## 9. Testing

- **`skip_unless_db_feature(name)`** in `sandloom.sdk.testing`, reading the same instance features. It joins the advanced tier and the baseline.
- **The conformance kit** runs on PostgreSQL and SQLite with real engines, covering concurrent claims, `conflict_insert ... WHERE`, locks, and the relay compare-and-set.
- **cachegrind.** Slices 2, 6 and 7 touch measured paths (claims, the RLS `BEGIN`, the planner's read-only units) and state before and after numbers on PostgreSQL.

## 10. Slices, in order

| # | slice | needs | acceptance |
|---|---|---|---|
| 1 | Renderer v2 skeleton: required members, versioning in the loader (B025), `features()`, `classify_error`; instance features from the renderer; minimum server version (B030); the kernel-names-no-database ratchet | — | conformance + unit tests; PostgreSQL features unchanged; ratchet green |
| 2 | Claims and upserts onto `conflict_insert`, `greatest`, `id_in`; the dedupe expiry in Python; `classify_error` for Busy | 1 | store and projection suites; cachegrind before and after |
| 3 | Locks: `lock_statement`; request log, projection purge, migrations (sync), `transaction_lock` on it | 1 | concurrency tests in the kit |
| 4 | Relay and saga sweep claim by row compare-and-set when `skip_locked` is missing | 1 | relay test on the compare-and-set path, forced on PostgreSQL too |
| 5 | `WallClock` for `CreatedAt`/`UpdatedAt` and the scaffold | 1 | drift none on PostgreSQL |
| 6 | Row security through `row_security()`; B026; the system channel; the computed label and warning; the ledger entries | 1 | RLS suites; cachegrind on the RLS `BEGIN` |
| 7 | Read-only and snapshot units through `begin_statement`; B027 | 1 | no silent no-op left |
| 8a | SQLite transactions and types in the driver: implicit transactions off, `BEGIN IMMEDIATE`, WAL, `busy_timeout`, UTC on load, typed UUID binds, `aiosqlite` extra, classification | 2, 3 | a unit-of-work and store suite on SQLite |
| 8b | Settings validator relaxed; schema by `create_all` for SQLite; `sandloom migrate` refuses (B028); production refusal (B029) | 8a | an app boots on SQLite |
| 8c | Test harness SQLite path (`core/testing`: databases, slots, ownership, resources); `skip_unless_db_feature` | 8b | standard modules' tests on SQLite |
| 8d | Generated app on SQLite; CI lane `second-db-test-lane` | 8c | section 6's "done when" |
| 9 | `Array`, `fulltext_index`, JSON forms; the requirement check rule (including `transaction_lock`) | 1, 8a | PostgreSQL advertises them; a module using one refuses on SQLite |
| 10 | Identity's claims query without `array_agg`/`LATERAL` | 8c | identity's suite on both databases |
| — | ADR-0011 amendment (renderer v2 and kit, why transaction locks are a database feature and not ADR-0011's private `lock` capability); `docs/infrastructure/database.md`; upgrade notes; changelog; DX eval re-run | each slice | per slice |

## Risks

- **Hot paths.** Renderer answers are computed once per dialect and cached. `begin_statement` is precomputed per combination. The cachegrind check is part of slices 2, 6 and 7.
- **Two code paths per guarantee** (the native claim and the compare-and-set relay). The compare-and-set path is also forced on PostgreSQL in a test, so it cannot rot.
- **The renderer grows into a second driver.** It returns SQL and facts only; connections, pools, sessions and the binding mechanics stay in the `sqlalchemy` driver. A database that needs another DBAPI model is a new driver.

## Changes from the review

The review of v1 found four blockers and twelve should-fixes. What changed:

- **Features and versions (B1).** Features no longer depend on the server version; the version is only a minimum, checked when the instance opens.
- **SQLite's schema (B2).** SQLite takes its schema from `create_all`; `sandloom migrate` refuses it.
- **SQLite transactions (B3).** The driver uses `BEGIN IMMEDIATE`, WAL and `busy_timeout`. SQLite therefore advertises `transaction_locks`, and the purge-refusal row is gone.
- **Slice 8 (B4).** Split into 8a to 8d.
- **Fewer new forms.**
  - `insert_or_skip` and `guarded_upsert` are dropped in favour of the existing `conflict_insert` plus `greatest`.
  - The relay keeps parallel claims through compare-and-set on the existing lease columns; no new table and no saga version column.
- **Locks.** `lock()` becomes `lock_statement()`, which returns SQL; PostgreSQL keeps `hashtextextended`.
- **Row security.** The binding mechanics stay in the driver.
- **Features.** The algebra is fixed. `row_security` and `search_index` are no longer features, and the JSON feature is split in two.
- **Settings and labels.** The settings gate stays in `core.config`; labels are computed, not declared.
- **New ground.** New error codes, SQLite types, multi-process SQLite, the production refusal, and the ADR amendment.

## Before slice 8a (from the re-check of v2)

The re-check found v2 ready for slice 1. These gaps must close before slice 8a.

- **Which units begin IMMEDIATE.** On SQLite, every unit not declared read-only begins `BEGIN IMMEDIATE`. The lazy request unit cannot know at BEGIN whether it will write, so reads before its first write must be covered too.
- **Locks on SQLite.** `lock_statement` returns no SQL on SQLite. The `transaction_lock` helper succeeds there only inside an IMMEDIATE unit, which already holds the database, and refuses (R015) otherwise. Identity's role administration therefore works on SQLite.
- **In-memory databases.** `sqlite://` and `:memory:` have no WAL and no second connection. They are refused, except the test harness's `StaticPool`, and they never advertise `snapshot_reads` or `transaction_locks`.
- **The saga sweep's compare-and-set.** `saga_instances` has no lease columns. The sweep claims by `UPDATE ... SET swept_at=:now WHERE id=:id AND (swept_at IS NULL OR swept_at <= :swept_before)`, adding `swept_at` in a core migration if it does not exist.
- **The request-log row is not "same on both".** On SQLite a concurrent duplicate waits at `BEGIN IMMEDIATE` until the first commits, then replays; it never answers Busy, and past `busy_timeout` it gets a lock error. That is labelled.
- **UTC on load.** `CreatedAt`/`UpdatedAt` and the kernel models use a portable `UtcDateTime` type that attaches UTC to naive values. Modules that declare `DateTime(timezone=True)` themselves are told to use it.
- **Slice dependencies, corrected:**
  - 8a also needs 4 and 7;
  - 8b also needs 5;
  - 8c also needs 6.
- **`DATABASE_SYSTEM_URL`.** Its validator is relaxed like `DATABASE_URL`'s.
- **`create_all` never alters a table.** After a model change, `sandloom dev` recreates the SQLite file.
- **The kernel's own `postgresql_where` partial indexes** (`models/outbox.py`, `models/saga.py`) are listed in the ratchet. They move to `partial_index`.

## Where driver code lives (owner, 2026-11-04)

`core/sql_alchemy` is the `sqlalchemy` driver's code. Three kinds of knowledge sit in or around it, separated by what changes when a database is added:

| knowledge | examples | lives in | changes for a new database |
|---|---|---|---|
| dialect-neutral driver code | unit of work, the stores, the tenant guard, `Base`/`CreatedAt`, the portable forms | `core/sql_alchemy` | no |
| SQL dialect | `ON CONFLICT`, advisory locks, RLS DDL, `clock_timestamp()` | the renderer (`core/drivers/sql_dialects.py`) | a new renderer |
| DBAPI | asyncpg's merged `BEGIN; set_config`, dead-connection detection by asyncpg exceptions, the planner's raw executors, the read-only policy fast path | a DBAPI adapter in `core/drivers/` | a new adapter, only if it needs the speed |

**The ratchet stays on `core/sql_alchemy`.** It is the guarantee that a new SQL database needs only a renderer and no change to the driver's code.

**The asyncpg specifics move into `core/drivers/` as a DBAPI adapter.** The driver already installs them when it builds the engine (`install_dead_connection_handling`, `install_tenant_rls`, `install_idle_ping`), so they move without breaking the layers. aiosqlite gets the plain path.

**`core/sql_alchemy` is not renamed.** Modules use part of it (`Base`, `TenantScoped`, the forms) through `sandloom.sdk.data` as the relational paradigm.

**New slice 7b, the DBAPI adapter,** comes before 8a. It moves `driver_compat`, the asyncpg parts of `dead_connection`, `read_only_policy`, the planner's raw executors and the RLS `BEGIN` fast path behind an adapter chosen by the URL's DBAPI. The cachegrind numbers on PostgreSQL must not change.

**Done for this spec** means the ratchet allowlist is empty except the ops tools.

## Slice 7b, as built

- **The seam.** `core/sql_alchemy/dbapi.py` holds `DbapiAdapter` (contract 1; the base class is the plain path) and its registry, chosen by `dialect.driver`. `core/drivers/dbapi_asyncpg.py` holds asyncpg's adapter.
- **What moved there.** `driver_compat`'s asyncpg probes, `begin_sql` and `adopt_started_transaction`. The asyncpg parts of `dead_connection`. The merged row-security `BEGIN` from `tenant_rls`. The 53300 check from `reconnect`. The connect arguments and server timeouts. The raw executor's `citext` rule from `planner_safety`.
- **What the adapter decides.** The gates of the read-only policy, the planner's raw executor, connection recovery and reconnect backoff. `merged_begin`, `raw_executor` and `connection_recovery` are kernel-only; a driver package may claim `read_only_policy` and `reconnect_backoff` (B025).
- **One BEGIN source.** The merged `BEGIN` of the units the kernel opens is the renderer's `begin_statement`.
- **25P02** is the renderer's `transaction_aborted` kind.
- **B031** refuses `DB_READ_ONLY_SESSIONS` without `readonly_transactions` or a client policy.
- **Still duck-typing asyncpg, adapter candidates for 8a:** `_checkout` (`raw.is_closed()`) and `_ping` (`SELECT 1` on the raw connection) in `core/drivers/sqlalchemy_database.py`, and the raw executor's `fetchrow` / `$n` calls in the planner adapter.

## Naive and aware timestamps (from the slice 5 review), for slice 8a

- **Every module-facing `DateTime(timezone=True)` moves to `UtcDateTime`.** This includes the projection generator's `deleted_at`, billing's `*_at` columns, tenancy's `expires_at` and identity's token expiries. On PostgreSQL the DDL is the same, so no migration is needed.
- **A check flags a bare `DateTime(timezone=True)` in module models.** It is a `sandloom check` rule, or part of the second-database schema lane.
- **The kernel's naive `DateTime()` tables stay naive UTC everywhere** (`_utc_naive_now`) and are never compared with `UtcDateTime` values, unless they are converted together with their code paths.
- **SQLite has one timestamp text format,** `YYYY-MM-DD HH:MM:SS.ffffff`, shared by `WallClock` and bound values.
- **The process runs in UTC.** On PostgreSQL with asyncpg a naive value means the process's local time; on SQLite it means UTC. Slice 8a either rejects naive binds on `UtcDateTime`, with the planner's raw path doing the same, or states the UTC process requirement in the docs.

## From the slice 7 review, for slices 7b and 8a

- **7b, read-only guard.** The read-only policy installs through the DBAPI adapter (asyncpg today; aiosqlite and psycopg next). Otherwise startup refuses `DB_READ_ONLY_SESSIONS` on an instance that has neither the policy nor `readonly_transactions`. The optimized-executor gate becomes the adapter's decision.
- **7b, one source of BEGIN text.** The merged row-security BEGIN (`tenant_rls.py`, the general executor and `ReadOnlyDbSession`) takes its text from `begin_statement`, so it cannot drift from the planner's.
- **8a, real read-only and snapshot on SQLite.** Read-only uses `PRAGMA query_only=ON` per read-only checkout, after which SQLite declares `readonly_transactions`. A snapshot is a deferred `BEGIN` under WAL, after which it declares `snapshot_reads`. The conformance kit requires `transaction_options` to be non-empty when a feature is declared.

## Slice 8a, as built

- **The driver serves SQLite.** `SQLALCHEMY_SQL_DIALECTS` is `{"postgresql", "sqlite"}`; `aiosqlite` is the `sqlite` extra of the `sandloom` package. `core/drivers/dbapi_aiosqlite.py` holds the aiosqlite adapter (no capability: the plain path plus `prepare_engine`).
- **Engine setup** (`DbapiAdapter.prepare_engine`, called where the driver builds an engine):
  - `connect`: `isolation_level = None`, then `busy_timeout` (`DB_STATEMENT_TIMEOUT_MS`, 5 s when 0), `foreign_keys = ON` and, for a file, WAL;
  - `begin`: the unit's `BEGIN` from its `transaction_options`, else the renderer's `BEGIN IMMEDIATE`, and `WRITE_LOCK_HELD` in the connection's `info`; a read-only unit's `PRAGMA query_only = ON`; an `AUTOCOMMIT` connection begins nothing; pysqlite's implicit BEGIN, which SQLAlchemy's isolation reset turns back on, is turned off again;
  - `commit`/`rollback`: the mark is cleared; `query_only` is reset before COMMIT, and lazily at the connection's next BEGIN after a rollback (no statement in a failing transaction).
- **The renderer.** `begin_statement` is `BEGIN IMMEDIATE` for every unit not read-only and `BEGIN DEFERRED` for a read-only one (one WAL snapshot, so a per-statement read unit gets the stronger answer). `transaction_options` carries the `BEGIN` and `query_only` for read-only units and `BEGIN IMMEDIATE` for an explicit writable snapshot. Features: `transaction_locks`, `snapshot_reads`, `readonly_transactions`. `classify_error`: BUSY/LOCKED → `lock_not_available`, `BUSY_SNAPSHOT` → `serialization_failure`, by name or by numeric extended code.
- **Locks.** `lock_statement` stays `None`. `portable.hold_transaction_lock` (behind `transaction_lock` and the projection purge lock) runs the statement where there is one, succeeds where `transaction_locks` is declared and the transaction holds `WRITE_LOCK_HELD`, and refuses R015 otherwise (a read-only unit). `WRITE_LOCK_HELD` joins `sandloom.driverkit`.
- **In memory.** B032 (`sqlite-unavailable`) refuses an in-memory URL, a SQLite URL without `+aiosqlite` and a missing aiosqlite, in the driver factory, `_pooled_engine` and the build-free `configuration_problems`. `build_disposable_engine` accepts memory on a `StaticPool`. An in-memory instance withholds `snapshot_reads` and `transaction_locks` from its features (the planner's own snapshot check is per dialect; an in-memory engine has one connection).
- **The adapter took the remaining duck-typing.** `DbapiAdapter.ping` and `.connection_closed`, asyncpg's being the old `SELECT 1` on the raw connection and `is_closed()`; the plain path is the dialect's ping. (`_checkout` raised `AttributeError` on aiosqlite before.)
- **AUTOCOMMIT.** The outbox's mark-delivered AUTOCOMMIT view runs on any database (one statement, its own transaction; on SQLite it takes the write lock for that statement alone). Its `dialect.name` row left the ratchet: 5 sites remain (migrations 4, `core/testing/database.py` 1).
- **Times.** Every module-facing `DateTime(timezone=True)` is `UtcDateTime()`; C029 (`utc-datetime-columns`) flags a bare one outside migrations. Naive binds are **refused**: `UtcDateTime.process_bind_param` raises `NaiveDateTimeError`, `coerce_compared_value` keeps comparisons on the type and lets `column - timedelta` bind an interval. The planner's raw executor skips bind processing, so a caller's `UtcDateTime` key, filter or constant takes the general executor; a keyset column (its value read back aware) stays raw. The standard modules' suites passed unchanged once the interval coercion was in.
- **Conformance kit.** Requires non-empty `transaction_options` for a snapshot unit when `snapshot_reads` is declared, and a non-plain snapshot `BEGIN`; it no longer requires the per-statement read unit to begin differently.
- **Tests.** `core/tests/test_sqlite_driver.py` on a file database: units, savepoints, IMMEDIATE, a waiting writer, `busy_timeout` → lock error, read-only refusal and reset, one snapshot, `transaction_lock` inside and outside, AUTOCOMMIT, UTC on load, request log, command log and inbox claims, parallel relays by compare-and-set, AUTOCOMMIT mark, the saga sweep, projection watermarks, B032.
- **Left for 8b/8c.** The settings validator, `create_all`, B028/B029, the harness's SQLite path and `skip_unless_db_feature`.
- **Review fixes (before commit).** A second unit of the same asyncio task that begins `BEGIN IMMEDIATE` while the task's unit holds the write lock of the same database file on another connection is refused at its BEGIN (`NestedWriterRefused`, R016), not after `busy_timeout`; another task still waits. The adapter tracks the holder per resolved database file and checks the holder connection's `WRITE_LOCK_HELD`, which every BEGIN clears first. Startup asks the DBAPI adapter (`DbapiAdapter.config_problem`), so `app_factory` names no DBAPI. An in-memory test engine marks no write lock (its `transaction_lock` refuses). The kit's "one snapshot begins differently" check is back, waived by the test's `read_units_share_one_snapshot` (SQLite). C029 also catches `DateTime(True)` and `TIMESTAMP(timezone=True)`.

## Before slice 8b (from the review of 8a)

**B1, a gate: nested write units on SQLite.** A local contract call (`module_api/_call.py`, "a FRESH write-admission scope"), a saga step or a child `open_uow()` inside a unit opens a second unit on another pooled connection. On SQLite every writable unit begins `BEGIN IMMEDIATE`, so the second waits for its own caller until `busy_timeout` (reproduced: 31 s, `database is locked`). Slice 8a refuses it at once (R016). Before an application boots on SQLite (8b), one of these is chosen:

| option | what it does | trade-off |
|---|---|---|
| (a) nested units as a SAVEPOINT | on a database whose writers are serial, a nested unit of the same task runs on the caller's connection inside a SAVEPOINT | the inner unit's commit is no longer durable on its own: an outer rollback undoes it. A contract command, the outbox rows and the command-log claim it wrote vanish with the caller, which changes the contract atomicity the effect model and sagas rely on (ADR-0005), on SQLite only |
| (b) the lazy request unit begins DEFERRED until its first write | a request unit reads without the write lock, then upgrades at its first write | an upgrade after another writer committed fails with `SQLITE_BUSY_SNAPSHOT` (the snapshot is stale): the unit must be retried from the start, which the kernel does not do for request units; and it only helps a caller that has not written yet, so a nested write after the caller's first write still deadlocks |
| (c) keep the refusal | R016 stays; SQLite serves applications without nested write units | a standard application calls contracts inside units (billing, tenancy), so it would not run on SQLite unchanged; the scope of "SQLite for development and tests" shrinks to what the refusal allows |

**Recommendation: (c) for 8b, measured against the standard modules, with (a) as the follow-up if their suites need nested writes.** (c) keeps one atomicity contract across databases and fails loudly; (b) moves the deadlock instead of removing it and adds a retry path; (a) is the only option that runs every application, but it must be an explicit, labelled weakening of contract atomicity (a guarantee-lattice label, like application-enforced isolation), decided by the owner. 8c's run of the standard modules' tests on SQLite counts the R016 refusals and decides between (c) and (a).

## Slice 8b, as built

- **Settings.** `DATABASE_URL` and `DATABASE_SYSTEM_URL` accept any `<dialect>+<dbapi>://` URL (`core.config.settings.database_url_problem`, no DBAPI named). The rest is checked at boot, build-free: B024 for a dialect without a renderer (as before), and now also for a DBAPI SQLAlchemy has no dialect class for or a synchronous one (`sql_alchemy/dbapi.async_dbapi_problem`, run after the adapter's `config_problem`, so B032 still speaks first for SQLite).
- **Two optional renderer facts**, read with `False` when absent (`portable.schema_from_models`, `portable.development_only`); not `REQUIRED_MEMBERS` and not Protocol members, so a v2 renderer without them still loads and the driverkit baseline is unchanged. `SqliteSql` sets both. The kernel asks the facts, never the dialect name.
- **Schema from the models.** `DatabaseProvider.ensure_model_schema(metadata, recreate=)` (default `False`: the chains own the schema); the sqlalchemy provider runs `sql_alchemy/model_schema.ensure_model_schema` when its renderer says `schema_from_models`. `create_app` registers a critical `infra`-phase startup task after the warm-up that asks every database instance whose URL's renderer says so (no provider is built for the others), with `Base.metadata` (every module declared by then; the kernel models imported by `model_schema`). One `engine.begin()` (`BEGIN IMMEDIATE`), so processes starting together build it one after another. Every such instance gets every mapped table; which module lives on which SQLite file is left to 8c/8d.
- **Drift (B028).** `create_all` never alters: startup compares, over tables present on both sides and both ways, the columns, their nullability, the indexes (name and uniqueness; an index whose `ddl_if` skips this database, such as `search_index` on SQLite, is not expected) and the unique constraints (by columns); a model table in a schema is reported, never skipped. Any difference refuses, naming each. Recreating needs an explicit signal, never the environment (`ENVIRONMENT` defaults to `dev`): the ops setting `DB_RECREATE_ON_MODEL_CHANGE`, which `sandloom dev run` sets for the app it serves (an explicit value wins), ignored in prod. Then the driver disposes the engine, renames the file and its `-wal`/`-shm`/`-journal` to `<file>.stale-<UTC time>` (`dbapi_aiosqlite.set_aside_database_file`; nothing is dropped, the stale file still opens) and builds a fresh one, logging both paths. This is the spec's "`sandloom dev` recreates the SQLite file".
- **Workers.** `verify_worker_infra` asks each database provider's `ensure_model_schema(None, recreate=False)` (`None`: every mapped table), so a worker builds or checks the schema and refuses on drift, never recreating; the provider releases its pool after, before the fork.
- **System URLs.** `configured_database_urls` (app factory) feeds B024's two checks every instance URL, each instance's `system_url` option and `DATABASE_SYSTEM_URL`; `infra.config.database_instance_url` is the one `url or DATABASE_URL` lookup.
- **`sandloom migrate` (B028).** `migrate.main` refuses after `enforce_configuration` when any configured database takes its schema from the models (`migrations.migration_path_problem`), except `migrate databases`. A mixed configuration (PostgreSQL plus a SQLite instance) is refused whole.
- **Production (B029).** `app_factory.development_database_config_problem` in `configuration_problems`: `ENVIRONMENT=prod` on a `development_only` database needs the accepted risk `development_database_in_prod` (`refused={"prod_strict"}`); the strict profile refuses it even when accepted (it would also fail B026 via `tenant_rls`).
- **Roles on SQLite.** `DATABASE_SYSTEM_URL` set on a default database without row security is refused (B026) instead of silently ignored; PostgreSQL without `tenant_rls` keeps ignoring it. The application-enforced warning is unchanged; the platform's modules map no `TenantScoped` table, so it does not fire for them.
- **R016 encounters.** None at boot or in the smoke: the platform root (identity, tenancy, billing, admin) starts through its full lifespan on a file, `/health` 200, `POST /auth/register` 202 (public, writes and notifies), `POST /api/v1/orgs` 201 and `GET /api/v1/orgs` 200 (authenticated). The consumers that would nest (billing on tenancy events) were not driven; 8c's suites count them.
- **Tests.** `core/tests/test_sqlite_boot.py`: the settings shape, B024 for unknown and sync DBAPIs, the renderer facts, B029 (refused, accepted, strict, not in dev/test or on PostgreSQL), B026 for `DATABASE_SYSTEM_URL`, B028 for `sandloom migrate`, `create_all` then drift refused (columns, nullability, a unique constraint, an index, a schema), the platform's models without drift, the recreate signal setting the file aside with its rows, no signal leaving it untouched, the worker checking without recreating, system-URL probes, PostgreSQL with `DATABASE_SYSTEM_URL` and no `tenant_rls` unaffected, the PostgreSQL provider a no-op, and a child process booting the platform on a file, then refused after a stray column with `ENVIRONMENT` unset (file untouched), then set aside under `DB_RECREATE_ON_MODEL_CHANGE`. `test_dev_command.py` pins that `dev run` sets the signal. Each was shown failing with its code reverted.
