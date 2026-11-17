---
title: Upgrade an application
description: Review and apply Sandloom package upgrades, generated contracts and database migrations with a rollback plan.
type: how-to
---

# Upgrade an application

Sandloom is an alpha. Pin and review the versions you deploy; do not infer
compatibility from a passing installation alone. The current published alpha
is `0.7.0a14`. See the [changelog](../changelog.md) and <!-- sandloom-release -->
[versioning policy](../contributing/versioning.md).

1. Record the installed kernel and optional battery versions, the application
   revision and deployed topology. Back up the database and security state
   following [Backups](operator-reference.md#backups-restore).
2. Read the release notes for every package being changed, including breaking
   changes and dependency constraints. Standard battery packages and the kernel
   have independent distributions; verify their declared compatible ranges.
3. Update the application's requirements to the selected published versions,
   then run `uv sync` and review the lockfile. A `--framework-source path` app
   consumes its local checkout; switch sources deliberately before assessing
   published-wheel behavior.
4. Run `uv run sandloom generate`, review generated changes, and run
   `uv run sandloom test --ci`. A compatibility error requires the documented
   migration or versioned contract/event rollout, not a deleted snapshot.
5. Review database migration plans in a staging copy. Run
   `uv run sandloom migrate upgrade-all` to inspect the plan and add `--apply`
   only when ready to execute it. Verify health, readiness and representative
   authenticated, tenant and billing flows.
6. Deploy using the reviewed package lock and migration plan. Keep the previous
   application artifact and a database-aware rollback/recovery plan; reverting
   Python code does not automatically undo schema or external payment effects.

## Refresh inspection tools and agent guidance

After installing a new package version, regenerate the authoring index and
refresh shipped skills from the application root:

```bash
uv run sandloom generate
uv run sandloom add skills
uv run sandloom graph
```

`generate` loads the application in its configured authoring environment and
refreshes contracts, snapshots and `.sandloom/inspection.json`. Cold `graph`
and `context MODULE` read that saved model; they never regenerate implicitly.

`add skills` updates the agent files it wrote and keeps the ones you edited; [keep the skills current](../ai/agent-tooling.md#keep-the-skills-current) has the details.
See [module maps](../ai/module-context.md) and the
[architecture explorer](../ai/architecture-graph.md) for formats and freshness.

For a breaking event payload, use [dual-publish versioning](../modules/between/event-versioning.md).
The release after `0.7.0a10` has its own guide:
[Upgrade to portable SQL and SQLite](upgrade-sqlite-and-portable-sql.md).
The [Public API 1.0 migration](upgrade-1.0.md) is a specific historical source
migration guide, not the general package-upgrade procedure.

If the upgrade fails, capture the error code, package versions and a redacted
reproduction for [support](../help.md).

## Upgrading a database driver to contract major 2

The `database` contract is major 2. Update a driver package that implements it
([Write a driver](../infrastructure/write-a-driver.md#a-database-driver)):

| Before | After |
| --- | --- |
| `contract_version = 1` | `contract_version = 2` |
| `session: Any` in your stores and `has_pending_changes` | `session: UnitSession` (`sandloom.driverkit`); narrow it inside your store |
| `execution_adapter()` and `contract_execution_adapter()` on the provider | `planner_adapter(self, *, contract=False)`, answering `None` where the database cannot run declared operations |
| Raw sessions only through an engine of your own | `session_factory`, a property answering a callable that yields a session of the instance (or `None`) |
| `OutboxStore`, `AuditStore`, `CommandLogStore`, `RequestLogStore` imported from `sandloom.core.infra.contracts` | Import them from `sandloom.core.infra.ports.database`, where they now live (`contracts` re-exports them for kernel code) |

## Upgrading an event bus driver to contract major 2

The `eventbus` contract is major 2: the `streams` transport is a typed
`EventTransportProvider` ([Write a driver](../infrastructure/write-a-driver.md#an-eventbus-driver))
and `memory` and `broadcast` are the kernel's own volatile buses. A driver or module that names
a feature of the previous vocabulary is refused at load, and the refusal names the
replacement. There is no alias.

| Before | After |
| --- | --- |
| `publish`, `subscribe` in a requirement or a driver's features | Drop them: every driver has both operations |
| `volatile` | Drop it: a volatile bus declares no `durable` |
| `at_least_once`, `consumer_groups` | `durable` |
| `durable`, `wildcard_subscribe` | Unchanged (`wildcard_subscribe` is the volatile buses' only) |
| `EventBusProvider.bus()` and flags read with `getattr` | `EventTransportProvider` and its declared features: `shared`, `blocking_consume`, `group_start_id`, `lease_renewal`, `dlq_ops`, `inspection`, `inventory` |
| `GroupOwnershipError`, `TransportBatchError` | `GroupOwnedElsewhere` (an `AlreadyExists`), `PartialPublish` (an `InfraError`; `published` and `retryable`) |
| Group owners, DLQ entries, group and inventory records as `dict`s | `GroupOwner`, `DeadLetter`, `GroupInfo`, `StreamInfo`, `ConsumerInfo` (`sandloom.driverkit`) |

## Upgrading a cache driver or a cache caller to contract major 2

The `cache` contract is major 2 and holds the operations of a module. These are `get`, `set`,
`delete`, `add`, `incr`, `get_many`, `set_many` and `windowed_counter`. `compare_and_set` and
`compare_and_delete` are behind the feature `cas`. The cache may evict a key. What a security
guarantee needs lives on `atomic_store`. There is no alias for what leaves.

| Before | After |
| --- | --- |
| `cache().incr_window(key, previous_key, ttl_seconds=...)` | `cache().windowed_counter(key, previous_key, ttl_seconds=...)`; `ttl_seconds` is required |
| `cache().incr_many(keys, ttl_seconds=...)` | `incr` for each key, with `asyncio.gather` where the round trips should overlap |
| `cache().set_nx_get(key, value, ttl_seconds=...)` | `add`, then `get` when it answers `False` |
| `cache().set_max(key, value, ...)` | Removed. A monotonic mark is `atomic_store` `set_subject_state` |
| `provider.set_subject_state(...)` on a `CacheProvider` | `atomic_store` `set_subject_state` |
| `provider.raw_client()` on a cache | Removed, and no `native()` replaces it. The Redis driver has the non-portable `redis_client()` for its own tests |
| `CacheProvider.compare_and_set` raising `NotImplementedError` without `cas` | Raises `Unsupported("cas")` |
| `contract_version = 1` in a cache driver | `contract_version = 2`; `incr_window` becomes `windowed_counter`, and the hints `incr_many`, `set_nx_get`, `set_max` and `set_subject_state` go |
| `CacheContentionError`, `portable_set_max`, `portable_set_subject_state`, `parse_plain_decimal`, `SET_MAX_MAX_ROUNDS` (`sandloom.core.infra.contracts`) | Removed. `parse_subject_state` and `SUBJECT_STATE_RANKS` are in `sandloom.core.infra.ports.atomic_store` |
| A Redis cache failure raised as a `redis.exceptions` error | `Unavailable`, `Timeout` or `PermissionDenied`; an `incr` of a stored non-integer raises `InvalidArgument` |

Test a cache driver with `CacheConformance` from `sandloom.driverkit.testing`.

## Upgrading a module to the task API

`sandloom.sdk.tasks` carries Sandloom's own task API in place of Celery's, and the `taskqueue`
contract is major 2 (`TaskQueueProvider.send` replaces `enqueue`).
There is no alias for what it replaces.

| Before | After |
| --- | --- |
| `@shared_task(name=..., acks_late=True, reject_on_worker_lost=True)` | `@task(at_least_once=True)`; drop `name=`: it must equal `<module>.<function>`. `at_least_once` needs the driver feature `redelivery` |
| `setup_periodic_tasks(sender)` and a Celery `crontab` | `@periodic(cron="<five fields, UTC>", id="<module>.<job>")` on a function without arguments. 300 s is `*/5 * * * *`, 3600 s `0 * * * *`, 86400 s `0 3 * * *`: a fixed slot, not "24 h after the scheduler started" |
| `task.delay(...)`, `.s`, `.si`, `.apply_async` | `await task.enqueue(Model(...), delay=timedelta(...), idempotency_key=...)` |
| `taskqueue().enqueue(task, payload, delay_seconds=60)` | `taskqueue().enqueue(task, payload, delay=timedelta(seconds=60))` |
| a module requirement or a driver declaring the feature `idempotency_key` | Drop it: the kernel serves the key on every driver through the `idempotency` `atomic_store`, and fails closed without one |

Four behaviours change with it:

1. An enqueue by name (`taskqueue().enqueue("<pkg>.tasks.<name>", ...)`) is checked against the
   task declarations of the enqueuing process, so `<pkg>.tasks` must be imported there or the
   call raises `InvalidArgument`. In a split deployment the web service imports the tasks
   module of every task it enqueues by name. A module enqueues its own tasks; work in another
   module goes through events or contracts.
2. The task goes to the queue the task declares (`@task(queue="slow")`), also from a process
   that never booted the worker table, such as the API process. It used to land in the
   default queue.
3. The dedupe namespace of an enqueue follows the driver's `isolation_identity()`. A key in
   flight at the upgrade is not recognised afterwards: drain the queue, or accept one repeat
   of a keyed enqueue sent before it.
4. The duplicate check of a `@periodic` `id` is per process, not per deployment: keep ids
   unique across your modules (`<module>.<job>`).

## Upgrading a task queue driver or a Celery command to the worker host

The worker and the scheduler are the `taskqueue` driver's host
([Write a driver](../infrastructure/write-a-driver.md#a-taskqueue-drivers-worker-host)):
`TaskQueueProvider.worker_host()` is part of the `taskqueue` contract, which is major 2.
There is no alias for what it replaces.

| Before | After |
| --- | --- |
| `TaskQueueProvider.raw_backend()` (the Celery app) | Removed, and no `native()` replaces it. Implement `worker_host()` in a driver; reach the app of a worker through the driver's own importable object |
| `celery -A sandloom.core.worker inspect\|call\|status` and Flower on `sandloom.core.worker` | `celery -A sandloom_celery.worker inspect\|call\|status`; the app is built from the `broker` option of `taskqueue.default`, boots no application and registers no task. |
| `provider.register_task_table(table, run)` on the Celery provider | Removed: the host registers the table it gets from `WorkerBoot.boot()` |
| `sandloom.core.worker.build_worker_app`, `get_worker_app`, `run_celery_command`, `install_heartbeat`, `heartbeat_is_fresh` | `sandloom.core.worker.run_worker`, `run_scheduler`, `check_worker` (the kernel side); the Celery side is `sandloom_celery.worker` |
| `sandloom.core.beat_lease` (`BeatLease`, `LeasedScheduler`, `install_beat_lease`) | Moved to `sandloom_celery.worker`; the lease is resolved by the kernel (`sandloom.core.infra.fenced_lease`) and passed to the scheduler as `lease` and `lease_name` |
| `sandloom.core.async_runner.install_celery_hooks`, `sandloom.core.gc_tuning.install_celery_hooks` | Removed: the host calls `WorkerBoot.on_process_start`, `on_process_stop` and `before_fork` |
| A health detail or error of the broker probe naming `scheme://host:port` | The details carry no broker address and no credential; read the error type |
| `sandloom worker` on macOS (the prefork pool never saw the task table) | The worker refuses `prefork` where billiard spawns: set `worker_pool` to `solo` or `threads` in the `celery` taskqueue instance's options (`INFRA`) |
| The Celery broker probe bounded by `HEALTH_CHECK_TIMEOUT_SECONDS` | The `celery` option `health_timeout_seconds`, filled from that setting when the configuration leaves it out; `probe_broker(app, limit_seconds=)` takes the bound as a required argument |

The three commands are unchanged for an operator. `sandloom worker check` now builds the
infrastructure registry from the settings and asks the driver's host. It needs the same
`DATABASE_URL` and `INFRA` as the worker, boots no application and asks
no broker.

## Upgrading to 0.7.0a15: the volatile event bus driver is `broadcast`

The volatile `eventbus` driver that ran over a `broadcast` instance was called `redis`. It
talks to no service of its own: it runs on the `broadcast` instance named by its option
`broadcast`. It is now named after that capability, as the `database` driver of
`atomic_store` is. An instance that selects it changes one line:

```diff
-INFRA={"eventbus": {"default": {"driver": "redis"}}}
+INFRA={"eventbus": {"default": {"driver": "broadcast"}}}
```

`EVENT_BUS_BACKEND=redis` becomes `EVENT_BUS_BACKEND=broadcast`. The driver no longer
needs the `redis` package: the Redis client belongs to the `redis` driver of
`broadcast`, which you still select under `INFRA.broadcast`. Behavior is the same.

The old name has no alias. Startup refuses an `eventbus` instance that still writes
`"driver": "redis"`, wherever it writes it. That covers `INFRA` and the composition's
`DEFAULT_INFRA`, as well as a configuration built in code and `EVENT_BUS_BACKEND`.
The error names the new spelling and prints no option, so it shows no url. Other
capabilities keep their `redis` driver.

## Upgrading the Celery driver's connection checks

The `celery` taskqueue driver now refuses, at startup, connections it used to accept.
Check each of these before the first start:

1. **A `rediss://` url without `ssl_cert_reqs`.** Kombu connected such a broker with no
   certificate check. Add `?ssl_cert_reqs=required&ssl_check_hostname=true` to the
   `broker` or `backend` url. Without `ssl_check_hostname=true`, any certificate of the
   trusted CA is accepted, for any name. `?ssl_cert_reqs=none` states that no check is
   wanted. An `amqps://` broker checks the certificate chain and not the hostname.
2. **A `sentinel://` url.** Set `master_name` on the instance, and `backend_master_name`
   for a `sentinel://` backend. Kombu reads the master only from the transport options,
   which a config module may no longer set to another value.
3. **An `sqs://` url with a host.** Kombu used plain HTTP for it. The driver now uses
   HTTPS, so an endpoint that speaks only HTTP, such as a local emulator, is
   unreachable. A `predefined_queues` entry of a config module may carry only its
   queue `url` and the backoff keys. Its credentials and region come from the `INFRA`
   url and the `region` option. The queue `url` must be in the region of the `INFRA`
   url's AWS host, or of the conf `region`. With the `INFRA` option `sqs_account_id` it
   must be in that account too. VPC endpoint and FIPS hosts are accepted. A conf
   `region` (in `broker_transport_options`) that differs from the region of the `INFRA`
   host is refused. A FIPS or VPC endpoint queue `url` does not move traffic: the
   `INFRA` host picks the endpoint. An `INFRA` broker host that names no readable
   region is refused, because it would leave the queue-url pin off. Three kinds of
   host are refused. One is a trailing dot on any host
   (`sqs.eu-west-1.amazonaws.com.`). Another is a percent escape or a non-ASCII
   character in the host. The third is a form under an AWS domain that the pin
   cannot read. These are the legacy `<region>.queue.amazonaws.com`, the dual-stack
   `sqs.<region>.api.aws` and `sqs-fips.<region>.amazonaws.com.cn`. The China regions
   have no FIPS endpoint. Use `sqs.<region>.amazonaws.com`. An emulator or proxy host
   without a trailing dot is accepted.
4. **A `;`-separated url.** A space or an empty chunk around a `;` is refused, and so
   are urls of different schemes.
5. **A config module that sets a result backend's endpoint, project or path.** For
   example `dynamodb_endpoint_url` or `s3_base_path`. State it in
   the `backend` url. The list is in [Background tasks](../modules/background/tasks.md).

An unbound `@shared_task` `.delay()` in a process that publishes now goes to the
`default` instance. A named instance no longer takes over `celery.current_app`.

## Upgrading to 0.7.0a14: the flat Redis settings

Startup refuses `CACHE_URL`, `BROKER_URL` and the six `CACHE_*` bounds with any value, in
the environment or in a `.env` file. Each message names what to write instead. The table is
in [Configure infrastructure](../infrastructure/configure.md#upgrade-flat-settings-that-became-driver-options).
No instance takes a url from a flat setting or from another instance any more. State the
url of every Redis instance the app selects in `INFRA`, and the Celery `broker` of
`taskqueue.default`. Two cases need care before the first start:

1. **A staging-like environment whose `INFRA` `cache.default` url differs from
   `CACHE_URL`.** Outside production the kernel synthesizes the `security` store
   (`atomic_store`) on the url `CACHE_URL` had. Without that variable the store has no url
   and startup fails with `MissingUrlError`. State `atomic_store.security.url` as the old
   `CACHE_URL` before you upgrade. A different url puts the store on another Redis, and the
   revocations and blocks written earlier are not seen. The same holds for `lease.beat`.
2. **A rolling deploy with a `broadcast`.** A `broadcast` that left its `url` out used to
   take the cache's. During the roll, old processes publish on that Redis and new ones on
   the url `INFRA` states. Set `broadcast.<instance>.url` to the old value first, deploy,
   then move it.

In production the kernel synthesizes no `security` store: declare it. A synthesized
instance that shares the cache's Redis database logs a WARNING there, not the INFO of a
development start.

A `CELERY_BROKER_URL`, `CELERY_BROKER_READ_URL`, `CELERY_BROKER_WRITE_URL` or
`CELERY_RESULT_BACKEND` can sit in the environment of a web process. If it differs from
the `taskqueue` instance's `broker` or `backend`, a publish now stops with a
`ForeignCeleryConfError`. Celery used to follow the variable and publish to that broker.
Unset it, or state its value in `INFRA`.

A Celery config module that sets a connection part is refused the same way. The parts
are `broker_transport`, `broker_host`, `broker_port`, `broker_vhost`, `broker_user`
and `broker_password`. The url does not matter: the `INFRA` url carries the whole
connection.

A config module may set `broker_use_ssl` only to `None` or `False`. It may set
`broker_transport_options` and `result_backend_transport_options` only with the tuning
keys listed in [Background tasks](../modules/background/tasks.md). A dict or `True` in
`broker_use_ssl` is refused the same way. So is a transport option such as `host`,
`password`, `ssl` or `sentinel_kwargs`. Move TLS options into the query of the `INFRA` url.
A `master_name` is accepted only when it equals the `INFRA` option of that name.

## Upgrading from 0.7.0a4 to 0.7.0a5

This alpha changes infrastructure extension APIs. Update the framework and
standard modules together, then regenerate and run the application gates before
deployment. Ordinary built-in accessor names such as `cache()` and `notifier()`
remain available. Custom capability declarations and driver packages need the
changes below ([ADR-0011](../adr/0011-driver-api.md)):

| Before | After |
| --- | --- |
| `capability("x", name, contract=X)` | `capability(X_REF, name)`, with `X_REF: CapabilityRef[X]` published by the capability's package |
| `Capability("x", X, name)` | `Capability(X_REF, name)` |
| `CapabilityRequirement("cache", features={"incr"})` | No feature needed: `incr` is required of every driver. The same goes for `get`, `set`, `add` and `delete`, for the object store's `put`, `get`, `delete` and `url`, and for `enqueue` |
| A driver package's `register(registry)` in the `sandloom.infra` group | The class by name in the `sandloom.drivers` group, with `contract_version` and `supported_features` on the class ([Write a driver](../infrastructure/write-a-driver.md)) |
| `STORAGE_TYPE=s3` starting with a placeholder | Install a driver package that publishes `objectstore.s3` |

An experimental capability in production now needs `experimental_capability` in
`SANDLOOM_ACCEPTED_RISKS`. Test URLs with `localhost` and a port from 32768 up are refused:
use `127.0.0.1`.
