# Spec: kernel ports and driver contracts, per capability

Status: **written for `idk-adr`, 2026-11-14**, against `design.md` at `9330edfe` (all nine owner
questions decided); **revised by `idk-adrb`** with the decided findings of the security and
ADR-conformance reviews. The decision record is [ADR-0016](../../docs/adr/0016-kernel-ports-and-driver-packages.md);
this file is the contract every later `idk-*` slice builds to. Line references are to `aefd3e52`
(the base of the design commits; they change no code).

Read the section of your capability, plus "Shared rules" and "Module path layout". What this spec
had to decide that `design.md` left open is listed at the end under "Decided in the spec". Nothing
is open for the owner.

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
| one capability's contract | `src/sandloom/core/infra/ports/<capability>.py` | the operation Protocol(s), value types, the provider ABC and the capability's `CapabilitySpec` constant (major, features with their kinds), module-facing and kernel-facing alike |
| kernel door | `src/sandloom/core/infra/ports/__init__.py` | exports the kernel-facing Protocols only: `AtomicStore`, `LeaseStore`, `Broadcast`, `EventTransport`, `WorkerHost`, `ProviderLifecycle`; written once by `idk-foundation` as a lazy table (PEP 562, like `testing.py` below), so a slice that adds one of these names never edits it |
| module door | `src/sandloom/sdk/infra.py` | re-exports the module-facing Protocols (`Cache`, `ObjectStore`, `Notifier`, `TaskQueue`), the errors and the accessors |
| driver door | `src/sandloom/driverkit/__init__.py` | re-exports every provider ABC, value type and error a driver needs |
| one capability's kit | `src/sandloom/driverkit/kits/<capability>.py` | `<Capability>Conformance`; the shared harness is `driverkit/kits/_harness.py` |
| public kit names | `src/sandloom/driverkit/testing.py` | the only import path for kits (pinned in the API baseline); resolves names lazily from a table, see below |
| built-in drivers until the split | `src/sandloom/core/drivers/<backend>_<capability>.py` | `redis_atomic_store.py`, `memory_lease.py`, `database_lease.py`, `database_atomic_store.py`, `celery_worker.py`, … |
| built-in drivers after the split | `packages/sandloom-redis/src/sandloom_redis/<capability>.py`, `packages/sandloom-celery/src/sandloom_celery/<capability>.py` | slices 17 and 18 move the files whole |
| tables of the `database` drivers | `src/sandloom/core/sql_alchemy/models/<table>.py`, a revision in the core chain `migrations/core/versions/`, the table name in the owned set of `migrations/core/env.py` | `idk-lease` (`infra_lease`), `idk-atomic-store-db` (`infra_atomic_store`) |
| shared by several slices | `core/infra/capabilities.py`, `core/drivers/bootstrap.py`, `core/infra/access.py`, `core/infra/registry.py`, `core/infra/ports/__init__.py`, `driverkit/testing.py` | touched once by `idk-foundation`, see "Registry files" below |

**Registry files.** `idk-foundation` writes them whole for this wave, so no later slice edits them:

- `capabilities.py` lists the `CapabilitySpec` constants and imports them from `ports/<capability>.py`.
  Foundation moves the existing constants there unchanged (the module-facing contracts of `cache`,
  `eventbus` and `taskqueue` stay defined in `contracts.py` and are re-exported from their ports file
  until their slice replaces them) and writes the final contracts of `atomic_store`, `lease` and
  `broadcast`, of `objectstore` and `notification` (their changes are errors only), and the base.
  A capability slice then changes its contract, major and features in its own ports file.
- `bootstrap.py` pre-registers **every** built-in driver row of this wave, each as a lazy
  `"module:Class"` target resolved when an instance selects it (as entry points are, ADR-0011
  decision 2), with its `tier`. A row whose file a later slice has not delivered yet is refused
  when selected (SANDLOOM-B022) with a message naming that slice. Slices 17 and 18 moved the files
  and kept the rows as lazy install-hint rows (target `sandloom_<pkg>…`, `requires` naming the
  package); `idk-config-defaults` (slice 19) deleted them with the hard-coded defaults (13, item
  45): the kernel keeps rows only for its own drivers, and a driver package is found by its entry
  point alone. The kernel selects no driver for `database`, `cache`, `eventbus`, `broadcast`,
  `taskqueue`, `atomic_store` or `lease`: the composition does (`create_app(default_infra=...)`,
  `sandloom_standard.DEFAULT_INFRA`), and a process whose composition and `INFRA` select none for
  an instance it cannot start without refuses with SANDLOOM-B037 in every environment, never
  falling back to `memory`.
- `access.py` gains `atomic_store(role)`, `lease(name)` and `broadcast(name)`, typed by the ports
  Protocols. `idk-atomic-adopt` removes `security_cache()` and `idempotency_cache()` when it moves
  their last caller.

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

The streams transport is about 1,475 lines in four files (`streams_transport.py`,
`streams_transport_base.py`, `streams_consume.py`, `streams_groups.py`); `idk-eventbus-move`
moves them as a package `core/drivers/redis_eventbus/` (one file per current file) when it makes them
an `EventTransportProvider`, and `idk-pkg-redis-streams` moves that package whole into `sandloom_redis/eventbus/`.

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
  a key-value lookup. `None` is only for a key known absent: a present value that cannot be read
  (a decode failure, a backend error, a timeout) raises, so a security read never mistakes a
  failure for "no revocation mark".
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
    tier: ClassVar[Literal["experimental", "beta", "stable"]] = "experimental"  # see 2.5
    development_only: ClassVar[bool] = False              # refused in production (memory)
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
- Majors after this wave: `cache` 2, `objectstore` 2, `notification` 2, `taskqueue` 2, `eventbus` 2
  (their obligations change: `notification` gains the error list of section 10 as `objectstore`
  does), `database` 1 until `idk-db-provider` (then 2), and `atomic_store`, `lease`, `broadcast` 1.
  `WorkerHost` is not a capability and has no major: it is reached through
  `TaskQueueProvider.worker_host()` and versioned with `taskqueue` (section 8). A driver written for
  another major is B022, so an old `sandloom-s3` is refused at load instead of raising
  `FileNotFoundError` where `NotFound` is due.
- `register_driver(..., features=...)` in `core/drivers/bootstrap.py:88-157` stops passing features;
  the registry reads `features_for`. A built-in that overrides `features()` is a test failure.
- `instance_features` on `DriverRegistration` (`registry.py:110`) goes: `features_for(options)` is it.
- `features_for(options)` is read within `supported_features`, itself within the spec's features
  (checked at load): the registry's preflight and `features()` both keep only declared features,
  so a driver whose `features_for` answers more never claims a guarantee it did not declare
  (`durable`, `tenant_rls`). A driver whose features depend on an option declares the union in
  `supported_features`.

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
- **`health`.** Bounded by the registry with `settings.health_check_timeout_seconds` (as
  `registry.py:1114` does); a timeout is `fail`. The registry's own failure record stops carrying
  `str(exc)` (`registry.py:1117-1118` does today) and carries the type name. `HealthResult.details` carries only non-secret facts: the driver
  name, an error **type** name, a latency, a count. Never `str(exc)`, a URL, a host with
  credentials or a key. The kit checks this with a driver whose backend raises an error carrying a
  marker secret. The registry does not vouch for a driver's text: in `health_all` a `details`
  value passes when it is `None`, a bool, a number or one token (`[A-Za-z0-9_.:-]{1,64}`: a type
  name, a driver name, `redis:<run_id>`); any other string becomes `"<redacted>"`, mappings and
  lists are filtered the same way. The kernel's own readers (`aggregate_health`, the capability
  probes of `/health` and `/ready`) record a failure's type name only, plus `missing_module` (the
  module name, one token) for an `ImportError`: an operator fact that carries no secret.
