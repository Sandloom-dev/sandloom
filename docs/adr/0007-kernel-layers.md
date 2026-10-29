# ADR-0007: Kernel layers and an acyclic kernel, enforced by import contracts

- **Status:** Accepted
- **Date:** 2026-10-25
- **Deciders:** Sandloom maintainers
- **Complements:** the module ↔ kernel boundary (`architecture_rules.yml`,
  `sandloom check`) and ADR-0005 (the effect model, whose commit pipeline the
  `unit_of_work` package implements)

## Context

The boundary between business modules and the kernel (`sandloom.core`) was
strictly enforced; the kernel's own structure was not. Measured on main
`3e659852` (228 kernel modules, 74k lines):

* **Cycles were hidden, not absent.** The module-level import graph looked
  acyclic only because cycles went through **function-local imports**
  (550 `# noqa: PLC0415` markers in `sandloom/core`, 616 in the tree). Counting
  every runtime import, one strongly connected component held **96 modules**
  (42 % of the kernel).
* Even the module-level imports, grouped by top-level kernel package, formed
  one **11-package cycle** (eventing, facade, infra, loader, migrations, orm,
  planner, projections, sql_alchemy, tasks, unit_of_work) — 27 packages once
  the namespace packages (`tasks`, `sql_alchemy` without `__init__.py`) became
  visible to the analyser.
* Five files of 1 300–1 900 lines sat in that knot: `unit_of_work.py`,
  `idempotency.py`, `facade.py`, `sql_alchemy/planner_adapter.py` and
  `sql_alchemy/outbox_store.py`.

A lazy import does not remove a dependency, it hides it from the interpreter.
Without a map nobody can say whether a new import goes "down" or "up", so the
knot grows one reasonable-looking import at a time.

## Principles

* **Acyclic Dependencies (ADP).** No cycle between kernel packages or modules,
  at any depth. A cycle means the parts cannot be understood, tested or
  replaced separately.
* **Stable Dependencies (SDP).** Depend in the direction of stability:
  configuration and foundation types change least and sit at the bottom;
  composition and tooling change most and sit at the top.
* **Dependency Inversion (DIP), sparingly.** A lower layer may expose a hook a
  higher one fills — only where there is a genuine extension point, each one
  justified below. A Protocol or hook that exists only to break an import is
  an artificial abstraction and is not used.
* **Composition root.** Concrete backends (`drivers`) are chosen and built by
  the composition layer (`app_factory`, `composition`, the worker), never
  reached from below.
* **Extract shared types downwards.** When two layers need the same value
  (a ContextVar, an exception, a URL helper), it moves to the lowest layer
  that both may import, instead of one importing the other.
* **Never hide a cycle behind a function-local import.** A function-local
  import in the kernel is legitimate only for an *optional* dependency, or to
  keep *import cost / import-time side effects* off the application's import
  (CLI, test harness, a driver SDK, metric registration). It states which
  (see "Function-local imports").

## Decision

1. **The layer map is an import-linter `layers` contract**
   (`.importlinter`, contract `kernel-layers`): `containers =
   sandloom.core`, `exhaustive = true`, so a new `sandloom.core.*` child fails until it
   is placed. Every runtime import counts, module-level and function-local;
   only `if TYPE_CHECKING:` bodies are excluded
   (`exclude_type_checking_imports`).
2. **The kernel is acyclic at every depth** — contract `kernel-acyclic`
   (`acyclic_siblings`, `ancestors = sandloom.core`, `depth = 20`): siblings of one
   layer (`:` in the map) may import each other, but never in a cycle, and no
   sub-package (`unit_of_work.*`, `facade.*`, …) may have one either.
3. **Configuration is a leaf** — contract `kernel-config-leaf` (`forbidden`):
   `sandloom.core.config` (settings, tiers, accepted risks, password parameters,
   replay limits, the profile names) imports no other kernel package.
