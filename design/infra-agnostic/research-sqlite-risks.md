# Research: best practices for the risks left by the SQLite work

Status: **research, 2026-11-05**. tm: `infra-risks-research`. Input to the slices after [spec.md](spec.md) slice 10.

**Reading guide.**
- Primary sources are cited inline: official docs, or source code by URL or file path.
- **[inference]** marks reasoning that no fetched source states.
- **[measured]** marks something run in this checkout for this note: SQLite 3.47.1, SQLAlchemy 2.1.5, aiosqlite 0.22.1. Each ran in an isolated environment: owner `portable-research`, a SQLite file under the session scratchpad, and Redis indexes 410–413.
- This note reuses [research-nested-writes-sqlite.md](research-nested-writes-sqlite.md) and does not repeat it.

**Owner constraints this note keeps:**
- no new architecture;
- drivers are equal;
- tenant isolation without RLS is allowed and labelled;
- SQLite is for development and tests;
- database-specific power comes as `sdk.data` forms;
- dialect knowledge lives in renderers and DBAPI knowledge in adapters;
- nested writes on SQLite are a SAVEPOINT of the caller (option a).

Where a recommendation touches one of these constraints, it says so and names the owner question.

## Summary

| # | risk | recommendation | cost | owner decision |
|---|---|---|---|---|
| 1 | SQLite-only nesting paths in `sql_alchemy/uow.py` | Move the nesting into its own driver module behind a two-implementation policy chosen once per engine, the way Django and SQLAlchemy keep the algorithm neutral and the backend facts separate. Add a stateful property test and a concurrency stress test. | M | no |
| 2 | Command log and saga crash replay only at the outermost commit | This is the standard: the dedupe record shares the transaction of the effect, as in an outbox or inbox. Keep G-CMD-01 equal by stating it in terms of "the transaction the effect commits in". Re-express the saga crash tests as laws over crash points. | S | **yes**: the ledger wording |
| 3 | Concurrency untestable on SQLite | Keep PostgreSQL as the mandatory lane. Mark race tests with feature markers. Fail CI when a test runs on no lane. Do not emulate PostgreSQL concurrency in the SQLite harness. Turn hangs into failures with `faulthandler_timeout`. | S | **yes**: CI policy |
| 4 | No static check for dialect-only SQL functions or raw SQL | New check rule over modules **and their tests**: an allowlist of portable `func.*` names, flags on `text()`, `literal_column()` and `.op()`, and a marked escape hatch. **It would have caught a live bug found here:** billing's `func.greatest`. | M | **yes**: strictness of `func.now()` |
| 5 | SQLite schema from `create_all`, PostgreSQL from migrations | Keep `create_all`, which is what Rails' `schema:load` does. Close the gap with a "migrations match models" gate on PostgreSQL, run per chain. Replaying the chains on SQLite in Alembic batch mode costs too much: 12 `op.execute`, CITEXT, CONCURRENTLY, `sha256`, trigram and RLS ops. | S | **yes**: confirm "no SQLite migrations" |
| 6 | `json_path` number parity only for int64, str, bool and null | On SQLite, read numbers with the `->` operator, which returns the number's JSON text as written. That equals PostgreSQL `json` `#>>`, and every portable JSON column is `json` on PostgreSQL. Full parity, no documented exception. | S | no |
| 7 | Unexplained hang of `test_sqlite_nested_units.py` | **Not a lock between runs. Reproduced alone:** 9 of 17 `-q` runs hung. The post-commit dispatcher's task swallows a cancellation in `flush()` at event-loop teardown, then waits on its queue forever. Fix in the dispatcher and the fixture. | S | no |

---

## 1. SQLite-only nesting paths inside the unit of work

### What exists

`core/sql_alchemy/uow.py` (348 lines) holds the following:
- `_serial_engine`;
- `_in_memory` and `_write_connection`;
- the test seam `pin_test_host` with a module-global `_PINNED` list;
- `savepoint_host`;
- `_NestingGate`;
- `_enter_nested` and `_close_nested`.

These sit beside the dialect-neutral `SQLAlchemyUnitOfWork`. The deferral of post-commit stages lives in `core/unit_of_work/__init__.py` (`defer_to_host`, `nested_host`, `_NESTED_NOTED_KEY`) and in `module_api/_call.py:342`.

The kernel asks a fact (`serial_writers`), never a dialect name, so the ratchet holds. The neutral path still pays a lookup: `savepoint_host` measured 199 ns in the 8c bench. And the two behaviours are interleaved in one class.

### How others structure it

