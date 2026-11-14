# Research: audit of the capability contracts

Read-only study at `aefd3e52`, 2026-11-14, for [the design](design.md), by reading code and
grep; no tests were run. Paths are relative to `src/sandloom/` unless they start with
`docs/`, `packages/` or `public_api_baseline.json`. All six built-in specs are `v1`. Every
driver shares `InfraProvider` (`core/infra/contracts.py:536-610`): `from_config`,
`features_for`, `features()`, a no-op `aopen`, a `health` that answers ok, an idempotent
`aclose`, `contract_version` (no default, :548), `options_schema`, `required_imports`,
`install_hint`.

## Per capability

**database.** Not public. Module door is SQLAlchemy `AsyncSession` (`sdk/_prim/data.py:24`),
not a Protocol. `DatabaseProvider` (`contracts.py:613-693`) has one abstract member,
`uow_factory`; the rest is a kernel-only second contract through optional stores
(`OutboxStore`, `AuditStore`, `CommandLogStore`, `RequestLogStore`), each taking
`session: Any` (:131, :346, :436, :504). `OutboxHandle.raw: Any` (:61), `execution_adapter() ->
Any` marked "PROTOTYPE" (:687), `ensure_model_schema(metadata: Any)` (:677). 18 features
(`capabilities.py:160-172`). `SqlDialect` contract 2 has a kit; the provider has none. The
`sqlalchemy` driver overrides `features()` and sets no `contract_version`.

**cache.** Not public; used by modules and heavily by the kernel. Protocol of 13 methods
(`contracts.py:696-727`); the ABC docstring still says "these five methods" (:735). Strings
only, whole-second TTLs. ABC extras not in the Protocol: `set_subject_state` (:895),
`raw_client() -> Any` (:920). Features mix meanings: `cas` means the method exists (else
`NotImplementedError`, :814); `get_many`, `incr_window`, `set_max` mean "one round trip" with a
portable fallback — and the `set_max` fallback without `cas` is not monotonic (:886-891). No kit.

**objectstore.** Public in practice: `driverkit` exports `ObjectStoreProvider` and
`ObjectStoreConformance`. Best-written semantics of any capability (key grammar,
`FileNotFoundError`, delete of an absent key succeeds, a failed put keeps the old object). Error
types `InvalidObjectKeyError`, `UploadTooLargeError`, `ObjectTooLargeError` (:1075-1084) are
exported only from `driverkit`, so a module sees them as `ValueError`. `ObjectContent` still
accepts a legacy no-argument `read()`. Kit gaps (`driverkit/testing.py:900-966`): its fixture is
the module Protocol, so it never checks `contract_version`, features, `health` or `aclose`;
nothing exercises `metadata`, `presigned_get`, `url()`, size errors or the unsupported-feature
error; the test driver returns a `memory://` URL without advertising `presigned_get` and passes;
no meta-test against broken drivers; no signature pin. Docs and ADR-0011 decision 6 still say
it is private. The local driver raises `FeatureUnsupportedError` (a registry error) from `url()`.
The S3 driver imports `boto3` at module top and declares no `required_imports`.

**taskqueue.** Not public. Protocol: `enqueue(task, payload, *, delay_seconds,
idempotency_key) -> str` (:1210); ABC adds `raw_backend() -> Any` (:1253). Features `delay`
and `idempotency_key` are parameters of the required method and nothing checks them;
`periodic` is a guarantee. No kit. `idempotency_key` depends on the kernel's cache
(`core/drivers/celery_taskqueue.py:30` → `core/tasks/enqueue.py:83`), so a third-party queue
cannot offer it without `core`.

**eventbus.** Not public; kernel only. `EventBusProvider.bus() -> Any` (:1276). The real
contract is `EventBusProtocol` (`core/eventing/bus_base.py:52-79`): constructor takes the
schema registry; `preflight`, `publish_event(prevalidated=)`, `subscribe_once`, `close`.
Duck-typed flags parallel to features: `durable`, `in_process`, `supports_batch_publish`,
`supports_prevalidated`, `native_batch_publish` (`bus_base.py:88-102`), read with `getattr` in
`consumers/callbacks.py:312`, `unit_of_work/dispatch.py:106,145,677`, `tasks/relay.py:567`;
`revocation_applier.py:456,479` reaches `bus.transport` and `bus.dead_letters`. Excluded from
`aopen_all` (`registry.py:1164`). Features `publish` and `subscribe` are effectively required
methods; `volatile` and `durable` are not checked as exclusive. No kit.

**notification.** Public. `send(Notification) -> None` (`contracts.py:1307-1326`); channels a
closed `Literal`; error rule "raises on a delivery failure" with no type. Kit complete in form
(contract, version, unknown features, drift, each channel, body kept out of logs, health,
second close), run against broken drivers (`test_driverkit.py:397-497`), signatures pinned
(:337). It does not test the delivery-failure error or `dedupe_key`.

