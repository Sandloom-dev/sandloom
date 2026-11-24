---
title: A breaking event change is a new .v2 class, published beside v1
impact: high
tags: events, versioning
---

A breaking change is a new `.v2` class next to `.v1`, published with `publish_event_versions` until no `.v1` reader is left.

The same `.vN` may only gain an optional field (SANDLOOM-T005). No check stops you from publishing `.v2` alone while a `.v1` reader is still deployed: that reader then hears nothing. Publish both in one unit, then drop `.v1`. The steps are in skill `sandloom-event-version-roll`.

## Incorrect

```python
await ItemCreatedV2(...).publish()  # every .v1 reader goes silent
```

## Correct

<!-- code-ok: D4 rule example, a fragment of a module file: D1 and D2 check it -->

```python
from sandloom.sdk.events import publish_event_versions  # sandloom: advanced v1 and v2

await publish_event_versions(
    {ItemCreated.channel: v1, ItemCreatedV2.channel: v2}  # each is a payload dict
)
```
