---
name: sandloom-event-version-roll
description: Roll one of this app's event channels from `<module>.<event>.v1` to `.v2` without breaking consumers in other services: add the v2 class, dual-publish with publish_event_versions, move the @on consumers, watch `sandloom streams consumers`, then retire v1. Use for any breaking payload change (a renamed, removed or retyped field, a new required field) or when asked to "bump the event version".
---

# Roll an event from v1 to v2

The worked example, with its tests, runs in CI: `uv run sandloom docs recipe
event-v2`.

**First decide whether you need a v2.** An optional new field is additive
and stays in `.v1` (`sandloom test` accepts it); readers treat a missing field as
"unknown". Anything else is breaking and needs `.v2`: on the same `.vN`,
`sandloom generate` and `sandloom test` refuse it (SANDLOOM-T005).

## Steps

1. **Declare v2 next to v1** in the producer's `events.py`; never edit v1.
   A field named like the Event API (`channel`, `meta`, `publish`,
   `wire_schema`) is refused (SANDLOOM-B015): keep such a wire key with an alias,
   `via: str = Field(alias="channel")`.
2. **Dual-publish** where v1 is published today, in the same unit:

   ```python
   from sandloom.sdk.events import (
       publish_event_versions,
   )  # sandloom: advanced dual-publish

   await publish_event_versions(
       {
           ItemCreated.channel: v1.model_dump(mode="json"),
           ItemCreatedV2.channel: v2.model_dump(mode="json"),
       }
   )
   ```

   Each version is checked against its own schema and gets its own outbox
   row and envelope id; they commit together or not at all.
3. **Move consumers** to a view of v2, one service at a time. A view
   declares every field the producer requires and the optional ones it
   reads. A consumer of both versions sees the event twice: keep one
   handler per module.
4. **Tests.** `with recording_uow() as unit:` (`sandloom.sdk.testing`) records
   both outbox rows (`unit.events`); `await deliver(ViewV2(...))` drives a
   consumer through the real runtime.
5. **Watch for v1 readers:** `uv run sandloom streams consumers`.
6. **Retire v1** when no group reads it in any environment: publish
   `await ItemCreatedV2(...).publish()` alone. Keep the v1 class while old
   events may be replayed (the stream retention, 7 days by default).

## Rules

- Never publish v2 only while a v1 reader is live.
- Delivery is unordered and at-least-once: order by a per-aggregate
  `version` field and dedupe on the envelope id (`durable=True`).
