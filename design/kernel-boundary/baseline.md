# Kernel boundary baseline: what the kernel knows about business modules

Commit: `b8d8506` (branch `arch/kernel-boundary`). Every `file:line` below is at that commit.
Task: tm `kb-baseline` (parent `kb-assess`). This file describes; it proposes nothing.

## 1. Scope, terms and method

**Kernel**: `sandloom.core`, `sandloom.sdk`, `sandloom.shared` and `sandloom.main`
(`src/sandloom/`). `sandloom.driverkit` is outside the question and has no module edge.
Kernel tests (`src/sandloom/core/tests/`) are listed separately: the build drops every
`tests` directory from the wheels (`tools/packaging/package_build.py:45`).

**Business modules**: `sandloom_identity`, `sandloom_tenancy`, `sandloom_billing`,
`sandloom_admin` (`src/sandloom_<m>/`, one wheel each under `packages/`).

**Edge**: a place where kernel code, data or configuration depends on a business module
existing, on its name, or on something the module owns (its contract, claim, setting,
URL, table, event or code).

**Columns**

- *Type*: `import` (static or dynamic Python import), `package` (what a wheel ships or
  depends on), `runtime call` (a contract call or an HTTP/route behaviour at run time),
  `configuration` (a setting, a default, a startup validation), `security authority` (an
  access decision that trusts a specific module), `naming` (a module's name, contract
  name, claim key, path or file name written into kernel code or text).
- *Owner*: the business module that owns the target (the contract, claim, setting, code).
- *Status*: `verified` (read at the cited line, and run where stated), `inferred`
  (follows from verified lines, not observed directly), `unknown` / **Unverified**.

**Classes**

- **(a)** the kernel knows that modules exist (protocol: loader, entry points,
  `CoreFacade`, claims contributors, fact contracts). Generic; names no module.
- **(b)** the kernel names a specific module or something a specific module owns.
- **(c)** the kernel ships module-owned code or artifacts inside its own package.

**Method.** Searches over `src/sandloom` (excluding `__pycache__`, `.claude/worktrees/`):

```bash
grep -rnE "sandloom_(identity|tenancy|billing|admin)" src/sandloom --include='*.py'
grep -rnE "[\"'](identity|tenancy|billing)[\"'.]"   src/sandloom --include='*.py'
grep -rnE "import_module|find_spec|entry_points|__import__" src/sandloom --include='*.py'
grep -rnE "^\s*(from|import) sandloom_(identity|tenancy|billing|admin)" src/sandloom
# domain words: stripe psp twofa sso invitation plan_code subscription_status credit organization_id
```

Then every inventory row (`_inputs/inventory.md`, written at `main cc90200`) was re-read
at `b8d8506`, and every row below was opened at its line. Commands run are listed in each
section.

## 2. Summary counts

Counted rows of sections 3-5 (one row = one edge; a row that cites several lines of
the same mechanism counts once).

| class | import | package | runtime call | configuration | security authority | naming | total |
|---|---|---|---|---|---|---|---|
| (a) protocol | 9 | 3 | 4 | 2 | 1 | 0 | **19** |
| (b) names a module | 1 | 0 | 6 | 10 | 3 | 20 | **40** |
| (c) ships module-owned code/artifacts | 1 | 4 | 0 | 0 | 0 | 0 | **5** |

Not counted as edges: docstring and help-text mentions (section 6), kernel APIs whose
only consumer is one module but whose code names no module (section 5.4), and kernel
tests (section 6.2).

Static imports of a business module from shipped kernel code: **0**
(`grep -rnE "^\s*(from|import) sandloom_…" src/sandloom` matches only two test files).
Dynamic imports that name a business module: **1** (B-I1).

## 3. Class (a): the kernel knows modules exist (protocol)

