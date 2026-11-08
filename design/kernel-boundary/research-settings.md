# Research: module-owned settings and the bootstrap of every entry point

tm `kb-research-settings` (parent `kb-assess`, input to decision D, `kb-alt-settings`).
Question (owner, 2026-11-08): settings are validated before modules load
(`create_app` → `enforce_configuration`). For a module to own its settings, do we need a
discover → validate → activate bootstrap that behaves the same in the app, the worker,
beat, `sandloom migrate` and the CLI, with tiers, doctor, diagnostics, secret redaction,
production checks and accepted risks intact and no environment read at module import?
And is a smaller change enough?

**Short answer.** No new bootstrap phase is needed. `create_app` already runs
discover (import and buffered registration), then validate, then activate (lifespan, worker
loop, migration run), and every entry point except two CLI commands builds the app through
it. Two changes are enough:

1. a module's settings are validated **at its registration**, from the source values the
   kernel already read, before its `setup` runs;
2. the checks that span all modules run **once, after the load pass**: env-name collisions,
   accepted-risk ids, removed settings, production checks, override reporting.

The research also found a parity gap that exists today: the module production refusals
(`TWOFA_ENCRYPTION_KEY`, the implicit fake PSP) run only in the ASGI lifespan. The worker,
beat, `sandloom migrate` and the operator CLIs build the same app and start anyway
(probe 1).

Evidence is at `38f33cb` (`arch/kernel-boundary`). For the files read here it is the same as
the fixed commit `b8d8506`: `git diff --stat b8d8506 HEAD -- src/sandloom/core/{config,app_factory,loader,cli} src/sandloom/core/{worker,composition}.py src/sandloom/{main,__init__}.py`
is empty. Paths below are relative to `src/`. Probes and their output are in
[`_evidence/research-settings/`](_evidence/research-settings/).

## 1. Current boot order, per entry point

### 1.1 The shared core: `create_app`

All rows of §1.2 except `config dump` reach this sequence
(`sandloom/core/app_factory/__init__.py:146-255`):

| step | what | settings read | file:line |
|---|---|---|---|
| 0 | composition root import: `sandloom.main` evaluates `default_modules()` (entry points only, no settings), then `app = create_app()` **at import** | — | `sandloom/main.py:27-30`, `:38`; `core/descriptors/module_names.py:67-80` |
| 1 | `init_logging()`: **the first settings access** forces `Settings()` (env > `.env` > `SECRETS_DIR`, then all validators) | `log_level`, `log_as_json` | `app_factory/__init__.py:161`; `core/observability/logging.py:170-175`; `core/config/settings.py:765-782` |
| 2 | `enforce_configuration(settings)`: checks that need kernel code (JWT key material, RPC, SQL dialect/RLS/DBAPI, dev DB in prod, strict profile) | kernel fields | `app_factory/__init__.py:166`; `app_factory/config.py:316-375` |
| 3 | startup warnings and **override reporting** (tiers, accepted risks, metrics) | kernel fields | `app_factory/__init__.py:168`, `:258-275`; `app_factory/config.py:409-421` |
| 4 | infra registry, event bus, core schemas | infra fields | `:175-182` |
| 5 | `apply_module_defaults` writes `SANDLOOM_MODULES` / `MODULE_REQUIRED` into the settings singleton | module lists | `:187`; `core/loader/plugin_loader.py:166-198` |
| 6 | load pass: per module, `module_registration` draft → `import_module` → `register(api)` → `define_module` discovery → **`setup(core)`, where modules read their settings** → dependency, chain and capability preflights; then entry points; then the consumes preflight | module fields (see §2) | `:192`; `plugin_loader.py:490-558`, `:525-526`, `:571-631`, `:668-736`; `sdk/_decl/module.py:541-556` |
| 7 | post-load checks: storage, isolation warnings, manifest, sagas, guarantee windows, token-signing role, RPC routes, compat check, consumer runtime, health checks, routes | kernel fields | `:194-252` |
| 8 | **lifespan only** (ASGI): `run_startup`: infra phase, then module startup tasks; a failing `critical` task aborts | whatever tasks read | `app_factory/lifecycle.py:247-292`; `core/lifecycle.py:48` |

Module production refusals are step-8 critical startup tasks registered in step 6:
`sandloom_identity/__init__.py:28-35` (TOTP key), `:39-47` (JWT_SECRET), and
`sandloom_billing/__init__.py:41-48` (fake PSP). Both modules log the problem at ERROR in
step 6 and refuse only in step 8.

### 1.2 Entry points

