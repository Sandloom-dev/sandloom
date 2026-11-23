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
double-write. These are revocation marks and user blocks. They are also RPC
nonces, the cache-form idempotency guard and the enqueue dedupe. It is the
`atomic_store` capability. Two instances are read by role: `INFRA.atomic_store.security`
and `INFRA.atomic_store.idempotency`. An absent `idempotency` instance is the
`security` instance. In production the `security` instance is required. On every
profile the application refuses to start unless the driver of its `security` and
`idempotency` instances declares `shared` and `never_evicts` for its options. A
`database` store on SQLite is not `shared`. It must not share a failure domain
with the module cache.

Every operation is atomic. An expired key is absent for every operation, also
while the backend still holds it. `add` creates over it, and `compare_and_set` and
`compare_and_delete` answer `False` on it.

[Configure backends](configure.md) shows where `INFRA` is set. [Cache](cache.md) is
the store this one is kept apart from. The [security model](../advanced/security-model.md)
lists what production checks about it.

## Drivers

| Driver | Backend | Declares | Use it |
| --- | --- | --- | --- |
| `redis` | a Redis server with `maxmemory-policy noeviction` | `shared`, `never_evicts` | the standard choice: one round trip per operation, plus one `CONFIG GET maxmemory-policy` on each new connection of its pool (the eviction check, before the connection's first command); its `url` is its own option (`atomic_store.security.url`), never the cache's |
| `database` | PostgreSQL (or SQLite, one host) | `shared` (PostgreSQL only), `never_evicts` | an application that runs on PostgreSQL alone and wants no Redis |
| `memory` | the process | `never_evicts` | development and tests only; production refuses it |

A `redis` store that shares `cache.default`'s host, port and database logs a startup warning
that names both instances. The cache may evict the revocation marks the store keeps.

### `database`

An option, never the default. It keeps its keys in the table
`infra_atomic_store` (`key`, `value`, `expires_at`; `NULL` never expires) of the
core migration chain.

The `url` must reach a database that has the core chain applied. Two ways get you
there. Point `url` at a database that hosts a module, where
`sandloom migrate upgrade-all --apply` creates the table. Or declare the database
as an `INFRA.database` instance and apply the core chain to it alone:

```bash
sandloom migrate upgrade-core --database security_db --apply
```

`aopen` refuses a database where the table is missing. Select the driver for an
instance, here on a database of its own:

<!-- code-ok: D4 an INFRA configuration fragment with placeholder credentials, nothing to run -->
```python
INFRA = {
    "database": {
        "security_db": {
            "driver": "sqlalchemy",
            "url": "postgresql+asyncpg://sandloom_store:<pw>@security-db:5432/sandloom",
        },
    },
    "atomic_store": {
        "security": {
            "driver": "database",
            "url": "postgresql+asyncpg://sandloom_store:<pw>@security-db:5432/sandloom",
        },
    },
}
```

Every option but `url` is optional. The pool takes `pool_size` (8) and
`pool_timeout_seconds` (5). It also takes `pool_recycle_seconds` (1800) and
`pool_pre_ping` (off). One statement may take `statement_timeout_seconds` (5).
Contention retries stop at `max_attempts` (8). A value is at most
`max_value_bytes` (1 MiB). The purge reads `purge_interval_seconds` (60) and
`purge_grace_seconds` (60). It also reads `purge_batch_size` (1000). An unknown
option fails startup.

How it behaves:

- **The database's clock decides expiry.** Every statement compares `expires_at`
  with `now()`, never with a process clock. A row with `expires_at <= now()` is
  absent. `add` is one `INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE expires_at <= now()`.
  `compare_and_set` and `compare_and_delete` are one `UPDATE` or `DELETE` whose
  `WHERE` holds the expected value and the liveness.
- **`set_subject_state` is exact.** It runs in one transaction. The row is read
  `FOR UPDATE` (SQLite takes its one write lock first), ranked in the driver as an
  exact `decimal.Decimal` and then written. A float or text never decides the rank.
  Two instants that differ only past the 15th significant digit rank by that digit.
  On SQLite the writes of one process take turns before their
  `statement_timeout_seconds` starts. A burst does not time out on the queue for
  SQLite's one write lock. The deadline still bounds each statement. Another
  process on the file waits on SQLite's own busy timeout.
- **No advisory locks.** It works behind pgbouncer in transaction pooling mode:
  no session state, no `LISTEN`, no prepared-statement names it must keep.
- **Keys without a TTL are kept.** The enqueue-dedupe pending record has none.
- **The driver purges its own table.** `aopen` starts one task on the driver's own
  engine. Every `purge_interval_seconds` it deletes expired rows in batches of
  `purge_batch_size`. The database computes the cutoff: `now()` minus
  `purge_grace_seconds`. A store on a database of its own is purged too. Several
  processes may purge at once without harm. A failed purge is logged as a warning
  with the error's type name, counted in
  `sandloom_atomic_store_operation_errors_total{operation="purge"}` and retried at
  the next interval. Until a row is purged it takes space and nothing else.
- **The table is logged, never `UNLOGGED`.** An unlogged table is emptied by a
  crash, which would lose revocation marks. The driver reads the table's
  persistence on `aopen` and in `health`. It refuses an unlogged table, and after a
  failed `health` check every operation raises until the next `aopen`.

Two more facts matter in operation. The table is created with a small
`fillfactor` and a low autovacuum scale factor, because every revocation and every
`set` rewrites a row. `expires_at` is not indexed, so those updates stay in place
(HOT).

`isolation_identity()` is `database:<system identifier>/<database name>`. It
reads `pg_control_system()`, which an unprivileged role may read. A managed
service that blocks it makes the call fail, and production attestation with it.
The production attestation refuses a store whose identity equals the default
cache's, so a `database` store never collides with a Redis cache. A security
store on the application's own database goes down with it.

SQLite is for one host, because a file belongs to one host. It declares
`never_evicts` and not `shared`. Production refuses it for a store that several
processes use.

#### Who may write the table

On a shared application database the table is writable by the application role.
It has no row-level security and the migrations exempt it, since a revocation mark
is not tenant data. With the same role, module SQL can delete or rewrite a
revocation mark or a block.

Give the driver its own role. Put its URL in `INFRA.atomic_store` with that role,
and remove the application role's access:

```sql
REVOKE ALL ON infra_atomic_store FROM sandloom_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON infra_atomic_store TO sandloom_store;
```

Without that separation the store is only as safe as the application's SQL.

#### What it costs

Each operation is one SQL statement in autocommit, which is one round trip. Two
exceptions exist. `set_subject_state` runs a transaction. `add_or_get` on a held
key runs the insert and then a read. Measured on a laptop against PostgreSQL 16
and Redis 7 in local containers, with the asyncpg driver and an idle pool (median
of 1,500 calls):

| operation | `redis` | `database` | difference |
| --- | --- | --- | --- |
| `get` (the revocation check of an authenticated request) | 0.42 ms | 0.59 ms | +0.2 ms |
| `compare_and_set` | 0.43 ms | 0.64 ms | +0.2 ms |
| `add_or_get`, creates the key | 0.46 ms | 0.80 ms | +0.3 ms |
| `add_or_get`, key held | 0.43 ms | 1.51 ms | +1.1 ms |
| `set_subject_state` (a revocation or block) | 0.44 ms | 1.77 ms | +1.3 ms |

An authenticated request pays one `get`: +0.2 ms here, and plan for +0.2 to
+0.7 ms as the database moves further away. A write costs more. A revocation or a
block costs about +1.3 ms, roughly four times Redis. An idempotency reservation
costs +0.3 ms when it creates the key and +1.1 ms when the key is held.
Revocations and blocks are rare. The numbers move with your network and pool, so
measure on yours before you rely on them.

The method: a script times each call on one idle connection and takes the median
of 1,500 calls per driver. The statement count per operation is pinned in
`tests/perf_budgets/test_budget_atomic_store_database.py`: `get` and
`compare_and_set` run one statement, as does `add_or_get` that creates the key.

Choose `database` when removing Redis from the deployment is worth that price.
Keep `redis` when you already run it, or when the request path is the first thing
you tune. That is one round trip fewer per operation, and the security store then
does not share load with your application's database.

#### Backups

The table is in the core chain, so a PostgreSQL dump of its database carries it.
A dump older than a later block or revocation lifts that block or revocation
silently. `sandloom backup plan` flags a database that holds the `security`
store, and its restore steps name `sandloom backup reconcile-security --apply`.

#### Dead connections

A connection pool that does no ping on checkout keeps each `get` at one round trip.
A pooled connection that died while idle fails the first send of an idempotent
operation, which the driver sends once more. `add`, `add_or_get`,
`compare_and_set` and `compare_and_delete` are never sent twice, so they fail as
`Unavailable` instead. Set `pool_pre_ping` to `true` to ping on every checkout. That
costs three more round trips with asyncpg.
