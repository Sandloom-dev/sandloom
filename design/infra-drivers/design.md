# Design: infrastructure out of the kernel, behind drivers

Status: **proposal for the owner, 2026-11-14**. tm: `infra-drivers` (under `infra-agnostic`).

Built from three studies, all at `aefd3e52`:

- [Kernel infrastructure inventory](research-kernel-inventory.md): where the kernel still
  talks to Redis, Celery, PostgreSQL tools and others without a capability.
- [Contract audit](research-contract-audit.md): the state of every capability contract,
  feature, kit and escape hatch.
- [External practice](research-external-practice.md): Dapr, Go CDK, Watermill,
  Celery/Kombu, Rails, Laravel, Django, OpenDAL, Spring, Kleppmann on leases.

Settled base this design keeps: ADR-0011 and its three amendments (two doors, entry points,
declared features, kits, `contract_version`, driver availability, `periodic`), the owner's
decision on module infrastructure dependencies
(`design/kernel-boundary/owner-decision-infra-deps.md`), and positioning row 10
(infrastructure behind capabilities, drivers chosen in configuration).

## The problem

The SQL work (renderer contract 2, `DbapiAdapter`, SQLite) took PostgreSQL out of the
kernel's runtime. The rest of the infrastructure is still the kernel's code:

1. **The kernel has a third, unwritten contract.** Modules see `sandloom.sdk.infra`
   Protocols, drivers see the provider ABCs. The kernel uses operations neither door
   shows, held together by `getattr` and `Any`:
   - the revocation mark, blocks, idempotency, enqueue dedupe, RPC replay and the consumer
     inbox need durable atomic registers (`set_subject_state`, `set_max`, `add`, CAS,
     compare-and-delete). They live as extras on the `cache` ABC, partly as Redis Lua.
   - the beat lease builds a fenced, renewable lease from `incr` + `add` + CAS. The private
     `lock` contract cannot replace it: no renewal, no fence.
   - contract-cache invalidation does pub/sub through `cache.raw_client()`.
   - the event bus is `EventBusProvider.bus() -> Any`. The real contract is
     `EventBusProtocol` plus duck-typed flags (`durable`, `supports_batch_publish`, …) read
     with `getattr`, and the ops tools reach `bus.transport` and `bus.dead_letters`.
   - the worker needs a Celery app through `raw_backend()`.
2. **Built-in drivers live inside the kernel, below its own layers.** About 3,500 lines of
   Redis Streams and pub/sub code sit in `core/eventing`, which is below `drivers` in the
   layer contract. The Celery worker, the beat lease, the async runner and GC tuning
   (about 1,400 lines) are in `core/`. The public `shared_task` and `cron` of `sandloom.sdk`
   are Celery's.
3. **Only two contracts can have outside drivers.** `notification` and `objectstore` have a
   public provider, a kit and an external package shape (S3 proved it). `cache`,
   `taskqueue`, `eventbus` and `database` have no kit and no export from
   `sandloom.driverkit`.
4. **Contracts disagree with each other.** Absence is `None` in one capability and
   `FileNotFoundError` in another. "Unsupported" is spelled three ways, one of them silent.
   Features mean four different things: an available method, one round trip, a guarantee,
   or a required verb. Built-ins declare features twice and skip the `contract_version`
   check.
5. **The ratchets cannot see it.** They look for imports and constructors. Redis commands on
   an injected client, and the kernel's own `raw_client` call, pass all of them.

Found on the way (fixed in the first slice, not design questions):

- `core/contract_cache_shadow.py:869` checks `getattr(provider, "raw_client", None) is None`.
  That is never true, because the base class defines the method. On any non-Redis cache
  the listener loops on `NotImplementedError` and flushes the shadow cache every ≤10 s.
- `sandloom_s3` imports `boto3` at module top and declares no `required_imports`, so a
  missing SDK is B022 instead of the B037 the 2026-11-11 amendment promises.
- `core/testing/session_checks.py` and `resources.py` import `redis` at top level, and the
  pytest plugin imports them on every run, so an app without the `redis` extra probably
  cannot run its tests (import verified, full run not).
- `docs/infrastructure/driverkit.md` and `write-a-driver.md` say `objectstore` is not
  public; the code exports it and its kit.
- There is no `otel` extra although `core/observability/tracing.py` imports the exporters.

## What everyone else does

The external study converges on six rules:

1. **Separate capabilities by semantics, not by backend.** A cache may evict; a lease has an
   owner, an expiry and a fence; a durable stream redelivers; a broadcast is online only.
   Dapr gives each its own building block; Laravel put locks inside the cache and tied them
   to eviction.
2. **A feature is an optional behaviour of the same call shape.** Dapr's state stores
   (`ETag`, `transactional`, `TTL`), OpenDAL's capability struct and Go CDK's harness flags
   all gate tests by the declared feature.