| id | type | source → target | evidence | status |
|---|---|---|---|---|
| A1 | package | `module_names` → every distribution's `sandloom.modules` entry points; module name = entry-point name, else last import segment | `core/descriptors/module_names.py:30`, `:59-60`, `:67`, `:95-104` | verified |
| A2 | import | `plugin_loader` → `importlib.import_module(name)` of each configured or discovered module, then its `register` | `core/loader/plugin_loader.py:525-526`, `:571-575`, `:710-719` | verified |
| A3 | configuration | `sandloom.main.default_modules()` → sorted installed entry-point names + `LOCAL_MODULES` (empty) | `main.py:24`, `:27-35` | verified |
| A4 | configuration | `create_app(default_modules=…)`: defaults apply only when `SANDLOOM_MODULES` is unset; kernel default is empty | `core/app_factory/__init__.py:154-159`; `Settings.sandloom_modules` default `[]` asserted in `core/tests/test_module_names.py:190-192` | verified |
| A5 | runtime call | `CoreFacade.add_claims_contributor` / `collect_claims` → each module's contributor, remote as `<owner>.claims.v1`; owner stamped into `claims_sources` | `core/claims.py:1-18`; `core/facade/_services.py:27-33`; `core/facade/_claims.py:210-232`, `:449-453` | verified |
| A6 | runtime call | access facts → `<owner>.facts.v1`, owner = first dotted segment of the fact name | `core/facade/_services.py:36-42`; `shared/authz/facts.py:534` (`fact_owner_contract`) | verified |
| A7 | import | admin compiler → `importlib.import_module(f"{descriptor.name}.admin")`, reads `RESOURCES` of every loaded module | `core/admin/compiler.py:455-470` | verified |
| A8 | runtime call | platform operations API: one router per loaded module at `/api/v1/platform/<name>` (audit, services, outbox, streams, runtime) | `core/admin/api/platform.py:262-280`; mounted `core/app_factory/__init__.py:251` | verified |
| A9 | package | contract compatibility: candidate package for contract owner = installed entry point of that name, then siblings of loaded modules | `core/contract_compat.py:139-153` | verified |
| A10 | import | worker → `<pkg>.tasks` of each loaded module | `core/worker.py:126-137` | verified |
| A11 | import | beat → `find_spec(module)`, AST read of `tasks.py` for `setup_periodic_tasks` | `core/tasks/periodic.py:15-28` | verified |
| A12 | import | migrations → `importlib.import_module` of each chain's model modules | `core/migrations/alembic_support.py:107`, `:117-118` | verified |
| A13 | import | `sandloom generate` → module and its `register` | `core/cli/generated.py:383`, `:390` | verified |
| A14 | import | `sandloom migrate extract-module` → `{pkg}.models` | `core/cli/extract.py:157` | verified |
| A15 | import | test harness → `{module}.models` | `core/testing/migrations.py:274-277` | verified |
| A16 | import | `define_module` → imports the module's own submodules | `sdk/_decl/module.py:95-100`, `:118` | verified |
| A17 | package | inspection and SDK owner lookup read `sandloom.modules` entry points | `core/cli/inspection/collect.py:242`; `sdk/_decl/_owner.py:86` | verified |
| A18 | security authority | token issuer is a declaration: `public(issues_tokens=True)` / `define_module(issues_tokens=True)` | `core/route_policy.py:160`; `core/module_api/_define.py:128-130` | verified |
| A19 | runtime call | live-adapter registry: `register_live_adapter(kind, adapter)` with kinds `entitlements`, `membership` (the mechanism; its defaults are B-R1) | `shared/authz/live_contracts.py:38-39`, `:125-140` | verified |

## 4. Class (b): the kernel names a specific module

### 4.1 Imports

| id | type | source → target | owner | evidence | status |
|---|---|---|---|---|---|
| B-I1 | import (dynamic) | `sandloom admin create` prompt → `importlib.import_module("sandloom_identity.password_policy")` inside `try/except ImportError`, fallback 8/100 | identity | `core/cli/password_check.py:21`, `:40-44`; used `core/cli/admin.py:49`, `:255` | verified |

### 4.2 Runtime calls to a named contract

| id | type | source → target | owner | evidence | status |
|---|---|---|---|---|---|
| B-R1 | runtime call | default live adapters registered at import: `billing.entitlements.v1` (`check_subscription_active`, `get_credit_balance`), `tenancy.membership.v1` (`has_role_at_least`) | billing, tenancy | `shared/authz/live_contracts.py:74-104`, `:107-111` | verified |
| B-R2 | runtime call | `tenant_kind` classifies a tenant as organization by asking the membership adapter (tenancy by default) | tenancy | `shared/authz/account.py:38`, `:154`, `:170-196` | verified |
| B-R3 | runtime call | `WELL_KNOWN_FACT_OWNERS`: facts `subscription.active`, `organization.subscription.active` → owner `billing` (so `billing.facts.v1`) | billing | `shared/authz/facts.py:526-531` | verified |
| B-R4 | runtime call | `sandloom admin create/list/…` → `identity.security.v1` (`password_problem`, `account_standing`, `create_platform_admin`, `take_over_platform_admin`, `set_password_token`, `list_platform_admins`) | identity | `core/cli/admin.py:61-62`, `:172-195`, `:251`, `:318-331`, `:362-383`, `:461` | verified |
| B-R5 | runtime call | `sandloom security revoke-sessions / force-password-reset [--tenant]` → `identity.security.v1` (`list_user_ids`, `account_standing`, `appoint_platform_admin`, batch method by name), `tenancy.membership.v1` (`list_member_user_ids`) | identity, tenancy | `core/cli/security.py:60-63`, `:134-144`, `:182-193`, `:367-389` | verified |
| B-R6 | runtime call | `fake_access_fact` (public, beginner in `sdk.testing`) knows the two subscription facts and stubs `billing.entitlements.v1` when billing is not loaded | billing | `core/testing/authz.py:21-31`, `:46-50`, `:120-128`; `sdk/testing/__init__.py:209-212` | verified |

### 4.3 Security authority

| id | type | source → target | owner | evidence | status |
|---|---|---|---|---|---|
| B-S1 | security authority | `require_platform_admin` (public in `sdk.web`) trusts role `admin` only when `"identity"` is in `claims_sources` and not in `claims_sources_failed` | identity | `shared/guards/platform.py:10-19`; export `sdk/web.py:55-56`, `:87`; used by `sandloom_identity/api/user_safety.py:21`, `sandloom_tenancy/api/platform.py:7`, `sandloom_billing/api/platform.py:12` | verified |
| B-S2 | security authority | `platform_operator` gate of every `/api/v1/platform/<name>` router: same `"identity"` source test on the bearer claims | identity | `core/admin/api/platform.py:54-76`, `:272` | verified |
| B-S3 | security authority | tenant allowlist from claim key `tenant_ids`, which `sandloom_tenancy` contributes; read by `TenantScope`, by `_resolve`, written by the test token helper | tenancy | `core/http/tenant_scope.py:5-6`, `:172`; `shared/authz/_resolve.py:68`; `core/testing/auth.py:49`; producer `sandloom_tenancy/claims.py:34` | verified |

