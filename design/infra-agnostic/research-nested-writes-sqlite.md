# Research: a nested, independently committed write unit on SQLite

Status: **research, 2026-11-04**. Primary sources are cited inline. **[inference]** marks reasoning that no fetched source states. Not covered: Hibernate's own SQLite dialect, Axon and Eventuate on SQLite (neither ships SQLite support), Debezium (no SQLite connector). Input to [design.md](design.md) and to SANDLOOM-R016.

## Question

A request's unit holds a write transaction. An in-process call to another module's contract command, a saga step or a child `open_uow()` opens a second unit on a second pooled connection, and that unit must commit on its own: its outbox rows and its command-log claim are durable whether or not the caller later commits. On SQLite every writable unit begins `BEGIN IMMEDIATE`, so the second unit waits for the write lock its own caller holds, and the caller waits for the second unit. Today the driver refuses this at `BEGIN` (SANDLOOM-R016). The options on the table were:

- (a) run the inner unit on the caller's connection as a SAVEPOINT;
- (b) begin the outer unit DEFERRED until its first write;
- (c) keep the refusal.

How do mature frameworks handle this, and is there a better option?

## Short answer

- **No framework makes this work on one SQLite file.** SQLite allows one write transaction per database file, and no lock mode, pragma or shared cache changes that. The only engine-level way out is `BEGIN CONCURRENT`, and it is not in a released SQLite.
- **Frameworks do one of three things:**
  - **Join or savepoint.** Rails `requires_new`, Django nested `atomic`, Ecto, Drizzle and Spring `NESTED` all run the inner block on the outer connection. The inner work then commits only when the outer transaction commits.
  - **Refuse.** Django's `atomic(durable=True)` raises `RuntimeError` when it is nested. This is the same choice as R016.
  - **Avoid the shared file.** Rails 8 puts Solid Queue, Solid Cache and Action Cable in their own SQLite files by default. Solid Queue documents that this costs transactional integrity with the app's data.
- **Test harnesses collapse everything onto one connection.** Rails pins one connection for every thread. Ecto's sandbox has a shared mode. Django's `TestCase` wraps each test in a transaction and turns off its own `durable` check inside it. Each of them documents that commit semantics cannot be tested this way and points to a committing test case.
- **The better option found: one SQLite file per module, as option (d) below.** Sandloom already runs every contract call "in the provider's unit on the provider's database", and each named database has its own unit and its own outbox. With a file per module, a cross-module command commits on its own file while the caller holds the lock on its own file. That keeps the production semantics exactly, which is what the microservices profile does with PostgreSQL. Nested units within one module (a child `open_uow()` on the same database) still have no answer, and R016 stays for them.

## Findings by source

### SQLite