| entry point | what runs, in order | reaches create_app | runs step 8 | settings forced at |
|---|---|---|---|---|
| API (`uvicorn`/`gunicorn sandloom.main:app`, or `<app>.asgi`) | gunicorn master loads the config only, never the app (`preload_app=False` required, `core/cli/gunicorn_conf.py:149-157`); each worker imports the root → §1.1 → lifespan | yes | **yes** | step 1 |
| Celery worker / beat (`celery -A sandloom.core.worker worker\|beat`) | `__getattr__("app")` → `get_worker_app` → `build_worker_app` → `load_composition_core` (`composition.py:55-68`; root name from `os.getenv` or `settings.sandloom_composition_root`, `:43-47`, which forces settings) → §1.1 steps 0-7 → `verify_worker_infra` (DB RLS/schema, strict profile) → Celery app → `discover_task_modules` imports `<pkg>.tasks` | yes | **no** | `composition.py:47` or step 1 |
| `sandloom migrate …` (compose one-shot: `deploy/compose/monolith.yml:116`; there is no separate `sandloom-migrate` script, `packages/sandloom/pyproject.toml:105-106`) | `sandloom:main` → `core/cli/migrate.py:440` → `enforce_configuration(settings)` `:446` → `migration_path_problem` `:449` → `_load_core()` `:469` → `load_composition_core` → §1.1 0-7 → command (every subcommand, including `databases` and `drift`, `:715-725`) | yes | no | `:446` |
| configured CLIs (`projection outbox streams contracts routes backup security config sagas`) | `sandloom/__init__.py:249` `_enforce_configuration()` (kernel only, `:117-123`) → subcommand; most call `load_composition_core` (`cli/backup.py:54`, `cli/security.py:110`) | yes, except **`config dump`** | only `projection rebuild` (`cli/projections.py:72`, `:97`) | `__init__.py:249` |
| `sandloom config dump` | `_enforce_configuration()` → `effective_config(settings)` iterates `type(cfg).model_fields` (`cli/config.py:160-195`, `:235-242`) | **no** | no | `__init__.py:249` |
| `sandloom doctor` | imports settings (`cli/doctor.py:533`, an invalid config becomes a FAIL line), `check_config`, `check_boot` (`configuration_problems`, `:205-211`), `check_overrides` (`:214`), then `load_composition_core` `:544` → modules, infra, … | yes (after the config section) | no | `:533` |
| `sandloom admin …` (static dispatch, `__init__.py:304-305`) | reads `settings.is_prod`, `frontend_url`, `admin_console_enabled` directly (`cli/admin.py:99`, `:124-132`, B-C10); `create`/`reset` call `load_composition_core` (`:83`); `export` builds the app (`:526-531`) | for acting commands | no | first read |
| `sandloom check --boot`, `sandloom generate` | `load_composition_app` (`cli/check.py:264-267`; `cli/generated.py:745`); `generate` first loads `.env.test` (`__init__.py:103-114`) | yes | no | step 1 |
| `sandloom add deploy`, deploy-profile check (T014) | `load_topology` → `load_module_topology`: a bare `CoreFacade`, registration only, consumes not enforced (`core/testing/deploy_profile.py:356-358`; `core/testing/boot.py:126-145`; `plugin_loader.py:652-665`) | no, but runs `register`/`setup` | no | first read |
| static (`new add check explain docs context graph keys test`) | build no settings by design (`__init__.py:74-82`) | no | no | never |
| tests | the pytest plugin loads `.env.test` into `os.environ` before settings are forced (`core/testing/plugin.py:114-130`, `core/testing/env.py:61`); fixtures call `create_app`; the singleton is changed with `setattr` (`core/testing/boot.py:69-86`, `core/testing/security.py:118`); tier tests build `Settings(_env_file=None, …)` directly (`core/tests/test_config_tiers.py:80-81`) | yes | where a test enters the lifespan | plugin |

Parity is tested today for the **kernel** checks only. G-CONFIG-07
(`core/tests/test_boot_configuration_checks.py:73-105`) runs four real processes (`import
sandloom.main`; `get_worker_app()`; `migrate main(['databases'])`; `sandloom config dump`)
and requires exit 1 with `INVALID_CONFIGURATION`. No case covers a module check.

## 2. What reads settings, and when

- **At import: nothing.** No bundled module reads `settings` or `os.environ` at module level.
  A grep found only a migration script reading `SANDLOOM_BILLING_DROP_ORIGIN` inside
  `upgrade()` (`sandloom_billing/migrations/versions/0005_billing_subscription_origin.py:65`).
  This is enforced: `tests/architecture/test_sdk_import_isolation.py:37-43` runs
  `tools/checks/import_audit.py` under `python -I` with an empty environment over
  `sandloom.sdk` and the bundled modules (G-SDK-01). A composition root is the exception:
  it builds the app at import by design (`:46-52`).
- **At registration (`setup`, step 6):** identity reads `jwt_secret`, `is_prod` and the TOTP
  keys (`sandloom_identity/__init__.py:28`, `:39`; `services/twofa_service.py:55-62`).
  Billing reads `billing_provider`, `is_prod`, `accepts_risk` and builds the PSP client from
  `stripe_*` (`sandloom_billing/__init__.py:41`, `:53`; `psp/__init__.py:50-77`). Admin and
  tenancy read nothing at registration.
- **At request, task or command time:** every other read. Per field, outside tests:
  `frontend_url` 12, `billing_deny_on_past_due` 4, `billing_portal_attribution_window_minutes` 3,
  the SSO ids and secrets 2-3 each, `tenant_claims_max_organizations` 2
  (`sandloom_tenancy/claims.py:25`, `contracts.py:133`), `admin_console_*` 1-2
  (`sandloom_admin/api.py:32-50`). Modules all import the kernel singleton:
  `from sandloom.sdk.infra import settings` (22 files).
- **Kernel readers of module settings** (baseline B-C1..B-C10, re-verified by grep): the
  validators at `core/config/settings.py:497-505` and `:623-659`; the risk registry
  `core/config/accepted_risks.py:56-62`; tiers at `core/config/tiers.py:332-338`, `:396-406`,
  `:620`, `:659-678`; the platform view `core/admin/api/platform.py:608`; `core/cli/admin.py:130-132`;
  the backup text `core/migrations/backup.py:252-259`; `SHARED_KEYS` and the dev env at
  `core/testing/deploy_profile.py:87`, `:928-942`; and the compose text of the scaffolds,
  `core/cli/scaffold_deploy.py:86-103` and `core/cli/project.py:970-982` (B-N6, B-N8). These
  name settings only in comments: `core/http/tenant_scope.py:138`,
  `shared/authz/atoms_claims.py:87`, `core/observability/guardrails.py:24`.

## 3. Findings

**F1. The module production refusals do not reach the worker, beat, migrate or the CLIs.**
[verified: probe 1] With `ENVIRONMENT=prod`, identity, tenancy and billing loaded, and
`TWOFA_ENCRYPTION_KEY` and `BILLING_PROVIDER` unset:

- `create_app()` builds and only logs the two problems at ERROR;
- `build_worker_app(core)` returns a Celery app;
- the critical tasks `_refuse_missing_twofa_key` and `_refuse_fake_psp_in_prod` are
  registered but run only in the lifespan
  ([`boot_parity_probe.out.txt`](_evidence/research-settings/boot_parity_probe.out.txt)).

So G-AUTH-19, G-RISK-07 and the prod part of G-BILLING-18 hold in the API process only.
I-18 ("boot refusals identical in the app, the worker, `sandloom migrate` and the CLI")
holds today only for kernel checks and the explicit-`fake` kernel validator. This gap exists
whatever decision D chooses, and it is the strongest argument for validating module
settings at registration rather than in startup tasks.

**F2. `sandloom config dump` never loads modules** (`cli/config.py:160-195`). Any design
that takes module fields out of the kernel `Settings` must make `config dump` build the
composition root, as `doctor` already does. Otherwise D-H1, D-H2 and D-H4 cannot hold. It
is also the CLI case of the G-CONFIG-07 parity test.

**F3. The module set is known only by importing the composition root, and that builds the
app** (`main.py:38`; a `sandloom new` root does the same, `cli/project.py:599-615`). So no
"discover without booting" path exists today for a CLI that wants the deployed module set.
Doctor, migrate and the operator CLIs all solve this by building the app.

**F4. Two kernel validators assume the full name and id lists before modules are known.**

- The accepted-risk unknown-id check runs inside `Settings` (`settings.py:191-206`), with the
  closed `RISKS` (`accepted_risks.py:50-145`).
- The prod typo check `_reject_production_setting_typos` (`settings.py:208-235`) compares the
  environment with kernel aliases only.

Both must move after the load pass, or their input must widen. The removed-settings scan
(`settings.py:167-189`) reads `os.environ` directly. For module-owned removed settings, the
same scan can run per module.

**F5. pydantic-settings does not detect one env name declared by two models.**
[verified: probe 2] Two `BaseSettings` classes with the field `api_key` both read
`API_KEY=shared` silently. D-F2 therefore needs a kernel check.

**F6. Cost: reading the sources dominates, not validating.** [verified: probe 2, median of 30,
Darwin arm64, Python 3.12]

| construction | `.env` (44 lines) + `SECRETS_DIR` | env only |
|---|---|---|
| kernel `Settings()` | 41.8 ms | 2.4 ms |
| module `BaseSettings`, the 22 `ModulesConfig` fields, own sources | 17.3 ms | 0.41 ms |
| module `BaseModel.model_validate` over one shared snapshot | ~0.001 ms | — |
| `entry_points().select("sandloom.modules")` | 3.6 ms | — |

For scale, `create_app` with three modules takes ~440 ms with settings already loaded
([`create_app_time_probe.out.txt`](_evidence/research-settings/create_app_time_probe.out.txt)).

Each `BaseSettings` instance re-reads `.env` and looks up one secrets file per field. If
every module re-reads its sources, a container that uses `SECRETS_DIR` pays ~17 ms per
module per process, and `.env` is parsed N+1 times. A single shared read costs nothing
measurable.

**F7. Registration runs outside `create_app` too.** The deploy tooling calls `register` and
`setup` on a bare facade (`load_module_topology`). Whatever validates at registration also
runs there, with the developer's `.env.test` (`__init__.py:253-258`). That is fine for ops
and type validation. Production checks must be no-ops outside prod, which the bundled ones
already are.

**F8. The semantic ratchet in the code is 15, not 16.** `SEMANTIC_COUNT_CEILING = 15` and
`SEMANTIC_BASELINE_COUNT = 13` (`core/tests/test_config_tiers.py:52-71`), and `tiers.SEMANTIC`
has 15 entries. requirements.md (D-B3) says "ratchet (16)". One of the two needs correcting.

## 4. Precedents