- **`isolation_identity`.** An opaque string naming the failure and eviction domain of the backing
  store: two instances with the same identity share eviction and memory. It starts with the driver
  name and a colon, so two drivers never collide: Redis `redis:<run_id>` from `INFO server` (today
  `RedisCache.deployment_identity`, `redis_cache.py:775`); database `database:<system
  identifier>/<database name>`; `memory` `memory:<per-process id>`; other capabilities name their
  backing store the same way (`smtp:<host>:<port>`, `s3:<endpoint>/<bucket>`). It carries no
  credential. The ABC default is `None` (unknown), which fails the kit and fails attestation
  closed; a call that raises also fails attestation (as `production_profile.py:291-295` does today).
- **Shared clients.** A driver package may share one client across its providers of several
  capabilities (keyed by connection options). The kernel never reaches that client: no `raw_client`,
  no `raw_backend`, no `native()` (owner decision 5). A package may document public attributes of
  its own class as non-portable.

### 2.5 Tiers

`tier` is a field of `DriverRegistration` (owner decision 7), and the registry, `sandloom doctor`,
`/diagnostics/infra` and B023 read it there, never on the class. Built-ins pass it to
`register_driver(..., tier=)` in `bootstrap.py`. An entry-point driver has no registration call, so
the registry fills the registration's `tier` from the class attribute `tier` (default
`experimental`) when it loads the entry point: the class attribute is how an external package
states its tier, the registration is what is read. `experimental`: implements the contract. `beta`: its kit passes in CI against
the real service. `stable`: beta plus the kit's reconnect-after-outage group and its auth options.
In production an `experimental` driver needs the accepted risk `experimental_driver` (SANDLOOM-B023,
extended from capabilities to drivers); `beta` logs one startup warning; `stable` is silent. This
repository's drivers are at least `beta`. `development_only` drivers (`memory`) are refused in
production whatever their tier, with no accepted risk. The `log` notifier is not `development_only`:
in production it advertises its channels only under the existing accepted risk
`log_notifier_in_prod`, and none without it.

### 2.6 Test isolation and metrics

- `test_isolation(options, worker_id) -> options`: what one pytest-xdist worker's instance uses so
  that parallel runs do not share state. It takes the instance's options because the answer is
  derived from them (another database index of the configured URL); design.md's
  `test_isolation(worker_id)` could not know the URL. Redis: another database index or key prefix. Database: the
  harness's database per worker. Default: the options unchanged (fine for `memory`). Replaces the
  URL rewriting in `core/testing/workers.py:54-65`.
- Drivers record metrics through a driverkit hook (`idk-driver-deps`): `sandloom.driverkit.observe`
  with `counter(name, documentation, labelnames)` and `histogram(...)`, owned by the kernel's
  registry. A driver imports no `prometheus_client` registry of the kernel and no kernel settings.

### 2.7 Kits (one harness)

Every kit is a pytest class with a `make_provider` fixture returning an unopened provider. The
harness (`kits/_harness.py`) opens it, closes it twice, and runs these groups before the
capability's own:

1. it is an instance of the provider ABC the kit names and satisfies the Protocol that ABC
   implements (for `eventbus` that is `EventTransportProvider` / `EventTransport`, the driver-door
   shape; for `taskqueue` it is `TaskQueueProvider`, which implements no module Protocol);
2. `contract_version` is an `int` equal to the capability's major;
3. `supported_features` ⊆ the spec's features, `features()` equals `features_for(options)`;
4. `performance_hints` names only operations of the capability;
5. `development_only` is a `bool`; `health()` answers within the bound and its `details` pass the
   redaction rule (the tier is earned by the kit, not checked by it: it is read from the
   registration, 2.5);
6. `isolation_identity()`: two providers built from the same options, and one built under another
   instance name with the same driver and options, return equal, non-`None` identities, each
   starting with `<driver>:` (the identity names the backing store, not the instance);
7. each feature group runs only when the feature is declared, and a declared-absent behaviour
   feature raises `Unsupported(feature)` from its call;
8. the error-mapping group: a provider built to fail (the kit's `make_failing_provider` fixture,
   optional) raises only `InfraError` subclasses, and the secret it was built with (the kit's
   `leak_marker`) appears in no error message and no health detail, keys included.

The kit itself is tested against broken drivers in `src/sandloom/core/tests/test_driverkit.py`: one
broken driver per obligation (a wrong version type, a feature not in the spec, a non-atomic
`add`, an `Unsupported` raised for a declared feature, a health `details` with a secret, an
`isolation_identity()` of `None` or without the driver prefix, …), each
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
    # major 2: each driver class states contract_version = 2, the ABC does not (13.37)
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
    # major 1: each driver class states contract_version = 1, the ABC does not (13.37)
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
- **An expired key is absent for every operation**, also while the backend still holds the row:
  `add` and `add_or_get` create over it, `compare_and_set` and `compare_and_delete` answer `False`
  on it, and `set_subject_state` writes over it as over an absent key. Otherwise a revocation
  write could fail, or a stale reservation answer 409, until a sweep runs.
- `add_or_get` (was `set_nx_get`): `(True, None)` when this call created the key, else
  `(False, value)` with the value that blocked it; atomic, so `value` is never `None`.
- `set_subject_state`: the value domain is `parse_subject_state` (`contracts.py:1024`):
  `v1:after:<decimal>` or `v1:blocked:<decimal>`. Writes only when the value ranks above the stored
  one (every `blocked` above every `after`; within a kind the decimal instant, compared exactly as
  a decimal number: `decimal.Decimal` in Python, `numeric` in SQL, never a float and never text);
  a stored value outside the domain is replaced. Exactness matters: a revocation writes instants
  that differ from the previous mark only past the 15th significant digit
  (`auth/marks.py:213-224`), which a float comparison loses and a text comparison misorders. Returns the value held after the call. A value
  outside the domain raises `InvalidArgument`. `ttl_seconds` applies only when written.
- `isolation_identity()` (lifecycle) is required to return a value for a driver that declares
  `shared`.
- **`set_max` is not part of the contract.** No kernel code calls it (`grep set_max` finds only
  the contract, the Redis driver and a test fake); revocation writes through `set_subject_state`
  (`auth/revocation.py:228-234`). See "Decided in the spec".

