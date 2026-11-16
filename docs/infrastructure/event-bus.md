---
title: Event bus
description: The transport that delivers events between modules and its streams and memory drivers.
type: concept
---

# Event bus

The event bus carries [events](../modules/between/events.md) between modules. Modules do not call it:
they publish with `await E(...).publish()` and consume with `@on`.

Choose the driver with `EVENT_BUS_BACKEND`. Unset (the default), it leaves the bus to the composition: `sandloom_standard` selects `streams`. Nothing selects `memory` for you, so name it for development (`EVENT_BUS_BACKEND=memory`). `EVENT_BUS_BACKEND=auto` is refused at startup: unset the variable.
The `streams` driver takes the option `url`, its own Redis connection, on every instance: `INFRA.eventbus.default.url` for the default bus. The bus does not inherit the cache's url.

## Drivers

### `streams`

Redis Streams: durable, delivered at least once, with consumer groups. Use it in production. It
does not support wildcard subscriptions.

### `memory`

In-process and volatile, with wildcard subscriptions.

**Limits.** For development and tests. It is refused in production unless
`SANDLOOM_ACCEPTED_RISKS` lists `volatile_event_bus`.
