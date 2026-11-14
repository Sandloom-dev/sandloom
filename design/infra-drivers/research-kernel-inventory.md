# Research: where the kernel touches infrastructure directly

Read-only study at `aefd3e52`, 2026-11-14, for [the design](design.md). Scope:
`src/sandloom/{core,sdk,shared,driverkit}`, `packages/sandloom/pyproject.toml`,
`tests/architecture`, `.importlinter`. Paths are relative to `src/sandloom/`; line counts are
approximate (`wc -l`). One behaviour was run: importing `sandloom.core.testing.session_checks`
with `redis` blocked raises ImportError.

## 1. Backend use outside `core/drivers/`

### Redis protocol with no `redis` import (the largest leak)

The event bus implementations live in `core/eventing/`, not in drivers;
`core/drivers/event_bus_driver.py` only wraps them. They call the redis-py client API through
an injected client factory, so import-based checks never see them.

| site | Redis feature | kernel duty | capability that could serve it |
|---|---|---|---|
| `core/eventing/pubsub_bus.py:129,131,217,280` | `psubscribe`, `subscribe`, `pubsub()` | volatile `redis` event bus | `broadcast` |
| `core/eventing/streams_transport.py:106-143,157` | `pipeline(transaction=True)`, `xadd(maxlen~)`, `xtrim(minid)` | durable publish | `eventbus` |
| `core/eventing/streams_consume.py:69,80,239,279,491,611,620,625` | `xautoclaim`, `xack`, `xadd` (DLQ), `xreadgroup`, `xpending`, `xlen` | consume loop, DLQ, redelivery | `eventbus` |
| `core/eventing/streams_groups.py:220,252,277,359` | `xlen`, `xpending`, `SET NX`, Lua CAS | group owner fingerprint | `lease` |
| `core/eventing/streams_inventory.py`, `streams_lease.py` | consumer inventory, in-flight batch lease | subscription inventory | `eventbus` |
| `core/contract_cache_shadow.py:845-848,886-887` | `cache.raw_client()`, `publish`, `pubsub.subscribe` | contract-cache invalidation (opt-in `CONTRACT_CACHE_SHADOW`) | `broadcast` |
| `core/tasks/event_ops.py:64-101`, `core/cli/events.py:68-72`, `core/admin/api/platform.py:46-47,500,546` | duck-typed `transport.replay_dlq`, Streams group and consumer listings | `sandloom streams` CLI, console repair | `eventbus` ops operations |

### Direct SDK imports

| site | SDK | kernel duty |
|---|---|---|
| `core/worker.py:251`, `:106-121` | celery | worker entry point, healthcheck; Celery-only |
| `core/beat_lease.py:442` | `celery.signals` | beat singleton lease (lease itself on cache CAS) |
| `core/async_runner.py:181`, `core/gc_tuning.py:256` | `celery.signals` | worker loop reset, GC freeze |
| `sdk/_prim/tasks.py:71,186` | `celery.shared_task`, `crontab` | the public task API |
| `core/migrations/backup.py`, `core/cli/extract.py`, `core/cli/dev/services.py`, 7 `core/testing/*` files | psycopg2 | PostgreSQL ops tools, test harness |
| `core/testing/session_checks.py:27`, `resources.py:47` (top level), `ownership.py:330`, `slots.py:175` | redis | test harness |
| `core/rpc_transport.py`, `rpc_transport_compat.py`, `rpc/proxy.py` | httpx, aiohttp | service-to-service RPC wire (no capability; out of scope) |
| `core/observability/tracing.py:44-59` | OpenTelemetry exporters | tracing; **no `otel` extra** |
| 15 files, e.g. `core/observability/metrics.py:16` | prometheus_client | metrics; base dependency |
| `core/observability/unreachable.py:33,91-140` | asyncpg/psycopg2/redis/kombu exception types by name | startup "cannot reach" message |

### Other findings

- Filesystem as storage: schema registry atomic write (`core/schema/registry.py:148-150`),
  contract-cache report directory.
- Not found in kernel code: smtplib outside the smtp driver, boto, LISTEN/NOTIFY,
  advisory locks outside renderers, websocket rooms.
