---
title: Change an event's payload
description: Add an optional field on the same channel, or roll a typed event from .v1 to .v2 with dual publishing until no consumer reads .v1.
type: how-to
---

# Change an event's payload (v1 to v2)

**Goal:** change what an event carries without breaking the consumers that
read it, including consumers in other services that upgrade on their own
schedule.

**Use it** whenever you edit the fields of an `Event` class whose schema is
already committed (`module.snapshot.json`, and the exported file in the
shared DTO package).

**Do not use it** for a new event: [add one](add-event.md). A channel that
has never been committed can still change freely.

## Prerequisites

- The producer's current schema is committed (`sandloom generate` wrote it).
- You know the channel's consumers. `sandloom streams consumers` lists the live
  subscriptions per channel version.

## Steps

### 1. Decide: same version or a new one

The `.vN` suffix is the channel's major schema version. `sandloom generate`
compares the producer's schema with the committed snapshot before it writes
anything, and `sandloom test` (which never writes) makes the same check. Exactly one change is accepted on the
same `.vN`:

- **Compatible: add an optional field.** Stay on `.v1`. Consumers must
  treat a missing value as "unknown", because events from producers that
  are not yet upgraded, and old events replayed from the stream or the
  dead-letter queue, do not carry it.

  ```python
  class NoteArchived(Event, channel="notes.note.archived.v1"):
      note_id: UUID
      tenant_id: UUID
      reason: str | None = None        # added: optional, with a default
  ```

- **Breaking: everything else.** A removed, renamed or retyped field, a new
  required field, a field that became optional, or a changed meaning. This
  fails with SANDLOOM-T005; publish a new channel `.v2` (steps 2 to 6).

### 2. Declare v2 next to v1

Add a second producer class in the producer's `events.py`. Leave the v1
class exactly as it is: its schema stays registered for as long as v1
events can still be delivered.

```python
class NoteArchived(Event, channel="notes.note.archived.v1"):
    note_id: UUID
    tenant_id: UUID

class NoteArchivedV2(Event, channel="notes.note.archived.v2"):
    note_id: UUID
    tenant_id: UUID
    archived_by: UUID
    reason: str
```

### 3. Publish both versions in one unit of work

`publish_event_versions` (from `sandloom.sdk.events`) publishes one domain event
under several versions of its channel:

```python
from sandloom.sdk.events import publish_event_versions

v1 = NoteArchived(note_id=note.id, tenant_id=note.tenant_id)
v2 = NoteArchivedV2(note_id=note.id, tenant_id=note.tenant_id,
                    archived_by=user_id, reason=reason)
await publish_event_versions({
    NoteArchived.channel: v1.model_dump(mode="json"),
    NoteArchivedV2.channel: v2.model_dump(mode="json"),
})
```

- **All or nothing.** Every version is checked against its own schema
  before any is written; all rows join the running unit's outbox and commit
  together, or none do. Versions are published in ascending order.
- **A real roll only.** It requires at least two versions of one base
  channel.
- **Inside a unit of work.** Outside one it raises, like `.publish()`.
- Signature: `publish_event_versions(payloads, *, database=None, bus=None,
  required=None)`.

### 4. Move consumers to v2, one service at a time

In each consuming module, add a view of `.v2` and a handler with **its own
name**, so it gets its own durable consumer group:

```python
class NoteArchivedV2(Event, channel="notes.note.archived.v2"):
    note_id: UUID
    tenant_id: UUID
    reason: str

@on(NoteArchivedV2)
async def on_note_archived_v2(event: NoteArchivedV2) -> None:
    ...
```

`sandloom add consumer <module> notes.note.archived.v2` writes this from the
producer's schema. Then delete the v1 handler. While a module subscribes to
both versions it sees each event twice, once per channel: the envelope ids
differ, so dedupe on the business key (here `note_id`), not only on the
envelope id. A new `.v2` consumer starts from the earliest retained event by
default; pass `start_from="latest"` if it must not process history that its
v1 handler already applied.

### 5. Test both sides

- Producer: `published(E)` records events sent with `E(...).publish()`.
  `publish_event_versions` is a lower-level call and is not recorded there;
  build the payloads in a function and test it, or assert on the outbox in
  a `real_uow_client` test.
- Consumers: `await deliver(NoteArchivedV2(...))` delivers to every handler
  of the channel; `times=2` checks the effect is applied once.

### 6. Retire v1

When no live consumer group reads `.v1` in any environment
(`sandloom streams consumers`), publish only v2:

```python
await NoteArchivedV2(...).publish()
```

Keep the v1 class until v1 events can no longer be replayed: stream
retention (`EVENT_STREAM_RETENTION_SECONDS`, 7 days by default) and any
entry still in a dead-letter queue (`sandloom streams dlq-list`).

## Verify

```bash
sandloom test notes
```

It must pass with the committed snapshot, and the new channel's schema must
appear in `module.snapshot.json` and the shared DTO package. Commit both.

## Errors you may hit

- [SANDLOOM-T005](../../reference/error-codes.md#sandloom-t005): a change other than an
  added optional field on the same `.vN`.
- [SANDLOOM-B015](../../reference/error-codes.md#sandloom-b015): two producer classes on
  one channel, or a channel without a version suffix.
- [SANDLOOM-B016](../../reference/error-codes.md#sandloom-b016): the v1 and v2 handlers
  of one module share a name.
- [SANDLOOM-C025](../../reference/error-codes.md#sandloom-c025): a consumer's view no
  longer matches the producer's schema.
- [SANDLOOM-R004](../../reference/error-codes.md#sandloom-r004): a payload does not
  match its version's schema.

## Related

- [Events and consumers](events.md).
- [Add a consumer](add-consumer.md).
- [Event contracts](../../reference/event-contracts.md): the channel catalogue.