4. **Enforcement.** `lint-imports` (import-linter, pinned exactly in the dev
   extras and `uv.lock`) runs from
   `tests/architecture/test_kernel_import_contracts.py` (which also refuses
   an `ignore_imports` entry without a `; reason:` comment) and from the
   `import-contracts` pre-commit hook. The earlier `sandloom check` rule
   `core-layers` and its baseline were removed: no `sandloom check` rule duplicates
   what the tool checks.
5. **No `ignore_imports`.** No contract has one. Should one ever be needed it
   is debt, not configuration: a written reason and an owner right above the
   entry; import-linter fails an entry that no longer matches.

### The map (top → bottom)

| layer | contents | why here |
|---|---|---|
| **tooling** | `cli`, `testing`, `contract_testing`, `archcheck` | operator CLIs (incl. `sandloom-migrate`: `cli.migrate`, `cli.extract`) and the test harness may reach everything |
| **composition** | `app_factory`, `admin`, `composition`, `worker`, `gc_tuning`, `guarantee_windows`, `di`, `lifecycle`, `facade` | wiring: the composition root, process entry points, startup checks, the public DI surface and the `CoreFacade` aggregator |
| **contracts & RPC** | `rpc`, `loader`, `module_api`, `contract_compat`, `contract_cache_shadow`, `rpc_transport`, `rpc_transport_compat` | sync contracts, the module loader and the internal RPC client/server; above HTTP because the RPC server is an HTTP endpoint and contracts map HTTP errors |
| **auth & HTTP** | `http`, `idempotency`, `operation_classes`, `auth`, `internal_auth`, `production_profile`, `route_markers`, `guard_order`, `rpc_binding`, `rpc_identity`, `rpc_pair_mac`, `ingress` | tokens, revocation, request guards and the HTTP pipeline; `ingress` holds the primitives the middlewares and guards share (client IP, rate limiter) |
| **drivers** | `_internal`, `drivers` | concrete backends implementing the infra contracts with the persistence and messaging machinery; `drivers.bootstrap` builds the registry |
| **messaging** | `projections`, `tasks`, `async_runner`, `beat_lease`, `consumers` | everything that runs a handler or task **in** a Unit of Work: the consumer runtime, the outbox relay and event operations, projections, the worker loop |
| **persistence** | `migrations`, `orm`, `sql_alchemy`, `planner`, `unit_of_work`, `tenant` | the Unit of Work and its commit pipeline, the relational paradigm, the planner, the tenant guard, the migration runner |
| **bus** | `eventing`, `runtime` | the event bus, transports, envelopes, delivery primitives, consumer inbox; `runtime` (active core) resolves the app-owned bus. Below persistence because the commit pipeline publishes |
| **infra** | `infra` | capability contracts, the registry, the access layer (`infra.access.cache()`) |
| **foundation** | `exceptions`, `tenant_context`, `contract_context`, `observability`, `signing_keys`, `contract_wire`, `effects`, `descriptors`, `contracts`, `schema`, `claims`, `utils`, `cpu_quota` | primitives every layer may read: context vars, the wire codec, effect records, descriptor and contract types, logging/metrics/tracing |
| **configuration** | `config` | settings and the value types it validates — a leaf |

`sandloom.core.auth` (tokens, bearer, revocation marks, blocks, freshness,
recovery) and `sandloom.core.config` (the settings split) are the packages of the
auth/config split; `auth` imports nothing above the auth & HTTP layer, and
`config` imports no other kernel package.

### The one hook

| hook | why it is a genuine extension point |
|---|---|
| `unit_of_work.dispatch.set_committed_events_hook` (armed by `contract_cache_shadow`) | the shadow contract cache is an optional, off-by-default observer of committed events; the commit pipeline must not know it exists, and no other consumer of "events just committed" belongs in the UoW |

Every other cycle was removed by moving code to the layer it belongs to (see
"What moved") or by merging two modules that were one unit.

### The default-registry fallback was dropped

