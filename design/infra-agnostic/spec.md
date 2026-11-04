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
