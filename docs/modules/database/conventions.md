---
title: Module tables
description: Declare a module's table as a model on Base, with TenantScoped for per-tenant rows, a <module>_ name, a new_id key and a CreatedAt column, and order lists by created_at then id.
type: concept
job: When I add data to a module, I want to declare its table once in the shape Sandloom expects, so its migration, its tenant filter and a later move of the module need no extra code.
purpose: The reader declares each table as a model in the module's models/ package, picks TenantScoped or Base alone, names it <module>_*, and gives it a new_id key and a CreatedAt column.
audience: [developer, agent]
level: first-app
---

# Module tables

A module keeps its data in tables that only it writes. Each table must reach
the module's migration and stay apart from other modules' tables. When a row
belongs to one customer, other customers must never read it.

A table is a SQLAlchemy model class in the module's `models/` package. It
inherits `Base` from `sandloom.sdk`, with `TenantScoped` in front when each
row belongs to one tenant: one customer account or organization. Its name
starts with `<module>_`. `sandloom migrate new <module>` writes the revision
for every such table, and
[`sandloom migrate extract-module`](../../operate/extract-module-data.md)
moves them with the module. The id defaults to `new_id` and `created_at` is
`Mapped[CreatedAt]`, so rows list in creation order.

`sandloom add module board` writes this example table:

```python title="board/models/item.py"
--8<-- "docs/snippets/generated/module/demo/modules/board/models/item.py"
```

## Tenant tables and global tables

`TenantScoped` adds an indexed `tenant_id` column. Sandloom fills it on insert
and filters every read, update and delete by the tenant the request is bound
to. [Multi-tenancy](multi-tenancy.md) holds the rest of the rules, from
binding the tenant to row-level security.

A table on `Base` alone holds rows for the whole system. A profile keyed by
the user's id is one:

```python title="profiles/models/profile.py"
--8<-- "docs/snippets/customize/profile_model.py"
```

