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
2. **Built-in drivers live inside the kernel, below its own layers.** About 1,475 lines of
   code that speak Redis Streams (`streams_transport`, `streams_consume`, `streams_groups`,
   `streams_transport_base`) and the pub/sub calls in `pubsub_bus.py` sit in `core/eventing`,
   which is below `drivers` in the layer contract. The rest of the Streams bus
   (`streams_bus`, `streams_dispatch`, `streams_lease`, `streams_inventory`, ~1,100 lines) makes
   no Redis call: it is the kernel's delivery logic (G-EVENT-*) above the transport and stays.
   The drivers themselves import kernel internals: `drivers/celery_taskqueue.py:24-30` imports
   settings, effects, `infra.config` and `tasks.enqueue.enqueue_once`; the Redis drivers import
   settings and metrics. ADR-0011 names "a driver imported from sandloom.core" as the fault it
   fixed; `sandloom-s3` imports only `driverkit`. The Celery worker, the beat lease, the async runner and GC tuning
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
(enforced by a ratchet, slices 3 and 20).

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
| `atomic_store` (new) | durable, **never evicting** key-value with TTL: `get`, `set(ttl)`, `delete`, `add`, `compare_and_set`, `compare_and_delete`, `set_max` (monotonic), `subject_state` read/write; `isolation_identity()` (replaces Redis `INFO server` in `deployment_identity`) and a declared eviction policy, both read by the production profile | revocation (G-REVOKE-03), blocks (G-BLOCK-01), idempotency (G-IDEM-01..03), enqueue dedupe (G-TASK-01..03), RPC replay | `redis` (checks `maxmemory-policy noeviction` on open and in health: no such check exists today), `memory` (dev and tests, refused in production), optionally `database` |
| `lease` (new, replaces private `lock`) | `acquire(name, ttl, owner) -> Lease \| None` with a monotonic `fence`, `renew`, `release`; no blocking wait | beat lease (G-OPS-01), saga sweep (`sagas/sweep.py:139`) | `redis`, `memory`, optionally `database` (row with a fence column) |
| `broadcast` (new) | online-only fan-out: `publish(channel, payload)`, `subscribe(channel)`; no persistence, no replay, declared best-effort | contract-cache invalidation (ADR-0003), the volatile event bus, future websocket rooms | `redis` (pub/sub), `memory` |
| `eventbus` | the durable transport made public: today's `MessageTransport` with typed operations (publish batch, consume with consumer groups, ack, claim stale, dead-letter, trim, replay, inventory, **group ownership**: claim, read, transfer, release, already `TransportCapability.GROUP_OWNERSHIP`, used by G-EVENT-02 and `extract`'s owner transfer). Schema validation, payload limits and the bus logic above the transport (`streams_bus`, `streams_dispatch`, `streams_inventory`, `pubsub_bus`) stay in the kernel | outbox relay, consumers, DLQ ops (G-EVENT-03..07, G-EVENT-10) | `streams` (Redis), `memory`; later NATS JetStream or Kafka |
| `worker` (the driver side of `taskqueue`) | `WorkerHost`: run a worker process, run the scheduler while holding a `lease`, process lifecycle hooks. Kernel boot (composition, `enforce_runtime`) stays in the kernel and calls the host | `sandloom worker`, `sandloom beat` (new commands; today every deploy runs `celery -A sandloom.core.worker`), G-INFRA-11, C-WORKER-01..06 | `celery` |

**Kept on `cache`, deliberately.** The rate limiter (`ingress/rate_limit.py:121`) and the
consumer inbox (`eventing/consumer_inbox.py:57`) use the default, evicting cache today and fail
open. G-HTTP-02 counts every client IP, 404s included: moving these keys onto the never-evicting
store that holds revocation marks would let an attacker fill it, after which revocation and
block writes fail. They stay on `cache`, with `windowed_counter` (today's
`incr_window(key, previous_key, ttl)`, one round trip) as a cache feature and the fail-open
behaviour written into the contract.

