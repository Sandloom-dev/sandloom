---
title: Timestamps, ids and list order
description: Use new_id, CreatedAt and UpdatedAt, and order lists by created_at then id, so a page of results has no ties.
type: concept
---

# Timestamps, ids and list order

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

See also [What a module is](modules.md).
