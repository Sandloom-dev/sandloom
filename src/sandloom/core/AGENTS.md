# Sandloom kernel-contributor contract (`sandloom.core`, `sandloom.sdk`, `sandloom.shared`)

For changes to the framework kernel itself. Module authors never need this file: their
contract is `src/sandloom/AGENTS.md`. The module rules still apply here wherever kernel code
acts like a module (units, effects, tenancy). Concepts: `docs/concepts/index.md`; every check
and refusal: `docs/reference/error-codes.md` (`sandloom.core.error_codes` is its source).

## Layers and imports (ADR-0007)

- **Layers.** `sandloom.core` is layered top (tooling, composition) to bottom
  (configuration): `.importlinter`, checked by `lint-imports` and
  `tests/architecture/test_kernel_import_contracts.py`.
  - A module imports its own layer or a lower one, with no cycle at any depth.
  - `sandloom.core.config` is a leaf and imports nothing above it.
  - `exhaustive`: a new `sandloom.core` child fails until you place it in a layer.
  - No `ignore_imports`: one would be debt needing a `; reason:` and an owner.
- **Function-local imports.** A kernel `# noqa: PLC0415` states its category:
  - `- optional: <reason>`: the dependency may be absent or unconfigured;
  - `- cold: <reason>`: import cost or side effects kept off the app import.

  The reason names a boundary (plugin entry, static command, gunicorn master,
  worker healthcheck, a driver SDK on the app path); a cycle break is not a
  category. A hoisted import is `import sandloom.core.x as _x` so test patches still
  apply. Check: `test_lint_suppression_ratchet.py`.
- **Other `# noqa` markers:** one code per line (`PGH004`, `RUF100`); `BLE001`
  and bandit `S…` need a reason after ` - `; `NOQA_BASELINE` only goes down
  (`docs/contributing/lint-policy.md`).
- **`sandloom.sdk` is the only public surface** (Public API 1.0, ADR-0010). Each
  public module declares `__all__` and `__beginner__`; the root re-exports
  the 29 beginner names. `public_api_baseline.json` records every
  name and signature (SANDLOOM-T008), and each module's generated reference block
  stays current (`sandloom docs --write`). Adding a name is an API decision.
  - **Levels:** public facades > `sandloom.sdk._decl` (L1, declarative) >
    `sandloom.sdk._prim` (L2 primitives) > `sandloom.core` (L3). `_decl` never imports
    `sandloom.core`; `sandloom.core` and `sandloom.shared` never import `sandloom.sdk`; every L2
    name L1 uses is public (`test_l1_uses_only_public_l2`).
  - **Importing `sandloom.sdk` reads no environment and does no I/O**
    (`test_sdk_import_is_side_effect_free`): settings stay lazy.
  - The framework is unreleased: remove a name outright, no compat aliases.

## Guarantees and effects (ADR-0005)

- **The effect model.** Every write-side guarantee is one model: outbox,
  durable inbox, command dedupe, `idempotency_guard` (cache and durable),
  enqueue dedupe, `on_commit` / `before_commit`. See
  `docs/adr/0005-effect-model.md`, `sandloom.core.effects` and `COMMIT_PIPELINE` in
  `sandloom.core.unit_of_work`.
  - **Do not add a new dedupe store or a new post-commit variant.** Reuse one,
    or amend the ADR. Review enforces this; no check can.
- **Guarantee windows are startup-checked** (`sandloom.core.guarantee_windows`,
  stable risk ids). The command TTL must cover the relay horizon, and
  `--inbox-days` must cover stream retention.
- **The core chain is infra-only**: outbox, audit, consumer_inbox and
  contract_command_log (`migrations/core/versions/`, shipped as
  `sandloom/core/migrations/core_chain`). Module tables never go there.

## Configuration (ADR-0006, configuration tiers)

