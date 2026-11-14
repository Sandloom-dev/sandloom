---
search:
  exclude: true
title: "ADR-0016: Kernel ports and driver packages"
---

# ADR-0016: Kernel ports and driver packages

- **Status:** Accepted (2026-11-14, by the owner, who decided the nine questions of the design)
- **Date:** 2026-11-14
- **Amends:** [ADR-0011](0011-driver-api.md), decision 1 (two doors become three) and decision 6
  (`objectstore` is public)
- **Design and research:** `design/infra-drivers/design.md` and the four research files beside it
  (kernel inventory, contract audit, external practice, owner questions)
- **Spec:** `design/infra-drivers/spec.md` (signatures, semantics, errors, features and kit
  obligations per capability; the guarantee table)
- **Implemented by:** the `infra-drivers` slices in tm, `idk-foundation` to `idk-review-b`

## Context

The SQL work took PostgreSQL out of the kernel's runtime. The rest of the infrastructure stayed
kernel code, held together by a contract nobody wrote down.

- **A third, unwritten door.** Modules see the `sandloom.sdk.infra` Protocols and drivers see the
  provider ABCs. The kernel uses operations neither shows. Revocation, blocks, idempotency and
  enqueue dedupe need durable atomic registers, which live as extras on the `cache` ABC and as
  Redis Lua. The beat lease builds a fenced lease from `incr`, `add` and compare-and-set.
  Contract-cache invalidation does pub/sub through `cache.raw_client()`. The event bus is
  `EventBusProvider.bus() -> Any` plus flags read with `getattr`. The worker takes a Celery app
  through `raw_backend()`.
- **Service drivers inside the kernel.** About 1,475 lines of Redis Streams code sit in
  `core/eventing`, below `drivers` in the layer contract. The Celery worker, the beat lease, the
  async runner and GC tuning are kernel code. The built-in drivers import kernel settings, metrics
  and `enqueue_once`, the fault ADR-0011 named.
- **Two contracts out of six can have outside drivers.** Only `notification` and `objectstore` have
  a public provider and a kit.
- **The contracts disagree.** Absence is `None` in one capability and `FileNotFoundError` in another.
  "Unsupported" is spelled three ways, one silent. A feature means an available method, one round
  trip, a guarantee or a required verb. Built-ins declare features twice and skip the
  `contract_version` check.
- **The ratchets cannot see it.** They look for imports and constructors, and a Redis command on an
  injected client passes all of them.

## Decision

### 1. Three doors

| door | reader | where | holds |
|---|---|---|---|
| module door | business modules | `sandloom.sdk.infra` | Protocols of operations a module may call |
| kernel door | the kernel | `sandloom.core.infra.ports` | Protocols of operations only the kernel calls |
| driver door | driver packages | `sandloom.driverkit` | provider ABCs implementing a module or kernel Protocol plus the lifecycle; the kits in `sandloom.driverkit.testing` |

A capability is module-facing or kernel-facing, never both with hidden extras. Modules never see
the kernel door (C004 extended). The kernel calls no `raw_*` method and reads no provider attribute
a Protocol does not declare, enforced by a ratchet. `taskqueue` is the one capability whose
module-facing contract a kernel layer serves: the provider implements a narrower driver operation
and a worker host below it, because enqueue dedupe is a kernel guarantee.

### 2. The capability set

Module-facing, each public with a kit:

| capability | contract | features |
|---|---|---|
| `database` | `AsyncSession` and the `sandloom.sdk.data` forms; driver side `DatabaseProvider` with typed stores and renderer contract 2 | as declared today, each with its kind |
| `cache` | `get`, `set`, `delete`, `add`, `incr`, `get_many`, `set_many`, `windowed_counter`. It may evict and lose data; no kernel guarantee rests on it | `cas` |
| `objectstore` | as today; absence raises `NotFound` | `metadata`, `presigned_get` |
| `notification` | `send(Notification)` | the channels |
| `taskqueue` | Sandloom's own task API: `@task`, `task.enqueue(payload, delay=, idempotency_key=)`, `@periodic(cron=, id=)` | `delay`, `periodic`, `redelivery` |

Kernel-facing, implementable by drivers, not callable by modules:

| capability | contract | serves |
|---|---|---|
| `atomic_store` (new) | durable, never-evicting key-value with TTL and atomic `add`, compare-and-set, compare-and-delete and the subject-state write | revocation, blocks, idempotency, enqueue dedupe, RPC nonces |
| `lease` (new, replaces the private `lock`) | `acquire` with a monotonic fence, `renew`, `release`, `holder`; no blocking wait | the beat lease, the saga sweep |
| `broadcast` (new) | online-only fan-out: `publish`, `subscribe`; no persistence, no replay | contract-cache invalidation, the volatile event bus |
| `eventbus` | the durable transport with typed operations, group ownership included | outbox relay, consumers, dead-letter operations |
| `worker` | `WorkerHost`, the driver side of `taskqueue`: run workers, run the scheduler while holding a `lease` | `sandloom worker`, `sandloom beat` |

The rate limiter and the cache-form consumer inbox stay on `cache` and fail open, as written in its
contract. Moving per-IP counters onto the never-evicting store would let an attacker fill the store
that holds revocation marks.

The kernel's instances keep their duties: `atomic_store` instances for security state and for
idempotency, and a `lease` instance for the beat. The production profile attests their separation
through the lifecycle's `isolation_identity()`, so a non-Redis driver can pass it.

`secrets`, `workflow`, `search`, `graph_store` and `document_store` stay experimental and private.

### 3. Contract rules, for every capability

1. **Errors.** One hierarchy, exported from `sandloom.sdk.infra` and `sandloom.driverkit`:
   `InfraError` with `NotFound`, `AlreadyExists`, `InvalidArgument`, `TooLarge`, `PermissionDenied`,
   `Unavailable` (retryable), `Unsupported(feature)` and `Timeout` (retryable). A miss is a result
   (`None`, `False`), never an exception. Code acts on the class, never on the message. The
   objectstore's `FileNotFoundError` becomes `NotFound` in one step. Each kit asserts the mapping.
2. **Features and performance hints.** A feature is an optional behaviour of a call shape every
   driver has, or a guarantee. It is never a required method and never a speed claim. A speed
   claim, such as one round trip for `get_many`, is a `performance_hint` that no correctness
   decision reads. A call that needs an absent feature raises `Unsupported`. A module that needs a
   feature declares it and is refused at load (SANDLOOM-B033, SANDLOOM-B037). No fallback weakens a
   guarantee silently.
3. **Declaration.** Once, on the class (`supported_features` or `features_for(options)`), built-ins
   included. `contract_version` is an integer major per capability, set by built-ins and checked
   like an external driver's.
4. **Lifecycle.** The registry calls `aopen` on every used instance before its first operation,
   the event bus included. A driver package may share one client across its capabilities; the
   kernel never reaches it. `health()` is bounded by the registry, and its `details` carry no
   secret, URL or exception text.
5. **No escape hatch in the contracts.** `raw_client` and `raw_backend` leave the provider ABCs,
   and there is no `native()` accessor. A driver package may document public attributes of its own
   class as non-portable. C003 extends to the kernel.
6. **One kit harness.** A `make_provider` fixture. The kit opens and closes the provider, and
   checks `contract_version`, the feature declaration, the error mapping and health. It runs the
   feature-gated groups, and is itself run against broken drivers. Kit signatures and
   `sandloom.driverkit.testing` are pinned in the API baseline.
7. **Tiers.** `experimental` implements the contract; `beta` passes its kit in CI against the real
   service; `stable` adds reconnect after an outage and the auth options. The tier is declared on
   the driver class and shown by `sandloom doctor`. In production an `experimental` driver needs the
   accepted risk `experimental_driver` (SANDLOOM-B023, extended from capabilities to drivers), and a
   `beta` one logs a warning. This repository's drivers are at least `beta`.

### 4. Packaging: the kernel ships no service driver

| distribution | holds |
|---|---|
| `sandloom` | the contracts, the registry, the kits; `memory` drivers for `cache`, `atomic_store`, `lease`, `broadcast` and `eventbus` (refused in production); `local` objectstore; `log` and `smtp` notification; the `sqlalchemy` database driver with the `postgresql` and `sqlite` renderers |
| `sandloom-redis` (new) | `cache`, `atomic_store`, `lease` and `broadcast` on Redis, the `streams` event transport, the client they share, and the Redis parts of the test harness as a pytest plugin |
| `sandloom-celery` (new) | the `celery` taskqueue with its `WorkerHost`, the beat under a `lease`, the async runner and the worker's GC tuning |
| `sandloom-s3` | as today |
| `sandloom[standard]` | depends on `sandloom-redis` and `sandloom-celery`, so the standard composition does not change |

