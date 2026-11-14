---
title: Atomic store
description: The store of revocation marks, user blocks, RPC nonces and idempotency reservations, with a Redis driver and a database driver that runs on PostgreSQL alone.
job: Choose and configure the atomic store of an application.
purpose: Keep security state and idempotency reservations on the backend the deployment already runs.
audience: [developer, operator]
level: building
type: concept
---

# Atomic store

The atomic store holds the small records the framework must never lose or
double-write: revocation marks, user blocks, RPC nonces, the cache-form
idempotency guard and enqueue dedupe. It is the `atomic_store` capability, read
through two instances by role, `INFRA.atomic_store.security` and
`INFRA.atomic_store.idempotency` (an absent `idempotency` instance is the
`security` instance). In production the `security` instance is required, must
declare `shared` and `never_evicts`, and must not share a failure domain with the
module cache.

Every operation is atomic. An expired key is absent for every operation, also
while the backend still holds it: `add` creates over it, `compare_and_set` and
`compare_and_delete` answer `False` on it.

[Configure backends](configure.md) shows where `INFRA` is set, [Cache](cache.md) is the
store this one is kept apart from, and the [security model](../advanced/security-model.md)
lists what production checks about it.

## Drivers

| Driver | Backend | Declares | Use it |
| --- | --- | --- | --- |
| `redis` | a Redis server with `maxmemory-policy noeviction` | `shared`, `never_evicts` | the standard choice: one round trip per operation |
| `database` | PostgreSQL (or SQLite, one host) | `shared` (PostgreSQL only), `never_evicts` | an application that runs on PostgreSQL alone and wants no Redis |
| `memory` | the process | `never_evicts` | development and tests only; production refuses it |

### `database`

An option, never the default. It keeps its keys in the table
`infra_atomic_store` (`key`, `value`, `expires_at`; `NULL` never expires) of the
core migration chain, so `sandloom migrate upgrade-all --apply` creates it on every
database that hosts a module. Select it for an instance:

```python
INFRA = {
    "atomic_store": {
        "security": {
            "driver": "database",
            "url": "postgresql+asyncpg://sandloom:<pw>@security-db:5432/sandloom",
        },
    },
}
```

Options (all optional but `url`): `pool_size` (8), `pool_timeout_seconds` (5),
`pool_recycle_seconds` (1800), `pool_pre_ping` (off), `statement_timeout_seconds` (5),
`max_attempts` (8) and `max_value_bytes` (1 MiB). An unknown option fails startup.

How it behaves:

- **The database's clock decides expiry.** Every statement compares `expires_at`
  with `now()`, never with a process clock, and treats a row with
  `expires_at <= now()` as absent. `add` is one
  `INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE expires_at <= now()`;
  `compare_and_set` and `compare_and_delete` are one `UPDATE` / `DELETE` whose
  `WHERE` holds the expected value and the liveness.
- **`set_subject_state` is exact.** It runs in one transaction: the row is read
  `FOR UPDATE` (SQLite takes its one write lock first), ranked in the driver as an
  exact `decimal.Decimal` (never a float or text), then written. Two instants that
  differ only past the 15th significant digit rank by that digit.
- **No advisory locks**, so it works behind pgbouncer in transaction pooling mode
  (no session state, no `LISTEN`, no prepared-statement names it must keep).
- **Keys without a TTL are kept** (the enqueue-dedupe pending record has none).
- **Expired rows are purged by the outbox relay's housekeeping**, in bounded
  batches every 30 seconds on each database it drains, a minute after they
  expired. Until then they take space and nothing else: no statement reads them.
- **The table is logged, never `UNLOGGED`.** An unlogged table is emptied by a crash,
  which would lose revocation marks. The driver reads the table's persistence on
  `aopen` and in `health` and refuses an unlogged one (`never_evicts` is verified
  there). The table is created with `fillfactor 70` and a small autovacuum scale
  factor, because every revocation and every `set` rewrites a row; `expires_at` is
  not indexed so those updates stay in place (HOT).
- **Identity.** `isolation_identity()` is `database:<system identifier>/<database
  name>` (`pg_control_system()`, which an unprivileged role may read; a managed
  service that blocks it makes the call fail and production attestation with it).
  The production attestation refuses a store whose identity equals the default
  cache's, so a `database` store never collides with a Redis cache; a security
  store on the application's own database goes down with it.

SQLite is for one host (a file belongs to one host), so it declares `never_evicts`
and not `shared`, and production refuses it for a store several processes use.

#### What it costs

Each operation is one SQL statement in autocommit (one round trip), except
`set_subject_state` (a transaction) and `add_or_get` when the key is held (the
insert, then a read). Measured on a laptop against PostgreSQL 16 and Redis 7 in
local containers, with the asyncpg driver and an idle pool (median of 1,500 calls):

| operation | `redis` | `database` | difference |
| --- | --- | --- | --- |
| `get` (the revocation check of an authenticated request) | 0.42 ms | 0.59 ms | +0.2 ms |
| `compare_and_set` | 0.43 ms | 0.64 ms | +0.2 ms |
| `add_or_get`, creates the key | 0.46 ms | 0.80 ms | +0.3 ms |
| `add_or_get`, key held | 0.43 ms | 1.51 ms | +1.1 ms |
| `set_subject_state` (a revocation or block) | 0.44 ms | 1.77 ms | +1.3 ms |

An authenticated request pays one `get` (+0.2 ms here; plan for +0.2 to +0.7 ms
as the database moves further away). A write costs more: a revocation or a block
about +1.3 ms, an idempotency reservation +0.3 ms when it creates the key and
+1.1 ms when the key is held. Revocations and blocks are rare. The numbers move
with your network and pool: measure on yours before you rely on them.

Choose `database` when removing Redis from the deployment is worth that price.
Keep `redis` when you already run it, or when the request path is the first thing
you tune: that is one round trip fewer per operation, and the security store then
does not share load with your application's database.

A connection pool that does no ping on checkout keeps each `get` at one round
trip. A pooled connection that died while idle fails the first send of an
idempotent operation, which the driver sends once more; `add`, `add_or_get`,
`compare_and_set` and `compare_and_delete` are never sent twice, so they fail as
`Unavailable` instead. Set `pool_pre_ping` to `true` to ping on every checkout
(three more round trips with asyncpg).