- **Django** keeps one neutral algorithm and moves the backend differences into feature flags and connection hooks.
  - `Atomic.__enter__` decides between a savepoint and a new transaction only from `connection.in_atomic_block`, `savepoint` and `needs_rollback` ([transaction.py](https://github.com/django/django/blob/main/django/db/transaction.py), ~L201–240).
  - Whether a savepoint is real is the connection's business: `_savepoint_allowed()` is `features.uses_savepoints and not get_autocommit()`, and `savepoint_commit` "do[es] nothing if savepoints are not supported" ([backends/base/base.py](https://github.com/django/django/blob/main/django/db/backends/base/base.py), ~L416–472).
  - `on_commit` callbacks registered inside an atomic block carry the set of open savepoint ids, so a callback dies with its savepoint's rollback (same file, ~L792–812). That is Sandloom's "tagged with the caller's innermost savepoint" in `defer_to_host`.
- **SQLAlchemy** puts per-backend transaction behaviour on the dialect: `do_begin`, `do_savepoint` and `do_release_savepoint` (`engine/default.py` L1006). The `Session` chooses a join strategy from one parameter, `join_transaction_mode`.
  - Values: `"conservative_savepoint"`, `"create_savepoint"`, `"rollback_only"`, `"continue_savepoint"` (`orm/session.py` ~L1700–1750, [session_transaction docs](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites)).
  - The strategy is a value chosen at construction, not a branch spread over the session's methods.
- **Rails.** A nested `transaction` joins the outer one, `requires_new: true` makes a savepoint, and `joinable: false` makes inner calls create savepoints ([abstract/database_statements.rb](https://github.com/rails/rails/blob/main/activerecord/lib/active_record/connection_adapters/abstract/database_statements.rb) ~L430–480). Backend capabilities are adapter predicates (`supports_savepoints?`, `supports_advisory_locks?`, `supports_lazy_transactions?` in [abstract_adapter.rb](https://github.com/rails/rails/blob/main/activerecord/lib/active_record/connection_adapters/abstract_adapter.rb)). The transaction manager reads the predicates and does not branch on the adapter's name.
- **Ecto.** A nested `transact` is simply executed in the outer one ([Ecto.Repo](https://ecto.hexdocs.pm/Ecto.Repo.html)). There is no per-backend nesting behaviour at all: the choice was made once, for every adapter.
- **Common pattern.** Each of them keeps:
  1. a backend-neutral nesting algorithm;
  2. backend facts as declared flags;
  3. the strategy chosen **once**: at connection, session or adapter construction, not per call.

### Recommendation

Isolate the nesting policy as a module of the driver. This is not a new architecture; it is the 7b pattern of a seam with a plain path.

- **`core/sql_alchemy/nesting.py`** (new) holds today's nesting code from `uow.py` unchanged: `savepoint_host`, `_NestingGate`, `_write_connection`, `pin_test_host`, `_enter_nested` and `_close_nested` (as functions of a `JoinCallerAsSavepoint` policy).
- **A plain policy, `IndependentUnits`,** whose `host_for(factory)` returns `None` without a lookup.
- **The policy is chosen once per engine.** `_serial_engine` already caches the fact on the engine; the cache becomes the policy object. The PostgreSQL path then pays one attribute read instead of the context lookups.
- **`SQLAlchemyUnitOfWork`** keeps only `policy.enter(self)` and `policy.close(self, session)`.
- **`_PINNED` becomes a `ContextVar`** (or a per-loop list). It is module-global today, so a pinned host of one test is visible to another test of the same process: under xdist each worker is one process, so a leak is only an ordering hazard. **[inference]**
- **`defer_to_host` stays in `unit_of_work`.** It is the effect pipeline's (ADR-0005) and dialect-neutral.

**Files:**
- `src/sandloom/core/sql_alchemy/uow.py`;
- new `src/sandloom/core/sql_alchemy/nesting.py`;
- `.importlinter` (place the new child);
- `core/testing/database.py` (imports `pin_test_host` from the new place);
- `core/tests/test_sqlite_nested_units.py` (imports).

**Tests that prove it:**
- **A stateful property test** (Hypothesis `RuleBasedStateMachine`, already a dev dependency; [stateful testing](https://hypothesis.readthedocs.io/en/latest/stateful.html)).
  - Rules: open unit, write row, publish event, call contract command, open child unit, savepoint around call, raise inside, commit, rollback, concurrent task nesting.
  - The oracle is a pure Python model. On SQLite: rows, outbox rows, command claims and `on_commit` calls are visible only after the outermost COMMIT, and are gone after any enclosing rollback. On PostgreSQL: each unit commits on its own.
  - It runs the same machine on both databases with the two oracles. This is the test that would have caught the 8c review's interleaved-savepoint bug.
- **A stress test:** 50 tasks × random nesting depth × random sleeps on one pinned host. The `_NestingGate` invariant: no task's statement runs while another task's savepoint is open. Assert it through a `before_cursor_execute` listener that records the task per statement.
- **Bench.** Unit open and close on PostgreSQL before and after, with cachegrind as AGENTS requires. Expected: equal or lower.

**Cost:** M, about one day; no behaviour change.

**Owner decision:** no.

---

## 2. Command log and saga crash replay hold only at the outermost commit

### What exists

- **G-CMD-01:** "A command contract's dedupe record is claimed in the provider's own UoW and commits iff the effect commits, so a retry after a crash following COMMIT replays the stored result and never re-executes" (`guarantees.yml` L1111).
- **On SQLite** the provider's unit is a SAVEPOINT of the caller (G-UOW-09), so the claim and the effect commit together with the caller. Five saga tests fail there (spec, slice 8c): the "crash after the provider committed" replay, the nondeterministic-arguments conflict after such a crash, the TTL guard after such a crash, and two row-lock races.

### How others do it

- **The dedupe record shares the transaction of the effect: that is the pattern.**
  - The idempotent consumer inserts the message id "into the `PROCESSED_MESSAGE` table" inside the transaction that updates the business entities; a duplicate fails the insert and is rolled back ([microservices.io, idempotent consumer](https://microservices.io/patterns/communication-style/idempotent-consumer.html)).
  - NServiceBus's outbox saves outgoing messages and dedupe data "in a single transaction … which also includes changes to business data"; until it commits, the incoming message is not acknowledged and is delivered again ([NServiceBus outbox](https://docs.particular.net/nservicebus/outbox/)).
  - Django's `on_commit` and Rails' `enqueue_after_transaction_commit` defer effects to the **outermost** commit (prior research).
  - **Conclusion.** "Recorded in the transaction the effect commits in" is the standard. Whether that transaction is the provider's own (PostgreSQL today) or the caller's (SQLite) changes **when** the effect is durable, not whether record and effect agree.
- **Spring names the two shapes:**
  - `REQUIRES_NEW`: "an independent physical transaction";
  - `NESTED`: "a single physical transaction with multiple savepoints" ([propagation](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/tx-propagation.html)).
  - Sandloom on PostgreSQL is REQUIRES_NEW; on SQLite it is NESTED. Neither breaks "record iff effect".

### What actually changes on SQLite [inference, from the code]

- **G-CMD-01's sentence stays true.** The claim commits iff the effect commits; both commit at the outer COMMIT. A crash before it loses both, so a retry re-executes, which is correct: nothing was applied.
- **What is lost is the provider's commit point** independent of the caller.
  - A caller (a saga transition) can no longer observe "provider committed, my transition did not". On SQLite that state cannot exist, because the step and the transition commit atomically.
  - The saga tests that **construct** that state therefore cannot run. They do not show a violated law.
  - The nondeterministic-arguments conflict (G-SAGA-04) is not raised after a crash because nothing was recorded. Nothing was applied either, so re-running with new arguments applies the step once.
- **The two row-lock races** are concurrency tests (risk 3), not crash semantics.

### Recommendation

**Keep the guarantee equal** by restating it in terms that are true on both databases. Proposed ledger text:

- **G-CMD-01:** "A command contract's dedupe record is written in the same transaction as its effect and commits iff the effect commits: a retry after that transaction committed replays the stored result and never re-executes; a retry after it rolled back executes once. The transaction is the provider's own unit, or, on a database whose writers are serial, its caller's (G-UOW-09)."
- **G-SAGA-07:** "A crash before the step call, or after the step's effect committed, converges without re-applying the step (same key, command-log replay). On a database whose writers are serial a local step's effect commits with the transition, so only the first case can occur."

**Tests:**
- Replace the PostgreSQL-shaped crash tests by a **law over crash points**: for each crash point (before call, inside provider, after provider return, before outer COMMIT, after outer COMMIT), a redrive with the same key leaves exactly one applied effect and one record.
- `tests/laws/test_saga_laws.py` already has this shape. Run it on both lanes. The crash points that cannot occur on SQLite are generated only where `serial_writers` is false, through a feature check, not a skip by name.
- The 5 failing saga tests are re-marked: the 3 crash tests become laws, and the 2 races get `skip_unless_db_feature("skip_locked")` (risk 3).

**Files:** `guarantees.yml` (G-CMD-01, G-SAGA-07 statements and tests), `tests/laws/test_saga_laws.py`, `core/tests/test_sagas_protocol.py`, `docs/modules/between/contracts.md`, `docs/modules/between/sagas.md`.

**Cost:** S.

**Owner decision: yes.** The ledger is changed only through the owner. Question: *"May G-CMD-01 and G-SAGA-07 be restated as 'the dedupe record commits in the same transaction as the effect, which on serial-writer databases is the caller's', instead of carrying a SQLite exception?"*

---

## 3. Concurrency that SQLite cannot test

### What exists

- Race tests skip on SQLite through `skip_unless_db_feature("skip_locked")`: 4 in billing (spec, slice 8c).
- The harness's `db` session begins at its first write and emulates PostgreSQL's READ COMMITTED reads.
- A race barrier that "waited forever on serial writers" was bounded by hand.

### How others do it

- **Django.**
  - Tests are gated by declared features: `skipUnlessDBFeature` and `skipIfDBFeature` ([testing tools, skipping tests](https://docs.djangoproject.com/en/5.2/topics/testing/tools/)).
  - Backend-specific gaps are listed on the backend itself: `DatabaseFeatures.django_test_skips` and `django_test_expected_failures` ([unit tests docs](https://docs.djangoproject.com/en/dev/internals/contributing/writing-code/unit-tests/#using-another-settings-module)).
  - The default settings run on SQLite. The CI coverage job runs on PostgreSQL, and "code paths specific to other databases … will appear as 'not covered'… This is expected and acceptable" (same page, "Code coverage on pull requests").
  - Behaviour that needs real commits (`select_for_update`) needs `TransactionTestCase`: "some database behaviors cannot be tested within a Django `TestCase`" ([testing tools](https://docs.djangoproject.com/en/5.2/topics/testing/tools/)).
- **Rails.** Adapter predicates (`supports_*?`) gate behaviour. Rails' own suite branches with `current_adapter?(:PostgreSQLAdapter)` (`activerecord/test/cases/helper.rb`) and runs the suite once per adapter.
- **Ecto.** The SQLite adapter "does not support async tests when used with Ecto.Adapters.SQL.Sandbox" ([Ecto.Adapters.SQLite3](https://ecto-sqlite3.hexdocs.pm/Ecto.Adapters.SQLite3.html)). The limit is declared, not emulated.
- **Common pattern.**
  - One authoritative lane runs everything that the production database supports.
  - The other lanes skip by declared feature.
  - Nobody emulates the concurrency of another database in a test harness.

### Recommendation

1. **PostgreSQL stays the mandatory, authoritative lane.** `second-db-test-lane` is additive.
2. **Every race test declares its feature:** `skip_unless_db_feature("skip_locked")`, `"row_locks"`, `"transaction_locks"`, never a dialect name. C030's form-to-feature table already holds the vocabulary.
3. **A skip ledger in CI.** Each lane writes its skipped test ids (`--junitxml` or `-rs`). A job fails when a test id is skipped on **every** lane, because that test runs nowhere. This is Django's `django_test_skips` turned into a check. **[inference]**
4. **Do not make the SQLite harness emulate PostgreSQL concurrency.**
   - Its begin-at-first-write reads are right: they match what a single-connection test sees on PostgreSQL.
   - Concurrency on SQLite has its own small suite of serial-writer semantics: `test_sqlite_driver.py` already covers the waiting writer, `busy_timeout` → lock error, and R016.
5. **Hangs become failures.**
   - Add `faulthandler_timeout = 300` to `[tool.pytest.ini_options]`. It is pytest's built-in and dumps every thread's stack ([pytest docs, faulthandler](https://docs.pytest.org/en/stable/how-to/failures.html#fault-handler)).
   - Bound every barrier wait in the kit with `asyncio.timeout`.
   - Risk 7 below is the case that motivated this.

**Files:**
- `pyproject.toml`;
- `.github/workflows/ci.yml` (the skip-ledger job);
- a small `tools/ci/skip_ledger.py`;
- the race tests in `src/sandloom_billing/tests` and `core/tests/test_sagas_protocol.py`.

**Tests:** the ledger job itself; a deliberately unmarked race test fails it.

**Cost:** S.

**Owner decision: yes (CI policy).** Question: *"Is the PostgreSQL lane mandatory for merge, with SQLite as a second lane that may skip by feature, and a test skipped on every lane a CI failure?"*

---

## 4. No static check for dialect-only SQL in modules and their tests

### What exists

- **C028 (`dialect-imports`, `archcheck/analyzers.py` L720)** catches four things: `sqlalchemy.dialects` imports, `sa.dialects`, `postgresql_*=` options, and `::` inside a `text()` literal.
- **Its own docstring says what it misses:** "SQL functions only one database has (`func.array_agg`) are not visible to a static check".
- **`runner._skip` drops every file under `tests/`** (`archcheck/runner.py` L262–268), so module tests are not checked at all. Slice 8c fixed several portability bugs in tests by hand.

### A live bug found during this research [measured]

`src/sandloom_billing/services/intent_reconciler.py:395` writes `reconcile_attempts=func.greatest(...)` in `_escalate`.
- SQLAlchemy renders it as `greatest(?, ?)` on SQLite.
- SQLite 3.47.1 answers `no such function: greatest`.
- No billing test reaches `_escalate`: the "checkout completed after revocation" branch of `_retry_expire`. That is why the SQLite suite (300 passed) did not see it.
- `sdk.data` already has the portable form `greatest(*exprs, session=)` (`portable.py` L1258), which renders `max(...)` on SQLite.

### How others do it

- **SQLAlchemy.**
  - `func.<name>` renders any unknown name verbatim. A `GenericFunction` subclass gives a name a type and a per-dialect compilation through `@compiles(Fn, "sqlite")` (the `sqlalchemy.ext.compiler` docs).
  - **[measured]** The registry is no proof of portability. With the PostgreSQL dialect imported, `functions._registry['_default']` holds `array_agg`, `to_tsvector`, `websearch_to_tsquery`, `hstore` and more.
  - So "is a registered GenericFunction" cannot be the rule.
- **Django.**
  - Its portable functions are classes in `django.db.models.functions`, each with `as_<vendor>` methods. For example, `Greatest.as_sqlite` renders `MAX` ([functions/comparison.py](https://github.com/django/django/blob/main/django/db/models/functions/comparison.py)).
  - The compiler dispatches on `as_` + `connection.vendor` ([sql/compiler.py](https://github.com/django/django/blob/main/django/db/models/sql/compiler.py), `compile`).
  - Vendor-only functions live in `django.contrib.postgres`, so an import says "PostgreSQL only".
  - **Conclusion.** Portability comes from a curated set of functions, and the non-portable set has a different import path.
- **Linters.** No mature Python linter checks SQLAlchemy `func` names against a dialect. SQL linters such as sqlfluff check SQL text per dialect, not Python expressions. A project-specific AST rule is the norm here, as C028 already is. **[inference]**

### Recommendation: a new check rule, `portable-sql` (next free C code)

- **`func.<name>` and `sa.func.<name>`:**
  - The name must be in `PORTABLE_SQL_FUNCTIONS`, pure data beside `DATABASE_FORM_FEATURES` in `core/descriptors/`, so `sandloom check` stays env-free.
  - Seed: `count`, `sum`, `min`, `max`, `avg`, `coalesce`, `nullif`, `lower`, `upper`, `abs`, `length` (SQLAlchemy maps `char_length`), `random`, `cast`.
  - Each name is proven by a test that compiles and executes it on every shipped renderer, so the list cannot rot.
  - Any other name is a finding. The fix text names the `sdk.data` form when one exists: `greatest` → `greatest()`, `array_agg` → `aggregate_list()`, `jsonb_*` → `json_path` and `json_contains`.
- **`func.now()` and `func.current_timestamp()`:** a **warning first**.
  - PostgreSQL `now()` is the transaction's start time ([PostgreSQL 9.9.5](https://www.postgresql.org/docs/current/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT)).
  - SQLAlchemy renders SQLite's `CURRENT_TIMESTAMP`, which is `YYYY-MM-DD HH:MM:SS`: seconds, no fraction ([SQLite CREATE TABLE, default values](https://www.sqlite.org/lang_createtable.html#dfltval)). That breaks the one text format of slice 8a (`YYYY-MM-DD HH:MM:SS.ffffff`), and text comparisons with `UtcDateTime` values are then off within a second. **[measured]** `SELECT CURRENT_TIMESTAMP` → `'2026-11-05 03:11:13'`.
  - Nine uses in the standard modules: tenancy's invitation service, billing's operation intents, identity's 2FA.
  - The fix is a bound Python time or the `WallClock` form.
- **`text("...")`, `literal_column("...")`, `.op("...")` and `sa.DDL` with a string literal:** a finding in module code unless the line carries `# sandloom: raw-sql <reason>`. C028's `::` case folds into it.
  - The two `server_default=text("0")` in billing models are portable; a small allowlist of literal shapes (numeric and quoted-string constants) avoids noise.
- **Scope: modules and module tests.**
  - Tests are where 8c found the portability bugs (UUID binds in `text()`, `interval` arithmetic, raw JSON reads).
  - In tests, a raw-SQL line is accepted when the test or its class carries `skip_unless_db_feature(...)`. That is the escape hatch tied to a declared feature, not to a dialect name.
  - Migrations stay exempt: they are one database's DDL history (C028's reason).
- **Severity.** An error for unknown `func` names and unmarked raw SQL. A warning for `now()` for one minor release, as C030 did for `transaction_lock`.

**Files:**
- `src/sandloom/core/archcheck/analyzers.py` (new analyzer, C028's docstring updated);
- `archcheck/runner.py` (rule registered, test files included for this rule only);
- `core/descriptors/database_forms.py` or a sibling (`PORTABLE_SQL_FUNCTIONS`);
- `core/error_codes.py` (entry and explain text);
- `docs/modules/database/conventions.md`;
- `src/sandloom_billing/services/intent_reconciler.py` (use `greatest`);
- the nine `func.now()` sites.

**Tests:**
- analyzer fixtures under `tests/architecture/_fixtures/` for each shape: alias imports, `sa.func`, `from sqlalchemy import func as f`, a marked line, and a test file with a feature marker;
- a test executing every allowlisted name on PostgreSQL and SQLite;
- a billing test that reaches `_escalate`, run on both lanes. Without the fix it fails on SQLite.

**Cost:** M.

**Owner decision: yes, narrow.** Question: *"Is `func.now()` in module code an error (use a bound time or `WallClock`) after one release of warnings, given that PostgreSQL's transaction-start semantics differ from SQLite's second-precision `CURRENT_TIMESTAMP`?"*

The rest is a check in the existing rule family and needs no decision.

---

## 5. SQLite schema from the models, PostgreSQL from migrations

### What exists

- **SQLite:** `ensure_model_schema` runs `create_all`, and startup refuses drift (B028 drift check, slice 8b).
- **PostgreSQL:** the Alembic chains, one per module plus the core chain. `sandloom migrate` refuses SQLite.
- **No test proves that the migrated PostgreSQL schema equals the models.** `compare_metadata` is used in only one test (`test_wall_clock.py`), for defaults. Billing checks only that its `env.py` lists every table.

### How others do it

- **Rails.** "Migrations, mighty as they may be, are not the authoritative source for your database schema." New databases are created with `bin/rails db:schema:load`, which is "faster and less error prone" than replaying history. `structure.sql` is for what `schema.rb` "cannot express … such as triggers, sequences, stored procedures" ([Rails guides, schema dumping](https://guides.rubyonrails.org/active_record_migrations.html#schema-dumping-and-you)).
  - This is Sandloom's `create_all` for SQLite: the schema from a declaration, not from the history.
- **Alembic batch mode.**
  - On SQLite it does "move and copy": reflect, create a new table, `INSERT … SELECT`, drop, rename.
  - `render_as_batch=True` makes autogenerate emit it.
  - Unnamed constraints need a naming convention.
  - "Batch table operations **do not work** with foreign keys that enforce referential integrity", so `PRAGMA foreign_keys` must be off ([Alembic batch](https://alembic.sqlalchemy.org/en/latest/batch.html)).
  - `op.execute` with raw SQL is passed through unchanged. **[inference]** The page does not translate it.
- **Django on SQLite** remakes the table for most alterations: create `new__app_model`, copy, drop, rename, restore indexes. It refuses to run while foreign-key checks are on ([sqlite3/schema.py](https://github.com/django/django/blob/main/django/db/backends/sqlite3/schema.py), `_remake_table`). Django can do this because its migrations are declarative operations, not SQL.
- **The drift gate others use:** `alembic check` (Alembic 1.9+) fails when autogenerate finds pending operations ([Alembic commands](https://alembic.sqlalchemy.org/en/latest/api/commands.html#alembic.command.check)). Django has `makemigrations --check`.

### Cost of running the real chains on SQLite [measured, by inspection]

- **Inventory:**
  - 26 module revisions: billing 11, identity 9, tenancy 6;
  - 13 core revisions.
- **Operations with no SQLite meaning, or that need rewriting:**
  - 12 `op.execute`, including `CREATE EXTENSION citext` and `pg_trgm` (identity 0001, 0006; tenancy 0001);
  - `DROP INDEX CONCURRENTLY` (identity 0006, 0007);
  - a data migration using `encode(sha256(convert_to(token, 'UTF8')), 'hex')` (tenancy 0002);
  - `UPDATE … SET … = now()`;
  - 3 `postgresql.CITEXT`, 6 `postgresql.UUID` (core and modules), 1 `postgresql.ARRAY`;
  - 3 `gin_trgm_ops` indexes, 6 `postgresql_where` partial indexes, 9 `clock_timestamp` defaults;
  - `enable_tenant_rls` (core 2×).
- **What batch mode adds:**
  - 14 `alter_column` and 18 `drop_column` that need batch blocks;
  - 4 `drop_constraint` that need names under a naming convention.
- **Every one would need a dialect guard or a renderer-written replacement** inside historical revisions. Revisions are frozen history: changing them risks the PostgreSQL path they exist for.
- **RLS ops on SQLite:** a no-op guard is easy, but meaningless, because SQLite has no row security (B026).
- **Estimate:** L, with a regression risk on PostgreSQL. The only gain is "migrations tested on SQLite", which is not a product goal (owner decision 4: SQLite is for dev and tests).

### Recommendation

1. **Keep `create_all` for SQLite.** This is the Rails `schema:load` practice, and spec §6 already says it.
2. **Close the drift chain on PostgreSQL** with a "migrations match models" gate per chain:
   - upgrade a scratch database to head;
   - run `compare_metadata` against the module's tables (the `env.py` `table_names` scope);
   - fail on any operation, ignoring only what the chain owns and the models cannot express (RLS policies, extensions).
   - With the B028 drift check on SQLite this gives transitivity: chain = models on PostgreSQL, and models = SQLite schema by construction.
   - **[inference]** A module whose chain forgets a column then fails on PostgreSQL, before anyone sees a different SQLite.
3. **Ship it as a `sandloom.sdk.testing` helper** (`assert_migrations_match_models(module)`), so application modules get it too, and run it in each standard module's suite. This is a test helper, not a new mechanism.

**Files:**
- `src/sandloom/core/testing/migrations.py` (the helper);
- `sdk/testing` export and the baseline;
- one test per standard module;
- `docs/reference/testing.md`.

**Tests:**
- the helper fails on a fixture chain missing a column and on an extra index;
- it passes on the four standard modules and the core chain.

**Cost:** S. A real finding is possible: a chain that disagrees with its models today.

**Owner decision: yes, a confirmation.** Question: *"Confirm that SQLite never runs the migration chains (Rails' `schema:load` model), and that 'migrations match models' on PostgreSQL is the gate that keeps the two schemas equal."*

---

## 6. `json_path` number parity

### What exists

The SQLite renderer answers `CAST(json_extract(col, path) AS TEXT)`, with booleans restated from `json_type` (`sql_dialects.py` ~L760). Numbers come back as SQLite REAL text, so the contract was narrowed to strings, booleans, null and 64-bit integers.

### Findings

- **PostgreSQL.**
  - `json` "stores an exact copy of the input text"; `jsonb` prints numbers "according to the behavior of the underlying `numeric` type": `1.230e-5` becomes `0.00001230`, trailing fractional zeros kept ([datatype-json](https://www.postgresql.org/docs/current/datatype-json.html#JSON-KEYS-ELEMENTS)).
  - `->>` and `#>>` return `text` ([functions-json, Table 9.47](https://www.postgresql.org/docs/current/functions-json.html#FUNCTIONS-JSON-OP-TABLE)).
- **SQLite.**
  - `json_extract` returns "INTEGER or REAL for a JSON numeric value", and `->>` returns "an SQL TEXT, INTEGER, REAL, or NULL value" ([json1](https://sqlite.org/json1.html#jex)).
  - `->` returns the JSON text of the element ([json1, `->`](https://sqlite.org/json1.html#jptr)).
- **Measured: SQLite `->` keeps the number as written.**

| JSON number | SQLite `CAST(json_extract)` (today) | SQLite `col -> path` | PostgreSQL `json #>>` |
|---|---|---|---|
| `1.50` | `1.5` | `1.50` | `1.50` (verbatim) |
| `1e2` | `100.0` | `1e2` | `1e2` |
| `1.230e-5` | `1.23e-05` | `1.230e-5` | `1.230e-5` |
| `-0.0` | `0.0` | `-0.0` | `-0.0` |
| `12345678901234567890` | `1.23456789012346e+19` | `12345678901234567890` | verbatim |

- **Every JSON column in the portable surface is `json` on PostgreSQL.**
  - The kernel uses `sqlalchemy.JSON` (`models/outbox.py` L79).
  - C028 forbids a module from declaring `postgresql.JSONB`.
  - So PostgreSQL returns the verbatim text, and SQLite's `->` returns the same text.
- **How others do it.**
  - SQLAlchemy does not promise text parity. It offers typed accessors, `as_integer()`, `as_float()` and `as_numeric(precision, scale)`, which "usually [do] not apply a CAST" (`sql/sqltypes.py` `JSON.Comparator`). SQLite renders `JSON_EXTRACT`, PostgreSQL `->>`/`#>>` (dialect `visit_json_getitem_op_binary`).
  - Django returns SQLite's native non-string values unchanged ("Some backends (SQLite at least) extract non-string values in their SQL datatypes", [fields/json.py](https://github.com/django/django/blob/main/django/db/models/fields/json.py) `from_db_value`). It `json.loads` the right-hand side of numeric lookups on backends without native JSON (`KeyTransformNumericLookupMixin`).
  - Neither gives text parity; both push numbers to typed comparison.

### Recommendation

- **The SQLite renderer reads numbers as JSON text.** Change the `case`:
  - `json_type` `'true'`/`'false'` → the words (as today);
  - `'integer'`/`'real'` → `column -> path`;
  - else `CAST(json_extract(column, path) AS TEXT)`.
- **The contract becomes "the scalar's JSON text as stored; strings unquoted"** for every scalar on both databases, with no number exception.
- **`->` needs SQLite 3.38,** already the renderer's minimum (slice 9).
- **A module that compares numbers** should compare in Python, or cast both sides explicitly. The docs keep that advice for computed comparisons. SQLite compares REAL beyond 2^53, PostgreSQL compares `numeric`.
- **Scope note.** If a future driver declares a `jsonb`-style column type, its renderer must state the number text form in its conformance answers. The kit's table of paths catches it.

**Files:**
- `src/sandloom/core/drivers/sql_dialects.py` (SQLite `json_path`). **Edited by another agent right now: sequence after that work.**
- `core/tests/test_database_feature_forms.py`: the number rows move from "pinned per database" to "same on both";
- `docs/infrastructure/database.md` and the `json_path` docstring.

**Tests:** the existing table of paths, extended with the five rows above, asserts equal text on PostgreSQL and a SQLite file. It fails with the old `CAST`.

**Cost:** S.

**Owner decision:** no. It widens the contract and weakens nothing.

---

## 7. The unexplained hang of `test_sqlite_nested_units.py`

### Question

Does a concurrent run of the same file against the same owner or database block, for example on an advisory lock or an owner-file wait with no timeout?

### Findings from the code

- **Ownership (`core/testing/ownership.py`) has no waits.**
  - SQLite claims read and write a `<file>.owner` text file.
  - PostgreSQL claims connect with `connect_timeout=5` and run `COMMENT ON DATABASE` and `ALTER DATABASE … SET`, both short autocommit statements.
  - Redis claims use 5 s socket timeouts.
  - The same owner is never refused.
- **Slots (`core/testing/slots.py`)** use `pg_try_advisory_lock`, which never waits. Explicit `SANDLOOM_TEST_*` overrides skip slots.
- **The file's own fixtures:**
  - `core` builds a SQLite file under `tmp_path`, unique per test;
  - `pg_core` creates and drops a uniquely named table;
  - `test_saga_steps_run_on_the_sqlite_test_harness` runs a child pytest with `timeout=300`.
  - Nothing is shared between two runs except Redis indexes and the PostgreSQL database named in `.env.test`.
- **Waits without a timeout do exist elsewhere in the harness.** They are not this file's:
  - `workers.ensure_database` calls `psycopg2.connect` without `connect_timeout` (`workers.py` L345, L360);
  - `schema_reset`'s PostgreSQL `TRUNCATE`, `DROP` and `CREATE EXTENSION` run with no `lock_timeout`, so they wait for another run's open transaction on the same database.

### Reproduced alone [measured]

- **Setup:** no other run, my own SQLite file and Redis indexes.
- **`-q` runs:** `pytest src/sandloom/core/tests/test_sqlite_nested_units.py -q -m "not subprocess_probe"` hung in **9 of 17** runs. The single test `test_a_contract_command_in_a_unit_commits_with_the_outer` alone hung in 3 of 4.
  - Each hung run was killed by my watchdog after 20–120 s.
  - Each printed only the session header, because a piped pytest's stdout is block-buffered. `| head -15` therefore showed nothing at all.
- **`-v` runs:** passed 3 of 3 in 3–6 s. **[inference]** Output timing shifts the race; `-v` is not a fix.
- **The stack** (`faulthandler_timeout=15`): the main thread is in pytest-asyncio's runner `close()` → `asyncio.runners._cancel_all_tasks` → `run_until_complete(gather(...))`, at the **teardown** of `test_a_contract_command_in_a_unit_commits_with_the_outer`.
- **The task it waits for**, dumped from a timer thread:
  `<Task cancelling name='Task-5' coro=<PostCommitDispatcher._run() running at core/unit_of_work/dispatch.py:359> wait_for=<Future pending>>`, where line 359 is `job, size = await self._queue.get()`.
- **Instrumented `PostCommitDispatcher.flush`:**
  - `enter flush cancelling=0`, then teardown, then **`exit flush ok cancelling=1`**.
  - The cancellation reached the task inside `flush()`, so inside `store.mark_delivered` on the SQLite AUTOCOMMIT view (`outbox_store/__init__.py` L208–218, `view.connect()` / `conn.execute`).
  - Something below it swallowed the `CancelledError`, and `flush` returned normally.
  - `_run` then went back to `await self._queue.get()` with the cancel already spent, and the loop's shutdown waits for it forever.
  - In the one run of four that passed, the probe logged `exit flush CancelledError cancelling=1`: the cancellation escaped and the task ended. Pass or hang depends only on where the cancel lands inside the mark.
- **Why this test:** it is the first in file order that publishes an event, so the dispatcher's task is still marking rows delivered when the `core` fixture disposes the engine (`provider.aclose()`) and the loop closes. The fixture does not `drain_post_commit()` first. The kit's own client fixture does (`core/testing/fixtures.py` L548).

**Answer:** a concurrent run of the same file does not block this one. The 7-minute run was this intermittent teardown hang, made invisible by `| head`. The parallel run in the other shell was a coincidence.

### Recommendation

1. **Dispatcher.** `PostCommitDispatcher._run` must not outlive a cancellation it did not see.
   - After each `await self.flush()` and each round, exit when `asyncio.current_task().cancelling()` is non-zero (re-raise `CancelledError`).
   - `flush()` must not run its marks under a swallowing call: find the swallow site, the aiosqlite AUTOCOMMIT path, with the probe described above, and fix it there. If it is in SQLAlchemy or aiosqlite, report it upstream and guard locally.
   - **[inference]** The same shape can strand a worker loop at shutdown on PostgreSQL if any mark path swallows a cancel. It is not reproduced there.
2. **The test's `core` and `pg_core` fixtures** call `await drain_post_commit(timeout=5)` before `provider.aclose()`, as the kit's fixtures do.
3. **`faulthandler_timeout`** in `pyproject.toml` (risk 3), so the next hang prints its stack instead of a silent pipe.
4. **Harness waits.** Add `connect_timeout=5` to `workers.ensure_database`, and `SET lock_timeout` (for example 30 s) before the PostgreSQL reset statements in `schema_reset`. A run that shares a database by mistake then fails with a lock error instead of waiting.

**Files:**
- `src/sandloom/core/unit_of_work/dispatch.py`;
- possibly `src/sandloom/core/sql_alchemy/outbox_store/__init__.py`;
- `src/sandloom/core/tests/test_sqlite_nested_units.py` (fixtures);
- `src/sandloom/core/testing/workers.py`;
- `src/sandloom/core/drivers/schema_reset.py`;
- `pyproject.toml`.

**Tests:**
- `test_post_commit_dispatcher.py` gets a test whose fake `mark_delivered` swallows a cancel. Closing the loop must finish within 1 s (it hangs today).
- 20 repeated `-q` runs of `test_sqlite_nested_units.py` under a watchdog, all finishing. Today about half hang.

**Cost:** S.

**Owner decision:** no.

---

## Recommended order of fix slices

1. **Hang and the billing bug.** Risk 7's dispatcher and fixture fixes, `faulthandler_timeout`, and the harness timeouts; then billing `_escalate` → `greatest()` with a test reaching it on both lanes.
   - The hang makes every later slice's test runs unreliable.
   - The bug is a production defect on SQLite today.
   - S.
2. **`portable-sql` check (risk 4)** over modules and module tests, with the allowlist test.
   - It finds the next `greatest` before a suite does.
   - It depends on 1 only for the billing fix.
   - The `func.now()` severity waits for the owner.
   - M.
3. **`json_path` numbers by `->` (risk 6).**
   - After the other agent's `sql_dialects.py` work lands.
   - S.
4. **"Migrations match models" gate (risk 5),** after the owner confirms. S.
5. **Lanes and skip ledger (risk 3),** after the owner decides the CI policy. Re-mark the race tests by feature. S.
6. **Nesting policy module and stateful tests (risk 1).**
   - Behaviour-neutral, measured.
   - Best done after 1, so the stress tests run on a dispatcher that cannot hang.
   - M.
7. **Guarantee wording and crash-point laws (risk 2),** after the owner approves the ledger text. It can run beside 6. S.
