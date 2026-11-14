# Spec: kernel ports and driver contracts, per capability

Status: **written for `idk-adr`, 2026-11-14**, against `design.md` at `9330edfe` (all nine owner
questions decided). The decision record is [ADR-0016](../../docs/adr/0016-kernel-ports-and-driver-packages.md);
this file is the contract every later `idk-*` slice builds to. Line references are to `aefd3e52`
(the base of the design commits; they change no code).

Read the section of your capability, plus "Shared rules" and "Module path layout". What this spec
had to decide that `design.md` left open is listed at the end under "Decided in the spec", for
review. What is the owner's to decide is under "Open for the owner".

## Contents

1. Module path layout
2. Shared rules: errors, features, declaration, lifecycle, tiers, test isolation, kits
3. `cache`
4. `atomic_store`
5. `lease`
6. `broadcast`
7. `eventbus`
8. `taskqueue` and `WorkerHost`
9. `objectstore`
10. `notification`
11. `database` provider
12. Which guarantee rests on which capability and feature
13. Decided in the spec
14. Open for the owner

## 1. Module path layout

Every slice follows this layout. A slice creates only the files of its capability, so slices of
different capabilities do not share a file (the exceptions are listed in the last row).

| what | path | notes |
|---|---|---|
| shared errors | `src/sandloom/core/infra/errors.py` | re-exported by `sandloom.sdk.infra` and `sandloom.driverkit` |
| provider base and lifecycle port | `src/sandloom/core/infra/ports/_base.py` | `InfraProvider` moves here from `core/infra/contracts.py:536-610`; `ProviderLifecycle` Protocol |
| one capability's contract | `src/sandloom/core/infra/ports/<capability>.py` | the operation Protocol(s), value types and the provider ABC of that capability, module-facing and kernel-facing alike |
| kernel door | `src/sandloom/core/infra/ports/__init__.py` | exports the kernel-facing Protocols only: `AtomicStore`, `LeaseStore`, `Broadcast`, `EventTransport`, `WorkerHost`, `ProviderLifecycle` |
| module door | `src/sandloom/sdk/infra.py` | re-exports the module-facing Protocols (`Cache`, `ObjectStore`, `Notifier`, `TaskQueue`), the errors and the accessors |
| driver door | `src/sandloom/driverkit/__init__.py` | re-exports every provider ABC, value type and error a driver needs |
| one capability's kit | `src/sandloom/driverkit/kits/<capability>.py` | `<Capability>Conformance`; the shared harness is `driverkit/kits/_harness.py` |
| public kit names | `src/sandloom/driverkit/testing.py` | the only import path for kits (pinned in the API baseline); resolves names lazily from a table, see below |
| built-in drivers until the split | `src/sandloom/core/drivers/<backend>_<capability>.py` | `redis_atomic_store.py`, `memory_lease.py`, `database_lease.py`, `celery_worker.py`, … |
| built-in drivers after the split | `packages/sandloom-redis/src/sandloom_redis/<capability>.py`, `packages/sandloom-celery/src/sandloom_celery/<capability>.py` | slices 17 and 18 move the files whole |
| shared by several slices | `core/infra/capabilities.py`, `core/drivers/bootstrap.py`, `core/infra/access.py`, `core/infra/registry.py`, settings | touched once by `idk-foundation`; later slices only add to the tables it creates |

Where the existing kits live, and which location this spec chose: the three kits that exist are
classes in one file, `src/sandloom/driverkit/testing.py` (`NotifierConformance` :68,
`SqlDialectConformance` :201, `ObjectStoreConformance` :901). This spec keeps that file as the
**public import path** and moves the implementations to `driverkit/kits/<capability>.py`, because
six new kits in one 975-line file would put every capability slice in one tm area. `idk-foundation`
moves the three existing kits (`kits/notification.py`, `kits/objectstore.py`, `kits/sql_dialect.py`)
and writes in `testing.py` a module `__getattr__` (PEP 562) over a fixed table of every kit name
this wave plans (`CacheConformance` → `kits.cache`, …). A capability slice then creates its kits file
and never edits `testing.py`. A name whose kit file does not exist yet raises `AttributeError` naming
the slice that delivers it. `__all__` lists the table, and the baseline pins it.

The streams transport is about 1,475 lines in four files; it moves as a package
`core/drivers/redis_eventbus/` (one file per current file), then whole into `sandloom_redis/eventbus/`.

## 2. Shared rules

### 2.1 Errors

`src/sandloom/core/infra/errors.py`, exported from `sandloom.sdk.infra` and `sandloom.driverkit`:

```python
class InfraError(Exception):
    """Base of every error a capability operation raises on purpose."""
    retryable: ClassVar[bool] = False
    def __init__(self, message: str, *, capability: str, instance: str | None = None) -> None: ...

class NotFound(InfraError): ...          # an addressed resource is absent (objectstore get)
class AlreadyExists(InfraError): ...     # a create that must not overwrite found one
class InvalidArgument(InfraError): ...   # a key, value, TTL or option outside the contract
class TooLarge(InfraError): ...          # a value, object or payload over a limit
class PermissionDenied(InfraError): ...  # the backend refused the credentials
class Unavailable(InfraError):           # transient: connection, failover, overload
    retryable: ClassVar[bool] = True
class Timeout(InfraError):               # the operation did not finish within its bound
    retryable: ClassVar[bool] = True
class Unsupported(InfraError):           # a call needs a feature the instance does not declare
    def __init__(self, feature: str, *, capability: str, instance: str | None = None) -> None: ...
    feature: str
```

Rules, for every capability:

- **A miss is a result.** A key that is absent answers `None`, a conditional write that loses
  answers `False`. `NotFound` is for an addressed resource a caller expected (an object), never for
  a key-value lookup.
- **Codes, not messages.** The kernel branches on the class and `retryable`, never on text.
- **Nothing else escapes on purpose.** A driver maps its backend's errors to this hierarchy. Any
  other exception from an operation is a driver bug; the kernel treats it like `Unavailable` where
  it must choose fail-open or fail-closed, and the kit fails on it.
- **No built-in base classes.** The errors do not also subclass `ValueError`, `LookupError` or
  `TimeoutError`. Code that caught `FileNotFoundError` from `objectstore.get` catches `NotFound`
  (upgrade note); there is no alias.
- **`FeatureUnsupportedError`** (`core/infra/registry.py:198`) stays what it is: the registry's
  load-time refusal (B033, B037). `Unsupported` is the call-time error.
- **Replaced:** `InvalidObjectKeyError`, `UploadTooLargeError`, `ObjectTooLargeError`
  (`contracts.py:1075-1084`), `CacheContentionError` (`contracts.py:943`), `GroupOwnershipError`
  (`eventing/transport_base.py:27`), `TransportBatchError` (`transport_base.py:131`), the
  `NotImplementedError` defaults of `contracts.py:814,820,927,1260` and `transport_base.py:193,334,355`.

### 2.2 Features and performance hints

- A **feature** is either a **behaviour** (an optional behaviour of a call shape every driver has:
  without it the call raises `Unsupported(feature)`) or a **guarantee** (a property no call shows:
  durability, sharing, redelivery, scheduling). Each capability below gives each feature its kind.
- A feature is never a required method and never a speed claim.
- A **performance hint** is a speed claim (`"one_round_trip"`) about one operation:
  `performance_hints: ClassVar[Mapping[str, str]]`. No correctness decision reads it; the
  benchmarks and `sandloom doctor` do.
- A module that needs a feature declares it (`CapabilityRequirement(..., features=...)`) and is
  refused at load without it (B033, B037). The kernel states its own needs the same way at startup
  for the instances it uses (the security `atomic_store`, the `beat` lease, the event bus).
- **No silent weakening.** A portable default that cannot keep the operation's guarantee does not
  exist; the operation is required, or it is behind a behaviour feature and raises.

### 2.3 Declaration

On the provider class, once (ADR-0011 decision 4), built-ins included:

```python
class InfraProvider(abc.ABC):
    capability: ClassVar[str]
    contract_version: ClassVar[int]                      # an integer major per capability
    supported_features: ClassVar[frozenset[str]] = frozenset()
    performance_hints: ClassVar[Mapping[str, str]] = MappingProxyType({})
    tier: ClassVar[Literal["experimental", "beta", "stable"]] = "experimental"
    development_only: ClassVar[bool] = False              # refused in production (memory, log)
    options_schema: ClassVar[type[BaseModel] | None] = None
    required_imports: ClassVar[tuple[str, ...]] = ()
    install_hint: ClassVar[str] = ""

    @classmethod
    def features_for(cls, options: Mapping[str, Any]) -> frozenset[str]: ...
    @classmethod
    def test_isolation(cls, options: Mapping[str, Any], worker_id: str) -> Mapping[str, Any]: ...
```

- `contract_version` becomes an `int` (it is `ClassVar[str]` at `contracts.py:548`; the notifier's
  `"v1"` becomes `1`). Built-ins set it and the registry checks them like external drivers (today the
  drift check runs only on a strict registry, `registry.py:769`).
- Majors after this wave: `cache` 2, `objectstore` 2, `taskqueue` 2, `eventbus` 2 (their obligations
  change), `notification` 1, `database` 1 until `idk-db-provider` (then 2), and `atomic_store`,
  `lease`, `broadcast`, `worker` 1. A driver written for another major is B022, so an old
  `sandloom-s3` is refused at load instead of raising `FileNotFoundError` where `NotFound` is due.
- `register_driver(..., features=...)` in `core/drivers/bootstrap.py:88-157` stops passing features;
  the registry reads `features_for`. A built-in that overrides `features()` is a test failure.
- `instance_features` on `DriverRegistration` (`registry.py:110`) goes: `features_for(options)` is it.

### 2.4 Lifecycle (driver door)

The kernel reads lifecycle through one kernel-door Protocol, so it reads no undeclared attribute:

```python
class ProviderLifecycle(Protocol):
    name: str
    driver: str
    def features(self) -> frozenset[str]: ...
    async def aopen(self) -> None: ...
    async def health(self) -> HealthResult: ...
    async def aclose(self) -> None: ...
    async def isolation_identity(self) -> str | None: ...
```

- **`aopen`.** The registry calls it once per used instance before the first operation
  (`aopen_all`), the event bus included (it is skipped today, `registry.py:1164`). A driver that
  cannot serve raises `Unavailable` or `PermissionDenied`; startup policy decides.
- **`aclose`.** Idempotent; the registry calls it at shutdown and after a failed `aopen`.
- **`health`.** Bounded by the registry with `HEALTH_CHECK_TIMEOUT_SECONDS` (as `registry.py:1114`
  does); a timeout is `fail`. `HealthResult.details` carries only non-secret facts: the driver
  name, an error **type** name, a latency, a count. Never `str(exc)`, a URL, a host with
  credentials or a key. The kit checks this with a driver whose backend raises an error carrying a
  marker secret.
- **`isolation_identity`.** An opaque string naming the failure and eviction domain of the backing
  store: two instances with the same identity share eviction and memory. Redis: `run_id` from
  `INFO server` (today `RedisCache.deployment_identity`, `redis_cache.py:775`). Database: the
  server's system identifier plus the database name. `memory`: a per-process id. Default `None`
  (unknown): attestation fails closed on `None`.
- **Shared clients.** A driver package may share one client across its providers of several
  capabilities (keyed by connection options). The kernel never reaches that client: no `raw_client`,
  no `raw_backend`, no `native()` (owner decision 5). A package may document public attributes of
  its own class as non-portable.

### 2.5 Tiers

`tier` on the class, read at registration and shown by `sandloom doctor` and
`/diagnostics/infra`. `experimental`: implements the contract. `beta`: its kit passes in CI against
the real service. `stable`: beta plus the kit's reconnect-after-outage group and its auth options.
In production an `experimental` driver needs the accepted risk `experimental_driver` (SANDLOOM-B023,
extended from capabilities to drivers); `beta` logs one startup warning; `stable` is silent. This
repository's drivers are at least `beta`. `development_only` drivers (`memory`, `log`) are refused in
production whatever their tier, with no accepted risk.

### 2.6 Test isolation and metrics

- `test_isolation(options, worker_id) -> options`: what one pytest-xdist worker's instance uses so
  that parallel runs do not share state. Redis: another database index or key prefix. Database: the
  harness's database per worker. Default: the options unchanged (fine for `memory`). Replaces the
  URL rewriting in `core/testing/workers.py:54-65`.
- Drivers record metrics through a driverkit hook (`idk-driver-deps`): `sandloom.driverkit.observe`
  with `counter(name, documentation, labelnames)` and `histogram(...)`, owned by the kernel's
  registry. A driver imports no `prometheus_client` registry of the kernel and no kernel settings.

### 2.7 Kits (one harness)

Every kit is a pytest class with a `make_provider` fixture returning an unopened provider. The
harness (`kits/_harness.py`) opens it, closes it twice, and runs these groups before the
capability's own:

1. it is an instance of the capability's provider ABC and satisfies its Protocol;
2. `contract_version` is an `int` equal to the capability's major;
3. `supported_features` ⊆ the spec's features, `features()` equals `features_for(options)`;
4. `performance_hints` names only operations of the capability;
5. `tier` and `development_only` are set; `health()` answers within the bound and its `details`
   pass the redaction rule;
6. each feature group runs only when the feature is declared, and a declared-absent behaviour
   feature raises `Unsupported(feature)` from its call;
7. the error-mapping group: a provider built to fail (the kit's `make_failing_provider` fixture,
   optional) raises only `InfraError` subclasses.

The kit itself is tested against broken drivers in `src/sandloom/core/tests/test_driverkit.py`: one
broken driver per obligation (a wrong version type, a feature not in the spec, a non-atomic
`add`, an `Unsupported` raised for a declared feature, a health `details` with a secret, …), each
of which must fail exactly the test that names it. Kit signatures and `sandloom.driverkit.testing`
are pinned in `public_api_baseline.json` (T008).

## 3. `cache` (module-facing)

**Replaces** `Cache` (`core/infra/contracts.py:696-727`) and `CacheProvider` (:730-929). Leaves it:
`compare_and_set`/`compare_and_delete` (behind `cas`), `incr_many`, `set_nx_get`, `set_max`,
`set_subject_state`, `raw_client`; `incr_window` becomes `windowed_counter`.

```python
class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def add(self, key: str, value: str, *, ttl_seconds: int | None = None) -> bool: ...
    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int: ...
    async def get_many(self, keys: Sequence[str]) -> list[str | None]: ...
    async def set_many(self, items: Mapping[str, str], *, ttl_seconds: int | None = None) -> None: ...
    async def windowed_counter(
        self, key: str, previous_key: str, *, ttl_seconds: int
    ) -> tuple[int, str | None]: ...
    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool: ...                                                     # feature cas
    async def compare_and_delete(self, key: str, expected: str) -> bool: ...  # feature cas

class CacheProvider(InfraProvider, Cache):
    capability: ClassVar[str] = "cache"
    contract_version: ClassVar[int] = 2
```

**Semantics.**

- **May evict, may lose data.** Any key may vanish before its TTL, and a restart may empty the
  store. Nothing a kernel guarantee depends on is stored here; the kernel's users of `cache` fail
  open by contract (rate limit, consumer inbox).
- Values are UTF-8 strings; keys are strings of at most 1024 bytes. TTLs are whole seconds,
  `>= 1`; `None` means no expiry, also on `compare_and_set` (the key loses any earlier TTL). A
  key is not readable after `ttl_seconds + 1` s (reads filter expired keys even where the backend
  purges later).
- `add` is atomic: of concurrent `add`s of one key exactly one answers `True`.
- `incr` is atomic; the TTL is set in the same step when the key is created (result `1`). A value
  that is not an integer raises `InvalidArgument`.
