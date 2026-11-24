---
title: A row owned through another tenant row is tenant data
impact: high
tags: tenancy, models, rls
---

A table whose rows belong to a tenant row (a note's comments, an order's lines) inherits `TenantScoped`, even with no `organization_id` column.

SANDLOOM-C014 sees only tables that look tenant-owned by their columns, so no check fails and every tenant can read these rows. After the change, `uv run sandloom migrate new <module> "<message>"` writes the table's row-level security policy.

## Incorrect

```python
class NoteComment(Base):  # reached through a Note, but visible to every tenant
    __tablename__ = "notes_comment"
    note_id: Mapped[UUID] = mapped_column(index=True)
```

## Correct

```python
class NoteComment(TenantScoped, Base):  # tenant_id, the filter and the RLS policy
    __tablename__ = "notes_comment"
    note_id: Mapped[UUID] = mapped_column(index=True)
```
