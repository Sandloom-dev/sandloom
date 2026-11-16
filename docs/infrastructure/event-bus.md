---
title: Event bus
description: The transport that delivers events between modules and its streams, redis and memory drivers.
type: concept
---

# Event bus

The event bus carries [events](../modules/between/events.md) between modules. Modules do not call it:
they publish with `await E(...).publish()` and consume with `@on`.

Choose the driver with `EVENT_BUS_BACKEND`. Unset (the default), it leaves the bus to the composition: `sandloom_standard` selects `streams`. Nothing selects `memory` for you, so name it for development (`EVENT_BUS_BACKEND=memory`). `EVENT_BUS_BACKEND=auto` is refused at startup: unset the variable.
The `streams` driver takes the option `url`, its own Redis connection, on every instance: `INFRA.eventbus.default.url` for the default bus. A bus never inherits the cache's url.
`redis` has no connection of its own and refuses a `url` at startup. It runs over a `broadcast` instance named by its option `broadcast` (default `default`), on that instance's Redis.

## Drivers

### `streams`

Redis Streams: durable, delivered at least once, with consumer groups. Use it in production. It
does not support wildcard subscriptions.

### `redis`

Redis Pub/Sub: volatile, delivered only to live subscribers, with wildcard subscriptions.

**Limits.** For development and tests. In production the provider is built only if
`SANDLOOM_ACCEPTED_RISKS` lists `volatile_event_bus`. Even then, startup refuses a volatile
default bus in production: the default `eventbus` instance must be durable, so use `streams`
there. A volatile instance other than `default` can run with the accepted risk.

### `memory`

In-process and volatile, with wildcard subscriptions.

**Limits.** The same as `redis`: for development and tests, with the same accepted risk and the
same production refusal for the default bus.
