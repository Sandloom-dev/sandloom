---
title: Upgrade to portable SQL and SQLite
description: Every change in the release that brings SQLite and portable SQL that can stop an application built on 0.7.0a10, the command that finds the affected code, and what to change.
type: how-to
job: When I upgrade an application past 0.7.0a10, I want to know which of my modules' code and settings the portable-SQL work breaks, how to find each place and what to write instead, so the upgrade passes its checks and tests on the first try.
purpose: The developer finds and fixes each breaking or changed behaviour of this release with one command per change, and can then run the application on SQLite if they want.
audience: [developer, agent]
level: building
---

# Upgrade to portable SQL and SQLite

In this release modules write SQL that every supported database runs alike.
An application can also run on a SQLite file for development and tests
([ADR-0011, amendment of 2026-11-05](../adr/0011-driver-api.md#amendment-2026-11-05-one-sql-driver-for-several-databases)).
PostgreSQL stays the production database, and its schema does not change.

This page lists each change that can stop an application built on
`0.7.0a10`. For each one: what changed, the command that finds your affected
code, and what to write instead.
The [changelog](../changelog.md) has the full release notes, and
[Upgrade an application](upgrade.md) the general procedure.

## Find what the upgrade touches

Update the framework and the standard module packages together, then run these
three commands from the application root:

```bash
uv run sandloom check
uv run sandloom migrate drift
uv run sandloom test
```

Each finding they report belongs to one section below. `migrate drift` needs
a PostgreSQL `DATABASE_URL`.

## Changes that `sandloom check` reports

### SQL dialect imports (SANDLOOM-C028)

Module code may no longer import `sqlalchemy.dialects` or pass dialect options
such as `postgresql_where=`. Migrations are exempt.

```bash
uv run sandloom check --only dialect-imports
```

| Before | After |
| --- | --- |
| `from sqlalchemy.dialects.postgresql import UUID` | `from sqlalchemy import Uuid` |
| `CITEXT` | `CaseInsensitiveText` from `sandloom.sdk.data` |
| an index with `postgresql_where=` | `partial_index(...)` |
| `pg_insert(Model)` | `conflict_insert(Model)` |
| `func.pg_advisory_xact_lock(key)` | `await transaction_lock(key)`, with a 64-bit integer key, and the `transaction_locks` feature declared (see [Locks need a declared feature](#locks-need-a-declared-feature-sandloom-c030)) |

On PostgreSQL each form writes the SQL your module wrote before. Run
`uv run sandloom migrate drift` after the change: it reports no difference.

### SQL that not every database runs (SANDLOOM-C031)

`sandloom check` now reads the SQL functions and the raw SQL of your modules
**and their tests**. It is an error.

```bash
uv run sandloom check --only portable-sql
```

| The finding | What to write instead |
| --- | --- |
| `func.now()`, `func.current_timestamp()` | `WallClock()` from `sandloom.sdk.data`: the database's clock at the statement, read back as an aware UTC time. Or bind `datetime.now(UTC)` |
| `func.array_agg(...)` | `aggregate_list(...)`, and declare the `list_aggregates` feature |
| `func.greatest(...)`, `func.least(...)` | a `case(...)` expression |
| `func.lower(...)`, `func.upper(...)` | a `CaseInsensitiveText` column for ASCII text, or compare in Python: SQLite folds only ASCII letters, in both |
| `func.avg(...)` | compute it in Python, or cast the result and waive the line with `# sandloom: allow portable-sql <reason>`: PostgreSQL answers a `Decimal`, SQLite a `float` |
| any other `func.<name>` outside `abs`, `char_length`, `coalesce`, `count`, `length`, `max`, `min`, `nullif`, `sum` | a form from `sandloom.sdk.data`, or a database feature form |
| `text(...)`, `literal_column(...)`, `.op(...)` and other raw SQL | SQLAlchemy expressions. A module test reads a kernel table through `sqlalchemy.table("audit_log", column(...))`. A line you reviewed carries `# sandloom: raw-sql <reason>` on the call or alone on the line above |
| a database URL literal such as `"postgresql://"` or `"sqlite+aiosqlite://"`, `create_mock_engine("postgresql+asyncpg://")` | the configured URL (`settings.database_url`), or compile against the test session's dialect (`db.bind.dialect`) |
| `x.dialect.name`, `dialect_name(...)`, `get_backend_name()` in a test | ask what the test needs: `skip_unless_db_feature(...)` (a race test whose writers must overlap: `skip_unless_db_feature("concurrent_writers")`) or `has_optimized_reads(engine)` from `sandloom.sdk.testing` |

A plain literal such as `server_default=text("0")` passes. A PostgreSQL cast
(`::`) in `text()` moved here from C028, and the marker does not admit it.

`WallClock()` is the time of the statement, not of the transaction:
PostgreSQL's `now()` returned the transaction's start. Where your code compared
two stamps written in one transaction, check that it does not need them equal.
[SQL functions and raw SQL](../modules/database/conventions.md#sql-functions-and-raw-sql)
has the details.

### Time columns are `UtcDateTime` (SANDLOOM-C029)

A `DateTime(timezone=True)` column in module code (also `DateTime(True)` and
`TIMESTAMP(timezone=True)`) is a finding.

```bash
uv run sandloom check --only utc-datetime-columns
```

Declare it `UtcDateTime()` from `sandloom.sdk`, a beginner name. On PostgreSQL it is the
same `timestamptz`, so no migration follows.

### Locks need a declared feature (SANDLOOM-C030)

A module that calls `transaction_lock` without declaring the
`transaction_locks` feature gets a warning in this release and an error in the
next minor release.

```bash
uv run sandloom check --only database-feature-forms
```

In the module's `define_module`, replace `"database"` in `capabilities=` with
`CapabilityRequirement("database", features=frozenset({"transaction_locks"}))`
(`sandloom.sdk.module`). The new forms (`Array`, `fulltext_index`,
`json_path`, `json_contains`, `aggregate_list`) are errors without their
feature from the start.

## Changes that tests and the drift step report

### Naive times are refused (`NaiveDateTimeError`)

`CreatedAt`, `UpdatedAt` and every `UtcDateTime()` column refuse a `datetime`
without a time zone, in a value and in a comparison: its instant is not known.
No check finds these binds; your tests raise `NaiveDateTimeError` where one
happens. To find candidates first:

```bash
grep -rnE "utcnow\(\)|datetime\.now\(\)|tzinfo=None" <your modules>
```

Bind aware times: `datetime.now(UTC)`, or `value.replace(tzinfo=UTC)` for a
value you know is UTC. `column - timedelta(...)` still works.

### Server defaults are compared (SANDLOOM-T006)

`sandloom migrate new`, `sandloom migrate drift` (the "migration drift" step of
`sandloom test`) and the migrations-match-models test now compare column server
defaults as well as types. A chain whose default differs from its model's fails
the step.

```bash
uv run sandloom migrate drift
```

For each `server default differs` it reports, either correct the model's
`server_default`, or add a revision with
`uv run sandloom migrate new <module> "<message>"` and review it. Then add the
one-line test the standard modules carry to each of your modules that has a
chain: `assert_migrations_match_models("<module>")` from
`sandloom.sdk.testing` ([Migrations match models](../reference/testing.md#migrations-match-models)).

### `json_path` reads text and refuses numbers

`json_path(column, *path)` reads the scalar's JSON text as stored: a number
stays as written (`1.50` reads `"1.50"`). It is compared with a `str` only.
A number on either side raises `TypeError` on every database, because
PostgreSQL refuses text against a number and SQLite would answer differently.
That covers these shapes:

- `json_path(...) == 1.5` and `json_path(...) > 2`;
- `Model.id == json_path(...)`;
- `coalesce(json_path(...), 0)`;
- a number bound to its parameter.

Your tests raise the error where it happens. Compare with the text as stored
(`== "1.50"`), or read the value and compare the number in Python. Do not cast
it in SQL: SQLite turns text it cannot read into `0`.

## Identity's claims helpers

`sandloom_identity.claims.identity_claims_select` and
`identity_claims_lateral` are removed: they joined `LATERAL` and called
`array_agg`, which SQLite does not have. Your tests or imports fail where you
used them. Select the two claim columns instead:

```python
roles, permissions = identity_claims_columns(User.id)
stmt = select(User.id, roles, permissions)
```

`identity_claims_columns` is in `sandloom_identity.claims`; its two
columns read the same lists on PostgreSQL and SQLite.

## Changes to `sandloom dev`

A SQLite `DATABASE_URL` in `.env` is now the checkout's own database, when it
is the file's last `DATABASE_URL` and stands outside the `sandloom dev` block.

| Command | Before | Now |
| --- | --- | --- |
| `sandloom dev run` | started PostgreSQL and Redis in Docker and served the app on them | serves the app on the file and starts no dev services |
| `sandloom dev up` | commented the line out | refuses and leaves `.env` as it is |

A PostgreSQL `DATABASE_URL` is handled as before. If your `.env` ends with a
SQLite `DATABASE_URL` and you use the dev services, remove or comment out that
line.

## Run your application on SQLite

This is optional. Add `sqlite` to the application's dev requirement on the
framework (`sandloom[check,testing,sqlite]`), then set the database in `.env`,
outside the `sandloom dev` block. Redis stays required:

```bash
DATABASE_URL=sqlite+aiosqlite:///./data/app.db
CACHE_URL=redis://127.0.0.1:6379/0
BROKER_URL=redis://127.0.0.1:6379/1
```

`uv run sandloom dev run` builds the schema from your models at startup;
`sandloom migrate` refuses a SQLite database (SANDLOOM-B028). The tests run on
SQLite with `SANDLOOM_TEST_DATABASE_URL=sqlite+aiosqlite:///./data/app_test.db`.
SQLite needs version 3.38 or newer (SANDLOOM-B030); check it with
`python -c "import sqlite3; print(sqlite3.sqlite_version)"`. A production
process refuses SQLite unless you accept the risk
`development_database_in_prod` (SANDLOOM-B029).

What SQLite changes for your code, nested units first, is on
[SQLite, for development and tests](../infrastructure/database.md#sqlite-for-development-and-tests).