**Private and experimental** (`core/infra/extra_capabilities.py`): no Protocol, kit or driver
except `document_store`'s memory driver. `lock`: `acquire(key, ttl) -> str|None`, `release`,
no renew. `secrets`: `get -> None`. `workflow`: `result -> Any`. `graph_store`: Cypher or
Gremlin strings. `document_store`: Mongo-shaped `query`; the kernel uses a
`ConditionalWriteStore` Protocol not on the ABC (`core/projections/stores.py:29-52`). The module
docstring still shows the removed `register(registry)`.

**Baseline.** `public_api_baseline.json` pins `sandloom.driverkit` member names, not method
signatures; `sandloom.driverkit.testing` is absent.

## A. Inconsistencies between capabilities

1. Absence: cache `get` → `None`, secrets `get` → `None`, objectstore `get` → `FileNotFoundError`.
2. "Unsupported" three ways: `NotImplementedError`, `FeatureUnsupportedError` (not exported by
   `driverkit`), and nothing (taskqueue `delay`).
3. Four feature styles: availability (`cas`, `metadata`), performance (`get_many`, `set_max`),
   guarantee (`periodic`, `durable`, `transactional_outbox`), required verbs (`publish`,
   `subscribe`, `transactions`).
4. Built-ins declare features twice (constant passed to `register_driver` in
   `core/drivers/bootstrap.py:88-157` and an overridden `features()`), the pattern ADR-0011
   decision 4 removed.
5. Built-ins skip the `contract_version` check; notifier uses `"v1"`, dialects integer `2`; the
   drift check runs only on a strict registry (`registry.py:769`).
6. Four kit shapes: `make_provider` fixture (notifier), a `store` Protocol (objectstore), class
   attributes (dialect), a function (`conditional_write_conformance`).
7. Health: Redis reports `str(exc)`, S3 the type name; Celery caches its probe; event-bus
   health skipped for `memory`; `HealthResult.details` has no redaction rule.
8. Accessors: `cache()` checks the type, `notifier()` casts, `objectstore()`/`taskqueue()` use
   `resolve` (`core/infra/access.py:44-122`); `security_cache()` returns the ABC.

## B. Where the contract leaks a backend

- Redis: `set_nx_get`, `incr_window`, `set_max` are Redis command shapes; `subject_state` is a
  Lua script lifted into the ABC (`contracts.py:851-918`); whole-second TTLs; `raw_client` for
  pub/sub; `deployment_identity` (`INFO server`) called by `production_profile.py:289` outside
  any contract.
- Celery: tasks are `celery.shared_task` (`sdk/_prim/tasks.py`); the worker needs a Celery app
  via `raw_backend` (`core/worker.py:106-123`); `periodic` means beat plus the lease; `enqueue`
  covers only the producer.
- Filesystem: `FileNotFoundError` as the objectstore absence error; filename constraints in the
  Protocol docstring.
- SQL/ORM: `session: Any`, `has_pending_changes` ("ORM identity map"), `metadata: Any`.
- Event bus: schema validation and payload limits bundled into the transport; `bus.transport`
  reached directly.
- Experimental: Mongo-shaped queries, Cypher/Gremlin strings.

## C. "Two doors, one class" does not hold for kernel consumers

There is a third door held together by `getattr` and `Any`:

- atomic counters and a fenced, renewable lease built on the cache (`core/beat_lease.py:148-153,202`);
  the private `lock` cannot replace it;
- revocation and blocks call `set_subject_state`;
- pub/sub through `raw_client()` (`core/contract_cache_shadow.py:845-850,885-887`). **Bug,
  confirmed by reading:** the guard `getattr(provider, "raw_client", None) is None` (:869) is
  never true, because the base class defines the method; on a non-Redis cache the listener loops
  on `NotImplementedError`, flushes the shadow cache every ≤10 s and drops broadcasts silently;
- consumer groups, durability and batch publish on the bus through duck-typed flags;
- the worker through the Celery app object;
- the four `DatabaseProvider` stores;
- taskqueue `idempotency_key` through the kernel's cache.

The kernel needs explicit kernel-facing Protocols — a lease, an atomic monotonic register, a
broadcast channel, a durable stream with consumer groups, a worker host — each with features and
a kit, so that `getattr` and `raw_*` disappear.

## D. What a third-party driver author is missing

1. An error model: no exported hierarchy for not found, unavailable/transient, unsupported, too
   large; modules cannot import the object-store errors.
2. Written semantics for cache (TTL precision, eviction, consistency), taskqueue (delivery,
   retries, codec, `delay_seconds` without `delay`), eventbus (ordering, at-least-once, ack).
3. Kits for cache, taskqueue, eventbus; the objectstore kit's lifecycle, version, feature and
   error checks, meta-tests and signature pins; `driverkit.testing` in the baseline.
4. Docs that match the code; a reference driver for each capability (only notification has one);
   the S3 package following the dependency guidance.
5. A lifecycle contract: who calls `aopen` and when, shared clients, health timeout and redaction.
6. A rule saying which kind a feature is, and a check of declaration against behaviour beyond
   notifier channels.
