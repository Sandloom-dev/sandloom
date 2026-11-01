---
title: Add a consumer
description: React to another module's event with `sandloom add consumer`, a typed view of its channel and an `@on` handler that applies the effect once.
type: how-to
---

# Add a consumer

**Goal:** your module reacts to an event another module publishes, such as
counting items when `tasks.item.created.v1` arrives.

**Use it** for work that may happen after the producer committed: counters,
read models, notifications, starting your own process.

**Do not use it** when the producer needs your answer before it commits
(that is a [contract](add-contract.md)), or to keep a list page in sync from
several channels with versions (that is a [projection](add-projection.md),
which generates its consumers for you). Delivery is asynchronous and
at-least-once; there is no ordering guarantee between events.

## Prerequisites

- Your module exists ([Add a module](../create.md)).
- The producer's schema is known to the generator, in one of two places:
  - the producer is a module in the same app (its `Event` class is found in
    the tree);
  - or the producer's exported schema file is in your shared DTO package,
    `<app>/shared/events/<channel>.json` (written by `sandloom generate` in
    the producer's repository).

  Otherwise you pass the fields yourself with `--field`, and the view is
  marked unverified.
- If `consumers.py` already exists, it must parse and import `Event, on`
  from `sandloom.sdk` (and the types the fields need).

## Steps

### 1. Run the generator in the consuming module

```bash
sandloom add consumer board notes.item.created.v1
```

When no producer schema is found, the generator refuses and asks for the
fields:

```bash
sandloom add consumer board billing.invoice.paid.v1 \
    --field invoice_id:UUID --field tenant_id:UUID --field amount:int
```

The generator **appends** a view class and an `@on` handler to
`<module>/consumers.py` (created when absent).

### 2. Read what it wrote

Both runs above, in one file:

```python
--8<-- "docs/snippets/generated/consumer/demo/modules/board/consumers.py"
```

- **A view, not a producer.** The channel's first segment (`notes`) is
  another module, so the class is a *view* of that channel: it is not
  registered as a schema, `.publish()` on it is refused, and payload fields
  it does not declare are ignored. You may delete fields you do not need;
  never add one the producer does not send.
- **Verified or not.** A view built from the producer's schema is checked
  against it. A view built from `--field` carries the comment
  `# unverified: no producer schema was found` and is reported as unverified
  until the producer's schema file is available.
- **The handler.** `@on(E)` registers the function as a durable consumer
  named after the function. Each delivery runs in its own unit of work;
  `db()` is that unit's session. The envelope id is claimed in the same
  unit, so a redelivered event applies its effect once. The tenant is bound
  from the event's `tenant_id` field.

### 3. Write the effect

Fill the handler with the effect, through your module's services. The
tutorial app's `stats` module counts items:

```python
--8<-- "examples/tutorial/tasklist/modules/stats/consumers.py:consumer"
```

```python
--8<-- "examples/tutorial/tasklist/modules/stats/services.py:services"
```

Keep the effect inside the handler's unit of work. An effect outside the
database (an email, a call to a provider) is not covered by the claim: make
it idempotent on the event's id or the business key.

To stop retrying an event that can never succeed, raise
`PoisonEventError` (from `sandloom.sdk.events`): the event goes to the
dead-letter queue at once. Any other exception rolls back the unit and the
event is redelivered, then dead-lettered after the delivery budget.

### 4. Options you may need

`@on` takes the options of the underlying consumer registration. The
defaults are right for most handlers:

| Option | Default | When to change it |
| --- | --- | --- |
| `name` | the function name | two handlers of one channel in one module need distinct names |
| `durable` | `True` | `False` only for an effect that is harmless when repeated |
| `required` | `True` | `False` if the app may start without this consumer running |
| `dead_letter` | `True` | `False` to retry forever with capped backoff (a `PoisonEventError` still dead-letters) |
| `start_from` | `"earliest"` | `"latest"` or a datetime for a new consumer that must skip history |
| `group` | `None` (`<module>:<name>`) | pin an explicit group id, for example to drain a group created under another name |
| `database` | `"default"` | the module's tables live in another database |
| `tenant` | `"derive"` | `"<field>"` for another UUID field, `None` to bind no tenant |

## Verify

Deliver the event in a test with `deliver`. With `times=2` the same envelope
id arrives twice, which proves the effect is applied once:

```python
--8<-- "examples/tutorial/tasklist/modules/stats/tests/test_stats.py:tests"
```

```bash
sandloom test stats
```

## Errors you may hit

- [SANDLOOM-B016](../../reference/error-codes.md#sandloom-b016): the handler's parameter
  is not the event class, a consumer name is used twice, or the tenant field
  does not exist on the event.
- [SANDLOOM-B015](../../reference/error-codes.md#sandloom-b015): the view's channel is
  not versioned, or a field is not a wire type.
- [SANDLOOM-C025](../../reference/error-codes.md#sandloom-c025): the view does not match
  the producer's schema, is unverified, or calls `.publish()`.
- [SANDLOOM-R001](../../reference/error-codes.md#sandloom-r001): the handler touched a
  tenant table with no tenant bound (the event has no `tenant_id`, or
  `tenant=None`).

## Related

- [Events and consumers](events.md): delivery, the dead-letter
  queue, consumer groups.
- [Add an event](add-event.md): the producer side.
- [Add a projection](add-projection.md): a read model fed by events.
- [Testing modules](../tooling/testing.md).
