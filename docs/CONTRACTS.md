# Contracts and event schemas

This compatibility page keeps established guarantee-ledger anchors. The
current module-facing API is `sandloom.sdk`; use the guides below rather than
legacy `sandloom.core` or `sandloom.shared.authz` imports.

## Sync service contracts

A provider declares a stateless async `@contract` class with wire-portable
arguments and results. Consumers use `Contract(REF)` through generated stubs;
DTOs live in the shared schema package. Provider runtime owns the unit and
transaction. See [contracts](concepts/contracts.md) and the
[SDK reference](reference/sdk/contracts.md).

## 5. Overwrite & Evolution (Current Rules)

Typed events use versioned channels and have one producer. A compatible change
within `.vN` only adds optional wire fields. Removing, renaming or retyping a
field, or adding a required field, needs a new version and a consumer rollout;
registration and generated-schema checks enforce the applicable boundaries.
See [typed events and evolution](concepts/events.md),
[adding an event](guides/add-event.md) and the
[event SDK reference](reference/sdk/events.md).

The old hand-written module registration, direct bus access, `CoreFacade` DI
examples and draft schema-registry plans are retained in the preparation
repository. They are not the current module-author contract.
