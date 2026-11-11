# Research: how a module declares and gets an infrastructure capability

Intended path: `design/kernel-boundary/research-infra-deps.md`. This copy is in the
scratchpad because the session's tm areas do not cover that path; see the hand-back.

- **Status:** research for the owner, 2026-11-08. Not a decision.
- **Branch read:** `arch/kernel-boundary` at `cb98d65`; the KB-04 matrix read from
  `bbdb2ea` (PR #88, on `origin/arch/kernel-boundary`).
- **Trigger:** KB-04 (`tests/integration/module_subsets/_module_subset_matrix.py`): on plain
  `sandloom` with module wheels (P2), the 12 subsets that contain identity or billing boot,
  `/health` 200, `/ready` 503 `taskqueue_status: fail`. I-19 expects "ready, or a refusal for a
  predefined reason".
- **Scope:** the `taskqueue` capability first; then cache, event bus, object store,
  notification.
- **Method:** code read at the lines cited; three behaviours checked by running the repository
  environment with `redis`, `celery` and `kombu` imports blocked (section 1.7); prior art from
  the URLs in section 2.

## 0. The answer in brief

1. The mechanism exists and is incomplete in use. A module declares infrastructure needs in
   `define_module(capabilities=[...])` as `CapabilityRequirement`s, and the loader preflights
   them build-free at startup. Identity and billing ship tasks and do not declare `taskqueue`.
2. Declaring `taskqueue` today changes nothing on plain `sandloom`. The `celery` driver is
   registered in core whether Celery is installed or not, and the preflight checks only driver
   metadata. Checked: `validate_requirement("taskqueue")` passes with Celery absent.
3. **Recommendation, in one line:** complete the existing capability mechanism, with no new
   subsystem.
   - A module with tasks requires `taskqueue`, declared and also derived from its `tasks.py`.
     A module with periodic tasks requires the new feature `periodic`.
   - The registry learns whether a driver's package is installed (`find_spec`, no import, no
     I/O).
   - An unmet required capability refuses startup with one new boot code naming the module,
     the capability, the driver and the fix.
   - Readiness keeps only the transient case.
   - Module wheels stay on plain `sandloom`. A composition profile (`sandloom[standard]`)
     brings Celery.
4. **Be honest about `taskqueue.v1`:** it covers enqueue only. Execution, periodic scheduling
   and redelivery are Celery today (`core/worker.py:104-121` refuses any other driver). A
   driver that can only enqueue is not compatible with billing. The feature `periodic` says
   so, and only Celery advertises it until a conformance kit exists.

## 1. What exists (facts)

### 1.1 Capabilities, drivers, configuration

- **The capability.** `taskqueue` is a versioned `CapabilitySpec`: `v1`, contract
  `TaskQueueProvider`, features `delay` and `idempotency_key` (`core/infra/capabilities.py:199-204`).
  It is in `BUILTIN_CAPABILITIES` with database, cache, objectstore, eventbus and notification
  (`capabilities.py:240-247`).
- **The contract is one operation:** `enqueue(task, payload, *, delay_seconds, idempotency_key)
  -> str` (`core/infra/contracts.py:1138-1166`). `raw_backend()` is declared "NOT part of the
  portable contract" (`contracts.py:1168-1177`). The provider's default `health()` returns `ok`
  (`contracts.py:592-594`), and the Celery driver does not override it
  (`core/drivers/celery_taskqueue.py`, no `health`).
- **A `default` instance is synthesized for every capability**
  (`DEFAULTED_CAPABILITIES`, `core/infra/config.py:43-50`):
  - taskqueue: driver `celery`, broker `BROKER_URL` (`config.py:127-137`);
  - cache: driver `redis` (`config.py:111-118`);
  - eventbus: `auto` resolves to `streams` in prod and `memory` elsewhere (`config.py:70-85`).

  `INFRA` overlays or adds instances (`config.py:157-201`). An instance does not record whether
  it was synthesized or configured by the operator.
- **Built-in drivers are registered unconditionally.**
  - `celery` for taskqueue: `core/drivers/bootstrap.py:104-110`. Celery itself is imported
    only when the provider is built (`celery_taskqueue.py:84-88`, `:224-226`).
  - `redis` for cache: imported lazily (`core/drivers/redis_cache.py:99-175`).
  - Both packages are extras of `sandloom` (`packages/sandloom/pyproject.toml`, extras `redis`,
    `celery`).
- **Driver packages** are found by name in the `sandloom.drivers` entry-point group and
  imported only when `INFRA` selects them (`core/infra/registry.py:960-1024`, ADR-0011
  decision 2).
  - A selected driver that fails to import is SANDLOOM-B022 (`registry.py:919-927`;
    `core/error_codes.py:969-982`).
  - A built-in driver whose package is missing never reaches that path: it is already
    registered.
- **Startup validation of instances** (`registry.validate`, `registry.py:731-766`) checks that
  each configured instance has a registered driver and valid options. It does not check that
  the driver's package is installed.
- **`aopen_all`** builds and opens every configured instance except the event bus
  (`registry.py:799-852`), with `strict=settings.is_prod` (`core/app_factory/lifecycle.py:124`).
  In production a failed build aborts startup.

### 1.2 What a module can declare, and what the kernel does with it

- **The requirement type.** `CapabilityRequirement(capability, instance="default",
  version="v1", features=frozenset(), required=True)` (`core/descriptors/__init__.py:56-72`).
  It is carried in `ModuleDescriptor.capabilities` (`:123-125`) and read through
  `effective_capabilities()`, which today returns the declared list (`:155-157`).
- **How modules declare it.** `define_module(capabilities=[...])` turns strings into
  requirements (`sdk/_decl/module.py:577-580`; L2 `core/module_api/_define.py:177-180`).
