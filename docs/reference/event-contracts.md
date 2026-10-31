---
title: Event Contracts
---

# Event Contracts

This is the normative reference for event channel naming, schema registration,
validation, and the **schema evolution policy**. For the conceptual overview see
[Events and consumers](../concepts/events.md).

## 1. Naming convention

Channels follow a strict, versioned dotted pattern:

```
<domain>.<component>(.<sub>)*.v<major>
```

Examples: `identity.auth.v1`, `billing.credits.spent.v1`,
`core.system.heartbeat.v1`.

Rules:

- Lowercase alphanumeric + underscore in each segment.
- The final segment **must** be `v<digits>`. `v0` is discouraged.
- Wildcard **subscriptions** may use a trailing `.*` (e.g. `identity.user.*`);
  **publishing** must always be concrete (no `*`).

## 2. Schema registry

An `EventSchema` is registered per `(channel, version)`. A module declares
its channels as typed `Event` classes in `events.py`; the class's fields are
its JSON schema, and the module registers it when it loads:

```python
--8<-- "examples/tutorial/tasklist/modules/tasks/events.py"
```

Level-2 code (a channel with a hand-written schema) registers it in
`setup(core)` through the [`CoreFacade`](sdk/infra.md):

```python
core.register_event_schema(
    channel="identity.user.created.v1",
    name="user_created",
    schema={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "email": {"type": "string", "format": "email"},
        },
        "required": ["id", "email"],
        "additionalProperties": False,
    },
)

core.register_event_schema_obj(event_schema)   # bulk register an instance
schemas = core.list_event_schemas()            # introspect
```

## 3. Validation modes

Validation runs during `publish` / `publish_event` for versioned channels
(`*.vN`):

A payload that fails its schema always raises `ValueError` and the publish is
aborted (failure counter ++), in every environment — there is no soft mode.

A publish on a versioned channel with **no registered schema** is refused too,
in every environment (`SANDLOOM-R003`; `sandloom check` rule `event-schema-channels`
catches it statically). A module declares its channels as typed `Event`
classes; a test publishing on a channel no module owns uses
`register_event_schemas` from `sandloom.sdk.testing`. A validation error lists every
violation with its JSON pointer (`SANDLOOM-R004`). Counters:
`sandloom_schema_validation_success_total`, `sandloom_schema_validation_failures_total`.

!!! note "Production payload cap"
    In production the framework defaults a 256 KiB `EVENT_PAYLOAD_MAX_BYTES` —
    unless the operator set the env var explicitly. This prevents an oversized
    event from being committed to the outbox and then failing forever at
    dispatch.

## 4. Evolution policy

This is the **stable** compatibility policy (it supersedes the earlier "Draft").

Within the same major version (`.vN`), these changes are **allowed in place**
(non-breaking — existing consumers keep working):

- Add an **optional** property (not in `required`).
- Add descriptive metadata (`description`, `examples`, `deprecated`).

These changes **require a new major version** (`.vN` → `.vN+1`):

- Remove a property (required or optional).
- Add a **required** property.
- Change the type of an existing property.
- Add **or** remove an enum value. An added value is breaking too: a consumer
  deployed in another service with the old schema (or an exhaustive `match`)
  rejects or mishandles it, and producer and consumers cannot be upgraded
  atomically — publish the new shape as `.vN+1` (dual-publish with
  `publish_event_versions` during the rollout).
- Tighten a constraint (e.g. add `maxLength`, narrow a pattern).

Enforcement at registration time: re-registering the same `(channel, version)`
with a non-identical, breaking schema raises `ContractValidationError` with a
diff summary; an identical schema (or filling in a previously `None` schema) is
idempotent and allowed. Only additive optional properties (and metadata) are
accepted in place.

**Rationale:** producer changes stay safe for existing consumers — only additive,
opt-in data can appear without forcing a consumer upgrade. A breaking change is
expressed as a new versioned channel, and producers can dual-publish `vN` and
`vN+1` during a migration window.

## 5. Core example schemas

Registered by the framework at startup as reference patterns:

- `core.system.heartbeat.v1`
- `core.system.module_loaded.v1`

## 6. Standard module channels

All bundled channels are additive-only within `v1`. Event payloads are durable
(outbox) and fan out to every consumer, so they carry **no PII** — consumers
resolve emails through the owning module by id.

| Channel(s) | Payload | Notes |
| --- | --- | --- |
| `identity.*.v1` (15 channels, see [Identity](../modules/identity.md#published-events)) | envelope `{event_id, event_type, actor, payload}` | one shared schema; per-event fields live in `payload` |
| `identity.user_registered.v1` | `payload: {user_id}` | no `email` |
| `identity.user_sessions_revoked.v1` | `payload: {user_id, reason, tokens_valid_after}` | `reason`: `deactivated` / `password_reset` / `password_changed` / `role_revoked`; `tokens_valid_after` is a unix timestamp — drop cached sessions for the user |
| `tenancy.organization_{created,updated,deleted}.v1` | `{organization_id, actor_id?}` | `organization_deleted` is published by the owner-only `DELETE /api/v1/orgs/{org_id}` |
| `tenancy.member_{added,removed,role_changed}.v1` | `{organization_id, user_id, role?, actor_id?}` | an ownership transfer publishes two `member_role_changed` events |
| `tenancy.invitation_{created,accepted,revoked}.v1` | `{organization_id, invitation_id, role?, actor_id?}` | `email` is a deprecated optional schema property, no longer emitted |
| `billing.subscription.{created,updated,canceled}.v1`, `billing.credits.{added,spent}.v1` | `{event_id, occurred_at, customer_id, tenant_type, tenant_id, data}` | `credits.spent` `data`: `{amount, balance, reason}` |

`billing.credits.v1` is a **service contract**, not an event channel — see
[Billing](../modules/billing.md#service-contracts).

## 7. Delivery guarantees

See [Events and consumers](../concepts/events.md#at-least-once-delivery-and-exactly-once-effects)
for at-least-once delivery, the stable envelope id, dead-lettering, and the
durable-inbox pattern for exactly-once effects.