**Instances.** The duty split already exists as settings: `SECURITY_CACHE_INSTANCE`,
`IDEMPOTENCY_CACHE_INSTANCE`, `BEAT_LEASE_CACHE_INSTANCE` (`config/trust.py:73,93`,
`config/runtime.py:145`), attested by `production_profile.py:132-173` through Redis
`INFO server`. They become instances of `atomic_store` (`security`, `idempotency`) and `lease`
(`beat`); the production profile attests separation through `isolation_identity()`, so a
non-Redis driver can pass it.

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
contract. `beat_lease.py` splits: `FencedLease`, `periodic_needs` and `read_beat_lease` (used by
the saga sweep, `cli/doctor.py:434` and `app_factory/ops.py:460`) stay in the kernel over the
port; `BeatLease`, `LeasedScheduler` and `install_beat_lease` go to the Celery package. The
Streams group owner is not a lease (a permanent marker with no TTL, transferred by CAS) and
stays in the eventbus contract.

### 3. Contract rules (every capability)

1. **Errors.** One hierarchy in `sandloom.sdk.infra.errors`, re-exported by `driverkit`:
   `InfraError` → `NotFound`, `AlreadyExists`, `InvalidArgument`, `TooLarge`,
   `PermissionDenied`, `Unavailable` (transient, `retryable = True`), `Unsupported(feature)`,
   `Timeout`. A miss is a result (`None`, `False`), not an exception. `ObjectNotFound`
   becomes a `NotFound` in one step (the kernel carries no deprecated API; upgrade note). Each
   kit asserts the mapping.
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
| `sandloom` (kernel) | the contracts, the registry, the kits, the `memory` drivers for `cache`, `atomic_store`, `lease`, `broadcast` and `eventbus` (refused in production; no `memory` taskqueue, owner answer 7 of infra-deps), `local` objectstore, `log` and `smtp` notification, the `sqlalchemy` database driver with the `postgresql` and `sqlite` dialects |
| `sandloom-redis` (new) | `cache.redis`, `atomic_store.redis`, `lease.redis`, `broadcast.redis`, `eventbus.streams`, and the shared client they use |
| `sandloom-celery` (new) | `taskqueue.celery` with its `WorkerHost`, beat under `lease`, the async runner and GC tuning for the worker |
| `sandloom-s3` | as today |
| `sandloom[standard]` | depends on `sandloom-redis` and `sandloom-celery`, so the standard composition is unchanged |

The extras `sandloom[redis]` and `sandloom[celery]` stay and install the new packages, so
install commands do not change. The `fast` and `admin` extras stop naming `redis` and `celery`
directly. Each package pins the kernel's minor version (a built-in and an entry point with one
name fail startup with B021). Drivers in the packages import only `sandloom.driverkit`; what
they take from the kernel today (settings, metrics, enqueue dedupe) becomes driver options, a
driverkit metrics hook, and a kernel taskqueue layer above the driver. A check bars
`packages/*` from importing `sandloom.core`. The new packages join the default dev
environment (`sandloom-s3` is only in the `packaging` group today, root `pyproject.toml:28`). `asyncpg` moves from a base dependency to a `postgres` extra
that the standard composition installs; `psycopg2` stays only where the PostgreSQL ops tools
need it (backup, extract, dev services, the test harness), each import lazy.

The database driver stays in the kernel this wave: the kernel's own stores (outbox, command
log, request log, audit) run on it, and a second database already works through renderers.
Moving it out is a later decision, not needed for independence.

### 5. Configuration

- `core/infra/config.py` stops hard-coding `sqlalchemy`, `redis` and `celery` as defaults. The
  defaults come from the composition: `sandloom-standard` and the `sandloom new` template
  write `INFRA` explicitly, the template selecting `memory` for development by name. Nothing
  selected is a B037 refusal in every environment that names what to install: `memory` is
  never an automatic fallback (owner answer 2 of infra-deps).
- Flat settings (`cache_url`, `broker_url`, `smtp_*`, `s3_*`) become driver options read by
  the driver's `options_schema`, in one step with upgrade notes: the kernel carries no
  deprecated API (owner question 6).
- The test harness (`core/testing/workers.py:54-65` rewrites `CACHE_URL`/`BROKER_URL` per
  xdist worker; `ownership.py`, `slots.py`, `cli/dev/services.py` speak Redis) isolates parallel
  runs through a driverkit hook (`test_isolation(worker_id) -> options`); the Redis parts move to
  a pytest plugin in `sandloom-redis`.