A `Base`-only model with a column such as `tenant_id` or `organization_id`
fails `sandloom check` with
[SANDLOOM-C014](../../reference/error-codes.md#sandloom-c014), unless it is a
listed exemption ([Multi-tenancy](multi-tenancy.md#opt-a-model-into-scoping)).

## Table names and the models package

The generated `models/__init__.py` imports every file of the package. A new
`models/<table>.py` joins the module's tables with no other registration.

The `<module>_` prefix is the module's claim on a table. Its `<module>` is
the last segment of the package name: `sandloom_billing` owns `billing_*`.
A module's `migrations/env.py` calls `run_module_migrations(__file__)`, and
autogenerate then compares only the tables with that prefix. A table named
outside it is left out of the revision `sandloom migrate new` writes.
`run_module_migrations(__file__, table_names=[...])` adds tables outside the
prefix.

A row of another module is referenced by its UUID, never by a foreign key.
The extraction refuses a foreign key that leaves the module, because the
two tables would end up in different databases.

## Timestamps, ids and list order

A list that pages or that a test asserts needs an order with no tie. Two
idioms give it; generated modules use both:

```python
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id
from sandloom.sdk.data import UpdatedAt  # sandloom: advanced last-write time


class Note(TenantScoped, Base):
    __tablename__ = "notes_notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]  # only on a row that changes


# newest first: the database clock, then the id
query = sa.select(Note).order_by(Note.created_at.desc(), Note.id.desc())
```

- **`CreatedAt` / `UpdatedAt`** are `timestamptz NOT NULL` columns the
  database stamps with its clock at the call, the time of the statement. That
  is `clock_timestamp()` on PostgreSQL and the UTC time to the millisecond on
  SQLite. `server_default=func.now()` is the trap: `now()` is the start of the
  transaction, so every row one transaction writes gets the same time. A
  request that inserts several rows would see them tie and come back in any
  order, and so would a test, since the `client` fixture runs a whole test in
  one transaction. On PostgreSQL `sandloom migrate new` renders
  `server_default=sa.text('clock_timestamp()')`.
- **They read back as timezone-aware UTC times** on every database. SQLite
  keeps no offset, so a column of your own that holds a time uses
  `UtcDateTime()` from `sandloom.sdk` rather than `DateTime(timezone=True)`
  ([SANDLOOM-C029](../../reference/error-codes.md#sandloom-c029)).
  It writes aware times in UTC and attaches UTC on load; on PostgreSQL it is
  the same `timestamptz`. Write aware times: a naive one is refused, since
  its instant is not known.
- **`new_id()`** returns a UUIDv7: its first 48 bits are the time in
  milliseconds and a counter orders the ids one process makes in the same
  millisecond, so ids sort in creation order and B-tree inserts stay at the
  right edge of the index. Keep `uuid.uuid4` for an id that must not reveal
  when it was made (a credential, an unguessable link).
- **Order by `created_at`, then `id`.** The first is one clock, the
  database's. The id breaks a tie of two rows stamped at the same time, as a
  multi-row `INSERT` or the same millisecond on SQLite gives. Ordering by the
  id alone gives the creation order of one process; across processes it
  follows their clocks.
- Recipe: `uv run sandloom docs recipe list-order`.

## Columns and statements on any database

The driver in configuration chooses the database, so a module never imports
`sqlalchemy.dialects`: that fails `sandloom check` with
[SANDLOOM-C028](../../reference/error-codes.md#sandloom-c028). Migrations are
exempt, since each one is the DDL history of one database. For what a dialect
used to give, use the portable form; on PostgreSQL each renders the same SQL.

| For | Use |
| --- | --- |
| a UUID column | `sqlalchemy.Uuid` |
| an email or another value compared without case | `CaseInsensitiveText` |
| an index over some rows, or unique among them | `partial_index(name, *columns, where=..., unique=...)` |
| substring search, `column.ilike("%term%")` | `search_index(name, *columns)` |
| insert unless the row exists, or insert or update | `conflict_insert(Model).on_conflict_do_nothing(...)`, `.on_conflict_do_update(...)` |
| one transaction at a time through a section | `await transaction_lock(key)` |

All but `Uuid` come from `sandloom.sdk.data`, and the two statements run on
`db()` unless given `session=`. The database driver writes each form in its
own SQL, and a database that cannot write one refuses instead of running
other SQL ([Infrastructure](../../infrastructure/database.md#portable-sql-forms)).
The check sees imports and dialect options such as `postgresql_where=`. SQL
functions and raw SQL have a check of their own, in the next section.

`aggregate_list` is an aggregate like `count`. Its items read back as the
column's type, in no set order. A group without rows reads `[]`. As a scalar
subquery it returns a list per row of the outer statement. A query can then
load a row and the codes it owns in one round trip. Declare the
`list_aggregates` feature for it, as for the forms below:

```python
codes = select(aggregate_list(Role.code)).where(Role.id == UserRole.role_id, UserRole.user_id == User.id)
user, role_codes = (await db().execute(select(User, codes.scalar_subquery()).where(User.id == uid))).one()
```

## SQL functions and raw SQL

SQLAlchemy writes `func.<name>` as it is spelled, so a function only one
database knows fails when the statement first runs on another. A module and
its tests call only the SQL functions that answer the same on every shipped
database. A test runs each of them on PostgreSQL and on SQLite, and checks
the value and its Python type:

`abs`, `char_length`, `coalesce`, `count`, `length`, `max`, `min`, `nullif`,
`sum`.

`max` and `min` of text order by the column's collation, which may differ
between databases.

Any other `func.<name>` fails `sandloom check` with
[SANDLOOM-C031](../../reference/error-codes.md#sandloom-c031), whether written
`func`, `sa.func`, an alias of `func` or a name from
`sqlalchemy.sql.functions`. The finding names the form to use:

| Instead of | Use |
| --- | --- |
| `func.now()`, `func.current_timestamp()` | `WallClock()` from `sandloom.sdk.data`, or a bound `datetime.now(UTC)` |
| `func.array_agg(...)` | `aggregate_list(...)` |
| `func.jsonb_...(...)` | `json_path(...)`, `json_contains(...)` |
| `func.to_tsvector(...)` | `fulltext_index(...)` and `matches(...)` |
| `func.greatest(a, b)` | `sqlalchemy.case((a >= b, a), else_=b)`, for non-NULL operands |
| `func.lower(c)`, `func.upper(c)` | a `CaseInsensitiveText` column, or fold in Python |
| `func.avg(c)` | `sqlalchemy.cast(func.avg(c), Float)`, on a waived line |

On SQLite, `lower` and `upper` fold only ASCII letters: `lower('ÄÖÉ')` stays
`'ÄÖÉ'`. SQLite's `NOCASE`, behind `CaseInsensitiveText`, has the same limit.
The average is a `Decimal` on PostgreSQL and a `float` on SQLite.

`func.now()` runs on both databases, yet answers differently. On PostgreSQL it
is the start of the transaction. On SQLite it is whole seconds, outside the
text that SQLite stores for a `UtcDateTime`. A comparison with such a column
is then off within a second. `WallClock()` is the database's clock when the
statement runs, read back as an aware UTC time on every database:

```python
from sandloom.sdk.data import WallClock  # sandloom: advanced the database clock
expired = update(Invitation).where(Invitation.expires_at <= WallClock())
await db().execute(expired.values(revoked_at=WallClock()))
```

Raw SQL reaches the database unread: `text()`, `literal_column()`, `DDL()`,
`exec_driver_sql()`, `.op()`, `.bool_op()`, and a string passed to
`.prefix_with()`, `.suffix_with()` or `.with_hint()`. Write the statement with
SQLAlchemy expressions instead. The check does not read the SQL strings of
`CheckConstraint(...)` and `Computed(...)`: keep those to what every database
accepts. A test reads a kernel table, such as
`audit_log`, through `sqlalchemy.table("audit_log", column("action", String))`,
since a module test does not import the kernel's models.

- **A reviewed line** carries `# sandloom: raw-sql <reason>` on the call, or
  alone on the line above it. A PostgreSQL cast (`::`) stays a finding: use
  `sqlalchemy.cast(expr, Type)`.
- **A literal** that is only a number, a quoted string, `NULL`, `TRUE` or
  `FALSE`, as in `server_default=text("0")`, is every database's and needs no
  marker.
- **A test tied to a feature** runs only where the feature is, so its SQL is
  not reported. That is a test or class decorated with
  `skip_unless_db_feature(...)`, or a test file whose `pytestmark` carries it.
- Migrations are exempt.

Code never asks which database runs it. Reading `x.dialect.name`,
`dialect_name(x)` or `url.get_backend_name()` is a finding too, in module code
and in tests. So is a database URL literal that names a backend, such as
`"sqlite+aiosqlite://..."`. Take the database from the configuration
(`settings.database_url`) or from the test's session (`db.bind.dialect`).
Ask for what the code needs instead:

- a module declares a database feature;
- a test is gated by `skip_unless_db_feature(...)`; a race test, whose
  writers must overlap, by `skip_unless_db_feature("concurrent_writers")`;
- a test asks `has_optimized_reads(engine)` (the planner's optimized executor
  can run), from `sandloom.sdk.testing`.

Compiling a statement for the bind's dialect,
`stmt.compile(dialect=db.bind.dialect)`, asks nothing. Only the raw-sql marker
admits a dialect read: a feature marker does not.

## Forms that need a database feature

Some forms need a feature that not every database has. PostgreSQL has every
one; SQLite has `json_path`, `transaction_locks` and `list_aggregates`.

| For | Use | Feature |
| --- | --- | --- |
| a column holding a list | `Array(String(40))`; find an item with `value == sqlalchemy.any_(Model.tags)` | `arrays` |
| full-text search over text columns | `fulltext_index(name, *columns, language="english")` in `__table_args__`, `matches([Model.title, Model.body], query)` | `fulltext_search` |
| a value inside a JSON column, as text | `json_path(Model.meta, "shipping", "country") == "FR"` | `json_path` |
| JSON that contains a document | `json_contains(Model.meta, {"tags": ["gift"]})` | `json_contains` |
| one transaction at a time through a section | `await transaction_lock(key)` | `transaction_locks` |
| the values of a group as one list, `array_agg` | `aggregate_list(Model.code)`, read as a Python list | `list_aggregates` |

A module that uses one declares the feature in its `define_module`, with
`CapabilityRequirement` from `sandloom.sdk.module`:

```python
capabilities=[CapabilityRequirement("database", features=frozenset({"arrays"}))]
```

- `sandloom check` finds a form without its declaration:
  [SANDLOOM-C030](../../reference/error-codes.md#sandloom-c030). For
  `transaction_lock` it is a warning in this release and an error in the next
  minor release.
- Startup refuses the module on an instance that lacks a feature it declares:
  [SANDLOOM-B033](../../reference/error-codes.md#sandloom-b033). A table
  built with an `Array` column or a `fulltext_index` counts as a declaration.
- A statement or table compiled for a database without the feature refuses:
  [SANDLOOM-R015](../../reference/error-codes.md#sandloom-r015).
- `matches` uses the index when it names the same columns in the same order,
  in the index's language. With another language the database scans every
  row. The query is what a user types, such as `cats -dogs` or `"a phrase"`.

What `json_path` reads, and how to compare it:

- It reads the scalar's JSON text as stored, the same on every database.
  Strings read as themselves (`"FR"`), booleans as `"true"` or `"false"`. Numbers read as written: `1.50` is `"1.50"`, `1e2` is `"1e2"`.
  Missing paths and JSON `null` read `None`. An index step (`0`) reads
  arrays only; on an object it reads `None`. Negative indexes are refused.
- Compare a `json_path` with a `str`: `json_path(Order.meta, "total") ==
  "1.50"`. A number raises `TypeError`, on either side and in `coalesce`:
  PostgreSQL refuses text against a number, and SQLite answers differently.
  `"1.50"` and `"1.5"` are different texts. Compare numbers in Python.
- Do not cast a `json_path` to a number in SQL. On SQLite `CAST('1e2' AS
  INTEGER)` is `1` and `CAST('abc' AS NUMERIC)` is `0`; PostgreSQL raises.
- Ordering a `json_path` (`<`, `ORDER BY`, `GROUP BY`, `DISTINCT`) uses the
  database's collation. SQLite sorts bytes; PostgreSQL usually sorts by
  language (`"a" < "B"`), even for ASCII. Use `.collate("C")` on PostgreSQL
  when byte order matters, or sort in Python.
- SQLite accepts `NaN` and `Infinity` in a JSON column (JSON5) and reads
  them as `None` and `"9e999"`. PostgreSQL refuses them on write.

## Related reading

- [What a module is](../index.md)
- [Write a migration](write-migration.md)
- [Multi-tenancy](multi-tenancy.md)