3. **A small shared error taxonomy**, acted on by code, never by message (Go CDK's
   `gcerrors`). Misses are results, not exceptions. Unsupported options raise, never no-op
   (Django `supports_defer`, Laravel `BadMethodCallException`).
4. **Escape hatches exist but are discouraged and tested** (Go CDK `As()`): code that uses
   one must degrade when the type does not match.
5. **Tiers come from conformance, not from where the code lives** (Dapr alpha, beta,
   stable; stable adds reconnect and auth scenarios).
6. **The core ships no service drivers.** SQLAlchemy external dialects, OpenTelemetry
   exporters and Dapr components live in their own distributions and depend on their own
   client libraries.

On locks, Kleppmann: a lease is a lock with a timeout, a paused holder can outlive it, and
only a fencing token makes it safe for correctness. The beat lease is an efficiency lock
already fenced by a counter; that is the shape to keep.

## Decision proposed

### 1. Three doors, each written down

| door | reader | where | what it holds |
|---|---|---|---|
| module door | business modules | `sandloom.sdk.infra` | Protocols of operations a module may call |
| kernel door | the kernel | `sandloom.core.infra.ports` | Protocols of operations only the kernel calls; modules never see them (C004 extended) |
| driver door | driver packages | `sandloom.driverkit` | provider ABCs that implement a module or kernel Protocol plus the lifecycle; the kits in `sandloom.driverkit.testing` |

A capability is either module-facing or kernel-facing, never both with hidden extras. The
kernel calls no `raw_*` method and reads no attribute a Protocol does not declare
(enforced by a ratchet, slice 12).

### 2. The capability set

Module-facing (public, each with a kit):

| capability | contract | features |
|---|---|---|
| `database` | SQLAlchemy `AsyncSession` plus `sandloom.sdk.data` forms; driver side `DatabaseProvider` with typed stores and `SqlDialect` contract 2 | as today (renderer-declared) |
| `cache` | `get`, `set(ttl)`, `delete`, `get_many`, `set_many`, `add`, `incr`. **May evict, may lose data.** Nothing the kernel's guarantees depend on | `cas` |
| `objectstore` | as today (portable key grammar, put/get/delete, failed put keeps the old object) | `metadata`, `presigned_get` |
| `notification` | as today, `send(Notification)` | channels |
| `taskqueue` | Sandloom's own task declaration (`task`, `cron` in `sandloom.sdk`) and `enqueue(task, payload, delay_seconds=, idempotency_key=)` | `delay`, `periodic` |

Kernel-facing (driver-implementable, not callable by modules):

| capability | contract | serves | built-in drivers |
|---|---|---|---|
| `atomic_store` (new) | durable, **never evicting** key-value with TTL: `get`, `add`, `compare_and_set`, `compare_and_delete`, `set_max` (monotonic), `incr_window(key, ttl_on_create)`, `subject_state` read/write | revocation (G-REVOKE-03), blocks (G-BLOCK-01), idempotency (G-IDEM-01..03), enqueue dedupe (G-TASK-01..03), RPC replay, consumer inbox (G-INBOX-03), rate limit (G-HTTP-01/02) | `redis` (noeviction checked in production), `memory` (dev and tests, refused in production), optionally `database` |
| `lease` (new, replaces private `lock`) | `acquire(name, ttl, owner) -> Lease \| None` with a monotonic `fence`, `renew`, `release`; no blocking wait | beat lease (G-OPS-01), saga sweep, Streams group owner | `redis`, `memory`, optionally `database` (row with a fence column) |
| `broadcast` (new) | online-only fan-out: `publish(channel, payload)`, `subscribe(channel)`; no persistence, no replay, declared best-effort | contract-cache invalidation (ADR-0003), the volatile event bus, future websocket rooms | `redis` (pub/sub), `memory` |
| `eventbus` | the durable transport made public: today's `MessageTransport` with typed operations (publish batch, consume with consumer groups, ack, claim stale, dead-letter, trim, replay, inventory). Schema validation and payload limits stay in the kernel above it | outbox relay, consumers, DLQ ops (G-EVENT-03..07, G-EVENT-10) | `streams` (Redis), `memory`; later NATS JetStream or Kafka |
| `worker` (the driver side of `taskqueue`) | `WorkerHost`: run a worker process, run the scheduler while holding a `lease`, process lifecycle hooks | `sandloom worker`, beat (G-INFRA-11, C-WORKER-01..06) | `celery` |

Unchanged and out of this wave: `secrets`, `workflow`, `search`, `graph_store`,
`document_store` stay experimental and private (ADR-0011: each becomes public with its first
driver and kit).