Facts behind B-S1/B-S2 (verified): the source name stamped into `claims_sources` is the
contributor's `owner`, which a module may pass explicitly, else the short name of the
registering module (`core/facade/_claims.py:219-228`); the short name is the entry-point
name, else the last import segment (`core/descriptors/module_names.py:95-104`). The
`roles` claim is contributed by `sandloom_identity/claims.py:65-68`, registered
`critical=True` at `sandloom_identity/__init__.py:57`.

### 4.4 Configuration

| id | type | source → target | owner | evidence | status |
|---|---|---|---|---|---|
| B-C1 | configuration | `ModulesConfig` ("Settings of the bundled business modules") mixed into `Settings`: `TWOFA_ENCRYPTION_KEY`, `ADMIN_CONSOLE_ENABLED`, `ADMIN_CONSOLE_DIST`, `BILLING_PROVIDER`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `BILLING_DENY_ON_PAST_DUE`, `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS`, `BILLING_INTENT_RECONCILE_AFTER_MINUTES`, `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS`, `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES`, `GOOGLE/GITHUB/MICROSOFT/LINKEDIN_CLIENT_ID/SECRET`, `TELEGRAM_BOT_TOKEN`, `SSO_ALLOWED_REDIRECT_URIS` (22 settings) | identity, billing, admin | `core/config/modules.py:1-190` (aliases at `:19`, `:30`, `:38`, `:48`, `:56`, `:61`, `:69`, `:82`, `:97`, `:109`, `:124`, `:135-170`, `:175`, `:184`); `core/config/settings.py:46`, `:100` | verified |
| B-C2 | configuration | `TENANT_CLAIMS_MAX_ORGANIZATIONS` in the kernel auth config; read only by tenancy | tenancy | `core/config/auth.py:88-95`; tier `core/config/tiers.py:620`; readers `sandloom_tenancy/claims.py`, `sandloom_tenancy/contracts.py` | verified |
| B-C3 | configuration | startup validation of `TWOFA_ENCRYPTION_KEY` key strength | identity | `core/config/settings.py:497-505` | verified |
| B-C4 | configuration | `_billing_requirements`: refuses explicit `BILLING_PROVIDER=fake` in prod; requires Stripe keys for `stripe` | billing | `core/config/settings.py:623-660` | verified |
| B-C5 | configuration | accepted risk `fake_psp_in_prod`, citing `sandloom_billing.psp.psp_configuration_error` | billing | `core/config/accepted_risks.py:56-62` | verified |
| B-C6 | configuration | tiers: `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` is a **semantic** setting (counts toward the ratchet of 16); the other module settings are ops; two removed `BILLING_*` settings, one citing `sandloom_billing.psp.sign_stripe_payload` | billing, identity, admin | `core/config/tiers.py:332-338`, `:396-406`, `:659-678` | verified |
| B-C7 | configuration | deploy-profile check (SANDLOOM-T014): `BILLING_DENY_ON_PAST_DUE` in `SHARED_KEYS`; dev env generator writes `TWOFA_ENCRYPTION_KEY`, `BILLING_PROVIDER=fake` and the `fake_psp_in_prod` risk | billing, identity | `core/testing/deploy_profile.py:66`, `:87`, `:928`, `:935-942` | verified |
| B-C8 | configuration | backup plan text lists `TWOFA_ENCRYPTION_KEY`, `STRIPE_*`, "operator reset-2fa" | identity, billing | `core/migrations/backup.py:252-259` | verified |
| B-C9 | configuration | platform console settings view includes `billing_provider` | billing | `core/admin/api/platform.py:608` | verified |
| B-C10 | configuration | `sandloom admin` reads `settings.admin_console_enabled` to choose the set-password link target | admin | `core/cli/admin.py:130-132`, `:145-160` | verified |

Readers of B-C1 settings outside the kernel (verified by grep of field names):
`twofa_encryption_key` → `sandloom_identity/services/twofa_service.py`; `admin_console_*`
→ `sandloom_admin/api.py`; `billing_*`, `stripe_*` → `sandloom_billing/{psp,access,services,api}`;
SSO and Telegram fields → `sandloom_identity/{integrations,services/sso_service}.py`.
Kernel readers: `settings.py` (B-C3, B-C4), `admin/api/platform.py` (B-C9),
`cli/admin.py` (B-C10). Settings classes in the kernel: one `BaseSettings`
(`core/config/settings.py:113`); a search for `BaseSettings|module_settings|settings_model`
in `core`, `sdk`, `shared` finds no other.

### 4.5 Naming (code, data and generated text that carry a module's name or owned identifier)

