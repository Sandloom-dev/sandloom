---
title: sandloom.driverkit
description: The API for driver packages - the notification contract, capability specs and references, and the conformance kit. Business modules never import it.
type: reference
---

# `sandloom.driverkit`

The API a [driver package](write-a-driver.md) implements. Most code should use
[`sandloom.sdk.infra`](../reference/sdk/infra.md) instead: a business module may not import this
package ([SANDLOOM-C004](../reference/error-codes.md#sandloom-c004)), and the kernel never imports
it. It ships inside `sandloom`.

A module-facing contract is public with its conformance kit, so `cache` and `taskqueue` are
not here yet. `notification` and `objectstore` are.

The kernel-facing contracts are final and exported before their kits, so a driver package can
build on them. A driver is conformant only once it passes its kit. A module never resolves a
kernel-facing capability ([SANDLOOM-B041](../reference/error-codes.md#sandloom-b041)).

A breaking change is a new contract version ([versioning](../contributing/versioning.md)).

## `sandloom.driverkit`

| Name | What it is |
| --- | --- |
| `NotifierProvider` | The contract of the `notification` capability: implement `async send(notification)`. |
| `ObjectStoreProvider` | The contract of the `objectstore` capability: implement `async put(key, data, *, content_type, metadata)`, `get(key)`, `delete(key)` and `url(key)`. Keys are logical names, never filesystem paths. |
| `ObjectContent` | What `put` accepts as `data`: bytes, or an async reader whose `read(size)` returns up to `size` bytes and `b""` at the end. A reader whose `read()` takes no size is not accepted. |
| `validate_object_key` | Raises `InvalidArgument` when a key is not a portable relative key. Call it first in every operation that takes a key. |
| `iter_object_chunks` | Reads an upload, bytes or stream, in chunks of the size you give. |
| `InfraError` | The base of every error a capability operation raises on purpose: `capability`, `instance` and the class attribute `retryable`. A driver maps its backend's errors onto its subclasses; any other exception from an operation is a driver bug. None of them is also a built-in exception such as `ValueError`. |
| `NotFound`, `AlreadyExists`, `InvalidArgument`, `TooLarge`, `PermissionDenied` | An addressed resource is absent (an object `get`), a create found an existing resource, a key, value or option is outside the contract, a value is over a limit, the backend refused the credentials. A key-value miss is `None`, never `NotFound`. |
| `Unavailable`, `Timeout` | Transient: the connection, a failover, an overload, or an operation past its bound. Both are `retryable`. |
| `Unsupported` | A call needs a behaviour feature the instance does not declare; `feature` names it. A module that declares the feature it needs is refused at load instead (`FeatureUnsupportedError`, SANDLOOM-B033/B037). |
| `AtomicStoreProvider` | The contract of the kernel-facing `atomic_store` capability: the security marks and idempotency records. |
| `LeaseProvider`, `Lease`, `LeaseHolder` | The contract of the kernel-facing `lease` capability, the fenced lease it grants and its current holder. |
| `BroadcastProvider`, `BroadcastMessage`, `Subscription` | The contract of the kernel-facing `broadcast` capability (online, at most once), one received message and a live subscription. |
| `Notification` | One outbound message: `channel`, `recipient`, `subject`, `body`, `data`, `dedupe_key`, `body_fields`. |
| `InfraProvider` | The base of every contract. A driver sets `contract_version` (the integer major of its capability), `supported_features` and optionally `performance_hints` (speed claims such as `{"get_many": "one_round_trip"}`, never a feature), `development_only` (refused in production) and `options_schema`; it may override `features_for(options)`, `test_isolation(options, worker_id)`, `aopen()`, `health()`, `aclose()` and `isolation_identity()`. It never overrides `features()`. |
| `HealthResult` | What `health()` returns. |
| `CapabilitySpec` | Declares a capability: `name`, `version` (what a module may pin), `major` (the `contract_version` a driver states; default: the major of `version`), `provider_contract`, `config_schema`, `feature_kinds` (each feature and whether it is a `behaviour` or a `guarantee`), `features`, `removed_features` (a previous major's feature names, each mapped to its replacement, which a refusal names), `kernel_only` (a kernel-facing capability modules cannot resolve), `description`, `stability`. A driver loads only for a spec whose major is readable. |
| `CapabilityRef` | A typed key callers resolve a capability with: `CapabilityRef[VectorIndex]("vector_index.v1")`. |
| `LockStatement` | What a SQL dialect renderer's `lock_statement` and `unlock_statement` return: the `statement`, built once per key shape, and the `parameters` that carry the key. The kernel runs `execute(lock.statement, lock.parameters)`. |
| `DbapiAdapter` | One DBAPI as the `sqlalchemy` driver sees it; the base class is the plain path. A subclass sets `name`, `contract_version` (1) and `capabilities` and overrides what it speeds up ([Add a DBAPI adapter](write-a-driver.md#add-a-dbapi-adapter)). |
| `DBAPI_CAPABILITIES` | Every capability of a `DbapiAdapter`. A driver package may claim `read_only_policy` and `reconnect_backoff`; `merged_begin`, `raw_executor` and `connection_recovery` are the kernel's own (SANDLOOM-B025). |
| `register_dbapi_adapter` | Registers an adapter for the DBAPI it names, replacing the one Sandloom ships; call it before the first engine is built. |

## `sandloom.driverkit.atomic_store`

The argument checks and the subject-state value domain every `atomic_store` driver shares,
so one key, TTL or value is refused the same way whatever the backend. They are the
kernel port's own functions, re-exported:

| Name | What it is |
| --- | --- |
| `check_key`, `check_value`, `check_ttl` | Call them first in every operation. `InvalidArgument` for a key, value type or TTL outside the contract (a TTL is 1 to `MAX_TTL_SECONDS`, 10 years), `TooLarge` for a value over the limit you pass. |
| `check_subject_state`, `parse_subject_state`, `subject_state_rank` | The domain of `set_subject_state` (`v1:after:<decimal>` / `v1:blocked:<decimal>`) and its exact ranking: every `blocked` above every `after`, within a kind the instant compared as a decimal, never a float or text. A value over `MAX_SUBJECT_STATE_CHARS` is `TooLarge`, and a stored one that long is corrupt without being matched. |
| `MAX_KEY_BYTES`, `DEFAULT_MAX_VALUE_BYTES`, `MAX_TTL_SECONDS`, `MAX_SUBJECT_STATE_CHARS`, `SUBJECT_STATE_RANKS`, `ATOMIC_STORE_PERFORMANCE_HINTS` | The bounds, the kind ranks and the performance hints a driver declares. |

A reply lost after a command ran is `Unavailable`: the error model has no "may have happened"
class. A driver therefore never re-sends `add`, `add_or_get`, `compare_and_set` or
`compare_and_delete` itself. Run twice they would answer `False` or the caller's own value to the
caller that won. A driver may re-send the idempotent ones only: `get`, `set`, `delete`,
`set_subject_state`.

## `sandloom.driverkit.testing`

Each provider kit is a pytest class with one test per obligation. Subclass it and
override the `make_provider` fixture to return an unopened provider. Override
`make_failing_provider` too to check a backend that fails. Every kit runs the shared
harness checks first
([Check it against the contract](write-a-driver.md#4-check-it-against-the-contract)).
The kits need `pytest` and `pytest-asyncio`.

The names of the kits still to come are listed already. Until its capability lands, such
a name raises `AttributeError` naming the work that delivers it:
<!-- lookup -->

- `AtomicStoreConformance`
- `CacheConformance`
- `DatabaseProviderConformance`
- `EventTransportConformance`
- `LeaseConformance`
- `TaskQueueConformance`

| Name | What it is |
| --- | --- |
| `BroadcastConformance` | The kit of a `broadcast` driver; point `make_provider` at a test backend. It publishes on channels of its own. Set `payload_limit_bytes` to the limit the driver declares (at least 1 MiB). Override `interrupt_connections(provider)` to cut the provider's connections, as an outage would, to run the outage group. `delivery_seconds` and `quiet_seconds` set the waits. |
| `NotifierConformance` | The kit of a `notification` driver. Override `recipients` for your test backend. Each channel is a behaviour feature: an undeclared channel raises `Unsupported(channel)`. |
| `ObjectStoreConformance` | The kit of an `objectstore` driver; point `make_provider` at a test namespace. Set `upload_limit_bytes` and `read_limit_bytes` to the test instance's limits. A driver declaring `metadata` overrides `read_metadata(provider, key)` to read them back. |
| `SqlDialectConformance` | A pytest class for a SQL dialect renderer. Subclass it and set `renderer` and `dialect`. It compiles each form the renderer writes, the optional `prefix_pattern` / `prefix_match` pair included. A renderer declaring `json_path` overrides `read_json_paths(document, paths)` to run it on the database; one stating `aware_times` overrides `read_aware_time(value)` to read an aware time back from it. |
| `JSON_PATH_DOCUMENT` | The JSON text `read_json_paths` stores, numbers as written. |
| `JSON_PATH_READS` | What `json_path` must read at each path of `JSON_PATH_DOCUMENT`: the scalar's JSON text as stored. |

## Entry points

| Group | Name | Value |
| --- | --- | --- |
| `sandloom.drivers` | `<capability>.<driver>` | `package.module:DriverClass` |
| `sandloom.capabilities` | `<capability>` | `package.module:SPEC` |