- **The preflight**, `_preflight_capabilities` (`core/loader/plugin_loader.py:411-455`), is
  called inside the module's registration transaction (`:561`, `:639`). For each requirement
  it calls `registry.validate_requirement` (`registry.py:576-642`), which checks:
  - the spec exists and the major version matches;
  - the instance is configured;
  - the driver is registered;
  - the requested features are known to the spec and declared by the driver.

  It builds nothing. On a failure:
  - a required requirement raises a `CapabilityError` subclass, the module fails to load, and
    startup aborts if the module is required;
  - an optional one logs a warning (`plugin_loader.py:447-455`).

  No code maps these errors: `EXCEPTION_CODES` (`error_codes.py:1789-1818`) has no entry for
  `InstanceNotConfiguredError`, `DriverNotRegisteredError` or `FeatureUnsupportedError`.
  They surface as `ModuleLoadError`, SANDLOOM-B001, which the KB-04 matrix treats as "an
  undeclared dependency" (never allowed).
- **Precedent 1: derive and declare, for database features** (ADR-0011 amendment,
  decision 6):
  - a feature is derived from the module's tables (`module_feature_uses`) and from declared
    requirements, and checked at load: B033 (`plugin_loader.py:458-500`);
  - `sandloom check` requires the declaration: C030 (`error_codes.py:592-620`);
  - a statement compiled without the feature refuses at run time: R015.
- **Precedent 2: derived contract requirements.** Discovery derives contract requirements from
  module-level `Contract(REF)` and merges them with declared `consumes`
  (`sdk/_decl/module.py:320`, `:575`). A required contract nobody provides or binds is B005.
- **The module snapshot** (`module.snapshot.json`) records `consumes` and not `capabilities`
  (checked on `src/sandloom_billing/module.snapshot.json`: keys `consumers, consumes, contracts,
  events, format, module, projections, sagas`).

### 1.3 What the bundled modules declare and use

| module | declares (`capabilities=`) | uses, by code |
|---|---|---|
| identity | `database` (features `transaction_locks`, `list_aggregates`), `cache`, `eventbus` (`src/sandloom_identity/__init__.py:82-97`) | `cache()` ×11; `notifier()` with `channel="email"` (`integrations.py:129-132`); `tasks.py` with 4 tasks and `setup_periodic_tasks` (`tasks.py:37-118`) |
| billing | `database`, `eventbus` (`src/sandloom_billing/__init__.py:76-79`) | `tasks.py`: `reconcile_operation_intents`, `acks_late=True, reject_on_worker_lost=True`, every 300 s (`tasks.py:51-67`) |
| tenancy | `database`, `eventbus` (`src/sandloom_tenancy/__init__.py:80-83`) | `notifier()` with `channel="email"` (`services/invitation_service.py:315-318`) |
| admin | none (`src/sandloom_admin/__init__.py:15`) | none of these |

- Neither identity nor billing enqueues a task from a request or a consumer. A search for
  `.delay(`, `apply_async`, `enqueue(` and `taskqueue()` in the four modules (tests excluded)
  finds only `.s()` inside the beat hooks. Their tasks are periodic only.
- Identity and tenancy send email through `notifier()` without declaring
  `CapabilityRequirement("notification", features={"email"})`. The log notifier advertises no
  channel in production, precisely so that such a declaration fails the preflight
  (`core/drivers/log_notifier.py:9-13`, `:45-54`). Without the declaration that protection does
  not apply: a production deployment left on the default `log` notifier
  (`core/config/services.py:60`) would log password-reset and invitation mails that nobody
  receives. This is inferred from the code and not run. It is the same defect as `taskqueue`:
  an incomplete declaration.

### 1.4 The task path: what is replaceable and what is Celery

Path: producer → broker → worker → scheduler → retry and recovery.

| step | today | portable through `taskqueue.v1`? |
|---|---|---|
| define a task | `sandloom.sdk.tasks.shared_task`, a typed wrapper of `celery.shared_task` (`sdk/_prim/tasks.py:121-165`); options `acks_late`, `reject_on_worker_lost` passed to Celery | no: Celery's decorator and options |
| enqueue | `Task.delay` / `apply_async` / `.s()` are Celery's. Without Celery, `_UnqueuedTask` refuses them all with `TaskQueueNotInstalled` (`sdk/_prim/tasks.py:79-117`), including where another taskqueue driver is configured. `taskqueue().enqueue(name, payload)` goes through the capability (`sdk/infra.py`, `core/di`) | only `taskqueue().enqueue` |
| enqueue dedupe | `enqueue_once` in the cache, reached from the Celery driver (`celery_taskqueue.py:131-152`, `core/tasks/enqueue.py:63`) | contract-level (feature `idempotency_key`) |
| broker | Celery and kombu | driver-internal |
| worker | `celery -A sandloom.core.worker`. It takes the app from `core.taskqueue(instance).raw_backend()` and refuses a backend that is not a Celery app (`core/worker.py:104-121`; guarantee C-WORKER-02, `guarantees.yml:6695-6702`). It imports `<pkg>.tasks` for loaded modules (`worker.py:126`) | no |
| periodic scheduling | the module's `setup_periodic_tasks(sender)` calls `sender.add_periodic_task(..., task.s())`, connected to `on_after_finalize` (`worker.py:370`). `cron()` returns a Celery `crontab` (`sdk/_prim/tasks.py:168-203`). One beat per broker and module set through a fenced cache lease (`core/beat_lease.py:1-30`, G-OPS-01) | no: Celery beat's API in module code |
| retry and recovery | `acks_late` off globally, prefetch 1 (`worker.py:215-234`); a task opts into at-least-once with `acks_late` + `reject_on_worker_lost` | no: Celery semantics |
| deploy check | SANDLOOM-T010: a module with periodic tasks needs one beat per deploy profile (`error_codes.py:1688-1702`), read by AST (`core/tasks/periodic.py:19-37`) | static, Celery-named |