- `core/_internal/db_provider.py:17-26` imports driver internals.
- `core/infra/config.py:141,157,181,193` hard-codes `sqlalchemy`, `redis`, `celery` as defaults
  and synthesises `smtp_*` and `s3_*` options from flat Settings.
- `core/drivers/bootstrap.py:10-11,206-219` imports `redis_cache` and `sqlalchemy_database`.

### Kernel duty → what it uses today

| duty | uses |
|---|---|
| revocation, blocks | `cache.set_subject_state` (`auth/revocation.py:230`, `auth/blocks.py:59`) |
| idempotency, security caches | `cache.add`, CAS (`idempotency/cache_store.py:55-96`, `rpc/inbound_auth.py:332`, `tasks/enqueue.py:84-120`) |
| rate limit | `cache.incr_window`, fallback `incr`+`get` (`ingress/rate_limit.py:122-128`) |
| beat lease | `cache.incr`, `add`, `compare_and_set`, `compare_and_delete` (`beat_lease.py:151-251`), refused without `cas` (`:336`) |
| consumer inbox | `cache.add` (`eventing/consumer_inbox.py:45`) |
| outbox relay, saga sweep, request log | `DatabaseProvider` + SQL (database work, done) |
| locks | the `lock` capability is not used; each duty builds its own on cache CAS |

### Database leaks outside `core/drivers/` (brief)

psycopg2 in 10 non-test files (~28 sites); dialect allowlist 21 sites in 10 files
(`tests/architecture/kernel_dialect_allowlist.json`); `pg_dump`/`pg_restore` in backup;
asyncpg URL rewriting in `migrations/urls.py`; `asyncpg` and `psycopg2-binary` are base
dependencies.

## 2. Built-in drivers in the kernel distribution

All registered in `core/drivers/bootstrap.py:83-157`.

| capability / driver | backend | extra | lines |
|---|---|---|---|
| database / `sqlalchemy` | SQLAlchemy, asyncpg, aiosqlite | base deps; `sqlite` extra | ~3,950 across driver, dialects, DBAPI adapters, pool, schema reset |
| cache / `redis` | redis-py, Lua | `redis` | 836 |
| eventbus / `memory`, `redis`, `streams` | redis-py | `redis` | driver 241 + ~3,500 in `core/eventing` |
| taskqueue / `celery` | Celery, kombu | `celery` | driver 254, probe 111, plus worker 430, beat lease 483, async runner 195, GC tuning 279, SDK tasks 203 |
| objectstore / `local` | filesystem | none | ~310 |
| notification / `log`, `smtp` | stdlib | none | ~265 |
| document_store / `memory` | none | none | 123 |

The `sandloom.sql_dialects` entry points `postgresql` and `sqlite` point into the kernel wheel.

**Why `sandloom-s3` is external:** its own wheel and dependencies; one entry point
`"objectstore.s3" = "sandloom_s3:S3ObjectStore"`; subclasses the public
`sandloom.driverkit.ObjectStoreProvider`; declares `contract_version`, features and
`options_schema`; loaded by name only when `INFRA` selects it (`core/infra/registry.py:1346-1415`).

**What a built-in needs to move out the same way:** a provider class with `from_config`
instead of a factory and a `register_driver` line; a public provider in `sandloom.driverkit`
(today missing for `CacheProvider`, `TaskQueueProvider`, `EventBusProvider`,
`DatabaseProvider`); a kit; a wheel with an entry point; removal of the hard-coded default
names. Redis is shared by three drivers (`event_bus_driver.py:18,65,114` imports
`redis_cache.build_transport_client`), so cache and event bus move together, and the event bus
code sits in `eventing`, a layer below `drivers`.

## 3. Backend semantics the kernel relies on

