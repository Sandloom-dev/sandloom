# Review A: infra-drivers wave (atomic_store, lease, broadcast, eventbus contract, event ops)

> **Status.** Fixed in the task `idk-review-a-fixes`: M1 (the Redis atomic_store
> judges `maxmemory-policy` on the first operation of a process that never ran
> `aopen`), M2 (every production profile requires `shared` and `never_evicts` of
> the `security` and `idempotency` stores, read from the driver's declaration),
> L1 (a failed `health` probe or a lost connection opens the eviction check again,
> for the lease and the Redis atomic_store). L2 went to LOW BATCH 5. The report
> below is the original text, unchanged.

Reviewed: main fa5390d6 (read-only; tests in a detached worktree at /tmp/review-a).
Tasks: idk-foundation (#190), idk-atomic-store (#197), idk-atomic-adopt (#206), idk-lease (#199),
idk-broadcast (#198), idk-eventbus-contract (#207), idk-event-ops (#212); the database atomic_store
driver (idk-atomic-store-db, #209) read alongside since the security duties can run on it.

Counts: H 0, M 2, L 2.

## Findings

### M1. Redis atomic_store serves without the never-evicts check in any process that never calls aopen (workers, CLI)

- `packages/sandloom-redis/src/sandloom_redis/atomic_store.py:368-376` (`_operation_client`) only repeats a refusal
  that `aopen` (`:555-594`) recorded; nothing checks `maxmemory-policy` on first use. `aopen` runs only
  through `InfraRegistry.aopen_all`, called only from the API lifespan
  (`src/sandloom/core/app_factory/lifecycle.py:139`); `src/sandloom/core/worker.py:205-209` states the
  worker never runs it. ADR-0016 section 3.4: "The registry calls `aopen` on every used instance before
  its first operation". Spec section 4: the Redis driver "on aopen and in health" refuses a non-noeviction server.
- Breaking state: a worker or CLI process (enqueue dedupe from a task, `revoke_user_tokens` / `block_user`
  called from a task, idempotency reservations) on a security/idempotency Redis with
  `maxmemory-policy allkeys-lru`. It writes revocation marks, blocks and reservations that can be
  evicted before their TTL. No refusal, no log line. In production an API process on the same instance
  would refuse at startup, so the exposure is a worker-only deployment, or a worker started while the API is down.
- Contrast: the Redis lease checks lazily on its first operation (`lease.py:277-287`, `_policy_checked`), so it is safe here.
- Verified: probe with a fake client (`client_factory`), policy `allkeys-lru`, no `aopen`:
  `await store.add("k", "v", ttl_seconds=10)` -> `True`, calls `['set']` (no CONFIG GET).
- Fix: run the policy check lazily on the first operation, as the lease does (and clear it after a
  connection failure). Or make the worker/CLI boot call `aopen` on the kernel instances it uses.

### M2. Outside multi_tenant_strict, production does not require `shared` / `never_evicts` of the security and idempotency stores

- `src/sandloom/core/app_factory/config.py:373-378` runs only `atomic_store_security_problem`
  (`src/sandloom/core/production_profile.py:121-132`). That check refuses an absent or `memory` security
  instance and nothing else. The feature check (`production_profile.py:358`) and the identity attestation
  run only in `enforce_runtime` under `PRODUCTION_PROFILE=multi_tenant_strict` (`:314-333`).
  `infra/access.py:83-98` resolves the role without a feature check.
- Spec section 4 (design/infra-drivers/spec.md:447): "The kernel requires `shared` and `never_evicts`
  of its `security` and `idempotency` instances in production." guarantees.yml G-REVOKE-03 / G-BLOCK-01
  list both features. The lease has the equivalent check (`fenced_lease.py:124`, `shared_required=settings.is_prod`
  from `worker.py:367`). The atomic_store has none.
- Breaking input: `ENVIRONMENT=prod`, default profile,
  `INFRA={"atomic_store":{"security":{"driver":"database","url":"sqlite+aiosqlite:////var/lib/app/sec.db"}}}`
  with two replicas. Each host has its own revocation marks and blocks, so a token revoked or a user
  blocked on one replica is accepted on the other. The same holds for any third-party driver that declares
  neither feature.
- Verified: probe. `atomic_store_security_problem(cfg)` -> `None`;
  `DatabaseAtomicStoreProvider.features_for({...sqlite...})` -> `frozenset({'never_evicts'})`.
- Fix: in production on every profile, refuse at startup (a configuration problem or registry open) a
  security or idempotency instance whose provider lacks `shared` or `never_evicts`. Keep the
  identity-separation check strict-only if that is intended.

### L1. A noeviction policy lost at runtime is reported by health but not enforced: the Redis lease drops a refusal its own comment says it enforces

- `packages/sandloom-redis/src/sandloom_redis/lease.py:155-158` says the refusal is "repeated by every
  operation until a check finds the policy fit". But `health` (`:320-348`) records `_refusal` while
  `_ready`/`_script` (`:277-306`) skip the check once `_policy_checked` is True, and never read `_refusal`.
  The Redis atomic_store `health` (`atomic_store.py:596-619`) records nothing. The database atomic_store
  does fail closed from health (`database_atomic_store.py:480-482`).
- Breaking state: the policy changes to `allkeys-lru` after `aopen`. Health turns `fail`, but leases are
  still granted (the fence counter can be evicted, so a fence repeats) and marks are still written.
- Verified: fake-client probe. After `aopen`, policy set to `allkeys-lru`: `health()` returned
  `fail/evicting_policy`, then `acquire(...)` returned a `Lease`.
- Fix: in the lease's `_script`, raise a recorded `_refusal` (or reset `_policy_checked` when health
  refuses). Make the Redis atomic_store's `health` record the refusal the way the database driver does.
  Otherwise, correct the comment.

### L2. The owner console's Streams ops still assume Redis Streams entry IDs

- `src/sandloom/core/admin/api/platform.py:239` (`ReplayIn.ids` pattern `^[0-9]+-[0-9]+$`) and `:495`
  (the `cursor` query uses the same pattern). The `EventTransport` port treats `DeadLetter.id` / `after`
  as opaque strings (`infra/ports/eventbus.py:77-88, 265-279`). idk-event-ops was meant to stop assuming
  Redis Streams.
- Breaking input: an external `EventTransport` that declares `dlq_ops` and uses UUID or offset ids.
  Through the console, listing past page 1 and every replay return 422. The CLI works.
- Verified: read.
- Fix: validate as an opaque bounded token (e.g. `^[A-Za-z0-9_.:-]{1,80}$`) and leave the format to the driver.

## Checked and found sound

- Port `atomic_store.py`: key, value and TTL checks shared by every driver; the subject-state domain is
  parsed with an exact `Decimal` built from a string (no context rounding); bounded at 128 characters
  before matching.
- Redis atomic_store: the Lua subject-state comparison (leading and trailing zero strip, length then
  bytewise digits, padded fraction) matches the Python order, including the 15th-digit case. A
  corrupt or over-long stored value is replaced. A losing write never touches the TTL. A block
  (`ttl=None`) clears an older TTL. `add`, `add_or_get` and CAS/CAD run on a client with 0 retries
  (redis-py 5.2.1 checked, pinned `<6`); a NOSCRIPT re-run is safe. The error text never carries
  backend text. `assume` is refused unless `assume_allowed` (prod plus `unverified_noeviction`) allows it,
  wired through `register_kernel_policies` for both atomic_store and lease.
- Memory driver and `InMemoryAtomicStore` fake: expired-as-absent, ranking via `subject_state_rank`, development_only.
- Database atomic_store: expired-as-absent in every statement (ON CONFLICT ... WHERE expires_at <= now(),
  NULL handled). `set_subject_state` under READ COMMITTED + FOR UPDATE re-reads the newest row, and the
  lost insert retries. The bounded `add_or_get` loop never answers `None`. Non-idempotent statements are
  not re-sent. An unlogged table is refused at aopen and from health.
- G-REVOKE-03: `_record_revocation_mark` reads, then calls `set_subject_state` (no set_max left); listed
  tests cover the fake and real Redis. G-BLOCK-01: block via `set_subject_state` with no TTL; unblock
  by a bounded CAS loop that never deletes. G-IDEM-01..05/08: reserve via one `add_or_get`, renew and
  finalise via CAS, release via CAD. Every store error becomes `_CacheUnavailable`, so `fail_closed`
  decides. Values stay under 1 MiB (replay body capped at 256 KiB; key at 200 chars).
- Fail-open/closed: the per-request read and the issuer check honour `revocation_fail_closed` (default
  True in prod). A failed block write is a 503. A failed unblock leaves the user blocked. The RPC
  nonce fails closed in prod. Enqueue dedupe answers 503/Uncertain and never re-sends on doubt.
- Production refusal of an absent or memory `INFRA.atomic_store.security`. The REMOVED entries for the
  three `*_CACHE_INSTANCE` settings. No `security_cache()` / `idempotency_cache()` left.
- Lease: Redis scripts (EXISTS/INCR/HSET/EXPIRE; token-checked renew and release; the fence key has no
  TTL). The database upsert keeps the fence across release and expiry; new names start at max+1;
  renew and release are token- and expiry-checked. `FencedLease` sets the local deadline from before
  the call, bounds each call at ttl/3 and stops on `False`. The beat refuses a non-shared lease in prod.
- Broadcast: the Redis subscription confirms SUBSCRIBE before returning, never reconnects (0 retries,
  fail on error), uses its own ping deadline for half-open sockets, and dedupes pmessage/message. The
  prefix is glob-escaped. The memory driver's overflow raises `Unavailable`. Both the shadow and the
  volatile bus treat `Unavailable` as a gap (flush and resubscribe).
- Eventbus contract: the rename table for removed features. `durable_transport` / `transport_for` raise
  instead of skipping silently. The memory transport's group-ownership semantics match the port.
  event_ops maps `Unsupported` and a missing transport to 501 in the console.

## Tests run (worktree at fa5390d6)

- Serial run (`-n 0`), own DB `sandloom_test_reviewa`, Redis 223/224, owner `reviewa`: test_lease.py, test_atomic_store_kit.py, test_atomic_adopt.py -> 176 passed (Postgres and real-Redis tests included). An earlier run over the wider in-scope set gave 403 passed; its 30 errors were all the Postgres credential error (test_user:test_password from .env.test), and the Postgres tests then passed in the rerun above.
- Probes (fake client, outside the repo) for M1, M2 and L1 as described above.