- `sandloom.driverkit` exports `InfraProvider` and `NotifierProvider` only
  (`src/sandloom/driverkit/__init__.py:20`, `:35-49`). ADR-0011 decision 6 keeps `taskqueue`
  private "until [its kit] exists".
- **Conclusion:** the capability lets an operator swap the enqueue side by `INFRA`. Running
  module tasks, scheduling them, and recovering after a lost worker are not replaceable today
  and are not specified by `taskqueue.v1`.

### 1.5 What billing and identity need from a queue

- **Billing.** `reconcile_operation_intents` closes payment operations no request will close
  (`src/sandloom_billing/services/intent_reconciler.py:1-55`):
  - stale `pending` intents, whose PSP session may exist and be payable by whoever holds its
    URL;
  - `revoked` intents whose expiry compensation failed;
  - it also escalates to an operator after the configured number of attempts (`followup_at`).

  It is idempotent: an overlapping run costs one extra attempt (`:47-51`). What it needs:
  - **periodic invocation at a bounded interval in at least one process while the deployment
    runs.** Without it, these intents are never closed or escalated; that is a correctness
    loss, not cosmetic;
  - it does not need exactly-once or a single scheduler: duplicates are tolerated;
  - redelivery after a lost worker helps, but the next tick re-scans the table, so a lost run
    heals at the next period. The guarantee is "the schedule runs", not "this message is
    redelivered".

  A driver that can only `enqueue` cannot provide this.
- **Identity.** The two scheduled tasks delete expired refresh tokens and tokens revoked more
  than 30 days ago (`src/sandloom_identity/tasks.py:37-75`, `:104-118`). The other two tasks
  are placeholders and are not scheduled (`:78-101`, `:117-118`).
  - Validity is checked on every use: a revoked or expired token is refused at refresh
    (`services/token_service.py:259-262`).
  - So the cleanup is storage hygiene: without it the `refresh_tokens` table grows, and no
    security or correctness guarantee is lost.

  Identity's queue need is optional by this reading; the owner decides (open question 1).

### 1.6 Readiness today (KB-02)

- `/ready` adds a taskqueue term while a loaded module has a `<pkg>.tasks` submodule, found
  with `find_spec`, not from a declaration (`core/app_factory/ops.py:125-139`). It probes the
  `default` instance by building it (`:143-157`, `:160-205`, `:292-345`). Guarantee G-OPS-23
  (`guarantees.yml:4903-4915`).
- Because the Celery provider's `health()` is the default `ok`, the term fails only when the
  provider cannot be built, that is when Celery is not installed. A broker outage does not fail
  it. So today's readiness term detects a permanent installation fact, not a transient one.
- `/health` always probes `db`, `cache`, `storage` and `eventbus`, whether or not anything uses
  them (`core/app_factory/lifecycle.py:189-209`). `sandloom doctor` probes every configured
  instance (`core/cli/doctor.py:286-308`).

### 1.7 Checked by running (repository venv, `redis`, `celery`, `kombu` imports blocked)

Script: a `sys.meta_path` finder that raises `ModuleNotFoundError` for those three names,
then `build_infra_registry(...)` and `create_app()` with `SANDLOOM_MODULES=[]`.

1. `registry.validate_requirement("taskqueue")`, also with `features={"delay"}`, and
   `validate_requirement("cache")` all **pass**. Celery is never imported. So adding
   `"taskqueue"` to billing's `capabilities=` alone would not change the KB-04 outcome.
2. Probes inside the lifespan:
   - `cache` **fail**, `No module named 'redis'`;
   - `taskqueue` **fail**, `No module named 'celery'`;
   - `eventbus` ok (memory) and `storage` ok (local).

   `aopen_all` (dev, non-strict) logged the Celery build failure as a warning.
3. Consequences, inferred from these results and from `registry.py:799-852` (not run):
   - **P1 in prod.** In production on plain `sandloom`, `aopen_all(strict=True)` builds the
     synthesized `taskqueue/default`, and Celery is missing, so startup aborts with
     `InfraStartupError`, even with no module that has tasks.
   - **Hidden by stubs.** In any environment on plain `sandloom`, `/health` fails on `cache`.
     The KB-04 matrix and the lean-install check replace every `core.health_checks` entry with a
     passing probe (`_module_subset_matrix.py`, `BOOT_PROBE`;
     `tools/packaging/check_package_distributions.py:492-493`). Their "ready" outcomes for the
     subsets without tasks hold only with stubbed infrastructure probes. The taskqueue term is
     not stubbed because it is not a `health_checks` entry; that is why it alone shows up.

### 1.8 How the other capabilities stand

| capability | built-in drivers | zero-dependency driver? | dev driver refused in prod? |
|---|---|---|---|
| database | `sqlalchemy` (asyncpg, base dependency); SQLite with the `sqlite` extra | PostgreSQL client in base | SQLite: `development_database_in_prod` (`core/config/accepted_risks.py`, B029) |
| eventbus | `memory`, `redis`, `streams` | `memory` | `volatile_event_bus` |
| objectstore | `local` | yes | `local_storage_in_prod` |
| notification | `log`, `smtp` | yes | `log` advertises no channel in prod unless `log_notifier_in_prod` |
| cache | `redis` | **no** (an `InMemoryCache` exists only in the test harness, `core/testing/_uow.py:272`) | none |
| taskqueue | `celery` | **no** | none |

Cache and taskqueue are the only capabilities whose only built-in driver needs an extra. The
kernel itself uses the cache: token revocation, user blocks, rate limits, idempotency and the
beat lease (`core/auth/revocation.py`, `core/auth/blocks.py`, `core/ingress/rate_limit.py`,
`core/idempotency/cache_store.py`, `core/beat_lease.py`). So the cache gap is a kernel gap; the
taskqueue gap is a module gap.