**Why `atomic_store` is not a feature of `cache`.** The guarantees above need a store that
never evicts and never loses an acknowledged write within its durability class. A cache is
allowed to do both. Today the difference is a production check that Redis runs
`noeviction`, applied to the whole cache. Splitting them lets an application use an evicting
cache for module data and a durable store for security state, and lets a driver declare
which it is. It also removes the `set_max` fallback that is not monotonic without CAS.

**Why `lease` is its own capability.** Owner, expiry and fence are not cache semantics
(Dapr, Kleppmann). The kernel already has the fenced shape in `beat_lease.py`; it becomes the
contract, and the Streams group owner's `SET NX` + Lua CAS moves onto it.

### 3. Contract rules (every capability)

1. **Errors.** One hierarchy in `sandloom.sdk.infra.errors`, re-exported by `driverkit`:
   `InfraError` → `NotFound`, `AlreadyExists`, `InvalidArgument`, `TooLarge`,
   `PermissionDenied`, `Unavailable` (transient, `retryable = True`), `Unsupported(feature)`,
   `Timeout`. A miss is a result (`None`, `False`), not an exception. `ObjectNotFound` is both
   `NotFound` and `FileNotFoundError` for one minor release. Each kit asserts the mapping.
2. **Features.** A feature is an optional behaviour or a guarantee, never a required method
   and never a speed hint. Speed hints (one round trip for `get_many`) become
   `performance_hints`, which no correctness decision reads. A call that needs an absent
   feature raises `Unsupported`; a module that needs it declares it and is refused at load
   (B033, B037). No fallback may weaken a guarantee silently.
3. **Declaration.** Once, on the class (`supported_features` or `features_for(options)`),
   built-ins included. Built-ins set `contract_version` and are checked like external ones.
   `contract_version` is an integer major per capability (the notifier's `"v1"` becomes `1`).
4. **Lifecycle.** Written in the driver door: who calls `aopen` (the registry, before the
   first operation), whether a client may be shared (a driver package may share one client
   across its capabilities; the kernel may not reach it), `health()` with a timeout and a
   redaction rule for `details`.
5. **Escape hatches.** `raw_client` and `raw_backend` leave the provider ABCs. A driver
   package may offer a typed `native()` accessor on its own class for application code; the
   kernel and modules may not call it (a C-check and a ratchet).
6. **Kits.** One harness shape for every capability: a `make_provider` fixture; the kit opens
   and closes the provider, checks `contract_version`, the feature declaration and health,
   runs feature-gated groups, and is itself run against broken drivers. Signatures and
   `sandloom.driverkit.testing` are pinned in the API baseline.
7. **Tiers.** `experimental` (implements the contract), `beta` (kit passes in CI against the
   real service), `stable` (beta plus reconnect-after-outage and the auth options). The tier
   is declared on the registration and shown by `sandloom doctor`; production with an
   experimental driver needs an accepted risk (B023 exists for capabilities, extended to
   drivers).

### 4. Packaging: the kernel keeps no service driver

| distribution | holds |
|---|---|
| `sandloom` (kernel) | the contracts, the registry, the kits, the `memory` drivers (refused in production), `local` objectstore, `log` and `smtp` notification, the `sqlalchemy` database driver with the `postgresql` and `sqlite` dialects |
| `sandloom-redis` (new) | `cache.redis`, `atomic_store.redis`, `lease.redis`, `broadcast.redis`, `eventbus.streams`, and the shared client they use |
| `sandloom-celery` (new) | `taskqueue.celery` with its `WorkerHost`, beat under `lease`, the async runner and GC tuning for the worker |
| `sandloom-s3` | as today |
| `sandloom[standard]` | depends on `sandloom-redis` and `sandloom-celery`, so the standard composition is unchanged |

The extras `sandloom[redis]` and `sandloom[celery]` stay and install the new packages, so
install commands do not change. `asyncpg` moves from a base dependency to a `postgres` extra
that the standard composition installs; `psycopg2` stays only where the PostgreSQL ops tools
need it (backup, extract, dev services, the test harness), each import lazy.

The database driver stays in the kernel this wave: the kernel's own stores (outbox, command
log, request log, audit) run on it, and a second database already works through renderers.
Moving it out is a later decision, not needed for independence.

### 5. Configuration

- `core/infra/config.py` stops hard-coding `sqlalchemy`, `redis` and `celery` as defaults. The
  defaults come from the composition: `sandloom-standard` and the `sandloom new` template
  write `INFRA` explicitly; a lean kernel with nothing selected gets `memory` drivers in
  development and a B037 refusal in production that names what to install.
- Flat settings (`cache_url`, `broker_url`, `smtp_*`, `s3_*`) become driver options read by
  the driver's `options_schema`. The old names stay one minor release as aliases with a
  warning (owner question 6).

