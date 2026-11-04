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
  database stamps with `clock_timestamp()`, the time of the statement.
  `server_default=func.now()` is the trap: `now()` is the start of the
  transaction, so every row one transaction writes gets the same time. A
  request that inserts several rows, and a test (the `client` fixture runs a
  whole test in one transaction), would see them tie and come back in any
  order. `sandloom migrate new` renders `server_default=sa.text("clock_timestamp()")`.
- **`new_id()`** returns a UUIDv7: its first 48 bits are the time in
  milliseconds and a counter orders the ids one process makes in the same
  millisecond, so ids sort in creation order and B-tree inserts stay at the
  right edge of the index. Keep `uuid.uuid4` for an id that must not reveal
  when it was made (a credential, an unguessable link).
- **Order by `created_at`, then `id`.** `created_at` is one clock, the
  database's; `id` breaks the rare tie of two rows stamped in the same
  microsecond (a multi-row `INSERT`). `order_by(id)` alone is the creation
  order of one process; across processes it follows their clocks.
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
The check sees imports and dialect options such as `postgresql_where=`, not SQL
functions only one database has, such as `func.array_agg`.

## Related reading

- [What a module is](../index.md)
- [Write a migration](write-migration.md)
- [Multi-tenancy](multi-tenancy.md)