### 1.9 Where the gap is, precisely

1. Incomplete declarations: identity and billing omit `taskqueue`; identity and tenancy omit
   `notification`/`email`.
2. The registry cannot tell a registered driver from a usable one: a built-in driver's package
   may be absent.
3. Synthesized defaults are treated as if the operator had selected them: probed, opened
   (strictly in prod), listed by doctor, whether or not anything requires them.
4. An unmet module capability has no code (B001), so it cannot be a "predefined reason" in I-19.
5. Readiness carries a static fact (package absent) that belongs to startup.
6. `taskqueue.v1` does not state the guarantees modules rely on (periodic, redelivery), so
   "compatible driver" has no definition.

## 2. Prior art

| system | consumer declares | provider chosen by | missing provider | default | fits Sandloom? |
|---|---|---|---|---|---|
| OSGi Require/Provide-Capability ([Core R8 §3.3.6](https://docs.osgi.org/specification/osgi.core/8.0.0/framework.module.html)) | an abstract namespace plus a filter on attributes; `resolution:=optional` | whatever installed bundle provides a matching capability | "a mandatory requirement forbids the bundle to resolve" | none | yes: requirement = capability + features (attribute filter), required or optional, checked before activation. Sandloom's `CapabilityRequirement` is this model already |
| Spring Boot starters and conditions ([auto-configuration](https://docs.spring.io/spring-boot/reference/features/developing-auto-configuration.html)) | an injection point of an interface type | auto-configuration under `@ConditionalOnClass` (class on the classpath, read without loading) and `@ConditionalOnMissingBean` | context fails to start with an unsatisfied dependency | a starter, "really an empty jar", brings a default provider | `@ConditionalOnClass` = check availability without importing (`find_spec`); a starter = a composition profile (`sandloom[standard]`), not a dependency of the library that consumes the interface |
| Django 6 tasks ([docs](https://docs.djangoproject.com/en/6.0/topics/tasks/)) | `@task`; `enqueue()` | `TASKS` setting | n/a | `ImmediateBackend` runs tasks immediately; built-in backends "are for development and testing only"; Django "does not provide a worker" | a framework may define the enqueue API and leave the worker to the backend; Django's default trades production safety for a working first run |
| django-tasks DatabaseBackend ([PyPI](https://pypi.org/project/django-tasks/0.2/)) | same | `TASKS` | n/a | a database queue with a `db_worker` command | the database-backed option, in a separate package |
| Rails Active Job ([guide](https://guides.rubyonrails.org/active_job_basics.html)) | a job class | `config.active_job.queue_adapter` per environment | n/a | Solid Queue (database) "the default queue backend for Rails version 8.0 onwards"; `async` in development, where "all outstanding jobs are lost" on a crash | per-environment default with honest loss semantics; a database queue as the production default |
| Laravel queues ([docs](https://laravel.com/docs/12.x/queues)) | `dispatch()` | `QUEUE_CONNECTION` | n/a | `database` in new apps; `sync` runs jobs immediately "for use during development or testing"; Redis needs `predis` or phpredis, SQS the AWS SDK | the vendor driver's library is installed with the driver choice, not by the job's package |
| Celery `shared_task` ([docs](https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html)) | a task in a reusable app with no app instance | the project's Celery app | n/a | n/a | Sandloom modules use this pattern; it decouples from the app, not from Celery |
| Dapr components ([schema, v0.11](https://v0-11.docs.dapr.io/operations/components/component-schema/)) | a building block (`state`, `pubsub`) | a component `type: pubsub.redis` with `version` | a failing component stops the sidecar unless `ignoreErrors: true` | none | capability type + version; refusal by default, tolerance by explicit opt-in |
| Python packaging ([PEP 508](https://peps.python.org/pep-0508/), [core metadata Provides-Dist](https://packaging.python.org/en/latest/specifications/core-metadata/#provides-dist-multiple-use), [entry points](https://packaging.python.org/en/latest/specifications/entry-points/)) | `Requires-Dist`, extras | the installer | install fails | n/a | a requirement names a distribution. There is no "any one of" and no virtual package pip acts on, and extras on entry points are "no longer recommended". Wheel metadata cannot say "needs some taskqueue driver"; that belongs in the code declaration, with drivers found through entry points |
| Kubernetes probes ([docs](https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/)) | n/a | n/a | n/a | n/a | readiness "determine[s] when a container is ready to accept traffic"; it is for initial work and "recovering from temporary faults", and may check "each required back-end service". A missing package is neither temporary nor recoverable by waiting: it belongs at startup |

What carries over:

- the consumer names an abstract capability and the guarantees it uses (OSGi, Dapr);
- availability is checked without loading the provider (Spring `@ConditionalOnClass`);
- an unmet mandatory requirement refuses activation (OSGi, Spring, Dapr);
- the provider's library comes with the deployment's choice, not with the consumer's package
  (Laravel, Spring starters);
- readiness is for transient conditions (Kubernetes).

What does not carry over:

- Django's silent `ImmediateBackend` default in production;
- a database queue in core as the first step. Rails and Laravel own their workers; Sandloom
  would be building a second task framework.

## 3. Answers to the five questions

### Q1. What a module declares, where, and what the kernel does with it

- **What:** the abstract capability `taskqueue`, plus the guarantees used as features:
  - `periodic` (new): the driver runs the module's periodic hooks at their interval in at least
    one process of the deployment;
  - `delay` (existing): request-path enqueue with a delay.

  Required or optional: billing required; identity optional if the owner agrees (1.5).
- **Per module, not per task.** A driver is chosen per instance, the module is the atomic unit,
  and per-task granularity would not change which driver is needed.
- **Where:** the existing `define_module(capabilities=[...])`. Never in wheel metadata.
- **Derived as well:** the kernel derives the same requirement from the module's code, as it
  does for database features and contracts:
  - a `<pkg>.tasks` submodule → `taskqueue` (the rule `ops.py:125-139` and the worker already
    use);
  - `setup_periodic_tasks` → feature `periodic` (`core/tasks/periodic.py:19-37`);
  - an explicit declaration overrides the derived `required` and adds features.
- **At composition time:** the preflight resolves each effective requirement: spec, version,
  instance, driver registered, **driver installed**, features. A required one that fails
  refuses startup with a code; an optional one is reported.

### Q2. No driver for a required capability

| option | production safety | DX of `pip install sandloom-billing` | 16-subset matrix | mono/split parity | owner rules |
|---|---|---|---|---|---|
| **(a) startup refusal with a code** | safe: nothing serves with an unmet requirement; the cause is named | clear first-run message with the install line or the `INFRA` alternative | P2 subsets with billing: "refused, predefined code"; P3: ready | same check in every process that loads the module (app, worker, `sandloom migrate`, CLI) | no undeclared dependency; no guarantee silently lost |
| (b) boot not ready (today) | the process runs; the pod never becomes ready; no code; the worker cannot start at all | discovered later, at `/ready` or in a stalled rollout | "not ready" is neither of I-19's outcomes | parity holds, but each service stalls | readiness carries a permanent fact |
| (c1) built-in in-process or eager driver as default | unsafe unless refused in prod; task bodies run in web processes; no redelivery; periodic needs a lease, and the existing lease needs a CAS cache, i.e. Redis | best first run | P2 ready in dev | in-process scheduling differs per process count | acceptable only with a prod refusal and an accepted risk, like `memory` eventbus or SQLite; needs a kit first |
| (c2) database-backed queue as default | durable; no new infrastructure | good | P2 ready | good | a new driver, worker and scheduler: the "second task framework" the review warns against; only after `taskqueue` guarantees and a kit exist |

Recommended: (a) now. (c1) or (c2) remain possible later as ordinary drivers under the same
declaration (open question 7). The declaration and the check are needed under every option.

### Q3. Packaging

- **Module wheels** depend on `sandloom` only, never on `sandloom[celery]`:
  - PEP 508 cannot express "any taskqueue driver";
  - an extra names a vendor and installs it where `INFRA` selects another driver;
  - the decision belongs to the composition.

  This is already the case (`packages/sandloom-billing/pyproject.toml`: `sandloom>=0.7.0a11,<0.8`).
- **`sandloom`** keeps `celery` and `redis` as extras, keeps `celery` as the default driver
  name of `taskqueue`, and gains per-driver availability metadata (requires `celery`, install
  hint `pip install 'sandloom[celery]'`).
- **Composition profiles** bring the drivers. The `sandloom[standard]` and `sandloom[admin]`
  extras already pull `sandloom[redis,celery]` (`packages/sandloom/pyproject.toml`, extras
  `standard`, `admin`), as does the `sandloom new` template (C-MODULE-03,
  `guarantees.yml:6030`). The `sandloom-standard` wheel itself depends on plain `sandloom`;
  the profile is the extra.
- **Another driver:** install its package, which publishes `taskqueue.<name>` in
  `sandloom.drivers` and advertises its features, then select it with
  `INFRA={"taskqueue": {"default": {"driver": "<name>"}}}` (ADR-0011).
  - If it does not advertise `periodic`, billing refuses with the new code naming the feature.
  - The framework worker stays Celery-only until the contract covers execution.
- **Diagnostics:**
  - `sandloom check` flags an undeclared capability (new check code);
  - `sandloom doctor` modules section: each module's effective requirements and whether each
    is met, with the same message as the refusal;
  - `sandloom doctor` infra section: skips synthesized instances nothing requires, shown as
    "unused";
  - `sandloom explain <code>` gives why and the fix.

### Q4. The other capabilities; extend or build?

The same model applies unchanged. All of it extends existing parts: `CapabilityRequirement`,
the preflight, `DriverRegistration`, `InstanceConfig`, the error catalogue, the readiness
probe, the doctor sections and the archcheck rules. No new subsystem.

- **cache:** identity's `"cache"` requirement becomes checkable once the `redis` driver
  declares it requires `redis`. The kernel's own cache use needs a kernel requirement
  (open question 2).
- **eventbus:** the `redis` and `streams` drivers declare they require `redis`. The prod
  default (`streams`) then refuses with a code on plain `sandloom`, instead of failing at first
  publish.
- **objectstore:** `local` only; nothing to add beyond deriving from `objectstore()` calls.
- **notification:** identity and tenancy declare `notification` with feature `email`, or the
  check derives it. The existing log-notifier protection then works in prod.

Under the owner rule "no new architectural subsystem without a failing existing mechanism",
the failing mechanism is the preflight. It is mechanically right but blind to package
availability and fed incomplete declarations. Fixing those two inputs is the change.

## 4. Recommendation: exact changes

Codes below are placeholders: **B0xx** is the next free boot code and **C0xx** the next free
check code. When this was written B036 was only named in the trust PoC patch
(`design/kernel-boundary/poc/trust/ta1-kernel.patch`) and C032 was reserved by the authz plan;
both are real codes now (`sandloom.core.error_codes`: B036 and C032 are taken, the next free
are B038 and C034).

1. **Capability spec.** Add the feature `periodic` to `TASKQUEUE_CAPABILITY`
   (`capabilities.py:199-204`), and to `CELERY_TASKQUEUE_FEATURES`
   (`celery_taskqueue.py:28`). Its definition: "the driver runs the module's periodic hooks at
   their interval in at least one process while the deployment runs; a run may repeat, a
   module tolerates duplicates." Its test: one example, the Celery worker with beat and the
   lease (C-EFFECT-01, G-OPS-01). It is not a method of the contract, so ADR-0011 decision 4
   holds. Do not add redelivery as a feature until a module requires it; billing does not
   (1.5).
2. **Declaration.**
   - Billing: `capabilities=["database", "eventbus",
     CapabilityRequirement("taskqueue", features=frozenset({"periodic"}))]`.
   - Identity: `CapabilityRequirement("taskqueue", features=frozenset({"periodic"}),
     required=False)` if open question 1 says optional, plus
     `CapabilityRequirement("notification", features=frozenset({"email"}))`.
   - Tenancy: the same `notification` declaration.
   - Optionally add `reason: str = ""` to `CapabilityRequirement`, like `ContractRequirement`,
     shown in refusals and doctor. That is a public API change: baseline (T008) and docs.
3. **Derivation.** `ModuleDescriptor.effective_capabilities()`
   (`descriptors/__init__.py:155-157`) adds the derived requirements:
   - `<name>.tasks` exists → `taskqueue`;
   - `has_periodic_tasks(name)` → feature `periodic`;
   - a declared `taskqueue` requirement wins for `required` and unions features.

   This is kernel-side, so it works for L1 (`define_module`) and L2 (hand-written `register`)
   modules, imports nothing (`find_spec`, AST), and names no module (I-02 allows a submodule
   derived from a descriptor). `sandloom generate` records the effective capabilities in
   `module.snapshot.json` (reviewed, T-gated like `consumes`).
4. **Static check C0xx `undeclared-capability`** in `core/archcheck/module_rules.py`, beside
   C030. It reports:
   - a `tasks.py` without a declared `taskqueue`;
   - a `setup_periodic_tasks` without the `periodic` feature;
   - a call to `cache()`, `notifier()` (and its `channel=` literal → feature),
     `objectstore()` or `taskqueue()` from `sandloom.sdk.infra` without the matching
     declaration.

   `sandloom add task` writes the declaration (and `--periodic` the feature). Fixtures go in
   `tests/architecture/_fixtures/`. Open question 6: keep C0xx, or rely on derivation alone.
5. **Driver availability, build-free.**
   - `DriverRegistration` (`registry.py:89-115`) gains `requires: tuple[str, ...]` (top-level
     import names) and `install: str` (a hint).
   - `register_driver` accepts them.
   - Built-ins: `celery` → `("celery",)`, `"pip install 'sandloom[celery]'"`; `redis` cache
     and the `redis`/`streams` eventbus drivers → `("redis",)`, `"pip install 'sandloom[redis]'"`.
   - `validate_requirement` (`registry.py:576-642`) checks `importlib.util.find_spec(n) is not
     None` for each name and raises a new `DriverUnavailableError(CapabilityError)` carrying
     the hint. No import, no network.
   - A plugin driver is already import-checked when `INFRA` selects it (B022). It may declare
     `requires` as a class attribute for the same message.
6. **Explicit and implicit instances.**
   - `InstanceConfig` (`config.py:56-63`) gains `implicit: bool = False`. It is set `True` in
     `_synthesize_defaults` and cleared when `INFRA` names the instance (`_overlay`).
   - `registry.validate()` checks availability for explicit instances only: an operator who
     selects an uninstalled built-in driver gets B022, like a plugin.
   - `aopen_all`, the `/health` capability checks (`lifecycle.py:189-209`), `health_all` and
     doctor's infra section act on an implicit instance only when a requirement (a module's,
     or the kernel's, item 10) names it. Otherwise they report it as `unused` in
     `/diagnostics/infra`.

   This removes the inferred prod refusal of 1.7 (3) for an app without task modules.
7. **Refusal code B0xx `capability-unavailable`** (boot):
   - raised by `_preflight_capabilities` for every unmet required requirement of a module at
     once, wrapping `InstanceNotConfiguredError`, `DriverNotRegisteredError`,
     `DriverUnavailableError` and `FeatureUnsupportedError`;
   - B033 stays for derived database features;
   - an entry in `EXCEPTION_CODES` and the catalogue, and in `sandloom explain`.

   Example message:

   > module 'sandloom_billing' requires capability 'taskqueue' (feature 'periodic', derived
   > from sandloom_billing/tasks.py), and instance 'default' cannot serve it: driver 'celery'
   > needs the package 'celery', which is not installed [SANDLOOM-B0xx]. Fix: pip install
   > 'sandloom[celery]', or install a taskqueue driver package that advertises 'periodic' and
   > select it in INFRA.

   A required module refuses startup. For an optional module that was explicitly selected,
   the owner decides whether it is fatal like answer #29 (open question 5).
8. **Readiness (amend G-OPS-23).**
   - `_TaskqueueReadiness.task_modules()` (`ops.py:183-191`) selects loaded modules whose
     effective requirements include a required `taskqueue`, instead of `find_spec`.
   - The "not installed" case can no longer reach `/ready`, because startup refused.
   - For "configured but temporarily unavailable → 503" to be true, the Celery driver gets a
     bounded broker check in `health()`: a kombu connection `ensure_connection` with
     `HEALTH_CHECK_TIMEOUT_SECONDS`, inside the existing TTL and single-flight cache, so no
     extra I/O per hit.
   - Recommended scope: the term applies when a loaded module requires `delay`, i.e. it
     enqueues on the request path, and in worker and beat processes. A web process whose
     modules only need `periodic` does not use the broker to serve traffic, so its readiness
     should not drop on a broker outage (Kubernetes: readiness is about accepting traffic).
     Open question 3.
9. **`taskqueue.v1` stated honestly** (`docs/modules/background/tasks.md`, ADR-0011 note):
   - the contract is enqueue, delay and enqueue dedupe;
   - execution, the beat hook and retry semantics are the Celery driver's;
   - `periodic` is advertised only by `celery`;
   - a second driver claiming it needs the `taskqueue` conformance kit first (ADR-0011
     decision 6);
   - `TaskQueueNotInstalled` and `_INSTALL_HINT` (`sdk/_prim/tasks.py:53-62`) stay as the
     run-time refusal, like R015 after B033. They are reached only outside a checked
     composition: scripts, tests, or a non-Celery driver with `.delay()`.

   KB-01's lazy import stays: it is what lets the composition reach the coded refusal instead
   of a `ModuleNotFoundError`.
10. **Kernel requirements (owner decision, open question 2).** Declare the kernel's own needs
    with the same type: `database`, `cache` and `eventbus`, checked at `create_app` through the
    same preflight (B010 already checks database features for request units). On plain
    `sandloom` without a cache driver this refuses P1 unless the owner picks one of:
    - `redis` in base dependencies;
    - a `memory` cache driver for development, refused in prod without an accepted risk,
      promoted from `core/testing/_uow.py:272`;
    - accepting that P1 refuses until `sandloom[redis]` is installed.
11. **I-19 wording.** Replace the first paragraph after the profiles with:

    > In each profile, in clean environments, each of the 16 subsets has an expected outcome.
    > A subset starts and becomes ready when every infrastructure capability its modules
    > require, declared or derived, has a configured instance whose driver is installed and
    > supports the required features. When one has none, startup refuses with
    > SANDLOOM-B0xx naming the module, the capability, the missing driver or feature, and the
    > fix. A configured capability that is temporarily unreachable makes readiness fail in
    > the processes that use it, naming it. A refusal is never caused by an undeclared
    > dependency. The predefined reasons: SANDLOOM-B005, SANDLOOM-B0xx, and a selected
    > module's configuration error (I-18).

    Change the table to the outcomes of section 6. Amend I-20's taskqueue note: "`/ready` 200
    with an unusable taskqueue" is closed by startup refusal plus readiness on reachability.
12. **Ledger.**
    - New guarantee: "a module's required capabilities, declared or derived from its code,
      are met at startup with an installed driver and the required features, or every entry
      point refuses with SANDLOOM-B0xx."
    - Amend G-OPS-23 (readiness keyed on the requirement; transient only).
    - New guarantee for implicit instances: "a synthesized instance nothing requires is neither
      opened nor probed."
    - DX eval re-run (AGENTS.md): `sandloom add task` output and the public API change.

## 5. Alternatives considered

| alternative | why rejected |
|---|---|
| Module wheels depend on `sandloom[celery]` | names a vendor in the module; packaging cannot say "any driver"; installs Celery where `INFRA` selects another driver; makes P2 "ready" by hiding a deployment decision |
| Keep `/ready` 503 as the P2 outcome (KB-02 as is) | puts a permanent installation fact in a transient probe; no code; a pod that never becomes ready; the worker cannot start anyway; not one of I-19's outcomes |
| Declare `taskqueue` and change nothing else | does nothing: the preflight passes with Celery absent (1.7, check 1) |
| Built-in in-process or eager taskqueue as default | needs a scheduler with a lease, and the existing lease needs a CAS cache, i.e. Redis in multi-process; runs task bodies in web processes; no redelivery; must be refused in prod; needs a kit. Possible later as a dev driver (open question 7) |
| Database-backed queue in core | the right shape for an infrastructure-agnostic production default (Rails 8, Laravel), but a new worker and scheduler is the second task framework; only after the guarantees and the kit |
| A separate `scheduler` capability for periodic work | no driver other than Celery beat exists; a feature of `taskqueue` states the guarantee within the existing spec. Revisit when a driver schedules without a queue |
| Per-task requirements | finer than any driver choice; the module is the atomic unit |
| Process roles (web needs no queue, worker does) as a kernel concept | a new mechanism, and a parity risk; the readiness scoping by feature (item 8) gets the useful part |
| Derivation only, no declaration and no C0xx | simpler, and possible; but declarations are what readers and agents see, and C030/B033 set the pattern of doing both (open question 6) |
| A universal capability registry or a second driver system for modules | the existing registry and preflight need two inputs fixed, not replacing; owner rule (tm decision, 2026-11-08) |

## 6. Tests that would prove it

**Registry and loader** (kernel unit, no batteries):

- `validate_requirement` raises `DriverUnavailableError` when `find_spec("celery")` is `None`
  (patched). Celery is not in `sys.modules` afterwards, and no socket is opened.
- An explicit `INFRA` instance on an uninstalled built-in driver → B022 at
  `build_infra_registry`.
- An implicit `taskqueue/default`, nothing requires it, prod settings, Celery absent →
  `create_app` and the lifespan succeed; `aopen_all` skips it; `/diagnostics/infra` shows it
  `unused`; doctor shows `unused`.
- Derivation:
  - a module with `tasks.py` and no declaration → effective `taskqueue` required;
  - with `setup_periodic_tasks` → feature `periodic`;
  - a declared `required=False` wins;
  - a hand-written L2 `register` gets the same.
- B0xx:
  - the message names module, capability, instance, driver, feature, where derived, and the
    install hint;
  - all unmet requirements of one module are listed;
  - `code_for_exception` returns B0xx;
  - `sandloom explain B0xx` exists.
- A feature mismatch: a fake `taskqueue.fake` driver without `periodic` → billing refuses
  with B0xx naming `periodic`; with `periodic` → loads, and the module code is unchanged.

**Static check** (archcheck): C0xx fixtures for `tasks.py` without `taskqueue`, a periodic
hook without `periodic`, `notifier().send(Notification(channel="email"))` without
`notification`/`email`, and `cache()` without `cache`. The bundled modules pass after item 2.

**Readiness** (amend `src/sandloom/core/tests/test_ready_taskqueue.py`):

- the term follows the effective requirement, not `find_spec`;
- the Celery `health()` with an unreachable broker → `fail` within the timeout, cached;
- `/health` never carries it;
- under item 8's scope, a `periodic`-only web process stays ready on a broker outage.

**Process parity:** app, worker, `sandloom migrate` and a composition-building CLI refuse
billing on plain `sandloom` with the same B0xx (answer B's process matrix pattern).

**Notification:** prod, `log` notifier, identity loaded → B0xx (feature `email`) unless
`log_notifier_in_prod` is accepted.

**KB-04 matrix**, new expected outcomes (`expected()` in `_module_subset_matrix.py`;
`PREDEFINED_REFUSALS` gains B0xx; `TASK_MODULES` replaced by the modules' effective
requirements). Options are those of open questions 1 and 2:

| profile | subsets | expected |
|---|---|---|
| P3 extras, repository | all 16 | ready; `taskqueue_status` `ok` where a module requires it, `unused` elsewhere |
| P2 plain `sandloom` | with billing (8) | refused B0xx: `taskqueue` (driver `celery` not installed, feature `periodic`) |
| P2 | with identity, without billing (4) | refused B0xx: `cache` (driver `redis` not installed). With identity's `taskqueue` optional, taskqueue is reported, not refused |
| P2 | none, tenancy, admin, tenancy+admin (4) | ready if kernel requirements stay unchecked; refused B0xx (`cache`) if item 10 is adopted without a base cache driver; ready if a base cache driver ships |
| P2 + `sandloom[celery,redis]` (new lane) | all 16 | ready |
| P2 + fake driver without `periodic` (new lane) | with billing | refused B0xx naming `periodic` |

The matrix should stop replacing the cache probe with a passing one, or record that it does,
so that a P2 "ready" means ready (1.7, result 3).

## 7. Open questions for the owner

1. **Identity's periodic cleanup:** required or optional? Refresh-token validity is checked on
   use (`token_service.py:259-262`), so the cleanup looks like hygiene: optional, reported by
   doctor.
2. **The kernel's own cache requirement and P1:** `redis` in base, a `memory` dev driver
   refused in prod, or P1 refuses without `sandloom[redis]`? Today P1 and P2 are "ready" only
   with stubbed probes.
3. **Readiness for `periodic`-only modules in web processes:** should a broker outage fail
   them? Recommendation: no; it should fail the worker's and beat's readiness.
4. **Celery `health()`:** should it check the broker (bounded, cached)? Without it,
   "temporarily unavailable → 503" is not observable.
5. **An explicitly selected optional module with an unmet required capability:** fatal, like
   answer #29?
6. **C0xx:** require the declaration (C030 pattern), or rely on derivation plus the snapshot?
7. **A zero-dependency taskqueue driver later:** an in-process dev driver refused in prod, or a
   database-backed driver after the `taskqueue` kit? Either one makes P2 "ready".
8. **Code numbers** for B0xx and C0xx.

## 8. The review points, verified

| # | point | verdict | evidence |
|---|---|---|---|
| 1a | Modules depending on `sandloom[celery]` is wrong as final architecture | confirmed | packaging cannot express "any driver" (PEP 508); `INFRA` selects drivers (`config.py:127-137`, `registry.py:1013-1024`) |
| 1b | Permanent `/ready` 503 is wrong as a normal install outcome | confirmed | the term only detects "cannot build" because Celery `health()` is the default `ok` (`contracts.py:592-594`, `ops.py:143-205`); Kubernetes readiness is for transient conditions |
| 1c | Third path: required abstract capability; missing → coded refusal; configured but down → 503; no need → ready without Celery; compatible driver → no module change | partly: adopted, with two amendments | "down → 503" needs a broker check in the Celery driver (item 8) and should apply where the broker is used; "compatible" must be defined by features (`periodic`), not by `enqueue` |
| 2 | The driver cannot yet be swapped end to end | confirmed | spec `capabilities.py:199-204`; default `celery` `config.py:127-137`; contract enqueue-only `contracts.py:1138-1177`; worker refuses non-Celery `worker.py:104-121`; Celery-shaped SDK `sdk/_prim/tasks.py:79-203`; beat hook `worker.py:370`; no kit, `driverkit/__init__.py:20` |
| 3 | Billing's reconciler is a correctness function; a queue is not compatible because it can enqueue | confirmed | `intent_reconciler.py:1-55`; it needs periodic invocation, tolerates duplicates, heals a lost run at the next tick (1.5) |
| 4 | `CapabilityRequirement("taskqueue")` alone does not detect a missing Celery | confirmed by running | 1.7 check 1; `bootstrap.py:104-110`; `registry.py:576-642` checks metadata only |
| 5 | Not every function in `tasks.py` makes the queue required; check identity | confirmed | identity's scheduled tasks are storage cleanup (`tasks.py:37-75`); validity is checked on use (`token_service.py:259-262`); placeholders unscheduled (`tasks.py:78-101`, `:117-118`) |
| 6 | Proposed I-19 wording | partly: adopted with additions | add "declared or derived", "installed driver with the required features", the code, and "in the processes that use it" (item 11) |
| 7 | A composition profile with Celery, not a wheel dependency of each module | confirmed, already so for the extra | `sandloom[standard]` and `sandloom[admin]` pull `sandloom[redis,celery]` (`packages/sandloom/pyproject.toml`); the `sandloom-standard` wheel depends on plain `sandloom`; module wheels on plain `sandloom` |

Additional findings, not in the review:

- **Notification.** Identity and tenancy send email without declaring `notification`, which
  bypasses the log notifier's prod protection (1.3).
- **Cache.** `/health` fails on plain `sandloom` (no `redis`), hidden by the matrix's stubbed
  probes (1.7).
- **Prod on plain `sandloom`.** It likely refuses at `aopen_all` on the synthesized Celery
  instance, even with no task module (1.7, inferred).
