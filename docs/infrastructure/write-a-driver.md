---
title: Write a driver
description: Connect a new backend to a Sandloom capability - implement the contract, publish it by name, select it with INFRA, and check it with the conformance kit.
type: how-to
---

# Write a driver

**Goal:** a package that adds a backend to a
[capability](index.md), selected by configuration,
with no change to business modules or to the kernel.

**Use it** for a `notification` or `objectstore` backend that Sandloom does not ship, such
as an email API or a Google Cloud Storage store. Use it also for a
[capability of your own](own-capability.md).

**Do not use it** for business logic: that is a [module](../modules/create.md). A contract is
public only with a conformance kit, so the `taskqueue` contract is not available yet.
The `cache`, `notification` and `objectstore` contracts are
(`CacheConformance`, [the cache contract](cache.md)). The `database` backend
carries the transaction guarantees and is not open to drivers
([why](database.md#the-relational-paradigm)). An `eventbus` driver is an
[`EventTransportProvider`](#an-eventbus-driver). It is kernel-facing. Its conformance
kit is `EventTransportConformance`; the Streams driver in `sandloom-redis`
(`sandloom_redis/eventbus/`) passes it.

!!! note "Version"
    This guide targets `sandloom==0.7.0a16`. <!-- sandloom-release -->
    [`sandloom.driverkit`](driverkit.md) is included in this release.

## Prerequisites

- An application created with `sandloom new`.
- A separate package for the driver, outside `<app>/modules`. A driver may import its
  backend's SDK and `sandloom.driverkit`; a module may not
  ([SANDLOOM-C004](../reference/error-codes.md#sandloom-c004)).

## Steps

### 1. Implement the contract

This complete driver is for development: it appends messages, including their bodies, to
a file.

```python title="sandloom_notify_file/driver.py"
--8<-- "docs/snippets/drivers/file_notifier.py"
```

- **`contract_version`** is the integer major of the contract the driver was written for
  (`notification` and `objectstore` are 2). It is required, and another major, or a
  string such as `"v1"`, is refused when the driver loads. Built-in drivers are checked
  the same way.
- **Errors.** An operation raises only `InfraError` subclasses from `sandloom.driverkit`:
  map the backend's errors to `NotFound`, `InvalidArgument`, `TooLarge`,
  `PermissionDenied`, `Unavailable` or `Timeout`. A call that needs a behaviour feature
  the instance does not declare raises `Unsupported(feature)`. Put the error type in
  a message, never the backend's text: it can carry a URL with credentials. Map only a
  transient fault (a lost connection, a failover) to `Unavailable`. The outbox relay
  reads it as an outage: no attempt is spent and the row is never dead-lettered.
  A reply the backend sends for this request alone (an unknown `ERR ...`) is
  `InvalidArgument`, which is not retryable. The `redis` drivers map through one
  classifier, `sandloom_redis._errors`. A driver may set a fixed `hint` sentence on the
  error, never text from the backend. The relay prints it on its outage line and
  decides nothing by it. Set it as an attribute (`error.hint = "..."`), not with the
  `hint=` keyword: the driver then works on kernels whose `InfraError` lacks it.
  The kernel names no client library's error. An error the relay does not recognise
  spends a delivery attempt, as the event's fault. The relay then logs a warning with
  the class name, never the message, once per class every five minutes. Read that line
  as a missing mapping. An unmapped outage burns attempts and dead-letters the rows
  after `OUTBOX_RELAY_MAX_ATTEMPTS`.
- **`supported_features`** names what the driver really does, and is its one declaration.
  A module that requires `features={"email"}` is refused at startup on a driver that does
  not advertise it. If a feature depends on an option, override the classmethod
  `features_for(options)` and still list the feature in `supported_features`: only declared
  features count. Do not override `features()`: an instance that reports other
  features than it declared is refused.
- **`options`** holds the keys of the `INFRA` entry, and nothing from another driver.
  Options can hold secrets, so never log them. For validation before the driver is built,
  set `options_schema` to a pydantic model.
- **The constructor** is called on first use, or at startup if an instance is configured.
  Open no connection at import. To open a pool at startup, report health in
  `/diagnostics/infra` or release resources, override `aopen`, `health` and `aclose`.
- **`Notification.body_fields`** holds the values `body` was built from, for a driver that
  renders its own template. It is as sensitive as the body: never log it.

A driver whose instance must not lose a key on a Redis registers its endpoint in `aopen` and
unregisters it in `aclose`. That covers an `atomic_store`, a `lease`, an event transport and a
task queue. The helper is
[`sandloom.driverkit.shared_endpoints`](driverkit.md#sandloomdriverkitshared_endpoints). The
operator is warned at startup when the instance shares a database with `cache.default`.
Registrations live per process, not per app: two apps built in one process (a test
suite) see each other's endpoints until `shared_endpoints.reset()`.

`self.implicit` on the provider is `True` for an instance the kernel synthesized in
development. Nobody wrote it in `INFRA`: it is an instance a composition selects without
an entry. That is its `default` instance of a capability, and the two named ones,
`lease.beat` and `atomic_store.security`. It is never `True` in production, where a
synthesized instance is as real as a declared one. A driver reads it for one thing, the
`declared=not self.implicit` of `register`. An overlap of synthesized instances then
logs at INFO, and an overlap with an instance the operator wrote logs as a WARNING.

A connection option (a url, a broker, a pool bound) is an option of your instance, written in
`INFRA`, with a default in the options schema. Do not read it from a flat setting, and do not
take it from another instance. No instance inherits another's url, so a driver that needs a
url refuses startup when its instance has none. One exception: the `streams` event bus of
`sandloom-redis` runs on the default cache client when its instance has no `url`. A durable
stream can then land on the database of `cache.default`, which may evict. The operator gets
the shared-database WARNING above and no refusal. State the bus's own `url` in `INFRA`.

A driver that reads what another capability's `aopen` set up declares
`opens_after = ("cache",)` on its class. `aopen_all` then opens those capabilities first,
whatever order `INFRA` lists them in. Two drivers that wait for each other are refused at
startup.

A driver whose option a flat setting still
feeds (the readiness probe's bound, `HEALTH_CHECK_TIMEOUT_SECONDS`) declares it with
`settings_fill`. That is a `SettingsFill` on the class. It says which setting fills which
option when an instance leaves the option out. The kernel applies it without naming your
driver, once the registry has your class. An option the instance states always wins.

- `bounds` fill an option left out. `caps` cut a number above the setting down to it.
- A table may name only `health_check_timeout_seconds`. No url and no client bound is
  filled from a setting: `CACHE_URL`, `BROKER_URL` and the `CACHE_*` bounds are removed
  settings. `SettingsFill` refuses any other name when the class is defined.

The fill is skipped when your package, or a package you require, is missing. It raises no
import error: the registry refuses an instance that cannot run. A driver that reads no flat
setting declares none.

A driver that connects to one server by url sets `url_option` on its class. The value is the
name of that option: `"url"`, or `"broker"` for a task queue. It must be a field of
`options_schema`.
`sandloom dev up` reads it. It writes a url into the checkout's `INFRA` for each instance on
your driver, so the kernel keeps no list of driver names. A driver that reaches no server by
url leaves it `None`.

A driver that keeps data on a server says so to `sandloom backup plan` with a classmethod,
`backup_hint(options)`. It returns a `BackupHint` (from `sandloom.driverkit`) or `None`.
`None` is the default: the driver has nothing to back up, as a memory driver or an
instance with no url.

The hint holds these fields:

- `url`: the server's url as the instance states it. `None` means the instance runs on
  the server of `cache.default`.
- `kind`: one word.
- `criticality`: `critical`, `required` or `optional`.
- `backup` and `restore`: the operator's steps.
- `tolerable_loss`: a note for data that may be lost.

The plan keeps one item per `(kind, url)`. Instances whose hints name one server share
it, and each step prints once. The kernel writes each role line itself, as
`<capability>:<instance>`. A `tolerable_loss` line prints only when every instance on
the server has one. The kernel masks the url before it prints it. Put the product's own
advice, such as the persistence a server needs and what a restore costs, in the hint and
not in the kernel.

A driver that holds the `security` or `idempotency` atomic store and returns `None` is
named in a critical `backing servers` warning of the plan. Those stores must survive a
restore, so the plan says it cannot vouch for them.

A driver that keeps something for the whole process, such as a client every instance of it
shares, closes it in the classmethod `aclose_shared()`. The registry calls it once at
shutdown for every driver class an instance loaded, after the instances have closed. It
must be safe to call when nothing was built. The default does nothing.

A driver records metrics through `sandloom.driverkit.observe`:
`counter(name, documentation, labelnames)` and `histogram(...)` return metrics on the
kernel's registry. A driver imports no metrics registry and no settings of the kernel.

`aclose` may be called twice. The `details` of `health()` carry only non-secret facts,
such as the error type name, under keys that are single words too. Never put the exception
text, a URL or a path there, as a value or as a key.

`isolation_identity()` names the store behind the instance. It starts with the driver
name and a colon (`file:/var/mail.log`, `s3:<endpoint>/<bucket>`). Two instances on one
store return the same identity, whatever their instance names. It carries no credential.
The default, `None`, means unknown, and the kit fails it.

### 2. Publish it by name

The entry-point name is `<capability>.<driver>`, and its value is your class:

```toml title="pyproject.toml"
[project]
name = "sandloom-notify-file"
version = "0.1.0"
dependencies = ["sandloom>=0.7.0a16,<0.8"]

[project.entry-points."sandloom.drivers"]
"notification.file" = "sandloom_notify_file.driver:FileNotifier"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Add the package to the application's dependencies. For a package next to the application:

```bash
uv add --editable ../sandloom-notify-file
```

Sandloom imports the class only when `INFRA` selects its name. A package nothing selects
runs no code, and the order of installed packages does not matter.

Declare mandatory packages in `project.dependencies` and optional backend SDKs as
package extras. The driver class can advertise an extra with `required_imports`,
a tuple of top-level Python import names, and `install_hint`, the install command.
Both attributes default to empty. Import optional SDKs inside the constructor or
an operation to keep the entry point importable before the extra is installed.

For example, add `smtp = ["aiosmtplib"]` under `[project.optional-dependencies]`
in a package named `sandloom-notify-example`. Then declare
`required_imports = ("aiosmtplib",)` and
`install_hint = "pip install 'sandloom-notify-example[smtp]'"`.

During preflight, Sandloom looks up the declared import names without building
the driver. A missing declared import raises `SANDLOOM-B037` with the install
hint when `INFRA` selects the instance or a module requires it. If the entry
point itself fails to import, Sandloom raises `SANDLOOM-B022`.

An import error raised by `aopen()` follows the startup policy for open failures.
Strict startup aborts with an infrastructure startup error, and development startup
logs the failure. The [conformance step](#4-check-it-against-the-contract) exercises
the driver's operations against a test backend.

### 3. Select it with `INFRA`

Put this in `.env` for the application and in `.env.test` for its tests. Everything other
than `driver` becomes an option:

```bash
INFRA={"notification":{"default":{"driver":"file","path":"/tmp/myapp-mail.log"}}}
```

Module code does not change: `await notifier().send(...)` now reaches your driver. To keep
the default and add a second instance, use another name (`"audit"` instead of `"default"`)
and resolve it with `notifier("audit")`.

### 4. Check it against the contract

`sandloom.driverkit.testing` holds the conformance kits, one per capability
(`NotifierConformance`, `ObjectStoreConformance`). Subclass yours in the driver's tests and
override the `make_provider` fixture to return a new, unopened provider. Each obligation is
its own test, so a failure names what to fix. The kit needs `pytest` and `pytest-asyncio`.
It sends real messages: point it at a test backend.

A package that tests against a real backend sets one variable in the CI job that runs
those tests, `SANDLOOM_<PACKAGE>_REQUIRED=1`. Where its probe finds no backend, the test
calls `required_or_skip("SANDLOOM_<PACKAGE>_REQUIRED", reason)` from
`sandloom.driverkit.testing`, not `pytest.skip`: the call fails when the variable is `1`
and skips otherwise. A backend that did not start then turns the job red, not green with
every test skipped. Name what is missing in `reason` and leave any URL out of it.

State how far the kit has proven the driver with the class attribute `tier`.
The default is `experimental`: the driver implements the contract. A `beta` driver passes
its kit in CI against the real service. Past that, `stable` adds reconnect after an outage
and the auth options. The registry copies the tier into the driver's registration when it loads the
entry point. `sandloom doctor` and `/diagnostics/infra` show it.

In production an `experimental` driver is refused unless `SANDLOOM_ACCEPTED_RISKS` lists
`experimental_driver` ([SANDLOOM-B023](../reference/error-codes.md#sandloom-b023)).
A `beta` driver logs one startup warning.

```python title="tests/test_conformance.py"
--8<-- "docs/snippets/drivers/test_file_notifier.py"
```

Every kit runs the same harness checks first, named `test_harness_*`:

| Check | What the driver must do |
| --- | --- |
| lifecycle | `aopen()`, then `aclose()` twice without an error. |
| contract | Subclass the capability's provider class and have every operation. |
| version | Set `contract_version` to the integer major of the capability. |
| features | Declare only features the capability defines; `features()` equals `features_for(options)`. |
| hints | Name only the capability's operations in `performance_hints`. |
| health | Set `development_only` to a bool. `health()` answers within the bound (5 seconds), each `details` key is one token and each value a number, a bool or one token. |
| identity | Two providers from the same options, and one under another instance name, return the same `isolation_identity()`, starting with `<driver>:`. |
| features in use | A declared behaviour feature works; an undeclared one raises `Unsupported(feature)`. For `notification` each channel is such a feature. |
| errors | A provider built to fail raises only `InfraError` subclasses, and no message shows the kit's `leak_marker`. |

The capability's own tests follow. A skipped test means the check does not apply. The
driver may advertise no channel, or you did not give the kit what it needs. Read the skip
reason rather than treating it as a pass.

To check the error mapping and the health of a failing backend, override
`make_failing_provider` too. Return a provider whose backend fails, such as one pointing at
a closed port. Put the kit's `leak_marker` in its password, and the kit checks that
neither `health()` nor an error message shows it. The identity check builds a provider
under another instance name with your class's `from_config`; override
`make_renamed_provider(provider, name)` if that cannot rebuild it.

`ObjectStoreConformance` also reads `upload_limit_bytes` and `read_limit_bytes`, the
limits of your test instance. When the read limit is not below the upload limit, override
`place_oversized_object(provider, key, data)` to write the object to the backend directly.
`metadata` is informational. A driver that declares it overrides
`read_metadata(provider, key)` to read the content type and metadata back. A driver
without it accepts them in `put` and drops them.

#### An eventbus driver

!!! note "Kernel-facing"
    This section describes the contract the kernel's bus runs over. Its conformance kit,
    `EventTransportConformance`, exists. The Streams driver, in `sandloom-redis`
    (`sandloom_redis/eventbus/`), passes it, error mapping and outage checks included. A
    driver raises `Unavailable` and `Timeout` itself: the kernel does not translate a
    backend exception.

An `eventbus` driver of the transport shape states `contract_version = 2` and
subclasses `EventTransportProvider` (`sandloom.driverkit`), an abstract base class. It is
the durable transport the kernel builds its bus over. The kernel keeps schema validation,
payload limits and the bus logic. The driver keeps the entries.

It implements every operation below. A driver that leaves one out cannot be
instantiated; the operations behind a feature default to `Unsupported(feature)`, and a
driver that declares the feature overrides them. `publish_many` returns one id per item
(the bus refuses any other count).

- `publish` and `publish_many`. A failed batch raises `PartialPublish`, which carries
  the ids appended before the failure.
- `ensure_group`, `consume`, `ack` and `ack_many`.
- `dead_letter` and `never_dead_letter`.
- Group ownership. `claim_group_owner` raises `GroupOwnedElsewhere` for a second
  `(module, database)`.
- `retention()`, and the operations of the features below.

| Feature | Kind | Operations |
|---|---|---|
| `durable` | guarantee | entries survive a transport restart within its durability class and a consumer crash; delivery is at least once |
| `shared` | guarantee | every process of the deployment sees the same streams and groups |
| `blocking_consume` | behaviour | `consume(block_ms=)` waits server-side |
| `group_start_id` | behaviour | `ensure_group(start_id=)` other than `"0"` |
| `lease_renewal` | behaviour | `renew_pending`, `renew_interval_seconds` |
| `dlq_ops` | behaviour | `list_dlq`, `replay_dlq` |
| `inspection` | behaviour | `group_info`, `stream_info`, `oldest_unacked_age_seconds` |
| `inventory` | behaviour | `register_consumer`, `list_consumers`, `forget_consumer` |

`publish_many` and `ack_many` say `one_round_trip` in `performance_hints` when each is one
round trip.

**The kit.** Subclass `EventTransportConformance` (`sandloom.driverkit.testing`) and
override `make_provider`. The harness checks the contract and the features first. A
declared feature's call must not raise `Unsupported`, and an undeclared one's must. The
kit then runs the core groups. They cover delivery, the dead-letter queue, ordered
batches, group ownership and retention. It runs one more group for each behaviour
feature you declare. The `shared` group checks that two providers of one configuration
see the same streams.

- Build the provider with `max_deliveries` equal to `EventTransportConformance.max_deliveries`
  and no retry backoff beyond a few milliseconds.
- With `lease_renewal`, build it with a reclaim idle time of about a second. The kit
  renews for five intervals and expects the entry to stay with its consumer, then to be
  reclaimed once renewal stops.
- Override `refused_stream(provider, stream)` and `failing_dlq_append(provider)` to make
  the backend fail. Each returns a context that holds the failure while it is open.
  `failing_dlq_append` covers an explicit `dead_letter` and the dead-lettering that
  `max_deliveries` triggers. In both the source entry must stay unacked until the
  dead-letter append succeeds.
- Override `retention_cases()` to give `(options, retention)` pairs. Override
  `make_provider_with_options(provider, options)` when `from_config` cannot rebuild the
  provider with other options. The kit gives `feature_calls(provider)`, one call per
  behaviour feature. Override it only when one of those calls does not suit the driver.
- Override `parked_ids(provider, stream)` when the driver has no `dlq_ops`. It returns the
  event ids parked in the dead-letter queue. With neither, the checks that an entry is
  parked are skipped, because a parked entry and a dropped one cannot be told apart.
- Override `interrupt_connections(provider)` to cut the provider's connections. The
  outage group, required of a stable-tier driver, then expects `consume` to raise
  `Unavailable` and consuming to resume without loss. Build the provider so that it does
  not retry the cut call silently.

A check without its hook is skipped, never passed. A stable-tier driver sets
`require_stable_hooks = True`, and then a missing hook fails the check. Such a driver
gives every hook above, or sets the test of a check it cannot run to `None` in its own
class, on the record.

The names of the previous major are gone, with no alias. A module or driver that names
`publish`, `subscribe`, `volatile`, `at_least_once` or `consumer_groups` is refused at
load, and the refusal names the replacement. The last two become `durable`; the first
three are dropped. The `memory` and `broadcast` buses are the kernel's own volatile buses,
not a driver shape.

#### A taskqueue driver's worker host

!!! warning "Kernel-facing"
    A `taskqueue` driver is not open to packages yet. Its conformance kit,
    `TaskQueueConformance`, covers `send` and the host. This section is the contract the
    kernel runs a worker over.

A `taskqueue` driver (`TaskQueueProvider`, `contract_version = 2`) implements `send` and
`worker_host()`. The host is a `WorkerHost` (`sandloom.driverkit`): the process that
runs the messages and the scheduler. It is **not a capability**. It has no `INFRA` entry
and no registration, and it is versioned with `taskqueue`. `worker_host()` is
synchronous and cheap: it builds the host over what the instance holds and connects to
nothing.

The kernel gives the host a `WorkerBoot` and calls nothing else of the driver:

| `WorkerBoot` | What the host does with it |
|---|---|
| `boot()` | once, in the main process, before it forks or consumes: the kernel loads the composition, runs the startup checks and answers the `TaskTable` (tasks with their queue and `at_least_once`, periodic entries) to register |
| `before_fork()` | once after `boot()`, right before the host forks processes that run messages (the kernel freezes its startup heap) |
| `run(task, payload, task_id=)` | for each message, outside any running loop: an `async def` body runs on the process's one persistent loop, a sync body directly; an exception propagates to the host's retry and redelivery rules |
| `on_process_start()` / `on_process_stop()` | in each process that runs messages: a fresh loop after a fork; dispose the engines on the loop that opened them |
| `run_coroutine(coro)` | on the same persistent loop, for the host's own async calls (the lease) |

The host answers three calls:

- `run_worker(boot, queues=, concurrency=, loglevel=)` blocks and returns the process
  exit code (a worker that died must not return 0). The kernel resolves `concurrency` from the
  CPU quota. The process model is not an argument. Keep it as an option of your driver, and
  raise `InvalidArgument` before consuming for a configuration you cannot run. A host that
  runs the messages in its own process starts and stops it itself
  (`boot.on_process_start()` / `on_process_stop()`). No child announces itself there, and
  `before_fork()` is not called. If the host runs messages inside the loop that fires its
  liveness timer, it touches the liveness file from a thread of its own.
- `run_scheduler(boot, lease=, lease_name=, lease_ttl_seconds=, loglevel=, state_file=)`
  sends each periodic task at its cron slots, and only while the process holds
  `lease_name` on `lease` (a `lease` store). Wrap the store in `FencedLease`
  (`sandloom.driverkit`): it keeps the renew cadence and the local deadline, and logs the
  standby. The kernel resolves the store, the name (broker, instance and module set) and
  the production check, so a second scheduler stands by.
- `check(timeout_seconds=)` answers whether a worker runs, without booting anything.
  Its details name no broker address or credential.

`TaskQueueConformance` has send groups and host groups. Send groups call `send` against a
throw-away backend. Host groups run `worker_host()` for real, one forked process per
host, on a backend the processes share. Each host gets a `WorkerBoot` that reports every
call and every run through a pipe. A platform without `fork` skips them. A driver whose
`worker_host()` answers `None` or raises `NotImplementedError` fails, because the method
is abstract. Set these fixtures:

- `make_provider` builds an unopened provider for the send groups. A `memory://` broker
  is enough there.
- `make_host_provider` builds a provider on a backend the forked hosts and the test
  share. It defaults to `make_provider`, so override it when that one is not shared.
- `make_lease_store` builds an unopened shared `lease` store. A driver that declares
  `periodic` must override it: the scheduler groups fail without it.
- `scheduler_clock` is optional. It freezes the driver's clock shortly before a cron slot
  and returns `advance`, so the scheduler groups do not wait for a real minute.

The scheduler groups run two schedulers on one lease. One sends a slot once while the
other stands by. After the first stops, the second takes over inside the same slot and
sends nothing for it.

A driver may keep an importable object for its own tools (the Celery driver's `app`, for
`celery inspect` and Flower). It documents that object itself: it is the driver's
interface, not part of the port. The `raw_backend()` escape hatch of earlier versions is
gone, and no `native()` replaces it.

#### A database driver

A `database` driver states `contract_version = 2` and implements
`DatabaseProvider` (`sandloom.core.infra.ports.database`, with the stores it
returns and `ExecutionAdapter`):

- `uow_factory()` makes the units. A unit's `session` is what the stores take,
  typed `UnitSession` (`sandloom.driverkit`: `flush()` and `in_transaction()`); your store narrows it
  to the concrete session of your engine.
- `isolation_identity()` names the store the options point at, never its
  credentials: a hash of its host, port and database, or of a file path. Two options naming one store must agree, and two
  stores must differ.
- `planner_adapter(*, contract=False)` returns the `ExecutionAdapter` that runs
  declared operations, or `None` (the default) when the database cannot. With
  `contract=True` it is the adapter on a dedicated contract pool, if you keep one.
- `session_factory` (optional) answers a callable that yields a raw session of
  this instance; the default is `None`.

`DatabaseProviderConformance` checks each of these, and that an event enqueued in
a unit that rolls back is never written (when `transactional_outbox` is declared).
Override `prepare_outbox_schema(provider)` if your schema comes from migrations
instead of `ensure_model_schema()`.

A database driver for a new SQL database also ships a **SQL dialect renderer**. It
writes the portable forms of `sandloom.sdk.data` in that database's SQL
([Database](database.md#portable-sql-forms)).

1. Implement `SqlDialect` from `sandloom.driverkit`, with `contract_version = 2` and the
   SQLAlchemy dialect `name`.
2. Give the oldest server it supports in `minimum_server_version`.
3. Return the features the database can give from `features()`. Leave out
   `concurrent_writers`: the kernel derives it from the `serial_writers` fact (a
   renderer without that fact has it), and a renderer listing it is refused.
4. Map the database's errors to kinds in `classify_error`.
5. Return a `RowSecurity` from `row_security()` when the database can confine rows
   to the tenant a transaction binds (the setting `TENANT_SETTING`), or `None`. It
   writes the policies and binds the tenant; its queries also check the roles at startup. With
   `None`, `tenant_rls` is refused
   ([SANDLOOM-B026](../reference/error-codes.md#sandloom-b026)) and tenant isolation
   there is application-enforced.
6. Say how a unit begins: `begin_statement(write=, read_only=, snapshot=)` returns
   the `BEGIN` text, or `None` for the plain one, and `transaction_options(read_only=,
   snapshot=)` the SQLAlchemy execution options to the same effect. Declare
   `snapshot_reads` and `readonly_transactions` only when they give one snapshot and
   refuse writes. Their units then need options of their own. Without
   `snapshot_reads` a one-snapshot scope is refused
   ([SANDLOOM-B027](../reference/error-codes.md#sandloom-b027)).
7. Return a `LockStatement(statement, parameters)` from `lock_statement` and
   `unlock_statement`, or `None` where the database has no such lock. Build the
   statement once per key shape and put the key in `parameters`: the kernel
   takes a lock per request and runs `execute(lock.statement, lock.parameters)`.
   A database whose writers are serial may declare `transaction_locks` with no
   lock statement. Its driver marks a transaction that holds the write lock:
   `WRITE_LOCK_HELD` in the connection's `info`. SQLite's `BEGIN IMMEDIATE`
   does so. The kernel's locks then succeed only in such a transaction.
8. Four renderer facts are optional, and all default to `False`. If the database runs no
   migration chain, set `schema_from_models = True`. The app
   then builds its schema with `create_all` at startup, and `sandloom migrate` refuses it
   ([SANDLOOM-B028](../reference/error-codes.md#sandloom-b028)). If it serves only
   development and tests, set `development_only = True`; production then refuses it
   without an accepted risk
   ([SANDLOOM-B029](../reference/error-codes.md#sandloom-b029)). If it has one
   writer at a time, set `serial_writers = True`. A nested write unit then
   commits with its caller
   ([Nested units](database.md#nested-units-commit-with-their-caller)). Without that
   fact the database has `concurrent_writers`, which race tests ask for. Set
   `aware_times = True` if `DateTime(timezone=True)` keeps the instant and the
   DBAPI reads it back aware. `UtcDateTime` then converts nothing and only
   refuses a naive time. The kit's `read_aware_time` must show it on the database.
   Optionally write the read planner's literal prefix filter, both members or
   neither: `prefix_pattern(prefix, ignore_case=)` answers the value to bind,
   and `prefix_match(column, pattern, ignore_case=)` the condition. Every
   character of the prefix matches itself, and only `ignore_case` folds case.
   Without them the planner writes `LIKE` / `ILIKE` with a backslash `ESCAPE`.
9. Write a feature form only when the database has it, and declare its feature
   then ([Database](database.md#forms-that-need-a-feature)):
   - `arrays`: `array_type(item_type)`;
   - `fulltext_search`: `fulltext_index_options()`, `fulltext_document(columns,
     language)` and `fulltext_match(document, query, language)`. Write the
     document's constants inline, so a query's expression is the index's;
   - `json_path`: `json_path(column, path)`, the scalar's JSON text as
     stored. A string reads unquoted, a number as written (`1.50`, `1e2`),
     never in the database's float text;
   - `json_contains`: `json_contains(column, value)`;
   - `list_aggregates`: `aggregate_list(value)`, the aggregate of a group's
     values, and `aggregate_list_type(item_type)`, the type its result is read
     through. That type reads a list (its `python_type` is `list`), each item
     as `item_type` reads its column. Both built-in renderers declare it, and
     identity needs it.

   A renderer that declares
   one of these features without its members is refused when it loads
   ([SANDLOOM-B025](../reference/error-codes.md#sandloom-b025)). Without the
   feature, leave the members out: the form refuses
   ([SANDLOOM-R015](../reference/error-codes.md#sandloom-r015)).
10. Name the renderer in the `sandloom.sql_dialects` entry-point group under the dialect
    name, and register the driver with `sql_dialects={"<name>"}`.

Check it with `SqlDialectConformance` from the same kit: set `renderer` and `dialect`. A
form the database cannot write returns `None`, and the kit checks that it is refused.
The kit compiles each feature form the renderer declares. To run `json_path`
on your database, override the kit's `read_json_paths(document, paths)`. It
stores `JSON_PATH_DOCUMENT` and answers what `json_path` reads at each path.
The kit checks each answer against `JSON_PATH_READS`, numbers included.
A renderer that declares `json_path` fails the kit without the override. Run
the other forms on the real database in your own tests too.

#### Add a DBAPI adapter

A new DBAPI for a database Sandloom already renders needs no renderer. It works
on the plain path as it is. Add a DBAPI adapter only if it needs the speed
([Database](database.md#the-dbapi-adapter)):

1. Subclass `DbapiAdapter` from `sandloom.driverkit`. Set `name` to SQLAlchemy's
   `dialect.driver` for the DBAPI. Keep `contract_version = 1`: another major is
   refused ([SANDLOOM-B025](../reference/error-codes.md#sandloom-b025)).
2. Set `capabilities`. A driver package may claim only `read_only_policy` and
   `reconnect_backoff`. The other three drive asyncpg's API and SQLAlchemy's
   private state, so only the kernel's own adapters claim them (B025).
3. Override what those capabilities use. `connect_args` and `server_settings`
   carry the timeouts, and `connection_lost` and `refuses_connections` classify
   connection errors.
4. Override the pool's probes if the DBAPI has cheaper ones. `ping` checks an
   idle pooled connection. `connection_closed` spots a dead one without a round
   trip.
5. Override `prepare_engine` for setup on each new engine. The aiosqlite adapter
   uses it to emit every unit's `BEGIN` itself.
   `config_problem(url)` refuses a database the DBAPI cannot open, before
   anything starts. Name the error code in its message.
6. Call `register_dbapi_adapter(adapter)` when the driver package loads, before
   the first engine is built. Your adapter replaces the one Sandloom ships for
   the same DBAPI, and the shipped one never replaces yours.

Every member you do not override keeps the plain answer. The plain
`connection_lost` asks the renderer, so a lost connection is a 503 on any
DBAPI that reports the server's error code. A DBAPI with no
`read_only_policy` on a database without `readonly_transactions` refuses
`DB_READ_ONLY_SESSIONS`
([SANDLOOM-B031](../reference/error-codes.md#sandloom-b031)).

### 5. Prove the application uses it

A passing application test does not show the driver was selected: the default `log` driver
also accepts every `send`. Assert on the effect. With `INFRA` in `.env.test`, a module test
can check it:

```python
from pathlib import Path

from sandloom.sdk.infra import Notification, notifier  # sandloom: advanced infra


async def test_mail_goes_through_the_file_driver(client) -> None:
    await notifier().send(
        Notification(channel="email", recipient="a@b.test", subject="Hi", body="x")
    )
    assert "a@b.test" in Path("/tmp/myapp-mail.log").read_text()
```

### 6. Isolate the backend in a parallel test run

A backend that parallel test workers would share (a Redis database, a broker) needs its half of
the harness's isolation: a *harness family*. The package registers it in the `sandloom.testing`
entry-point group (`"<name>" = "package.testing.family:FAMILY"`) and builds it from
`sandloom.driverkit.testing_family`. `sandloom-redis` is the model. The hooks, the refusals and
what the family module must not import at load time are in
[Harness families](../modules/testing.md#harness-families-a-backends-half-of-the-parallel-run-isolation).
A driver whose backend is a database file or an in-process store needs none.

## Verify

```bash
uv run sandloom test
```

On a running application, `/diagnostics/infra` lists the installed driver packages and the
driver of each instance. It needs `INTERNAL_API_TOKEN`
([Operator reference](../operate/operator-reference.md)).

## Errors you may hit

| Error | Cause and fix |
| --- | --- |
| [SANDLOOM-B021](../reference/error-codes.md#sandloom-b021) | The selected name has two providers, or a package published a built-in's name. Uninstall one, or rename your driver. |
| [SANDLOOM-B022](../reference/error-codes.md#sandloom-b022) | The selected driver failed to import, is not a subclass of the contract, has no integer `contract_version`, or was written for another major version. It may also declare invalid dependency metadata, or be a `development_only` driver selected in production. The message names the problem. |
| [SANDLOOM-B037](../reference/error-codes.md#sandloom-b037) | A declared import for a selected or required driver is missing. Follow the install hint or select another installed driver in `INFRA`. |
| `Configured infrastructure instances have no registered driver` | `INFRA` names a driver no installed package publishes. |
| `ProviderContractError` | An instance reports other features than its class declares. |
| `FeatureUnsupportedError` | A module requires a feature the driver does not advertise. |
| [SANDLOOM-B033](../reference/error-codes.md#sandloom-b033) | A module uses a `sandloom.sdk.data` form, or declares a database feature, that the instance's renderer does not declare. |
| `Input should be 'log' or 'smtp'` | A driver package's name was set in `NOTIFIER_BACKEND`. Use `INFRA`. |

## Related

- [Infrastructure](index.md): interfaces, drivers and instances.
- [Add your own capability](own-capability.md): a new kind of infrastructure.
- [`sandloom.driverkit`](driverkit.md): every name it exports.
- [Customize a standard module](../modules/batteries/customize.md): what a notification driver can do for identity's mail.
- [ADR-0011](../adr/0011-driver-api.md): why it works this way.
