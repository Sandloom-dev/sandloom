# Changelog — Sandloom (framework + bundled modules)

> Entries before Sandloom's first public alpha describe internal development of the
> predecessor project. They are implementation history, not evidence of a
> published Sandloom release. Current installation and license information is
> in README.md and LICENSE.

All notable changes to Sandloom are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project follows
[Semantic Versioning](https://semver.org/). While the major version is `0`, a
**minor** release may contain breaking changes; they are listed first. The
website renders this same changelog.

## [Unreleased]

### Developer experience

- Make tenancy's initial migration install its own `citext` prerequisite on a
  fresh database, including deployments without identity.
- Fix the standalone CLI handoff for all standard batteries: defer generation
  when app dependencies are missing, print a clear installation note, and run
  generation immediately after `uv sync` in every quickstart. Validate bare,
  identity, tenancy, billing and combined applications in parallel CI jobs.

- Link framework error diagnostics, warnings and offline explanations to their
  exact sections at `sandloom.dev/docs/reference/error-codes/`.
- Update installation instructions, README badges and examples to the current
  published alpha, `0.7.0a2`.

### CI and publishing

- Start full serial isolation without a change-classification barrier, and
  avoid repeating additional audited child-only probes in that pass. Keep
  every in-process isolation case and all full-matrix tests. Record fixture
  measurements and primary-source comparisons of mature projects' CI.

- Use reviewed Node.js 24 Actions, with a single uv cache writer per Python key.
- Speed up child-server readiness probes and close the conformance matrix at
  the end of its test module. Avoid repeating child-process-only probes in the
  serial isolation pass; the full compatibility matrix still runs them.
- Create GitHub Releases after successful PyPI verification, using the exact
  reviewed archives, changelog notes, source commit and SHA-256 manifest.

## [0.7.0a2]

### Publishing

- Prepare all four `0.7.0a2` packages and update the generated-application baseline.
- Publish through GitHub OIDC after full validation and explicit owner approval.
  Reuse the exact validated archives, retain their hashes, resume matching partial
  uploads and verify the final PyPI files. Manual runs default to dry-run mode.

### Package metadata

- Refresh PyPI descriptions for the framework and its identity, tenancy and billing
  modules, including the AI-driven positioning, pluggable infrastructure and
  deployment model. Description changes take effect with a new PyPI release.

### CI

- Add one required merge check covering the suites selected for each PR. Run
  documentation, packages, architecture, admin and dependency review through
  the main workflow without duplicate PR triggers. Failed, cancelled or
  unexpectedly skipped checks block merging; manual full validation stays opt-in.

### Documentation

- Update contributor instructions, installation, package descriptions and
  generated application READMEs to reflect the published alpha. Clarify the
  build-only release workflow and branch-protection verification procedure.

## [0.7.0a1] — 2026-10-28

Initial public alpha release of `sandloom`, `sandloom-identity`,
`sandloom-tenancy` and `sandloom-billing`. Dependencies and generated apps
explicitly admit the alpha release. Earlier headings describe internal versions.

### Fixed

- Remove compromised emSign CA trust: require `certifi>=2026.10.19`, use
  `uv>=0.13.4` in tooling/builds and Python 3.12.16 in the production image.
- Contract DTO copies preserve matching validation/serialization aliases and
  distinguish models and enums with the same name across provider submodules.
- Automatic module discovery refuses ambiguous providers before registration.
- A fresh reviewed source export passes its own selection check without a commit.

### Breaking — `sandloom generate`; contract DTOs authored in the provider (2026-10-28)

- `sandloom generate [module ...]` writes every generated file: module, route
  and contract snapshots, contract stubs, DTO copies and exported event
  schemas, after the compatibility checks (an incompatible change, SANDLOOM-T001
  / T004 / T005 / T007 / C012, writes nothing; a weaker route policy is still
  approved only by `sandloom routes snapshot --write`). `sandloom test` never
  writes, in dev or with `--ci`: a stale file fails with "generated files are
  stale: run `sandloom generate`". Every `sandloom add` generator and
  `sandloom new` run `sandloom generate` last.
- A provider authors its contract DTOs (pydantic models, Enums) in its own
  `schemas` module. `sandloom generate` writes a wire-shape copy of each one a
  signature names into the shared DTO package (`<shared>/contracts/<module>_dto.py`,
  marked `@dto_copy`); the stub names the copy and carries its Protocol
  (`ContractRef("x.y.v1", Protocol)`). A `Contract(REF)` caller receives the
  copy, locally and over RPC; the provider receives its own class. A copy
  carries fields, types, constraints, defaults, aliases, Literal / Enum values,
  nested models and `model_config`; not methods or validators.
- SANDLOOM-C012 is restated: refused are a DTO outside the closed set of wire
  shapes or one a copy cannot carry (at `@contract`), another module's or a
  function-local DTO (at registration), a missing copy (`sandloom check`) and a
  stale or hand-edited copy (`sandloom test`). `sandloom add contract` scaffolds
  the DTO in the module's `schemas.py`.
- Contract DTO dumps on the wire use each field's alias (`by_alias=True`), so
  an aliased DTO round-trips.
- `sandloom.shared.schemas` is removed (no shims): `UserContact` moved to
  `sandloom_identity.schemas.contact` (callers: `sandloom.shared.contracts.identity_dto`),
  `MemberState` to `sandloom_tenancy.schemas.membership` (callers:
  `sandloom.shared.contracts.tenancy_dto`), the member-event ordering rule to
  `sandloom.shared.membership` and `UserContext` to `sandloom.shared.subject`.

### Breaking — module ecosystem distributions (2026-10-28)

- Eight distributions became four. `sandloom` is the framework: the kernel,
  `sandloom.sdk`, `sandloom.shared` (framework DTOs, guards, authz), the CLI
  and the composition root `sandloom.main`. `sandloom-identity`,
  `sandloom-tenancy` and `sandloom-billing` are ecosystem module packages.
  `sandloom-framework`, `sandloom-shared`, `sandloom-platform`,
  `sandloom-infra-redis` and `sandloom-infra-celery` are removed (no shims):
  depend on `sandloom[redis,celery]` or `sandloom[standard]` instead.
- Extras of `sandloom`: `redis`, `celery`, `standard` (Redis + Celery + the
  three modules, the former platform), `fast`, `check`, `testing` (pytest,
  pytest-asyncio, pytest-xdist for the test kit) and `sentry` (imported only
  when `SENTRY_DSN` is set). Third-party dependencies are ranges
  (`>=tested,<next breaking release`); exact versions live only in `uv.lock`.
  `requests` and `python-multipart` are dropped; `itsdangerous` and
  `email-validator` moved to the module packages that use them.
- The bundled modules moved to top-level import packages: `sandloom.identity`
  is `sandloom_identity` (source `src/sandloom_identity/`), likewise
  `sandloom_tenancy` and `sandloom_billing`.
- Modules register in the `sandloom.modules` entry-point group (formerly
  `sandloom.plugins`), e.g. `identity = "sandloom_identity"`. The entry-point
  name is the module name (tables `identity_*`, migration chain
  `alembic_version_identity`, channels and contracts `identity.*`); any other
  module's name is the last segment of its import path. `SANDLOOM_MODULES`,
  `MODULE_REQUIRED` and deploy profiles accept either the name or the import
  path. `MODULE_LOAD_ENTRYPOINTS=true` loads every installed module package.
- `sandloom.main:app` composes every installed `sandloom.modules` entry point
  when `SANDLOOM_MODULES` is unset; `DEFAULT_MODULES` is removed and the kernel
  names no module. `MODULE_FS_SCAN` is removed; setting it to true fails
  startup.
- A requested module name with two providers (two installed packages, or an
  installed package and an app-local module) fails startup with
  SANDLOOM-B020, naming both.
- `sandloom.shared` is unchanged and ships whole inside the `sandloom`
  distribution: the hand-written contract DTOs (`schemas/`), the generated
  stubs and event schemas of the standard modules (`contracts/`, `events/`),
  `authz/` and `guards/`. Every process installs `sandloom`, so every caller
  imports the DTOs (SANDLOOM-C012). An app's `<app>.shared` and
  `CONTRACT_DTO_PACKAGES` are unaffected.
- `sandloom new <app> [--with identity,tenancy,billing]`: the app depends on
  `sandloom[redis,celery]>=0.7,<0.8` plus `sandloom-<module>>=0.7,<0.8` per
  module; `--framework-source <path>` adds editable sources for exactly those
  distributions.
- `sandloom check`: an app module importing an installed module package, in
  any spelling, is SANDLOOM-C001. The framework tree's manifest uses
  top-level module packages (`modules_path: src`, `modules_package: ""`), the
  layout of a third-party module repository.
- `tools/packaging/check_package_distributions.py` (CI: `packages.yml`) builds
  the four wheels, checks import layering across distributions, installs
  `sandloom` alone to run every CLI subcommand, and composes the modules from
  `sandloom[standard]`. The default Docker image tag is `sandloom:local`.
- Module distributions version independently, starting at 0.7.0, and depend
  on `sandloom>=0.7,<0.8`. Release tags: `sandloom-vX.Y.Z` and
  `sandloom-<module>-vX.Y.Z`.

### Breaking — Sandloom rename

- The framework is now **Sandloom**. Python imports use `sandloom`, the CLI is
  `sandloom`, and distributions use the `sandloom-*` prefix (including
  `sandloom-framework` and `sandloom-platform`). Replace the former `aja` names
  in applications and reinstall their environment from the updated dependencies.
- Environment variables, diagnostic codes, metrics, RPC headers, Redis keys,
  PostgreSQL tenant settings and internal endpoints use the new Sandloom prefix.
  The rename includes RPC wire tags and cryptographic domain labels;
  rebuild all services together and recreate development databases/caches. Do not
  mix builds from before and after the rename. No compatibility aliases ship.
- The deployment model, module boundaries, transaction ownership, tenancy and
  public API behavior are unchanged.
- Documentation uses **Zensical** with `zensical.toml`, native search and native
  `llms.txt` / Markdown exports. Build or preview with `tools/docs/docs_site.py`.
  CI builds a documentation artifact; deployment is a separate operator action.
- The owner console uses the Sandloom mark, Manrope / IBM Plex Mono typography
  and a sand, forest and clay palette in both light and dark themes.


### Added — Public API 1.0, hand-off H0 (ADR-0010)

- `sandloom.sdk`: the public namespace skeleton (spec §2.2). Every existing name
  of the capability table is re-exported from its kernel home; the names new
  in 1.0 are typed stubs frozen to the §2.3 signatures that raise
  `NotImplementedError` naming their hand-off. Layers `_decl` (L1) and
  `_prim` (L2) with import-linter contracts (`sdk-kernel-surface`,
  `kernel-never-imports-sdk`, `sdk-decl-l1`, `sdk-layers`). The `sandloom.sdk`
  modules are public alongside today's (`architecture_rules.yml`,
  `public_api_baseline.json`).
- Lazy settings: importing the framework, `sandloom.sdk` or a bundled module reads
  no environment and builds no `Settings`; entry points force them
  (`load_settings()`, `enforce_configuration()`). `SANDLOOM_JSONSCHEMA_BACKEND` is
  now the ops field `Settings.jsonschema_backend` (read at schema compile).
  `tools/checks/import_audit.py` and `test_sdk_import_is_side_effect_free`.
- `sandloom.core.loader.manifest.AppManifest` (spec §5.7): the loader records one
  `ModuleManifest` per loaded module (`ModuleLoadSummary.manifests`);
  `create_app` publishes `app.state.app_manifest`.
- Error catalogue: every code of spec Appendix A, reserved with
  `ErrorCode.planned` naming the hand-off that implements its check
  (`sandloom explain` says so).
- `tools/packaging/public_api_migration.py`: the Appendix C scanner (no unmapped
  name), run by the architecture suite.

### Added — sagas (ADR-0008, phase 0 + phase 1)

- `sandloom.core.sagas`: a small orchestrator over the existing effect
  primitives. Definitions in `sandloom.core.descriptors` (re-exported by
  `module_api`): `Saga[InputT]`, `SagaContext[InputT]`, `step`,
  `local_step`, `StepKind`; `core.register_saga(DEF)` (adds the called
  contracts to the module's consumes; checked against the providers'
  describe documents at startup); `start_saga` / `saga_status` /
  `SagaHandle` / `SagaInputConflict` / `operation_key` in `sandloom.core.di`.
- Core migration `0010_saga_state`: `saga_instances` + `saga_steps` (tenant
  RLS with system access under `tenant_rls`). `sandloom-migrate extract-module`
  copies a module's instances.
- One advance consumer per saga module (`<module>:sagas@advance`), a
  lease-guarded timer sweep per database (`sandloom.core.beat_lease.FencedLease`,
  extracted from the beat lease), the closed outcome table
  (`sandloom.core.sagas.outcomes`), resolve-before-compensate, parking (metric +
  `core.saga.parked` audit row). Ops settings: `SAGA_SWEEP_INTERVAL_SECONDS`,
  `SAGA_SWEEP_BATCH`, `SAGA_COMPENSATION_DEADLINE_SECONDS`,
  `SAGA_RETRY_BASE_SECONDS`, `SAGA_RETRY_MAX_SECONDS`, `SAGA_RETENTION_DAYS`.
- Guarantee window / accepted risk `saga_horizon_beyond_command_dedupe`.
- `sandloom sagas list|show|retry|park|resolve`, an `sandloom doctor` `sagas` section,
  `/diagnostics/core` lists the definitions, metrics `sandloom_saga_*` and the
  `SandloomSagaParked` alert.
- `saga_harness` pytest fixture (`sandloom.core.testing.sagas`) and
  `sandloom-scaffold saga <module> <name>`.
- `examples/academy`: the worked course-enrollment saga with a
  split-conformance case. `benchmarks/ladder/saga_rung.py`.
- `sandloom.core.descriptors` is a package now (`descriptors/__init__.py` +
  `descriptors/sagas.py`); its public names are unchanged.

### Breaking (kernel internals only) — kernel layers and import contracts (ADR-0007)

- The kernel is layered and acyclic, checked by import-linter
  (`.importlinter`, `lint-imports`, pinned in the dev extras; run by
  `tests/architecture/test_kernel_import_contracts.py` and pre-commit).
  The documented public API (`sandloom.core.di`, `facade`, `module_api`,
  `descriptors`, `orm`, `projections`, `testing`, `contract_testing`,
  `sandloom.shared.*`) is unchanged; `public_api_baseline.json` and the contract
  snapshots are unchanged.
- Internal modules moved WITHOUT re-exports at the old paths (code importing
  kernel internals must follow): `sandloom.core.infra.drivers.*` /
  `infra.bootstrap` → `sandloom.core.drivers.*` / `drivers.bootstrap`;
  `eventing.consumers` / `eventing.runtime` → `consumers.callbacks` /
  `consumers.runtime`; `eventing.versioning` → `unit_of_work.versioning`;
  `eventing.transport` removed (`PoisonEventError` etc. in
  `eventing.delivery`); `migrations.migrations_cli` / `migrations.extract`
  → `cli.migrate` / `cli.extract` (`sandloom-migrate` entry point updated);
  `migrations.rls` → `sql_alchemy.rls`; `sandloom.core.database` removed (`Base`
  via `sandloom.core.orm`); `sql_alchemy.testing` → `testing.database`;
  `http.client_ip` / `http.rate_limit` → `ingress.*`;
  `loader.service_registry` → `module_api._service_registry`.
- `unit_of_work`, `idempotency`, `facade`, `sql_alchemy.planner_adapter`
  and `sql_alchemy.outbox_store` are packages split by responsibility; their
  package modules no longer re-export submodule names. Patch a kernel global
  in the module that defines it.
- Removed aliases: `idempotency._ArmedKey`, `ingress.rate_limit._client_ip`,
  `migrations.migrations.ensure_outbox_table`, `infra.config.CAPABILITIES`,
  `consumers.callbacks.ALLOW_VOLATILE_CONSUMERS_ENV`.
- The non-production default-registry fallback is gone (`sandloom.core.infra.defaults`
  removed): `di.cache()` / `di.notifier()` and every capability accessor with
  no bound registry raise in every environment, with a hint (bind one via
  `create_app()`, `sandloom.core.composition.load_composition_core()` or the
  `sandloom.core.testing` harness). `.importlinter` has no `ignore_imports`; the
  kernel has no import cycle.
- Logger names follow the new module paths for modules that log under
  `__name__` (`sandloom.core.drivers.redis_cache`,
  `sandloom.core.drivers.sqlalchemy_database`, `sandloom.core.consumers.runtime`).

### Breaking — every compatibility path removed (no deprecation period)

During this internal development milestone, old behaviour was removed instead
of kept behind modes,
aliases or warnings. Replacements: `docs/guides/upgrading-0.8.md`, section
"Removed without a deprecation period (0.8)".

- The accepted-risk boolean aliases listed in the ADR-0006 entry below
  (`BILLING_ALLOW_FAKE_PSP_IN_PROD`, `NOTIFIER_ALLOW_LOG_IN_PROD`,
  `STORAGE_ALLOW_LOCAL_IN_PROD`, `SANDLOOM_ALLOW_VOLATILE_CONSUMERS`,
  `SANDLOOM_ALLOW_UNVERSIONED_DDL_REPAIR`, `PASSWORD_HASH_ALLOW_WEAK`,
  `JWT_ALLOW_SHARED_SECRET_SPLIT`) are removed settings, refused unless set
  to their inert value: `SANDLOOM_ACCEPTED_RISKS` is the only way to accept a
  production risk. The *migration* tier is gone (tiers are semantic + ops);
  semantic 16, ratchet ceiling 16.
