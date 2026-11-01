---
title: Add an event
description: Declare a typed event a module publishes with `sandloom add event`, publish it in a unit of work, and test it.
type: how-to
---

# Add an event

**Goal:** a new typed event that your module publishes, such as
`notes.note.archived.v1`, so other modules can react to it.

**Use it** when something happened in your module that other modules may
care about, and the producer does not need their answer.

**Do not use it** when you need an answer now (use a
[contract](add-contract.md)), or to react to another module's event (that
is a [consumer](add-consumer.md): a *view* of the other module's channel,
never a second producer). To change the payload of an event you already
publish, see [Change an event's payload](event-versioning.md).

## Prerequisites

- The module exists ([Add a module](../create.md)).
- If `events.py` already exists, it must parse and import `Event` from
  `sandloom.sdk` (and `UUID` from `uuid` for the default fields). The generator
  refuses otherwise and names the missing import.

## Steps

### 1. Run the generator

```bash
sandloom add event notes NoteArchived
sandloom add event notes NoteArchived --field note_id:UUID --field reason:str   # explicit fields
```

- The channel is derived from the module and the class name:
  `NoteArchived` in `notes` gives `notes.note.archived.v1`.
- Without `--field`, the event gets `<first word>_id: UUID` (here
  `note_id`). A `tenant_id: UUID` field is always added; a consumer binds
  the tenant from it.
- `--field name:type` takes `str`, `int`, `float`, `bool`, `UUID`,
  `datetime`, `date` or `Decimal`.
- The class is **appended** to `events.py` (created when absent). Existing
  code is never rewritten.

### 2. Read what it wrote

The appended class (the rest of the file is the module's existing events):

```python
--8<-- "docs/snippets/generated/event/demo/modules/notes/events.py:13:15"
```

The channel's first segment is the module's name, so this class is the
channel's **producer**: `sandloom generate` (run last by `sandloom add`)
snapshots its schema and exports it to the
shared DTO package (`<app>/shared/events/notes.note.archived.v1.json`), where
consumers in other repositories verify their views against it.

### 3. Publish it

Publish from a service, inside the unit of work that makes the change. The
event is written to the transactional outbox and is delivered only if the
unit commits. The tutorial app's `tasks` module does it like this:

```python
--8<-- "examples/tutorial/tasklist/modules/tasks/services.py:12:17"
```

`.publish()` outside a unit of work raises (SANDLOOM-R002). In a route, the
request's unit is already open.

### 4. Test it

`published(E)` returns the events of `E`'s channel that the code under test
published:

```python
--8<-- "examples/tutorial/tasklist/modules/tasks/tests/test_tasks.py:tests"
```

## Verify

```bash
sandloom test notes
```

The first run writes the producer's schema to `module.snapshot.json` and to
the shared DTO package. Commit both. From then on, only an added optional
field is accepted on the same `.v1`; anything else fails with SANDLOOM-T005.

## Errors you may hit

- [SANDLOOM-B015](../../reference/error-codes.md#sandloom-b015): the channel is not
  `<module>.<entity>.<verb>.vN`, a field is not a wire type, two producer
  classes share a channel, or the producer lives outside its module.
- [SANDLOOM-R002](../../reference/error-codes.md#sandloom-r002): `.publish()` ran
  outside a unit of work.
- [SANDLOOM-R004](../../reference/error-codes.md#sandloom-r004): the payload does not
  match the channel's schema.
- [SANDLOOM-T005](../../reference/error-codes.md#sandloom-t005): a breaking change on a
  channel that already has a committed schema; publish a `.v2`.

## Related

- [Events and consumers](events.md): delivery, ordering, the
  dead-letter queue.
- [Add a consumer](add-consumer.md).
- [Change an event's payload](event-versioning.md).
- [Units of Work and `db()`](../database/units.md): why the event commits
  with the row.
