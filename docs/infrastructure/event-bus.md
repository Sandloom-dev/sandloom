---
title: Event bus
description: The transport that delivers events between modules and its streams, redis and memory drivers.
type: concept
---

# Event bus

The event bus carries [events](../concepts/events.md) between modules. Modules do not call it:
they publish with `await E(...).publish()` and consume with `@on`.

Choose the driver with `EVENT_BUS_BACKEND`. The default, `auto`, chooses `streams` in production and `memory` elsewhere.
A named instance takes the option `url`, its own Redis connection.

## Drivers

### `streams`

Redis Streams: durable, delivered at least once, with consumer groups. Use it in production. It
does not support wildcard subscriptions.

### `redis`

Redis Pub/Sub: volatile, delivered only to live subscribers, with wildcard subscriptions.

**Limits.** For development and tests. It is refused in production unless
`SANDLOOM_ACCEPTED_RISKS` lists `volatile_event_bus`.

### `memory`

In-process and volatile, with wildcard subscriptions.

**Limits.** The same as `redis`: for development and tests, and it needs the same accepted risk in
production.
