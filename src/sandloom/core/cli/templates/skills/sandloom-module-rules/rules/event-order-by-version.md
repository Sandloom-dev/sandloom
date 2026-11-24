---
title: Events arrive more than once and out of order
impact: high
tags: events, consumers, ordering
---

Delivery is at-least-once and unordered: carry a per-aggregate `version` in the event and apply an event only when its version is higher than the stored one.

A consumer that copies the event's fields over the stored row lets a late old event undo a newer one. Store the version on the row.

## Incorrect

<!-- code-ok: D4 rule example, a fragment of a module file: D1 and D2 check it -->
```python
@on(NoteRenamedView)
async def on_renamed(event: NoteRenamedView) -> None:
    row = await db().get(NoteCopy, event.note_id)
    row.title = event.title  # a late old delivery wins
```

## Correct

<!-- code-ok: D4 rule example, a fragment of a module file: D1 and D2 check it -->
```python
@on(NoteRenamedView)
async def on_renamed(event: NoteRenamedView) -> None:
    row = await db().get(NoteCopy, event.note_id)
    if row.version < event.version:  # a late or repeated delivery changes nothing
        row.title, row.version = event.title, event.version
```