| id | type | source → target | owner | evidence | status |
|---|---|---|---|---|---|
| B-N1 | naming | `sandloom generate` writes the admin manifest only when `"sandloom_admin"` is composed | admin | `core/cli/generated.py:547-552` | verified |
| B-N2 | naming | manifest file name `admin-manifest.json` defined in the kernel and again in the admin module | admin | `core/cli/generated.py:70`; `sandloom_admin/api.py:16` | verified |
| B-N3 | naming | set-password link `{base}/admin/set-password#token=…`: a page of the admin console | admin | `core/cli/admin.py:154`; page `admin/src/features/auth/set-password-page.tsx` | verified |
| B-N4 | naming | start page probes for a route under `/admin` and links "Console" and `sandloom admin create` | admin | `core/app_factory/pages.py:134-139`, `:287-302` | verified |
| B-N5 | naming | `sandloom new`: `_BUNDLED = ("identity","tenancy","billing","admin")`; admin implies identity; distribution name map; install check `find_spec(f"sandloom_{name}")` | all four | `core/cli/project.py:54`, `:1134-1142`, `:1225`, `:1410-1419` | verified |
| B-N6 | naming | `sandloom new` text blocks keyed on bundled modules: `_IDENTITY_NOTE`, `_LOG_LINKS`, `_IDENTITY_ENV`, `_BILLING_ENV` | identity, billing | `core/cli/project.py:120`, `:129`, `:970`, `:977`, `:1319-1339` | verified |
| B-N7 | naming | first-run wizard `MODULES` with descriptions; identity forced when admin chosen | all four | `core/cli/wizard.py:55-62`, `:403-415`, `:441-446` | verified |
| B-N8 | naming | `sandloom add deploy`: `MODULE_ENV` keyed `sandloom_identity` / `sandloom_billing`; help text names bundled modules | identity, billing | `core/cli/scaffold_deploy.py:86-103`, `:860-865` | verified |
| B-N9 | naming | deploy templates: `init-databases.sh` database list; compose comment "loading identity" holds `JWT_PRIVATE_KEY`; `sandloom keys` prints "issuer ONLY (identity)" | identity, tenancy, billing | `core/cli/templates/deploy/postgres/init-databases.sh:7`, `:20`; `core/cli/templates/deploy/compose/microservices.yml.tmpl:28`; `core/cli/keys.py:237` | verified |
| B-N10 | naming | app skill template `sandloom-split-deploy` lists `add deploy identity/tenancy/billing` | identity, tenancy, billing | `core/cli/templates/skills/sandloom-split-deploy/SKILL.md:12-19` | verified |
| B-N11 | naming | recipes (`sandloom docs recipe …`, 9 recipes) import bundled stubs and DTOs (`TENANCY_MEMBERSHIP_V1`, `BILLING_CREDITS_V1`, `BILLING_ENTITLEMENTS_V1`, `MemberState`) and declare `tenancy.member_*.v2` views; 29 lines match `identity|tenancy|billing` | tenancy, billing | `sdk/_prim/recipes.py:18`, `:44`, `:326`, `:383`, `:485`, `:493-509`, `:571`, `:604-606` | verified |
| B-N12 | naming | public authz atoms reading billing's claim and contracts: `Subscribed` (claim `subscription_status`, `ACTIVE_SUBSCRIPTION_STATUSES = {"active","trialing"}`), `Feature` (claim `features`), `WithinLimit` (claim `limits`), `SubscriptionActive`, `Credits` (via B-R1); `Account.user()/org()` "the billing account"; `TenantKind` | billing | `shared/authz/atoms_claims.py:32`, `:77-118`, `:304-310`, `:327-345`; `shared/authz/atoms_live.py:121`, `:143`; `shared/authz/account.py:1-20`, `:41`, `:54-90` | verified |
| B-N13 | naming | `OrgRole` with roles `member/admin/owner` hard-coded (tenancy's role set), via B-R1 | tenancy | `shared/authz/atoms_live.py:168-197` | verified |
| B-N14 | naming | beginner tier exports billing/tenancy atoms: `Credits`, `OrgRole`, `Subscribed`, `SubscriptionActive` in `sdk.authz.__beginner__` and in the root (`__beginner__ = tuple(__all__)`) | billing, tenancy | `sdk/authz.py:143-151`; `sdk/__init__.py:23-24`, `:33-37`, `:52-58`, `:75` | verified |
| B-N15 | naming | `Permissions` (`perm.users.*`, `perm.roles.*`, …) and `Roles` (`admin/support/ops`): "Identity owns the authoritative rows and seeds these codes"; public in `sdk.authz` | identity | `shared/authz/constants.py:1-31`; `shared/authz/__init__.py:48`; `sdk/authz.py:57`, `:61` | verified |
| B-N16 | naming | `UserContext.roles` / `.permissions` first-class, commented "From 'identity' module"; docstring names billing and tenancy claims | identity | `shared/subject.py:60-77` | verified |
| B-N17 | naming | `sandloom check` rules name tenancy's function `require_org_role` and constants `ROLE_ADMIN/ROLE_OWNER/ROLE_MEMBER`, the atom `OrgRole`, the role literals, and `BILLING_ATOMS` / `ORG_IF_ORGANIZATION` | tenancy, billing | `core/archcheck/api10_rules.py:167-178`, `:499-502`; `core/archcheck/module_rules.py:57-64`; tenancy defines `require_org_role` at `sandloom_tenancy/access/__init__.py:59` | verified |
| B-N18 | naming | error-code texts for billing atoms (SANDLOOM-B017, C027 `account-route-mismatch`) | billing | `core/error_codes.py:513`, `:883-888`; `core/archcheck/runner.py:134` | verified |
| B-N19 | naming | migration lint knows identity's index `external_identities.ix_external_identities_provider` | identity | `core/testing/static_checks.py:71` | verified |
| B-N20 | naming | `contract_cache_shadow` documents `CONTRACT_CACHE_SHADOW` with `tenancy.membership.v1` and `tenancy.member_*.v1` as its configuration example; ADR-0003 motivates it with tenancy reads | tenancy | `core/contract_cache_shadow.py:1-21` | verified |

## 5. Class (c): the kernel ships module-owned code or artifacts

| id | type | source → target | owner | evidence | status |
|---|---|---|---|---|---|
| C1 | package | generated contract stubs in the kernel package: `billing_credits_v1`, `billing_entitlements_v1`, `identity_contact_v1`, `identity_security_v1`, `tenancy_membership_v1` (import `sandloom.core.descriptors.contract_refs`) | billing, identity, tenancy | `shared/contracts/*_v1.py`; e.g. `shared/contracts/billing_credits_v1.py:4`, `:55` | verified |
| C2 | package | DTO copies: `identity_dto` (`AccountStanding`, `PlatformAdmin`, `UserContact`), `tenancy_dto` (`MemberState`) | identity, tenancy | `shared/contracts/identity_dto.py:6-7`, `:24`, `:36`, `:48`; `shared/contracts/tenancy_dto.py:6`, `:22` | verified |
| C3 | package | 34 event JSON schemas (billing 7, identity 15, tenancy 12) | billing, identity, tenancy | `shared/events/*.json` | verified |
| C4 | package | generator and rules make `sandloom.shared` the artifact home: `shared_package: sandloom.shared`; `_shared_packages` always includes `"sandloom.shared"`; the kernel wheel ships `shared` whole | billing, identity, tenancy | `architecture_rules.yml:17`; `core/cli/generated.py:483-488`, `:576`; `packages/sandloom/pyproject.toml:129-140` | verified |
| C5 | import | `shared/membership.py`: hand-written consumer logic for `tenancy.member_*` ordering (`member_event_action`, `refreshed_state_wins`), imports tenancy's DTO copy | tenancy | `shared/membership.py:1-32` | verified (that the logic is tenancy-owned: inferred from its docstring `:1-25`) |

Who consumes C1-C5 (verified by grep):

- `sandloom_admin/startup.py:14` imports `IDENTITY_SECURITY_V1` (C1).
- `sandloom_billing/services/tenant_resolver.py:56` imports `MemberState` from the kernel
  wheel's `tenancy_dto` (C2): billing reads tenancy's DTO through the kernel wheel.
- `sdk/_prim/recipes.py` (B-N11) imports C1 and C2.
- C5 has one importer in the repository: `tests/integration/test_revocation_pipeline.py:41`;
  docstrings in `sandloom_tenancy/__init__.py:44`, `sandloom_tenancy/contracts.py:95`,
  `sdk/_decl/projections.py:328-329` point to it.
- `sandloom_identity/schemas/contact.py:7`, `operator.py:5` and
  `sandloom_tenancy/schemas/membership.py:29` mention C2 in docstrings only.

### 5.4 Kernel API with exactly one module consumer (no edge in code; context)

These kernel names do not name a module in code, but each has one consumer and a
docstring tied to that module. Verified by grep over `src/sandloom_*` and `examples/`.

| kernel name | only consumer | evidence |
|---|---|---|
| `operation_intent`, `release_check`, `external_call`, `ResolvedAccount` ("the billing account", "PSP session") | `sandloom_billing` | `core/unit_of_work/intent.py:1`, `:33`, `:91-92`; `core/route_policy.py:166`, `:216-220`; export `sdk/web.py:10-13`, `:37` |
| `MoneyLedger` (docstring example `billing_credit_ledger`) | `sandloom_billing/models/credits.py:18`, `:43` | `core/sql_alchemy/money_ledger.py:6`; export `sdk/data.py:66` |
| `create_access_token`, `hash_password`, `block_user`, `issues_tokens` | `sandloom_identity` | `sdk/security.py:8`, `:22`; consumers in `sandloom_identity/{__init__,api/auth,api/sso,api/user_safety,consumers,services/token_service}.py` |
| `tenant_kind`, `TenantKind` (B-R2) | none outside the kernel | export `sdk/tenant.py:7-8`, `:22` |
| `Permissions`, `Roles` (B-N15) | none: no import outside `shared/authz/__init__.py:48` and `sdk/authz.py` | grep of `Permissions\.|Roles\.|import.*Permissions|import.*Roles` over `src`, `examples`, `tests` |
| `Feature`, `WithinLimit` claim keys `features`, `limits` (B-N12) | no bundled module contributes these claim keys (billing contributes `subscription_status`, `plan_code`: `sandloom_billing/claims.py:3-7`) | grep of `"features"` over `src/sandloom_*` outside tests: no match |

## 6. Mentions that are not edges

### 6.1 Docstrings, help and comments

148 `.py` files (169 files with non-`.py`) under `core`, `sdk`, `shared`, `main.py`,
excluding tests, match `\b(identity|tenancy|billing)\b`. The word also matches unrelated
uses (`rpc_identity`, key identity), so the count is an upper bound. By directory:
`core/cli` 24, `shared/events` 15, `core/config` 11, `core/testing` 10, `shared/authz` 9,
`shared/contracts` 7, `core/sql_alchemy` 7, `core/module_api` 6, `sdk/_prim` 5, five each
in `core/unit_of_work`, `core/facade`, `core/eventing`, the rest 1-4. Rows above already
cover the functional ones; the remainder are examples such as
`"billing.credits.v1"` in `core/contract_testing.py:28`, `core/testing/sagas.py:30`,
`core/descriptors/sagas.py:413-422`, `sdk/_decl/sagas.py:5`, `:66`,
`core/sql_alchemy/durable_inbox.py:60`, `:121`, `core/config/contracts.py:23`,
`core/facade/_services.py:31`, `:40`, `:45`, `core/migrations/migrations.py:618`, `:632`,
`core/cli/migrate.py:288`, `:374`. Reproduce:
`grep -rlE "\b(identity|tenancy|billing)\b" src/sandloom/{core,sdk,shared} src/sandloom/main.py | grep -v /tests/`.

`core/observability/guardrails.py:24` names `stripe_secret_key` in a comment; the
redaction list itself is generic substrings.

### 6.2 Kernel tests (not shipped)

92 files in `src/sandloom/core/tests` mention a module name; two import a module:
`test_redis_cache_bounded.py:277` (`sandloom_identity.lockout`),
`test_asymmetric_jwt.py:418` (`sandloom_identity`). Root `tests/` import
`sandloom.shared.membership` and `sandloom.shared.contracts` (section 5).

## 7. Runtime graph (`sandloom graph`)

Commands (run at `b8d8506`, dev environment from `uv sync`):

```bash
uv run sandloom graph --format text
uv run sandloom graph --format json --detail full --max-bytes 2000000
```

Text output:

```
Sandloom architecture · declared model
Freshness: source changed since last generate

billing: … └─ 28 routes · 10 tables · provides billing.credits.v1, billing.entitlements.v1 · 7 events · calls tenancy
identity: … └─ 49 routes · 9 tables · provides identity.contact.v1, identity.security.v1 · 15 events
tenancy: … └─ 17 routes · 4 tables · provides tenancy.membership.v1 · 12 events
```

JSON facts (verified):

- `scope`: `authoring_composition`, modules `billing`, `identity`, `tenancy`. `sandloom_admin`
  is absent: the graph composes the installed `sandloom.modules` entry points
  (`core/cli/inspection/collect.py:242-250`), and the dev environment installs
  `sandloom`, `sandloom-billing`, `-identity`, `-tenancy` only (`uv pip list`;
  `pyproject.toml:11-14`); admin is on the test `pythonpath` only (`pyproject.toml:185`).
- edges: `produces_event` 34, `requires_capability` 7, `provides_contract` 5,
  `owns_consumer` 2, `consumes_event` 2, `requires_contract` 1.
- module relation: one, `billing → tenancy` `optional_contract` (`tenancy.membership.v1`,
  `required: false`). No cycles, no unresolved requirements.
- identity consumes kernel events `core.auth.user_blocked.v1` and
  `core.auth.tokens_revoked.v1` (module → kernel direction).
- `process_counts`: 94 routes, 0 sagas.
- `freshness.state`: `source_changed` for all three modules.

What the graph does not show: the kernel is not a node. None of the edges in sections 3-5
appears in it; kernel → module calls (B-R1..B-R6), claim-key reads (B-S3) and the
platform gate (B-S1, B-S2) are invisible to `sandloom graph`.

## 8. Package graph (`packages/*/pyproject.toml`)

| wheel | ships (`[tool.sandloom.sources]`) | depends on | extras | entry points |
|---|---|---|---|---|
| `sandloom` 0.7.0a11 | `src/sandloom` → `sandloom` (core, sdk, shared incl. C1-C3, driverkit, main); `migrations/core` → `sandloom/core/migrations/core_chain`; excludes `sandloom/AGENTS.md`, `CLAUDE.md`, every `tests` dir | 22 third-party (fastapi, sqlalchemy, pydantic, pyjwt, argon2-cffi, …); no business module | `redis`, `celery`, **`standard`** = `sandloom[redis,celery]` + `sandloom-identity`, `-tenancy`, `-billing` `>=0.7.0a11,<0.8`; **`admin`** = `sandloom[redis,celery]` + `sandloom-admin`, `sandloom-identity`; `fast`, `check`, `testing`, `sqlite`, `sentry` | script `sandloom`; `sandloom.sql_dialects` postgresql, sqlite |
| `sandloom-identity` | `sandloom_identity` | `sandloom>=0.7.0a11,<0.8`, pyotp, authlib, qrcode, pillow, itsdangerous, email-validator | none | `sandloom.modules`: `identity` |
| `sandloom-tenancy` | `sandloom_tenancy` | `sandloom`, email-validator | none | `tenancy` |
| `sandloom-billing` | `sandloom_billing` | `sandloom` | none | `billing` |
| `sandloom-admin` | `sandloom_admin` (incl. built console assets) | `sandloom` | none | `admin` |

Evidence: `packages/sandloom/pyproject.toml:16-53`, `:61-104`, `:106-112`, `:129-140`;
`packages/sandloom-<m>/pyproject.toml` `dependencies`, `[project.entry-points."sandloom.modules"]`,
`[tool.sandloom.sources]` (parsed with `tomllib`); test-dir exclusion
`tools/packaging/package_build.py:45`. Core migration chain `0001`-`0013` holds
infrastructure tables only (file names in `migrations/core/versions/`).

Package edges from the kernel wheel to module wheels: the two extras (`standard`,
`admin`). No module wheel ships its own stubs, DTO copies or event schemas; they ship in
the `sandloom` wheel (C1-C4). Every module wheel depends on `sandloom`; no module wheel
depends on another.

## 9. Existing guards

| guard | what it checks (read in code) | catches | misses | run at b8d8506 |
|---|---|---|---|---|
| `tests/architecture/test_shared_purity.py` (G-KERNEL-10) | AST imports of `src/sandloom/shared/**` against `sandloom.<m>` for each subdirectory `m` of `src/sandloom` with `__init__.py`, except `core`, `shared`, `sdk` (`:20-39`); relative imports resolved (`:47-58`) | `shared` importing `sandloom.driverkit` (the only discovered "module": printed `{'driverkit'}`) | every business module: they are top-level `sandloom_<m>`, never `sandloom.<m>`; dynamic imports; `core` and `sdk`; non-import coupling | passes |
| G-KERNEL-13 `src/sandloom/core/tests/test_module_names.py:165-192` | exact string constants `identity`, `tenancy`, `billing`, `sandloom_<n>`, `sandloom.<n>` in `core/config/*.py`, `core/app_factory/*.py`, `core/loader/plugin_loader.py`, `main.py`; `sandloom_modules`, `module_required` defaults are `[]` | a default module list in settings, app factory, loader or `main` | `admin` / `sandloom_admin`; any substring (`"billing_provider"`, `"identity.security.v1"`, `"sandloom_billing.psp…"`); every other directory (`cli`, `shared`, `sdk`, `testing`, `admin`, `archcheck`, …). Lines that pass it inside its own scope: B-C1, B-C3, B-C4, B-C5, B-C6 | passes (called directly) |
| `.importlinter` + `tests/architecture/test_kernel_import_contracts.py` | 7 contracts: core layers, acyclic siblings, config leaf, sdk → public kernel surface, kernel never imports sdk/driverkit, `_decl` L1, sdk layers | import direction inside `sandloom` | anything outside `sandloom`: `root_package = sandloom`, no `include_external_packages`, so `sandloom_*` are not in the graph; no contract names `sandloom_*`; dynamic imports by computed name; string coupling | `lint-imports`: 7 kept, 0 broken (484 files, 1739 dependencies) |
| `tools/packaging/distribution_imports.py` (via `check_package_distributions.py` step 2, `:252-262`; unit test `tests/architecture/test_distribution_imports.py`) | every runtime import in each wheel's `.py` files, module-level and function-local, `TYPE_CHECKING` skipped (`:136-173`); a first-party import must resolve to a wheel in the importer's dependency closure (`:274-283`) | a kernel import of a module wheel **only when the kernel wheel does not depend on it**; dynamic `importlib.import_module("literal")` / `__import__("literal")` and any call whose name ends in `import`/`import_module` with a string literal (`:118-133`) | **extras rule**: `read_wheel` collects every `Requires-Dist` regardless of extra marker (`:68-71`) and `_closure` follows them all (`:202-204`), so `sandloom` may import `sandloom_identity/_tenancy/_billing/_admin` through `standard`/`admin`; a dynamic import whose argument is a name or f-string (`password_check.py:42` passes a variable); string coupling. The `try/except ImportError` exemption applies to third-party imports only (`:274-285`): a guarded first-party import is checked | probe (synthetic wheels, `check()`): static import with extra declared → passes; without extra → flagged; guarded `try/except ImportError` without extra → flagged; `import_module("literal")` without extra → flagged; `import_module(NAME)` and `import_module(f"…")` → pass |
| `tools/packaging/check_package_distributions.py` lean install (`check_lean_install`, `:307-379`) | clean venv with the `sandloom` wheel alone; fails if `redis`, `celery`, `sentry-sdk`, `sandloom-identity` got installed (`:324-328`); every subcommand `--help`, `sandloom docs`, `sandloom new`, battery handoff, inspection smoke, RPC transport smoke, and `create_app()` + `import sandloom.main` with `SANDLOOM_MODULES=[]` and no loaded modules (`:399-413`) | a module-level kernel import of a module on the paths those commands import; a kernel that cannot build an app with no modules | code paths not executed (a request through `require_platform_admin`, `sandloom admin create` beyond `--help`, live atoms); string and contract coupling (B-R*, B-S*, B-C*); leak check lists only `sandloom-identity` among module wheels, not tenancy, billing, admin | not run (builds and installs wheels; network) |
| `tests/architecture/test_module_boundaries.py` | cross-module imports inside `src/sandloom_*` (kernel excluded, `:27-36`) | module → module imports | kernel → module: out of scope | not run |
| `tools/checks/check_architecture.py` | non-core files under `src/sandloom` against `architecture_rules.yml` public API and forbidden patterns (`:1-18`) | module-facing rules | kernel → module: out of scope | not run |

No guard found for: contract names in kernel code (B-R*), claim keys or claim sources
(B-S*), module settings in kernel config (B-C*), generated artifacts in the kernel package
(C1-C4), CLI/template naming (B-N5..B-N10). Search: `tests/architecture/*.py`,
`src/sandloom/core/tests/*.py`, `tools/checks`, `tools/packaging` for `sandloom_identity`,
`_BUNDLED`, `names no module`.

## 10. Corrections to the earlier inventory (`_inputs/inventory.md`)

The inventory was produced at `main cc90200`; line numbers below are at `b8d8506`.

1. **Header commit.** It cites `main cc90200`, not `b8d8506`. The in-flight item in its
   section B (branch `claude/generate-stubs-sdk-c52485`) is merged at `b8d8506`
   (`977eeea`, `8715381`); framework-tree stubs still import
   `sandloom.core.descriptors.contract_refs` (`shared/contracts/billing_credits_v1.py`).
2. **A / guard text: "distribution_imports.py:12 exempts guarded imports".** Wrong for
   this case. The exemption covers third-party imports only (`distribution_imports.py:11-13`,
   `:274-285`). `password_check.py:42` passes because its argument is a variable, not a
   literal (`:129-132`), and because the extras rule would allow `sandloom-identity` anyway.
3. **"identity/schemas/contact.py:7, operator.py:5; tenancy/schemas/membership.py:29" as
   users of the DTO copies.** Docstring mentions, not imports. The only module importer of
   a DTO copy is `sandloom_billing/services/tenant_resolver.py:56`.
4. **D: `Feature` / `WithinLimit` "Public beginner".** They are public in `sdk.authz` but
   not in `__beginner__` (`sdk/authz.py:143-151`). Beginner atoms: `Credits`, `OrgRole`,
   `Subscribed`, `SubscriptionActive`.
5. **D: `Permissions` / `Roles` "used by sandloom_identity only".** No importer anywhere
   outside `shared/authz/__init__.py:48` and `sdk/authz.py`; identity does not seed these
   codes from them (no `perm.users` literal in `src/sandloom_identity`).
6. **E: settings list incomplete.** `ModulesConfig` also holds `ADMIN_CONSOLE_ENABLED`,
   `ADMIN_CONSOLE_DIST` (admin) and `TELEGRAM_BOT_TOKEN` (identity): 22 settings.
   `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` is a semantic tier (`tiers.py:332-338`).
7. **Missed edges:** B-S3 also at `shared/authz/_resolve.py:68` and `core/testing/auth.py:49`;
   B-C7 (`core/testing/deploy_profile.py` shared keys and dev env); B-C10 and B-N3
   (`cli/admin.py` reads `admin_console_enabled`, links `/admin/set-password`); B-N2
   (manifest file name shared with `sandloom_admin/api.py:16`); B-N17 names tenancy's
   function `require_org_role` and `ROLE_*` constants; B-N20 (`contract_cache_shadow`);
   A7 (admin compiler) as protocol.
8. **Guard gaps: "test_shared_purity … cannot fail".** It can fail: its discovered set is
   `{'driverkit'}`, so a `shared` import of `sandloom.driverkit` fails it. It cannot see
   `sandloom_*`.
9. **Guard gaps: `.importlinter`.** In addition to having no forbidden contract,
   `root_package = sandloom` without `include_external_packages` keeps `sandloom_*` out of
   the analysed graph.
10. **Line shifts** (content verified): `generated.py:480` → `:483-488`;
    `generated.py:543` → `:551`; `accepted_risks.py:56-61` → `:56-62`;
    `tiers.py:396-405` → `:396-406`; `architecture_rules.yml:14-17` → `:17`.
11. **F: wizard lines.** "default selection none" holds: the checkbox starts with `()`
    and the typed prompt reads "Enter: none" (`core/cli/wizard.py:450-472`); the module
    list is at `:55-62`, the admin → identity rule at `:403-415`, `:441-446`.
12. **H: "Core migration chain 0001-0013 is clean"**: file names checked (all
    infrastructure); revision contents not read.

## 11. Recorded rationale (inputs, checked where cited)

- ADR-0001 update 2026-10-28: `sandloom.shared` ships whole in the `sandloom` wheel with
  "the standard modules' generated stubs and event schemas"; the kernel names no module
  (`docs/adr/0001-framework-distribution.md:236-262`). Verified.
- `shared/authz/constants.py:3-6`: constants are in `shared` so modules need not import
  identity. Verified.
- `shared/authz/live_contracts.py:13-15`: defaults "keep the current wiring working".
  Verified.
- ADR-0003 table: `OrgRole` reads membership through the live adapter
  (`docs/adr/0003-fewer-sync-rpc-membership-reads.md:28`). Verified.
- `design/first-admin/design.md:65-69`: the CLI calls identity through
  `identity.security.v1` so it works on a split deploy. Verified.
- `design/admin-delivery/research.md:214-224`: console as a module; manifest written by
  `sandloom generate`. Verified.
- `core/config/settings.py:636-639`: comment "the kernel names no business module" next
  to the billing validator. Verified.
- ADR-0010 lines 55-72, 189-199, 269 (beginner tier with billing atoms, required
  `of=Account`): **Unverified** (not read in this pass).
- ADR-0009 item 6 as the source of the admin risk tag: `shared/guards/platform.py:10` and
  `core/admin/api/platform.py:75` cite it; the ADR text was not read. **Unverified**: that
  no ADR justifies the hard-coded `"identity"` source.
- Repository history starts at `8d719dd` (2026-10-28, "Initial Sandloom source
  snapshot"). Verified (`git log --reverse`).

## 12. Unverified or unknown

- Whether any edge in section 4 is exercised in a split deployment differently from the
  monolith (no split run in this pass).
- `features` / `limits` claim producers outside this repository (only this repository was
  searched).
- `check_package_distributions.py` lean and standard installs at `b8d8506`: read, not run.
- ADR-0009 and ADR-0010 line references in the inventory.
