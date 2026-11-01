---
name: sandloom-event-version-roll
description: Roll a Sandloom event channel from `<module>.<event>.v1` to `.v2` without breaking consumers in other services — add the v2 Event class, dual-publish with publish_event_versions, move the @on consumers, watch `sandloom streams consumers`, then retire v1. Use for any breaking payload change (renamed/removed/retyped field, new required field) or when asked to "bump the event version".
---

# Roll an event from v1 to v2

Channels are contracts with other services, and those services upgrade on
their own schedule.

**First decide whether you need a v2 at all.** An **optional** new field is
additive and stays in `.v1` (`sandloom test` accepts it). Consumers must then
treat a missing field as "unknown", because older producers omit it.
Anything else is breaking and needs `.v2`; on the same `.vN` it fails
`sandloom generate` and `sandloom test` with SANDLOOM-T005:

- removing, renaming or retyping a field;
- a new required field;
- a changed meaning.

## Steps

1. **Declare v2 next to v1** in the producer's `events.py`:

   ```python
   class CreditsSpent(Event, channel="billing.credits.spent.v1"):     # unchanged
       ...
   class CreditsSpentV2(Event, channel="billing.credits.spent.v2"):
       ...
   ```

   Leave the v1 class **unchanged**. `uv run sandloom generate` snapshots both
   schemas and exports both to the shared DTO package.
2. **Dual-publish** from the one place that publishes today, in the same
   unit:

   ```python
   from sandloom.sdk.events import publish_event_versions  # sandloom: advanced dual-publish (.v1 -> .v2)

   await publish_event_versions({
       CreditsSpent.channel: v1.model_dump(mode="json"),
       CreditsSpentV2.channel: v2.model_dump(mode="json"),
   })
   ```

   Both versions land in the outbox in one transaction: all commit or none
   do. Each payload is validated against its own schema. Each version is
   its own outbox row with its own envelope id (published v1 first, then
   v2); nothing links the two ids, so `published(CreditsSpentV2)` in a test
   and a consumer's durable claim see them as two events.
3. **Move consumers** to v2, one service at a time.
   - Add a view of v2 and its `@on(CreditsSpentV2)` handler; keep the v1
     handler until the v2 one is live everywhere. Each has its own durable
     group.
   - The two versions have different envelope ids, so a consumer of both
     sees the event twice during the overlap: dedupe on the business key.
4. **Tests.** `with recording_uow() as unit:` (`sandloom.sdk.testing`) records
   both outbox rows (`unit.events`: channel, payload, version); drive each
   consumer with `await deliver(CreditsSpentV2(...))`. A worked example with
   its tests, run in a generated app by CI: `sandloom docs recipe event-v2`.
5. **Watch for v1 readers:** `uv run sandloom streams consumers`. When no live
   group reads `<m>.<event>.v1` (in every environment), go to step 6.
6. **Retire v1.** Replace `publish_event_versions` with
   `await CreditsSpentV2(...).publish()`. Keep the v1 class (its schema stays
   registered) as long as old events may be replayed from the DLQ or the
   stream (retention: `EVENT_STREAM_RETENTION_SECONDS`, default 7 days).

## Rules

- Never publish v2 only while a v1 reader is live.
- Never edit v1's class in place.
- Delivery has no ordering guarantee. Order by a per-aggregate `version`
  field, not by arrival.
- Concept: `docs/modules/between/events.md`.