The new packages are released in lockstep with the kernel and pin its minor version
(`sandloom>=X.Y,<X.(Y+1)`). The extras `sandloom[redis]` and `sandloom[celery]` stay and install
them, so install commands do not change. Drivers in packages import only `sandloom.driverkit`, and a
check bars `packages/*` from importing `sandloom.core`. What drivers take from the kernel today
becomes driver options, a driverkit metrics hook, and a kernel layer above the taskqueue driver.
The release tooling becomes data-driven before the first package moves.

The database driver stays in the kernel: the kernel's own stores run on it, and a second database
already works through renderers.

### 5. Configuration

- `core/infra/config.py` stops hard-coding `sqlalchemy`, `redis` and `celery` as defaults. The
  standard composition and the `sandloom new` template write `INFRA` explicitly; the template
  selects `memory` for development by name. `memory` is never an automatic fallback.
- The flat settings `CACHE_URL`, `BROKER_URL`, `SMTP_*` and `S3_*` become options read by each
  driver's `options_schema`, in one step with upgrade notes and no aliases.
- Parallel test runs are isolated through a driverkit hook, `test_isolation(options, worker_id)`,
  instead of the kernel rewriting Redis URLs per worker.

### 6. The owner's decisions (2026-11-14)

1. `atomic_store` is its own kernel capability, not a durable flavour of `cache`.
2. Redis and Celery leave the kernel wheel this wave, as `sandloom-redis` and `sandloom-celery`,
   released in lockstep and installed by the existing extras. No shims.
3. `lease` on the database: yes, a logged row with a fence and no advisory locks. `atomic_store` on
   the database: yes, as an option with a stated price (+0.2 to 0.7 ms per authenticated request,
   twice that on a write), not the standard default. `broadcast` and the task queue stay on Redis
   and Celery.
4. Sandloom has its own task API: `@task`, `task.enqueue(payload, delay=, idempotency_key=)` and a
   declarative `@periodic(cron=, id=)` replacing `setup_periodic_tasks(sender)`. The driver dedupes
   a periodic run by `(id, slot)` under a `lease`.
5. `raw_client` and `raw_backend` leave the contracts and no `native()` is added.
6. The flat connection settings become driver options in one step, with upgrade notes and no
   aliases.
7. Driver tiers go through the B023 mechanism with the accepted risk `experimental_driver`.
8. A database-backed store counts for the instance attestation. The check becomes "declares
   `never_evicts` and differs from the evicting module cache by `isolation_identity()`". Redis keeps
   its `run_id` comparison and gains the missing `noeviction` check.
9. The worker commands change in one step, with no shim: `sandloom worker`, `sandloom beat` and
   `sandloom worker check`. `sandloom-celery` keeps a stable importable Celery app for
   `celery inspect` and Flower.

### 7. What this must not change

- **The guarantees keep their meaning and tests.** The texts that name the cache or Redis
  (G-REVOKE-03, G-OPS-01, the instance attestation) are reworded to name the capability, and
  `guarantees.yml` records the capability and feature each one needs.
- **No extra network round trip on the request path.** The revocation and block read, the rate
  limit and the idempotency reservation keep one round trip each. `atomic_store` on Redis keeps
  the Lua scripts, measured with the cachegrind ladder before and after the slices that move them.
- **Install commands and the standard composition behave the same.**

## Consequences

- ADR-0011 decision 1 now reads as decision 1 of this record. Decision 6 holds with `objectstore`
  public: `ObjectStoreProvider` and its kit have been exported from `sandloom.driverkit` since the
  S3 driver.
- Public API changes, each with upgrade steps and no alias: the cache Protocol shrinks to module
  operations; the task API replaces `shared_task`, `cron` and the beat hook; the objectstore errors
  change class; `raw_client` and `raw_backend` go; the flat settings move to driver options; the
  worker is started by `sandloom worker` and `sandloom beat`.
- `cache`, `objectstore`, `taskqueue` and `eventbus` move to contract major 2, so a driver written
  for the old contract is refused at load (SANDLOOM-B022) instead of misbehaving.
- The `idk-ratchets` slice adds shrink-only allowlists: no backend SDK in `core/`, no `raw_*` call
  or undeclared provider attribute, no transport protocol in `core/eventing`. `idk-ratchets-close`
  empties them.
- Rejected, with reasons in the design: `atomic_store` as a feature of `cache`; the private `lock`
  as the lease; a `native()` accessor; aliases for the old settings and the old worker command;
  moving the database driver out this wave; a `memory` taskqueue.