- Internal RPC has one authentication, the ADR-0004 pair MAC (+ the
  caller's Ed25519 signature on commands, `RPC_ALLOWED_CALLS`):
  `RPC_AUTH_MODE` and `RPC_RESPONSE_BINDING` are removed settings (inert
  values `service_mac` / `strict`). Every `SERVICE_BINDINGS` entry pins
  `"service_id"`; a process with bindings must hold `RPC_DH_PRIVATE_KEY`;
  responses are always keyed (an unkeyed error answer is `503
  RemoteServiceUnavailable` with the provider's code). `INTERNAL_API_TOKEN`
  and `INTERNAL_ENDPOINTS_OPEN_IN_DEV` never open `/_sandloom/rpc`.
- Contracts: undecorated session-taking providers and session arguments
  are refused (`@contract_method` + `contract_session()` only);
  `rpc_idempotency_key` is removed from `sandloom.core.di`.
- `AccessFact` `claim_interpret` / `ClaimInterpreter` removed — use
  `claim_rule=ClaimRule...`.
- An access token without `iat_us` is revoked by any revocation mark of its
  subject (no whole-second comparison).
- Event consumers no longer adopt pre-namespacing group names (groups are
  `<module>:<name>` or an explicit `group=`).
- RLS: `upgrade_tenant_rls` and the legacy bypass-policy detection removed.
- `ensure_outbox_table` alias removed (`ensure_core_infra_tables`).
- Identity: no legacy SHA-256 Fernet key fallback for TOTP secrets, no 8-hex
  backup codes. Tenancy list endpoints: the `after` query parameter is
  removed (`cursor` + `X-Next-Cursor`).
- The `sandloom.core` package re-exports (`from sandloom.core import settings`, ...)
  are removed.

### Changed — kernel package splits (no behaviour change)

- `sandloom.core.security`, `sandloom.core.auth_freshness`, `sandloom.core.security_recovery`
  → package `sandloom.core.auth` (`hashing`, `tokens`, `bearer`, `marks`,
  `revocation`, `local_marks`, `verification`, `freshness`, `blocks`,
  `recovery`).
- `sandloom.core.settings` → package `sandloom.core.config` (`config.settings` with
  per-domain sections, `config.tiers` (was `config_tiers`),
  `config.accepted_risks`). The config layer imports nothing above it; the
  checks that need kernel code (asymmetric JWT key material, internal-RPC
  keys / bindings / allowlist, `multi_tenant_strict` refusals) run at boot in
  `sandloom.core.app_factory.enforce_configuration`, called first by
  `create_app` and reported by `sandloom doctor` as `config/boot`.

### Breaking — accepted risks replace prod opt-outs; single-valued flags removed (ADR-0006)

- New `SANDLOOM_ACCEPTED_RISKS` (JSON or comma list of stable risk ids,
  `sandloom.core.accepted_risks`): the one place a deployment accepts a weaker
  guarantee. Ids: `fake_psp_in_prod`, `log_notifier_in_prod`,
  `local_storage_in_prod`, `volatile_event_bus`, `unversioned_ddl_repair`,
  `weak_password_hash`, `shared_hs_secret_split`, `relay_disabled`. An
  unknown id fails startup; every accepted risk in effect is logged at every
  start, exported as `sandloom_accepted_risk_active{risk}` (and
  `sandloom_config_override_active{kind="accepted_risk"}`), listed by
  `/diagnostics/core` (`configuration.accepted_risks`) and `sandloom doctor`.
- Deprecated (removed in 0.9), still honoured as aliases of their risk id:
  `BILLING_ALLOW_FAKE_PSP_IN_PROD`, `NOTIFIER_ALLOW_LOG_IN_PROD`,
  `STORAGE_ALLOW_LOCAL_IN_PROD`, `INFRA.eventbus.<name>.allow_volatile`,
  `SANDLOOM_ALLOW_VOLATILE_CONSUMERS` (outside prod), `SANDLOOM_ALLOW_UNVERSIONED_DDL_REPAIR`,
  `PASSWORD_HASH_ALLOW_WEAK`, `JWT_ALLOW_SHARED_SECRET_SPLIT`.
- `OUTBOX_RELAY_ENABLED` is semantic: `off` in a prod app process is refused
  at startup unless `relay_disabled` is accepted. Gunicorn non-leader
  workers now run with the new value `delegated` (was `off`).
- Removed, with their code paths: `LEGACY_UNSCOPED_EFFECTS`, `DB_LAZY_BEGIN`
  (`sandloom.core.sql_alchemy.lazy_begin` is now `read_only_policy`), the
  `INFRA.database.<name>.verify_roles` option, `SCHEMA_VALIDATION_SOFT`
  (off-schema events are refused in every environment), `MODULE_REQUIRE_LISTED`,
  `BEAT_LEASE_ENABLED`, `CONTRACT_READ_SCOPE_MEMO`,
  `BILLING_FAKE_ALLOW_UNSIGNED_WEBHOOKS` (and `FakeStripeClient`'s
  `allow_unsigned_webhooks`). A removed setting still set to its inert value
  logs a warning; any other value fails startup.
- `BILLING_DENY_ON_PAST_DUE` moved to the ops tier (product policy).
- Tiers: semantic 30 → 18 (ratchet ceiling 18), migration 3 → 8 (the
  aliases), ops 179 → 179. Upgrade steps: `docs/guides/upgrading-0.8.md`.

### Added — durable HTTP idempotency and guarantee windows (ADR-0005 amendment)

- `idempotency_guard(..., durable=True)`: the record is claimed in the
  request's own `DbSession` Unit of Work (new core table
  `http_idempotency_log`, migration `0009_http_idempotency_log`, additive)
  and completed with the response right before `COMMIT`, so it commits iff
  the endpoint's effect commits: replay of the stored response, `422` on a
  reused key, `409` at once while in flight, no record after a rollback, a
  returned `5xx` never replayed, an ambiguous COMMIT never double-applies.
  Third adapter of the shared dedupe log; expired records are purged by the
  relay housekeeping / `sandloom outbox purge` and copied by `extract-module`.
- **Breaking (startup check)**: `create_app` refuses an
  `operation_class("F")` route whose idempotency guard is the
  response-storing cache form or fail-open (`FinancialGuardError`). Use
  `durable=True`, or `store_response=False` + `fail_closed=True` when the
  endpoint owns its durable dedupe record. The bundled F routes already
  comply (no change).
- `sandloom.core.guarantee_windows`: production refuses (other environments
  warn) `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` below the relay's redelivery
  horizon; `sandloom outbox purge --inbox-days` refuses a cutoff inside
  `EVENT_STREAM_RETENTION_SECONDS` + 1 day unless `--force`. Each problem has
  a stable risk id (`command_dedupe_below_retry_horizon`,
  `inbox_purge_below_redelivery`).
- ADR-0005 wording: at-most-once (was best-effort) for `on_commit` /
  unscoped publish / `required=False` audit; exactly-once-effect scoped to the
  recording database; replay-only = response dedupe; ordering, the
  ambiguous-COMMIT table and the stage-3 hand-off documented.
  `effects.Guarantee.AT_MOST_ONCE` (`BEST_EFFORT` is now its alias).

### Changed — one effect model (ADR-0005)

- `docs/adr/0005-effect-model.md` defines effects, boundaries, guarantee
  levels, the dedupe record and the commit pipeline, and maps every
  mechanism (outbox + relay, durable / cache consumer inbox, command dedupe,
  `idempotency_guard`, enqueue dedupe, `on_commit`, `before_commit`,
  `revocation_at_commit`, `record_audit`) to boundary x guarantee.
- New `sandloom.core.effects` (guarantee levels, record statuses, bounds, key /
  args digests, replay encoding) and `sandloom.core.sql_alchemy.dedupe_log`: the
  one relational claim / complete / replay / purge implementation.
  `durable_inbox` (`consumer_inbox`) and the contract command log
  (`contract_command_log`) are now adapters over it — same tables, same SQL,
  same public surface. The cache dedupe forms take their bounds, tags and
  digests from `sandloom.core.effects`.
- `unit_of_work.COMMIT_PIPELINE` documents the stages every UoW ends through
  (before_commit -> COMMIT -> outbox dispatch -> on_commit); the test seams
  (`get_db` override, `sandloom.core.testing.run_on_commit`) now run the same
  stage function instead of re-assembling it.
- The ADR-0005 rule for new effect mechanisms is enforced in review; a
  heuristic `sandloom check` rule (`effect-primitives`) was tried and removed —
  `sandloom check` keeps only deterministic rules.

### Added — configuration tiers and profile-driven semantic settings

- Every setting is classified in `sandloom.core.config_tiers` as **semantic**
  (changes a guarantee: 30, incl. two `INFRA` driver options), **ops**
  (capacity / operations: 179) or **migration** (transitional, with a removal
  version: 3). A new `Settings` field fails `test_config_tiers.py` until it is
  classified; the semantic count is ratcheted (it may only go down, a new one
  needs an `adr=` reference).
- The semantic settings' effective values are defined per profile (`dev`,
  `test`, `prod`, `prod` + `PRODUCTION_PROFILE=multi_tenant_strict`) in one
  table, generated as `docs/reference/configuration-tiers.md`. The table is
  today's behaviour — no default, refusal or runtime decision changes.
- An *override* (a semantic setting moved towards its weaker end, a
  deprecated value, an active migration flag) is now visible: a WARNING at
  every production start (INFO for a deliberate dev opt-in), the gauge
  `sandloom_config_override_active{setting,kind}`, `configuration.overrides` in
  `/diagnostics/core`, and one `config/override:<setting>` check in
  `sandloom doctor` (WARN in prod: exit code 1).
- Migration flags carry `removed_in` and a test fails once the package
  version reaches it: `LEGACY_UNSCOPED_EFFECTS`, `DB_LAZY_BEGIN` and the
  `INFRA.database.<name>.verify_roles` option are due in 0.8.

### Deprecated — RPC auth modes `shared_token` / `dual`, `RPC_RESPONSE_BINDING=permissive`

- Review-map D-14: `RPC_AUTH_MODE=shared_token` and `dual`, and
  `RPC_RESPONSE_BINDING=permissive`, are removed in 0.9 (target:
  `service_mac` + `strict`). A process that calls internal RPC
  (`SERVICE_BINDINGS`) — or, in prod, serves it with `INTERNAL_API_TOKEN` —
  logs `DEPRECATED: … is removed in 0.9` at startup, in every environment
  (previously only `shared_token`, only in prod). It is a logged warning plus
  the gauge above, deliberately NOT a Python `DeprecationWarning`: those are
  errors in the framework and app test suites (`filterwarnings`), so a
  default-config app would fail its tests on an operator decision.
- `sandloom.core.app_factory.warn_shared_token_rpc` is replaced by
  `warn_configuration_overrides` (returns the overrides it logged).

### Changed — `sandloom.core.rpc` is a package (internal, behaviour-neutral)

- The 3,200-line `sandloom/core/rpc.py` is split into `sandloom/core/rpc/`: `proxy`,
  `client`, `outbound` (caller), `server`, `inbound_auth`, `dispatch`
  (provider), `context`, `binding`, `envelope`, `wire`, `errors` (both
  sides) and `readiness`. Nothing changes on the wire; every
  `from sandloom.core.rpc import X` keeps working.
- Tests that monkeypatch an RPC internal patch it where it is looked up
  (`sandloom.core.rpc.proxy._rpc_client`, `sandloom.core.rpc.server._unpack`, …):
  setting such a name on the `sandloom.core.rpc` package itself raises
  `AttributeError` naming the right target, instead of silently patching
  nothing.

### Fixed — read scope recovers from an aborted shared transaction

- A local read contract that swallowed a database error left the request's
  shared read connection (`ContractReadScope`) in an aborted transaction, so
  every later local read contract on that database in the same request
  failed with "current transaction is aborted" (SQLSTATE `25P02`). The scope
  now drops that connection when a call on it hits `25P02` and reruns the
  call in its own Unit of Work (what it would have done without the scope);
  later calls share a fresh connection. Counted as `stats["replaced"]`.

### Changed — `sandloom.core.module_api` is a package

- `module_api.py` is split into internal submodules (`sandloom.core.module_api._*`:
  registration, `define_module`, contract method / runtime / dedupe, read
  scope, schema, provider). The public surface is unchanged —
  `from sandloom.core.module_api import X` works for every previous `X`. Business
  modules must not import the `_` submodules (`architecture_rules.yml`
  `forbidden_patterns`).

### Added — opt-in idempotency cache instance (write-path p99)

- `IDEMPOTENCY_CACHE_INSTANCE` (default empty = unchanged: records stay in
  `SECURITY_CACHE_INSTANCE`) moves idempotency records — `idempotency_guard`
  reservations / replays / releases, task-enqueue dedupe
  (`taskqueue().enqueue(..., idempotency_key=...)`) and the cache fallback of
  contract-command dedupe — to their own named cache instance. Revocation
  marks, emergency blocks and the RPC replay guard stay in the security
  cache. Rationale: on a security Redis with `appendfsync always` the
  idempotency writes' fsyncs queue the hot revocation-mark reads
  (`docs/design/write-login-profile-2026-10-22.md`). Run the instance with
  the security Redis's durability (`appendfsync always`, `noeviction`);
  `fail_closed=True` guards still answer `503` when it is down. New
  accessors `sandloom.core.infra.access.idempotency_cache()` /
  `idempotency_cache_name()`.
- `PRODUCTION_PROFILE=multi_tenant_strict`: a set idempotency instance must
  be declared in `INFRA["cache"]` on a Redis server other than the default
  cache's (`Settings` validator; `default` is refused, sharing the security
  Redis's server warns) and is attested as a separate Redis process at
  startup, like the security cache.
- Both compose profiles carry a commented-out `idempotency-redis` service and
  env block; `deploy/README.md` → "Separate idempotency Redis (opt-in)".
  `sandloom backup plan` labels the idempotency Redis's role.

### Added — operator tooling (review map H-10, C-05, H-22, H-08)

- **Beat singleton lease** (`sandloom.core.beat_lease`): the framework beat
  (`celery -A sandloom.core.worker beat`, and `worker -B`) schedules only while it
  holds a fenced lease `sandloom:beat:lease:<scope>` in the cache (`SET NX` + TTL,
  renewed by compare-and-set on its own token every TTL/3). A second beat on
  the same broker and module set stands by and takes over when the holder
  releases or dies; a holder that lost its token stops at once. New settings
  `BEAT_LEASE_ENABLED` (default `true`), `BEAT_LEASE_TTL_SECONDS` (30),
  `BEAT_LEASE_NAME`, `BEAT_LEASE_CACHE_INSTANCE`. **Behaviour change:** beat
  refuses to start (exit 1) when that cache lacks the `cas` feature; set
  `BEAT_LEASE_ENABLED=false` to opt out.
- `sandloom-migrate upgrade-all|upgrade-core|upgrade-module --lock-timeout SECONDS`
  bounds the wait for a concurrent run's advisory lock (default
  `MIGRATION_LOCK_TIMEOUT_SECONDS`). While waiting the runner logs the holder
  (pid, `application_name`, user, client, query start — `pg_locks` +
  `pg_stat_activity`) every 5 s, and the timeout error names it. Runners now
  connect with `application_name='sandloom-migrate'`. `migration_lock()` takes
  `timeout=`.
- `sandloom config dump [--json] [--only PREFIXES] [--non-default]`: the effective
  settings with the source of each value (env / dotenv / secrets_dir /
  default); credentials redacted by name and type, URL passwords masked,
  recursively inside `INFRA` / `SERVICE_BINDINGS`. `--show-secrets` is
  refused with `ENVIRONMENT=prod`.
- `sandloom doctor [--json] [--skip SECTIONS] [--timeout S] [--lag-warn N]`:
  read-only preflight — config sanity (incl. `multi_tenant_strict`), the
  composition root and consumed-contract bindings, health of every
  configured infra instance, applied vs code migration heads per database
  (`sandloom.core.migrations.migrations.migration_status`, read-only
  connections), Streams consumer groups (pending / lag) and RLS role /
  coverage checks. Exit `0` ok, `1` warnings, `2` failures; never prints a
  secret. Runbook: `docs/operations/operator-reference.md` → Preflight and effective
  configuration.

### Added — supply-chain gates (review map A-30)

- CI job `supply-chain` (`tools/checks/supply_chain.sh`, also runnable via
  `tools/ci/ci_local.sh --jobs supply-chain`): CycloneDX SBOM from `uv.lock` as
  the `backend-sbom` artifact, plus a PR dependency-review workflow. SAST is
  covered by ruff `S`. See `docs/contributing/supply-chain.md`.
- Redis script SHA-1 marked `usedforsecurity=False` (bandit B324; no behaviour change).

### Changed — static analysis ratchet (ruff)

- ruff now also enforces `PLC0415`, `BLE`, `S` (bandit), `ASYNC`, `SIM`,
  `PERF`, `C901` (max-complexity 10), `RUF` (incl. RUF100: dead `# noqa`
  fails) and `PGH004` (no blanket `# noqa`). Existing violations are
  baselined as per-line markers; behaviour is unchanged.
- The 395 `# noqa: WPS433` markers (wemake-python-styleguide never ran) are
  now `# noqa: PLC0415`; 167 dead markers of tools that never run (ANN, SLF,
  A, N, D, PT, WPS…) or that suppressed nothing are gone, their reasons kept
  as plain comments. Every `BLE001` and bandit marker carries a one-line
  reason.
- New architecture test `tests/architecture/test_lint_suppression_ratchet.py`:
  the number of markers per ratcheted code may only go down, and the
  config-level ignores of those rules are pinned.
- The kernel package `sandloom/core/migrations` (migration runner, `sandloom-migrate`,
  `sandloom backup`, `extract-module`) is linted and formatted for the first time —
  the `src/sandloom/*/migrations` exclude meant for generated revisions also
  matched it; only `*/migrations/versions` is excluded now.
- Safe simplifications applied (collapsed ifs, a ternary, explicit and/or
  parentheses, literal unpacking, `_`-prefixed unused unpacked names).
- Policy and how to pay suppressions down: `docs/contributing/lint-policy.md`.

### Changed — pool checkout fairness (Oracle 2026-10-23 read tail)

- `LingeringQueuePool` fair checkout now uses **bounded barging**
  (`DB_POOL_FAIR_BARGE_MS`, default 25): while the oldest waiting checkout
  has waited less than the bound, a returned connection goes back to the
  idle queue and wakes that waiter, and a caller already running may take it
  first. Past the bound the connection is handed straight to the waiter and
  newcomers queue, so starvation stays bounded. `0` restores the strict FIFO
  handoff of 2026-10-22, whose reserved connections idled until their
  waiter's task resumed (mean 6.7 ms on Oracle). Timeout, cancellation,
  room-wake-up and `sandloom_db_pool_checkout_waiters` semantics are unchanged.
  `tools/checks/bench_pool_checkout.py --e2e` adds an end-to-end stand (real CPU,
  `--cpu-clock`, `--stall-ms`). Re-measure on Oracle before relying on it:
  `docs/design/pool-checkout-fairness-2026-10-22.md`.

### Garbage collector tuning for server processes

- **Behaviour change (default on):** gunicorn workers (`sandloom.core.cli.gunicorn_conf`)
  and the Celery worker (`sandloom.core.worker`) now configure CPython's cyclic GC
  once per process (new module `sandloom.core.gc_tuning`):
  - gunicorn workers: when the app reports `lifespan.startup.complete`, before
    the worker accepts traffic.
  - Celery: in the parent before the pool forks.
  - The configuration: `gc.collect(); gc.freeze()` (`GC_FREEZE_AFTER_STARTUP`,
    default `true`), then `gc.set_threshold(2000, 10, 10)` (`GC_THRESHOLDS`,
    default `2000,10,10`, CPython 3.13+'s own default; empty = interpreter
    default; skipped on CPython 3.14.0–3.14.4).
  - Local stand (identity + bench, 2 workers, Python 3.12): a full collection
    drops from ~35 ms to ~2 ms of CPU, GC from 4–6% to ~0.2% of worker CPU,
    and the median calm-host client p99 from 48 ms to 12–14 ms.
  - `create_app` itself never touches the collector.
  - Restore the old behaviour with `GC_FREEZE_AFTER_STARTUP=false GC_THRESHOLDS=`.
  - Details: `docs/design/gc-tuning-2026-10-23.md`, `docs/operations/operator-reference.md`
    → *Garbage collector tuning*.
- **New metrics** (`GC_METRICS_ENABLED`, default `true`, gunicorn workers):
  - `sandloom_gc_pause_seconds{generation}` (histogram)
  - `sandloom_gc_collected_objects_total{generation}`
  - `sandloom_gc_frozen_objects`
  - The `gc.callbacks` hook never calls prometheus_client inside a collection
    (its value lock is not reentrant). It fills a preallocated ring that the
    event loop flushes once per second.
