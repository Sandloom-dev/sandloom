# Research: how mature ORMs keep a database-agnostic core

Status: **research, 2026-11-04**, primary sources cited inline. **Inference** marks a reading the source does not state. Input to [design.md](design.md).

## Django

- **Boundary.** `DatabaseWrapper` with `features`, `ops` and a per-backend `SchemaEditor`. Migration logic stays in `django.db.migrations`; turning operations into SQL "is the job of the `SchemaEditor`" ([schema editor](https://docs.djangoproject.com/en/5.2/ref/schema-editor/)).
- **Capabilities.** `BaseDatabaseFeatures` flags with conservative defaults ([features.py](https://github.com/django/django/blob/main/django/db/backends/base/features.py)):
  - `has_select_for_update_skip_locked=False`;
  - `supports_update_conflicts=False`;
  - `can_return_rows_from_bulk_insert=False`;
  - `can_rollback_ddl=False`;
  - `minimum_database_version`.

  `supports_transactions` is probed at runtime.
- **Refusal and its exceptions.** Unsupported `nowait`/`skip_locked`/`of` "raises a `NotSupportedError`. This prevents code from unexpectedly blocking." But `select_for_update()` on SQLite "will have no effect" ([querysets](https://docs.djangoproject.com/en/5.2/ref/models/querysets/)).
- **Emulation.** SQLite ALTER by table rebuild, "slow and occasionally buggy" ([migrations](https://docs.djangoproject.com/en/5.2/topics/migrations/)).
- **Database-specific power** goes in the optional `django.contrib.postgres`: fields, indexes, constraints, `CreateExtension`, concurrent indexes, full-text search ([contrib.postgres](https://docs.djangoproject.com/en/5.2/ref/contrib/postgres/)).
- **Tests.**
  - `skipIfDBFeature`/`skipUnlessDBFeature` read the same flags ([testing tools](https://docs.djangoproject.com/en/5.2/topics/testing/tools/)).
  - Third-party backends declare `django_test_skips` and `django_test_expected_failures` with reasons.
  - **Inference:** Django's own suite then serves as their conformance suite.
- **Third-party backends.** "The Django versions and ORM features supported by these unofficial backends vary considerably" ([databases](https://docs.djangoproject.com/en/5.2/ref/databases/)). There is no separate backend API version.
- **Documented pitfalls:**
  - MySQL's case-insensitive collation and unique constraints;
  - Oracle's `''` = NULL;
  - non-transactional DDL on MySQL/Oracle;
  - `bulk_create(update_conflicts=…)` needs `unique_fields` on PostgreSQL/SQLite only, and on MySQL `ignore_conflicts` downgrades other errors to warnings.

## Rails Active Record

- **Boundary.** `AbstractAdapter`, with SQL produced by an Arel visitor per adapter ([abstract_adapter.rb](https://github.com/rails/rails/blob/main/activerecord/lib/active_record/connection_adapters/abstract_adapter.rb)).
- **Capabilities.** About 40 `supports_*?` predicates, nearly all `false` by default: `supports_advisory_locks?`, `supports_partial_index?`, `supports_insert_returning?`, `supports_insert_on_duplicate_update?`, `supports_insert_conflict_target?`, `supports_ddl_transactions?`.
- **Refusal up front.** `insert_all`/`upsert_all` raise `ArgumentError` ("does not support :unique_by") before running ([insert_all.rb](https://github.com/rails/rails/blob/main/activerecord/lib/active_record/insert_all.rb)). `unique_by` is documented as "(PostgreSQL and SQLite only)" ([Relation](https://api.rubyonrails.org/classes/ActiveRecord/Relation.html)).
- **Escapes and plug-ins.**
  - `disable_ddl_transaction!`;
  - `structure.sql` when `schema.rb` "cannot express everything" ([migrations guide](https://guides.rubyonrails.org/active_record_migrations.html));
  - `ConnectionAdapters.register(...)` for third-party adapters.

## Hibernate / JPA

- **Boundary.** One `Dialect` per product, aware of the server version, with `getMinimumSupportedVersion()`. It supplies `getLimitHandler()`, `getSequenceSupport()`, `getForUpdateSkipLockedString()`, and a function registry that is the portable function vocabulary ([Dialect](https://docs.hibernate.org/orm/6.6/javadocs/org/hibernate/dialect/Dialect.html)).
- **Tiers.** Core dialects are tested by the team. Community dialects are "maintained on a best-effort basis" with no CI; promotion needs a test server and a maintainer, and neglect means demotion ([dialect guide](https://docs.hibernate.org/orm/6.6/dialect/dialect.html)).
- **Tests.** `DialectFeatureChecks` (`SupportsSkipLocked`, …), the counterpart of `skipUnlessDBFeature` ([source](https://github.com/hibernate/hibernate-orm/blob/main/hibernate-testing/src/main/java/org/hibernate/testing/orm/junit/DialectFeatureChecks.java)).

## SQLAlchemy and Alembic

- **Boundary.** A `Dialect` with compilers and flags (`supports_native_uuid`, `supports_multivalues_insert`, …) ([internals](https://docs.sqlalchemy.org/en/20/core/internals.html)). `@compiles(Construct, "dialect")` overrides rendering per database ([compiler ext](https://docs.sqlalchemy.org/en/20/core/compiler.html)).
- **No portable upsert in Core.** `ON CONFLICT` lives in the PostgreSQL-specific `insert()` ([postgresql dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html)).
- **Conformance.**
  - A third-party dialect subclasses `SuiteRequirements` (`exclusions.open()`/`closed()`) and runs `from sqlalchemy.testing.suite import *` ([README.dialects](https://github.com/sqlalchemy/sqlalchemy/blob/main/README.dialects.rst)).
  - It registers in the `sqlalchemy.dialects` entry-point group.
  - Requirements are the "global database feature support policy".
- **Tiers.** "Supported version" vs "Best effort"; external dialects carry no guarantee ([dialects](https://docs.sqlalchemy.org/en/20/dialects/)).
- **Alembic.** `DefaultImpl` holds per-database variance (`transactional_ddl`) ([ddl api](https://alembic.sqlalchemy.org/en/latest/api/ddl.html)); batch mode emulates ALTER on SQLite ([batch](https://alembic.sqlalchemy.org/en/latest/batch.html)).

## Ecto

- **Boundary.** Behaviours split by capability: `Ecto.Adapter` is the minimum, `Queryable`, `Schema` and others are optional ([Ecto.Adapter](https://ecto.hexdocs.pm/Ecto.Adapter.html)). **Inference:** capability shows in the type system.
- **Migrations.**
  - Per-database options: `:concurrently`, `:include`, `:nulls_distinct`, `:after`.
  - Transactional DDL "for Postgres, but not true for MySQL".
  - The migration lock is an adapter strategy (`:pg_advisory_lock`) ([Ecto.Migration](https://ecto-sql.hexdocs.pm/Ecto.Migration.html)).
- **Tests.** One shared integration suite. Each adapter excludes tags with reasons, version-aware for PostgreSQL ([myxql helper](https://github.com/elixir-ecto/ecto_sql/blob/master/integration_test/myxql/test_helper.exs), [pg helper](https://github.com/elixir-ecto/ecto_sql/blob/master/integration_test/pg/test_helper.exs)).

## jOOQ, Prisma, Drizzle, Laravel

- **jOOQ.** `@Support({dialects})` on each API method. Emulation is first-class (`IS DISTINCT FROM` via CASE) ([dialects](https://www.jooq.org/doc/latest/manual/sql-building/dsl-context/sql-dialects/)).
- **Prisma.** A compiled `ConnectorCapability` enum (`NativeUpsert`, `InsertReturning`, `PartialIndex`, `DistinctOn`, …) checked by the schema validator ([capabilities.rs](https://github.com/prisma/prisma-engines/blob/main/psl/psl-core/src/datamodel_connector/capabilities.rs)), plus a published feature matrix ([database features](https://www.prisma.io/docs/orm/reference/database-features)).
- **Drizzle.** No common table object: "You need to choose a dialect" ([schema](https://orm.drizzle.team/docs/sql-schema-declaration)).
- **Laravel.** The base `Grammar` throws "This database engine does not support …" ([Grammar.php](https://github.com/laravel/framework/blob/12.x/src/Illuminate/Database/Query/Grammars/Grammar.php)). MySQL/MariaDB "ignore the second argument of the `upsert` method" ([queries](https://laravel.com/docs/12.x/queries)).

## The common pattern

1. **The core speaks in intents** and a per-database object renders them. The core never branches on vendor names.
2. **Capabilities are named, discoverable, false by default and version-aware.**
3. **The same capability vocabulary drives runtime refusal and test selection.** A shared suite with declared, reasoned exclusions is the conformance test.
4. **Database-specific power lives in a separate, opt-in namespace or package** (`contrib.postgres`, `dialects.postgresql`).
5. **Schema history is per database**, with a non-transactional-DDL escape per migration.
6. **Support tiers are explicit**: supported means in CI with a maintainer; community means best effort.
7. **Backends plug in by registry or entry point.** None versions the backend API separately.

| strategy | when used | cost |
|---|---|---|
| Emulate | the result is provably identical | hidden performance and locking cost |
| Refuse | a silent change would alter correctness | less portable, but loud and early |
| Flag + fallback | the caller can pick another algorithm | two paths to test |
| Silent no-op | anti-pattern (SQLite `select_for_update`, MySQL upsert target) | bugs that appear only under concurrency |

**Inference:** the documented pitfalls cluster around upsert conflict semantics, locking (FOR UPDATE, SKIP LOCKED, advisory locks), RETURNING, DDL transactionality, collation and NULL vs empty string. These are exactly what an outbox, an idempotency store and tenant isolation rest on.