| duty | semantic | guarantee | a second driver must provide |
|---|---|---|---|
| revocation, blocks | atomic `set_subject_state` / `set_max` (Lua `redis_cache.py:564,648`) | G-REVOKE-03, G-BLOCK-01 | atomic monotonic max, or CAS plus non-eviction |
| rate limit | one-round-trip incr with TTL at creation (Lua `:286-311`) | G-HTTP-01, G-HTTP-02 | atomic incr-with-TTL |
| idempotency, enqueue dedupe, RPC replay | `add` (SET NX), CAS/CAD (Lua `:693,705`) | G-IDEM-01..03, G-TASK-01..03, G-INBOX-03 | atomic add, CAS, CAD, durability |
| beat lease | fenced lease via incr, add, CAS; Celery beat the only `periodic` driver | G-OPS-01, G-INFRA-11, C-WORKER-06 | fenced lease plus a scheduler that honours it |
| durable bus | consumer groups, PEL redelivery (`XAUTOCLAIM`), DLQ before ack, trim, group-owner `SET NX`, `MULTI/EXEC` batch publish | G-EVENT-10, G-EVENT-03..07, G-WINDOW-03 | at-least-once, groups, dead-consumer claim, DLQ-then-ack, batch publish |
| volatile bus | pub/sub, pattern subscribe | G-EVENT-14 | fan-out to live subscribers |
| contract-cache shadow | pub/sub through `raw_client` | ADR-0003 (opt-in) | no portable surface exists |
| task enqueue | Celery `send_task`, kombu serializer errors | G-TASK-01..05, C-WORKER-01..06 | idempotent enqueue, `delay`, `periodic` |

`InMemoryCache` (`core/testing/_uow.py:272`) ignores TTLs and is not a registered driver; no
second cache driver is registered anywhere.

## 4. Private and undefined capability specs

`core/infra/extra_capabilities.py:181-249`: `document_store` (memory driver, used as a
projection target), `graph_store`, `workflow`, `search`, `lock`, `secrets` — all experimental,
no driver and no kernel use except `document_store`. `LockProvider` has `acquire(ttl)` and
`release(token)`, no renewal and no fence.

## 5. Ratchets today

| test | checks | gap |
|---|---|---|
| `test_infra_constructed_only_in_drivers.py` | `create_async_engine`, `Celery`, `Storage`, `redis.from_url` outside drivers | misses `redis.Redis(...)`, `psycopg2.connect`, commands on an injected client |
| `test_relational_paradigm_boundary.py` | runtime SQLAlchemy imports in `core/` | allowlist |
| `test_kernel_names_no_database.py` | database names, dialects, pgcodes | 21 sites, shrink-only |
| `test_no_legacy_infra_imports.py`, `FORBIDDEN_BACKEND_ROOTS` | modules import no redis, celery, boto3, … | the kernel is not scanned |
| `test_no_raw_provider_usage.py` | modules call no `raw_client`, `raw_backend`, `raw` | the kernel is not scanned |
| `.importlinter` | kernel layers; kernel never imports `driverkit` or a business module | no contract bars `core.*` from redis, celery, psycopg2 |
| `tools/packaging/check_package_distributions.py` | lean wheel has no redis, celery; celery not imported | says nothing about asyncpg, psycopg2, prometheus |

## Ranked coupling points

1. Redis event bus code in `core/eventing` (~3,500 lines, raw redis calls, G-EVENT-*).
2. The public `shared_task` and `cron` are Celery (public API change).
3. `worker.py`, `beat_lease.py`, `async_runner.py`, `gc_tuning.py` are Celery-only (~1,400 lines).
4. `CacheProvider` is private with one driver; the kit must prove the atomic semantics the
   security guarantees use.
5. The Streams-shaped ops surface (`tasks/event_ops.py`, `cli/events.py`, admin platform API).
6. Contract-cache shadow raw pub/sub (small, opt-in).
7. The shared Redis client between cache and event bus.
8. Hard-coded default drivers and flat Settings.
9. psycopg2 and Redis in the test harness and dev services.
10. Base dependencies: asyncpg, psycopg2, prometheus; no OTel extra.

## Surprises

- `core/testing/session_checks.py:27` and `resources.py:47` import `redis` at top level; the
  pytest plugin imports `session_checks` in `pytest_sessionstart`. A test run of an app without
  the `redis` extra probably fails (import verified, full run not).
- The event bus and the kernel's `raw_client` call are invisible to every ratchet.
- Only `objectstore` and `notification` have a public contract, a kit and an external-style
  class.
