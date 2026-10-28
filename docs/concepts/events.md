---
title: Events and consumers
description: Typed Event classes, publishing through the outbox, @on consumers, at-least-once delivery and exactly-once effects.
type: concept
---

# Events and consumers

An **event** is a fact one module states about its own data, such as "a task
was created". Other modules react to it without importing the producer. An
event is a typed class, `class TaskCreated(Event, channel="tasks.task.created.v1")`;
`await TaskCreated(...).publish()` writes it to the transactional outbox of
the running unit, so it is sent if and only if the change commits. A
**consumer** is an `async def` decorated with `@on(TaskCreated)`; each
delivery runs in its own unit. Delivery is at least once, so a consumer that
must apply an effect once claims the event id in its own transaction, which
`@on` does for you by default. Events are Sandloom's asynchronous cross-module
channel; the synchronous one is a [contract](contracts.md).

## What you get by default / what you declare

| You get by default | You declare |
| --- | --- |
| The class's JSON schema registered, snapshotted in `module.snapshot.json` and exported to the shared DTO package by `sandloom generate` | `class E(Event, channel="<module>.<entity>.<verb>.v1")` with wire-type fields |
| Producer or view role derived from the channel's first segment | `role="producer"` / `role="view"` only when the layout cannot tell |
| `.publish()` writes to the outbox of the running unit; the event is sent iff the unit commits | `await E(...).publish()`, inside a unit (a request, a consumer, `open_uow()`) |
| A stable envelope id, the schema version taken from the `.vN` suffix, delivery metadata on `event.meta` | nothing |
| A durable consumer group `<module>:<function name>`, claimed once per envelope id in the consumer's unit | `@on(E)` on `async def f(event: E) -> None` |
| The tenant bound from a `tenant_id: UUID` field of the event | `tenant="<field>"` for another UUID field, `tenant=None` for none |
| Readiness gating: the process is not ready until every required consumer is subscribed | `required=False` for a deliberately best-effort consumer |
| Dead-lettering after the bus's delivery budget, or at once on `PoisonEventError` | `dead_letter=False` for an effect that must never be parked |
| A compatibility check: only an added optional field passes on the same `.vN` | a new `.v2` channel for any other change |

## How to use it

### Declare and publish

A producer lives in the module's `events.py`. The channel is
`<module>.<entity>.<verb>.vN` in lower case; its first segment is the short
name of the module that owns it.

```python
--8<-- "examples/tutorial/tasklist/modules/tasks/events.py"
```

Publish from code that runs inside a unit, typically right after the write
it reports:

```python
await ItemCreated(item_id=item.id, tenant_id=item.tenant_id).publish()
```