- **New benchmark stand** `benchmarks/ladder/gc_stand.py` (+ `gc_stand_conf.py`,
  `gc_stand_report.py`): the shipped gunicorn config with a per-worker GC pause /
  thread-CPU probe under open-loop load.

### Enforced promises (review map A-03, A-23, I-19)

- **Breaking (startup):** `create_app` refuses (`GuardOrderError`) a route
  whose `idempotency_guard` FastAPI solves before its authenticating
  dependency — the guard would reserve / replay for a caller the route has not
  authenticated. Declare the guard after the auth (an endpoint parameter after
  `Depends(get_current_user_id)`). Authenticating dependencies are
  `sandloom.shared.authz.current_context` (so `get_current_user_id`, `require(...)`,
  `authorize_resource`) and the platform console's operator guard; mark a
  custom one with `@authenticates` (`sandloom.core.di`). Fixed in the `helpdesk`
  example (ticket create) and the benchmark `/dw/7` route.
- **New `sandloom check` rule `tenant-scoped-models`:** an ORM model or Core
  `Table` declaring `tenant_id` must inherit `TenantScoped`, or be listed under
  the new manifest key `tenant_scope_exempt` (`table` + required `reason`;
  stale entries fail). `billing_operation_intents` is exempt (its `tenant_id`
  is the billing subject, C-13 policy). The `projecthub` taskboard projection
  and project snapshot are now `TenantScoped` with RLS (migration
  `0004_taskboard`); under `tenant_rls` an unscoped `rebuild_all` / cold
  `rebuild_entity` sees no rows — rebuild with `--tenant`.
- **New gate:** every bundled module boots alone through `create_app` +
  lifespan (`src/sandloom/core/tests/test_module_boots_alone.py`), refusing only
  with the documented `ContractBindingError`; new test seam
  `sandloom.core.testing.boot_module_alone` / `BootOutcome` / `ModuleBootError`,
  used by each example app.
### Recovery — backups, security-state reconciliation, incident commands

- `sandloom backup plan` lists what to back up per database instance (with the
  chains it hosts) and per Redis endpoint (security cache, event-bus streams,
  task broker), plus object store and secrets, with backup and restore steps.
- `sandloom backup verify <dump>` restores a `pg_dump` into a throw-away database,
  checks every hosted migration chain is at its head, core tables, index /
  constraint validity and foreign keys, reports the newest audit row, drops it.
- `sandloom backup reconcile-security`: after restoring the security Redis from an
  older snapshot, re-derives emergency blocks (newest `auth.user.blocked` /
  `.unblocked` audit row per user, or an off-site `--audit-export` file) and
  revocation marks (`core.auth.tokens_revoked.v1` outbox rows inside the mark
  lifetime). A stale snapshot no longer silently unblocks a user. Exit code is
  the traffic gate of the restore runbook.
- `sandloom security revoke-sessions | force-password-reset (--all | --tenant |
  --user)` and `sandloom security audit-export`: dry-run by default, `--apply`
  needs `--reason`, production also `--confirm-production`; batched, durably
  deduplicated (`--operation-id` resumes), audited per user
  (`actor_type="operator"`).
- identity: new `identity.security.v1` contract (`list_user_ids`,
  `revoke_sessions`, `require_password_reset`); `users.password_reset_required`
  (migration `0008_identity`, additive) — a password sign-in of such a user
  answers `403` with `X-Password-Reset-Required: true` until the reset flow or
  a password change clears it.
- identity: `POST /api/v1/users/{user_id}/reset-2fa` — class `A` operator
  reset of a lost second factor (platform admin, not on oneself,
  `Idempotency-Key` required, audited `identity.twofa.reset`, ends every
  session).
- tenancy: `tenancy.membership.v1.list_member_user_ids` (additive read).
- `OutboxStore.list_channel_since` (non-abstract; SQLAlchemy implements it).
- docs/operations/operator-reference.md: "Backups & restore" rewritten with the restore order and
  traffic gate; new "Security incident runbook" (database leak, credential
  leak, key compromise).

### Added — operational metrics (review map B-07, H-09, C-07, H-14)

- Delivery lag: `sandloom_outbox_oldest_pending_age_seconds{database}` (relay, one
  indexed `min(emitted_at)` every 10 s, also while the broker circuit is
  open), `sandloom_streams_group_oldest_unacked_age_seconds{stream,group}` (group
  heartbeat), `sandloom_event_end_to_end_latency_seconds{consumer}` (`emitted_at` →
  handler success) and `sandloom_event_consumer_handler_duration_seconds{consumer}`.
- Database pools: `sandloom_db_pool_connections{database,pool,state}`,
  `sandloom_db_pool_max_connections`, `sandloom_db_pool_overflow` and
  `sandloom_db_pool_checkout_waiters` (the fair-gate queue), sampled every 5 s off
  the request path; `DatabaseProvider.pool_stats()` on the SQLAlchemy driver.
- `sandloom_cache_operation_errors_total{instance,operation}` (Redis cache driver),
  `sandloom_auth_revocation_check_duration_seconds{mode}`,
  `sandloom_rate_limit_rejected_total{route}` /
  `sandloom_rate_limit_unavailable_total{outcome}`,
  `sandloom_rpc_client_request_duration_seconds{peer,outcome}` and
  `sandloom_http_untrusted_forwarded_headers_total` (`X-Forwarded-For` while
  `TRUST_PROXY_HEADERS` is off).
- New optional store / transport hooks with `None` defaults:
  `OutboxStore.oldest_pending_age_seconds()` and
  `MessageTransport.oldest_unacked_age_seconds()`.
- `docs/operations/operator-reference.md`: metric reference and a suggested alert table. All new
  gauges are Gunicorn-multiprocess safe (`livesum` / `livemostrecent`); a test
  now requires a `live*` mode on every registered gauge.

### Security and correctness — 22 October review remediation

- Harden tenant ORM/Core boundaries, SSO step-up and concurrent unlink/reset,
  revocation-aware idempotency replay, strict RPC response binding, transport
  limits/deadlines and HTTP authority isolation. Preserve DB-atomic financial
  dedupe and live fail-closed revocation checks.
- Isolate malformed Streams entries, targeted replay and pending ownership;
  renew handler leases, bound reconnect/drain behavior and preserve outbox
  provenance. Add versioned projection tombstones and repair task shutdown.
- Aggregate Gunicorn metrics correctly; align shutdown grace, independent
  health probes, broker/relay readiness, persistent deployment storage and
  safe operational logs. Retire unsafe prototype lazy autocommit reads.
- Repair package sources, generated typing/Compose, key generation, test
  resource admission, secret detection and API-baseline coverage.
- Licensing: no open-source license is granted. The MIT statement in
  `backend/README.md` was removed; the distributions carry no `LICENSE` file
  or `license` metadata (all rights reserved).
- Apply additive core migration `0008` before deployment. Review
  the archived hardening upgrade notes for
  intentional security compatibility changes; current requirements are in the
  [security model](https://sandloom.dev/docs/reference/security-model/). Historical benchmarks do not
  qualify this new artifact; external credential rotation remains separate.
- Outbox relay: a failed / broker-parked row holds the rows behind it on its
  channel (no attempt counted) so a later event never overtakes an earlier one
  parked in backoff.
- Task enqueue: a send that provably never reached the broker (payload not
  serializable, unknown task) releases the idempotency reservation instead of
  leaving it pending forever.
- Redis cache: tests pin that non-idempotent commands (INCR, Lua scripts,
  `SET NX`) are sent once on a lost reply (the stock redis-py client); they
  fail if an upgrade starts re-sending commands.

### Changed — billing reads never write (behaviour change)

- `GET /api/v1/billing/subscription` and `GET /api/v1/billing/credits` are
  pure declared reads (execution planner, a `READ ONLY` transaction). They no
  longer create the tenant's billing customer (and its zero balance row) on
  first use: a tenant without a customer answers exactly what a freshly
  created one did — `{"status": "none", "current_period_end": null,
  "trial_end": null, "cancel_at_period_end": false, "plan": null}` /
  `{"balance": 0, "debt": 0, "in_debt": false}` — and nothing is written.
  The customer is created by `POST /checkout-session` / `POST
  /customer-portal` (the operation intent), which every purchase, grant and
  webhook goes through. Observable elsewhere: a tenant that never started a
  checkout has no row in `/api/v1/platform/billing/customers` (nothing to
  adjust), and a token minted for a user without a customer carries no
  `subscription_status` claim (it carried `"none"` once a `GET` had run);
  every claim rule (`Subscribed`, `subscription.active`) denies both alike.
  The `billing.entitlements.v1` / `billing.credits.v1` contracts already
  answered "no customer" as false / 0 / `{}` / 402 and are unchanged.

### Added — execution planner

- `JoinRead` may be keyed on the joined row only (`by=()`) when `join_by` /
  `join_where` pin it to one row (a unique key of `join`) and `via` is unique
  on the entity; anything that could return several rows is refused.
- `ListOperation(optional={arg: column}, prefix={arg: column},
  prefix_ignore_case=...)`: equality / literal-prefix (`LIKE` / `ILIKE`)
  filters applied only when the argument is not `None`, on both executors;
  one statement per combination of present filters (at most four filters),
  residual to the covering-index rule. The cursor is bound to every argument.
- projecthub's `GET /api/v1/taskboard/tasks` runs on the planner
  (`taskboard.rows.recent`, migration `0003_taskboard` adds
  `ix_taskboard_ptqi_tenant_updated`); a prefix's `%` / `_` are now matched
  literally, and equal `updated_at` values are ordered by `task_id` DESC.

### Added — test API

- `sandloom.core.testing` seams for module tests that previously reached kernel
  internals: `use_security_cache`, `revocation_mark_key`, `block_mark_key`,
  `record_revocation_checks`, `stop_revocation_listener`, `set_environment`,
  `operation_classes` / `route_policy` (`RoutePolicy`), and migration-test
  helpers `temporary_database` (fixture `scratch_database`),
  `upgrade_core_chain`, `migrate_module`, `run_sql`. `revoked_tokens.available
  = False` simulates an unreachable revocation store.

### Changed — tests

- The bundled identity / tenancy / billing tests (and the helpdesk example)
  pass the `test-imports` whitelist with no allowlist entries: billing seeds
  organizations through tenancy's HTTP API, conformance tests take the
  `sandloom_core` / `sandloom_app` fixtures, and the error-handler / rate-limit unit
  tests moved to the kernel suite. The `sandloom check` help lists every rule.

### Added — pair-MAC internal RPC authentication (ADR-0004)

- `RPC_AUTH_MODE=service_mac` (target for split deployments) and
  `service_mac_dual` (migration from `service_identity`): every RPC request
  carries `X-SANDLOOM-RPC-Auth`, an HMAC-SHA256 under a per-pair, directional key
  HKDF-derived from the two services' X25519 keys (`sandloom.core.rpc_pair_mac`).
  Audience-bound (a captured request cannot be replayed to another service),
  per-attempt nonce with a replay guard (per process, plus a `SET NX` in the
  security cache for commands: `RPC_REPLAY_GUARD`), freshness window
  `RPC_AUTH_MAX_SKEW_SECONDS` (default 60 s). Per read call ≈ 31 µs → 3 µs on the
  caller and ≈ 95 µs → 8 µs on the provider (measurements in the ADR).
  Rotation via `RPC_DH_EXTRA_PRIVATE_KEYS`; `sandloom keys generate --purpose
  rpc-dh`. The microservices profile and the split conformance matrix run
  `service_mac`. Metric `sandloom_rpc_auth_rejected_total{reason}`.
- Key-compromise impersonation mitigation: in `service_mac` COMMAND calls
  also carry the caller's Ed25519 signature
  (`RPC_COMMANDS_REQUIRE_SIGNATURE`: `auto` = on in prod, `on`, `all`,
  `off` — refused under `multi_tenant_strict`), so a leaked provider X25519
  key can forge reads at most, never effects. Stale
  `RPC_DH_EXTRA_PRIVATE_KEYS` are warned about (refused under the strict
  profile); the signed context carries the attempt's nonce; per-caller
  replay quota (`RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES`, default half the
  table); metric `sandloom_rpc_auth_clock_skew_total{caller}`; `RPC_REPLAY_GUARD=local` is
  refused in prod; `HEAD` on the RPC status route is `405`.
- Ed25519 identity contexts carry `aud` when the binding pins its provider;
  a provider refuses a context addressed to another service.

### Changed — internal RPC error mapping

- A provider's identity / authorization rejection (`rpc_identity_*`,
  `rpc_caller_forbidden`, `rpc_auth_*`, `rpc_replay_unavailable`) now reaches
  the caller as `503 RemoteServiceUnavailable` with that code, never as the
  provider's 401 / 403 (a deployment problem is not the end user's).

### Changed — dependencies

- pydantic 2.14.1 / pydantic-core 2.50.1, pydantic-settings 2.16.1,
  SQLAlchemy 2.1.5, authlib 1.9.1 / joserfc 1.7.7, hiredis 3.5.0, httptools
  0.9.0, websockets 17.3, sentry-sdk 2.72.0, jsonschema-rs 0.59.0 (`fast`
  extra); dev tools ruff 0.17.1, pytest-mock 3.17.0, pre-commit 4.7.0 (also
  the `sandloom new` dev pins); docs mkdocstrings 1.0.8 / mkdocstrings-python
  2.0.10, pymdown-extensions 12.3. SQLAlchemy 2.1.5's stock async queue now
  takes an idle connection without `wait_for` too; the linger-pool tests
  accept both behaviours.

- Preserve Boolean parameter coercion, deferred raiseload and root discriminator
  handling through conservative planner fallback to the ORM.

### Changed — production execution planner

- Eligible declared reads use the optimized PostgreSQL/asyncpg executor by
  default. `PLANNER_OPTIMIZED_EXECUTOR=false` restores the general executor.
- Automatic ORM fallback preserves custom column processors, application
  listeners, eager loading and nonunique single-row cardinality checks. Cached
  plans also respect policies installed after preparation.
- All optimized reads use READ ONLY transactions, including databases without
  tenant RLS. Nullable keyset ordering is refused and partial indexes no longer
  prove unconditional uniqueness or covering order.

### Fixed — release qualification

- PostgreSQL connection establishment uses a finite timeout and capped
  exponential backoff with equal jitter after transport failures. One recovery
  probe per process/pool prevents queued requests from reconnecting together;
  connection failure returns 503 and never replays SQL or a transaction.
- FIFO admission for contended database checkouts prevents new arrivals from
  starving existing waiters; one timeout covers admission and queue waiting.
- Identity propagates infrastructure outages instead of reporting bad tokens
  or silently treating authenticated callers as anonymous.
- Planner cleanup invalidates cancelled connection returns and releases a lease
  when read-unit setup fails.

### Fixed — framework review hardening

- Self-contained source distributions for all eight packages, with source-to-wheel
  roundtrip validation and clean-environment installs.
- Atomic enqueue deduplication using the security cache: replay successful task
  IDs, reject conflicting parameters, and preserve ambiguous reservations for
  reconciliation. Task effects still require DB-atomic idempotency.
- Matching local/RPC read memo policy, isolated mutable results, and invalidation
  that cannot be undone by an in-flight read.
- Fail closed on missing architecture module directories; export the documented
  `db_uow` fixture; reject keyword/invalid scaffold names; generate RLS policies
  only when the target database enables the matching runtime hook.
- Release publishing waits for complete CI and package checks, verifies tag
  versions, and blocks on dependency audit failures. Coverage includes the kernel.


## [0.7.0] — 2026-10-13

