---
title: Guarantees and labels
description: What each effect guarantees (atomic, no loss, no duplicate, visibility), how the label is derived from the mechanism and its windows, and where the ledger lives.
type: concept
---

# Guarantees and labels

Sandloom's guarantees are the product: a write commits with its events, a
redelivered event is applied once, a retried command does not repeat. But a
guarantee holds only under conditions: the right store, a long enough dedupe
window, a durable bus. So Sandloom never labels an effect unconditionally. It
**derives** each effect's label from the mechanism that implements it, the
configured provider and its windows, and shows a label only where it deviates
from the default.

## The vocabulary

A label has a **delivery** part and a **visibility** part.

| Term | Meaning |
| --- | --- |
| `atomic` | the effect commits if and only if the change that caused it commits |
| `no_loss` | once the change committed, the effect eventually happens |
| `no_dup` | the effect happens at most once, even under retries and redelivery |
| `immediate` | visible to the next read |
| `eventual` | visible after a delay (a consumer, a projection) |

`atomic` implies `no_loss` and `no_dup`. Labels combine by meet: delivery sets
intersect and visibility takes the weaker, so a chain of effects is as strong
as its weakest link. Response replay (an idempotent HTTP retry) is a separate
axis, described in [Route policy](../http/route-policy.md#idempotency).

## Labels per effect

| Effect | `no_dup` when | `no_loss` when | `atomic` when | Visibility |
| --- | --- | --- | --- | --- |
| a write in the route's write store | always | always | always | immediate |
| an outbox event (its delivery) | never: delivery is at-least-once, consumers dedupe on the envelope id | the outbox and a durable bus; a delivery may be **parked** in the dead-letter queue | the intent only | eventual |
| a consumer effect with `durable=True` | the inbox is in the **same database** as the effect, and inbox retention is at least stream retention plus one day | the outbox and a durable bus, always qualified: `dead_letter=True` parks on poison or after the delivery budget; `dead_letter=False` parks only on an explicit `PoisonEventError` | no | eventual |
| a consumer effect in another store | the target write is conditional (version-guarded, see [Projections](../between/projections.md)) | as above | no | eventual |
| a command | the provider has a durable command log and its dedupe TTL covers the caller's retry horizon; the cache fallback gives **no** `no_dup` (not crash-safe) | the caller retries with the same key until a definite answer | no | immediate |
| an `on_commit` callback | always | never (a crash after the commit loses it) | no | none |
| a write in an opted-out secondary (`allow_multi_db_write`) | a keyed or version-guarded write | never | never | immediate |

Read the table as "this effect has this property **when** the condition
holds". The conditions are checked where they can be:

- **Guarantee windows** are checked at startup: the command dedupe TTL
  against the relay horizon, the inbox retention against the stream
  retention. A window that does not hold is a named, accepted risk, refused
  in production unless listed in `SANDLOOM_ACCEPTED_RISKS`
  ([ADR-0006](../../adr/0006-accepted-risks.md),
  [configuration tiers](../../reference/configuration-tiers.md)).
- **Features the defaults need** (a durable command log, conditional writes)
  are checked at boot.
- **The write store** of a route is inferred statically, so the durable
  idempotency record, the audit rows and the outbox rows land in the same
  database as the write ([Write admission](write-admission.md)).

## What a module author relies on

- Your route's write, its events (as intents), its audit rows and its
  idempotency record commit together or not at all.
- An event you publish is delivered at least once, in no particular order;
  every consumer must tolerate a duplicate and an out-of-order event
  ([Events](../between/events.md)).
- A consumer with `durable=True` applies each event once, if its effect is in
  the consumer's own database.
- A command call is deduplicated by its key in the provider; it is **not**
  atomic with your unit. Compose commands that must undo each other as a
  saga ([Sagas](../between/sagas.md)).
- A projection is eventually consistent and converges ([Projections](../between/projections.md)).
- An `on_commit` callback can be lost on a crash: never put an effect there
  that must happen.

??? note "The ledger"

    Every guarantee Sandloom promises and every capability a user relies on is an
    entry with a stable id (`G-...` for guarantees, `C-...` for capabilities)
    and the tests, `sandloom check` rules and import contracts that prove it. The
    ledger (`guarantees.yml`) is a CI gate: an enforcing test that
    disappears, an entry without enforcement and not recorded as a gap, or an
    ADR section without ledger ids fails the build. Soundness scenarios exist
    for every row of the table above, including the cache fallback and the
    expired-window cases (C-MSTORE-03).

    The page: [Guarantees & capabilities](../../reference/guarantees.md).

## Design rationale

- [ADR-0005: the effect model](../../adr/0005-effect-model.md): boundaries,
  guarantee levels, the dedupe record, the commit pipeline, the ambiguous
  COMMIT table and the guarantee windows. Read its matrix before relying on a
  guarantee. Do not add a new dedupe store or post-commit variant: reuse one,
  or amend the ADR.
- [ADR-0006: accepted risks](../../adr/0006-accepted-risks.md)
- [ADR-0010: Public API 1.0](../../adr/0010-public-api-1.0.md), "Guarantee labels"