**Instances.** The kernel uses two by role, named in `INFRA`: `INFRA.atomic_store.security`
(revocation marks, blocks, RPC nonces) and `INFRA.atomic_store.idempotency` (cache-form idempotency
guard, enqueue dedupe, command cache fallback). An absent `idempotency` instance is the `security`
instance. The settings that named cache instances for these duties, `SECURITY_CACHE_INSTANCE` and
`IDEMPOTENCY_CACHE_INSTANCE` (`config/trust.py:73,93`), are removed in one step and listed in
`config/tiers.py` `REMOVED`, so a leftover setting fails startup with the fix (`idk-atomic-adopt`,
which also moves their readers off them: `migrations/backup.py:140-141`,
`production_profile.py:132,146,277-279`, `infra/access.py:65`). **In production an absent
`INFRA.atomic_store.security` is a startup refusal** in `config_problems`
(`production_profile.py:105`); the instance is never built from the cache's options or from
`memory`.

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

- `redis`: keeps the current Lua scripts (one round trip each), each treating an expired key as
  absent (Redis does by itself). On `aopen` and in `health` it reads `maxmemory-policy` and refuses
  unless it is `noeviction` (no such check exists today). The option `never_evicts_check` is
  `verify` (default) or `assume`:
  - with `verify`, a refused `CONFIG GET` is `PermissionDenied` at `aopen`, with a fix naming the
    option;
  - `assume` only skips a **refused** read: a policy that can be read and is not `noeviction` is
    still refused, and `health` keeps trying the read on every probe (it reports `degraded` with
    the reason while the read stays refused);
  - in production `assume` needs the accepted risk `unverified_noeviction` (the B023 mechanism,
    listed by `sandloom doctor`); without it startup refuses.

  `isolation_identity` is `redis:<run_id>`.
- `memory`: `development_only`, declares `never_evicts` only.
- `database` (`idk-atomic-store-db`, an option): a logged table `infra_atomic_store (key primary key,
  value, expires_at)` in the core chain. Every statement treats a row with
  `expires_at <= now()` as absent, as the lease does (section 5):
  - `add`: `INSERT … ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, expires_at =
    EXCLUDED.expires_at WHERE infra_atomic_store.expires_at <= now() RETURNING key`; a returned row
    is `True`.
  - `add_or_get`: the same insert; when it returns no row, a read of the live value in the same
    transaction. A read that finds the key expired in between retries the insert (bounded; past
    the bound `Unavailable`), so the answer's value is never `None`.
  - `compare_and_set`: `UPDATE … WHERE key = :key AND value = :expected AND (expires_at IS NULL OR
    expires_at > now())`; `compare_and_delete`: `DELETE` with the same condition.
  - `set_subject_state`: in one transaction, the row read `FOR UPDATE` (expired: absent), ranked in
    the driver with `parse_subject_state` and `decimal.Decimal`, then written; an absent key is
    written by the insert of `add` and the loop repeats once when that insert loses.
  - `get` filters expired rows; the driver's own periodic task purges them later, on its own
    engine, with the cutoff computed by the database.

  It declares `shared` and `never_evicts`. Its price is stated in its docs page: +0.2-0.7 ms per
  authenticated request; measured on a write, +1.3 ms for `set_subject_state` (about four times
  Redis) and +1.1 ms for `add_or_get` on a held key.

**Kit (`AtomicStoreConformance`).** The cache kit's key-value groups minus eviction tolerance; 50
concurrent `add`s, one winner; 50 concurrent `add_or_get`s, one creator and every loser sees the
winner's value; CAS races (one winner of N); `compare_and_delete` by a stale owner fails;
`set_subject_state`: `blocked` outranks `after`, a lower instant is ignored, equal instants keep
the stored value, two instants that differ only past the 15th significant digit rank by that digit
(both orders), a corrupt stored value is replaced, an out-of-domain value raises, the TTL applies
only when written, 50 concurrent writers end at the maximum; TTL is never shortened by a losing
write; expired keys (1 s TTL, read after 2 s, before any purge): `add`, `add_or_get`,
`compare_and_set`, `compare_and_delete` and `set_subject_state` behave as on an absent key; group
`shared`: two providers built from the same options see each other's writes; group
`never_evicts`: the driver's own eviction check (Redis: a server with `allkeys-lru` is refused at
`aopen`, also with `never_evicts_check=assume`). Broken-driver tests: a non-atomic `add`, a
`set_subject_state` that lets `after` overwrite `blocked`, one that compares instants as floats, a
CAS that ignores `expected`, an `add` that loses to an expired key.

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
    # major 1: each driver class states contract_version = 1, the ABC does not (13.37)
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
- **Instance.** The kernel uses the instance `INFRA.lease.beat` (beat and saga sweep;
  `sagas/sweep.py:130-139`). The setting `BEAT_LEASE_CACHE_INSTANCE` (`config/runtime.py:142-145`,
  read at `beat_lease.py:334,410`) is removed in one step and listed in `config/tiers.py` `REMOVED`
  (`idk-lease`).

**Errors.** `InvalidArgument` (TTL `< 3`, empty name), `Unavailable`, `Timeout`. A lost lease is a
result (`False`), never an exception.

**Features.** `shared` (guarantee: every process of the deployment contends for the same lease;
`memory` lacks it). The kernel requires `shared` of `beat` in production.
**Performance hints.** none.

**Driver obligations.**