The last ignored edge was `infra.defaults -> drivers.bootstrap`: outside
production, `di.cache()` / `di.notifier()` with no registry bound quietly built
a default registry from the flat settings (built-in drivers, no plugins). No
test or example relied on it (with the fallback forced to raise, the full suite
and the three example apps still passed), so it was removed rather than
inverted into a hook — an inverted hook would have made the fallback depend on
which composition module had been imported. `sandloom.core.infra.defaults` is gone;
`infra.access` resolves through `get_active_infra()`, which raises in every
environment, with a hint naming the composition roots that bind a registry
(`create_app`, `sandloom.core.composition.load_composition_core`, the
`sandloom.core.testing` harness). The kernel has **no** import cycle at any level.

### Function-local imports

A kernel `# noqa: PLC0415` states its category, checked by
`test_kernel_function_local_imports_state_their_category`
(`tests/architecture/test_lint_suppression_ratchet.py`):

* `- optional: <reason>` — a dependency that may be absent or unconfigured
  (sentry, OpenTelemetry, uvloop, prometheus multiprocess mode, probes of
  private driver internals whose absence is the reported fallback);
* `- cold: <reason>` — import cost or import-time side effects kept off the
  application's import: the CLI, test harness and migration tooling, driver
  SDKs (redis-py, asyncpg, Celery, pure-Python jsonschema) imported when the
  driver is built, the outbox relay (it registers its metrics on import),
  `rpc_transport` (it imports httpx).

A cycle break is not a category, and no kernel file is exempt. A hoisted kernel import
is written `import sandloom.core.x as _x` and used as `_x.name`: the lookup stays
late-bound, so a test patching `sandloom.core.x.name` behaves as with the old local
import.

