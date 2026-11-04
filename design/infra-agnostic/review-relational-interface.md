# Review: the relational interface for module authors

Status: **independent review, 2026-11-04**, read-only, branch `claude/modules-portable`. One claim was checked by running code (**[ran]**); the rest by reading (**[read]**) unless marked **[assumed]**. Input to the infra-agnostic design ([design.md](design.md)).

## Overall

The transactional core is better than most frameworks offer:
- a unit of work that owns commit, in the order before_commit → COMMIT → outbox → on_commit;
- savepoints that drop the effects queued inside them;
- task ownership of a session (R013) and one writing unit per request (R007);
- a fail-closed ORM tenant guard plus PostgreSQL RLS, with the roles checked at startup;
- one migration chain per module, drift detection (T006) and an extraction tool.

The gaps are in everyday modelling and querying. The dialect ban (C028) removed PostgreSQL features (JSONB, ARRAY, enums, full-text search) without a portable form for them. Two escape hatches, upsert and raw SQL, are tied to `unscoped()`, which turns off the tenant guard.

## By area

1. **Model declaration.**
   - Good: UUIDv7 `new_id`, `CreatedAt`/`UpdatedAt` on `clock_timestamp()`, the created_at-then-id order, `MoneyLedger`'s unique operation key, `relationship()` inside a module (billing uses `lazy="raise_on_sql"`), other modules' rows by plain UUID.
   - Gaps:
     - no `MetaData(naming_convention=...)` on `Base`;
     - no portable JSON(B), ARRAY, enum or money/Decimal guidance (`sa.JSON` is plain `json` on PostgreSQL);
     - no soft-delete or optimistic-locking (`version_id_col`) pattern;
     - `UpdatedAt` is not set by Core/bulk `update()`, which the docs leave ambiguous.
2. **Sessions and transactions.**
   - Good: `db()`, `open_uow()`, `begin_nested()`, `on_commit`/`before_commit`, the outbox, durable inbox and operation keys, operation intents.
   - Gaps:
     - no isolation-level choice for write units;
     - no retry on serialization failure or deadlock;
     - no batch helper;
     - the global `DB_STATEMENT_TIMEOUT_MS`, which a unit cannot override.
   - **Bug [read]:** `open_uow()` opens `"default"`, while `units.md` says "the module's primary database".
3. **Querying.**
   - Good: the read planner (keyset pages, HMAC cursors), `with_for_update` in the examples, `conflict_insert`, `transaction_lock`.
   - Gaps:
     - no cursor helper for plain `select()`;
     - no N+1 or async lazy-load guidance (`raiseload`, `selectinload`, `MissingGreenlet`);
     - streaming is undocumented;
     - no full-text search;
     - no typed mapping of IntegrityError.
4. **Multi-tenancy.**
   - Strong: the guard walks joins, subqueries, eager loads and `Session.get`; RLS has no bypass; the roles are verified; there is a system channel.
   - Gaps:
     - **[ran] `conflict_insert` on a `TenantScoped` model raises `UnsafeTenantOperationError`** even with a tenant bound;
     - C015 flags every `text()` outside `unscoped()`, even on global tables;
     - **[assumed]** module tests build schemas with `create_all`, so they never run under RLS.
5. **Migrations.**
   - Good: per-module chains, autogenerate with RLS, an advisory lock, drift detection T006, `migrate_module`, `extract-module`.
   - Gaps:
     - no zero-downtime guidance (expand/contract, `CONCURRENTLY`, batched backfills, `NOT VALID`);
     - **no `lock_timeout` on migration connections**;
     - no downgrade policy;
     - `migrations.md` calls T006 planned, while other pages say it is active.
6. **Multiple databases.** Named databases, write admission and `allow_multi_db_write`, an outbox per database. Read replicas, partitioning and sharding are absent and unstated.
7. **Testing.**
   - Good: a rolled-back `client`/`db`, `real_uow_client`, `db_uow`, ownership per run, `scratch_database`, `migrate_module`.
   - Missing:
     - factories or seeds;
     - a tenant-isolation test recipe;
     - an RLS-on test mode;
     - a migration round-trip recipe.
8. **Operations.**
   - Good: pool settings, a separate contract pool with `lock_timeout`, statement and idle timeouts.
   - Missing: a slow-query log and query metrics.
9. **Portability.**
   - The forms are well engineered: a registry by entry point, no silent fallback, the same PostgreSQL SQL.
   - Today the kernel still needs PostgreSQL:
     - `CreatedAt` emits `clock_timestamp()`;
     - `aiosqlite` is not installed;
     - the settings require `postgresql+asyncpg`;
     - module migrations are one database's history.
   - Developers lost JSONB/ARRAY/enum/FTS, and portability is still not reached. Either commit to "PostgreSQL first, portable later" and add portable forms for those features, or label PostgreSQL-only constructs instead of banning them.
10. **Ergonomics.**
    - Many read entry points (`db()`, `DbSession`, `ReadOnlyDbSession` "PROTOTYPE", `PlannedReads`, `SnapshotReads`, `UowReads`, `session_reads`, `Database`, `make_db_dependency`) with no decision table.
    - Public docstrings point to kernel paths.
    - Error codes and `sandloom explain` are excellent.

## Prioritized gaps

**P0**
1. Upsert on tenant tables is refused **[ran]**: let ORM inserts (including `conflict_insert`) through when every row's `tenant_id` is absent or equals the bound tenant, and refuse only mismatches (`core/tenant.py`).
2. C015 forces `unscoped()` for any raw SQL: raw SQL that names no tenant table passes, and a `tenant_sql()`/`scoped_text()` helper adds the bound tenant (`archcheck/analyzers.py`, `sdk.tenant`).
3. `open_uow()` defaults to `"default"`: resolve the calling module's database, or fix the docs and require the name (`sdk/_prim/data.py`).
4. Migrations without `lock_timeout`: set `lock_timeout`/`statement_timeout` on migration connections and add a zero-downtime page (`core/migrations/alembic_support.py`, `docs/modules/database/`).
5. The async lazy-load and N+1 trap: default relationships to `raise_on_sql` or add a check; document `selectinload`/`joinedload`.

**P1**
6. Portable `Json` (jsonb on PostgreSQL), `Enum` and `Array` forms; money as integer minor units or `Numeric`.
7. `naming_convention` on `Base.metadata`.
8. Retry on 40001/40P01 (`open_uow(retry_on_conflict=n)`, a route marker); typed `ConflictError(constraint=...)`.
9. Per-unit options `open_uow(isolation=, read_only=, statement_timeout=)` and a chunked-batch recipe.
10. A cursor helper for plain `select()`.
11. Testing: a tenant-isolation recipe, an RLS test mode, a migration round-trip, factories or seeds.
12. Docs: T006 status, the SQLite renderer claims, kernel paths in docstrings, a "which read API" table.

**P2**
13. Soft-delete and `version_id_col` recipes; full-text `search_index(kind="fulltext")`.
14. A slow-query log and metrics.
15. State the read-replica, partitioning and sharding position.
16. Document `UpdatedAt` on bulk updates; note the unprefixed `users` table as an exemption.
