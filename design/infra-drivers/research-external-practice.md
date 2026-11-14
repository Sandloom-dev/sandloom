# Research: how mature systems define portable infrastructure contracts

Web study, 2026-11-14, for [the design](design.md). Pages marked [F] were fetched; [M] is the
researcher's background knowledge, not re-verified.

## Systems

**Dapr** ([building blocks](https://docs.dapr.io/developing-applications/building-blocks/)) [F].
Fifteen building blocks (workflow, service invocation, pub/sub, state, bindings, actors,
secrets, configuration, distributed lock, cryptography, jobs, …), separated by purpose; new ones
start alpha (lock still is). Features are a per-component matrix
([state stores](https://docs.dapr.io/reference/components-reference/supported-state-stores/)):
CRUD, transactional, ETag, TTL, actors, workflow; actors need transactions and ETags.
[Certification](https://docs.dapr.io/operations/components/certification-lifecycle/): alpha
(implements the interface), beta (passes all conformance tests in a managed environment), stable
(plus certification tests: reconnection, all auth options, provisioning; maintainer-owned; at
least one prior release). Metadata fields are part of the contract; a breaking change is a new
`v2` component beside the old one
([developing a component](https://github.com/dapr/components-contrib/blob/master/docs/developing-component.md)).
Layered retries (Dapr's over the broker's) cause repeated delivery.

**Go CDK** ([structure](https://gocloud.dev/concepts/structure/)) [F]. A concrete portable type
wraps a small driver interface (the `database/sql` model); shared logic lives in the portable
type, which can add methods without breaking drivers. `As()` exposes native types and is
discouraged; code that uses it must degrade when the type does not match.
[`gcerrors`](https://pkg.go.dev/gocloud.dev/gcerrors): twelve codes (NotFound, AlreadyExists,
InvalidArgument, FailedPrecondition, PermissionDenied, ResourceExhausted, Unimplemented,
DeadlineExceeded, …); programs act on the code, never the message.
[`drivertest`](https://pkg.go.dev/gocloud.dev/blob/drivertest): `RunConformanceTests(t,
newHarness, asTests)`; the harness declares booleans such as `SupportsCopy` and the suite skips
groups accordingly; `AsTest` hooks verify the escape hatch per driver.

**Watermill** ([pub/sub](https://watermill.io/docs/pub-sub/)) [F]. Publisher and Subscriber
with `Close()`; one message at a time, Ack requests the next, Nack redelivers; offsets commit
only after Ack; at-least-once, consumers must be idempotent.

**Kombu / Celery** [M]. Transports differ in capability (`implements` flags). At-least-once with
late acks; Redis visibility-timeout redelivery is a known gotcha; ETA is worker-side. Celery
beat is one scheduler with no lease: two beats run jobs twice — the argument for a lease.

**Rails** [F for ActiveJob, ActionCable]. ActiveJob: one interface, adapter by configuration;
Solid Queue (database) default since Rails 8; features vary per adapter and are documented;
`enqueue_after_transaction_commit`. ActionCable: async (dev), Solid Cable, Redis, PostgreSQL
(NOTIFY payload limit 8000 bytes); broadcast is "purely an online queue". Solid Cache, Solid
Cable, Solid Queue: no extra service by default [M].

**Laravel** ([cache](https://laravel.com/docs/12.x/cache)) [F]. Default cache driver is the
database. Atomic locks are part of the cache (`LockProvider`): `lock(name, ttl)`, `get`,
`block`, `release`, an owner token passable to a job; `funnel` raises when the store has no
locks. `add` is atomic set-if-absent; tags unsupported on some stores and fail at runtime.
Custom driver = implement `Store`, call `Cache::extend`.

**Django** ([DEP 14](https://github.com/django/deps/blob/main/final/0014-background-workers.rst))
[F]. `BaseTaskBackend` (`validate_task`, `enqueue`, `get_result`, `close`); feature flags
`supports_defer`, `supports_coroutine_tasks`; unsupported options raise `InvalidTaskError`; no
delivery guarantee defined. Backends by `BACKEND`/`OPTIONS`; external packages ship other
database backends [M].

**OpenDAL** ([Capability](https://opendal.apache.org/docs/rust/opendal/struct.Capability.html))
[F]. About 90 fields, mostly booleans plus typed limits; systematic names (`presign_read`,
`write_can_empty`); some flags describe a fallback path.

**Spring / Micronaut** [M]. Spring Cache: minimal contract, TTL per provider; Spring
Integration `LockRegistry` with Redis, JDBC and ZooKeeper implementations. Micronaut: drivers as
separate modules chosen by classpath and configuration.

## Rate limits and locks

- Atomic increment with TTL: Redis INCR+EXPIRE (Lua), SQL `INSERT … ON CONFLICT DO UPDATE` on a
  window key; Memcached lacks atomic create-with-TTL. Treat it as a declared feature or a
  durable-store operation.
- [Kleppmann](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) [F]:
  a lock is a lease; a paused holder can outlive it; Redlock gives no fencing token. Use a lease
  for efficiency; for correctness use a fenced write (row version, epoch). A database row
  (`holder`, `expires_at`, `fence` incremented on acquire) gives a free fencing token; advisory
  locks are session-bound and break under transaction pooling.

## Cross-cutting patterns

- Granularity: a feature when the call shape is the same with optional behaviour; a separate
  capability when semantics, failure modes or consistency differ (lease vs cache, bus vs queue,
  durable bus vs online broadcast).
- Delivery semantics are declared (Watermill), not left unstated (Rails, Django).
- A shared error taxonomy; unsupported options raise, never no-op.
- Escape hatches are allowed, discouraged, tested.
- Health includes recovery without restart (Dapr reconnection scenario).
- Out-of-tree drivers: SQLAlchemy external dialects, OpenTelemetry exporters, Django's external
  database backends; Dapr keeps everything in one contrib repository with a certification ladder.

## Recommendations taken into the design

- Capabilities by semantics: cache (evicting), a durable atomic store or counter, lease with a
  fence, durable event bus with groups and ack, online broadcast, task queue with `defer` and
  `periodic`, object store, notifier, secrets.
- Atomic counter: either a cache feature or part of a durable store; the scheduler lease is
  kernel code over `lease`, which avoids the beat duplicate-run problem.
- Database-backed defaults (Solid style) for the small coordination capabilities, with their
  limits declared as features.
- Error model after `gcerrors`, with a `retryable` flag; misses are results; the kit asserts the
  mapping.
- Kits with a harness and feature-gated groups; tiers experimental, beta (kit passes in CI
  against the real service), stable (plus resilience: reconnect, auth, redelivery).
- Escape hatch only through an explicit accessor that modules and the kernel may not call.
- Versioning: within a major only additions; semantics, error mapping or a required method
  change is a new major; refuse an unsupported major with a code and a fix.
- Core ships memory, database-backed and filesystem drivers; Redis, S3, SQS and other service
  drivers are separate distributions found by entry point.

## Gaps

Dapr's distributed-lock page returned 404; Kombu/Celery, Spring, Micronaut, ActiveStorage, Solid
Cache, OpenTelemetry exporters and Django's cache/email/storage pages were not fetched ([M]).