## Order of work (slices of `infra-drivers`)

Every slice keeps the old path working until its last caller has moved; there is no big-bang
switch. Registry files (`capabilities.py`, `bootstrap.py`, `access.py`, settings) are touched
once, in the foundation, so the capability slices after it can run side by side.

| # | slice (alias) | needs | delivers |
|---|---|---|---|
| 0 | `idk-fixes` | — | the five research findings above |
| 1 | `idk-adr` | owner's answers | ADR-0016 and the per-capability spec |
| 2 | `idk-foundation` | 1 | error hierarchy, feature rules, uniform kit harness with broken-driver tests, built-ins declare `contract_version` once, `driverkit.testing` in the baseline; the `core/infra/ports` package and the specs, accessors and settings of `atomic_store`, `lease`, `broadcast` registered |
| 3 | `idk-ratchets` | 2 | the kernel ratchets now, each with a seeded shrink-only allowlist (no backend SDK in `core/`, no `raw_*` or undeclared provider attribute, `eventing` speaks no transport); a stale entry does not fail, so slices do not collide on the allowlist |
| 4 | `idk-driver-deps` | 2 | drivers import only `driverkit` (metrics hook, options instead of settings, enqueue dedupe in a kernel taskqueue layer); a check that `packages/*` import no `sandloom.core` |
| 5 | `idk-test-harness` | 2 | parallel test isolation through the driverkit hook; no Redis import or protocol in `core/testing` outside a lazy allowlisted spot |
| 6 | `idk-atomic-store` | 2 | contract, kit, `redis` and `memory` drivers (the Lua moves from the cache driver), eviction check, `isolation_identity` |
| 7 | `idk-atomic-adopt` | 4, 6 | revocation, blocks, idempotency, enqueue dedupe, RPC replay onto `atomic_store`; the instance settings and production attestation through the port; guarantee texts updated |
| 8 | `idk-cache-contract` | 7 | `cache` shrinks to module operations plus `windowed_counter`; rate limit and inbox use it; kit; `memory` cache driver (fulfils `kb-ic-5`); removal of `incr_window`, `incr_many`, `set_nx_get`, `set_max` from `sandloom.sdk.infra.Cache` in the API baseline and upgrade notes |
| 9 | `idk-lease` | 2 | contract, kit, drivers; `FencedLease` over the port; beat lease and saga sweep on it; private `lock` removed |
| 10 | `idk-broadcast` | 2, 0 | contract, kit, drivers; contract-cache shadow and the volatile bus use it |
| 11 | `idk-eventbus-contract` | 2 | `MessageTransport` public with typed operations, features and group ownership; kit against `memory` and `streams`; every duck-typed read goes (`bus_base`, `consumers/callbacks`, `consumers/runtime`, `unit_of_work/dispatch`, `tasks/relay`, `app_factory/revocation_applier`, `cli/extract`, `cli/doctor`) |
| 12 | `idk-event-ops` | 11 | `sandloom streams`, `tasks/event_ops.py`, admin platform API on the transport's ops operations |
| 13 | `idk-task-api` | 2 | Sandloom's `task`, `cron` and a periodic declaration replacing the `setup_periodic_tasks(sender)` hook; the `Task` Protocol without `s`/`si`/`apply_async`; derivation (`descriptors/derived_capabilities.py`, `tasks/periodic.py`) and T010 follow; modules and docs (`docs/modules/background/tasks.md`, `docs/reference/sdk/tasks.md`, `templates/module-AGENTS.md`) updated; taskqueue kit |
| 14 | `idk-worker-cli` | 2 | `sandloom worker`, `sandloom beat`, `sandloom worker healthcheck`; deploy compose, `cli/project.py`, `cli/scaffold_deploy.py`, templates, examples and T010 use them |
| 15 | `idk-worker-port` | 4, 9, 13, 14 | `WorkerHost` port; kernel boot stays, `worker.py`, the Celery half of `beat_lease.py`, `async_runner.py`, `gc_tuning.py` move behind the port; `.importlinter` updated |
| 16 | `idk-db-provider` | 2 | typed stores, no `execution_adapter`, `_internal/db_provider.py` stops importing driver internals, provider kit (replaces `infra-pg-features`) |
| 17 | `idk-pkg-redis` | 4, 5, 8, 9, 10, 12 | `sandloom-redis`: the Redis drivers and the four Streams transport files; their tests, `guarantees.yml` and `public-source.json` entries, root `pyproject.toml`/`uv.lock`, extras |
| 18 | `idk-pkg-celery` | 4, 14, 15 | `sandloom-celery`: taskqueue driver, `WorkerHost`, beat under a lease; same bookkeeping |
| 19 | `idk-config-defaults` | 17, 18 | no hard-coded default drivers, flat settings become driver options, `asyncpg` to an extra, `core/testing/workers.py` on options |
| 20 | `idk-ratchets-close` | 16, 17, 18, 19 | the allowlists of slice 3 empty or justified, strict from here on |
| 21 | `idk-review-a` | 7, 8, 9, 10, 12 | review: correctness, security, ADR-0016 conformance |
| 22 | `idk-review-b` | 20 | review of the rest |