Security, billing-correctness and split-deployment release with **breaking
changes** (pre-1.0: a minor may break; `docs/contributing/versioning.md`). It
contains everything merged after 0.6.0 ([Maryarty/auto_job_applyer#39](https://github.com/Maryarty/auto_job_applyer/pull/39) and [Maryarty/auto_job_applyer#41](https://github.com/Maryarty/auto_job_applyer/pull/41)–[Maryarty/auto_job_applyer#87](https://github.com/Maryarty/auto_job_applyer/pull/87); #80 is an open issue, #82 is this release). Every
distribution (`sandloom-framework`, `sandloom-shared`, `sandloom-identity`, `sandloom-tenancy`,
`sandloom-billing`, `sandloom-infra-redis`, `sandloom-infra-celery`, `sandloom-platform`) is
versioned at 0.7.0 as an internal baseline (not published to PyPI), and `sandloom new` now pins
`>=0.7.0`.

### Upgrade notes (0.6.0 → 0.7.0)

The historical 0.7 step-by-step guide is archived.
For the current source API, use [Upgrading to Public API 1.0](https://sandloom.dev/docs/guides/upgrading-1.0/).
In short:

1. Run `sandloom-migrate upgrade-all --apply` — new core-chain revision
   `0006_contract_command_log` (every hosting database), tenancy `0004`–`0005`,
   billing `0004`–`0009`. Billing `0005` changes who is entitled (see
   "Billing"): list legacy subscriptions with no catalog price BEFORE
   deploying.
2. Fix every `publish_event` / `record_audit` call that ran outside a Unit of
   Work (they now raise), or mark the deliberate best-effort ones;
   `LEGACY_UNSCOPED_EFFECTS=true` is a 0.7-only escape hatch (removed in 0.8).
3. Split production deployments: asymmetric JWT (`JWT_PRIVATE_KEY` on the
   issuer only, `JWT_PUBLIC_KEYS` everywhere) or the explicit
   `JWT_ALLOW_SHARED_SECRET_SPLIT=true`; plan the move to
   `RPC_AUTH_MODE=service_identity` + X25519 response-binding keys.
4. Move contract DTOs out of module packages (`sandloom.shared.*` or
   `CONTRACT_DTO_PACKAGES`); commit `contracts.snapshot.json` per module.
5. Billing: a Celery worker + exactly one beat must run wherever billing is
   loaded (intent reconciler); the microservices profile now ships
   `billing-worker` / `billing-beat` ([Maryarty/auto_job_applyer#83](https://github.com/Maryarty/auto_job_applyer/pull/83)). Custom PSP adapters implement
   the extended `StripeClientPort`; custom cache drivers backing the security
   cache need `set_max` or `cas` ([Maryarty/auto_job_applyer#85](https://github.com/Maryarty/auto_job_applyer/pull/85)).
6. Apps: add `operation_class(...)` to every route and the
   `assert_operation_classes(app)` test.

### Breaking changes

- **`publish_event` / `record_audit` are guaranteed by default** ([Maryarty/auto_job_applyer#52](https://github.com/Maryarty/auto_job_applyer/pull/52)).
  `publish_event` (and `publish_event_versions`) outside a Unit of Work raises
  `UnitOfWorkRequiredError` (new, `sandloom.core.di`) instead of publishing straight
  to the bus; `record_audit` defaults to `required=True` and raises
  `AuditUnavailableError` outside a UoW, without an audit store or with a
  non-UUID `actor_id`. Best-effort is explicit (`required=False`) and, in
  business-module code, needs a `# sandloom: best-effort <reason>` marker — new
  `sandloom check` rule `best-effort-effects`. Transitional
  `LEGACY_UNSCOPED_EFFECTS` (default `false`; 0.7 only, **removed in 0.8**)
  restores the 0.6 defaults for calls without an explicit `required=`, with a
  logged warning and a `DeprecationWarning` per call. New test fixture
  `db_uow`.
- **Production split processes refuse a shared HS secret** ([Maryarty/auto_job_applyer#51](https://github.com/Maryarty/auto_job_applyer/pull/51)). A prod
  process that loads no token issuer (no module declared
  `issues_tokens=True`) or binds remote services (`SERVICE_BINDINGS`) does
  not start with an HS* `JWT_ALGORITHM` unless
  `JWT_ALLOW_SHARED_SECRET_SPLIT=true`; under
  `PRODUCTION_PROFILE=multi_tenant_strict` asymmetric JWT is required. A
  monolith (issuer loaded, no bindings) is unaffected.
- **Contract DTOs must be importable by every caller** ([Maryarty/auto_job_applyer#54](https://github.com/Maryarty/auto_job_applyer/pull/54)). A contract
  registered by a module may not name a pydantic model / Enum defined inside
  a (loaded) module package or a function: `ContractTypeError` at
  registration, `sandloom check` rule `contract-dtos`. Allowed: `sandloom.shared.*` and
  the packages in the new `CONTRACT_DTO_PACKAGES`.
- **Billing grants fail closed** ([Maryarty/auto_job_applyer#73](https://github.com/Maryarty/auto_job_applyer/pull/73), [Maryarty/auto_job_applyer#77](https://github.com/Maryarty/auto_job_applyer/pull/77)). A subscription whose price is
  not in the catalog grants nothing (`price_id` NULL; a change to an unknown
  price withdraws the previous entitlements); a subscription created without
  an operation intent (`origin='psp'`) grants only with
  `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS=true` (default `false`); existing
  rows are backfilled `origin='legacy'` and keep granting **only if their
  price is known**. A credit-pack completion without a `payment_intent`
  grants nothing (audited `billing.credits.grant_refused`). Credit balances
  may become negative after a refund / dispute (the `amount >= 0` CHECK is
  dropped). Details under "Billing".
- **PSP port (`sandloom.billing.psp.StripeClientPort`)** ([Maryarty/auto_job_applyer#69](https://github.com/Maryarty/auto_job_applyer/pull/69), [Maryarty/auto_job_applyer#76](https://github.com/Maryarty/auto_job_applyer/pull/76)):
  `create_checkout_session` returns `PspSession(id, url, expires_at)` (was the
  URL string) and takes `subscription_metadata`; new required methods
  `expire_checkout_session(session_id)` and
  `list_checkout_sessions(customer_id=, created_gte=)`. Custom PSP adapters
  must implement them.
- **Tenancy revokes on every role change, fail closed** ([Maryarty/auto_job_applyer#47](https://github.com/Maryarty/auto_job_applyer/pull/47)). `change_role`
  and `transfer_ownership` revoke the affected members' access tokens (the
  client gets `401 "Token has been revoked"` and refreshes); every tenancy
  revocation (removal, leave, role change, transfer, org deletion) is
  `require_immediate=True` — with the revocation store down the change is
  refused with `503` instead of committing without its mark.
- **`RPC_RESPONSE_BINDING=strict` means keyed** ([Maryarty/auto_job_applyer#67](https://github.com/Maryarty/auto_job_applyer/pull/67)): v1 (unkeyed) replies
  are rejected, an `x25519` reply must come from the binding's
  `"service_id"`, and strict with a service identity refuses to start with an
  unpinned binding or without the service's X25519 key.
- **Route markers are pruned from the executed dependencies** ([Maryarty/auto_job_applyer#74](https://github.com/Maryarty/auto_job_applyer/pull/74)).
  `operation_class(...)`, `strict_revocation()` and `revocation_freshness(...)`
  stay in `route.dependencies` but no longer appear in `route.dependant`;
  read them with `sandloom.core.route_markers.route_marker_calls(route)`. Only
  factory-registered markers are pruned (a `functools.wraps` copy of a
  marker around a real guard is kept and runs).
- **Dependencies** ([Maryarty/auto_job_applyer#48](https://github.com/Maryarty/auto_job_applyer/pull/48), [Maryarty/auto_job_applyer#62](https://github.com/Maryarty/auto_job_applyer/pull/62), [Maryarty/auto_job_applyer#64](https://github.com/Maryarty/auto_job_applyer/pull/64)): `sandloom-framework` now requires
  `aiohttp>=3.14.5,<3.15` (default internal-RPC transport) and pins
  `asyncpg>=0.32.0,<0.33` and `sqlalchemy[asyncio]>=2.1.4,<2.2` to the ranges
  whose driver internals the fast paths are verified against.

- **Revocation marks need `set_max` or `cas` on the security cache** ([Maryarty/auto_job_applyer#85](https://github.com/Maryarty/auto_job_applyer/pull/85)).
  `CacheProvider` gains `set_max(key, value, *, ttl_seconds=None) -> str`
  (feature `set_max`; the base class supplies a portable read / `add` /
  `compare_and_set` default). A custom cache driver serving
  `SECURITY_CACHE_INSTANCE` with neither `set_max` nor `cas` now REFUSES
  revocation-mark writes (retried, then the durable applier; `503` for a
  `require_immediate=True` change) instead of risking a lowered mark. The
  bundled Redis driver and `sandloom.core.testing.InMemoryCache` implement
  `set_max` atomically.

### Security

- **Asymmetric access tokens** ([Maryarty/auto_job_applyer#51](https://github.com/Maryarty/auto_job_applyer/pull/51)). `JWT_ALGORITHM=EdDSA|RS256|ES256`:
  the issuer (identity) holds `JWT_PRIVATE_KEY` + `JWT_KID`, every service
  verifies with `JWT_PUBLIC_KEYS` (`{"<kid>": "<PEM>"}`, several during a
  rotation) and cannot mint (`TokenIssuanceUnavailable`). Each algorithm is
  bound to one key type (no algorithm confusion; no kid / unknown kid / HS
  token / `none` rejected). Critical startup role check
  (`sandloom.core.security.token_signing_problem`); `JWT_SECRET` is optional in
  asymmetric mode and refused on verify-only processes. `sandloom keys generate
  [--alg] [--kid] [--out-dir]`; rotation runbook in
  `docs/reference/security-model.md`. `PLANNER_CURSOR_SECRET` keys planner
  cursors where neither `INTERNAL_API_TOKEN` nor `JWT_SECRET` exists. The
  microservices profile signs with EdDSA.
- **Service identity for internal RPC** ([Maryarty/auto_job_applyer#57](https://github.com/Maryarty/auto_job_applyer/pull/57)). `RPC_AUTH_MODE=service_identity`:
  each service signs as `RPC_SERVICE_ID` with its own Ed25519
  `RPC_PRIVATE_KEY` + `RPC_KEY_ID`; providers verify with `RPC_SERVICE_KEYS`
  and authorize per contract / method against `RPC_ALLOWED_CALLS` (deny by
  default; 401 unverifiable, 403 `rpc_caller_forbidden`). A caller whose
  allowlist entry misses one of its own `SERVICE_BINDINGS` refuses to start;
  `sandloom keys allowlist` derives the map, `sandloom keys generate --purpose rpc
  --service <id>` makes a key. `dual` = migration mode; `shared_token` stays
  the default and logs a DEPRECATED line on every production start. The
  microservices profile uses `service_identity`.
- **RPC responses bound to their request** ([Maryarty/auto_job_applyer#64](https://github.com/Maryarty/auto_job_applyer/pull/64), [Maryarty/auto_job_applyer#67](https://github.com/Maryarty/auto_job_applyer/pull/67)). Every RPC request
  carries a nonce (`X-SANDLOOM-RPC-Nonce`); the provider answers with
  `X-SANDLOOM-RPC-Response-MAC`; a response bound to another call is never
  returned (retryable transport error, pool discarded). v2 binds with a
  SECRET key: per-pair X25519 ECDH + HKDF over each service's DEDICATED X25519
  key (`RPC_DH_PRIVATE_KEY`, `RPC_DH_KEY_ID`, shared `RPC_SERVICE_DH_KEYS`;
  never the Ed25519 signing key), else a key derived from the internal token;
  the MAC also covers the request's signatures. `RPC_RESPONSE_BINDING`
  (`permissive` default — v1 / unbound replies counted in
  `sandloom_rpc_response_unkeyed_total` / `sandloom_rpc_response_unbound_total`;
  `strict`, the microservices profile's default). `SERVICE_BINDINGS[...]
  ["service_id"]` pins the provider. Rejections:
  `sandloom_rpc_response_binding_rejected_total{reason}`. Not a substitute for
  TLS / mTLS.
- **Configurable argon2id work factor + rehash on login** ([Maryarty/auto_job_applyer#61](https://github.com/Maryarty/auto_job_applyer/pull/61)).
  `PASSWORD_HASH_PROFILE` (`strong` default = unchanged m=65536/t=3/p=4;
  `owasp_min` = m=19456/t=2/p=1; `custom`) with `PASSWORD_HASH_TIME_COST` /
  `_MEMORY_KIB` / `_PARALLELISM` overrides. Production refuses parameters
  below the OWASP minimum unless `PASSWORD_HASH_ALLOW_WEAK=true` (logged as a
  SECURITY warning on every start). Identity re-hashes a stored hash with
  other parameters after a successful login, in the same UoW (failures never
  block the login; `sandloom_password_rehash_total{outcome}`).
  `sandloom.core.di.password_hasher()` / `rehash_password_if_needed(...)`.
- **Revocation windows closed** ([Maryarty/auto_job_applyer#45](https://github.com/Maryarty/auto_job_applyer/pull/45), [Maryarty/auto_job_applyer#47](https://github.com/Maryarty/auto_job_applyer/pull/47)). Tokens carry `iat_us`
  (microseconds); a token is revoked iff its issue instant is `<=` the mark
  (legacy whole-second tokens in the mark's second are revoked too). The mark
  is written at `now + AUTH_REVOCATION_CLOCK_SKEW_SECONDS` (default 2 s) and
  never moves backwards (compare-and-set). `revoke_user_tokens_on_commit`
  also writes `core.auth.tokens_revoked.v1` to the outbox, retries the
  post-commit write (`AUTH_REVOCATION_WRITE_ATTEMPTS`,
  `AUTH_REVOCATION_RETRY_BASE_SECONDS`; `auth_revocation_deferred_total`), and
  identity's durable consumer `sessions@tokens_revoked` applies a lost write.
  `require_immediate=True` writes the mark before commit or refuses the change
  (`503`); identity (password change / reset, role revocation, deactivation)
  and `block_user` use it. A corrupt (unparsable) mark rejects every token of
  the subject (`auth_revocation_corrupt_mark_total`) until the issuance check
  repairs it. Revocation consumers are `dead_letter=False` (retried forever
  with capped backoff); while one is stuck `/ready` answers 503
  `revocations_pending` (`auth_revocation_pending_seconds`), `/health` is
  unaffected.
- **Authority at commit — ADR-0003 TOCTOU gaps G1–G3** ([Maryarty/auto_job_applyer#69](https://github.com/Maryarty/auto_job_applyer/pull/69)). Tenancy's
  class-A writes lock the acting member's row `FOR SHARE` (target + actor in
  user-id order, no deadlock between admins acting on each other); identity's
  admin mutations re-check the admin role inside the write transaction under
  an RBAC advisory lock; new `before_commit(callback)`,
  `revocation_at_commit()` and `subject_commit_check(request)`
  (`sandloom.core.di`) re-read the caller's revocation / block marks right before
  COMMIT (tenancy `POST /invitations/accept` uses it); billing's class-F
  routes run operation intents (see "Billing"). Guarantees stated precisely
  in ADR-0003 ("Guarantees, precisely").
- **Route operation classes, machine-checked** ([Maryarty/auto_job_applyer#68](https://github.com/Maryarty/auto_job_applyer/pull/68)).
  `operation_class("P"|"R"|"RS"|"W"|"F"|"A")` (`sandloom.core.di`, a marker on a
  route or router). `create_app` refuses a declared class that contradicts
  its route (two classes, a staleness budget outside class R, an RS/W/F/A
  route that is not always-strict — `OperationClassError`); the CI gate
  `sandloom.core.testing.assert_operation_classes(app)` fails on an unclassified
  route (generated by `sandloom new`, used by `examples/` and the platform's
  `tests/architecture/test_operation_classes.py`, which pins ADR-0003's
  route map). Billing routes split into `billing_read_router` (GET `/plans`,
  `/subscription`, `/credits`) and the always-strict `billing_router`
  (checkout, portal) — same paths, responses and OpenAPI.
- **`idempotency_guard(store_response=False)`** ([Maryarty/auto_job_applyer#69](https://github.com/Maryarty/auto_job_applyer/pull/69)): in-flight dedupe only
  (409 / 422), nothing stored or replayed. Billing's checkout / portal routes
  use it with renamed scopes (`billing.checkout_intent`,
  `billing.customer_portal_intent`): a checkout / portal URL is never replayed
  from the cache.
- **`PRODUCTION_PROFILE=multi_tenant_strict`** ([Maryarty/auto_job_applyer#54](https://github.com/Maryarty/auto_job_applyer/pull/54)): in prod refuses to
  start unless `TENANCY_MODE=multi`, `tenant_rls` + role verification on every
  database, fail-closed revocation, `SECURITY_CACHE_INSTANCE` on a different
  Redis server than the default cache, contract compat check on when split and
  (since [Maryarty/auto_job_applyer#51](https://github.com/Maryarty/auto_job_applyer/pull/51)) asymmetric JWT when split. A critical startup task checks that
  every `TenantScoped` table has RLS enabled + FORCEd with its policy.
  `SECURITY_CACHE_INSTANCE` (default `default`) carries revocation marks,
  blocks and idempotency records.

### Billing

- **Operation intents for checkout / customer portal** ([Maryarty/auto_job_applyer#69](https://github.com/Maryarty/auto_job_applyer/pull/69); ADR-0003 G1).
  Authorize (live role + membership version) → a billing-owned intent
  (`billing_operation_intents`, migration `0004_billing`) committed before the
  PSP call → PSP call keyed by the intent (a timeout leaves it pending, `503`;
  the retry re-sends the same key; a PSP 4xx fails it, `502`) → re-check
  authority and session marks right before issuing (`403` lost / `409`
  changed; the intent is revoked and the checkout session expired). An issued
  checkout intent is final: a retry returns its stored URL after the re-check
  (an issued portal intent answers `409`). The routes no longer use the
  request `DbSession` (each step is its own short UoW). Intent paths are
  owner-scoped. Webhook gate: a credit grant or a NEW subscription applies only
  for an issued checkout intent of the event's customer (and session);
  otherwise journaled failed, audited `billing.intent.unauthorized_completion`,
  intent flagged `followup_at`. `invoice.paid` must come from the
  subscription's customer; unknown Stripe statuses are not applied;
  `incomplete_expired` is terminal.
- **Fail-closed entitlements** ([Maryarty/auto_job_applyer#73](https://github.com/Maryarty/auto_job_applyer/pull/73); ADR-0003 items 9, 11).
  `billing_subscriptions.origin` (`0005_billing`: `intent` | `psp` | `legacy`;
  existing rows backfilled `legacy`, later inserts default to `psp`). A
  subscription event with items sets the price from them (unknown → NULL,
  audited `billing.subscription.unknown_price`). `grants_access`: no catalog
  price → no access; `psp` rows only with
  `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS=true`. A downgrade past `0005`
  refuses while `psp` rows exist unless `SANDLOOM_BILLING_DROP_ORIGIN=1`.
  `billing.subscription.created/updated/canceled.v1` carry
  `data.grants_access` (additive).
- **One subscription per checkout intent, at its price** ([Maryarty/auto_job_applyer#76](https://github.com/Maryarty/auto_job_applyer/pull/76); item 16):
  exactly one item at the intent's catalog price, quantity 1
  (`billing_subscriptions.intent_id`, partial UNIQUE, `0006_billing`); a
  credit-pack completion must name the intent's price.
- **Intent reconciliation** ([Maryarty/auto_job_applyer#76](https://github.com/Maryarty/auto_job_applyer/pull/76); items 7, 13, 15). Beat task
  `sandloom.billing.tasks.reconcile_operation_intents` (every 5 min, `acks_late`)
  closes `pending` intents older than `BILLING_INTENT_RECONCILE_AFTER_MINUTES`
  (default 30) by finding their PSP session through the intent marker
  (expire → `revoked`; none → `failed`; paid → `followup_at`) and retries
  failed expire compensations, bounded by
  `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS` (default 5). An issued intent is
  never touched. A retry of an issued checkout whose session expired answers
  `410` (`psp_session_expires_at`; legacy rows: created + 24 h). Migration
  `0007_billing`.
- **Refunds and disputes reverse credits** ([Maryarty/auto_job_applyer#77](https://github.com/Maryarty/auto_job_applyer/pull/77); item 12).
  `charge.refunded`, `charge.dispute.created` / `.updated` / `.closed` reverse
  (a won dispute restores) a credit-pack purchase's credits through the
  append-only ledger, deduped per PSP event; partial refunds / disputes
  proportional, inquiries audit-only; events arriving before the purchase are
  recorded (`billing_psp_payment_states`) and applied at grant time; orphan
  payment states are purged after 90 days. New tables `billing_credit_grants`,
  `billing_psp_payment_states` (`0008_billing`); events
  `billing.credits.reversed.v1`, `billing.credits.restored.v1`; audits
  `billing.credits.reversed` / `.restored` / `.reversal_refused`,
  `billing.charge.disputed`. A balance may be negative (spends are refused
  while short); `GET /credits` adds `debt` and `in_debt`. Subscription access
  is not changed by a dispute (temporary policy, ADR-0003 item 17).
- **Portal change attribution** ([Maryarty/auto_job_applyer#77](https://github.com/Maryarty/auto_job_applyer/pull/77); item 10): a plan /
  `cancel_at_period_end` change with no portal intent of the customer issued
  within `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES` (default 60) is audited
  `billing.subscription.unattributed_change` — never blocked
  (`billing_operation_intents.issued_at`, `0009_billing`).

### Contracts, events and data

- **Durable command idempotency** ([Maryarty/auto_job_applyer#50](https://github.com/Maryarty/auto_job_applyer/pull/50)). The dedupe record of a command
  contract lives in the PROVIDER's database (`contract_command_log`, core
  chain `0006`), claimed in the command's own UoW and committed iff the effect
  commits: a crash after the commit replays, never re-runs; same key + other
  arguments → `409`; a concurrent duplicate waits up to
  `CONTRACT_DB_LOCK_TIMEOUT_MS` (`503 ContractCallInProgress`); results
  > 256 KiB are non-replayable markers. Records expire after
  `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` and are purged by the relay
  housekeeping / `sandloom outbox purge`. The cache is no longer involved (a driver
  without `command_idempotency` falls back to the fail-closed cache dedupe).
- **Rolling-upgrade contract compatibility** ([Maryarty/auto_job_applyer#54](https://github.com/Maryarty/auto_job_applyer/pull/54)). Per-module
  `contracts.snapshot.json` (`sandloom contracts snapshot [--write|--check]`; a
  stale snapshot fails the suite), `sandloom contracts check --against
  <snapshot|dir|url> [--as provider|consumer]`, runtime comparison of every
  bound provider's live describe document (`CONTRACT_COMPAT_CHECK=ready`
  default → `/ready` fails with `contract_incompatible`; `strict` also aborts
  startup; `off`; `CONTRACT_SNAPSHOT_PATHS`); a startup warning for remote
  bindings without a snapshot. `ContractRequirement(methods=...)` narrows the
  check.
- **`sandloom-migrate extract-module`** ([Maryarty/auto_job_applyer#53](https://github.com/Maryarty/auto_job_applyer/pull/53)): moves a module's existing data out
  of a shared database (offline: fence, copy with manifest + checksums,
  sequences, outbox / inbox / audit / command-log hand-off, Streams
  group-owner transfer; `--verify`, `--drop-source`, `--unfence`; dry-run by
  default). Runbook: `deploy/README.md`.
- **`ListOperation` and planned read contracts** ([Maryarty/auto_job_applyer#39](https://github.com/Maryarty/auto_job_applyer/pull/39)). Declared
  tenant-scoped keyset list reads with an opaque HMAC cursor
  (`PLANNER_LIST_MAX_LIMIT`, default 500; `InvalidCursor` / `PageLimitError`
  → 400); `@contract_method(planned=True)` + `contract_reads()` (no UoW,
  read-only lease on the contract pool). `tenancy.membership.v1` is planned;
  `GET /orgs`, `/orgs/{id}`, `/orgs/{id}/members` are planned routes (API
  unchanged, plus `cursor` / `X-Next-Cursor`); tenancy migration `0004`
  adds the keyset indexes.
- **Versioned member events** ([Maryarty/auto_job_applyer#45](https://github.com/Maryarty/auto_job_applyer/pull/45), [Maryarty/auto_job_applyer#47](https://github.com/Maryarty/auto_job_applyer/pull/47)). `tenancy.member_*.v1` carry an
  optional per-(org, user) `version` (`tenancy_member_versions`, `0005_tenancy`);
  accepting an invitation / creating an org publishes
  `tenancy.member_added.v1`. Rolling-upgrade rule
  `sandloom.shared.schemas.membership.member_event_action`: a version-less event
  refreshes from the new `tenancy.membership.v1.get_member_state`.
- **Event consumers** ([Maryarty/auto_job_applyer#47](https://github.com/Maryarty/auto_job_applyer/pull/47)): `register_event_consumer(...,
  dead_letter=False)` retries a failing delivery forever with capped backoff
  instead of dead-lettering it; a handler raises `PoisonEventError`
  (`sandloom.core.di`) for an event that can never apply (dead-lettered at once,
  CRITICAL).
- **Contract-read cache shadow** ([Maryarty/auto_job_applyer#68](https://github.com/Maryarty/auto_job_applyer/pull/68); off by default): `CONTRACT_CACHE_SHADOW`
  measures what a caller-side TTL cache of a read contract would have done
  (`sandloom_contract_cache_shadow_total`, per-request share, time avoided, stale
  hits by kind) without serving from it; `CONTRACT_CACHE_SHADOW_REPORT_DIR`
  + `benchmarks/ladder/agg_shadow.py`.

### Performance

- **Internal RPC client is aiohttp** ([Maryarty/auto_job_applyer#62](https://github.com/Maryarty/auto_job_applyer/pull/62), [Maryarty/auto_job_applyer#64](https://github.com/Maryarty/auto_job_applyer/pull/64)): `RPC_TRANSPORT=aiohttp`
  (default) or `httpx` (keep it behind an egress proxy — aiohttp ignores
  `HTTP(S)_PROXY`). One RPC ~1.25 → ~0.36 ms CPU at concurrency 64; the
  mixed-profile split premium +21.6 % → +16.9 % at saturation. Bounded
  per-peer pool (`RPC_POOL_MAX_CONNECTIONS`, `RPC_POOL_IDLE_SECONDS`,
  `RPC_MAX_RESPONSE_BYTES` → 502), `sandloom_rpc_client_*` metrics. aiohttp
  internals are probed (`sandloom.core.rpc_transport_compat`,
  `VERIFIED_AIOHTTP_RANGE`); a failed probe falls back to httpx
  (`sandloom_rpc_transport_fallback_total`).
- **Fewer PostgreSQL round trips on the write path** ([Maryarty/auto_job_applyer#46](https://github.com/Maryarty/auto_job_applyer/pull/46)): outbox
  mark-delivered is ONE autocommit statement (4 → 1 RT per flush; an
  event-publishing RLS request 9 → 6 RT); `non_tenant_statements()` skips the
  tenant setting for allowlisted system statements (relay tick 12 → 9 RT);
  **`DB_RLS_MERGED_BEGIN` is on by default** (`BEGIN …; set_config` in one
  message on `tenant_rls` + asyncpg; kill switch `false`).
- **Zero per-request cost of route markers** ([Maryarty/auto_job_applyer#74](https://github.com/Maryarty/auto_job_applyer/pull/74)): ~35–40k instructions per
  marker per request removed (strict + class route 314k → 241k instr/request).
- **Non-blocking pool checkout at max overflow** ([Maryarty/auto_job_applyer#66](https://github.com/Maryarty/auto_job_applyer/pull/66)): −47 % instructions
  per checkout+return cycle at max overflow (guarded by the
  `pool_fast_checkout` driver probe).

### Fixes

- **Never a 500 from a pooled connection that died while idle** ([Maryarty/auto_job_applyer#55](https://github.com/Maryarty/auto_job_applyer/pull/55)): a
  closed / broken asyncpg connection is replaced at checkout or, when provably
  nothing was sent, reconnected in place for the transaction's first message
  (kill switch `DB_DEAD_CONNECTION_RETRY=false`); otherwise
  `DatabaseUnavailable` (503, `Retry-After`) instead of a masked 500. The idle
  ping is really one round trip.
- **Driver-internals guard for the DB fast paths** ([Maryarty/auto_job_applyer#48](https://github.com/Maryarty/auto_job_applyer/pull/48)):
  `sandloom.core.sql_alchemy.driver_compat` probes asyncpg / SQLAlchemy private
  state once per process; on a mismatch the plain path is used
  (`sandloom_db_driver_fast_path_disabled_total{probe}`). `non_tenant_statements()`
  checks every statement it covers (`NonTenantStatementError` outside prod).
- **Revocation marks no longer fail under contention** ([Maryarty/auto_job_applyer#85](https://github.com/Maryarty/auto_job_applyer/pull/85)). The mark was
  written with a read / `add` / `compare_and_set` loop bounded at 8 attempts;
  a writer that lost 8 races in a row gave up (`revoke_user_tokens` returned
  `False`, the mark could stay below its value until the durable applier ran,
  a `require_immediate` change got a spurious `503`). It is now one
  `set_max` (Redis: one atomic Lua round trip; values compared exactly as
  plain decimals; TTL only when written). Mark readers share that value
  domain (`parse_plain_decimal`): a mark that is not a plain non-negative
  decimal (e.g. `1e9`, a sign) reads as corrupt — fail closed, repaired at
  issuance; framework writers never wrote such values. Portable default
  `sandloom.core.infra.contracts.portable_set_max` (read / `add` /
  `compare_and_set`, retrying while the stored value is lower;
  `CacheContentionError` only after `SET_MAX_MAX_ROUNDS` = 1000 lost races,
  i.e. a livelock against a non-monotonic writer); without `cas` it degrades
  to a plain `set` (NOT monotonic) unless called with
  `overwrite_without_cas=False`, which the revocation mark does. The Redis
  driver falls back to that loop only for a client without Lua scripting
  (logged once); errors inside the script path propagate.
- **Gateway's static IP kept out of the dynamic address pool** ([Maryarty/auto_job_applyer#87](https://github.com/Maryarty/auto_job_applyer/pull/87)): the
  microservices network gets `ip_range: ${SANDLOOM_IP_RANGE:-172.28.0.128/25}`, so
  no container started before the gateway can take its static
  `SANDLOOM_GATEWAY_IP` (.10) — with #83's two extra containers one did, and the
  gateway failed with "Address already in use". `SANDLOOM_SUBNET`,
  `SANDLOOM_GATEWAY_IP` and `SANDLOOM_IP_RANGE` must change together (the gateway IP
  inside the subnet, outside the range); `test_deploy_profiles` pins it and
  `tools/ci/ci_local.sh` exports a matching range.
- **Microservices profile runs billing's worker + beat** ([Maryarty/auto_job_applyer#83](https://github.com/Maryarty/auto_job_applyer/pull/83)): billing's
  periodic tasks (intent reconciler, orphan payment-state purge) never ran in
  `deploy/compose/microservices.yml`. New `billing-worker`
  (`BILLING_WORKER_CONCURRENCY`, default 1) and `billing-beat`, each on
  billing's own broker; connection budget 109 (+10 reserve) of
  `max_connections` 150. `test_deploy_profiles` now asserts that every module
  with `setup_periodic_tasks` has exactly one beat (and a worker on its
  broker) in every profile that serves it.
- **Revocation copies** ([Maryarty/auto_job_applyer#79](https://github.com/Maryarty/auto_job_applyer/pull/79), [Maryarty/auto_job_applyer#81](https://github.com/Maryarty/auto_job_applyer/pull/81)): on a `revocation_freshness` route the
  staleness budget now holds at decision time (a copy that expired or was
  invalidated during the live block-mark read is replaced by a live read);
  only a drop of the SAME subject (or a wholesale clear) discards an in-flight
  read, so foreign broadcasts on the server-wide channel no longer stop
  caching. Strict routes were never affected.

### Testing & tooling

- **Test resource ownership** ([Maryarty/auto_job_applyer#75](https://github.com/Maryarty/auto_job_applyer/pull/75)): a test database (comment) and Redis
  index (`sandloom:test:owner`) carry an owner (`SANDLOOM_TEST_OWNER`, else the
  checkout root); another owner's resource aborts the session before anything
  is reset (exit 4) unless `SANDLOOM_TEST_TAKE_OWNERSHIP=1`.
- **Connection budget** ([Maryarty/auto_job_applyer#75](https://github.com/Maryarty/auto_job_applyer/pull/75)): small test pools by default (explicit
  environment wins), a fail-fast headroom check
  (`SANDLOOM_TEST_SKIP_CONNECTION_CHECK=1` disables it), `-n auto` capped to what
  fits, the split conformance matrix boots in waves of <= 40 connections.
- **Process-global state restored after every test** ([Maryarty/auto_job_applyer#75](https://github.com/Maryarty/auto_job_applyer/pull/75))
  (`sandloom.core.testing.isolation`); `run_on_commit` runs `before_commit` checks
  first ([Maryarty/auto_job_applyer#69](https://github.com/Maryarty/auto_job_applyer/pull/69)).
- **`SANDLOOM_TEST_DATABASE_URL` / `_CACHE_URL` / `_BROKER_URL` win over
  `.env.test`** and the reset guard refuses any other database ([Maryarty/auto_job_applyer#43](https://github.com/Maryarty/auto_job_applyer/pull/43)); the
  repository's pre-push full run requires an explicit `SANDLOOM_TEST_DATABASE_URL`
  (`tools/checks/prepush_full_tests.sh`, [Maryarty/auto_job_applyer#75](https://github.com/Maryarty/auto_job_applyer/pull/75)).
- **Race-free subprocess servers** ([Maryarty/auto_job_applyer#78](https://github.com/Maryarty/auto_job_applyer/pull/78)): `sandloom.core.testing.served`
  (`ReservedPort`, `spawn_served`, a per-spawn identity probe) for split
  conformance and the extraction / split e2e suites.
- **`sandloom.core.testing.wait_revocation_listener_subscribed()`** ([Maryarty/auto_job_applyer#79](https://github.com/Maryarty/auto_job_applyer/pull/79))
  replaces `PUBSUB NUMSUB` waits (flaky under xdist).
- `assert_contract_conformance(..., transport=)` runs the RPC side over a
  real socket (`aiohttp` / `httpx`); `served_rpc(core)` ([Maryarty/auto_job_applyer#62](https://github.com/Maryarty/auto_job_applyer/pull/62)).
- `tools/ci/ci_local.sh`: race-free microservices gateway port (Docker
  allocates it) and opt-in `SANDLOOM_CI_LOCAL_TIMEOUT_SCALE` for overloaded hosts
  ([Maryarty/auto_job_applyer#84](https://github.com/Maryarty/auto_job_applyer/pull/84)).
- Release measurement ([Maryarty/auto_job_applyer#86](https://github.com/Maryarty/auto_job_applyer/pull/86)): cachegrind, login-free SaaS mixes, this
  release vs 276fe84 — no regression in instructions per request (monolith
  −0.5..−0.7 %, split ±0.2 %; split premium +5.3..+6.0 % in instructions);
  wall-clock (CPU µs, rps, latency percentiles) deferred to a dedicated Linux
  machine.
- Local Docker CI runner `tools/ci/ci_local.sh` (act + fallback) while GitHub
  Actions is unavailable ([Maryarty/auto_job_applyer#58](https://github.com/Maryarty/auto_job_applyer/pull/58), [Maryarty/auto_job_applyer#59](https://github.com/Maryarty/auto_job_applyer/pull/59)); installed-wheel RPC transport smoke
  `tools/packaging/check_wheel_rpc_transport.py` ([Maryarty/auto_job_applyer#67](https://github.com/Maryarty/auto_job_applyer/pull/67)).
- Benchmarks: ladder harness fixes ([Maryarty/auto_job_applyer#41](https://github.com/Maryarty/auto_job_applyer/pull/41)), RPC / write-path diagnostic stands
  ([Maryarty/auto_job_applyer#42](https://github.com/Maryarty/auto_job_applyer/pull/42), [Maryarty/auto_job_applyer#60](https://github.com/Maryarty/auto_job_applyer/pull/60)), merged-BEGIN soak ([Maryarty/auto_job_applyer#49](https://github.com/Maryarty/auto_job_applyer/pull/49)), integration run ([Maryarty/auto_job_applyer#40](https://github.com/Maryarty/auto_job_applyer/pull/40)), argon2
  presets and split premium ([Maryarty/auto_job_applyer#65](https://github.com/Maryarty/auto_job_applyer/pull/65)), full SaaS-mix stand ([Maryarty/auto_job_applyer#39](https://github.com/Maryarty/auto_job_applyer/pull/39)).

### Migrations in this release

| chain | revision | what |
|---|---|---|
| core (every hosting database) | `0006_contract_command_log` | durable command dedupe table |
| tenancy | `0004_tenancy_keyset_order_indexes` | keyset-order indexes (plain `CREATE INDEX`) |
| tenancy | `0005_tenancy_member_versions` | per-(org, user) member version |
| billing | `0004_billing_operation_intents` | operation intents |
| billing | `0005_billing_subscription_origin` | `origin` (backfill `legacy`, default `psp`) |
| billing | `0006_billing_subscription_intent` | `intent_id` + partial UNIQUE |
| billing | `0007_billing_intent_reconcile` | reconciliation columns |
| billing | `0008_billing_credit_grants` | credit grants, payment states, CHECK `amount >= 0` dropped |
| billing | `0009_billing_intent_issued_at` | `issued_at` |

## [0.6.0] — 2026-10-10

0.5.0 was never tagged: everything listed for it below ships in 0.6.0, and
the deprecations announced "for removal in 0.6.0" move to 0.7.0 (a
deprecation lasts at least one released minor). Breaking changes first.

### Upgrade notes (0.4.0 → 0.6.0)

1. **`get_core` is async.** `Depends(get_core)` is unchanged; replace every
   direct call `core = get_core(request)` with `core = await get_core(request)`,
   or — where the caller cannot await — with the new SYNC accessor
   `sandloom.core.di.core_from_request(request)` (same value). The same applies to
   a hand-called `Capability(...)` dependency and billing's `get_psp`.
   `sandloom check` flags every un-awaited direct call (rule `get-core-await`).
   Why no compatibility shim: FastAPI runs a SYNC dependency in the thread
   pool, so a `get_core` that is both a sync function (for direct calls) and
   a no-hop dependency cannot exist; a sync `get_core` returning an awaitable
   proxy would keep the per-request thread hop the change removes. The old
   form fails loudly (a coroutine, "never awaited" warning), never silently.
2. **`tenant_rls` deployments**: follow "Security — breaking (tenant RLS)"
   below (`upgrade_tenant_rls` revision per RLS table, table ownership off the
   app role, optional system channel).
3. **Execution planner (new, opt-in)**: nothing changes for code that does not
   use it. To adopt: declare `ReadOperation` / `JoinRead` next to the model,
   switch a read-only route from `db: DbSession` to `reads: PlannedReads` and
   call `await reads.one(op, **key)`; a route that writes keeps `DbSession`
   and adds `reads: UowReads`; code holding a session uses
   `sandloom.core.orm.session_reads(session)`. Enable the driver-level executor
   per deployment with `PLANNER_OPTIMIZED_EXECUTOR=true` (PostgreSQL +
   asyncpg). See `docs/design/execution-planner.md`.

### Added — execution planner (declared operations)

Public surface (additive; `public_api_baseline.json` updated):

- **`sandloom.core.orm.ReadOperation(entity, result, by=("id",), database=, name=)`**
  — a declared single-row read by an equality key; the result is ALWAYS the
  declared pydantic DTO (never an ORM entity), whichever executor runs it.
  Tenant scoping is inferred from `TenantScoped` and cannot be opted out of.
- **`sandloom.core.orm.JoinRead(..., join=, via=, join_by=, join_where=,
  joined_fields=)`** — a single-row read of `entity` joined to `join` on a
  DECLARED foreign key to its primary key; `by` must cover a primary / unique
  key (at most one row on every executor); the tenant predicate is applied to
  every `TenantScoped` side. Tenancy's membership guard
  (`MembershipService.get_role`) now runs the declared
  `member_role_in_active_org` (same SQL, same semantics).
- **`sandloom.core.orm.Reads`** (the handle) and **`sandloom.core.orm.session_reads(session)`**
  — declared reads ON an open Unit of Work (request `DbSession`,
  `db_session()`, `contract_session()`): the unit's transaction, connection
  and tenant; read-your-own-writes.
- **`sandloom.core.di.PlannedReads`** (`get_planned_reads`) — a route DECLARED
  read-only: `publish_event` / `record_audit` / `on_commit` raise
  `ReadOnlyScopeError`, the database transactions are `READ ONLY`, and the
  route may not also open a Unit of Work (`PlanningError`). General-executor
  reads of one route share ONE read-only transaction (the scope's read unit),
  so N declared reads cost what the same reads on a `DbSession` cost.
- **`sandloom.core.di.UowReads`** (`get_uow_reads`) — declared reads inside the
  route's read-write Unit of Work (mixed routes); always the general executor.
- **`ModuleAPI.register_operations(*ops)`** — a module declares its
  operations in `register(core)` / `setup`: each is planned by a CRITICAL
  startup task (a declaration that does not match the schema fails the boot),
  the executor the kernel picked and the reason are logged
  (`sandloom.core.planner`, e.g. `general executor — 'label' is not a plain
  column attribute`), and `/diagnostics/core` lists every registered
  operation with its current plan under `planner`. Tenancy registers
  `member_role_in_active_org`.
- **Isolation is part of the read contract.** A read-only scope is READ
  COMMITTED, explicitly (`BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY` /
  the read unit's `isolation_level`): two reads of one scope MAY see
  different snapshots. **`sandloom.core.di.SnapshotReads`** (and
  `read_scope(..., isolation="repeatable_read")`) gives the whole scope ONE
  `REPEATABLE READ READ ONLY` snapshot (every read runs in the scope's read
  unit, on either executor); any other isolation is refused at planning.
- **Locking reads are never planned**: `ReadOperation(..., lock=...)` is
  refused at declaration (`PlanningError`); locks stay on `DbSession` +
  `select(...).with_for_update(...)`, whose behaviour is unchanged (tested);
  a planner transaction (READ ONLY) refuses `FOR UPDATE` outright.
- `sandloom.core.di.core_from_request(request)` — sync accessor of the runtime;
  `sandloom check` rule `get-core-await`.
- `sandloom.core.planner.read_scope(core, owner)` — the read-only scope outside
  HTTP (workers, scripts). `DatabaseProvider.execution_adapter()` — optional
  capability-contract method (default `None`: declared operations cannot run
  on that database, planning says so).
- Setting **`PLANNER_OPTIMIZED_EXECUTOR`** (default `false`): run eligible
  operations (single table / FK join, every DTO field a plain column,
  PostgreSQL + asyncpg, read-only scope) on the driver-level executor —
  `BEGIN READ ONLY; set_config(tenant)` in one message, the generated
  statement with an explicit tenant predicate, `COMMIT` (one autocommit round
  trip without `tenant_rls`). Every other operation runs on the general (ORM)
  executor; `Plan.reason` says why.

Guarantees (both executors, one shared layer; tests in `benchmarks/tests/
test_planner_*.py` and the S5 suite; `test_planner_equivalence.py` runs every
declared operation on both executors and both entry points and requires equal
results, equal errors and no side effects): the tenant is bound per execution and
never stored in a plan (fail closed before any I/O), explicit tenant predicate
+ RLS, read-only enforced twice (scope + database), one connection lease that
terminates and invalidates on any error or cancellation.

Measured (`benchmarks/ladder/RESULTS.md`, S5 secure read): general executor =
request UoW (842 vs 842 µs CPU/request), optimized executor 478 µs (−43 %).
Two declared reads on the general executor now share one transaction
(ladder rungs `L6S5two` / `L6S5p2` / `L6S5po2`; see the design doc §16).

### Changed — breaking (request-path dependencies are async)

- **`sandloom.core.di.get_core` is now `async def`.** FastAPI runs a sync `def`
  dependency in the anyio thread pool on every request; `get_core` (an
  attribute read) cost a thread hop per request. `Depends(get_core)` is
  unchanged; a direct call becomes `core = await get_core(request)`. The
  `Capability(...)` dependency and billing's `get_psp` are async for the same
  reason. A test (`core/tests/test_async_dependencies.py`) enumerates every
  framework dependency of the composition root's routes and of the public
  dependency factories and fails on a sync one.

### Security — breaking (tenant RLS)

Breaking for deployments that enabled `tenant_rls` (opt-in; no bundled module
or example does). Per this project's 0.x policy (a **minor** release may
contain breaking changes, listed first) this ships in the next minor (planned
as 0.5.0, released as 0.6.0).
No transitional flag restores the old bypass — keeping it would keep the
vulnerability. The one transitional knob is `verify_roles: false`, which skips
the new startup role checks with a loud warning while roles are being fixed;
it is deprecated on arrival and will be removed in 0.7.0.

- **The tenant RLS policy no longer has a bypass.** The previous predicate
  accepted `current_setting('sandloom.tenant_bypass') = 'on'`, a custom setting any
  role may set, so any SQL-capable path running as the application role
  (`text()`, `exec_driver_sql`, the raw driver connection, injected SQL) could
  read and write every tenant on `tenant_rls` databases. The policy is now
  `tenant_id = <canonical sandloom.tenant_id or NULL>`: no "all tenants" value; an
  unset/empty/malformed setting yields zero rows and refused writes instead of
  a cast error.
- **`unscoped()` lifts only the ORM guard** on a `tenant_rls` database (a
  warning is logged when it runs with no tenant bound). Behaviour without
  `tenant_rls` is unchanged.
- **System channel** for cross-tenant work:
  `sandloom.core.infra.drivers.system_db.system_session(actor=, reason=,
  database=, tenant_id=)` — a separate engine logged in as a separate
  least-privilege role (`DATABASE_SYSTEM_URL` / INFRA `system_url`), reaching
  only tables migrated with `enable_tenant_rls(..., system_access=True)`;
  every session is logged and audited (`sandloom.system_db.session`). Kernel-only:
  the `infra-imports` rule now also rejects dynamic imports of driver
  modules and `sandloom.core.sql_alchemy.tenant_rls`.
- **Startup role verification** (`verify_roles`, default on): a `tenant_rls`
  database refuses to start — in every environment — when the application
  role is superuser / `BYPASSRLS` / owner of an RLS table / member of a
  privileged or the system role, or when a legacy bypassable policy is left.
  Runs in the API lifespan and at Celery worker boot (`verify_worker_infra`).
  New options `require_system_channel`, `verify_roles`, `system_url`.
- **Migration:** add a revision calling `sandloom.core.orm.upgrade_tenant_rls(op,
  "<table>")` for every RLS table and move table ownership off the app role
  (docs/concepts/multi-tenancy.md). Residual risk (documented, not fixed):
  SQL running as the app role can still set `sandloom.tenant_id` to another
  tenant — see the RLS threat model in docs/reference/security-model.md.

The framework absorbs per-module boilerplate
([ADR-0002](https://sandloom.dev/docs/adr/0002-framework-absorbs-module-boilerplate/)): a
business module now carries only its domain. Nothing here is breaking —
existing conftests, env.py files and hand-written `register(core)` keep
working.

### Performance — secure tenant reads (S5)

Measured on the benchmark stand in `benchmarks/` (`benchmarks/ladder/RESULTS.md`:
uvicorn uvloop + httptools, 1 worker, conc 64, interleaved A/B rounds, server
CPU per request; Apple M-series laptop, PostgreSQL in Docker — absolute
numbers are host-specific, the deltas are the point). S5 = JWT +
`TenantScope` + `require(Subscribed())` + a `TenantScoped` PK read under
FORCEd RLS as a NOBYPASSRLS role. Default ON (no configuration):

- **`tenant_rls` hook without engine events.** The tenant GUC is now set by
  wrapping the dialect's `do_execute` / `do_executemany` /
  `do_execute_no_params` (+ `do_begin` / `do_commit` / `do_rollback` for the
  per-transaction state) instead of `before_cursor_execute` and transaction
  listeners — any engine event moved every statement onto SQLAlchemy's
  instrumented path. Same semantics (transaction-local `set_config`, lazy
  re-sync, first statement of every transaction syncs, ROLLBACK TO SAVEPOINT
  re-syncs, fail-closed) and the same round trips; S5 −19…−33 µs/request,
  −196k instructions. Covered paths: ORM, Core, `text()`, `exec_driver_sql`,
  executemany / insertmanyvalues, streaming, savepoints,
  `session.connection()`, `run_sync`; the raw driver connection is not
  covered and fails closed in PostgreSQL (tests:
  `src/sandloom/core/tests/test_tenant_rls_paths.py`, incl. a reference-cycle guard
  that caught a first version's p99 regression).
- **`ProcessTimeMiddleware` caches its labelled Prometheus children** per
  (method, route template, status), reads method/route off the ASGI scope (no
  `Request`), bounded cache; identical `/metrics` output and `X-Process-Time`
  (tested against the previous implementation). −4…−17 µs/request.
- **The ORM tenant guard's criterion is built once**; the tenant value is a
  bound parameter read from the context at execution time (never cached in the
  statement or the compiled cache). −20…−32 µs/request, −174k instructions,
  cyclic garbage per request 5 → 0 (tests:
  `src/sandloom/core/tests/test_tenant_criteria_prebuilt.py`).

Experimental, default OFF:

- **`DB_RLS_MERGED_BEGIN`** (prototype, `tenant_rls` + asyncpg): `BEGIN …;
  SELECT set_config(tenant)` as ONE simple-query message, the adapter handed
  a started asyncpg transaction; any failure or cancellation in flight
  terminates and invalidates the connection. RLS read UoW 4 → 3 round trips,
  S5 −41 µs/request (tests: `tests/perf_budgets/test_rls_merged_begin.py`
  incl. cancellation at every await point).
- `DB_LAZY_BEGIN` and `DB_READ_ONLY_SESSIONS` (below) remain prototypes.

### Added

- **Declared read-only sessions prototype** (`DB_READ_ONLY_SESSIONS`,
  default **off**; `ReadOnlyDbSession` / `get_read_only_db` in `sandloom.core.di`).
  A route that DECLARES `db: ReadOnlyDbSession` gets an autocommit read
  executor (one round trip per SELECT, no BEGIN/COMMIT) that REFUSES any
  write, flush, `publish_event`/`record_audit`, locking read or raw statement
  with `ReadOnlySessionError` before it is sent. No guessing: undeclared
  routes are unchanged. With `tenant_rls` the reads keep their transaction so
  `SET LOCAL` covers them. Off: `ReadOnlyDbSession` is exactly `DbSession`.
  Tests: `tests/perf_budgets/test_read_executor_{semantics,requests}.py`.
- **Lazy BEGIN prototype** (`DB_LAZY_BEGIN`, default **off**,
  `sandloom.core.sql_alchemy.lazy_begin`). Defers a Unit of Work's `BEGIN` until
  its first non-read statement: plain compiled `SELECT`s before it run in
  autocommit and a read-only UoW sends neither `BEGIN` nor `COMMIT` (S3:
  3 → 1 PostgreSQL round trips, unchanged module code). Applied only where it
  is equivalent — server default isolation `READ COMMITTED`, no explicit
  isolation/readonly/deferrable, no `tenant_rls` on the engine, no locking
  `SELECT`, no `execution_options(sandloom_lazy_begin=False)` opt-out (required for
  a `SELECT` calling a side-effecting function). Measured in
  `benchmarks/ladder/RESULTS.md`; budget/semantics tests in
  `tests/perf_budgets/test_budget_lazy_begin.py`.
- **Overflow-linger connection pool** (`LingeringQueuePool`, the driver's pool
  for every server database; `DB_POOL_OVERFLOW_LINGER_SECONDS`, default 30,
  `0` = stock QueuePool). Stock QueuePool closes a returned overflow
  connection as soon as `pool_size` others are idle and reconnects on the next
  shortage, so load hovering above `DB_POOL_SIZE` churned connect/close
  continuously (TCP + TLS + SCRAM + session setup on the event loop: ~15.8 ms
  CPU per connect with asyncpg 0.30, 3.7 ms with 0.32). This was the root
  cause of the CI uvicorn `--loop uvloop` vs `asyncio` S3/S4 anomaly (a
  positive feedback loop sensitive to loop scheduling). Linux docker, S3, 64
  connections, old stack: uvloop 3200 -> 1310 us/req CPU, asyncio 1830 ->
  1400, ~350 -> 30 connections per run; current stack: 1540-1850 ->
  920-1140 us/req, p99 2-3x lower. The total bound
  (`DB_POOL_SIZE + DB_MAX_OVERFLOW`) is unchanged; after a quiet window the
  pool shrinks back to `DB_POOL_SIZE` on checkin (an idle process keeps its
  surplus until the next checkins or `pool_recycle`). Budget:
  `perf_budgets/test_budget_pool_churn.py`.
- **Route dispatch index** (`sandloom.core.http.route_index`, installed by
  `create_app` once every module is loaded): requests scan only the routes
  whose leading literal path segments (up to 3, e.g. `/api/v1/orgs`) can
  match, plus every parameterised-prefix / `Mount` / custom route, in the
  original registration order — matching semantics identical to Starlette's
  linear scan (FULL precedence, first PARTIAL -> 405, `redirect_slashes`,
  404, websockets). Rebuilt automatically when routes are added later.
  cachegrind, full ASGI stack, platform app (79 routes): `/health/simple`
  1.07M -> 0.60M instr/request, 404 1.44M -> 0.48M, `/api/v1/orgs` -2.8e5.
- **pytest-xdist support** (`sandloom.core.testing.workers`): under
  `pytest -n auto --dist loadfile` each worker gets database `<db>_gw<N>`
  (created with citext/uuid-ossp) and Redis DB index `+ 16 * (N + 1)`
  (`SANDLOOM_TEST_REDIS_DB_STRIDE`; the Redis needs `--databases 256`). The
  harness re-applies it after every `.env.test` load; tests reload with
  `sandloom.core.testing.reload_test_env` / read the file for subprocesses with
  `worker_env_values` instead of bare `load_dotenv` / `dotenv_values`.
- `GUNICORN_KEEPALIVE` (default 75 s) — the workers' idle keep-alive
  (uvicorn `timeout_keep_alive`), above common LB idle timeouts. Each worker
  logs its loop / HTTP implementation at boot.
- **PostgreSQL round-trip budget tests** (`tests/perf_budgets`): a wire
  counting proxy pins the steady-state round trips of `GET /auth/me` (4),
  `GET /api/v1/orgs` (3), `POST /api/v1/orgs` (9), members (4), credits (7),
  the Stripe credit-pack webhook (12), a relay batch of 50 (6), billing spend
  and the claims contributor.
- `OutboxStore.deliver_batch(...)` / `purge_delivered(...)` and
  `RelayBatchResult` (additive contract; the defaults keep the per-row path,
  the SQLAlchemy store claims and settles a batch in a constant number of
  round trips).
- `OUTBOX_RETENTION_HOURS` (default 168, 0 = keep) and
  `sandloom outbox purge [--hours] [--batch] [--inbox-days]`.
- `DB_POOL_PING_IDLE_SECONDS`, `DB_STATEMENT_TIMEOUT_MS` (30000),
  `DB_IDLE_IN_TX_TIMEOUT_MS` (60000).
- `sandloom.core.unit_of_work.drain_post_commit()` / `PostCommitDispatcher`.
- Tenancy: optional keyset pagination (`limit` <= 500, `after`) on
  `GET /api/v1/orgs` and `GET /api/v1/orgs/{id}/members`.
- **`sandloom.core.testing` pytest plugin.** `sandloom.core.testing` is now a package
  (every existing name — `bind_test_uow`, `recording_uow`, `run_on_commit`,
  `InMemoryCache`, `RecordingSession` — stays importable). An application
  activates the harness once with `addopts = ["-p", "sandloom.core.testing.plugin"]`
  (no `pytest11` entry point, so no other suite changes): it loads the nearest
  `.env.test` before any framework import and provides `sandloom_app`, `db_engine`
  (disposable-test-database guard, schema reset incl. core infra tables),
  `db`, `client` (requests share `db` as the UoW), `make_headers`,
  `user_headers` and the aliases `test_db` / `async_client` / `test_engine`;
  pools are released per test. Ini option `sandloom_composition_root`.
  Framework-tree modules import the fixtures from a one-line conftest
  (`from sandloom.core.testing.fixtures import *`).
- `sandloom.core.testing.make_user_headers(user_id=None, tenant_id=None, **claims)`
  — access token with `token_type`, `iat`, `tenant_id` + `tenant_ids`,
  `email` / `email_verified` defaults.
- `sandloom.core.migrations.run_module_migrations(__file__)` — a module chain's
  env.py in two lines (package, `<pkg>.models` and `<module>_` derived from
  the env.py location; keywords override).
- `sandloom.core.module_api.define_module(__name__, version=..., consumes=...,
  capabilities=..., setup=...)` — declarative `register`: mounts `<pkg>.api`'s
  router, registers `<pkg>.events.EVENT_SCHEMAS`, calls `setup(core)`,
  returns the `ModuleDescriptor`.
- `sandloom check` rule **`test-imports`**: module tests must not import
  `sandloom.core._internal`, `sandloom.core.sql_alchemy`, `sandloom.core.database` or
  `sandloom.core.infra.drivers` (use `sandloom.core.testing`). Manifest allowlists are
  honoured; the bundled identity suite is allowlisted legacy debt.
- `sandloom-scaffold --conftest/--no-conftest`.
- More harness fixtures: `real_uow_client` (requests through the REAL request
  UoW on the test database/cache, no `db` override), `sandloom_cache` (the
  `default` cache on `.env.test`'s `CACHE_URL`, FLUSHDB'd per test; yields the
  provider), `ambient_uow` (recording UoW for the whole test),
  `revoked_tokens`, `sandloom_core`; helpers `committing_uow(session_factory)`,
  `capture_effects(monkeypatch, *modules)`,
  `override_instance(core, capability, ..., driver=..., **options)` and
  `registration_surface(register)` (pin what `register(core)` registers).
- `InfraRegistry.instance_config(capability, name)`,
  `snapshot_instance(...)` / `restore_instance(snapshot)` — reconfigure an
  instance temporarily without touching registry internals.

### Dependencies (measured upgrade — see `docs/reference/dependencies.md`)

Locked → new: FastAPI 0.116.1 → **0.136.3** (capped `<0.137`), Starlette
0.47.2 → **1.6.0** (floor `>=1.3.1`; lock-only cap `<1.7`), pydantic 2.11.7 →
**2.14.0** (core 2.33.2 → 2.50.0), pydantic-settings 2.10.1 → 2.15.0,
SQLAlchemy 2.0.42 → **2.1.4** (`sqlalchemy[asyncio]`), asyncpg 0.30.0 → 0.32.0,
alembic 1.16.4 → 1.20.0, celery 5.5.3 → 5.6.3 / kombu 5.5.4 → 5.6.2, uvicorn
0.35.0 → 0.54.0, uvicorn-worker 0.3.0 → 0.4.0, orjson 3.11.9 → 3.13.0, msgpack
1.1.0 → 1.2.3, cryptography 45.0.6 → 50.0.2, python-multipart 0.0.20 → 0.0.32,
prometheus-client 0.22.1 → 0.26.0, PyJWT 2.13.0 → 2.15.1, psycopg2-binary
2.9.10 → 2.9.13, authlib 1.6.1 → 1.8.0, pillow 11.3.0 → 12.3.0, requests
2.32.4 → 2.34.2, python-dotenv 1.1.1 → 1.2.4 (plus transitive: anyio 4.15.1,
urllib3 2.8.0, idna 3.20, …). **redis-py stays 5.2.1** (hiredis 3.4.2): 7.4
costs +11..35 % instructions per command, 8.x +29..92 %, and no advisory
affects 5.2.1. argon2-cffi 25.1.0 and httpx 0.28.1 were already current.
Most of the bumps carry security fixes (python-multipart, cryptography,
PyJWT, msgpack, starlette, authlib, pillow, urllib3, anyio, requests).

cachegrind, full ASGI stack of the platform app, instructions per request
(N = 1000; before → after): claims-only authz route 1.26 M → 1.14 M (−9 %),
50-item `list[Item]` response 3.38 M → 2.27 M (−33 %), `/health/simple`
0.72 M → 0.77 M and 404 0.59 M → 0.65 M (+8 / +10 %; with the GC frozen
+1 / +3 % (0.715 M → 0.725 M, 0.586 M → 0.602 M) — the rest is SQLAlchemy 2.1's larger import graph making
gen-2 collections dearer). ORM operations on SQLAlchemy 2.1.4 are within ±4 %
of 2.0 (reads −2..−4 %, insert / `text()` +2..+4 %).

- **The request UoW now commits BEFORE the response is sent.** FastAPI ≥ 0.118
  exits a yield dependency after the response unless it is declared with
  `scope="function"`; every framework UoW dependency (`DbSession`,
  `Database(name)`, the authz / identity / tenancy guards) now is, so a client
  never observes a `200` before the write and its on-commit hooks (token
  revocation) committed. A hand-written `Depends(get_db)` must pass
  `scope="function"` too (FastAPI caches per scope; mixing them opens a
  second UoW). Pinned by `test_uow_commits_before_response.py`.
- **PostgreSQL URLs always name the driver.** SQLAlchemy 2.1 maps a bare
  `postgresql://` to psycopg 3: migrations now use `postgresql+psycopg2://`
  (`to_sync_url` also accepts a bare `postgresql://` and rewrites asyncpg's
  `ssl=` to libpq `sslmode=`); `alembic.ini`'s placeholder is explicit.
- The `sqlalchemy.ext.mypy` plugin (removed in 2.1) is dropped from the mypy
  config; DML `rowcount` is read through a `CursorResult` cast.
- `gunicorn_conf.server_implementation()` reads the running loop (uvicorn
  ≥ 0.36 passes a loop factory instead of installing a policy); the boot log
  line resolves the worker's configured `loop` setting.
- `sandloom new` apps get an optional `fast` extra (`sandloom-framework[fast]`:
  hiredis, jsonschema-rs) that their `dev` extra includes and the generated
  Dockerfile installs (`--extra fast`); `packages/sandloom-framework` gains the
  same `fast` extra and the packages' dependency floors follow the backend.
- `benchmarks/` pins follow `uv.lock` (`check_versions` passes).
- Python 3.14: the locked set installs from wheels (`uv sync --frozen --python
  3.14`, no source builds) and the full suite passes on 3.14.8.

### Changed

- **Python floor 3.11** (framework, `packages/*`, examples, `sandloom new`
  template); `.python-version` and the images use **3.12**. ruff targets
  py311 (`datetime.UTC` etc. applied; `UP042` StrEnum rewrite deliberately
  ignored).
- Runtime deps: `uvicorn[standard]` (uvloop + httptools) and
  `uvicorn-worker`; gunicorn's worker class is
  `uvicorn_worker.UvicornWorker` (`uvicorn.workers` is deprecated upstream).
- **Production image** rebuilt around a uv-built `/opt/venv`: dependency
  layer cached on `uv.lock` (bind + cache mounts), dependencies
  byte-compiled at install, the sandloom package compiled separately, no uv / pip
  / setuptools / tests / `.env` in the runtime (new `.dockerignore`).
  `import sandloom.main` in the container: 2190 -> 940 ms CPU; rebuild after a
  code change 5.0 -> 2.7 s; image 441 -> 452 MB. Same layout for the
  `sandloom new` Dockerfile.
- `qrcode` / Pillow are imported only when a 2FA QR code is generated and
  `jsonschema` on the first payload validation (not at app import).
- CI: lint / format / mypy (cached) / architecture in a separate job parallel
  to a test matrix (3.11, 3.13; coverage on 3.12 with
  `COVERAGE_CORE=sysmon`) that runs `-n auto --dist loadfile`;
  `setup-uv@v10` (uv 0.12.24), `setup-python@v5`, `paths-filter@v3`.
- **Post-commit dispatch is off the request path.** `DbSession` /
  `Database(name)` / contract calls enqueue committed events to a per-process
  coalescer (publish in commit order, ONE batched mark-delivered UPDATE per
  database every <= 50 ms) instead of publishing and opening a second Unit of
  Work before the response. An in-process (memory) bus still receives events
  in line; `db_session()` still publishes and marks in line. At-least-once is
  unchanged (the relay delivers whatever the coalescer does not). Tests that
  assert `delivered_at` after a request `await drain_post_commit()`.
- **Pools**: `pool_pre_ping` (3 round trips per checkout) replaced by an
  idle-aware 1-round-trip ping; LIFO pools; `statement_timeout` /
  `idle_in_transaction_session_timeout` sent in the startup packet.
  `CONTRACT_DB_POOL_SIZE` / `_MAX_OVERFLOW` defaults 2 / 3 -> 5 / 5 (monolith
  compose app: 3 / 3).
- **Relay**: batched claim (`FOR UPDATE SKIP LOCKED`, per-batch token) and
  settle — 712 -> 6 round trips per batch of 50; no sleep after a full batch;
  the dead-letter sweep runs every 30 ticks.
- **Outbox indexes** (core migration `0005_outbox_partial_indexes`): full
  `ix_outbox_messages_pending` / `ix_outbox_messages_channel` replaced by
  partial `ix_outbox_messages_dead` / `ix_outbox_messages_delivered` (-33% WAL
  per insert + deliver).
- Module migrations drop duplicate indexes: tenancy `0003_tenancy`
  (`ix_tenancy_members_organization_id`), billing `0003_billing`
  (`ix_billing_credit_ledger_customer_id`).
- Tenant guard: no work on the ORM hot path when no `TenantScoped` model is
  mapped, and no loader criteria on statements that reach no tenant table.
- Billing: claims contributor 5 -> 2 statements, spend 5 -> 4, webhook mark 2
  -> 1, `BillingProduct.prices` no longer eager (`raise_on_sql`; the catalog
  loads it), an org billing context with a minimum role makes ONE membership
  contract call (was two).

- `sandloom-scaffold` / `sandloom new` generate the new forms: `define_module`
  `__init__.py`, 2-line `migrations/env.py`, tests on `client` /
  `user_headers` / `make_headers` / `db`; `sandloom new` projects load the plugin
  from pytest addopts and no longer get a root `conftest.py`; app modules get
  no `tests/conftest.py` (framework-tree modules get a one-line one).
- The example apps (linkly, helpdesk, projecthub) use the new forms; their
  bespoke conftests are gone (helpdesk keeps only its seed fixtures).
- The bundled tenancy and billing modules use the new forms: `register =
  define_module(__name__, ..., setup=_setup)` (registration surface pinned by
  a test) and conftests reduced to their domain fixtures on top of the
  harness; their `test-imports` allowlist entries are removed.
- The bundled `identity` module is declared with `define_module` (same
  routers in the same order, event schemas, critical claims contributor and
  2FA-key startup guard — pinned by `test_module_registration.py`). Its test
  suite runs on the shared harness (conftest 481 → 286 lines) and imports no
  kernel internals, so its `test-imports` allowlist entry is gone.
- `sandloom new` projects and the example apps set
  `filterwarnings = ["error::DeprecationWarning"]`; the framework's own suite
  errors on DeprecationWarnings attributed to `sandloom.*` code.

### Performance (contract runtime, internal RPC, event consumers, Celery)

Measured with deterministic counters (cachegrind instructions, Postgres /
Redis round trips); budgets pinned in `tests/perf_budgets/`.

- **Request-scoped read scope for local read contracts.** Every HTTP request
  opens a lazy `ContractReadScope` (`ContractReadScopeMiddleware`, wired by
  `install_service_rpc_routes`; `contract_read_scope()` for workers/scripts):
  local read-contract calls on one database share ONE contract-pool
  connection/transaction (READ COMMITTED, always rolled back, never the
  caller's transaction — a provider still never sees the caller's uncommitted
  writes, as over RPC), and identical read calls (contract, method, bound
  tenant, arguments) are memoised for the request; any command / legacy call
  (local or remote) clears the memo. The connection returns to the pool as soon
  as the request awaits anything else (`CONTRACT_READ_SCOPE_IDLE_MS=0`).
  A policy check (`get_role` + `has_role_at_least` + a repeat): contract-pool
  work 3 checkouts / 3 BEGIN / 3 statements / 3 ROLLBACK → 1 / 1 / 2 / 1;
  CPU per request −49%. Opt out: `CONTRACT_READ_SCOPE_ENABLED=false`
  (`CONTRACT_READ_SCOPE_MEMO=false` keeps sharing without the memo).
  Behaviour note: within one request an identical read returns the memoised
  value even if another transaction committed a change in between (a command
  made by this request always invalidates it).
- **Wire codec v2 + RPC codec negotiation.** Fully typed contract parameters /
  results (no `Any`, no ambiguous unions) cross the boundary as
  annotation-driven JSON-native values in pydantic-core
  (`sandloom.core.contract_wire.WirePlan`); immutable exact-type values skip the
  local round trip. Local `list[Item]`×100 call: 31.2M → 8.3M instructions
  (−73%); local UUID-pair call 315k → 182k. The describe document carries
  `contract.codecs`; a proxy sends typed arguments (`"codec": 2`) and accepts
  a typed result (`X-SANDLOOM-RPC-Accept-Codec: 2;<hash>`, answered with
  `X-SANDLOOM-RPC-Codec: 2`) only with a provider that advertises codec 2 — old
  and new peers interoperate both ways (codec 1 stays supported).
- **Thin internal RPC route.** `/_sandloom/rpc/*` are plain Starlette routes and an
  outermost `RpcFastPathMiddleware` serves them before the HTTP middleware
  stack and FastAPI dependency resolution (internal token, signed context,
  body cap, error envelope, request id and HTTP RED metrics are handled by the
  handler). RPC UUID-pair call through the full app: 2.53M → 0.91M
  instructions (−64%).
- **Streams consumer batching.** Consumers read `EVENT_STREAM_READ_COUNT`
  (default 64, was 16) entries per `XREADGROUP`; a batch's inbox dedupe is one
  `MGET`, its marks one pipeline and its acks one multi-id `XACK`: Redis round
  trips per 64-event batch 194 → 5 (3.0 → 0.08 per event). Failure handling is
  per message as before (a failed delivery is neither marked nor acked); marks
  / acks of a batch's successes are written after the batch, so a crash
  mid-batch redelivers them (at-least-once, as documented).
- **Event schema validation** goes through `sandloom.core.contracts.payload_validation`
  — jsonschema-rs when installed (pure-Python `jsonschema` fallback; same draft
  detection and format policy), compiled once per schema. The payload-size
  guard measures compact `orjson` bytes (the transport's encoding) instead of
  `json.dumps` with spaces / ASCII escapes, so the cap is slightly more
  permissive. Error messages of the Rust engine differ in wording. Preflight
  per event (5-field schema): 292k → 34k instructions.
- **Claims collection** runs independent contributors concurrently (issuer
  contributors first, in the issuer's session; same merge order and "first
  critical failure in registration order aborts" result; sequential when a
  test session is bound).
- Contract runtime: signatures / return hints cached; bound contract methods
  and proxy methods built once per instance.
- **Celery:** apps built by the driver and the worker default to
  `task_ignore_result=True` (opt out per task with `ignore_result=False`) and
  `worker_prefetch_multiplier=1`; `enqueue` sends with `ignore_result=True`
  (no per-task result subscription) on a small dedicated executor instead of
  `asyncio.to_thread`. Idempotent tasks should opt in to
  `acks_late=True, reject_on_worker_lost=True`.

### Added (performance)

- `sandloom-framework[fast]` extra: `redis[hiredis]` (C reply parser; consumer CPU
  per 64-event batch −15–20%) and `jsonschema-rs`. The production image
  installs it; the `dev` extra includes it.
- `CacheProvider.get_many` / `set_many` (features `get_many`, `set_many`;
  default fallbacks loop over `get` / `set`; Redis: `MGET` / one pipeline).
- `MessageTransport.ack_many` (Redis: one multi-id `XACK`),
  `ConsumerInbox.processed_many` / `mark_processed_many`.
- `BaseEventBus.publish_event(..., prevalidated=True)` is documented for the
  post-commit dispatch of already-preflighted UoW events too.

### Fixed (examples)

- helpdesk's ticket service no longer passes its session to
  `tenancy.membership.v1` contract methods (deprecated and ignored; a
  `TypeError` once the shim is removed).

### Fixed

- `test_uow_outbox` (and one `test_kernel_hardening` case) patched a dead
  module-global event bus and failed whenever an app had been built earlier
  in the same process (any API test first, or an xdist worker); they now bind
  the bus through the session / active-runtime seam like production.

- The Redis cache driver's default (and named-instance) client is now
  event-loop-aware: it is rebuilt when the running loop differs from the one
  it was created on (the stale client is closed best-effort), so a second
  loop in one process no longer fails with "Event loop is closed". Module
  conftests' `_reset_cache_client` workaround is obsolete.

### Reliability & performance — cache and auth hot paths

- **Bounded Redis cache client.** The `redis` cache driver's clients had no
  socket / connect timeout and an unbounded pool: a blackholed or hung Redis
  made every cache call await forever, so the fail-open / fail-closed
  branches of the rate limiter, token revocation, idempotency guard and login
  lockout never ran. Clients now carry `CACHE_SOCKET_TIMEOUT_SECONDS` (0.5),
  `CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS` (0.5), TCP keepalive,
  `CACHE_HEALTH_CHECK_INTERVAL_SECONDS` (30), a BLOCKING pool capped at
  `CACHE_MAX_CONNECTIONS` (64, waiting `CACHE_POOL_TIMEOUT_SECONDS` = 1.0) and
  a connection-error-only retry (`CACHE_RETRY_ATTEMPTS` = 1; timeouts are
  never retried). The default event-bus transport gets a sibling client
  without the read timeout (`get_default_transport_client`) — blocking
  `XREADGROUP` / pub-sub reads legitimately wait longer.
- **argon2 on a bounded pool.** Password hash/verify ran on the loop's
  default executor (`min(32, cpus + 4)` threads, ~64 MiB each): 32 concurrent
  logins peaked at 958 MiB RSS on a 10-CPU host. They now run on a dedicated
  pool of `PASSWORD_HASH_CONCURRENCY` threads (default `min(4, CPUs)`, cgroup
  aware) — 317 MiB for the same burst. `sandloom.core.di.run_password_hashing`.
- **passlib removed** (unmaintained; `crypt` DeprecationWarning on 3.11+).
  `sandloom.core.security` and identity's `AuthService` use argon2-cffi's
  `PasswordHasher` with the exact parameters passlib produced
  (`$argon2id$v=19$m=65536,t=3,p=4`, 16-byte salt, 32-byte digest): every
  stored hash verifies unchanged and does not need a rehash
  (`password_needs_rehash` / `AuthService.needs_rehash`). `passlib` /
  `types-passlib` are gone from the dependencies and the `sandloom new` wheel.
  `AuthService.pwd_context` is replaced by `AuthService.password_hasher`.
- **Fused cache operations** on `CacheProvider`, each with a portable default
  composed from the core ops (so every driver supports them) and a
  one-round-trip Redis implementation (features `incr_window`, `incr_many`,
  `set_nx_get`): the rate limiter's INCR+EXPIRE+GET-previous-window is one
  `EVALSHA` (2 → 1 round trips per limited request); the idempotency reserve
  is one `SET … NX GET` (Redis ≥ 7; degrades to SET NX + GET) — an idempotent
  first request costs 2 Redis commands instead of 3; a failed login bumps both
  lockout counters in one script. All Lua scripts now run via `EVALSHA`
  (script body sent once; `NOSCRIPT` reloads).
- **JWT verify** skips parsing the unverified header when no
  `JWT_PREVIOUS_SECRETS` are configured (the key is the current secret
  whatever the `kid`).
- **`scrub_text`** no longer rescans every position of long identifiers
  (lookbehind-anchored key + substring prefilters; output proven identical on
  a generated corpus): −58% instructions on a clean 80-char message,
  9.65 M → 79 k (~120x) on a 2 KB traceback.
- **identity migration `0003_identity`** drops the unselective boolean index
  `ix_refresh_tokens_is_revoked`: no query uses it (every `is_revoked`
  predicate is paired with an indexed `id` / `token` / `family_id` /
  `user_id`; the cleanup filters on `revoked_at`), it only cost an index
  write on every token mint and revocation. Run `sandloom-migrate upgrade-all`.
- **Login does 2 fewer Postgres statements** (9 → 7): the unused
  `selectinload(User.user_roles)` is gone and identity's claims contributor
  resolves roles + permissions in one LEFT JOIN.
- **Revocation freshness policy** (`sandloom.core.auth_freshness`, re-exported by
  `sandloom.core.di`). Revocation stays strict by default (every request reads the
  mark). A GET/HEAD route may declare
  `Depends(revocation_freshness(max_staleness_s=N))`, `0 < N <= 5`: a
  per-process copy of the mark (or its absence) at most N s old answers;
  `revoke_user_tokens` clears the revoking process at once and broadcasts on
  the Redis pub/sub channel `sandloom:auth:revocations`, other processes drop their
  copy on receipt, and a lost broadcast delays a revocation by at most N s.
  `create_app` refuses a budget on any other method (`RevocationPolicyError`);
  `strict_revocation()` (used by identity, tenancy and billing routers) and
  `AUTH_REVOCATION_STRICT_PATH_PREFIXES` make routes always strict (a budget
  there is ignored, logged). This replaces the global
  `AUTH_REVOCATION_LOCAL_TTL_SECONDS` knob of this release cycle (up to 300 s,
  every route), which is gone — the variable is now ignored (strict).
- **Emergency user block**: `block_user(user_id, reason=, actor_id=)` /
  `unblock_user(...)` (`sandloom.core.di`) write / remove
  `sandloom:auth:blocked:{user_id}` (no TTL), audited `auth.user.blocked` /
  `auth.user.unblocked` (`record_audit(required=True)`, so they must run in a
  UoW; a cache outage raises `BlockUnavailableError`, 503). The block mark is
  read live on EVERY authenticated request, whatever the route's budget
  (`401 "Account blocked"`); on a strict route it shares the revocation
  mark's round trip (`MGET`). Its cache outage follows
  `AUTH_REVOCATION_FAIL_CLOSED`. Issuers refuse a blocked user with a live
  check before any side effect — identity's password / 2FA login, SSO and
  SSO-2FA completion and the refresh exchange (`ensure_user_not_blocked`,
  `sandloom.core.di`). On commit the block also revokes the user's access tokens
  and publishes `core.auth.user_blocked.v1`; identity's new durable consumer
  `sessions@user_blocked` revokes every refresh token (works across the
  split: block from any service).
- Performance-budget tests (`tests/perf_budgets/`) pin the round trips:
  authenticated request = 1 Redis command (an `MGET` of the revocation and
  block marks; a single `GET` of the block mark on a budgeted route whose
  local copy is fresh), + 1 for a rate-limited route, idempotent first request ≤ 2 (+ auth), password login
  ≤ 3 Redis commands (+ 1 rate-limited) and ≤ 7 SQL statements.

## [0.4.0] — 2026-10-08

The split-deployment release: "one codebase, monolith or microservices by
configuration" is now verified for every module subset (the split conformance
matrix) and the review findings of that promise are fixed. Upgrade notes are
given with each breaking change.

### Breaking changes

**Contracts: identical semantics locally and over RPC**

- **A contract call is observably identical locally and over RPC.**
  Upgrade notes:
  - `register_service` now validates the provider: every PUBLIC member must be
    a `@contract_method` (or a deprecated legacy session-taking coroutine) —
    make helpers/state private (`_name`) — and every parameter and the return
    must be annotated with a contract wire type (`sandloom.core.contract_wire`:
    JSON scalars, `None`, `UUID`, `datetime`/`date`, `Decimal`, `Enum`,
    `Literal`, pydantic models, containers/unions of those, `Any`); `*args` /
    `**kwargs` are rejected. Violations raise `ContractTypeError` at
    registration (module load fails) instead of a remote `404`.
  - Local calls go through the same codec as RPC: arguments are bound with
    Python's rules and validated/coerced against the annotations, results are
    decoded against the return annotation (local callers now get exactly the
    types a remote caller gets; tuples/sets/Decimal/Enum/models survive RPC).
    A binding/validation failure is `ContractArgumentError` (422, a
    `TypeError`) on both paths, including unknown kwargs remotely.
  - `ContractProvider` (what `core.get_service` returns locally) exposes ONLY
    the contract's methods; private / non-contract attributes raise
    `AttributeError`.
  - A provider `HTTPException` / `http_status` exception now reaches a LOCAL
    caller as `ContractCallError` (an `HTTPException` subclass with the same
    status, JSON-encoded detail, headers and `code`) — exactly what the RPC
    client raises. `ContractIdempotencyKeyMissing` / `…Conflict` /
    `ContractCallInProgress` / `ContractIdempotencyUnavailable` are
    `ContractCallError` subclasses (still `ValueError` / `RuntimeError`).
  - Over RPC a domain `401`/`403` keeps its status (it was turned into a `503`
    "not authorized"); headers such as `Retry-After` are preserved; an unknown
    service/method at the provider is `501 ServiceUnavailableInDeployment`
    (it was a plain `404`); only transport/trust failures and provider crashes
    are `503 RemoteServiceUnavailable` (now with a `code`).
  - RPC keeps no idempotency record for reads, and none of its own for
    decorated commands (the contract runtime's dedupe, identical locally and
    remotely, covers them; TTL `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`, ≤ 7 days;
    results over 256 KiB are stored as a non-replayable marker →
    `ContractResultNotReplayable` 409 on a repeat). The proxy no longer invents
    a per-call idempotency key: legacy session-taking methods now really
    require an explicit `rpc_idempotency_key(...)` over RPC, and calls in one
    such block are keyed per (key, method, arguments) instead of colliding.
  - The proxy sends no signed context when there is neither a tenant nor a
    caller key, and a provider without `INTERNAL_API_TOKEN` in open-dev mode
    accepts an unsigned tenant-less context (every dev-mode RPC used to 503).

- **Adding an enum value is a breaking schema change.**
  `classify_schema_change` (schema registry, module-load preflight, contract
  diff) now reports added enum values as breaking, like removed ones: a
  consumer deployed in another service with the old schema rejects or
  mishandles the new value. *Upgrade:* publish the new shape as `.vN+1` and
  dual-publish (`publish_event_versions`) until every consumer moved.

**Eventing (split deployments)**

- **Consumer groups are namespaced by module: `<module>:<name>`.** Equally
  named consumers of two modules — or one module deployed in two services —
  used to share ONE Streams group and split each other's events (and, with
  their inboxes in different databases, double-apply after `XAUTOCLAIM`). The
  exactly-once inbox key follows the group. *Upgrade:* the new group is created
  at the old bare-name group's last-delivered position (no history replay);
  drain entries still pending in the old group (pin `group="<name>"`), then
  `XGROUP DESTROY` it. Consumers on a bare facade (tests/scripts) keep `name`.
- **Startup fails** on two consumers of one process resolving to the same
  group, and on a group already owned by another module / physical database
  (`sandloom:group-owner:<stream>:<group>`). After deliberately moving a module to
  another database: drain, then `sandloom streams release-group`.
- **Streams are trimmed**: `EVENT_STREAM_RETENTION_SECONDS` (default 7 days,
  `XTRIM MINID ~`) and optional `EVENT_STREAM_MAXLEN` (`MAXLEN ~`).
- **DLQ replay targets the dead-lettering group only** (other groups ack-skip
  the replayed entry); `sandloom streams dlq-replay --group`.
- **`sandloom projection rebuild` no longer enters the ASGI lifespan** (it joined
  live consumer groups and, in prod, started the relay); it runs the startup
  tasks only. Run it as a job after the source services are up.
- Post-commit dispatch publishes one unit of work's events sequentially in
  emission order (stops at the first failure; the relay delivers the rest).

**Access facts, billing tenant resolution, token revocation**

- **Access facts are split-safe.** Registering a fact exposes the owner's
  `<owner>.facts.v1` contract (`describe()` with a declarative `ClaimRule`,
  `evaluate()` for live answers). `Entitled(...)` / `Subscribed()` in a process
  without the owner resolve the fact through the bound contract (cached 300 s)
  and decide exactly like the monolith — previously `500 fact_unregistered`.
  An unregistered/unbound fact is now indeterminate `501` (was `500`); a bound
  but unreachable owner is `503`. `AccessFact(claim_rule=...)` is the new
  declarative interpretation (`claim_interpret` still works locally, but such
  a fact is offered to other processes without claim mode); billing's
  `subscription.active` uses it, so `shared` no longer duplicates billing's
  `past_due` grace rule (with no subscription owner deployed, `Subscribed()`
  accepts only `active`/`trialing`).
- `Subscribed()` honours provenance: when the token's authoritative
  provenance shows billing did not contribute (e.g. `claims_sources_failed`),
  it escalates to billing's live answer instead of denying 402 for the token's
  lifetime (indeterminate 503 without a live path).
- Billing's tenant resolution no longer falls back SILENTLY to the personal
  tenant when `tenancy.membership.v1` is neither local nor bound but the token
  shows tenancy is deployed (`tenant_ids`/`tenant_id`/`org_id` claim or
  `tenancy` provenance): it answers `501`. An explicit `org_id` without the
  contract is now `501` (was `404`). Personal-only deployments (no tenant
  claims, no tenancy provenance) keep resolving the personal tenant.
- Token-revocation marks no longer expire with the WRITER's
  `ACCESS_TOKEN_EXPIRE_MINUTES`: they live `AUTH_REVOCATION_MARK_TTL_SECONDS`
  (new; default `max(access TTL + 120s, 24h)`), so in a split deployment a
  mark written by tenancy cannot expire before tokens minted by identity.

**Split deployment (probes, modules, bindings)**

- **Remote peers no longer affect `/health`, and affect `/ready` only when
  opted in.** `SERVICE_BINDINGS` entries are probed for readiness only with
  `"required": true` (previously every binding was "required" unless
  `"required": false`, and the probe ran in `/health` too — one stopped
  service made all its consumers unhealthy and not-ready). Upgrade: add
  `"required": true` to the bindings a service truly cannot serve without.
  Probes are concurrent, capped by `SERVICE_PROBE_TIMEOUT_SECONDS` (default
  1 s) and use the binding's own `internal_token`; `/diagnostics/services`
  reports every binding's reachability.
- **Modules listed explicitly in `SANDLOOM_MODULES` are required by default**
  (when `MODULE_REQUIRED` is empty): an import failure aborts startup.
  Upgrade: set `MODULE_REQUIRED` to the subset you need, or
  `MODULE_REQUIRE_LISTED=false` for the old behaviour. `/ready` is 503
  whenever modules were requested but none loaded.
- **`SERVICE_BINDINGS` is validated at startup**: unknown keys, non-`http(s)`
  URLs, bad `timeout_seconds` / `required` / `critical` values, and bindings
  with no internal token (global or per binding, outside the dev open-endpoint
  opt-in) are configuration errors.
- **`ModuleDescriptor.consumes`** (`ContractRequirement(name, required=,
  reason=)`): the loader checks at startup that every consumed contract is
  provided locally or bound in `SERVICE_BINDINGS` — a missing required one
  aborts startup (`ContractBindingError`), a missing optional one warns; the
  result is in `/diagnostics/core` (`modules.consumes`). Billing declares
  `tenancy.membership.v1` (optional).

**Deploy profiles**

- The microservices compose binds exactly each service's consumed contracts
  (identity binds only the claims contracts it collects, tenancy binds
  nothing, billing binds `tenancy.membership.v1`); a module added to a service
  must declare and bind what it calls.
- Postgres `max_connections` is set explicitly to 150 in both profiles; the
  contract pool is `CONTRACT_DB_POOL_SIZE=1` / `CONTRACT_DB_MAX_OVERFLOW=2`
  and beat runs with 1 + 0 pools (the stock defaults exhausted
  `max_connections` 100 at peak). Raising concurrency or pools means raising
  `max_connections` too (deploy/README "Sizing", pinned by
  `test_deploy_profiles`).
- The gateway 404s every unrouted path and no longer 301-redirects
  `/api/v1/billing`.

### Added

- **Split conformance matrix** (`tests/split_conformance`, marker
  `split_conformance`, part of the default suite and required in CI): boots
  every non-empty subset of identity/tenancy/billing as real processes —
  bound to a peer via `SERVICE_BINDINGS` and alone — and requires the bound
  variant to answer a request script (auth, orgs, members, invitations,
  billing, revocation, and `OrgRole` / `SubscriptionActive` / `Credits` /
  `Entitled` / `Subscribed` gates) identically to the monolith, the unbound
  one identically or with an explicit 404/501/503.
- `SANDLOOM_SERVICE_NAME` is set for every long-running process of both compose
  profiles.
- Eventing: consumer `start_from` (`earliest` | `latest` | timestamp) and
  `group=` pin on `register_event_consumer` / `ProjectionConsumerDescriptor`;
  opt-in `bind_tenant_from="tenant_id"` binds the payload tenant for the
  handler; `SANDLOOM_SERVICE_NAME` labels consumer ids / diagnostics;
  `EVENT_STREAM_PREFIX` namespaces every Streams key; per-group lag / pending
  (`sandloom_streams_group_lag`, `sandloom_streams_group_pending`, `/diagnostics/consumers`
  fields `group`, `module`, `service`, `lag`, `pending`); a Redis subscription
  inventory with `sandloom streams consumers` / `sandloom streams groups`; a publish-side
  warning + counters when no live consumer reads a channel or a sibling
  version starves; `publish_event_versions` (from `sandloom.core.di`) for v1 → v2
  dual-publishing.
- Framework worker sizing: `worker_concurrency` from `WORKER_CONCURRENCY` or
  the container CPU quota (capped at 2) instead of Celery's host-core default;
  `WORKER_MAX_MEMORY_PER_CHILD` / `WORKER_MAX_TASKS_PER_CHILD`; a heartbeat
  file + `python -m sandloom.core.worker healthcheck` liveness check.
- Deploy profiles: one named image (`SANDLOOM_IMAGE`), worker healthchecks and
  512 MiB limits, persistent `noeviction` Redis in the monolith too, shared
  auth/notifier/past-due settings in the common env anchor, parametrized
  gateway port / subnet / IP; the nginx gateway re-resolves services via
  Docker DNS, fails fast (2 s connect timeout), caps bodies at 50 MiB like the
  app, no longer 301-redirects `/api/v1/billing` and 404s every unrouted path.
  The smoke test adds a worker-task check and a peer-down scenario.
- Contract wire codec `sandloom.core.contract_wire` and the RPC describe document
  (`GET /_sandloom/rpc/{service}` → `contract: {hash, methods}`; responses carry
  `X-SANDLOOM-Contract-Hash`, the proxy re-describes on change).
- Contract conformance harness `sandloom.core.contract_testing`
  (`assert_contract_conformance`: local vs in-process RPC); applied to every
  shipped contract.
- Dedicated contract DB pool per database instance (`CONTRACT_DB_POOL_SIZE`,
  `CONTRACT_DB_MAX_OVERFLOW`) with `lock_timeout`
  (`CONTRACT_DB_LOCK_TIMEOUT_MS`): an in-process contract call / claims
  contributor no longer starves the request pool (a request pool of 1 used to
  deadlock until the pool timeout).

### Fixed

- Projecthub taskboard: a project update racing a task insert no longer
  strands stale project fields (local `taskboard_project_snapshot` serialises
  both writers; rebuild re-fans project fields). Docs: CONTRACTS.md no longer
  calls new enum values non-breaking for cross-service consumers; event
  ordering (none, even within a channel) is documented.
- `register_live_adapter` is documented as consumer-side configuration and
  warns when a provider module registers an adapter for its own contract.

## [0.3.0] — 2026-10-08

The security / correctness hardening release (review findings). Upgrade notes
are given with each breaking change.

### Breaking changes

**Kernel and contracts**

- **`create_app` moved into the kernel** (`sandloom.core.app_factory`, shipped in
  `sandloom-framework`). `sandloom/main.py` is only the batteries composition root; an
  `sandloom new` app builds on the kernel factory. Workers, migrations and the ops
  CLIs boot the composition root named by `SANDLOOM_COMPOSITION_ROOT` (default
  `sandloom.main`; `sandloom new` apps use `<app>.asgi`).
- **Contract methods no longer take the caller's session.** Every
  service-contract call (local or RPC) runs in the PROVIDER's own Unit of Work
  on its own database (`@contract_method` + `contract_session()`); a caller
  that still passes a session gets a `DeprecationWarning` and it is ignored.
  Commands (`@contract_method(command=True, idempotency_key=...)`) are not
  atomic with the caller's transaction (saga).
- **Command contracts are deduplicated by the framework, fail-closed.** The
  runtime records `(contract, method, tenant, idempotency key) -> (args
  digest, result)`: a repeat replays the stored result, the same key with
  other arguments raises `ContractIdempotencyConflict` (409), a concurrent
  repeat `ContractCallInProgress` (503) and an unavailable idempotency store
  `ContractIdempotencyUnavailable` (503) — a command never runs without the
  store. RPC-level dedupe of commands is fail-closed too (it failed open).
- **Legacy undecorated session-taking contract methods** are treated as
  commands (with a `DeprecationWarning`) and now REQUIRE an idempotency key
  when invoked over internal RPC (`422` without one). **They will be removed in
  the next minor release** — decorate them with `@contract_method`.
- **Internal RPC error mapping.** Only a failed method lookup is `404` and only
  argument binding/validation is `422`; an `AttributeError` / `ValueError`
  raised inside a provider is now a provider error (`500`), and exceptions with
  the `http_status` protocol keep their status.
- **Tokens require `iat`** (and `exp`); access tokens must carry
  `token_type: "access"` everywhere — including the idempotency principal and
  tenant identity (one bearer parser: `sandloom.core.security.bearer_claims`).
- **Token revocation after commit**: use
  `sandloom.core.di.revoke_user_tokens_on_commit(user_id, reason=...)`; the
  revocation mark is written atomically (CAS) and never moves backwards.
- **`sandloom check` gained the `kernel-handles` rule** (`core.event_bus`,
  `core.infra`, `core.service_registry`, `app.state.core` from module code).
  Applications that reached kernel handles now fail the gate.
- **`sandloom.core.module_api._contract_target`** carries the contract name (only
  relevant to code that set this private context variable).

**Production configuration** (startup fails otherwise)

- `TWOFA_ENCRYPTION_KEY` is required wherever identity is loaded (no
  `JWT_SECRET` fallback in prod).
- `BILLING_PROVIDER` must be chosen explicitly; the fake PSP is refused in
  prod unless `BILLING_ALLOW_FAKE_PSP_IN_PROD=true` (demos only); `stripe`
  requires `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET`.
- `INTERNAL_API_TOKEN` is required for split deployments: it authenticates
  internal RPC (signed caller context) and unlocks the internal endpoints.

**Identity**

- `POST /auth/register` always answers **`202`** with the same body (no
  account enumeration); password accounts sign in only after email
  verification (`403` before).
- 2FA management endpoints (`/auth/2fa/init`, `/enable`, `/disable`,
  `/backup-codes/regenerate`) require **`current_password`** (password-less
  accounts: an access token issued in the last 300 s).
- **SSO flow**: `/redirect` returns `{auth_url, state, binding}` and sets the
  `sandloom_sso_binding` cookie; the callback must present the binding (cookie or
  `X-SSO-Binding`), uses PKCE / OIDC nonce, and a 2FA account receives an MFA
  challenge to finish with `POST /auth/sso/mfa`. An email auto-link to a 2FA
  account is written only after the second factor; a mis-bound callback no
  longer consumes the state.
- **Refresh rotation grace window**: replaying a token rotated at most 10 s ago
  (or losing a concurrent refresh) while the family has a live successor
  answers **`409`** ("retry with the new token") instead of revoking the
  family; beyond the window reuse still revokes the family (`401`).
- **Lockout keyed per (account, client IP)** with a soft per-account delay
  across IPs (from 20 failures, up to 5 s per attempt) — a third party can no
  longer lock an account out. `lockout.ensure_not_locked` /
  `register_failure` / `reset_failures` take an optional `client_ip`.

**Tenancy**

- `PATCH /api/v1/orgs/{id}` no longer accepts `is_active` (`422`);
  deactivation is the owner-only delete.
- Accepting an invitation requires the token's `email` to match **and**
  `email_verified: true`.
- Only the **owner** may invite with `role: "admin"` (`403` for admins).

**Billing**

- The PSP client is no longer a service contract: **`billing.psp.v1` was
  removed** (use billing's own endpoints; tests use `install_psp_client`).
- Subscription / credits gates deny with **`402`** (payment required).
- `billing.credits.v1.spend`: an `"organization"` spend must match the bound
  tenant (`403`); personal (`"user"`) spends are not checked against it.
- Billing services raise domain errors (`sandloom.billing.exceptions`:
  `InsufficientCreditsError` 402, `CreditOperationConflictError` 409,
  `BillingTenantMismatchError` 403) instead of `HTTPException`; the HTTP
  statuses are unchanged and the contract still raises `HTTPException`.
  `CreditsService.get_balance` was removed (use the contract's `get_balance` /
  `EntitlementsService.get_credit_balance`).
- Credit-pack checkout webhooks require `metadata.customer_id` and an
  `amount_total` + `currency` equal to the catalog price; otherwise the event
  is journaled `failed` and nothing is granted.

### Added

- `sandloom check` — the framework's architecture analyzers as a CLI for apps.
- `sandloom new` emits per-module migration chains, worker/beat, CI and `sandloom check`;
  `sandloom-migrate revision <module>` for per-module chains.
- Framework Celery worker entrypoint (`sandloom.core.worker`, `run_async`).
- Signed RPC caller context (tenant + idempotency key) and idempotent RPC.
- Opt-in PostgreSQL row-level security (`tenant_rls` database feature).
- JWT key rotation (`JWT_KID`, `JWT_PREVIOUS_SECRETS`) and framework-wide token
  revocation (`revoke_user_tokens`, `revoke_user_tokens_on_commit`).
- `sandloom.core.testing.InMemoryCache` — a full `CacheProvider` (incl. `cas`) for
  unit tests.
- Optional PSP capability `retrieve_subscription` (Stripe + fake): webhook
  same-second ties apply the PSP's current subscription state.
- Two-process split-deployment e2e (`tests/e2e`); with
  `SANDLOOM_E2E_REQUIRED=1` (CI) missing infrastructure fails instead of skipping.
  CI also smoke-tests the microservices compose profile through the gateway
  (`deploy/smoke/microservices-smoke.sh`).

### Fixed

- Many security fixes across identity (SSO takeover/login CSRF/2FA bypass,
  TOTP replay, refresh reuse detection, single-use reset links), tenancy
  (org lifecycle, invitations) and billing (fail-closed webhooks, monotonic
  subscription events, catalog-based credit grants); fail-closed tenant
  isolation and internal endpoints; CAS-guarded idempotency transitions;
  durable event consumers with supervision and DLQ tooling.