- `get_many` / `set_many`: ordered; not atomic across keys. ABC defaults loop over `get` / `set`.
- `windowed_counter`: `incr(key)` (TTL on creation) and the value of `previous_key`, read after the
  increment. Required; the ABC default does the two operations. It serves the sliding-window rate
  limiter (`ingress/rate_limit.py:121-130`).
- With `cas`: `compare_and_set` swaps only when the key holds `expected` (absent: `False`);
  `compare_and_delete` deletes only on equality. Without `cas` both raise `Unsupported("cas")`.

**Errors.** `get`, `set`, `delete`, `add`, `incr`, `get_many`, `set_many`, `windowed_counter`:
`InvalidArgument` (key length, TTL `< 1`, non-integer for `incr`), `TooLarge` (value over the
driver's limit), `Unavailable`, `Timeout`. `compare_and_*`: the same plus `Unsupported("cas")`.

**Features.** `cas` (behaviour).
**Performance hints.** `get_many`, `set_many`, `windowed_counter`: `one_round_trip`.
**Kit (`CacheConformance`).** Round trip, absent key, overwrite, delete of an absent key; TTL expiry
within `ttl + 1` s (with a 1 s TTL); concurrent `add` (50 tasks, one winner); concurrent `incr`
(no lost update); `incr` sets the TTL on creation only; non-integer `incr`; `windowed_counter`
returns the post-increment count and the previous value; group `cas`: swap, lost swap, absent key,
TTL removal with `None`, `compare_and_delete`; without `cas`: both raise `Unsupported("cas")`.
**Built-ins.** `redis` (sandloom-redis), `memory` (kernel, `development_only`; fulfils `kb-ic-5`).

## 4. `atomic_store` (kernel-facing, new)

**Replaces** the extras the kernel takes from the cache today: `compare_and_set` (:798),
`compare_and_delete` (:818), `set_nx_get` (:851), `set_subject_state` (:895), the portable loops
`portable_set_subject_state` and `portable_set_max`, the Lua scripts in `core/drivers/redis_cache.py`,
`RedisCache.deployment_identity` (:775), and the accessors `security_cache()` / `idempotency_cache()`
(`core/infra/access.py:71,95`).

```python
class AtomicStore(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def add(self, key: str, value: str, *, ttl_seconds: int | None = None) -> bool: ...
    async def add_or_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]: ...
    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool: ...
    async def compare_and_delete(self, key: str, expected: str) -> bool: ...
    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str: ...

class AtomicStoreProvider(InfraProvider, AtomicStore):
    capability: ClassVar[str] = "atomic_store"
    contract_version: ClassVar[int] = 1
```

**Semantics.**

- **Every operation is required and atomic.** No portable default composes one from others. The
  drivers that need a compare-and-set loop (the `database` driver for `set_subject_state`) write it
  inside the driver; driverkit offers the loop as a helper.
- **Durability.** An acknowledged write is never lost within the driver's durability class, and a
  key never disappears before its TTL. A store with `never_evicts` never evicts under memory
  pressure (it refuses the write with `Unavailable` instead).
- TTL rules as `cache` (whole seconds, `>= 1`, `None` = no expiry, also on `compare_and_set`; not
  readable after `ttl + 1` s).
- `add_or_get` (was `set_nx_get`): `(True, None)` when this call created the key, else
  `(False, value)` with the value that blocked it; atomic, so `value` is never `None`.
- `set_subject_state`: the value domain is `parse_subject_state` (`contracts.py:1024`):
  `v1:after:<decimal>` or `v1:blocked:<decimal>`. Writes only when the value ranks above the stored
  one (every `blocked` above every `after`; within a kind the decimal instant, compared exactly);
  a stored value outside the domain is replaced. Returns the value held after the call. A value
  outside the domain raises `InvalidArgument`. `ttl_seconds` applies only when written.
- `isolation_identity()` (lifecycle) is required to return a value for a driver that declares
  `shared`.
- **`set_max` is not part of the contract.** No kernel code calls it (`grep set_max` finds only
  the contract, the Redis driver and a test fake); revocation writes through `set_subject_state`
  (`auth/revocation.py:228-234`). See "Decided in the spec".

**Instances.** The kernel uses two by role: `security` (revocation marks, blocks, RPC nonces) and
`idempotency` (cache-form idempotency guard, enqueue dedupe, command cache fallback). An
`idempotency` instance that is not configured is the `security` instance. The settings that name
them today are the subject of "Open for the owner" O1.

**Errors.** Every op: `InvalidArgument` (key, TTL, subject-state domain), `TooLarge`, `Unavailable`
(including a refused write on a full non-evicting store), `Timeout`. Contention inside a driver's
CAS loop past its bound is `Unavailable`.

**Features.**

| feature | kind | meaning |
|---|---|---|
| `shared` | guarantee | every process of the deployment reads the same state (`memory` lacks it) |
| `never_evicts` | guarantee | the store never evicts a key before its TTL; verified by the driver on `aopen` and in `health` |

The kernel requires `shared` and `never_evicts` of its `security` and `idempotency` instances in
production. **Performance hints.** `get`, `add_or_get`, `set_subject_state`, `compare_and_set`:
`one_round_trip` (the request-path budget; see ADR-0016 "What it must not change").

**Driver obligations.**

- `redis`: keeps the current Lua scripts (one round trip each). On `aopen` and in `health` it reads
  `maxmemory-policy` and refuses unless it is `noeviction` (no such check exists today); a refused
  `CONFIG GET` is `PermissionDenied` with a fix naming the `never_evicts_check` option the operator
  must set to `assume` to vouch for the server. `isolation_identity` is `run_id`.
- `memory`: `development_only`, declares `never_evicts` only.
- `database` (`idk-atomic-store-db`, an option): a logged table `(key primary key, value, expires_at)`;
  `add` by `INSERT … ON CONFLICT DO NOTHING`, `compare_and_set` by `UPDATE … WHERE value = :expected`,
  `set_subject_state` by a CAS loop in one transaction; expiry filtered on read and purged by the
  relay's sweeper; declares `shared` and `never_evicts`. Its price is stated in its docs page:
  +0.2-0.7 ms per authenticated request, twice that on a write.

**Kit (`AtomicStoreConformance`).** The cache kit's key-value groups minus eviction tolerance; 50
concurrent `add`s, one winner; 50 concurrent `add_or_get`s, one creator and every loser sees the
winner's value; CAS races (one winner of N); `compare_and_delete` by a stale owner fails;
`set_subject_state`: `blocked` outranks `after`, a lower instant is ignored, equal instants keep
the stored value, a corrupt stored value is replaced, an out-of-domain value raises, the TTL applies
only when written, 50 concurrent writers end at the maximum; TTL is never shortened by a losing
write; group `shared`: two providers built from the same options see each other's writes; group
`never_evicts`: the driver's own eviction check (Redis: a server with `allkeys-lru` is refused at
`aopen`). Broken-driver tests: a non-atomic `add`, a `set_subject_state` that lets `after`
overwrite `blocked`, a CAS that ignores `expected`.

## 5. `lease` (kernel-facing, new; replaces the private `lock`)

**Replaces** the cache operations inside `FencedLease` (`core/beat_lease.py:78-255`: acquire
:147-155, renew :197-237, release :244-254), `beat_lease_holder` (:400-415), and `LockProvider`
(`core/infra/extra_capabilities.py:150-161`, removed with its spec at :223).

```python
@dataclass(frozen=True, slots=True)
class Lease:
    name: str
    owner: str
    token: str          # opaque, unique per grant
    fence: int          # strictly greater than every fence granted before for this name
    ttl_seconds: int

@dataclass(frozen=True, slots=True)
class LeaseHolder:
    owner: str
    fence: int

class LeaseStore(Protocol):
    async def acquire(self, name: str, *, ttl_seconds: int, owner: str) -> Lease | None: ...
    async def renew(self, lease: Lease) -> bool: ...
    async def release(self, lease: Lease) -> bool: ...
    async def holder(self, name: str) -> LeaseHolder | None: ...

class LeaseProvider(InfraProvider, LeaseStore):
    capability: ClassVar[str] = "lease"
    contract_version: ClassVar[int] = 1
```