After slice 2 ten slices can start (slice 10 also waits for 0), in three tracks: coordination (6 → 7 → 8,
9, 10), events (11 → 12) and tasks (13, 14 → 15). Files several slices touch
(`infra/contracts.py`, `static_checks.py`) are in each one's area, so tm runs those slices one
after another without a link.

The existing tasks fold in: `infra-cache-driver` waits for 8; `kb-ic-5` is delivered by 8;
`infra-eventbus-driver` (NATS or Kafka) waits for 11; `infra-docs-honest` comes last;
`infra-pg-features` is replaced by 16; `infra-s3-driver` is done (a13).

## What it must not change

- The guarantees listed above keep their meaning and tests. Texts that name the cache
  (G-REVOKE-03, G-OPS-01, and the instance attestation) are reworded to name the capability;
  `guarantees.yml` names the capability and feature each one needs.
- No extra network round trip on the request path (revocation and block read, rate limit,
  idempotency): `atomic_store.redis` keeps the Lua scripts, measured with the cachegrind
  ladder before and after slices 7 and 8.
- Install commands and the standard composition behave the same.

## Questions for the owner

1. **`atomic_store` as its own kernel capability** (recommended), or the atomic operations as
   features of `cache` with a `durable` feature?
2. **Redis and Celery drivers out of the kernel wheel** into `sandloom-redis` and
   `sandloom-celery` this wave (recommended), or only behind public contracts first and the
   wheel split later?
3. **Database-backed drivers** for `atomic_store` and `lease`, so an application runs with
   PostgreSQL alone (Solid Cache / Solid Queue style)? Recommended: yes for these two, they
   are small and a row gives a free fence. The price: a database read on every authenticated
   request (the block check, G-BLOCK-01) instead of a Redis read; measured with the cachegrind
   ladder and documented as a small-deployment option. `broadcast` and the task queue stay
   Redis and Celery (your answer 7 deferred a zero-dependency queue).
4. **The public task API**: replace `celery.shared_task` and `crontab` in `sandloom.sdk` with
   Sandloom's own `task` and `cron` (breaking; alpha), with Celery as the first driver?
5. **Escape hatches**: drop `raw_client`/`raw_backend` from the contracts and allow only a
   typed `native()` on a driver package's own class for application code (recommended), or
   remove them entirely?
6. **Settings**: move `CACHE_URL`, `BROKER_URL`, `SMTP_*`, `S3_*` to driver options in one
   step with upgrade notes (recommended: the kernel rule allows no deprecated API and ADR-0011
   keeps nothing for compatibility), or keep aliases for one minor release as an exception?
7. **Driver tiers** (`experimental`, `beta`, `stable`) declared on the registration and
   enforced in production like experimental capabilities?
8. **Instance attestation**: may an `atomic_store` on the database count as the "separate
   server" that production requires for `SECURITY_CACHE_INSTANCE` today (it is separate from
   the module cache, but shares the server with application data)? Recommended: yes, the
   attestation compares `isolation_identity()` of the security store with the module cache,
   not with the database.
9. **Worker command**: `sandloom worker` / `sandloom beat` replace `celery -A
   sandloom.core.worker` in one step (recommended, no shim, upgrade note), or keep the old
   module path as a shim?