- **Classify every new setting** in `sandloom.core.config.tiers` as semantic or
  ops. Check: `src/sandloom/core/tests/test_config_tiers.py`.
  - The semantic count is ratcheted (ceiling 16) and needs an ADR to grow.
    Prefer an ops knob or a profile-chosen value.
- **A production weakening is an accepted risk, never a boolean.** Add an id
  to `sandloom.core.config.accepted_risks` and read it with
  `settings.accepts_risk(id)`. An unknown id fails startup.
- **Removed settings** (`config_tiers.REMOVED`) fail startup unless they are
  set to their inert value.
- **Read configuration only through `Settings`, never `os.getenv`.** The
  exceptions are `SANDLOOM_COMPOSITION_ROOT` (process env first) and
  `SANDLOOM_WORKER_TASKQUEUE`.

## Infrastructure layer

- **Drivers construct backends.** A backend constructor
  (`create_async_engine`, `redis.from_url`, `Celery(...)`, `Storage(...)`)
  appears only in `sandloom.core.drivers/**`, built lazily by `get_default_*()`
  factories. Check: `test_infra_constructed_only_in_drivers`.
- **SQLAlchemy stays inside the relational surface**: `orm`, `tenant`, `di`,
  `sql_alchemy/`, `projections/upsert`, `migrations/` and the drivers.
  `TYPE_CHECKING` imports are exempt. Check: `test_relational_paradigm_boundary`.
- **Outbox and audit writes go through the `OutboxStore` / `AuditStore`
  contracts** (`provider.outbox()` / `.audit()`). `sandloom.core.di` never imports
  a concrete ORM model.
- **New capabilities ship as contracts only.** Register a `CapabilitySpec`
  with `stability="experimental"`; a driver plugin supplies the backend.

## Kernel conventions

- **The kernel names no module.** Module names come only from `sandloom.core.descriptors.module_names`
  (entry-point name, else the last import segment; an ambiguous name: SANDLOOM-B020).
- **Kernel code reads a request's bearer only through
  `sandloom.core.auth.bearer.bearer_claims(request)`**, which returns access
  tokens only.
- **Route policy is model E** (ADR-0009, `sandloom.core.route_policy`): deny by
  default, everything else derived. The declarations a route may carry
  (`public`, `no_idempotency`, `replay_safe`, `sensitive`, `operation_intent`,
  `writes`, `allow_multi_db_write`) are no-op markers that `create_app` prunes
  from `route.dependant`; `resolve_route_policy` reads them through
  `sandloom.core.route_markers.route_marker_calls`.
- **Startup refusals belong in `create_app` / `enforce_configuration`**, which
  every entry point runs (app, worker, `sandloom migrate`, the `sandloom` CLI). Each
  refusal has an `sandloom.core.error_codes` entry and says what to change.
- **The framework carries no deprecated API.** `error::DeprecationWarning:sandloom`
  turns one into a test failure.
- **Hot paths are measured.** Change the request path, the UoW or eventing
  only with numbers: cachegrind instruction counts on a shared machine
  (`benchmarks/`, `docs/design/*profile*`).
- **The test harness is framework code** (`sandloom.core.testing`, public through
  `sandloom.sdk.testing`). A seam module tests need goes there.

## Before you push

- `uv run sandloom generate`, `uv run sandloom test --full`, then `uv run sandloom test --ci`
  (no ruff fixes) and `uv run python tools/checks/check_architecture.py`, plus:
  - the slow suites: `SANDLOOM_TEST_REDIS_DB_STRIDE=auto uv run pytest -q -n auto --dist loadfile`
    (split conformance included; `sandloom test` will wrap it).
- Run the local CI stand-in from the repo root:
  `tools/ci/ci_local.sh <ref> [--jobs a,b]` (`docs/contributing/local-ci.md`).
- On a shared machine, use your own test DB and Redis (`SANDLOOM_TEST_*`), never
  `pkill` by pattern, and measure CPU only when the machine is idle.
