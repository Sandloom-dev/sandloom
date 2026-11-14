# Research: the open owner questions

Study of 2026-11-14 for [the design](design.md), task `idk-owner-questions`. It has two parts:

- **Code facts.** Read-only reading and grep at `131f94c1`. No tests or benchmarks were run.
- **External practice.** Pages fetched on the web. Claims not re-verified are marked [M].

Question 6 (settings move to driver options in one step) was answered by the owner, and is not covered here.

## Q1. `atomic_store` as its own capability

**Code**

- Every authenticated request makes one live `GET` of `sandloom:auth:subject:{sub}`
  (`auth/verification.py:133`). That one record holds both the revocation mark and the block.
- The result is memoised only within the request. Every authenticated write adds a second `GET`
  before COMMIT (`unit_of_work/hooks.py:232-257`).
- Round trips are pinned in `tests/perf_budgets/test_cache_round_trips.py`.
- Automatic idempotency (`Idempotency-Key` on authenticated writes) is already in SQL
  (`http_idempotency_log`). The cache form covers two cases:
  - an explicit `idempotency_guard()` without `durable=True`;
  - routes with no request unit.
- Enqueue dedupe keeps its pending record with **no TTL** (`tasks/enqueue.py:83-135`).
- The RPC nonce is `SET NX` with a TTL of `3*skew`.
- Revocation marks live ≥24 h. Blocks have no TTL.

**Practice**