- **Fields are plain data** from a closed set of wire shapes (the table
  below); anything else is refused at class definition
  ([SANDLOOM-B015](../reference/error-codes.md#sandloom-b015)). A field named
  `channel`, `meta`, `publish` or `wire_schema` is refused at class
  definition (SANDLOOM-B015: it would shadow the event's API). To keep such a
  wire key, alias the field: `via: str = Field(alias="channel")` is
  published as `channel`, and every consumer reads it back by that key.
  Unknown keyword arguments are refused at construction.
- **Payloads hold ids and amounts, not PII.** Consumers can read the rest
  through the owner's read contract.
- **Order is yours to carry.** Events have no ordering guarantee (below). If
  consumers must apply events in order, add a per-aggregate `version: int`.
- **Several databases in one unit stack:** `publish(database="x")` chooses
  which database's outbox gets the row. Without it, an ambiguous choice
  raises instead of guessing.
- `.publish()` outside any unit raises `UnitOfWorkRequiredError`
  ([SANDLOOM-R002](../reference/error-codes.md#sandloom-r002)). There is no outbox to
  make it reliable. The deliberately lossy L2 form,
  `publish_event(..., required=False)`, needs a `# sandloom: best-effort <reason>`
  marker ([SANDLOOM-C007](../reference/error-codes.md#sandloom-c007)).

The generator writes a producer class for you: `sandloom add event <module>
<name>`. See [Add an event](../guides/add-event.md).

### What an event field may be

An event payload is plain data that every reader, old or new, decodes the
same way. The set of shapes is closed: each field, at every depth, is one of
the allowed shapes, and a class with any other is refused when it is defined
([SANDLOOM-B015](../reference/error-codes.md#sandloom-b015)), with the field's path
(`OrderPlaced.lines[].item`) and the fix. The check judges what pydantic
validates, so a custom type is fine when it validates as an allowed shape.

| Allowed | Notes |
| --- | --- |
| `str`, `int`, `bool`, `None` | constraints (`Field(ge=0)`, `StringConstraints`) included |
| `float` | finite: an event's own floats are (`allow_inf_nan=False` on the `Event` base); a DTO states it, `ConfigDict(allow_inf_nan=False)` or `Field(allow_inf_nan=False)` |
| `UUID`, `datetime`, `date`, `time`, `timedelta`, `Decimal` | published as strings; a `Decimal` is `format: decimal` in the wire schema; a `datetime` / `time` carries its UTC offset to the minute (not a zone name) |
| an `Enum` (`StrEnum`, `IntEnum`), a `Literal` | values are JSON scalars |
| `Any` | an opaque JSON value, read as it is (`dict[str, Any]` for an opaque object): give it JSON values (a `UUID` in it is read back as a `str`) |
| `X \| None` | any allowed `X` |
| a union of `Literal` / `Enum` members (`Literal["a", "b"] \| Literal[1]`, `Color \| Level \| None`) | no member reads another member's published value |
| `list[T]`, `tuple[T, ...]`, `tuple[A, B]`, `dict[str, T]` | `set` / `frozenset` of scalars |
| a pydantic `BaseModel` DTO | each field has one wire name: `alias=` or an `alias_generator`, non-empty and unique in its model; `exclude=` on a field with a default |
| in `model_config` (an event's or a DTO's) | `extra` (`"forbid"`, `"ignore"`, `"allow"`), `frozen`, `alias_generator`, `validate_by_name` / `populate_by_name`, `validate_by_alias`, `serialize_by_alias`, `strict`, `str_strip_whitespace`, `str_to_lower`, `str_to_upper`, `str_min_length`, `str_max_length`, `allow_inf_nan=False`, `validate_default`, `validate_assignment`, `revalidate_instances`, `from_attributes`, `title`, `json_schema_extra`, `use_attribute_docstrings`, `loc_by_alias`, `hide_input_in_errors`, `cache_strings` and the other options that change neither the published JSON nor its reading |
| `Annotated[A \| B, Field(discriminator="kind")]` | every branch a DTO with a `kind: Literal[...]` tag |
| validators with `mode="after"` | they see the validated value and run on every read: keep them idempotent; `mode="before"` / `"wrap"` only on a field with no DTO below it |

| Refused | Fix |
| --- | --- |
| a union of scalars whose members can read the same value (`int \| str`, `UUID \| str`, `float \| int`, `datetime \| str`, `Literal[1] \| Literal[True]`), in any `union_mode`, strict or not | use one type, or a discriminated DTO |
| a union holding a DTO or a container without a discriminator (`A \| B`, `A \| dict[str, Any]`, `list[int] \| str`), in any `union_mode` | give every DTO branch a `kind: Literal[...]` field and declare `Field(discriminator="kind")` |
| a callable `Discriminator` | name a `Literal` tag field: `Field(discriminator="kind")` |
| a DTO that overrides `__init__` or `__new__` | move the logic to `@model_validator(mode="after")` or a classmethod constructor |
| `mode="before"` / `"wrap"` validators over a DTO (model or field level), `mode="plain"` / `PlainValidator` anywhere | `mode="after"` |
| `field_serializer`, `model_serializer`, `PlainSerializer`, `SerializeAsAny`, `computed_field` | publish each value as a regular field |
| a wire name that does not round-trip: `serialization_alias` or `validation_alias` alone, `AliasChoices`, `AliasPath`, an alias with `validate_by_alias=False`, an empty one (`alias=""`, an `alias_generator` returning `""`) | `Field(alias="wireName")`: one name both ways |
| two fields with one wire name (`alias="x"` twice, an `alias_generator` mapping two names to one key); a field named like another field's wire name in a model that reads by name (an event does) | one wire name per field: rename or re-alias one |
| a `model_config` option that changes the published JSON or its reading: `ser_json_timedelta`, `ser_json_temporal`, `ser_json_inf_nan`, `ser_json_bytes`, `val_json_bytes`, `val_temporal_unit`, `coerce_numbers_to_str`, `regex_engine`, `use_enum_values`, `polymorphic_serialization`, `json_encoders`, `json_schema_mode_override`, `allow_inf_nan=True`, any option not listed above | drop it: the wire form is pydantic's default JSON |
| a `float` that may be NaN or infinite (a DTO's without `allow_inf_nan=False`, `Field(allow_inf_nan=True)`), `Field(coerce_numbers_to_str=True)` | `allow_inf_nan=False`; drop `coerce_numbers_to_str` |
| `Field(exclude=True)` (or `exclude_if`) without a default | give it a default |
| a dataclass, a `TypedDict`, a `NamedTuple`, a `RootModel`, an unparametrised generic DTO | a `BaseModel` DTO (parametrise a generic: `Page[Item]`) |
| `bytes`, `AnyUrl`, `Json`, `Path`, IP addresses and other non-JSON scalars | `str` (validate it with an after validator) |
| `dict[int, T]`, bare `dict`, a set of non-scalars | `dict[str, T]`, `list[T]` |

Why closed: a reader built for these shapes ignores a newer producer's key
at every depth and returns the same value, of the same type, a constructed
event holds. Each refused shape breaks that somewhere: an undiscriminated
union can pick the wrong branch and drop a newer key's value, a union of
overlapping scalars reads `"123"` back as `123`, a custom `__init__` or a raw
validator builds a DTO with its own strict validator, a one-way, empty or
shared wire name or a serializer publishes what the reader does not read
back, a `ser_json_*` option publishes `timedelta(seconds=2)` as `2000.0`,
and JSON has no NaN. A property test builds random events from the whole
set (and from what it refuses) and holds every one that defines to an
exact round trip through JSON
(`tests/sdk/test_sdk_event_roundtrip.py`).

`from_wire` reads JSON data (a delivery's decoded payload), never Python
objects: a DTO instance in a payload is refused. A class rebuilt after a
configuration change (`model_rebuild`) is checked and gets a new reader on
its next decode.

### Producers and views

The same `Event` base class declares both sides of a channel:

- A **producer** is a class whose channel starts with the defining module's
  own short name. Its schema is registered and exported; it may publish. A
  module defines at most one producer class per channel.
- A **view** is a class for another module's channel, declared by the
  consumer with only the fields it reads. A view is never registered, and
  its `.publish()` is refused (`EventDefinitionError`;
  [SANDLOOM-C025](../reference/error-codes.md#sandloom-c025) finds it statically).
  Unknown payload fields are ignored when a view is decoded.

```python
--8<-- "examples/tutorial/tasklist/modules/stats/consumers.py:consumer"
```

A view's fields are checked against the producer's schema: the producer in
the same app first, else the exported schema file
(`<shared package>/events/<channel>.json`), else nowhere, which is reported
as "unverified" (SANDLOOM-C025, a warning). `sandloom add consumer <module> <channel>`
writes a view from the producer schema, so it starts compatible.
`role="producer"` / `role="view"` state the role when the layout cannot
derive it (advanced); a stated producer outside the channel's module is
refused ([SANDLOOM-B015](../reference/error-codes.md#sandloom-b015)).

### Consume

`@on(E)` turns a coroutine that takes exactly one parameter, the event, into a
durable consumer. Put it in the module's `consumers.py`; discovery registers
it. The options are those of the L2 `register_event_consumer`:

| Option | Default | Meaning |
| --- | --- | --- |
| `name=` | the function name | the consumer's name; its group is `<module>:<name>` |
| `durable=` | `True` | claim the envelope id in the consumer unit's database before the handler runs (the durable inbox) |
| `required=` | `True` | startup fails if it cannot subscribe; `/ready` fails while a lost subscription reconnects |
| `dead_letter=` | `True` | park a delivery in `<channel>.dlq` after the bus's delivery budget; `False` retries forever with capped backoff |
| `start_from=` | `"earliest"` | where a **new** group starts: `"earliest"` (everything within stream retention), `"latest"`, or a `datetime` |
| `group=` | `None` | pin an explicit group id (for example to drain a group created under another name) |
| `database=` | `"default"` | the database of the consumer's unit and inbox claim |
| `tenant=` | `"derive"` | bind the tenant from `tenant_id: UUID`; `"<field>"` names another UUID field; `None` binds none |

Inside the handler, `db()` is the consumer unit's session; the unit commits
when the handler returns and rolls back, for redelivery, when it raises.
`event.meta` holds the delivery metadata (`id`, `channel`,
`schema_version`, `trace_id`, `emitted_at`, `tenant_id`). It is not part of
the payload and exists only on delivered instances: reading it on an event
you built yourself raises `EventMetaUnavailable`.

Without a bound tenant a handler cannot touch `TenantScoped` tables (they
fail closed); consumers never inherit the publisher's tenant context. With
`tenant="derive"` and a `tenant_id` field, the tenant is bound for you; a
missing or invalid value fails the delivery.

See [Add a consumer](../guides/add-consumer.md).

### Groups in split deployments

A consumer's group is namespaced by the module that declares it, so two
modules, or one module deployed in two services, that reuse a name never
share a group and never split each other's events. Replicas of one
deployment share the group. Startup fails when two consumers of one process
resolve to the same group, when an explicit `group=` is already used by
another module, or when the group is already owned by another module or
physical database (the first joiner records an owner fingerprint).

`/diagnostics/consumers` reports each local consumer's state, connection
attempts, last error, group, module and the group's lag and pending count.
Startup retries required consumers for `CONSUMER_STARTUP_TIMEOUT_SECONDS`;
reconnect backoff is `CONSUMER_RETRY_BASE_SECONDS` /
`CONSUMER_RETRY_MAX_SECONDS`. A handler failure for one event never makes the
process unready: that event stays unacknowledged for redelivery or the DLQ.

### At-least-once delivery and exactly-once effects

- The envelope id equals the outbox row id and is **stable across
  redeliveries**. Duplicate deliveries do happen (the post-commit dispatch
  and the relay can race); this is by design.
- With `durable=True` (the default) the consumer unit claims
  `(consumer, envelope id)` in its own database before your handler runs; a
  duplicate is skipped. The claim commits with your effect, so a crash
  before COMMIT redelivers and a crash after it never re-applies. This is
  exactly-once **effect** on that database's state, not exactly-once
  delivery.
- With `durable=False` the dedupe is a cache record: best effort, fail-open,
  TTL-bounded. Use it only for idempotent or non-critical handlers.
- An effect in **another** store (a cache, an external API, another
  database) is not covered by the claim. Make it idempotent, key it with
  `operation_key(...)`, or write it conditionally.
- L2 code that registers a raw `(session, envelope)` handler claims
  explicitly: `if not await durable_inbox("<consumer>").claim(session,
  envelope["id"]): return`.

### Poison events and the dead-letter queue

- A handler error rolls the unit back and the delivery is retried with
  per-message backoff. After the bus's delivery budget a `dead_letter=True`
  consumer's delivery is parked in `<channel>.dlq`, recording the reason and
  the group.
- Raise `PoisonEventError` (from `sandloom.sdk.events`) for an event that can
  never apply, such as a malformed payload: it is dead-lettered at once,
  even on a `dead_letter=False` consumer. That is the only way out of a
  consumer that otherwise retries forever.
- Inspect and replay once the cause is fixed. A replay is targeted at the
  group that dead-lettered the entry; other groups skip it.

```bash
sandloom streams dlq-list   <channel> [--limit N]
sandloom streams dlq-replay <channel> [--id DLQ_ID ...] [--limit N]
```

- On the producer side, an outbox row that exhausts
  `OUTBOX_RELAY_MAX_ATTEMPTS` is dead-lettered too (`dead_at` set,
  `outbox_dead_total` incremented), not retried forever. Repair it with
  `sandloom outbox list-dead` / `sandloom outbox requeue`. A broker outage does not
  burn attempts: it trips the relay's circuit breaker.
- Streams are trimmed by age (`EVENT_STREAM_RETENTION_SECONDS`, default 7
  days) and optionally by length (`EVENT_STREAM_MAXLEN`). A consumer offline
  longer than retention misses events.

### No ordering guarantee

There is no ordering guarantee, not even within one channel or one aggregate:
concurrent units, relay redeliveries and retries interleave. Only one unit's
own post-commit dispatch is sequential in emission order. Order by a
per-aggregate `version` in the payload and apply it with a version guard
(see [Projections](projections.md)). If a `version` field was added as
optional within a `.v1`, events from not-yet-upgraded producers lack it:
treat such an event as "unknown order" and refresh from the owner's read
contract instead of applying or dropping it.

### Changing an event

The `.vN` suffix is the major schema version and is authoritative: the outbox
row and the envelope take `schema_version` from it.

- **Compatible on the same `.vN`:** adding an optional field: one with a
  default of any kind, a literal (`note: str | None = None`) or a factory
  (`tags: list[str] = Field(default_factory=list)`). The wire schema lists
  it as required (it is always emitted) and marks it `"x-sandloom-default": true`;
  a reader fills it when an older payload omits it. `sandloom generate`
  compares the producer's schema with the previous committed snapshot before
  writing anything (`sandloom test` compares and never writes); any other
  change fails with
  [SANDLOOM-T005](../reference/error-codes.md#sandloom-t005).
  Producers and consumers of one `.vN` deploy independently, so the check
  holds both directions: a new reader accepts what an old producer sends,
  and an old reader accepts what a new producer sends. Loosening a
  constraint (`le=10` to `le=20`) or removing a field breaks the second
  one, even on a model open to extra keys. A
  model open to extra keys (`ConfigDict(extra="allow")`) may already carry
  any key as an extra, so a field it newly declares must accept whatever
  the old model allowed there (`score: Any = None`, not `score: int = 0`).
- **Readers ignore unknown keys at every depth.** An `@on` consumer, a view
  or a projection decodes a payload with `from_wire`: a key its class does
  not declare is ignored, at the top level and in every nested DTO, even one
  declared `ConfigDict(extra="forbid")` (construction stays strict; only the
  reader ignores). The reader, built when the class is defined, is the
  class's own validation schema with `extra="forbid"` read as
  `extra="ignore"` everywhere and nothing strict (a DTO declared
  `strict=True` still reads a UUID published as a string), so aliases,
  discriminators and validators behave as in the class and run once, and
  every value is an instance of its declared class. That is what makes an
  added optional field safe for an old reader anywhere in the payload, and
  the wire schema says so: it carries no `additionalProperties: false`. A
  DTO open to extra keys (`extra="allow"`) keeps them as data.
- **Removing a default breaks.** `tags: list[str] = Field(default_factory=list)`
  becoming `tags: list[str]` leaves the schema's `required` list unchanged,
  but a producer from before `tags` existed still sends payloads without it,
  and only a defaulted reader fills it: losing the default fails with
  SANDLOOM-T005 (adding a default to a required field is compatible). The
  `"x-sandloom-default"` mark belongs to the framework: a `json_schema_extra`
  that sets it is refused at class definition
  ([SANDLOOM-B015](../reference/error-codes.md#sandloom-b015)).
- **Anything else** (a renamed, removed, retyped or newly required field) is
  a new channel, `<module>.<entity>.<verb>.v2`, with its own producer class.
  Publish both versions in one unit until no live consumer reads `.v1`:

```python
from sandloom.sdk.events import publish_event_versions

await publish_event_versions({
    ItemCreated.channel: v1.model_dump(mode="json"),
    ItemCreatedV2.channel: v2.model_dump(mode="json"),
})
```

`sandloom streams consumers` shows the live readers per channel version. See
[Change an event's payload](../guides/event-versioning.md).

### The bus

`EVENT_BUS_BACKEND` selects the transport of the default `eventbus` instance:
`auto` (default: `streams` in production, `memory` otherwise), `streams`
(Redis Streams: durable groups, ack, redelivery, DLQ), `redis` (Pub/Sub,
volatile) and `memory` (in-process, for dev and tests). Volatile buses are
refused in production, and durable consumers are refused on Pub/Sub, unless
the deployment accepts the risk `volatile_event_bus` (`SANDLOOM_ACCEPTED_RISKS`).
See [Capabilities and infrastructure](capabilities-and-infrastructure.md).

### Testing

`published(ItemCreated)` lists what the code under test published;
`deliver(event)` (an instance or a channel and payload, `times=` for
duplicates) runs your consumers as the runtime would. See the
[testing reference](../reference/testing.md).

## What breaks it

| Code | When |
| --- | --- |
| [SANDLOOM-B015](../reference/error-codes.md#sandloom-b015) | a channel that is not `<module>.<...>.vN`, a field outside the closed set of wire shapes, a reserved field name, two producers of one channel, a stated producer outside its module |
| [SANDLOOM-B016](../reference/error-codes.md#sandloom-b016) | an `@on` handler that is not `async def`, does not take exactly the event, has a duplicate name, or names a tenant field that is not a `UUID` field |
| [SANDLOOM-C025](../reference/error-codes.md#sandloom-c025) | a view incompatible with (or unverified against) its producer, or `.publish()` on a view |
| [SANDLOOM-T005](../reference/error-codes.md#sandloom-t005) | a breaking schema change on the same `.vN` |
| [SANDLOOM-R002](../reference/error-codes.md#sandloom-r002) | `.publish()` outside a unit |
| [SANDLOOM-R003](../reference/error-codes.md#sandloom-r003) | a publish on a channel with no registered schema |
| [SANDLOOM-R004](../reference/error-codes.md#sandloom-r004) | a payload that does not match its channel's schema (validated before the outbox write, in every environment) |
| [SANDLOOM-C007](../reference/error-codes.md#sandloom-c007) | a best-effort publish without its marker |

`sandloom explain <code>` prints the cause and the fix.

??? note "Guarantees, precisely"
    - **Outbox (G-OUTBOX-01, 02, 04 to 07, 10).** Inside a unit the event row
      commits with the change or not at all. It is preflighted (channel,
      schema, size) before it is written, so an event the bus would reject is
      refused to the caller instead of committed. A failed post-commit
      dispatch never fails the committed operation; the relay retries with
      capped backoff and dead-letters after `OUTBOX_RELAY_MAX_ATTEMPTS`. The
      label is **intent only** for atomicity: the row is atomic with the
      change, the delivery is eventual.
    - **Delivery (G-OUTBOX-05, G-OUTBOX-15, G-EVENT-10 to 12).** At least
      once, no ordering, stable envelope id. `no_loss` holds with the outbox
      and a durable bus, qualified: a delivery may be **parked** in the DLQ.
      Stream retention bounds how long a consumer may be offline.
    - **Consumer effects (G-EVENT-05 to 08, G-INBOX-01 to 03).** With
      `durable=True`, `no_dup` holds iff the inbox is in the same database as
      the effect and inbox retention is at least stream retention plus one
      day (G-WINDOW-03, checked by `sandloom outbox purge --inbox-days`).
      `no_loss` is always qualified: `dead_letter=True` parks on poison or
      after the delivery budget; `dead_letter=False` parks only on an explicit
      `PoisonEventError` (G-EVENT-07).
    - **Groups and readiness (G-EVENT-01 to 04).** Module-namespaced groups,
      startup refusals on collisions, required consumers gate startup and
      `/ready`.
    - **Schemas (G-EVENT-13, G-OUTBOX-06, C-EVENT-01, C-EVENT-03).**
      Re-registering an identical schema is a no-op; dual publishing commits
      every version together; `sandloom streams` reports live readers per version.

    Labels are derived from the configured mechanism, never assumed. The full
    matrix is in [ADR-0005](../adr/0005-effect-model.md); every id above is
    in the [guarantee ledger](../reference/guarantees.md). See also
    [Guarantees and labels](guarantees.md).

??? note "How it works"
    - `class E(Event, channel=C)` is the L1 form of
      `core.register_event_schema(C, "E", E.model_json_schema())`;
      `await e.publish()` is `await publish_event(C, e.model_dump(mode="json"))`;
      `@on(E)` is `core.register_event_consumer(C, callback, name=...,
      bind_tenant_from="tenant_id", ...)` with a callback that decodes
      `envelope["payload"]` into `E` and attaches `EventMeta`. An L2 module
      writes those calls in a hand-written `register`; the registrations are
      equal (`test_l1_equals_l2_registration`).
    - The envelope is unchanged: `id`, `channel`, `schema_version`,
      `trace_id`, `emitted_at`, `payload`.
    - On the request path the post-commit dispatch is handed to a background
      coalescer, off the response; scripts and tasks wait for it. Tests that
      assert delivered state let the harness drain it.
    - The outbox, inbox and relay live in the kernel (`src/sandloom/core/`);
      each database owns its own outbox, marked by the dispatcher and relay of
      the database that produced the row.

## Design rationale

- [ADR-0005: the effect model](../adr/0005-effect-model.md) (outbox, inbox,
  guarantee windows).
- [ADR-0010: Public API 1.0](../adr/0010-public-api-1.0.md) §3 (typed events
  and consumers) and §6 (derived labels).
- [Event contracts reference](../reference/event-contracts.md) (naming rules,
  the registry, evolution policy).