- **One writer per file.** In WAL mode, "since there is only one WAL file, there can only be one writer at a time" ([wal](https://www.sqlite.org/wal.html), §2.2). SQLite "will only allow one writer at any instant in time" ([when to use](https://www.sqlite.org/whentouse.html), §2). If many threads must write "at the same instant (and they cannot queue up and take turns)", use a client/server engine (checklist item 2).
- **Transaction modes** ([transactions](https://www.sqlite.org/lang_transaction.html), §2.1–2.2):
  - **DEFERRED:** the transaction "does not actually start until the database is first accessed". A later write upgrades it "if possible, or return[s] SQLITE_BUSY".
  - **IMMEDIATE:** starts the write at once and "might fail with SQLITE_BUSY if another write transaction is already active on another database connection".
  - **EXCLUSIVE:** the same as IMMEDIATE in WAL mode.
  - **Nesting:** "Transactions created using BEGIN...COMMIT do not nest. For nested transactions, use the SAVEPOINT and RELEASE commands." A BEGIN inside a transaction is an error.
- **Busy codes** ([result codes](https://www.sqlite.org/rescode.html)):
  - **SQLITE_BUSY:** a conflict "with a separate database connection". After a successful `BEGIN IMMEDIATE`, "no subsequent operations on the same database through the next COMMIT will return SQLITE_BUSY".
  - **SQLITE_BUSY_SNAPSHOT:** a WAL connection tried "to promote a read transaction into a write transaction but finds that another database connection has already written". This is the failure of option (b).
  - **SQLITE_LOCKED:** a conflict within the same connection, or with a connection that shares its cache.
- **busy_timeout cannot break this wait.** The busy handler is skipped only when SQLite itself sees a lock-upgrade deadlock between two connections ([busy_handler](https://www.sqlite.org/c3ref/busy_handler.html)). **[inference]** SQLite cannot see that connection B's waiter is the code that connection A's owner is waiting on, so it keeps calling the handler until the timeout. A longer timeout only makes the failure slower.
- **Connections in one process are isolated like connections in two processes.** A reader on another connection sees only committed work, "regardless of whether the two database connections are in the same thread, in different threads of the same process, or in different processes". There is "no isolation between operations that occur within the same database connection" ([isolation](https://www.sqlite.org/isolation.html)).
- **Savepoints** ([savepoint](https://www.sqlite.org/lang_savepoint.html)):
  - An outermost SAVEPOINT acts as BEGIN DEFERRED.
  - Releasing the outermost savepoint is COMMIT.
  - "The RELEASE of an inner transaction does not cause any changes to be written to the database file." A savepoint is never durable on its own.
- **Shared cache does not help.**
  - The docs call it "an obsolete feature. The use of shared-cache mode is discouraged." Even with it, "at most one connection to a single shared cache may open a write transaction at any one time".
  - `read_uncommitted` changes only read locks ([shared cache](https://www.sqlite.org/sharedcache.html)).
- **ATTACH.** A transaction over several attached files is atomic unless the journal mode is WAL. In WAL mode, transactions "continue to be atomic within each individual database file", but a crash can leave some files changed and others not ([attach](https://www.sqlite.org/lang_attach.html)). **[inference]** ATTACH does not create a second, independent transaction: one connection still has one transaction stack. Only separate connections to separate files commit independently.
- **BEGIN CONCURRENT** ([branch doc](https://www.sqlite.org/src/doc/begin-concurrent/doc/begin_concurrent.md)):
  - Locking "is deferred until a COMMIT is executed". Conflicts are detected per page, and a conflict returns SQLITE_BUSY_SNAPSHOT at COMMIT, so "all the client can do is ROLLBACK".
  - COMMIT itself is still serialised.
  - It lives on a separate branch that is periodically brought up to date with trunk ([branch check-in for 3.47.2](https://www.sqlite.org/src/info/d7299bfeb1eb031d)). No fetched source shows it in a trunk release, and Python's `sqlite3` links the system's release build. **[inference]** It would let an inner unit commit while the outer is open. It would also drop the "IMMEDIATE holds the lock" property that Sandloom's `transaction_lock` and claims rely on.
- **WAL2** keeps two WAL files so the log stops growing during long checkpoints. It describes one writer switching between the files and says nothing about more writers ([wal2](https://www.sqlite.org/cgi/src/doc/wal2/doc/wal2.md)).

### SQLAlchemy (pysqlite and aiosqlite)

- **The legacy `sqlite3` transaction mode breaks savepoints.** "a new SAVEPOINT emitted before a BEGIN will function on its own but fails to participate in the enclosing transaction". The fix is to emit BEGIN yourself ([base.py L141–195](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_0/lib/sqlalchemy/dialects/sqlite/base.py)). Sandloom's adapter already does this ([database.md](../../docs/infrastructure/database.md), "Implicit transactions are off").
- **Pools.** A file database under aiosqlite uses `AsyncAdaptedQueuePool` (since 2.0.38). `:memory:` uses `StaticPool`, which "forces all coroutines to share a single DBAPI connection… a ROLLBACK in one coroutine will also discard uncommitted work from any other coroutine" ([aiosqlite.py L57–101](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_0/lib/sqlalchemy/dialects/sqlite/aiosqlite.py)). This is the cost of sharing one connection, stated by the library itself.

### Django

- **Nesting.**
  - An inner `atomic` creates a savepoint. `savepoint=False` joins the outer transaction.
  - `durable=True` asserts the block is outermost and raises `RuntimeError` when nested ([transactions](https://docs.djangoproject.com/en/5.1/topics/db/transactions/)).
  - `on_commit` callbacks run after the outermost commit.
  - For a follow-up that must fail the main transaction, the docs point to two-phase commit, not to a second connection.
- **The durable check is turned off inside `TestCase`.** `Atomic.__enter__` raises only if the enclosing block is not `_from_testcase` ([transaction.py L182–206](https://github.com/django/django/blob/main/django/db/transaction.py)).
- **TestCase vs TransactionTestCase.** `TestCase` encloses each test in a transaction that is rolled back, so "some database behaviors cannot be tested". `TransactionTestCase` "may call commit and rollback and observe the effects". `captureOnCommitCallbacks()` emulates commit ([testing tools](https://docs.djangoproject.com/en/5.1/topics/testing/tools/)).
- **SQLite** ([databases](https://docs.djangoproject.com/en/5.1/ref/databases/)):
  - "database is locked" means "more concurrency than sqlite can handle in default configuration".
  - A larger `timeout` "won't really do anything to solve them".
  - Since 5.1, `OPTIONS["transaction_mode"]` sets DEFERRED (the default), IMMEDIATE or EXCLUSIVE ([sqlite3/base.py L144, L182–195](https://github.com/django/django/blob/main/django/db/backends/sqlite3/base.py)). With IMMEDIATE, "the usage of `ATOMIC_REQUESTS` is discouraged".

### Rails / Active Record

- **IMMEDIATE by default.**
  - `begin_db_transaction` issues `:immediate`. Deferred is used only for isolated or explicitly deferred transactions ([sqlite3/database_statements.rb L25–35](https://github.com/rails/rails/blob/main/activerecord/lib/active_record/connection_adapters/sqlite3/database_statements.rb)).
  - `default_transaction_mode: :immediate` is set on the raw connection ([sqlite3_adapter.rb L199](https://github.com/rails/rails/blob/main/activerecord/lib/active_record/connection_adapters/sqlite3_adapter.rb)).
  - Merged in [PR #50371](https://github.com/rails/rails/pull/50371), "Ensure SQLite transaction default to IMMEDIATE mode".
  - The default pragmas include WAL ([sqlite3_adapter.rb L149–156](https://github.com/rails/rails/blob/main/activerecord/lib/active_record/connection_adapters/sqlite3_adapter.rb)).
- **Busy handling.**
  - `timeout` maps to `busy_handler_timeout`, "a non-GVL-blocking, fair retry interval busy handler" ([8.0 CHANGELOG](https://github.com/rails/rails/blob/8-0-stable/activerecord/CHANGELOG.md), [PR #51958](https://github.com/rails/rails/pull/51958)).
  - 7.1 added a `retries` option ([7.1 CHANGELOG](https://github.com/rails/rails/blob/7-1-stable/activerecord/CHANGELOG.md)).
  - These make waiting for another writer cheaper. They do not resolve a wait on oneself.
- **Nesting.** A nested `transaction` joins the outer one. `requires_new: true` makes a savepoint ([abstract/database_statements.rb L430–480](https://github.com/rails/rails/blob/main/activerecord/lib/active_record/connection_adapters/abstract/database_statements.rb)). Rails has no in-thread "independent commit": that takes a second connection, which on SQLite waits for the lock.
- **Tests.** Transactional tests call `pool.pin_connection!(lock_threads)`, so "every checkout returns the same connection" ([test_fixtures.rb L197–225](https://github.com/rails/rails/blob/main/activerecord/lib/active_record/test_fixtures.rb), [connection_pool.rb L392–411](https://github.com/rails/rails/blob/main/activerecord/lib/active_record/connection_adapters/abstract/connection_pool.rb)). This is option (a), used in tests only.
- **Solid Queue, Cache and Cable on SQLite.**
  - The Rails 8 SQLite template gives `cache`, `queue` and `cable` their own files ([sqlite3.yml.tt](https://github.com/rails/rails/blob/main/railties/lib/rails/generators/rails/app/templates/config/databases/sqlite3.yml.tt)).
  - Solid Queue says that keeping jobs in the app's database "enables a powerful yet sharp tool". By default "Solid Queue is configured in a different database". `enqueue_after_transaction_commit` defers the enqueue until commit.
  - On SQLite `use_skip_locked` has no effect "as writes are sequential" ([README](https://github.com/rails/solid_queue/blob/main/README.md)).

### Ecto

- **Nesting.** A nested `transact/2` is "simply executed". On an inner error, "the whole outer transaction is aborted". The docs advise composing inside one transaction ([Ecto.Repo](https://ecto.hexdocs.pm/Ecto.Repo.html)).
- **SQLite adapter** ([Ecto.Adapters.SQLite3](https://ecto-sqlite3.hexdocs.pm/Ecto.Adapters.SQLite3.html)):
  - `default_transaction_mode` is `:deferred`; `:immediate` and `:exclusive` are available.
  - `busy_timeout` defaults to 2000 ms.
  - "does not support async tests when used with Ecto.Adapters.SQL.Sandbox", because only one write transaction runs at a time.
- **Sandbox.** It wraps each test's connection in a transaction. Shared mode lets "any process" use that one connection, and such tests cannot run concurrently ([SQL.Sandbox](https://ecto-sql.hexdocs.pm/Ecto.Adapters.SQL.Sandbox.html)).

### Laravel

- **Transaction mode.**
  - On PHP 8.4+, `SQLiteConnection` begins `BEGIN {transaction_mode} TRANSACTION` with `DEFERRED` as the default ([SQLiteConnection.php](https://github.com/laravel/framework/blob/12.x/src/Illuminate/Database/SQLiteConnection.php)).
  - The skeleton config exposes `busy_timeout`, `journal_mode`, `synchronous` and `transaction_mode` ([config/database.php L35–45](https://github.com/laravel/laravel/blob/12.x/config/database.php)).
- **Nesting.**
  - Nested `DB::transaction` creates a savepoint when the level is above 0 ([ManagesTransactions.php L148–170](https://github.com/laravel/framework/blob/12.x/src/Illuminate/Database/Concerns/ManagesTransactions.php)).
  - `attempts:` retries deadlocks ([database docs](https://laravel.com/docs/12.x/database)).
  - Nothing documents an independent commit on SQLite.

### Spring

- **REQUIRES_NEW and NESTED** ([propagation](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/tx-propagation.html)):
  - REQUIRES_NEW uses "an independent physical transaction" that "acquires its own resources such as a new database connection". The docs warn it may exhaust the pool and "potentially [cause] a deadlock" when threads wait for an inner connection.
  - NESTED is "a single physical transaction with multiple savepoints".
- **On SQLite.** No primary source covers REQUIRES_NEW on SQLite. **[inference]** The inner connection's write waits for the outer one's lock until the driver's busy timeout, the same self-wait as R016, with no early refusal.

### Prisma and Drizzle

- **Prisma.** A nested `db.transaction(...)` "is a separate transaction that commits or rolls back on its own, and the outer one cannot roll it back" ([transactions](https://www.prisma.io/docs/orm/prisma-client/queries/transactions)). The page says nothing about SQLite locking. **[inference]** On SQLite this is the R016 situation.
- **Drizzle.** Nesting uses savepoints. The SQLite transaction config takes `behavior: 'deferred' | 'immediate' | 'exclusive'`, with `deferred` as the default:
  - the config option: [sqlite-core/session.ts L207](https://github.com/drizzle-team/drizzle-orm/blob/main/drizzle-orm/src/sqlite-core/session.ts);
  - the default and the savepoints: [better-sqlite3/session.ts L81–100](https://github.com/drizzle-team/drizzle-orm/blob/main/drizzle-orm/src/better-sqlite3/session.ts).

### libSQL / Turso, LiteFS, Litestream

- **Turso.**
  - "with default configuration only allows one connection to write at a time".
  - With `PRAGMA journal_mode = 'mvcc'` and `BEGIN CONCURRENT`, "multiple connections can write simultaneously". If two touch the same rows, one gets a conflict and "must roll back and retry" ([concurrent writes](https://docs.turso.tech/tursodb/concurrent-writes)).
  - This is the Turso engine, not stock SQLite, and Python's `sqlite3` cannot use it.
- **LiteFS.** One primary node. Replicas get `SQLITE_READONLY`, and the application forwards writes to the primary ([primary](https://docs.fly.io/litefs/primary)). This forwards writes across nodes, not within a process.
- **Litestream.** WAL only. It takes "periodic but short write locks", so applications must set `busy_timeout` ([tips](https://litestream.io/tips/)).

### Outbox and saga frameworks

- **Temporal SQLite plugin.** With no `MaxConns` configured, the pool is held to one open connection, "for backward compatibility and safety". More connections need WAL ([plugin.go L92–120](https://github.com/temporalio/temporal/blob/main/common/persistence/sql/sqlplugin/sqlite/plugin.go)). This is the "one connection per process" pattern.
- **MassTransit.** The EF Core outbox supports SQLite through `UseSqlite()`, which sets a `SqliteLockStatementProvider` ([EntityFrameworkOutboxConfigurationExtensions.cs L153–176](https://github.com/MassTransit/MassTransit/blob/develop/src/Persistence/MassTransit.EntityFrameworkCoreIntegration/Configuration/EntityFrameworkOutboxConfigurationExtensions.cs)). It documents no single-writer behaviour.
- **Watermill SQLite.** "If you are writing within a transaction, create a connection for that transaction only". "If you must share a connection, protect it with a mutual exclusion lock" ([watermill sqlite](https://watermill.io/pubsubs/sqlite/)).
- **NServiceBus, Axon, Eventuate, Debezium.** None supports SQLite; see the stores listed in [research-reliability-mechanisms.md](research-reliability-mechanisms.md) §1.
- **Common finding.** None of these frameworks offers an inner unit that commits independently inside an outer SQLite transaction. They either share one connection or keep their tables in a different database.

## Options

| option | what it does | semantics kept | semantics lost | complexity | who does it |
|---|---|---|---|---|---|
| **(a) savepoint on the caller's connection** | the inner unit is `SAVEPOINT … RELEASE` inside the outer transaction | inner partial rollback; one connection; no wait | inner durability: the outer rollback undoes the inner effects, outbox rows and command claim; inner `on_commit` must wait for the outer commit; saga steps "committed" before a later failure vanish, while compensations still run and RPC steps do not roll back | medium: the unit, effect pipeline and on_commit must know they are nested | Rails `requires_new`, Django nested `atomic`, Laravel, Drizzle, Spring `NESTED`; test harnesses (Rails `pin_connection!`, Ecto shared sandbox, Django `TestCase`) |
| **(b) outer begins DEFERRED until its first write** | the outer takes the lock only when it writes | call-then-write flows | write-then-call is still a self-wait; a read-then-write upgrade fails with SQLITE_BUSY_SNAPSHOT, which `busy_timeout` cannot retry; IMMEDIATE-based `transaction_lock` and claims stop holding | low | Django, Ecto and Laravel defaults (DEFERRED); Rails moved away from it to IMMEDIATE |
| **(c) refuse (R016)** | the driver raises at the inner `BEGIN` | everything that runs | the call itself | done | Django `atomic(durable=True)` raises `RuntimeError` when nested |
| **(d) one SQLite file per module** (new) | each module's named database is its own file and engine; a contract command commits on the provider's file | every production semantic of a cross-module call, saga step or projection: the same shape as the microservices profile | nothing for cross-module calls; same-module nesting (child `open_uow()` on the same database) is still refused | low to medium **[inference]**: per-file migrations and core chain; startup wiring of module → file | Rails 8 Solid Queue/Cache/Cable in their own SQLite files; Sandloom's own "provider's database" rule |
| **(e) yield the lock when the outer has not written** (new, [inference]) | if the outer holds IMMEDIATE but has written nothing and taken no `transaction_lock`, end it, run the inner unit, then `BEGIN IMMEDIATE` again | READ COMMITTED for the outer across the call, as PostgreSQL's default gives | the outer snapshot and any lock its reads relied on; write-then-call is still refused | medium: track "written" and "lock taken" per unit | no framework found |
| **(f) run the inner unit after the outer commits** | `on_commit`, outbox or event instead of a contract command | durability of both | the synchronous answer and atomic decision; the caller cannot act on the result | low (exists) | Django `on_commit`, Rails `enqueue_after_transaction_commit`; R016's own fix text |
| **(g) one process-wide writer connection or write queue** | all units share one connection, or queue for it | no lock errors between independent tasks | **[inference]** a queue cannot order a cycle: the outer waits on the inner's result, so the inner can only run on the same connection, which is (a); SQLAlchemy warns that a ROLLBACK on a shared connection discards the others' work | medium | Temporal (MaxConns 1), Watermill (mutex), SQLAlchemy `StaticPool` |
| **(h) BEGIN CONCURRENT / MVCC** | optimistic writers, conflict at COMMIT | independent commits | not in released SQLite or Python's `sqlite3`; page-level or row-level conflicts need retries; IMMEDIATE-based locks lost | high (custom build) | SQLite branch, Turso engine |
| **(i) shared cache, longer busy_timeout, ATTACH** | — | — | do not help: one writer per cache; the wait is on oneself; one connection holds one transaction | — | discouraged by sqlite.org |
| **(j) PostgreSQL in tests** | tests that need nested commits run on PostgreSQL | everything | the light SQLite setup for that test | low | Django's advice ("switching to another database backend"), Sandloom's R016 fix text |

## Recommendation for Sandloom

1. **Keep (c) as the default.**
   - It fails at once with a code and a fix. That is what Django chose for the same contract (`durable=True`).
   - It is honest, because SQLite cannot give the semantics.
   - (b) does not fix write-then-call, and it trades R016 for SQLITE_BUSY_SNAPSHOT, which cannot be retried in place. Reject it.
2. **Add (d): on SQLite, a dev or test profile gives each module its own database file.**
   - Contract commands already run "in the provider's own unit on the provider's database". Each named database "has its own unit and its own outbox, and no transaction spans two of them" ([database.md](../../docs/infrastructure/database.md)). Modules share no foreign keys or SQL.
   - A file per module therefore changes no guarantee for cross-module calls, saga steps that call other modules, or projections. R016 then fires only for nesting within one module.
   - This is also the layout the microservices profile uses, so dev on SQLite tests the split shape.
   - Rails 8 made the same move for its framework tables on SQLite.
   - **To verify before building [inference]:**
     - modules can be bound to named databases by profile (`ModuleDescriptor.database_instances()` exists);
     - each file gets the core chain (outbox, audit, inbox, command log) and its module's migrations;
     - write admission accepts a request unit on file A while a contract unit commits on file B;
     - WAL per file. Atomicity across files is not needed, since no transaction spans two files.
3. **Do not ship (a) as a runtime mode. At most, allow it as a test-only fixture.**
   - That is how Rails, Ecto and Django use it.
   - If it ships, label it: *the nested unit is not durable on its own; an outer rollback undoes its writes, outbox rows and command-log claim; its `on_commit` runs after the outer commit; saga steps are not compensable as separate commits; a retry re-runs the command body*.
   - It must refuse an RPC (remote) provider, because a remote commit cannot join the savepoint.
4. **Consider (e) only after measuring.** Do HTTP idempotency claims or audit rows already make most outer units "written" before the handler runs? If so, (e) rarely applies. If it ships, label it: *reads before the call and reads after it see different snapshots (READ COMMITTED), as on PostgreSQL*.
5. **Keep labelling SQLite as a single-writer development database** in [database.md](../../docs/infrastructure/database.md), and name R016's fixes in order:
   - give the module its own file (d);
   - do the write in the caller's unit;
   - run it after commit (f);
   - use PostgreSQL (j).
