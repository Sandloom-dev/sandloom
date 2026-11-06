# Design: a database-agnostic kernel

Status: **proposal for the owner, 2026-11-04**. tm: `infra-agnostic`.

Built from:
- [the kernel inventory](inventory-kernel-postgres.md);
- [how ORMs do it](research-orm-backends.md);
- [how reliability mechanisms port](research-reliability-mechanisms.md);
- [the review of the relational interface](review-relational-interface.md).

## The problem

Business modules are now dialect-free (C028), but the kernel still runs on PostgreSQL only:
- row-level security for tenant isolation;
- `ON CONFLICT` claims in the outbox, inbox and idempotency stores;
- `SKIP LOCKED` relays and saga ownership;
- advisory locks;
- `clock_timestamp()`;
- REPEATABLE READ READ ONLY snapshots;
- asyncpg internals;
- a settings gate on `postgresql+asyncpg://`.

The inventory finds about 24 syntax sites, 22 capability sites, 20 driver-level sites and 25 tooling sites. The five hardest are:
1. RLS;
2. SKIP LOCKED partitioning;
3. non-blocking advisory-lock claims;
4. asyncpg internals on hot paths;
5. the hard gates that hide silent downgrades.

## What everyone else does (the pattern to copy)

Django, Rails, Hibernate, SQLAlchemy, Ecto, jOOQ, Prisma and Laravel all converge on the same pattern:
1. **The core speaks in intents.** A per-database object renders or executes them, and the core never branches on a vendor name.
2. **Capabilities are declared, named, false by default and version-aware.** Examples: Django's `BaseDatabaseFeatures`, Rails' `supports_*?`, Prisma's `ConnectorCapability`, Hibernate's version-aware `Dialect`.
3. **One capability vocabulary drives both runtime refusal and test selection** (`skipUnlessDBFeature`, SQLAlchemy's `SuiteRequirements`, Ecto's tags). A shared suite with declared, reasoned exclusions is the conformance test for third-party backends.
4. **Database-specific power sits in an opt-in namespace** (`django.contrib.postgres`, `sqlalchemy.dialects.postgresql`), never in the portable API.
5. **Schema history is per database**, with a non-transactional-DDL escape.
6. **Support tiers are explicit**: in CI with a maintainer, or best effort.
7. **Refusal beats a silent no-op.** The documented production bugs (SQLite `select_for_update`, MySQL's upsert ignoring its target) are all silent no-ops, and they cluster exactly on the primitives our kernel rests on: upsert conflict, locks, RETURNING, DDL transactions.

The reliability research adds the portable primitives:
- a **unique key plus INSERT, with the duplicate-key error classified by the driver**, as in NServiceBus, Temporal (`IsDupEntryError`) and Eventuate;
- **native named locks** (advisory lock, `GET_LOCK`, `sp_getapplock`), or a lease table with expiry;
- **ORM filter plus composite keys** as the tenancy baseline, with database RLS as optional defense in depth;
- **application-side ordering** (UUIDv7), not a database clock.

## Decision proposed: PostgreSQL first, portable by contract

Sandloom stays a PostgreSQL-first framework: one tier-1 database, with every guarantee implemented and measured there. Portability becomes a property of the architecture, not a promise that everything runs everywhere:
- the kernel asks a driver;
- the driver declares what it can do;
- a guarantee the driver cannot give either refuses to start or runs under a weaker, stated label, never silently.

A second database arrives as a driver package that passes the conformance suite. This keeps today's performance and guarantees intact and makes each further database a bounded piece of work.

## The architecture

```text
modules ──> sandloom.sdk ──> kernel (intents only) ──> database contract ──> driver package
                                                        ├─ DatabaseFeatures   (declared capabilities)
                                                        ├─ SqlDialect         (syntax intents)
                                                        ├─ behaviour ports    (stores, locks, relay, isolation)
                                                        └─ ConnectionAdapter  (driver internals, errors)
```

### 1. `DatabaseFeatures`: one declared capability object per driver

It replaces the scattered `dialect.name == "postgresql"` checks, the free-form feature set, and the `sql_dialects=` field added in this wave.
- **Shape.** Flags default to `False`, each with a reason string and the minimum server version.
- **Startup.** Reads the server version and refuses a server below the minimum.
- **Flags:**
  - SQL forms: `savepoints`, `transactional_ddl`, `insert_returning`, `upsert_conflict_target`, `upsert_where`, `partial_index`, `case_insensitive_text`, `search_index`;
  - locking: `skip_locked`, `named_lock_transaction`, `named_lock_session`;
  - isolation and reads: `row_security`, `snapshot_reads`, `readonly_transactions`;
  - timeouts: `statement_timeout`, `lock_timeout`.
- **The same flags** drive `skip_unless_db_feature` in module and kernel tests, and the B024/R015 refusals.

### 2. `SqlDialect`: syntax intents (extends what exists)

Today it has `case_insensitive_text`, `partial_index`, `search_index`, `conflict_insert` and `transaction_lock` for modules. Add the kernel's own intents:
- `insert_or_skip(table, row, key) -> inserted`: ON CONFLICT DO NOTHING, or INSERT IGNORE, or INSERT with a savepoint and duplicate classification;
- `guarded_upsert(table, row, key, version) -> inserted | updated | stale`: ON CONFLICT ... WHERE, or the MySQL `IF()` form, or the UPDATE→INSERT→retry emulation;
- `id_in(column, ids)`: replaces `ANY(:ids)`;
- `bulk_insert(...)`: replaces the `unnest` fast path, keeping the portable fallback.

`statement_clock()` is not a renderer intent; see timestamps below. The conformance kit (built in this wave) grows with every intent and includes **concurrency tests** for upsert and locks, where the documented bugs live.

### 3. Behaviour ports (Ecto-style, built on the store contracts that exist)

`OutboxStore`, `AuditStore`, `CommandLogStore` and `RequestLogStore` stay. New ports:

| port | PostgreSQL | portable fallback | refuse when |
|---|---|---|---|
| `ErrorClassifier` | SQLSTATE 23505/40001/40P01/55P03 | per-driver codes | missing: every reliability guarantee needs it |
| `LockProvider.named_lock(key, scope, wait)` | advisory locks, try and xact | `GET_LOCK` (session), `sp_getapplock`, a lease table | a transaction-scoped lock is needed and only a session lock exists |
| `RelayClaimer` | `FOR UPDATE SKIP LOCKED` | one relay under a named session lock, or lease columns | never; label becomes "one relay" |
| `TenantIsolation` | ORM filter + RLS | ORM filter + composite `(tenant_id, …)` keys | `tenant_isolation=database` configured without `row_security` |
| `SnapshotReads` | REPEATABLE READ READ ONLY | none | a route declares snapshot reads |

### 4. `ConnectionAdapter`: driver internals

These are the hot-path optimisations measured with cachegrind:
- the merged `BEGIN; set_config`;
- dead-connection detection;
- reconnect;
- `server_settings` timeouts;
- the read-only policy;
- the planner's raw executors.

They move behind one adapter that the asyncpg driver implements with the same numbers. A new driver implements the plain path and states that it does.

### 5. The kernel names no database

A new architecture test is the C028 analyzer applied to the kernel. It allows only `src/sandloom/core/drivers/**` and `sandloom.sdk.postgres` (below) to import a dialect or compare `dialect.name`, and nothing else in `core`, `sdk` or `shared`.

## Per-mechanism decisions

| mechanism | today | decision | guarantee on a non-PostgreSQL driver |
|---|---|---|---|
| inbox / command / HTTP idempotency claims | `ON CONFLICT` in `dedupe_log` | `insert_or_skip` in the effect's transaction; `ErrorClassifier` for Busy | same (exactly-once effect); refuse without classification |
| HTTP "busy, never wait" | `pg_try_advisory_xact_lock` | `LockProvider` try-lock, transaction scope | same, or refuse |
| outbox relay | SKIP LOCKED CTE | `RelayClaimer` | at-least-once kept; parallel relays become one |
| sagas | `pg_insert`, SKIP LOCKED | unique correlation key + version column; `FOR UPDATE` where present | same; multi-process SQLite refused |
| projections | `pg_insert ... WHERE version` (SDK!) | `guarded_upsert` | same (monotonic) |
| tenant isolation | ORM guard + RLS (strict profile) | ORM guard + composite keys baseline; RLS optional capability | labelled "application-enforced"; the strict profile still requires `row_security` |
| timestamps | `clock_timestamp()` default | `CreatedAt` from an application clock, monotonic per process; order stays `(created_at, id)` with UUIDv7 ids | same ordering contract |
| read-only / snapshot units | PostgreSQL GUCs, a silent no-op elsewhere | `readonly_transactions`, `snapshot_reads` flags | refuse a declared snapshot instead of a no-op |
| migrations lock | advisory lock | `LockProvider` session scope; reload applied revisions after acquiring (Rails) | same |
| module schema history | one database per chain | unchanged; `atomic=False` escape for non-transactional DDL | per database |

## PostgreSQL power without leaking it: `sandloom.sdk.postgres`

The review's sharpest point is that C028 took JSONB, ARRAY, enums and full-text search away from modules with nothing in their place. Following `django.contrib.postgres`:
- **Portable forms** in `sandloom.sdk.data` where a meaningful equivalent exists:
  - `Json` (jsonb on PostgreSQL, JSON elsewhere);
  - `Enum` (native, or a check constraint);
  - money as integer minor units or `Numeric`.
- **An opt-in `sandloom.sdk.postgres`** for what only PostgreSQL has: `ARRAY`, `JSONB` operators, full-text search, exclusion constraints, `DISTINCT ON`. A module that imports it declares `requires_database("postgresql")`; C028 allows exactly that namespace, and startup refuses the module on another database.

## Testing and support tiers

- **Tier 1, PostgreSQL:** full suite and performance budgets.
- **Tier 2, SQLite for development and tests:** once the kernel ports land, one worker process, `transaction_lock` and RLS refused.
- **Community drivers:** best effort, must pass the conformance suite, no CI promise.
- **Matrix CI.** The second-database lane grows from the schema build added in this wave to the full module suites, with exclusions taken from `DatabaseFeatures`.
- **Versioned driver API.** The driver kit already has `contract_version` (B022); it extends to renderers and ports. A new capability flag is additive and false by default, so older drivers keep working and refuse the new form.

## Not portability, but found on the way (the review's P0s)

These are separate tasks, ordered by risk:
1. **Migrations run with no `lock_timeout`.** An ALTER queued behind a long transaction blocks all traffic. Add `MIGRATION_LOCK_TIMEOUT_MS` and a zero-downtime page (expand/contract, concurrent indexes, batched backfills).
2. **`conflict_insert` on a `TenantScoped` table is refused** even with a tenant bound. Admit inserts whose `tenant_id` is absent or equal to the bound tenant.
3. **C015 forces `unscoped()` for any raw SQL.** Split "raw SQL" from "cross-tenant", and add `scoped_text()`.
4. **`open_uow()` opens `default`**, not the module's database as `units.md` says.
5. **The async lazy-load / N+1 trap.** `raise_on_sql` by default, or a check, plus `selectinload` guidance.

## Order of work (slices of `infra-agnostic`)

1. **Foundation:** `DatabaseFeatures`, the kernel-names-no-database test (with an allowlist that shrinks), and the drivers' declared server minimum.
2. **`ErrorClassifier`, `insert_or_skip`, `guarded_upsert`.** Moves `dedupe_log`, `saga_store`, `projections/upsert`, `projection_watermarks` and the SDK projections onto them (the old `infra-pg-features` scope).
3. **`LockProvider`:** request-log try-lock, projection purge, migrations.
4. **`RelayClaimer`** and the saga ownership change.
5. **`TenantIsolation` port** and the labels in `guarantees.yml` and the strict profile.
6. **Timestamps** from the application clock; `CreatedAt` no longer uses `clock_timestamp()`.
7. **`ConnectionAdapter`**, keeping the cachegrind numbers.
8. **Settings gate → driver-declared URLs**; the SQLite tier-2 driver; the full second-database CI lane.
9. **`Json`/`Enum` forms and `sandloom.sdk.postgres`.**
10. **Identity claims without `array_agg`/`LATERAL`** (existing task).

Slices 2–6 are independent once slice 1 lands.

## Questions for the owner

1. Is **PostgreSQL first, portable by contract** the position, rather than "every guarantee on every database"?
2. **Tenant isolation:** is an application-enforced mode, labelled as such, acceptable on databases without RLS, or must production always have database-enforced isolation (the strict profile stays PostgreSQL or SQL Server)?
3. **Which second database is the target first:** SQLite for development and tests, MySQL, or SQL Server?
4. **PostgreSQL-only features:** add `sandloom.sdk.postgres` as an opt-in namespace, or keep the ban and add only portable forms?