| system | how a plug-in declares settings | how it avoids eager import | where it validates | secrets | boot order lesson |
|---|---|---|---|---|---|
| Spring Boot `@ConfigurationProperties` ([external config](https://docs.spring.io/spring-boot/reference/features/external-config.html)) | a typed class bound to a prefix; relaxed binding (`PORT` → `port`); registered by `@EnableConfigurationProperties` or a scan | binding happens only for registered beans; auto-configurations are named in `META-INF/spring/…AutoConfiguration.imports`, and their conditions are read with ASM ("annotation metadata is parsed by using ASM"), so a class need not load ([auto-configuration](https://docs.spring.io/spring-boot/reference/features/developing-auto-configuration.html)) | `@Validated` classes are validated when bound, i.e. when the bean is created: only where the starter is active | the starter owns the namespace: "use a unique namespace" and do not use Spring's own | the type-safe class is the unit of ownership; validation is lazy per bean, yet before the app serves |
| Spring configuration metadata ([format](https://docs.spring.io/spring-boot/specification/configuration-metadata/format.html)) | `META-INF/spring-configuration-metadata.json`, generated at compile time from the classes; name, type, description, default, `deprecation{level, replacement, since}` | tooling reads the JSON, not the classes | not used at runtime for binding | — | static metadata is a **tooling** artifact (IDE, docs). `level: error` means "no longer bound", the analogue of `RemovedSetting`. A runtime "properties migrator" exists but is an opt-in dependency, removed after migration |
| Spring `FailureAnalyzer` ([how-to](https://docs.spring.io/spring-boot/how-to/application.html)) | analyzers registered in `spring.factories` | — | turns a bind or validation exception into "description + action" | — | one place renders every startup refusal with the fix; the analogue of `INVALID_CONFIGURATION` plus `sandloom explain` |
| Django system checks ([checks](https://docs.djangoproject.com/en/5.1/topics/checks/)) + app registry ([applications](https://docs.djangoproject.com/en/5.1/ref/applications/)) | an app registers check functions, "for example, in the `AppConfig.ready()` method"; `deploy=True` checks run only with `check --deploy` | `django.setup()` has three stages: import app configs (no models), import models, `ready()` | before most management commands (`runserver`, `migrate`), **but not in the WSGI stack**; `SILENCED_SYSTEM_CHECKS` | — | Sandloom's accepted risks are modelled on `SILENCED_SYSTEM_CHECKS` (ADR-0006). Django skips checks in WSGI for speed: Sandloom deliberately refuses at every entry point (G-CONFIG-07), a stricter rule worth keeping |
| ABP `SettingDefinitionProvider` ([settings](https://abp.io/docs/latest/framework/infrastructure/settings)) | each module defines `SettingDefinition`s (name, default, `IsVisibleToClients`, `IsEncrypted`, `IsInherited`) | "ABP automatically discovers this class" in the loaded module assemblies | value providers in a chain (Default → Configuration → Global → Tenant → User) | `IsEncrypted` | per-setting flags (visible, encrypted) are what Sandloom needs for the console view (B-C9) and redaction; ABP's runtime-mutable tenant/user values are out of scope here |
| pydantic-settings ([docs](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/)) | one `BaseSettings` with `BaseModel` sub-models; `env_nested_delimiter`; `settings_customise_sources` for custom sources | — | at instantiation | `secrets_dir`: "Note that a non existent directory will only generate a warning" | composition across separate classes is not a feature. Nested models mean `BILLING__PROVIDER`-style names, a rename. A custom source over one snapshot is the supported way to share sources |
| Airflow providers ([custom providers](https://airflow.apache.org/docs/apache-airflow-providers/howto/create-custom-providers.html), [PR #32765](https://github.com/apache/airflow/pull/32765)) | entry point `apache_airflow_provider` → `get_provider_info()` returns a dict with a `config` section (type, default, `sensitive`, `renamed.previous_name`) | discovery "does not involve any importing of provider classes" (PR #32765) | the provider configuration is loaded per CLI command by a decorator; `version` is exempt | `sensitive` options | a metadata-only discovery phase exists because Airflow's core config must initialise **without** providers. Each command then opts in, which gives parity by convention, not by construction |
| Home Assistant config flow ([config flow](https://contributors.home-assistant.io/core/integrations/config-flow/)) | `"config_flow": true` in the manifest; schemas in `config_flow.py` | the manifest is static JSON | at entry creation: "the flow validates everything before the entry exists"; `async_setup_entry` only uses it | entry data | validation belongs to the integration and happens before it is activated; config is user data in a store, not env, so the comparison is limited |

What carries over:

- **Ownership:** a typed class per plug-in, in its own namespace (Spring, ABP).
- **No eager import:** static metadata is a tooling optimisation (Spring metadata, Airflow
  `provider_info`). It is needed where importing is expensive or has side effects. Sandloom
  has already made module import side-effect free and audited (G-SDK-01), so importing the
  composed modules is the discovery.
- **Validate where loaded, before activation:** Spring binds at bean creation and Home
  Assistant validates before the entry exists. Both are lazy per plug-in, but before serving.
- **Secrets:** a per-field flag (ABP `IsEncrypted`, Airflow `sensitive`) instead of a
  name heuristic alone.
- **Boot order:** none of these systems validates every plug-in's settings in one pass
  before loading. Airflow and Django add per-command opt-ins and lose parity: Django skips
  WSGI, Airflow exempts `version`. Sandloom's existing rule, refusing at every entry point,
  is stricter than all of them.

## 5. Does Sandloom need a two-phase bootstrap?

For each part of discover → validate → activate, what exists:

| need | exists today | missing |
|---|---|---|
| know the module set before validating module settings | step 5-6: kernel settings → `apply_module_defaults` → `resolve_module_names` (B020) → import | — |
| read a module's declaration without side effects | import is audited side-effect free (G-SDK-01) | a place for the declaration that the loader can read **before** `register` calls `setup`: an attribute of the `register` callable that `define_module` builds (it already sets `register.__dict__["sandloom_l1"]`, `sdk/_decl/module.py:581`), or a `ModuleAPI` call at the top of a hand-written `register` |
| validate before the module reads its settings | — | validation inside the registration draft, before `setup` |
| validate everything that spans modules before any I/O | step 7 runs before any I/O in every entry point | one post-load function (collisions, risks, removed, prod checks, overrides) |
| refuse identically everywhere | kernel checks: yes (G-CONFIG-07). Module checks: **no** (F1) | refusals raised in steps 6-7, not in step 8 |
| tooling without a deployed environment (docs page, tier test, `sandloom check`) | `tools/docs/gen_config_reference.py:36`, `:218` iterate `Settings.model_fields` | iterate the declarations of the modules being documented or tested (imported directly; import is free of side effects) |

Every entry point except `config dump` already runs discover (step 6, as a buffered draft),
then validate (steps 2, 6-7), then activate (step 8, the worker loop, the migration run).
**The smaller change is enough:** validate each module's settings at its registration with
the kernel's sources, and add one post-load check. A metadata-only pre-load phase (Airflow,
Spring metadata) would solve a problem Sandloom does not have, eager import cost. It would
add a second discovery mechanism next to entry points and `SANDLOOM_MODULES` (H-12).

## 6. Options

Scenarios are from requirements.md §5 (D-H1..D-F4). "Mechanism" follows H-12: a new thing a
module author or operator must know.

### O1. Each module reads its own `BaseSettings`; the kernel only registers the class (no framework over pydantic)

A module writes `class BillingSettings(BaseSettings)` with the kernel's `model_config`
copied. It builds the class lazily in `setup` and hands it to `core.register_settings(cls,
instance)` for tiers and dump. Production checks raise in `setup`.

- Mechanisms: 1, a registration call.
- Startup: one source read per module. ~0.4 ms each env-only, ~17 ms each with `.env` and
  `SECRETS_DIR` (F6). `.env` is parsed N+1 times.
- Module-author DX: plain pydantic, nothing new to learn. But the module must copy the
  source configuration (`env_file`, `case_sensitive`, the `SECRETS_DIR` hook of
  `settings.py:61-68`, `:129-143`), and nothing makes it.
- Entry-point parity: holds for type errors and checks raised in `setup` (every entry point
  builds the app). Optional modules swallow registration errors (`plugin_loader.py:555-558`).
- Integration: dump, tiers and redaction see only what the module registered. Provenance
  (`cli/config.py:136-157`) and secrets resolution can differ from the kernel's when the
  copy drifts. Risks, removed settings, shared keys and the console view each need a further
  call.
- Fails D-F2 (no collision check) and D-F4 (the closed registry stays) unless more is added.
  Its weak point is that nothing enforces the kernel's guarantees over the module's settings.

### O2. Validated at registration from the kernel's sources; one post-load check (recommended)

The SDK provides a `ModuleSettings` base. It is a pydantic model whose only source is the
kernel's per-process snapshot of env > `.env` > `SECRETS_DIR` (a `PydanticBaseSettingsSource`
over the snapshot, so the JSON decoding of complex fields stays the same). A module passes
it to `define_module(settings=BillingSettings)`, or calls `api.settings(BillingSettings)`
in a hand-written `register`. The loader validates it inside the registration draft, before
`setup`. After the load pass, `create_app` runs one `module_configuration_problems(core)`.
The declaration carries per-field metadata (tier, secret, shared, console-visible, backup
note), module-namespaced `Risk`s, `RemovedSetting`s and `@prod_check` functions. Kernel
tools read that metadata for every loaded module.

- Mechanisms: 4, each against a confirmed violation:
  1. the declaration (B-C1, B-C2; I-13, I-18);
  2. the post-load check (F1, B-C3, B-C4; I-18 parity);
  3. module-namespaced accepted risks (B-C5, T10);
  4. declaration metadata read by tiers, dump, doctor, backup, the console view and the
     profile check (B-C6..B-C9).

  Compared against the existing ones: critical startup tasks (fail parity, F1); kernel
  `Settings` fields (fail I-13); module-local `BaseSettings` (O1: no shared sources, no
  collision check).
- Startup: the snapshot replaces today's read (no extra I/O). Validation per module is
  µs-scale over the snapshot (F6). The post-load check is O(fields). `config dump` gains
  one composition-root build (~0.9 s, a CLI only).
- DX: one class per module, normal pydantic fields and validators. Field names stay as
  today, so a reader changes from `settings.billing_provider` to
  `BillingSettings.current().billing_provider` (or `settings_of(BillingSettings)`). It is
  typed under mypy. Tests use a `sandloom.sdk.testing` override in place of `setattr` on
  the singleton.
- Parity: by construction, because every entry point that reaches step 7 refuses there.
  `config dump` is changed to build the root (F2).
- Fully passes D-H1..D-H4, D-B2..D-B5, D-F1..D-F3. D-B1 and D-F4 are owner questions (§9).

### O3. Static metadata and a pre-load discover phase (Spring metadata, Airflow `provider_info`)

Each module distribution publishes its settings in an entry-point group
(`sandloom.settings = pkg.settings:BillingSettings`) or as a generated JSON file. Before any
module loads, the kernel reads the metadata of every installed module and validates the
selected set in one pass. Only then does it load.

- Mechanisms: O2's four plus an entry-point group and a generated metadata artifact (its
  home is decision B), with drift checks.
- Startup: +3.6 ms entry-point scan. No import is saved: importing `pkg.settings` imports
  `pkg/__init__.py` first, so only a JSON artifact avoids the import.
- DX: two places to keep in sync. App-local modules (`LOCAL_MODULES`, `<app>/modules`) have
  no distribution and need a second path.
- Parity: same as O2. What it buys: `config dump` and docs without building the app;
  collision detection against installed but unloaded modules at every boot; a D-B1 warning.
  O2 can get the last two from `sandloom check` and `sandloom test` statically (§7.5).
- Rejected under H-12: the extra mechanism removes no violation that O2 leaves.

### O4. One composed `Settings`: kernel plus all module models, three-phase bootstrap

First read a bootstrap subset (`SANDLOOM_MODULES`, `MODULE_*`, `SANDLOOM_COMPOSITION_ROOT`).
Then import the modules and compose `Settings` dynamically (`create_model` over the kernel
and module mixins). Validate once, then load.

- Mechanisms: a bootstrap settings class, dynamic composition, and the singleton rebuilt
  after the module set is known (today a class swap, `settings.py:793-816`).
- Startup: one validation, but the module import moves before kernel validation, so
  `enforce_configuration` order changes in every entry point.
- DX: `settings.billing_provider` keeps working (no reader churn), but its type is invisible
  to mypy: the field is on a dynamic class. Two apps with different module sets in one
  process (tests, `load_topology` beside `create_app`) share one singleton and conflict.
- Parity: holds. Integration is easy, because existing kernel code keeps iterating one model.
- Rejected: it re-couples the kernel `Settings` type to whatever is installed and breaks
  per-app module sets.

### O5. Namespaced nested env (`BILLING__PROVIDER`, pydantic `env_nested_delimiter`)

A variant of O4 with nested sub-models instead of flat mixins. It renames all 22 variables
plus `TENANT_CLAIMS_MAX_ORGANIZATIONS`. With no aliases allowed (ADR-0010:75), every running
deployment must change its environment in one release. It solves nothing O2 does not, so it
is rejected. A prefix *convention* for new modules is still worth considering (§9, QS4).

### Summary

| | O1 | **O2** | O3 | O4 | O5 |
|---|---|---|---|---|---|
| mechanisms added | 1 | 4 | 6 | 3 + order change | 3 + rename |
| extra startup cost / process | 0.4-17 ms per module | ≈ 0 | +3.6 ms | ≈ 0 | ≈ 0 |
| reader churn in modules | per read | per read (same field names) | per read | none | per read |
| parity incl. worker/migrate/CLI | yes (re-raise needed) | yes | yes | yes | yes |
| D-F2 collisions | no | loaded set; static gate for installed | installed set | loaded set | n/a |
| tiers/doctor/dump/redaction/risks/backup/console/profile | partial, per call | yes, one declaration | yes | yes | yes |
| env names migrated | 0 | 0 | 0 | 0 | 23 |
| one source read per process | no | yes | yes | yes | yes |

## 7. Recommendation: O2

### 7.1 Declaration (sketch, not an API proposal for review yet)

```python
# sandloom_billing/settings.py: imports only sandloom.sdk; no env read at import
from typing import Literal
from pydantic import SecretStr
from sandloom.sdk.module import ModuleSettings, Risk, RemovedSetting, prod_check, setting

class BillingSettings(ModuleSettings):
    billing_provider: Literal["fake", "stripe"] = setting(
        "fake", env="BILLING_PROVIDER", console=True)
    stripe_secret_key: SecretStr | None = setting(
        None, env="STRIPE_SECRET_KEY", backup="secret manager, never beside the dumps")
    billing_deny_on_past_due: bool = setting(False, env="BILLING_DENY_ON_PAST_DUE", shared=True)
    billing_grant_intentless_subscriptions: bool = setting(
        False, env="BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS",
        tier="semantic", profile={"dev": False, "test": False, "prod": False, "prod_strict": False},
        strength=(True, False))
    # … the other billing fields, unchanged names and bounds (core/config/modules.py:45-130)

    risks = (Risk("fake_psp_in_prod", weakens="…fake checkout sessions, no real payment"),)
    removed = (RemovedSetting("BILLING_ALLOW_FAKE_PSP_IN_PROD", "0.8", hint="…"),
               RemovedSetting("BILLING_FAKE_ALLOW_UNSIGNED_WEBHOOKS", "0.8", hint="…"))

    @prod_check
    def _psp(self, platform) -> str | None:      # platform: the kernel Settings (read-only)
        if self.billing_provider == "fake" and not platform.accepts_risk("billing.fake_psp_in_prod"):
            return "BILLING_PROVIDER=fake is not allowed with ENVIRONMENT=prod: …"
        return None

# sandloom_billing/__init__.py
register = define_module(__name__, settings=BillingSettings, setup=_setup, …)
```

- Undeclared tier: `ops`, as today for 21 of the 22 settings.
- Secrets: the field type (`SecretStr`) or `secret=True`. The name heuristic of
  `cli/config.py:39-73` stays as a backstop.
- The declaration lives in the module's own package, so only the module ships it.
- `@prod_check` functions return problems instead of raising. Doctor can then list them
  without refusing, the way `configuration_problems` works today (`app_factory/config.py:316`).

### 7.2 Boot order after the change

1. Kernel settings load from the **source snapshot** (env, `.env` parsed once, `SECRETS_DIR`
   looked up by declared alias). Kernel validators run as today, minus B-C3, B-C4, the
   module entries of REMOVED and the accepted-risk unknown-id check.
2. `enforce_configuration`: unchanged, kernel only.
3. Load pass. Per module, inside its draft: import → read `register`'s declared model →
   validate it from the snapshot (type, bounds, field validators) → store the instance on
   the facade → `setup` (which may read it). A `ModuleSettingsError` fails the load and is
   **re-raised even for an optional module** (QS2).
4. **Post-load check**, right after `load_modules_with_summary`
   (`app_factory/__init__.py:192`):
   - one env name declared twice, across the kernel and the loaded modules (D-F2, with a new
     B-code);
   - accepted-risk ids against kernel ids plus loaded modules' ids (D-F4 rule, §9);
   - each loaded module's `removed` against the snapshot;
   - each loaded module's `@prod_check` in prod;
   - the prod typo check over the union of names;
   - override logging and metrics for module semantic settings.

   Any problem raises `ConfigurationError(INVALID_CONFIGURATION + …)`. That is the same exit
   and message shape as G-CONFIG-07, so all entry points of §1.2 that build the app refuse.
5. Activation, unchanged: the lifespan, the worker loop, the migration run. The identity and
   billing critical startup tasks that only re-check settings are removed. Identity's
   `JWT_SECRET` requirement becomes a `@prod_check`-style check that is active in every
   environment.

`sandloom config dump` builds the composition root (`load_composition_core`) and lists the
kernel fields plus the loaded modules' fields. `_enforce_configuration()` in the CLI stays
kernel-only for commands that never build the app.

### 7.3 Keeping `.env`, secrets and one validation per process

- **One read:** `_load_into` (`settings.py:765-782`) first builds a frozen, case-insensitive
  snapshot from the same pydantic-settings sources and precedence
  (`settings_customise_sources`, `settings.py:129-143`). Kernel `Settings` and every
  `ModuleSettings` read through a source over that snapshot. `config dump` provenance
  (`cli/config.py:136-157`) reads the snapshot's per-key origin instead of re-running the
  sources. That removes the second read it does today.
- **`SECRETS_DIR`:** the snapshot looks up a secrets file only for a declared alias (kernel
  plus loaded modules), as pydantic does per field today. It never lists or reads the whole
  directory.
- **One validation per process:** kernel `Settings` once, as today. Each module model once
  per app build: the instance is cached on the facade, and the snapshot is shared. Tests
  that change the kernel singleton between app builds get a fresh validation per app, which
  is µs-cheap. Reads go through the active core, like the other runtime accessors
  (`core/di.py:134-145`). A read with no app bound raises, so an import-time read fails loudly
  and the import audit catches it too.
- **No env read at import:** validation happens in step 3, never at import. The import audit
  gains the fixture module (§8, P5).

### 7.4 Where each kernel item goes

| baseline item | today | after |
|---|---|---|
| B-C1 `ModulesConfig` (22) | kernel mixin `config/modules.py`, `settings.py:46`, `:100` | `IdentitySettings` (TOTP key, SSO ×9, `SSO_ALLOWED_REDIRECT_URIS`), `BillingSettings` (10), `AdminSettings` (2); names unchanged |
| B-C2 `TENANT_CLAIMS_MAX_ORGANIZATIONS` | `config/auth.py:88-95` | `TenancySettings` |
| B-C3 TOTP key strength | `settings.py:497-505` | identity `@prod_check` (strength + presence, now in every entry point) |
| B-C4 `_billing_requirements` | `settings.py:623-660` | billing: the Stripe-key requirement as a model validator (any env), the fake-PSP refusal as a `@prod_check`. One message replaces today's two (`settings.py:641-645` vs `psp/__init__.py:61-66`) |
| B-C5 `fake_psp_in_prod` | closed `RISKS` | billing's `risks`; kernel registry = kernel tuple ∪ loaded modules' |
| B-C6 tiers | `tiers.py:332-338`, `:396-406`, `:620`, `:659-678` | field metadata and module `removed`. The kernel ratchet loses one name (13 → 12 baseline, ceiling 15 → 14). Billing's own test pins its semantic setting, so the standard-install total stays 15 (D-B3) |
| B-C7 `SHARED_KEYS`, dev env | `deploy_profile.py:87`, `:928-942` | the T014 check unions the `shared=True` fields of the modules each service loads (it already loads their topology, `:356-358`). The dev env generator asks each module for dev defaults or required-in-prod fields |
| B-C8 backup text | `backup.py:252-259` | the plan lists each loaded module's `backup=` notes on secret fields |
| B-C9 console `billing_provider` | `platform.py:608` | the platform view lists `console=True` fields of loaded modules, under the same key (D-B5) |
| B-C10 `sandloom admin` link target | `cli/admin.py:130-160` | **not a settings question.** The kernel CLI needs to know whether a console is served; reading another module's setting by name would still name the module. Route to decision A, e.g. a link-target provider the admin module registers |
| B-N6, B-N8 scaffold env text | `cli/project.py:970-982`, `cli/scaffold_deploy.py:86-103` | generated from the declarations of the scaffolded modules (`${VAR:?…}` for required-in-prod fields) |
| docs reference page | `tools/docs/gen_config_reference.py:218` | kernel table plus one section per documented module, from its declaration |
| tier test | `test_config_tiers.py:92-107` over `Settings` | kernel test over the kernel; a `sandloom.core.testing` helper each module's tests call over its declaration, and `sandloom test` runs it for every module in the app |

### 7.5 Env-name migration

- **No variable changes name.** Ownership moves; the 23 names stay byte-identical. Running
  deployments change nothing for settings.
- **Accepted-risk id.** If risk ids are namespaced by module name (QS1, recommended), the
  id `fake_psp_in_prod` becomes `billing.fake_psp_in_prod`. With no aliases (ADR-0010:75),
  the old id becomes an unknown id that refuses startup with the replacement in the message
  (billing declares it as a renamed risk). This is the only operator-visible break: one
  value in `SANDLOOM_ACCEPTED_RISKS`, with upgrade steps and a release note as for PR #70.
  Without namespacing nothing changes, but D-F4 cannot tell an absent module's id from a
  typo.
- **Removed settings move with their owner.** `BILLING_ALLOW_FAKE_PSP_IN_PROD` and
  `BILLING_FAKE_ALLOW_UNSIGNED_WEBHOOKS` refuse with today's message where billing is loaded
  (D-B4). They are inert elsewhere, as every other billing variable will be.
- **Collisions with modules not loaded** in a process are caught statically: `sandloom check`
  and `sandloom test` import every module of the app and compare declared names. Import has
  no side effects, so the cost is a few ms.

### 7.6 Trade-offs accepted with O2

- **Module checks now refuse migrations and workers.** A prod `sandloom migrate` one-shot
  without `STRIPE_*` or the TOTP key refuses where it ran before. This is what I-18 asks for,
  but compose files must give the migrate service the module env
  (`deploy/compose/monolith.yml:116`, `microservices.yml:272-301`): verify in the PoC.
- `config dump` becomes ~1 s slower and needs the modules importable.
- Module readers change from the singleton to a typed accessor: 22 files, about 25 test
  files that set these fields.
- A module setting set in a process that does not load the module is silent unless Q6 asks
  for a warning. That warning needs the installed modules' declarations, which means
  importing modules that are not loaded.
- Four new SDK names (`ModuleSettings`, `setting`, `prod_check`, a testing override), all in
  the advanced tier: an API decision under I-17. The beginner tier is untouched.

## 8. PoC plan

Two steps. First, a mechanism commit to the kernel. Second, a fixture module **outside
`src/`** (`tests/fixtures/kb_inventory/`, entry point `inventory`) that declares:

- `INVENTORY_API_KEY` (secret);
- `INVENTORY_SYNC_INTERVAL_SECONDS` (ops, `ge=5`);
- `INVENTORY_ALLOW_OVERSELL` (semantic, profile `False`);
- risk `inventory.oversell_in_prod`;
- a `@prod_check` refusing oversell in prod unless that risk is accepted;
- a removed setting `INVENTORY_LEGACY_MODE`.

Then billing's settings are ported as the second subject. Run with
`SANDLOOM_TEST_OWNER=kb-settings`, own Postgres and Redis containers (not db 0), and remove
them afterwards.

| # | criterion | pass | fail |
|---|---|---|---|
| P1 | I-13 | adding the fixture module changes no file under `src/sandloom` (`git diff --stat -- src/sandloom` empty after the mechanism commit) | any kernel edit for the fixture |
| P2 | parity (D-H1, D-H3, D-F1) | the G-CONFIG-07 harness (`core/tests/test_boot_configuration_checks.py:73-105`) with new cases (invalid `INVENTORY_SYNC_INTERVAL_SECONDS=1`; oversell in prod; `BILLING_PROVIDER` unset in prod with billing; weak TOTP key with identity; an env name declared twice): every entry point (app import, worker `get_worker_app`, beat, `migrate main(['databases'])`, `sandloom config dump`) exits 1, the last stderr line starts with `INVALID_CONFIGURATION` and names the setting; `sandloom doctor` reports the same reason as a FAIL line | any entry point starts, or the messages differ |
| P3 | accepted risk (T10) | with `inventory.oversell_in_prod` accepted, all entry points start; the WARNING is logged, `sandloom_accepted_risk_active{risk}` is set, and `/diagnostics/core` and doctor list it | refusal, or a silent start |
| P4 | absent module (D-B1, D-H4, D-F4) | same env, `SANDLOOM_MODULES` without `inventory`: all entry points start; `config dump` lists no `INVENTORY_*`; the invalid value is neither validated nor shown; the risk id behaves as the owner's Q8 answer, identically everywhere | a refusal, a listed value, or entry points that disagree |
| P5 | no env read at import (D-F3) | `tools/checks/import_audit.py` under `python -I`, empty env, with `kb_inventory` and the ported billing: no violation; a module-level `BillingSettings.current()` fails the audit | any `environ`/`Settings` spy hit |
| P6 | redaction (D-H2) | `config dump` shows `INVENTORY_API_KEY` and `STRIPE_SECRET_KEY` as `<redacted>`; `--show-secrets` is refused in prod; a log record carrying the key is scrubbed | a value in clear |
| P7 | sources, one read | the value comes from the process env, from `.env`, and from a `SECRETS_DIR` file in turn, with precedence env > dotenv > secrets; a spy counts one `.env` parse and one secrets lookup per declared alias per process; module validation runs once per app build | a second `.env` parse, or a value from the wrong source |
| P8 | tiers (D-B3, D-B4) | the tier helper classifies every fixture field; the kernel ratchet passes with one fewer name; the standard-install semantic total is unchanged; `INVENTORY_ALLOW_OVERSELL=true` in dev is logged at INFO as an override; `INVENTORY_LEGACY_MODE=1` refuses with the removed-setting message | an unclassified field passes, or the counts drift |
| P9 | split (D-B2, D-F2 static) | a microservices profile where two services load `inventory` with different `INVENTORY_*` shared fields fails T014; `sandloom check` flags a name declared by two installed modules | either passes |
| P10 | bundled port | with billing ported: G-BILLING-18, G-RISK-07 and G-AUTH-19 tests pass unchanged except the added worker, migrate and CLI expectations; the console view shows `billing_provider` in mono and omits it without billing (D-B5) | a ledger test weakened |
| P11 | cost | `create_app` with the bundled modules plus the fixture, 10 alternating base/candidate runs: median difference within base-against-base noise; `config dump` wall time reported | a difference beyond noise without an owner's approval |

## 9. Open questions for the owner

Existing questions this research informs:

- **Q6 (D-B1):** a module's variable set where the module is not loaded. A warning needs
  the installed modules' declarations. O2 can do it only by importing modules that are not
  loaded; O3 can do it from metadata. Recommendation: stay silent at runtime and let the
  static gate check.
- **Q7 (D-B3):** do a third-party module's semantic settings count toward the framework's
  ratchet? O2 lets each module keep its own ratchet; the kernel pins only its own.
- **Q8 (D-F4):** an accepted-risk id whose module is not loaded. In a split deployment the
  shared env anchor carries every service's risks, so refusing would break every service
  that does not load billing. Recommendation: with namespaced ids, an id of an unloaded
  module is inert and listed as "not applicable here"; an id inside a loaded module's
  namespace that the module does not declare is refused.

New questions:

- **QS1:** namespace accepted-risk ids by module name (`billing.fake_psp_in_prod`), which
  costs a one-time operator change with no alias, or keep flat ids and give up typo
  detection for module risks?
- **QS2:** a settings error in a module declared `optional=True`: fatal (recommended:
  configuration is explicit operator intent; I-20), or skip the module as for other load
  failures?
- **QS3:** confirm the behaviour change of F1. Module production refusals then stop the
  worker, beat, `sandloom migrate` and the operator CLIs, not only the API. Should this
  apply even before the settings move, as a one-line change per module (raise in `setup`
  instead of registering a critical startup task)?
- **QS4:** env names: keep all 23 (recommended). Require new modules to prefix with their
  module name (scaffolded by `sandloom add module`, checked by `sandloom check`), or only
  forbid collisions?
- **QS5:** B-C10, the admin console link target: confirm it belongs to decision A (a
  provider the admin module registers), not to settings.
- **QS6:** `sandloom config dump` builds the composition root, so it gets slower and needs
  the modules importable. Is that acceptable, or should a `--kernel-only` mode remain?
- **QS7:** requirements.md D-B3 says the ratchet is 16; the code says 15 (F8). Which is
  meant?

## 10. Not covered, unverified

- That a lifespan without infrastructure would refuse on the module tasks after the infra
  phase. Probe 1's lifespan stopped at `infra_warm_instances` first, with no database. The
  module refusal in the lifespan is covered by the existing billing tests
  (`sandloom_billing/tests/test_webhook_hardening.py`, G-RISK-07).
- Beat: assumed equal to the worker, since it uses the same `sandloom.core.worker` app
  (`deploy/compose/monolith.yml:225`). Not run separately.
- The password-hash split (platform mechanism vs identity policy) is `kb-alt-settings`
  scope and not analysed here. `PASSWORD_HASH_*` stay kernel settings in every option above.
- The precedents table rests on the pages cited. The Spring `@Validated` timing (validated
  at binding, i.e. bean creation) is stated in the page's validation section but was only
  partly read.