- `redis`: the current shape: a fence counter key without TTL (`INCR`), the lease key by `SET NX EX`,
  renew and release by compare-and-set and compare-and-delete scripts. The fence key must not be
  evicted, so the driver runs the `noeviction` check of `atomic_store.redis` on `aopen` and in
  `health`, with the same `never_evicts_check` option and the same accepted risk
  `unverified_noeviction` for `assume` in production (Redis' default policy passes it).
- `memory`: `development_only`.
- `database` (owner decision 3, yes): a logged table `infra_lease (name primary key, owner, token,
  expires_at, fence)` in the core chain (section 1), in `core/drivers/database_lease.py`. Acquire is
  one statement: `INSERT … ON CONFLICT (name) DO UPDATE SET owner, token, expires_at = now() + ttl,
  fence = infra_lease.fence + 1 WHERE infra_lease.expires_at <= now() RETURNING fence`. Renew and
  release are `UPDATE … WHERE name = :name AND token = :token AND expires_at > now()` (an expired
  lease is not held, as on Redis where its key is gone); release sets `expires_at = now()` and keeps
  the row, so the fence survives. No advisory lock (they break under pgbouncer transaction pooling).

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
    # major 1: each driver class states contract_version = 1, the ABC does not (13.37)
```

**Semantics.**

- **Online only, at most once.** A message reaches the subscriptions that are live when it is
  published. No persistence, no replay, no acknowledgement. `publish` succeeding means the backend
  accepted it, not that anyone received it.
- **No silent loss on a live subscription.** A subscription delivers every message published to its
  channels while it is live, or its iterator raises `Unavailable`: a driver whose buffer overflows
  or whose connection drops raises; it never skips a message and carries on. What `broadcast` does
  not bound (a lost `publish`, a subscriber that was not live) stays bounded by its user: the
  contract-cache shadow's TTL bounds staleness (`contract_cache_shadow.py:851`).
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
subscription; a subscriber that does not read while 10,000 messages are published either receives
all of them in order or its iterator raises `Unavailable`; `aclose` ends iteration; group `pattern_subscribe` (prefix match, no match on another
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

**One capability, two driver shapes.** Every `eventbus` registration is an `InfraProvider` with
`capability = "eventbus"` and major 2, and the kernel reads every one through `ProviderLifecycle`
and `features()`. What differs is what the kernel builds over it:

- **Transport shape** (`streams`, and every external driver: NATS, Kafka, …): the class is an
  `EventTransportProvider` (below), exported from `sandloom.driverkit`, and the kernel builds its
  durable bus over it. This is the only shape a driver package can implement, and the only one
  `EventTransportConformance` tests.
- **Volatile-bus shape** (`memory`, `redis`): the class is a kernel-internal `VolatileBusProvider`
  (in `core/eventing`, not exported from driverkit) that implements no `EventTransport`; the kernel
  builds its volatile bus from it (`memory_bus.py` in process, `pubsub_bus.py` over a `broadcast`
  instance). Its tests are the volatile buses' kernel tests, not the transport kit.

The in-memory transport (`memory_transport.py`, `MemoryTransport`) is the kit's reference transport
and the test harness's, not an `INFRA` choice.

**Feature vocabulary.** The features of today's `EVENTBUS_CAPABILITY` (`core/infra/capabilities.py:213-231`)
and `EVENTBUS_DRIVER_FEATURES` (`core/drivers/event_bus_driver.py:119-129`) map as below. Feature
names a module declares in `CapabilityRequirement("eventbus", features=...)` are public API: a
removed name is refused at load like any undefined feature (`registry.py:992-998`), and
`EVENTBUS_CAPABILITY` carries a rename table so the refusal's fix names the replacement. Upgrade
note in `idk-eventbus-contract`, no alias.

| today | after | kind | why |
|---|---|---|---|
| `publish` | removed | | a required operation of every driver (rule 2.2: never a feature); drop it from requirements |
| `subscribe` | removed | | as `publish` |
| `volatile` | removed | | the absence of `durable` says it; G-EVENT-14 and G-RISK-04 rest on that absence |
| `durable` | `durable` | guarantee | unchanged name; `app_factory/__init__.py:408` keeps refusing a production bus without it |
| `at_least_once` | removed, use `durable` | | every transport delivers at least once (Semantics below), and only transports declare `durable` |
| `consumer_groups` | removed, use `durable` | | groups are required operations of every transport |
| `wildcard_subscribe` | `wildcard_subscribe` | behaviour | volatile-bus shape only: a `prefix.*` subscription; `memory` declares it, `redis` declares it and refuses at `aopen` a `broadcast` instance without `pattern_subscribe`; no transport declares it (`streams_bus.py:261` refuses it today) |
| (new) | `shared`, `blocking_consume`, `group_start_id`, `lease_renewal`, `dlq_ops`, `inspection`, `inventory` | see the table below | transport shape |

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
    # major 2: each driver class states contract_version = 2, the ABC does not (13.37)
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
- **Retention** is a `streams` driver option (`retention_seconds`, `max_length`), reported by
  `retention()`. The settings `EVENT_STREAM_RETENTION_SECONDS` and `EVENT_STREAM_MAXLEN`
  (`config/eventing.py:56-68`) are removed in one step, listed in `config/tiers.py` `REMOVED`
  (they are tiered at `tiers.py:506-509`), with upgrade notes and no alias (design section 5: flat
  settings become driver options). Every reader moves to `retention()` or the options:
  `core/guarantee_windows.py:93`, `core/projections/operations.py:139`, the settings fallback of
  `streams_transport_base.py:107-112`, and the deploy-profile list `core/testing/deploy_profile.py:80-81`.
  `idk-eventbus-contract` adds the options and `retention()` and moves the three code readers;
  `idk-config-defaults` removes the two settings with the other flat settings (until then
  `core/infra/config.py` fills the options from them, as it does for `CACHE_URL`).
  `EVENT_STREAM_PREFIX` becomes the `streams` option `prefix` (the namespace of every Streams key)
  the same way: filled from the setting until `idk-config-defaults` removes it, and filled by the
  `override_instance` test seam so a streams override stays namespaced.
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
`shared_task` (:121-165), the Celery `cron` helper (:168-200, which returns a Celery `crontab`; it
goes, and `@periodic(cron=...)` takes the expression as a string), `TaskQueueNotInstalled` (:56), the
beat hook `setup_periodic_tasks(sender)` in modules, and the module-facing `TaskQueue.enqueue(task:
str, …, delay_seconds=)` (`contracts.py:1210-1220`).

```python
P = TypeVar("P", bound=BaseModel)
R = TypeVar("R", None, Awaitable[None])          # a sync or an async body

class Task(Protocol[P, R]):
    name: str
    queue: str
    async def enqueue(
        self, payload: P, *, delay: timedelta | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...
    def __call__(self, payload: P) -> R: ...      # inline call: the function as written

class PeriodicTask(Protocol[R]):
    name: str
    id: str
    cron: str
    max_interval: timedelta                       # longest gap between two slots of `cron`
    def __call__(self) -> R: ...

@overload
def task(fn: Callable[[P], R], /) -> Task[P, R]: ...
@overload
def task(*, queue: str = "default", at_least_once: bool = False
         ) -> Callable[[Callable[[P], R]], Task[P, R]]: ...

def periodic(*, cron: str, id: str, queue: str = "default", at_least_once: bool = False
             ) -> Callable[[Callable[[], R]], PeriodicTask[R]]: ...

class TaskQueue(Protocol):                       # taskqueue(name): enqueue by name
    async def enqueue(
        self, task: str, payload: Mapping[str, Any], *, delay: timedelta | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...
```

- A task is a module-level function, **sync or async** (Django 6 allows both; the owner accepted
  that shape), taking one pydantic model of wire types (the contract DTO rules) or nothing. There is
  no new refusal of sync functions. The worker runs an `async def` body on its persistent loop
  (through `run_async`) and a sync body directly in the worker process, outside any running loop,
  so a sync body that calls `run_async` keeps working; new code and the generator write `async def`.
- The task name keeps today's derivation, `f"{fn.__module__}.{fn.__name__}"`
  (`sdk/_prim/tasks.py:91`, also Celery's default), which equals today's explicit names
  (`sandloom_billing.tasks.reconcile_operation_intents`, `sandloom_identity.tasks.cleanup_expired_refresh_tokens`);
  there is no `name=`. Two tasks with one derived name (a function redefined in one module) are
  refused at load.
- **Before anything is reserved**, the kernel layer checks what it can know without the broker,
  so a refusal never leaves a dedupe key behind (a pending key has no TTL, `enqueue.py:86`): the
  payload validates and serialises to JSON (G-TASK-02 snapshot), the task name is in the task table,
  and a `delay` needs the instance's `delay` feature (`Unsupported("delay")` raised here). Then it
  goes through the kernel layer: with `idempotency_key`, `enqueue_once` (`core/tasks/enqueue.py:63`)
  on the `idempotency` `atomic_store`, then `TaskQueueProvider.send`. It returns the task id. The id
  is generated by the kernel and passed to the driver.
- `at_least_once=True` (today `acks_late=True, reject_on_worker_lost=True`) needs the driver
  feature `redelivery`, derived at load (B037).
- `@periodic(cron="*/5 * * * *", id="billing.reconcile-intents")` declares a periodic task (a task
  without payload). `cron` is five fields, evaluated in UTC. `id` is unique in the deployment
  (duplicate: refused at load). The declarations are read at load like `tasks.py` is today
  (`descriptors/derived_capabilities.py`, `tasks/periodic.py:23`); a module with one needs the feature
  `periodic`. The `cron` helper of `sandloom.sdk` goes with Celery's `crontab` (upgrade note: pass
  the expression to `@periodic`).
- Every interval in use today maps to cron: billing's reconciler, 300 s
  (`RECONCILE_INTERVAL_SECONDS`, `sandloom_billing/services/reconciler_health.py:21`) to
  `*/5 * * * *`; identity's refresh-token cleanup, 3600 s (`sandloom_identity/tasks.py:108`), to
  `0 * * * *`; identity's revoked-token cleanup, 86400 s (`sandloom_identity/tasks.py:115`), to
  `0 3 * * *` (a fixed daily slot instead of "24 h after the scheduler started").
- Billing's reconciler heartbeat derives its interval from the periodic declaration
  (`reconcile_operation_intents.max_interval`, the longest gap between two consecutive slots of
  its cron, computed when the decorator runs), not from a second constant:
  `RECONCILE_INTERVAL_SECONDS` goes and cannot drift from the cron string.
  `RECONCILE_MAX_SILENCE_SECONDS` stays three intervals.
- Calling a task inline runs it in the caller's process, as today: `await task(payload)` for an
  `async def`, `task(payload)` for a sync one.
- `idempotency_key` stops being a taskqueue feature (below): a module that declared
  `CapabilityRequirement("taskqueue", features={"idempotency_key"})` drops it (upgrade note; the
  undefined name is refused at load like any other).

### 8.2 Driver door

```python
class TaskQueueProvider(InfraProvider):
    capability: ClassVar[str] = "taskqueue"
    # major 2: each driver class states contract_version = 2, the ABC does not (13.37)
    async def send(
        self, task: str, payload: bytes, *, task_id: str, delay: timedelta | None = None,
        at_least_once: bool = False, queue: str = "default",
    ) -> str: ...
    def worker_host(self) -> WorkerHost: ...     # added by idk-worker-port, see below

@dataclass(frozen=True, slots=True)
class TaskTable:                                 # idk-task-api
    tasks: Mapping[str, TaskSpec]                # name -> queue, at_least_once
    periodic: Sequence[PeriodicSpec]             # id, cron, task name, queue

class WorkerBoot(Protocol):                      # implemented by the kernel, called by the host
    def boot(self) -> TaskTable: ...             # composition, enforce_runtime, task discovery
    def run(self, task: str, payload: bytes, *, task_id: str) -> None: ...  # outside any running loop
    def before_fork(self) -> None: ...           # once in the main process after boot, before children fork (GC freeze)
    def on_process_start(self) -> None: ...      # a process that runs messages starts (fresh loop after a fork)
    def on_process_stop(self) -> None: ...       # dispose engines on the worker loop (G-TASK-04)
    def run_coroutine(self, coro: Coroutine[Any, Any, T]) -> T: ...  # on the persistent loop (the scheduler's lease store)

class WorkerHost(Protocol):
    def run_worker(
        self, boot: WorkerBoot, *, queues: Sequence[str] | None = None,
        concurrency: int | None = None, loglevel: str = "INFO",
    ) -> int: ...                                # blocks; the process exit code
    def run_scheduler(
        self, boot: WorkerBoot, *, lease: LeaseStore, lease_name: str,
        lease_ttl_seconds: int = 30, loglevel: str = "INFO",
        state_file: str | None = None,
    ) -> int: ...
    def check(self, *, timeout_seconds: float) -> HealthResult: ...
```

The process model is not an argument of the port (idk-pkg-celery, review of PR 221: a pool is Celery
vocabulary, not what every host has). It is the `celery` option `worker_pool: Literal["prefork",
"solo", "threads"] = "prefork"`, set in `INFRA` on the taskqueue instance, and the host reads it from
its own options. The `solo` and `threads` pools run messages in the host's own process, so the
Celery host calls `on_process_start` / `on_process_stop` itself there (no pool child announces
itself) and never calls `before_fork` (nothing forks, so no heap is frozen for children). Before it
boots anything the host raises `InvalidArgument`, naming the option or setting, for `prefork` where
billiard does not fork its children (macOS: spawned children never see the boot-registered table;
set `solo` or `threads`), for `WORKER_MAX_MEMORY_PER_CHILD` or `WORKER_MAX_TASKS_PER_CHILD` with
`solo` or `threads` (they recycle prefork children, which those pools lack) and for
`WORKER_CONCURRENCY` above 1 with `solo`. `solo` runs a message inside the loop that fires the
host's liveness timer, so in `solo` and `threads` the heartbeat file is touched from a daemon thread
(a task longer than the `check` window must not read as a dead worker); the file then no longer
shows a loop wedged by something other than a task.
`loglevel` is the process's log level (`sandloom worker|beat --loglevel`). `lease_ttl_seconds` is
`BEAT_LEASE_TTL_SECONDS`, and `state_file` the scheduler's state file (`sandloom beat --schedule`,
ignored by a host that keeps none). Both stay kernel-owned: they are the kernel's lease and file
settings, passed through the port, and not options of any driver.

**`WorkerHost` is not a capability.** It has no `INFRA` entry, no registration and no major of its
own: the kernel reaches it only through `TaskQueueProvider.worker_host()`, and it is versioned with
`taskqueue`. `ports/__init__.py` exports it as a kernel-facing Protocol.

**Which slice writes what.** `idk-task-api` writes `ports/taskqueue.py` with the module door,
`TaskQueueProvider.send`, `TaskTable`, `TaskSpec` and `PeriodicSpec` (it derives the table from the
declarations), and keeps today's `core/worker.py` running the table until the port exists.
`idk-worker-port` adds `WorkerBoot`, `WorkerHost` and the abstract `worker_host()` to the same file,
the Celery host behind them, and the kit's host groups (execution, scheduler, `check`). Both land
before the wave ends, so `taskqueue` major 2 includes `worker_host()`; no external taskqueue driver
exists in between.

**Replaces** `TaskQueueProvider.enqueue` and `raw_backend` (`contracts.py:1223-1262`), the
`raw_backend()` call in `core/worker.py:106-123`, `celery -A sandloom.core.worker worker|beat` in
deploy files (owner decision 9: `sandloom worker`, `sandloom beat`, `sandloom worker check`), and
the Celery-only halves of `core/worker.py`, `core/beat_lease.py`, `core/async_runner.py` and
`core/gc_tuning.py`, which move to `sandloom-celery` behind the port.

**Semantics.**

- `send` publishes one message under the given `task_id` (the broker's job id), at most once per
  call. It never retries a publish on its own.
- `queue` is the queue the task declares (`@task(queue=...)`); the kernel layer passes it from the
  declared task on every enqueue, so an enqueue from a process that never booted the worker table
  still lands in the declared queue. A driver sends to `queue` (the default queue needs no routing).
- **Error classes carry the dedupe decision** (G-TASK-03). `InvalidArgument` (unknown task,
  serialisation refused) and `TooLarge` mean nothing was published: the kernel releases the
  reservation (today `TaskNotSent`, `enqueue.py:38`). `Unavailable` and `Timeout` mean the publish
  may have happened: the key stays pending (503 `task_enqueue_uncertain`).
- `delay` without the feature `delay` raises `Unsupported("delay")` (today the parameter is
  accepted and nothing checks it). The kernel layer checks it before reserving (8.1), so the
  driver's own refusal is a second line, never what releases a key.
- **Execution.** The host calls `WorkerBoot.run` for each message; the kernel runs an async body on
  one persistent loop per worker process (fresh after fork, refused inside a running loop; G-TASK-04,
  C-WORKER-04) and a sync body directly,
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
`PermissionDenied`. `WorkerHost.run_worker`: `InvalidArgument` for a configuration the host cannot
run (above), before it consumes. `enqueue` (kernel layer) keeps its HTTP errors: `TaskEnqueueConflict` (409),
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

**Kit (`TaskQueueConformance`).** From `idk-task-api`: `send` carries `queue`; `delay` group and its
absence (`Unsupported`); the error classes of `send`; `send` of an unknown task raises
`InvalidArgument`, a group optional for a driver that cannot reject at send (a broker such as Celery
accepts any task name; the kernel rejects an undeclared task before it reserves a key, so nothing is
published and no key is left). From
`idk-worker-port`: `send` then the host runs the task once with the payload (a sync and an async
task); an unknown task runs nothing; a delayed task does not run before its delay; the host's loop is
one per process; a scheduler with a cron `* * * * *` sends once per slot while holding the lease, a
second scheduler on the same lease sends nothing; `check` reports a live worker. Kernel-layer tests
(not the kit): an enqueue refused for `delay` or an unknown task leaves no dedupe key. Broken-driver
tests: a `send` that raises `Unavailable` after a refused serialisation, a scheduler that ignores the
lease.
**Built-ins.** `celery` (sandloom-celery). A `memory` taskqueue is deferred, not rejected (owner
answer 7 of infra-deps): none this wave.

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
    # major 2: each driver class states contract_version = 2, the ABC does not (13.37)
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
`InvalidArgument`, `Unsupported("presigned_get")`. Key validation comes first: an invalid key
raises `InvalidArgument` before the feature check, so `url(key)` with an invalid key raises
`InvalidArgument` on a driver without `presigned_get` too (the kit's `test_object_invalid_keys`). A
message names the error type or code, never the backend's text, a URL or a host path (the
`local` driver maps `OSError` to `PermissionDenied` or `Unavailable`).

**Features.** `metadata` (behaviour, informational per 13.42: content type and metadata are stored
and read back), `presigned_get` (behaviour). **Performance hints.** none.
**Kit (`ObjectStoreConformance`).** Moves to the harness (`make_provider` instead of a `store`
Protocol fixture, so it checks version, features, health and close); adds `NotFound`, the size
errors, `url` with and without `presigned_get` (the test driver that returns `memory://` without
the feature must fail), and `metadata` round trip.
**Built-ins.** `local` (kernel), `s3` (sandloom-s3).

## 10. `notification` (module-facing, public)

Unchanged shape: `Notifier.send(notification: Notification) -> None` and `NotifierProvider`
(`contracts.py:1280-1326`); `contract_version` becomes `2` (an int; it is the string `"v1"` today).
The major moves because the error obligations change as `objectstore`'s do: a driver written to
"raises on a delivery failure" may raise anything, and a driver of major 1 is refused at load
(B022) instead of escaping the error list below.

**Semantics.** `send` returns after the backend accepted the message for delivery; it is not a
delivery receipt. `dedupe_key` is advisory: a driver may suppress a duplicate, nothing may rely on
it (no feature). `body` and `body_fields` are never logged; `data` may be.

**Errors.** `InvalidArgument` (recipient not valid for the channel), `Unsupported(channel)` (a
channel the driver does not declare), `TooLarge`, `PermissionDenied`, `Unavailable` (retryable:
the caller may retry), `Timeout`. "Raises on a delivery failure" (:1326) becomes this list.

**Features.** `email`, `sms`, `push`, `inapp` (behaviour each). **Performance hints.** none.
**Kit.** `NotifierConformance` gains the error group (a driver built to fail raises an
`InfraError`), `Unsupported` for an undeclared channel, and `dedupe_key` accepted.
**Built-ins.** `log` (in production only under the accepted risk `log_notifier_in_prod`), `smtp`: tier `beta`, earned by a CI lane that runs
`NotifierConformance` against a real SMTP server container (mailpit as a service container; the
kit reads the accepted message back through mailpit's HTTP API). An in-process SMTP server does not
count. `idk-foundation` adds the lane with the kit move; until the lane is green the registration
says `experimental`.

## 11. `database` provider (driver door; `idk-db-provider`)

**Replaces** `DatabaseProvider` (`contracts.py:613-693`). The module door is unchanged
(`AsyncSession` and the `sandloom.sdk.data` forms); renderers keep contract 2 (ADR-0011 amendment of
2026-11-05).

```python
class DatabaseProvider(InfraProvider):
    capability: ClassVar[str] = "database"
    # major 2: each driver class states contract_version = 2, the ABC does not (13.37) — from idk-db-provider
    def uow_factory(self) -> Callable[[], UnitOfWorkContract]: ...
    def outbox(self) -> OutboxStore | None: ...         # transactional_outbox
    def audit(self) -> AuditStore | None: ...           # audit
    def command_log(self) -> CommandLogStore | None: ...   # command_idempotency
    def request_log(self) -> RequestLogStore | None: ...   # http_idempotency
    def has_pending_changes(self, session: UnitSession) -> bool: ...
    @property
    def session_factory(self) -> Callable[[], AsyncSession] | None: ...   # default None; get_db's source
    def planner_adapter(self, *, contract: bool = False) -> ExecutionAdapter | None: ...   # default None
    async def ensure_model_schema(self, metadata: MetaData | None = None, *, recreate: bool) -> bool: ...
```

- `execution_adapter()` (:687) becomes the declared `planner_adapter(*, contract=False)`
  (`ExecutionAdapter`, moved to `ports/database.py`, with the four stores and `OutboxHandle`; no
  runtime probe in the planner); `session: Any` becomes a kernel Protocol of a unit's session
  (`UnitSession`) and `metadata: Any` SQLAlchemy `MetaData` (the driver door may name SQLAlchemy
  types; the kernel's relational surface already does).
- `session_factory` is the declared raw-session factory of an instance; `get_db` (signature
  unchanged) takes the `default` instance's of the active core, so an `INFRA`-overridden URL wins.
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
| G-RPC-08 | atomic_store (`security`) | `add` with TTL (nonce) | `shared` | "security cache" to "security store" |
| G-RPC-07, -09 | none (per-process replay guard; settings validation) | | | no |
| G-PROFILE-01, -06, -07, -08 (instance attestation) | atomic_store (`security`, `idempotency`), cache (`default`) | lifecycle `isolation_identity()` | `never_evicts`, `shared` | yes (owner decision 8): "declares `never_evicts` and differs from the module cache by `isolation_identity()`"; Redis keeps the `run_id` comparison and gains the `noeviction` check; an absent `INFRA.atomic_store.security` is refused; a `None` or raising identity fails; `never_evicts_check=assume` needs the accepted risk `unverified_noeviction` |
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
7. **`isolation_identity()` is on the lifecycle port** of every provider, not on `atomic_store`
   alone: attestation compares the security store with the evicting `cache`. The identity starts
   with `<driver>:`; the harness requires every driver of every capability to return equal,
   non-`None` identities for two providers built from the same options; `None` or a raising call
   fails attestation.
8. **`development_only` on the provider class** marks the `memory` drivers, refused in
   production with no accepted risk, separate from `tier`. The `log` notifier is not one: in
   production it advertises channels only under the accepted risk `log_notifier_in_prod`.
9. **Contract majors.** `cache`, `objectstore`, `notification`, `taskqueue`, `eventbus` go to 2 in
   this wave (`notification` because its error obligations change like `objectstore`'s); new
   capabilities start at 1. `WorkerHost` is not a capability and has no major (item 28).
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
16. **Retention becomes `streams` driver options**, reported by `retention()` on the transport.
    `EVENT_STREAM_RETENTION_SECONDS` and `EVENT_STREAM_MAXLEN` are removed in one step (design
    section 5, the same rule as the flat connection settings), and their readers
    (`guarantee_windows.py:93`, `projections/operations.py:139`, `streams_transport_base.py:107-112`)
    move to `retention()` or the options.
17. **Task API shape.** Sync **and** async task functions (Django 6, accepted by the owner; no
    refusal of sync functions), one pydantic payload or none; names derived as today,
    `f"{fn.__module__}.{fn.__name__}"` (no collision seen: tasks are module-level), no `name=`;
    `delay: timedelta`; `at_least_once=` instead of `acks_late`/`reject_on_worker_lost`;
    `@periodic(cron=, id=)` is cron only, UTC, makes the function a task by itself, and replaces both
    the beat hook and the Celery `cron` helper. `PeriodicTask.max_interval` lets billing's heartbeat
    derive its interval from the declaration.
18. **Taskqueue driver op is `send(task, payload: bytes, task_id=)`** under a kernel layer;
    `idempotency_key` stops being a driver feature; new feature `redelivery`; `InvalidArgument` and
    `TooLarge` mean "not published", `Unavailable` and `Timeout` mean "maybe published".
19. **`notification.dedupe_key` stays advisory** (no feature).
20. **`smtp` reaches `beta`** through a CI lane running its kit against a mailpit container; an
    in-process SMTP server does not count, and the registration says `experimental` until the lane
    is green.
21. **Health redaction**: `details` carries an error type name, never `str(exc)`; the registry's
    own failure record (`registry.py:1117-1118`) follows.
22. **The Redis `noeviction` check has an operator override with a bar.** Managed Redis services
    often refuse `CONFIG GET`; the option `never_evicts_check` is `verify` by default and `assume`
    lets the operator vouch for the policy. `assume` only skips a refused read (a readable policy
    other than `noeviction` is still refused), `health` keeps trying the read, and in production
    `assume` needs the accepted risk `unverified_noeviction`. The `lease` driver on Redis shares the
    check and the option.
23. **`tier` on `DriverRegistration`** (owner decision 7). Built-ins pass it to `register_driver`;
    for an entry-point driver the registry copies the class attribute `tier` (default
    `experimental`) into the registration at load. Nothing reads the class attribute after that.
24. **`test_isolation(options, worker_id) -> options`**, not design.md's `test_isolation(worker_id)`:
    the per-worker options are derived from the configured ones (another database index of the URL).
25. **Expired is absent in `atomic_store`.** Every operation treats an expired, unpurged key as
    absent; the database driver writes each statement with `expires_at <= now()` conditions like
    the lease, and ranks subject states with `decimal.Decimal`, never float or text (design.md's
    `GREATEST` is not used). The lease's renew and release also require `expires_at > now()`.
26. **`None` only for a key known absent** (2.1): an unreadable present value raises.
27. **`broadcast` never drops silently on a live subscription**: every message or `Unavailable`.
28. **`WorkerHost` is not a capability.** It is reached through `TaskQueueProvider.worker_host()`,
    which `idk-worker-port` adds together with `WorkerBoot` and `WorkerHost`; `idk-task-api` writes
    the rest of `ports/taskqueue.py`. `WorkerBoot` is synchronous, so a sync task body runs outside
    any loop and `run_async` keeps working in it.
29. **Enqueue checks before it reserves.** Payload, task name and the `delay` feature are checked
    before the dedupe key is reserved, since a pending key has no TTL.
30. **Registry files are written once, by `idk-foundation`** (section 1): every `CapabilitySpec`
    constant lives in its `ports/<capability>.py`; `bootstrap.py` pre-registers every built-in row of
    the wave as a lazy `"module:Class"` target; `ports/__init__.py` and `driverkit/testing.py` are
    lazy tables. Capability slices edit only their own files.
31. **Role-named instances replace the instance settings** (was O1; closed by owner question 6 and
    design section 2 "Instances"): `INFRA.atomic_store.security`, `INFRA.atomic_store.idempotency`
    (absent: the `security` instance) and `INFRA.lease.beat`; `SECURITY_CACHE_INSTANCE`,
    `IDEMPOTENCY_CACHE_INSTANCE` and `BEAT_LEASE_CACHE_INSTANCE` are removed and listed in
    `config/tiers.py` `REMOVED`. In production an absent `INFRA.atomic_store.security` is a startup
    refusal in `config_problems`; it is never built from the cache's options or from `memory`.
32. **The `eventbus` feature vocabulary** (section 7): `publish`, `subscribe` and `volatile` are
    removed, `at_least_once` and `consumer_groups` fold into `durable`, `durable` and
    `wildcard_subscribe` keep their names; one capability has a transport shape (the only one a
    package implements) and a kernel-internal volatile-bus shape.
33. **Kernel tables of the database drivers** live in the core migration chain: `infra_lease`
    (`database_lease.py`) and `infra_atomic_store` (`database_atomic_store.py`).
34. **`CapabilitySpec.major`** (foundation deviation 1): the spec carries the driver contract's
    integer major next to the module-facing `version`, default the major of `version` read in every
    form `versions_compatible` accepts (`v2`, `V2`, `2`, `2.0`, `v2.1.0`). It moves on its own when
    driver obligations change and the module-facing shape does not (`objectstore` and
    `notification` are 2 at `version` `v1`). A spec with no readable major loads no driver (B022).
35. **Contracts live in `ports/`, `contracts.py` re-exports them** (deviation 2) until each slice
    removes its re-export; the `database` spec and its provider ABC have their own
    `ports/database.py` (the stores stay in `contracts.py` until `idk-db-provider`), so
    `capabilities.py` only lists specs and `idk-db-provider` never edits it.
36. **`CapabilitySpec` is defined in `ports/_base.py`** (deviation 3) beside `InfraProvider`, so a
    ports file imports no registry module; `capabilities.py` and `driverkit` re-export it.
37. **`contract_version` is stated per driver class, not on the ABC** (deviation 4): an ABC that
    stated it would let a driver inherit a major it was not written for. The snippets in sections
    3-11 show the major in a comment.
38. **`InfraRegistryError`** (deviation 5) is the base of the registry's configuration errors
    (selecting, loading, opening a driver), kept apart from `InfraError`, the errors an operation
    raises.
39. **`CapabilitySpec.kernel_only`** marks `atomic_store`, `lease` and `broadcast`. The kernel
    reaches them through `InfraRegistry.provider`; `resolve` (behind `CapabilityRef`,
    `Capability(...)`, `get_core().capability`) and a module's `CapabilityRequirement`, required
    or optional, are refused with SANDLOOM-B041. `eventbus` is kernel-facing but not marked: the
    kernel derives an `eventbus` requirement for modules that publish, and the facade's
    `eventbus()` resolves it; `idk-eventbus-contract` decided: it stays not `kernel_only`, because
    both would be refused with SANDLOOM-B041 (recorded in the `ports/eventbus.py` docstring).
40. **`CapabilitySpec.removed_features`** maps a previous major's feature name to its replacement;
    every unknown-feature refusal (driver load, requirement, resolve) names the replacement. A
    removed name is never a feature of the spec. The eventbus and taskqueue slices fill their
    tables in their own ports files (item 32).
41. **`SqlDialectConformance` is a renderer kit with no lifecycle**; it takes `renderer` and
    `dialect` and runs only the version and feature checks of 2.7.
42. **`metadata` is an informational objectstore feature**: a driver without it accepts and drops
    `content_type`/`metadata`; a module checks `features()` before relying on them. The kit's
    `test_object_metadata` checks the round trip on a driver that declares it and that `put(...,
    metadata=...)` succeeds on one that does not.
43. **The smtp mailpit kit lane and smtp `isolation_identity`** (`smtp:<host>:<port>`) belong to
    `idk-tiers`.
44. **The `sqlalchemy` `isolation_identity` is `sqlalchemy:<sha256, 16 hex>`**, not 2.4's
    `database:<system identifier>/<db>`: the hash keeps credentials out of the string. Its material
    is the backend, host, the `host` query parameter, port and database; the absolute path of a
    SQLite file; a per-instance marker for `:memory:`.
45. **The rows of a moved driver stayed until `idk-config-defaults`, which deleted them** (decided
    on PR #220, `idk-pkg-redis`; deviates from section 1, which had slices 17 and 18 delete them).
    Until then a row stayed in `BUILTIN_DRIVER_ROWS` as a lazy install-hint row, because the kernel
    still synthesized the redis `atomic_store`, `lease` and `broadcast` defaults. Slice 19 removed
    the rows `cache.redis`, `taskqueue.celery`, `eventbus.streams`, `atomic_store.redis`,
    `lease.redis` and `broadcast.redis` with those defaults, and changed three things:
    - **Defaults come from the composition.** `sandloom.core.infra.config` names no driver other
      than the in-tree `memory`/`local`/`log` selectors of the flat settings. A composition passes
      `create_app(default_infra=...)` (`sandloom_standard.DEFAULT_INFRA`, and the explicit `INFRA`
      that `sandloom new` writes); `INFRA` overrides it per instance. A tool process
      (`sandloom worker check`) reads the same selection from the root's package
      (`composition.root_default_infra`). The suite's root `conftest.py` selects the standard
      drivers and `.env.test` sets `EVENT_BUS_BACKEND=memory`.
    - **Startup refuses what nothing selects.** The kernel's instances (`database`, `cache`,
      `eventbus`, and `atomic_store.security`; `REQUIRED_AT_STARTUP`) with no selected driver, or
      a driver whose package is missing, refuse startup with SANDLOOM-B037 in every environment,
      one message naming every instance, what to select and the install command. `memory` is
      selected only by name (`INFRA`, or `EVENT_BUS_BACKEND=memory`), never as a fallback. In
      production `atomic_store.security` is never taken from a composition default: `INFRA` names
      it, as `production_profile` already required.
    - **The install hint is a table, not a row.** `KNOWN_DRIVER_PACKAGES` in
      `sandloom.core.drivers.bootstrap` maps `(capability, driver)` to the package's import name and
      its install command (`pip install sandloom-redis`, `pip install sandloom-celery`) and is
      registered with `InfraRegistry.register_driver_package`. An instance that selects such a
      driver while the package is absent (no entry point registered, or `find_spec` finds nothing)
      is refused with the hint; any other unregistered name stays `DriverNotRegisteredError`. A test
      keeps the table equal to the packages' entry points.
    - **The policy hook.** `InfraRegistry.set_policy(capability, **hooks)` passes each hook as the
      constructor argument of that name to a provider class that declares it, whether the class
      comes from an entry point or a row (a class without it is built by `from_config`).
      `register_kernel_policies` sets `assume_allowed=unverified_noeviction_allowed` for
      `atomic_store` and `lease`, so `never_evicts_check=assume` still works in development and
      under the `unverified_noeviction` accepted risk in production.
46. **`sandloom-celery` keeps the `taskqueue` row of item 45 and names its modules
    `taskqueue`, `worker` and `broker_probe`** (`idk-pkg-celery-move`). The three files move whole
    to `sandloom_celery/{taskqueue,worker,broker_probe}.py` (section 1 names no file), so the
    stable importable Celery app of 8.2 is the module attribute `app` of `sandloom_celery.worker`:
    `celery -A sandloom_celery.worker inspect|call|status` (earlier text said `sandloom_celery.app`).
    The provider declares `tier = "beta"` (2.5) on the strength of `TaskQueueConformance` runs
    against a real broker. `idk-pkg-celery-lane` moves the driver's tests into
    `packages/sandloom-celery/tests` (slots, worker pool, broker probe, defaults, the kit's send and
    host groups) and extends the CI job `celery-drivers`: it builds the wheel, lints, runs strict
    `mypy packages/sandloom-celery/src`, then the package tests and the kernel's driver-row tests
    against the built wheel and a Redis broker (the root mypy run and pytest paths do not reach
    `packages/`). `SANDLOOM_CELERY_REQUIRED=1` (the scheme `SANDLOOM_<PACKAGE>_REQUIRED` of
    `SANDLOOM_REDIS_REQUIRED`), set by the job, makes an unreachable broker or cache an error, as
    it does a broker whose scheme is not a network transport (`memory://`) and a run whose
    `sandloom_celery` is not under `site-packages` (the lane tests the built wheel); without it
    the real-broker tests skip with their reason. The job writes a skip ledger (lane `celery`)
    that the `skip-ledger` job merges. A shared driverkit helper for the `_REQUIRED` guards is
    the follow-up task `idk-driverkit-required-guard`. The kernel keeps
    `test_celery_driver_rows` (bootstrap rows), `test_celery_error_mapping` (the kernel enqueue
    layer over the driver) and `test_beat_lease` (the kernel's lease resolution and the Celery
    holder over one fake store).

## 14. Open for the owner

None. O1 (the instance settings) is decided as item 31.