**Amendment (2026-10-25): a lazy import sits at a boundary.** "Tooling path"
alone is not a reason: a module that only the CLI, the test harness or the
migration tooling imports already pays nothing on the app's import, so its
imports are module-level. A `cold:` marker names the boundary it keeps: the
pytest plugin's entry and the settings snapshot it must not trigger before
`.env.test` is loaded, a static command that runs unconfigured (`sandloom check`,
`sandloom admin`, `sandloom add`), gunicorn's master (it loads `gunicorn_conf`
before `on_starting`), the worker healthcheck, the `sandloom.core.migrations`
package `__init__` (no Alembic for `urls`), or a heavy driver SDK on the app
path. Paying the CLI / harness / migration markers down took `sandloom/core` from
296 markers (274 `cold`, 22 `optional`) to 116 (94 `cold`, 22 `optional`);
`NOQA_BASELINE["PLC0415"]` (which also counts `sandloom.shared`'s 8) went from 304
to 124. `sandloom.main` imports the same modules in the same time.

### What moved (no re-exports at the old paths)

Only the documented public API keeps its import paths (`sandloom.core.di`,
`facade`, `module_api`, `descriptors`, `orm`, `projections`, `testing`,
`contract_testing`, `sandloom.shared.*`); `public_api_baseline.json` and the
contract snapshots are unchanged. Every importer (kernel, modules, examples,
templates, tests, docs) was updated.

| from | to |
|---|---|
| `infra/drivers/*`, `infra/bootstrap.py` | `drivers/*`, `drivers/bootstrap.py` |
| `eventing/consumers.py`, `eventing/runtime.py` | `consumers/callbacks.py`, `consumers/runtime.py` (with `start_event_consumers`) |
| `eventing/versioning.py`, `sql_alchemy/savepoints.py` | `unit_of_work/versioning.py`, `unit_of_work/savepoints.py` |
| `eventing/transport.py` | deleted (shim); `PoisonEventError`, `stream_start_id`, `START_FROM_*` in `eventing/delivery.py` |
| `migrations/migrations_cli.py`, `migrations/extract.py` | `cli/migrate.py` (`sandloom-migrate` entry point), `cli/extract.py` |
| `migrations/rls.py` | `sql_alchemy/rls.py`; `to_sync_url` in `migrations/urls.py` |
| `database.py` | deleted; `Base` in `sql_alchemy/base.py` (re-exported by the public `orm`) |
| `sql_alchemy/testing.py` | `testing/database.py` |
| `http/client_ip.py`, `http/rate_limit.py` | `ingress/` |
| `loader/service_registry.py` | `module_api/_service_registry.py` |
| `TenantScope` | `http/tenant_scope.py` |
| request id ContextVar, contract-call ContextVars | `observability/request_context.py`, `contract_context.py` |
| read guard, `durable_inbox()` | `module_api/_read_guard.py`, `sql_alchemy/durable_inbox.py` |
| outbox / Streams operations of the CLI | `tasks/event_ops.py` (shared with the owner console) |
| cgroup helpers | `cpu_quota.py` |

Split along responsibilities, each a package whose submodules are the import
paths:

| file (lines) | package |
|---|---|
| `unit_of_work.py` (1 899) | `unit_of_work/{scope, hooks, dispatch, effects, savepoints, versioning}` |
| `idempotency.py` (1 690) | `idempotency/{__init__ (guard), fingerprint, replay, durable}` |
| `facade.py` (1 609) | `facade/{__init__, _state, _services, _claims, _registries, _runtime}` (a base-class chain of `CoreFacade` mixins) |
| `sql_alchemy/planner_adapter.py` (1 409) | `planner_adapter/{__init__, forms, compiler, units}` |
| `sql_alchemy/outbox_store.py` (1 266) | `outbox_store/{__init__, statements, relay, audit}` |

Aliases removed: `idempotency._ArmedKey`, `ingress.rate_limit._client_ip`,
`migrations.migrations.ensure_outbox_table`, `infra.config.CAPABILITIES`,
`consumers.callbacks.ALLOW_VOLATILE_CONSUMERS_ENV`, and the package-level
re-exports of the five split packages, of `module_api` / `facade` privates, of
`cli.events` and of `migrations.migrations`.

## Consequences

* A new upward import or cycle fails `lint-imports`, the architecture suite
  and pre-commit, function-local or not.
* Breaking a cycle needs a design move — move the code, extract the shared
  type downwards, merge what is one unit — not a `# noqa: PLC0415`.
* Code that monkeypatches a kernel global patches the module that defines it
  (the split packages have no re-exports).
* Observable but intended: the logger names of moved modules follow their new
  path (`sandloom.core.drivers.redis_cache`, `sandloom.core.drivers.sqlalchemy_database`,
  `sandloom.core.consumers.runtime`); importing `sandloom.core.di` registers the kernel's
  ORM models (it imports the relational UoW at module level now). Import-time
  side effects of `sandloom.main` are otherwise unchanged: the same third-party
  modules and the same Prometheus collectors are loaded.

### Numbers (main `3e659852` → this change, after merging the auth/config split)

| | before | after |
|---|---|---|
| kernel modules | 228 | 281 |
| largest SCC, every runtime import | 96 | **0** (no cycle) |
| package-level cycles (top level / any depth) | 11-package (27 with namespace packages) | **0** |
| `# noqa: PLC0415`, `sandloom/core` / tree | 550 / 616 | 265 / 332 (−284; kernel: 244 `cold`, 21 `optional`, none exempt) |
| largest of the five split files | 1 899 lines | 798 (`idempotency/__init__.py`), the rest ≤ 673 |

Cost — cachegrind instructions per request (`benchmarks/ladder/cg`, `CG_K=200`,
mean of two runs) and `sandloom.main` import time (median of 7), measured on the
layering change before the auth/config merge:

| | main `3e659852` | this change | Δ |
|---|---|---|---|
| HW7 | 8.157 M | 8.076 M | −1.0 % |
| L6W3 | 6.430 M | 6.372 M | −0.9 % |
| WLlf (login) | 13.690 M | 13.515 M | −1.3 % |
| R2.srv | 1.0713 M | 1.0511 M | −1.9 % |
| `import sandloom.main` | 1.83 s | 1.70 s | −7 % (wall clock, noisy) |

## Related reading

- [Contributor map](../contributing/repository.md)
- [Module boundaries](../concepts/modules.md)
