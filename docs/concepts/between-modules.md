---
title: Choose how modules talk
description: Modules never import each other. When to use an event, a contract, a projection or a saga.
type: concept
---

# Choose how modules talk

Modules never import each other. Pick the mechanism by what the caller needs:

| You need to | Use | Guide |
| --- | --- | --- |
| tell others something happened | an [event](events.md) | [Add an event](../guides/add-event.md) |
| react to something another module did | an [`@on` consumer](events.md) | [Consume an event](../guides/add-consumer.md) |
| ask another module now | a [contract](contracts.md) | [Provide and call a contract](../guides/add-contract.md) |
| show a list that spans modules | a [projection](projections.md), never a request-time join | [Build a projection](../guides/add-projection.md) |
| run a process across modules, with undo | a [saga](sagas.md) owned by one module | [Add a saga](../guides/add-saga.md) |

## Event or contract?

Most choices come down to these two.

| | Event | Contract |
| --- | --- | --- |
| **The caller** | moves on | waits for the answer |
| **Timing** | published after commit, handled later | runs during the call |
| **Delivery** | at least once, so a consumer may see a duplicate; `@on` claims each event once | one call; a command is deduplicated by its idempotency key |
| **Transaction** | atomic with the producer's write, through the outbox | runs in the provider's own unit, not atomic with the caller's |
| **If the other side is down** | the default `streams` bus delivers the event later, within [stream retention](events.md#poison-events-and-the-dead-letter-queue); the volatile `redis` and `memory` buses reach only live subscribers | the call fails with a 503 |
| **Direction** | the producer does not know who listens | the caller names the provider |

Prefer an event when the other module only needs to know something happened. Use a contract
when the caller cannot continue without the answer.

## Referring to another module's data

Store the other module's id as a plain UUID, never as a foreign key. A module may later move
to its own database, and a foreign key could not follow it.