## Order of work (slices of `infra-drivers`)

Slice 0 needs nothing. Slices 1–2 need the owner's answers. Slices 3–10 are independent of
each other once 1–2 land, except where noted. 11–13 follow.

0. **Fixes found by the research** (the five items above).
1. **ADR-0016 and the contract spec**: this design with the owner's answers, the spec per
   capability with semantics, errors and features.
2. **Shared foundation**: the error hierarchy, the feature rules (a test classifies every
   declared feature), the uniform kit harness with broken-driver meta-tests, built-ins
   declaring `contract_version` once, `sandloom.driverkit.testing` in the baseline.
3. **`atomic_store`**: contract, kit, `redis` and `memory` drivers (the Lua moves from the
   cache driver).
4. **Kernel duties onto `atomic_store`** (revocation, blocks, idempotency, enqueue dedupe,
   RPC replay, consumer inbox, rate limit), then the `cache` contract shrinks to its module
   operations and gets its kit. Needs 3.
5. **`lease`**: contract, kit, drivers; the beat lease and the Streams group owner move onto
   it; the private `lock` spec is removed.
6. **`broadcast`**: contract, kit, drivers; the contract-cache shadow and the volatile bus use
   it.
7. **`eventbus` contract**: `MessageTransport` public with typed operations and features, kit
   against `memory` and `streams`; the `getattr` flags go.
8. **Event ops onto the contract**: `sandloom streams`, `tasks/event_ops.py` and the admin
   platform API use the transport's DLQ, group and inventory operations. Needs 7.
9. **Tasks without Celery in the SDK**: Sandloom's `task` and `cron`, the `WorkerHost` port,
   the taskqueue kit (enqueue, delay, idempotency, periodic under a lease); `worker.py`,
   `beat_lease.py`, `async_runner.py`, `gc_tuning.py` move behind the port. Needs 5.
10. **`DatabaseProvider` contract**: typed stores (no `session: Any`), no `execution_adapter`
    prototype, `_internal/db_provider.py` stops importing driver internals, a provider kit.
    Replaces what is left of `infra-pg-features`.
11. **Packages**: `sandloom-redis` and `sandloom-celery` cut out of the kernel wheel;
    `core/eventing` keeps no transport code; extras and the standard distribution point at
    them; `asyncpg` to an extra; configuration defaults and settings aliases (section 5).
    Needs 3–9.
12. **Kernel ratchets**: no backend SDK import in `core/` outside drivers (shrink-only
    allowlist for the PostgreSQL ops tools and the test harness), no `raw_*` and no
    undeclared provider attribute in the kernel, `core/eventing` imports no transport.
13. **Review** (correctness, security, ADR conformance) after slices 3–8 and again after 11,
    per the wave cadence.

The existing tasks fold in: `infra-cache-driver` becomes the `memory` drivers plus proof
that `cache` leaks no Redis (after 4); `infra-eventbus-driver` (NATS or Kafka) waits for 7;
`infra-docs-honest` comes last; `infra-pg-features` is replaced by 10; `infra-s3-driver` is
done (a13).

## What it must not change

- The guarantees listed above keep their statements and tests; only their provider changes.
  `guarantees.yml` names the capability and feature each one needs.
- No extra network round trip on the request path (revocation and block read, rate limit,
  idempotency): `atomic_store.redis` keeps the Lua scripts, measured with the cachegrind
  ladder before and after slice 4.
- Install commands and the standard composition behave the same.

## Questions for the owner

1. **`atomic_store` as its own kernel capability** (recommended), or the atomic operations as
   features of `cache` with a `durable` feature?
2. **Redis and Celery drivers out of the kernel wheel** into `sandloom-redis` and
   `sandloom-celery` this wave (recommended), or only behind public contracts first and the
   wheel split later?
3. **Database-backed drivers** for `atomic_store` and `lease`, so an application runs with
   PostgreSQL alone (Solid Cache / Solid Queue style)? Recommended: yes for these two, they
   are small and a row gives a free fence. `broadcast` and the task queue stay Redis and
   Celery (your answer 7 deferred a zero-dependency queue).
4. **The public task API**: replace `celery.shared_task` and `crontab` in `sandloom.sdk` with
   Sandloom's own `task` and `cron` (breaking; alpha), with Celery as the first driver?
5. **Escape hatches**: drop `raw_client`/`raw_backend` from the contracts and allow only a
   typed `native()` on a driver package's own class for application code (recommended), or
   remove them entirely?
6. **Settings**: keep `CACHE_URL`, `BROKER_URL`, `SMTP_*`, `S3_*` as aliases for one minor
   release (recommended), or move to driver options in one step?
7. **Driver tiers** (`experimental`, `beta`, `stable`) declared on the registration and
   enforced in production like experimental capabilities?