- Redis advises a separate instance for keys that must not be evicted. Under `volatile-*`, any key
  with a TTL can be evicted ([eviction](https://redis.io/docs/latest/develop/reference/eviction/)).
- Laravel makes locks an optional cache interface. A store without them throws at runtime.
- django-ratelimit on a database cache silently undercounts, and nothing checks for it.
- Systems that care about correctness give this state its own abstraction:
  - Dapr: a state store with ETags, first-write-wins and TTL;
  - Rails: Kredis kept apart from Solid Cache, which evicts FIFO;
  - Spring: `LockRegistry`.

**Recommendation: yes, a separate capability.** The "durable cache" variant is exactly the case
where the failure is silent (Laravel, django-ratelimit).

## Q2. Redis and Celery out of the kernel wheel this wave

**Code**

- `sandloom-s3` is tested by its own workflow (`objectstore-s3.yml`) and is released by
  `sync_release.py --write` together with the kernel.
- The package lists are maintained by hand, and the checks are special-cased by name. The places
  to touch:
  - `sync_release.py:22`, `pypi_release.py:18`, `check_package_distributions.py:50,222,290,628`;
  - `release.yml`;
  - `public-source.json`.
- 18 test files import redis or celery directly; 149 files mention them.

**Practice (Airflow providers, 2.0)**

- Separate packages installed through extras, with a lower bound on the core version.
- The main pain [M]: a provider calling a core API that the installed core does not have, and
  dependency conflicts.
- Airflow kept the old import paths as deprecated shims.

**Recommendation: yes, this wave, under four conditions.**

1. Release the packages in lockstep with the kernel and pin `sandloom>=X.Y,<X.(Y+1)`.
2. Keep the `sandloom[redis]` / `sandloom[celery]` extras.
3. Drivers import only `driverkit`. This is already slice `idk-driver-deps`, and it removes
   Airflow's main problem.
4. Make the release tooling data-driven first: the list of packages in one place, with no
   special-casing by name. Then adding a package touches no code. This is a step of
   `idk-pkg-redis`.

No shims for the old paths: the kernel carries no deprecated API, and there are no such paths in
user code (see Q5).

## Q3. Database-backed `atomic_store` and `lease`

**Code**

- What is already there:
  - tables of the right shape (`http_idempotency_log`, `contract_command_log`, an outbox with lease
    columns);
  - a generic claim/complete/purge layer (`sql_alchemy/dedupe_log.py`);
  - a sweeper in the relay loop (every 30 s, batches of 1000, `tasks/relay.py:258-290`).
- The saga sweep takes a lease from the cache. Sagas already use `FOR UPDATE SKIP LOCKED`.
- pgbouncer is not in deploy; it appears only as advice. The code relies on session-level advisory
  locks (migrations, `schema_reset`).

**Practice**

- Solid Cache at 37signals: reads ~40% slower than Redis, yet p95 fell from 375 to 225 ms because
  of a larger cache ([post](https://dev.37signals.com/solid-cache/)).
- BigBinary: PostgreSQL is ~2x slower than Redis on reads.
- Latency [M]: 0.3–1 ms per PostgreSQL round trip versus 0.1–0.3 ms for Redis.
- UNLOGGED tables are emptied after a crash and are not replicated, so they are wrong for this
  state.
- Session advisory locks do not work under pgbouncer transaction pooling.
- Dapr PostgreSQL deletes expired rows in the background, and filters them on read.
- Solid Queue removes duplicate runs with a unique index.

**Recommendation**

- **`lease` on the database: yes.** Calls are rare (beat, saga sweep). Use a logged table:
  `name`, `holder`, `expires_at`, `fence`. Acquire with `INSERT … ON CONFLICT … WHERE expires_at
  < now()` and `fence = fence + 1`. Advisory locks are not used.
- **`atomic_store` on the database: yes, as an option with a stated price,** not as the standard
  composition's default.
  - The price: +0.2–0.7 ms per authenticated request, and twice that on a write.
  - What it buys: an application on PostgreSQL alone.
  - Implementation:
    - a logged table;
    - `set_max` via `GREATEST`;
    - CAS via `UPDATE … WHERE value = $old`;
    - expiry filtered on read, plus the relay's existing sweeper;
    - autovacuum tuning and a lower fillfactor against bloat.
  - Its own slice after `idk-atomic-store`, gated by the same kit and the round-trip budget.

## Q4. Sandloom's own task API

**Code**

- Only two modules define tasks:
  - `sandloom_identity`: 4 tasks plus `setup_periodic_tasks`;
  - `sandloom_billing`: 1 task plus `setup_periodic_tasks`.
- No example defines a task.
- `.delay` and `.apply_async` are never called outside tests. `.s()` appears only in those two beat
  hooks and in the generator (`cli/add.py`, `sdk/_prim/recipes.py`).
- `sdk/_prim/tasks.py` adds three things over Celery:
  - the `Task[P,R]` Protocol;
  - an inline fallback when Celery is not installed;
  - `cron("5 fields")`.
- Nothing in `src` calls `taskqueue().enqueue(idempotency_key=)` outside the driver.

**Practice**

- Django 6.0: `@task(priority, queue_name, takes_context)`, `.enqueue()` / `.aenqueue()` returning
  a `TaskResult`, JSON arguments, enqueue on commit. It has no periodic API
  ([docs](https://docs.djangoproject.com/en/6.0/topics/tasks/)).
- Periodic schedules:
  - Solid Queue: `recurring.yml`;
  - procrastinate: `@app.periodic(cron=…)`, deduplicated by the database.
  - Celery beat and taskiq require exactly one scheduler, or jobs run twice.

**Recommendation: yes, with a Django-6-like shape.**

- `@task` and `task.enqueue(payload, delay=, idempotency_key=)`.
- A declarative `@periodic(cron="…", id="…")` instead of `setup_periodic_tasks(sender)`.
- Deduplication of a periodic run by `(id, slot)` belongs to the driver under `lease`, not to "run
  one beat".
- The migration is cheap: 5 tasks in 2 modules, the generator, and 2 doc pages.

## Q5. Escape hatches

**Code**

- `raw_client` and `raw_backend` are called only by the kernel itself:
  - `contract_cache_shadow.py:845-885`;
  - `worker.py:109`;
  - `testing/fixtures.py:335`;
  - and ~18 tests.
- Modules are barred by C003.
- Nothing found:
  - no doc or example tells application code to use them;
  - no `.native()`;
  - no `.client` on a provider.

**Practice**

- Go CDK puts `As()` on the portable type and discourages it.
- SQLAlchemy and Django expose the native connection on the wrapper [M].
- None of them enforces "the core does not use it".

**Recommendation: remove them from the contracts entirely, and add no `native()` for now.** No
application code uses them. A driver package may have public attributes on its own class
(`RedisCache.client`), documented as non-portable. C003 is extended to the kernel. A portable
accessor is added when a real case appears.

## Q7. Driver tiers

**Code**

- B023 exists for capabilities only (`drivers/bootstrap.py:178-203`): in prod with a configured
  experimental capability, startup refuses unless
  `SANDLOOM_ACCEPTED_RISKS=["experimental_capability"]` is set.
- `DriverRegistration` has no tier field.

**Practice**

- Dapr's alpha/beta/stable are certification criteria only, with no runtime refusal.
- Terraform's tiers are badges.
- Kubernetes: alpha is off by default and is enabled by an explicit flag.

**Recommendation: yes, through the existing mechanism.**

- A `tier` on the registration (`experimental` / `beta` / `stable`), shown by `doctor`.
- In prod, an `experimental` driver refuses as B023 does, until the risk `experimental_driver` is
  accepted (opt-in, as in Kubernetes).
- `beta` only warns.
- Built-in drivers and the packages in this repository are declared at least `beta`, since their
  kits run against real services in CI.

## Q8. Attestation of the security store

**Code**

- `production_profile.py` checks two things:
  - statically (`config_problems`, :132-174): `SECURITY_CACHE_INSTANCE` is not `default` and its
    host:port differs from the default cache;
  - at runtime (`enforce_runtime`, :273-299): the Redis `run_id` differs, which guards against DNS
    aliases.
- The stated threat is the **eviction policy**: "maxmemory policies are per server", "an evicting
  cache can never drop a revocation mark" (:22-23, :98-101). It is not blast radius and not
  credentials.
- The idempotency split exists for latency only (`security-model.md:896-907`).
- Guarantees: G-PROFILE-01/08, G-IDEM-16, G-REVOKE-19.

**Practice**

- OWASP asks for a protected store and server-side revocation, and says nothing about physical
  isolation.
- Solid Queue and Solid Cache recommend a separate database but support a shared one.

**Recommendation: yes, a database-backed store counts.** The check states the real threat:

- "the security store declares `never_evicts`";
- "it is not the same instance as the evicting module cache", compared by `isolation_identity()`.

For Redis, `isolation_identity` is the `run_id`, as today, plus a `maxmemory-policy noeviction`
check. That check does not exist today, although `security-model.md:872-895` recommends the
setting. The database driver passes by nature: it does not evict.

## Q9. The worker command

**Code**

- `celery -A` / `sandloom.core.worker` appears on ~135 lines:
  - deploy 17, examples 14, docs 9 + 4 generated, tests ~18;
  - static checks and the deploy-profile test 6;
  - generators 7;
  - docstrings ~17.
- Two parsers match the literal argv `["celery","-A","sandloom.core.worker","worker"|"beat"]`: T010
  and the deploy-profile test.
- The CLI is one flat argparse table (`src/sandloom/__init__.py:168-242`). The closest precedents
  are `outbox`, `streams` and `sagas` in `_CONFIGURED_COMMANDS`.

**Practice**

- Sentry: `sentry run worker`; `sentry celery` does not exist.
- Airflow: `airflow celery worker`.
- Rails: `bin/jobs` with `check`.
- Airflow's Helm chart still probes with raw `celery inspect ping` [M].

**Recommendation: one step, no shim.**

- Commands: `sandloom worker`, `sandloom beat`, `sandloom worker check` (replaces `python -m …
  healthcheck`).
- T010 and the deploy profile parse the new argv.
- `sandloom-celery` keeps a stable importable Celery app (`sandloom_celery.app`). This is not a
  shim of the old path: it is the driver's native interface for `celery inspect` and Flower.

## Found along the way

- `benchmarks/ladder/STANDS.md:234-240` still says `MGET` on the write rungs; ADR-0009 moved to `GET`.
- Cachegrind is run by hand and is not in CI. The measurement in `idk-atomic-adopt` is manual, or
  through `tests/perf_budgets`.
- The enqueue-dedupe pending record has no TTL, so a database store must support non-expiring keys.