**Semantics.**

- `acquire` grants the lease when nobody holds it (or the holder's lease expired), else answers
  `None`. It never waits. `ttl_seconds >= 3` (as `FencedLease` requires today); `owner` is a
  diagnostic label (`<host>:<pid>` by default), not an identity check.
- **Fence.** Each grant of one name carries a fence strictly greater than every earlier grant's,
  across expiry, release and a restart of the backend within its durability class. A consumer that
  writes elsewhere under the lease passes the fence and the target rejects a lower one (Kleppmann).
  The beat lease and the saga sweep are efficiency locks and only log the fence; the contract
  carries it so that a correctness user can rely on it.
- `renew` extends the lease to a fresh `ttl_seconds` only while `lease.token` still holds it:
  `True` when extended, `False` when lost. The fence does not change on renew.
- `release` ends the lease only when `lease.token` holds it (`True`), else `False`. A released name
  keeps its fence counter.
- `holder` reads the current holder without changing anything (`sandloom doctor`,
  `app_factory/ops.py:460`, `cli/doctor.py:434`).
- **Time.** Expiry is measured by the backend's clock. The holder treats the lease as lost at a
  local deadline measured from **before** the acquire or renew call that set it; `FencedLease`
  keeps that rule (and its `ttl/3` operation bound, its renew cadence and its standby logging) over
  the port, in the kernel. `FencedLease`, `periodic_needs` and `read_beat_lease` stay in the kernel;
  `BeatLease`, `LeasedScheduler` and `install_beat_lease` go to `sandloom-celery`.
- **Instance.** The kernel uses the instance `beat` (beat and saga sweep; `sagas/sweep.py:130-139`).

**Errors.** `InvalidArgument` (TTL `< 3`, empty name), `Unavailable`, `Timeout`. A lost lease is a
result (`False`), never an exception.

**Features.** `shared` (guarantee: every process of the deployment contends for the same lease;
`memory` lacks it). The kernel requires `shared` of `beat` in production.
**Performance hints.** none.

**Driver obligations.**

- `redis`: the current shape: a fence counter key without TTL (`INCR`), the lease key by `SET NX EX`,
  renew and release by compare-and-set and compare-and-delete scripts. The fence key must not be
  evicted, so the driver runs the `noeviction` check of `atomic_store.redis` on `aopen` and in
  `health` (Redis' default policy passes it).
- `memory`: `development_only`.
- `database` (owner decision 3, yes): a logged table `(name primary key, owner, token, expires_at,
  fence)`. Acquire is one statement: `INSERT … ON CONFLICT (name) DO UPDATE SET owner, token,
  expires_at = now() + ttl, fence = lease.fence + 1 WHERE lease.expires_at < now() RETURNING fence`.
  Renew and release are `UPDATE … WHERE name = :name AND token = :token`; release sets
  `expires_at = now()` and keeps the row, so the fence survives. No advisory lock (they break under
  pgbouncer transaction pooling).

**Kit (`LeaseConformance`).** One of N concurrent acquirers wins; a second acquire answers `None`;
renew by the holder extends past the original TTL; renew and release by a stale token answer
`False`; after expiry (3 s TTL) another owner acquires with a greater fence; after release another
owner acquires at once with a greater fence; `holder` reports owner and fence and `None` when free;
TTL `< 3` raises. Broken-driver tests: a fence that repeats after release, a renew that ignores the
token.

## 6. `broadcast` (kernel-facing, new)

**Replaces** the pub/sub calls through `cache.raw_client()` in `core/contract_cache_shadow.py:845-887`
and the Redis pub/sub client use in `core/eventing/pubsub_bus.py:86-150,217,280`.

```python
class Subscription(Protocol):
    def __aiter__(self) -> AsyncIterator[BroadcastMessage]: ...
    async def aclose(self) -> None: ...

@dataclass(frozen=True, slots=True)
class BroadcastMessage:
    channel: str
    payload: bytes

class Broadcast(Protocol):
    async def publish(self, channel: str, payload: bytes) -> None: ...
    async def subscribe(self, channels: Sequence[str]) -> Subscription: ...

class BroadcastProvider(InfraProvider, Broadcast):
    capability: ClassVar[str] = "broadcast"
    contract_version: ClassVar[int] = 1
```

**Semantics.**

- **Online only, at most once.** A message reaches the subscriptions that are live when it is
  published. No persistence, no replay, no acknowledgement. `publish` succeeding means the backend
  accepted it, not that anyone received it.
- **Order.** Messages of one publisher on one channel arrive in publish order at each subscription.
  No order across publishers or channels.
- **Gaps.** When a subscription loses its connection, its iterator raises `Unavailable`; the
  subscriber resubscribes and treats the interval as a gap (the contract-cache shadow flushes; the
  volatile bus logs a drop). A driver does not silently reconnect behind an open iterator.
- `subscribe` with a channel ending in `.*` matches every channel with that prefix, only with
  `pattern_subscribe`; without it such a channel raises `Unsupported("pattern_subscribe")`.
- Payload limit: every driver accepts at least 1 MiB; above its limit `publish` raises `TooLarge`.

**Errors.** `publish`: `InvalidArgument` (empty channel), `TooLarge`, `Unavailable`, `Timeout`.
`subscribe`: `InvalidArgument`, `Unsupported("pattern_subscribe")`, `Unavailable`. Iteration:
`Unavailable` on a lost connection.

**Features.** `pattern_subscribe` (behaviour), `shared` (guarantee: subscribers in every process of
the deployment receive it; `memory` reaches only its own process).
**Performance hints.** none.
**Users.** The contract-cache shadow (ADR-0003, opt-in `CONTRACT_CACHE_SHADOW`; needs `shared` when
more than one process serves) and the volatile event bus (eventbus driver `redis`, section 7).
**Built-ins.** `redis` (sandloom-redis), `memory` (kernel, `development_only`).
**Kit (`BroadcastConformance`).** A live subscriber receives a message; a subscriber that joins
later does not receive earlier ones; order of one publisher on one channel; several channels on one
subscription; `aclose` ends iteration; group `pattern_subscribe` (prefix match, no match on another
prefix) and its absence (`Unsupported`); group `shared`: a second provider from the same options
receives; the `TooLarge` limit at the driver's declared size; stable tier: an outage makes iteration
raise `Unavailable` and a resubscribe works after it.

## 7. `eventbus` (kernel-facing)

**Replaces** `EventBusProvider.bus() -> Any` (`core/infra/contracts.py:1265-1277`), the transport ABC
`MessageTransport` (`core/eventing/transport_base.py:161-355`), `TransportCapability` and
`probe_transport_capabilities` (`core/eventing/transport_capabilities.py:33-104`), the bus flags
read with `getattr` (`core/eventing/bus_base.py:88-102`: `durable`, `in_process`,
`supports_batch_publish`, `supports_prevalidated`, `native_batch_publish`), and the methods only the
Streams transport has (`streams_groups.py:213` `stream_info`, :293 `group_owner`, :304
`release_group_owner`, :394 `forget_consumer`; `streams_consume.py:544-584` `renew_pending`, :614
`stats`). The bus logic above the transport stays kernel code: `streams_bus`, `streams_dispatch`,
`streams_inventory`, `streams_lease`, schema validation and payload limits.

**Drivers and the bus.** The `eventbus` capability keeps its user-facing driver names:

| driver | what the kernel builds | package |
|---|---|---|
| `streams` | the kernel's durable bus over a `streams` `EventTransportProvider` | sandloom-redis |
| `memory` | the kernel's in-process volatile bus (`memory_bus.py`), no transport | kernel, `development_only` |
| `redis` | the kernel's volatile bus (`pubsub_bus.py`) over a `broadcast` instance (option `broadcast`, default `default`) | kernel bus, `broadcast.redis` from sandloom-redis |

Only `streams` is an `EventTransportProvider`. The in-memory transport (`memory_transport.py`) is
the kit's reference transport and the test harness's, not an `INFRA` choice.

```python
@dataclass(frozen=True, slots=True)
class Delivery:                       # unchanged, transport_base.py:147
    id: str
    stream: str
    group: str
    envelope: EventEnvelope

@dataclass(frozen=True, slots=True)
class GroupOwner:
    module: str
    database: str
    service: str | None

@dataclass(frozen=True, slots=True)
class Retention:
    max_age_seconds: int | None       # None: unbounded
    max_length: int | None

class EventTransport(Protocol):
    # publishing
    async def publish(self, stream: str, envelope: EventEnvelope) -> str: ...
    async def publish_many(self, items: Sequence[tuple[str, EventEnvelope]]) -> list[str]: ...
    # consuming
    async def ensure_group(self, stream: str, group: str, *, start_id: str = "0") -> None: ...
    async def consume(
        self, stream: str, *, group: str, consumer: str, count: int = 10,
        block_ms: int | None = None,
    ) -> list[Delivery]: ...
    async def ack(self, delivery: Delivery) -> None: ...
    async def ack_many(self, deliveries: Sequence[Delivery]) -> None: ...
    async def dead_letter(self, delivery: Delivery, *, reason: str) -> None: ...
    def never_dead_letter(self, stream: str, group: str) -> None: ...
    async def renew_pending(self, deliveries: Sequence[Delivery], consumer: str) -> None: ...  # lease_renewal
    def renew_interval_seconds(self) -> float: ...                                          # lease_renewal
    # group ownership (G-EVENT-02, extract-module)
    async def claim_group_owner(self, stream: str, group: str, owner: GroupOwner) -> None: ...
    async def group_owner(self, stream: str, group: str) -> GroupOwner | None: ...
    async def list_group_owners(self) -> list[tuple[str, str, GroupOwner]]: ...
    async def transfer_group_owner(
        self, stream: str, group: str, *, expected: GroupOwner, owner: GroupOwner
    ) -> bool: ...
    async def release_group_owner(self, stream: str, group: str) -> bool: ...
    # retention (G-EVENT-12, guarantee windows)
    def retention(self) -> Retention: ...
    # dead-letter operations (dlq_ops)
    async def list_dlq(
        self, stream: str, *, count: int = 100, after: str | None = None
    ) -> list[DeadLetter]: ...
    async def replay_dlq(
        self, stream: str, *, ids: Sequence[str] | None = None, count: int = 100,
        group: str | None = None,
    ) -> int: ...
    # inspection
    async def group_info(self, stream: str) -> list[GroupInfo]: ...
    async def stream_info(self, stream: str) -> StreamInfo: ...
    async def oldest_unacked_age_seconds(self, stream: str, group: str) -> float | None: ...
    # inventory
    async def register_consumer(self, stream: str, group: str, info: ConsumerInfo) -> None: ...
    async def list_consumers(self, *, include_stale: bool = False) -> list[ConsumerInfo]: ...
    async def forget_consumer(self, stream: str, group: str) -> bool: ...

class EventTransportProvider(InfraProvider, EventTransport):
    capability: ClassVar[str] = "eventbus"
    contract_version: ClassVar[int] = 2
```

`DeadLetter`, `GroupInfo`, `StreamInfo` and `ConsumerInfo` are frozen dataclasses with the fields
the `dict`s carry today (`streams_transport.py:166`, `streams_groups.py:189,213,361`); `idk-eventbus-contract`
writes them from those. `oldest_unacked_age_seconds` drops the `last_delivered_id`, `pending` and
`lag` hints of `transport_base.py:245-253`: the transport reads them itself.

**Semantics.**

- **At least once.** A delivery is redelivered until acked or dead-lettered. A failed delivery is
  not redelivered before its retry backoff (`base * 2**(deliveries-1)`, capped); a consumer that died
  has its pending deliveries reclaimed after `min_idle_ms`. After `max_deliveries` a delivery moves
  to `<stream>.dlq`, unless `never_dead_letter(stream, group)` exempted the group; the source is
  acked only after the DLQ append succeeded. `dead_letter` moves one at once (G-EVENT-07).
- **Order.** `publish_many` appends in order. A group receives new entries in append order;
  redeliveries may arrive out of order.
- **Publish failures.** `publish_many` raises `PartialPublish(InfraError)` carrying `published`
  (the ids of the leading items known appended) and the cause, `retryable` from the cause (today
  `TransportBatchError`). Callers treat the rest as undelivered (a retry may duplicate, never lose).
- **Groups.** `ensure_group` is idempotent; `start_id` (`"0"`, `"$"`, `"<ms>-0"`) applies to a new
  group only and needs `group_start_id` for anything but `"0"`. Consumer names are per process
  (`process_consumer_id`, kernel).
- **Group ownership** (required, not a feature). `claim_group_owner` records the owner on first
  claim and raises `GroupOwnedElsewhere(AlreadyExists)` when a different `(module, database)` holds
  it (the message of `transport_base.py:48-66`). The marker has no TTL and is not a lease.
  `transfer_group_owner` is a compare-and-set on the whole owner. The consumer runtime treats
  `GroupOwnedElsewhere` as permanent (no retry; it is not `retryable`).
- **Retention** is a driver option (`retention_seconds`, `max_length`; today the settings
  `EVENT_STREAM_RETENTION_SECONDS` and `EVENT_STREAM_MAXLEN`), reported by `retention()`, which
  `core/guarantee_windows.py:93` reads instead of the setting.
- Targeted DLQ replay: a replayed entry carries `target_group`; every other group acks it without
  running handlers (G-EVENT-11).

**Errors.** `publish`: `TooLarge`, `Unavailable`, `Timeout`; `publish_many`: `PartialPublish`.
`ensure_group`: `Unsupported("group_start_id")`, `Unavailable`. `consume`, `ack`, `ack_many`,
`dead_letter`, `renew_pending`: `Unavailable`, `Timeout`; `renew_pending` without the feature:
`Unsupported("lease_renewal")`. Ownership: `GroupOwnedElsewhere`, `Unavailable`. DLQ, inspection and
inventory operations without their feature: `Unsupported(feature)`.

**Features.**

| feature | kind | meaning |
|---|---|---|
| `durable` | guarantee | entries survive a transport restart within its durability class and a consumer crash |
| `shared` | guarantee | every process of the deployment sees the same streams and groups |
| `blocking_consume` | behaviour | `consume(block_ms=)` waits server-side |
| `group_start_id` | behaviour | `ensure_group(start_id=)` other than `"0"` (G-EVENT-09) |
| `lease_renewal` | behaviour | `renew_pending` and `renew_interval_seconds` |
| `dlq_ops` | behaviour | `list_dlq`, `replay_dlq` (G-EVENT-11, `sandloom streams`, admin) |
| `inspection` | behaviour | `group_info`, `stream_info`, `oldest_unacked_age_seconds` |
| `inventory` | behaviour | `register_consumer`, `list_consumers`, `forget_consumer` |

The bus's own facts (`in_process`, `supports_prevalidated`, `supports_preflighted`) are attributes
of the kernel's bus classes, read by kernel code through a kernel type, not through the transport.
The volatile bus (`memory`, `redis`) has no `durable`, so G-EVENT-14 and G-RISK-04 rest on its
absence. **Performance hints.** `publish_many`, `ack_many`: `one_round_trip`.

**Kit (`EventTransportConformance`).** Run against `MemoryTransport` and `streams`: publish and
consume; ack stops redelivery; an unacked delivery returns after backoff; `max_deliveries` moves it
to the DLQ and acks the source only after; `never_dead_letter` keeps redelivering; `dead_letter`
parks at once; `publish_many` order and `PartialPublish` on a failure in the middle; two groups each
receive every entry; ownership: first claim records, a second owner raises `GroupOwnedElsewhere`,
the same owner passes, transfer by CAS, release; `retention()` echoes the options; feature groups as
declared. Stable tier: an outage during `consume` raises `Unavailable` and consuming resumes without
loss. Broken-driver tests: an ack that does not stop redelivery, an ownership claim that accepts a
second owner.

## 8. `taskqueue` and `WorkerHost`

The module-facing contract is the task API, served by a **kernel layer** above the driver (enqueue
dedupe is a kernel guarantee, G-TASK-01..03). The driver door is `TaskQueueProvider.send` plus a
`WorkerHost`. So for this capability the provider ABC does not subclass the module Protocol (ADR-0016
records this exception to ADR-0011 decision 1).

### 8.1 Module door (`sandloom.sdk`, owner decision 4)

**Replaces** `Task` (`sdk/_prim/tasks.py:29-50`, with `s`, `si`, `apply_async`, `delay`),
`shared_task` (:121-165), `cron` (:168-200), `TaskQueueNotInstalled` (:56), the beat hook
`setup_periodic_tasks(sender)` in modules, and the module-facing `TaskQueue.enqueue(task: str, …,
delay_seconds=)` (`contracts.py:1210-1220`).

```python
P = TypeVar("P", bound=BaseModel)

class Task(Protocol[P]):
    name: str
    queue: str
    async def enqueue(
        self, payload: P, *, delay: timedelta | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...
    async def __call__(self, payload: P) -> None: ...

class PeriodicTask(Protocol):
    name: str
    id: str
    cron: str
    async def __call__(self) -> None: ...

@overload
def task(fn: Callable[[P], Awaitable[None]], /) -> Task[P]: ...
@overload
def task(*, queue: str = "default", at_least_once: bool = False
         ) -> Callable[[Callable[[P], Awaitable[None]]], Task[P]]: ...

def periodic(*, cron: str, id: str, queue: str = "default", at_least_once: bool = False
             ) -> Callable[[Callable[[], Awaitable[None]]], PeriodicTask]: ...

class TaskQueue(Protocol):                       # taskqueue(name): enqueue by name
    async def enqueue(
        self, task: str, payload: Mapping[str, Any], *, delay: timedelta | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...
```

- A task is an `async def` taking one pydantic model of wire types (the contract DTO rules) or
  nothing; a sync function is refused when the module's `tasks.py` is imported (a new T-code,
  `idk-task-api`). The body runs on the worker's persistent loop: modules stop calling `run_async`.
- The task name is `f"{fn.__module__}.{fn.__qualname__}"`, which equals today's explicit names
  (`sandloom_billing.tasks.reconcile_operation_intents`); there is no `name=`.
- `enqueue` validates and serialises the payload to JSON before anything is reserved (G-TASK-02
  snapshot), then goes through the kernel layer: with `idempotency_key`, `enqueue_once`
  (`core/tasks/enqueue.py:63`) on the `idempotency` `atomic_store`, then `TaskQueueProvider.send`.
  It returns the task id. The id is generated by the kernel and passed to the driver.
- `at_least_once=True` (today `acks_late=True, reject_on_worker_lost=True`) needs the driver
  feature `redelivery`, derived at load (B037).
- `@periodic(cron="*/5 * * * *", id="billing.reconcile-intents")` declares a periodic task (a task
  without payload). `cron` is five fields, evaluated in UTC. `id` is unique in the deployment
  (duplicate: refused at load). The declarations are read at load like `tasks.py` is today
  (`descriptors/derived_capabilities.py`, `tasks/periodic.py:23`); a module with one needs the feature
  `periodic`. The intervals in use today map to cron (300 s `*/5 * * * *`, 3600 s `0 * * * *`).
- Calling a task inline (`await task(payload)`) runs it in the caller's process, as today.

### 8.2 Driver door

```python
class TaskQueueProvider(InfraProvider):
    capability: ClassVar[str] = "taskqueue"
    contract_version: ClassVar[int] = 2
    async def send(
        self, task: str, payload: bytes, *, task_id: str, delay: timedelta | None = None,
        at_least_once: bool = False,
    ) -> str: ...
    def worker_host(self) -> WorkerHost: ...

class WorkerBoot(Protocol):                      # implemented by the kernel, called by the host
    def boot(self) -> TaskTable: ...             # composition, enforce_runtime, task discovery
    async def run(self, task: str, payload: bytes, *, task_id: str) -> None: ...
    async def on_process_start(self) -> None: ...
    async def on_process_stop(self) -> None: ... # dispose engines on the worker loop (G-TASK-04)

@dataclass(frozen=True, slots=True)
class TaskTable:
    tasks: Mapping[str, TaskSpec]                # name -> queue, at_least_once
    periodic: Sequence[PeriodicSpec]             # id, cron, task name, queue

class WorkerHost(Protocol):
    def run_worker(
        self, boot: WorkerBoot, *, queues: Sequence[str] | None = None,
        concurrency: int | None = None,
    ) -> int: ...                                # blocks; the process exit code
    def run_scheduler(self, boot: WorkerBoot, *, lease: LeaseStore, lease_name: str) -> int: ...
    def check(self, *, timeout_seconds: float) -> HealthResult: ...
```

**Replaces** `TaskQueueProvider.enqueue` and `raw_backend` (`contracts.py:1223-1262`), the
`raw_backend()` call in `core/worker.py:106-123`, `celery -A sandloom.core.worker worker|beat` in
deploy files (owner decision 9: `sandloom worker`, `sandloom beat`, `sandloom worker check`), and
the Celery-only halves of `core/worker.py`, `core/beat_lease.py`, `core/async_runner.py` and
`core/gc_tuning.py`, which move to `sandloom-celery` behind the port.

**Semantics.**

- `send` publishes one message under the given `task_id` (the broker's job id), at most once per
  call. It never retries a publish on its own.
- **Error classes carry the dedupe decision** (G-TASK-03). `InvalidArgument` (unknown task,
  serialisation refused) and `TooLarge` mean nothing was published: the kernel releases the
  reservation (today `TaskNotSent`, `enqueue.py:38`). `Unavailable` and `Timeout` mean the publish
  may have happened: the key stays pending (503 `task_enqueue_uncertain`).
- `delay` without the feature `delay` raises `Unsupported("delay")` (today the parameter is
  accepted and nothing checks it).
- **Execution.** The host runs each task's coroutine through `WorkerBoot.run` on one persistent
  loop per worker process (fresh after fork, refused inside a running loop; G-TASK-04, C-WORKER-04),
  stores no result and prefetches one message (G-TASK-05, C-WORKER-05). With `redelivery`, an
  `at_least_once` task whose worker dies mid-run is redelivered.
- **Scheduler.** `run_scheduler` sends each periodic task at its cron slots only while it holds
  `lease_name` on `lease` (through `FencedLease`), and deduplicates a run by `(id, slot)`: a second
  scheduler that held the lease for the same slot does not send it again (the slot key lives under
  the lease's fence). The lease scope rule of `beat_lease.py:64-71` (broker, taskqueue instance,
  module set) stays, computed by the kernel and passed as `lease_name` (C-WORKER-06).
- `check` answers the worker healthcheck (today the heartbeat file, `worker.py:237-300`).
- `sandloom-celery` keeps a stable importable Celery app for `celery inspect` and Flower: the
  driver's own interface, not a shim (owner decision 9).

**Errors.** `send`: `InvalidArgument`, `TooLarge`, `Unsupported("delay")`, `Unavailable`, `Timeout`,
`PermissionDenied`. `enqueue` (kernel layer) keeps its HTTP errors: `TaskEnqueueConflict` (409),
`TaskEnqueueUnavailable` and `TaskEnqueueUncertain` (503) (`enqueue.py:26-60`).

**Features.**

| feature | kind | meaning |
|---|---|---|
| `delay` | behaviour | `send(delay=)` |
| `periodic` | guarantee | the driver's host runs the scheduler (G-INFRA-11: in at least one process while the deployment runs; a run may repeat) |
| `redelivery` | guarantee | an `at_least_once` task is redelivered when its worker dies |

`idempotency_key` stops being a driver feature: the kernel layer serves it on every driver (it
needs the `idempotency` `atomic_store`, and fails closed without one).
**Performance hints.** none.

**Kit (`TaskQueueConformance`).** `send` then the host runs the task once with the payload; an
unknown task raises `InvalidArgument` and nothing runs; `delay` group (not run before the delay)
and its absence (`Unsupported`); the host's loop is one per process; a scheduler with a cron
`* * * * *` sends once per slot while holding the lease, a second scheduler on the same lease sends
nothing; `check` reports a live worker. Broken-driver tests: a `send` that raises `Unavailable` after
a refused serialisation, a scheduler that ignores the lease.
**Built-ins.** `celery` (sandloom-celery). No `memory` taskqueue (owner answer 7 of infra-deps).

## 9. `objectstore` (module-facing, public)

**Replaces** `ObjectStore` and `ObjectStoreProvider` (`contracts.py:1102-1207`) and the error
classes at :1075-1084. The Protocol keeps its shape; what changes is errors and `url`.

```python
class ObjectStore(Protocol):
    async def put(
        self, key: str, data: ObjectContent, *, content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> str: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def url(self, key: str) -> str: ...                    # feature presigned_get

class ObjectStoreProvider(InfraProvider, ObjectStore):
    capability: ClassVar[str] = "objectstore"
    contract_version: ClassVar[int] = 2
```

**Semantics.** The key grammar of `contracts.py:1138-1155` (relative UTF-8, non-empty segments,
at most 1024 bytes, no dot segments, backslash or NUL). A completed `put` publishes one object; a
source read failure or size refusal keeps the previous object. `get` of an absent key raises
`NotFound` (was `FileNotFoundError`). `delete` of an absent key succeeds. `url` returns a
time-limited retrieval link without checking existence, only with `presigned_get`; without it `url`
raises `Unsupported("presigned_get")` (the local driver raises `FeatureUnsupportedError` today,
`local_objectstore.py:87-93`). `ObjectContent` stops accepting a reader whose `read()` takes no
size (`_UnsizedObjectReader`, :1108).

**Errors.** `put`: `InvalidArgument` (key; was `InvalidObjectKeyError`), `TooLarge` (was
`UploadTooLargeError`), `PermissionDenied`, `Unavailable`, `Timeout`. `get`: `InvalidArgument`,
`NotFound`, `TooLarge` (buffered read limit; was `ObjectTooLargeError`), `PermissionDenied`,
`Unavailable`, `Timeout`. `delete`: `InvalidArgument`, `PermissionDenied`, `Unavailable`. `url`:
`InvalidArgument`, `Unsupported("presigned_get")`.

**Features.** `metadata` (behaviour: content type and metadata are stored and read back),
`presigned_get` (behaviour). **Performance hints.** none.
**Kit (`ObjectStoreConformance`).** Moves to the harness (`make_provider` instead of a `store`
Protocol fixture, so it checks version, features, health and close); adds `NotFound`, the size
errors, `url` with and without `presigned_get` (the test driver that returns `memory://` without
the feature must fail), and `metadata` round trip.
**Built-ins.** `local` (kernel), `s3` (sandloom-s3).

## 10. `notification` (module-facing, public)

Unchanged shape: `Notifier.send(notification: Notification) -> None` and `NotifierProvider`
(`contracts.py:1280-1326`); `contract_version` becomes `1` (int).

**Semantics.** `send` returns after the backend accepted the message for delivery; it is not a
delivery receipt. `dedupe_key` is advisory: a driver may suppress a duplicate, nothing may rely on
it (no feature). `body` and `body_fields` are never logged; `data` may be.

**Errors.** `InvalidArgument` (recipient not valid for the channel), `Unsupported(channel)` (a
channel the driver does not declare), `TooLarge`, `PermissionDenied`, `Unavailable` (retryable:
the caller may retry), `Timeout`. "Raises on a delivery failure" (:1326) becomes this list.

**Features.** `email`, `sms`, `push`, `inapp` (behaviour each). **Performance hints.** none.
**Kit.** `NotifierConformance` gains the error group (a driver built to fail raises an
`InfraError`), `Unsupported` for an undeclared channel, and `dedupe_key` accepted.
**Built-ins.** `log` (`development_only`), `smtp` (tier `beta` through a kit lane against an
in-process SMTP server in CI).

## 11. `database` provider (driver door; `idk-db-provider`)

**Replaces** `DatabaseProvider` (`contracts.py:613-693`). The module door is unchanged
(`AsyncSession` and the `sandloom.sdk.data` forms); renderers keep contract 2 (ADR-0011 amendment of
2026-11-05).

```python
class DatabaseProvider(InfraProvider):
    capability: ClassVar[str] = "database"
    contract_version: ClassVar[int] = 2                 # from idk-db-provider
    def uow_factory(self) -> Callable[[], UnitOfWorkContract]: ...
    def outbox(self) -> OutboxStore | None: ...         # transactional_outbox
    def audit(self) -> AuditStore | None: ...           # audit
    def command_log(self) -> CommandLogStore | None: ...   # command_idempotency
    def request_log(self) -> RequestLogStore | None: ...   # http_idempotency
    def has_pending_changes(self, session: UnitSession) -> bool: ...
    async def ensure_model_schema(self, metadata: MetaData | None = None, *, recreate: bool) -> bool: ...
```

- `execution_adapter()` (:687) goes; `session: Any` becomes a kernel Protocol of a unit's session
  (`UnitSession`, written by `idk-db-provider`; it does not exist yet) and `metadata: Any`
  SQLAlchemy `MetaData` (the driver door may name SQLAlchemy types; the kernel's relational surface
  already does).
- A store method answers `None` exactly when its feature is not declared; the kit checks the pair.
- `_internal/db_provider.py` stops importing driver internals.

**Features** (kinds): `transactions`, `savepoints` (behaviour); `write_admission`,
`transactional_outbox`, `audit`, `command_idempotency`, `http_idempotency`, `tenant_rls`
(guarantee); the renderer features of `capabilities.py:124-140` (behaviour, renderer-declared).
**Kit (`DatabaseProviderConformance`).** The store/feature pairs, a unit commits and rolls back, the
outbox write is atomic with the unit, `has_pending_changes`, `ensure_model_schema`; replaces
`infra-pg-features`.
**Built-ins.** `sqlalchemy` with the `postgresql` and `sqlite` renderers (kernel, this wave).

## 12. Which guarantee rests on which capability and feature

`guarantees.yml` gains, per entry, the capability and feature it needs (`idk-atomic-adopt` and the
slices after it write them). The texts that name the cache or Redis are reworded to name the
capability.

| guarantee | capability (instance) | operations | features it needs | reworded |
|---|---|---|---|---|
| G-REVOKE-01, -02 | atomic_store (`security`) | `get`, `set_subject_state` | `shared`, `never_evicts` (production) | no |
| G-REVOKE-03 | atomic_store (`security`) | `set_subject_state` | `shared`, `never_evicts` | yes: "written with `set_subject_state`", the `set_max` clause goes |
| G-REVOKE-10, -12, -21 | atomic_store (`security`) | `get`: one round trip per request | perf hint `get: one_round_trip` (measured, not read) | "cache" to "security store" |
| G-REVOKE-19 | atomic_store | instance roles `security` / `idempotency` | | yes |
| G-BLOCK-01, -02, -03, -06 | atomic_store (`security`) | `set_subject_state`, `compare_and_set` (unblock) | `shared`, `never_evicts` | G-BLOCK-06: the portable loop lives in drivers |
| G-IDEM-01, -02, -03, -05 | atomic_store (`idempotency`) | `add_or_get`, `compare_and_set` (renew, finalise), `compare_and_delete` (release) | `shared` | no |
| G-IDEM-08, -18 | atomic_store (`idempotency`) | any op raising `Unavailable` / `Timeout` | | no |
| G-IDEM-16 | atomic_store | instance roles | | yes |
| G-IDEM-09..12 | database | `request_log()` | `http_idempotency` | no |
| G-TASK-01, -02, -03 | atomic_store (`idempotency`) + taskqueue | `add`, `get`, `compare_and_set`, `compare_and_delete`; `send(task_id=)` and its error classes | `shared` | G-TASK-03 names the error classes |
| G-TASK-04, -05; C-WORKER-01..05 | taskqueue (`WorkerHost`) | `run_worker`, `WorkerBoot` | | C-WORKER-01, -02: the new commands |
| G-INFRA-11 | taskqueue | `run_scheduler` | `periodic` | "beat hook" to `@periodic` |
| G-OPS-01; C-WORKER-06 | lease (`beat`) + taskqueue | `acquire`, `renew`, `release`; `run_scheduler(lease=)` | lease `shared` | yes: "fenced lease", no "cache" and no "CAS-capable cache" |
| G-SAGA-08 | lease (`beat`) | `acquire`, `renew`, `release` | `shared` | no |
| G-HTTP-02 | cache (`default`) | `windowed_counter`, fail open on `InfraError` | none | no |
| G-INBOX-03 | cache (`default`) | `get`, `set`, fail open | none | no |
| G-RPC-07, -09 | atomic_store (`security`) | `add` with TTL (nonce) | `shared` | "security cache" to "security store" |
| G-PROFILE-01, -06, -07, -08 (instance attestation) | atomic_store (`security`, `idempotency`), cache (`default`) | lifecycle `isolation_identity()` | `never_evicts`, `shared` | yes (owner decision 8): "declares `never_evicts` and differs from the module cache by `isolation_identity()`"; Redis keeps the `run_id` comparison and gains the `noeviction` check |
| G-EVENT-01, -02 | eventbus | `ensure_group`, group ownership | | G-EVENT-02 names `GroupOwnedElsewhere` |
| G-EVENT-03, -05 | eventbus | `aopen`, `health`, `consume`, `ack` | | no |
| G-EVENT-06 | eventbus | `never_dead_letter`, the delivery budget | | no |
| G-EVENT-07 | eventbus | `dead_letter` | | no |
| G-EVENT-09 | eventbus | `ensure_group(start_id=)` | `group_start_id` | no |
| G-EVENT-10 | eventbus (`streams`) | redelivery, reclaim, DLQ order | `durable` | "Streams transport" to "a `durable` transport; on Redis, XAUTOCLAIM" |
| G-EVENT-11 | eventbus | `replay_dlq` | `dlq_ops` | no |
| G-EVENT-12 | eventbus | `retention()`, driver options | | yes: driver options, not settings |
| G-EVENT-14, G-RISK-04 | eventbus drivers `memory` / `redis` (volatile) over broadcast | absence of `durable` | | no |
| G-EVENT-04, -08, -13 | kernel bus logic, no capability | | | no |
| G-MIGRATE-07, -08 (owner transfer) | eventbus | `transfer_group_owner`, `release_group_owner` | | no |
| G-INFRA-10 | every driver | `required_imports`, `install_hint` | | no |
| contract-cache invalidation (ADR-0003, no id) | broadcast | `publish`, `subscribe`, gap on `Unavailable` | `shared` with more than one process | n/a |

## 13. Decided in the spec

For the orchestrator's review; each departs from or details `design.md`.

1. **Kit files.** Kits live in `driverkit/kits/<capability>.py`; `driverkit/testing.py` stays the
   only public path and resolves names lazily from a table `idk-foundation` writes, so capability
   slices never share that file (section 1). The existing kits are in `testing.py` today.
2. **Contracts per file.** Each capability's Protocol, value types and provider ABC live in
   `core/infra/ports/<capability>.py`, module-facing ones included; `ports/__init__.py` exports only
   the kernel-facing ones. `core/infra/contracts.py` keeps the database stores until
   `idk-db-provider`.
3. **Error module.** `sandloom.sdk.infra` is a module, not a package, so the errors are defined in
   `core/infra/errors.py` and exported by name from `sandloom.sdk.infra` and `sandloom.driverkit`
   (design.md wrote `sandloom.sdk.infra.errors`). No error subclasses a built-in exception.
4. **`set_max` dropped from `atomic_store`.** Design.md lists it; no kernel code calls it, and
   revocation writes through `set_subject_state`. G-REVOKE-03 is reworded instead.
5. **`add_or_get` added to `atomic_store`.** The cache-form idempotency guard reserves with
   `set_nx_get` in one round trip (`idempotency/cache_store.py:50-56`); without it the request path
   gains a round trip.
6. **`windowed_counter` is a required cache operation with a performance hint**, not a feature:
   design.md calls it a cache feature, but its meaning is "one round trip", which rule 2 makes a hint.
   The ABC default composes `incr` and `get`.
7. **`isolation_identity()` is on the lifecycle port** of every provider (default `None`, fails
   attestation), not on `atomic_store` alone: attestation compares the security store with the
   evicting `cache`.
8. **`development_only` on the provider class** marks `memory` and `log` drivers, refused in
   production with no accepted risk, separate from `tier`.
9. **Contract majors.** `cache`, `objectstore`, `taskqueue`, `eventbus` go to 2 in this wave;
   `notification` stays 1 as an int; new capabilities start at 1.
10. **`url()` of `objectstore` is gated by `presigned_get`** and raises `Unsupported("presigned_get")`
    without it.
11. **`lease.holder(name)`** is part of the contract (the doctor and the ops endpoint read the
    holder), and `release` keeps the fence (the database driver keeps the row).
12. **Lease drivers on Redis run the `noeviction` check** like `atomic_store.redis`, since the fence
    key has no TTL.
13. **`broadcast` gaps surface as `Unavailable` from the iterator**; the payload floor is 1 MiB.
14. **`eventbus` driver names stay** (`streams`, `memory`, `redis`); only `streams` is a transport
    provider; `redis` becomes the kernel's volatile bus over `broadcast`. `MemoryTransport` is the
    kit reference, not an `INFRA` choice.
15. **Group ownership and `never_dead_letter` are required** eventbus operations, not features; the
    optional diagnostics fold into `inspection` and `inventory`, DLQ inspection and replay into
    `dlq_ops`; `batch_publish`/`batch_ack` become required with loop defaults plus hints.
16. **`retention()` on the transport** replaces the reads of `EVENT_STREAM_RETENTION_SECONDS` in
    `guarantee_windows.py`, since retention becomes a driver option.
17. **Task API shape.** `async def` tasks with one pydantic payload or none; derived names, no
    `name=`; `delay: timedelta`; `at_least_once=` instead of `acks_late`/`reject_on_worker_lost`;
    `@periodic` is cron only, UTC, and makes the function a task by itself.
18. **Taskqueue driver op is `send(task, payload: bytes, task_id=)`** under a kernel layer;
    `idempotency_key` stops being a driver feature; new feature `redelivery`; `InvalidArgument` and
    `TooLarge` mean "not published", `Unavailable` and `Timeout` mean "maybe published".
19. **`notification.dedupe_key` stays advisory** (no feature).
20. **`smtp` reaches `beta`** through a kit lane against an in-process SMTP server.
21. **Health redaction**: `details` carries an error type name, never `str(exc)`.
22. **The Redis `noeviction` check has an operator override.** Managed Redis services often refuse
    `CONFIG GET`; the option `never_evicts_check` is `verify` by default and `assume` lets the
    operator vouch for the policy. With `verify`, a refused read is `PermissionDenied` at `aopen`.

## 14. Open for the owner

- **O1. The instance settings.** `SECURITY_CACHE_INSTANCE`, `IDEMPOTENCY_CACHE_INSTANCE`
  (`config/trust.py:73,93`) and `BEAT_LEASE_CACHE_INSTANCE` (`config/runtime.py:142`) name cache
  instances; after this wave they name instances of `atomic_store` and `lease`. Owner question 6
  covered the flat connection settings, not these. Recommended, by the same rule (one step, upgrade
  notes, no aliases): remove the three settings and use role-named instances, `INFRA.atomic_store.security`,
  `INFRA.atomic_store.idempotency` (absent: the `security` instance) and `INFRA.lease.beat`, listed in
  `config_tiers.REMOVED` so a leftover setting fails startup with the fix. The alternative is to
  rename them (`SECURITY_STORE_INSTANCE`, `IDEMPOTENCY_STORE_INSTANCE`, `BEAT_LEASE_INSTANCE`). It
  blocks `idk-atomic-adopt` and `idk-lease`, not `idk-foundation`.
