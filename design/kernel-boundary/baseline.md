# Kernel boundary baseline: what the kernel knows about business modules

Commit: `b8d8506` (branch `arch/kernel-boundary`). Every `file:line` below is at that commit.
Task: tm `kb-baseline` (parent `kb-assess`). This file describes; it proposes nothing.
Closed by tm `kb-baseline-close` (Codex review 2026-11-08): verdict, rationale and
provisional-action columns on every (b) and (c) row; A19 reclassified as disputed
(section 3.1); the installs and the graph run, not read (sections 7, 13); the
security-authority facts (section 14). Transcripts: `_evidence/`.

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

**Verdict, rationale, action** (columns of every (b) and (c) row, and of A19)

- *Verdict* against the owner rule "the kernel knows no business module":
  `allowed` (the name is incidental: the mechanism is generic), `violation` (the kernel
  depends on a module-owned thing), **`disputed`** (a reading exists under which the
  thing is a platform concept or composition, not module-owned; the ADRs decide).
- *Rationale*: who owns the thing by its nature: `platform guarantee` (a property the
  framework promises every app: isolation, admission, revocation), `module domain`
  (a business module's own concept), `application composition` (which modules an app
  installs and wires). A disputed row names both readings.
- *Action (provisional)*: `retain`, `relocate` (the thing moves to its owner),
  `generalize` (the kernel keeps a mechanism, the module supplies the content),
  `investigate` (a question is open). Provisional: labels for the assessment, not
  decisions; the ADRs decide.

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
| (a) protocol | 9 | 3 | 3 | 2 | 1 | 0 | **18** |
| (a) reclassified disputed: A19 (section 3.1) | 0 | 0 | 1 | 0 | 0 | 0 | **1** |
| (b) names a module | 1 | 0 | 6 | 10 | 3 | 20 | **40** |
| (c) ships module-owned code/artifacts | 1 | 4 | 0 | 0 | 0 | 0 | **5** |

Not counted as edges: docstring and help-text mentions (section 6), kernel APIs whose
only consumer is one module but whose code names no module (section 5.4), and kernel
tests (section 6.2).

**Verdicts** over the 46 judged rows ((b), (c) and A19). An edge is a place to judge,
not a thing to remove: 40 (b) rows are not 40 removals.

| verdict | (b) | (c) | A19 | total | rows |
|---|---|---|---|---|---|
| allowed | 1 | 0 | 0 | **1** | B-N20 |
| violation | 28 | 4 | 0 | **32** | B-I1, B-R1-B-R3, B-R6, B-C1-B-C10, B-N1-B-N4, B-N8, B-N9, B-N12-B-N15, B-N17-B-N19, C1-C3, C5 |
| disputed | 11 | 1 | 1 | **13** | B-R4, B-R5, B-S1-B-S3, B-N5-B-N7, B-N10, B-N11, B-N16, C4, A19 |

| action (provisional) | retain | relocate | generalize | investigate |
|---|---|---|---|---|
| rows | 1 | 17 | 11 | 17 |

Most disputed rows share one shape: a platform concept (the platform admin, security
revocation, tenant isolation, the claim format of roles, the composing app's shared
package, live checks) whose only implementation today is one bundled module, so the
kernel names that module. The rest (B-N5-B-N7, B-N10, B-N11) are starter, template and
recipe text, which `kb-assess` schedules after the four decisions.

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
| A19 | runtime call | live-adapter registry: `register_live_adapter(kind, adapter)` with kinds `entitlements`, `membership` (the mechanism; its defaults are B-R1). **Reclassified: disputed** (section 3.1) | `shared/authz/live_contracts.py:38-39`, `:125-140` | verified, run |

### 3.1 A19 reclassified as disputed

| id | verdict | rationale | action (provisional) |
|---|---|---|---|
| A19 | **disputed** | platform guarantee (a live-check indirection the kernel offers) vs module domain (its only kinds are billing's and tenancy's questions) | investigate |

Question: can a third-party module (e.g. `inventory`) add its own access check without
a kernel change? Answer from code, run in the lean venv
(`_evidence/a19-live-adapter-probe.txt`):

- **Not through this registry.** `register_live_adapter` accepts exactly the kinds
  `entitlements` and `membership`; any other kind raises `ValueError: unknown live
  adapter kind 'inventory'; expected ['entitlements', 'membership']`
  (`shared/authz/live_contracts.py:139-144`; probe step 1). The kinds are the questions
  of three fixed atoms, `SubscriptionActive` / `Credits` and `OrgRole`
  (`live_contracts.py:3-15`, `:43-71`). The table holds one adapter per kind for the
  whole process (`:114`, `:164`): a third-party adapter registered for `membership`
  replaces tenancy's binding for every `OrgRole` in the process (probe step 2:
  `membership slot now -> inventory.stock.v1`).
- **Yes through two other public paths**, with no kernel change:
  1. a subclass of `Requirement` (public in `sandloom.sdk.authz`, advanced;
     `shared/authz/algebra.py:41-62`) with its own `evaluate`; it composes with `& | ~`
     (probe step 3: allowed);
  2. an `AccessFact` (public) registered by the module through
     `ModuleAPI.register_access_fact` (`core/module_api/_registration.py:525-534`) and
     gated with `Entitled("inventory.<fact>")` (`shared/authz/atoms_fact.py:32-120`);
     the fact's owner is its first dotted segment, so a remote process resolves it
     through `inventory.facts.v1` (`shared/authz/facts.py:534-541`); a claim-mode fact
     is trusted only when its owner is in `claims_sources` (`facts.py:296-307`; probe
     step 4: allowed with `inventory` in `claims_sources`, `fact_unavailable` without).
- **Limits of those paths** (facts, probe step 5): a fact whose name does not start
  with its owner resolves to the wrong owner (`stock.available` → `stock.facts.v1`)
  unless the kernel's `WELL_KNOWN_FACT_OWNERS` lists it (B-R3), which is how billing's
  `subscription.active` reaches `billing.facts.v1`; a `Requirement` subclass gets no
  adapter indirection (the contract it calls is its own code).

Why disputed: the registry itself is closed to modules (only kernel-defined kinds) and
its two kinds are billing's and tenancy's domain questions; the extension the
question asks for exists, but beside the registry, not through it.

## 4. Class (b): the kernel names a specific module

### 4.1 Imports

| id | type | source → target | owner | evidence | status | verdict | rationale | action (provisional) |
|---|---|---|---|---|---|---|---|---|
| B-I1 | import (dynamic) | `sandloom admin create` prompt → `importlib.import_module("sandloom_identity.password_policy")` inside `try/except ImportError`, fallback 8/100 | identity | `core/cli/password_check.py:21`, `:40-44`; used `core/cli/admin.py:49`, `:255` | verified | violation | module domain | relocate |

### 4.2 Runtime calls to a named contract

| id | type | source → target | owner | evidence | status | verdict | rationale | action (provisional) |
|---|---|---|---|---|---|---|---|---|
| B-R1 | runtime call | default live adapters registered at import: `billing.entitlements.v1` (`check_subscription_active`, `get_credit_balance`), `tenancy.membership.v1` (`has_role_at_least`) | billing, tenancy | `shared/authz/live_contracts.py:74-104`, `:107-111` | verified | violation | module domain | generalize |
| B-R2 | runtime call | `tenant_kind` classifies a tenant as organization by asking the membership adapter (tenancy by default) | tenancy | `shared/authz/account.py:38`, `:154`, `:170-196` | verified | violation | module domain | investigate |
| B-R3 | runtime call | `WELL_KNOWN_FACT_OWNERS`: facts `subscription.active`, `organization.subscription.active` → owner `billing` (so `billing.facts.v1`) | billing | `shared/authz/facts.py:526-531` | verified | violation | module domain | generalize |
| B-R4 | runtime call | `sandloom admin create/list/…` → `identity.security.v1` (`password_problem`, `account_standing`, `create_platform_admin`, `take_over_platform_admin`, `set_password_token`, `list_platform_admins`) | identity | `core/cli/admin.py:61-62`, `:172-195`, `:251`, `:318-331`, `:362-383`, `:461` | verified | **disputed** | platform guarantee (operator bootstrap of the platform admin) vs module domain (identity's users) | investigate |
| B-R5 | runtime call | `sandloom security revoke-sessions / force-password-reset [--tenant]` → `identity.security.v1` (`list_user_ids`, `account_standing`, `appoint_platform_admin`, batch method by name), `tenancy.membership.v1` (`list_member_user_ids`) | identity, tenancy | `core/cli/security.py:60-63`, `:134-144`, `:182-193`, `:367-389` | verified | **disputed** | platform guarantee (security revocation) vs module domain (identity's users, tenancy's members) | investigate |
| B-R6 | runtime call | `fake_access_fact` (public, beginner in `sdk.testing`) knows the two subscription facts and stubs `billing.entitlements.v1` when billing is not loaded | billing | `core/testing/authz.py:21-31`, `:46-50`, `:120-128`; `sdk/testing/__init__.py:209-212` | verified | violation | module domain | relocate |

### 4.3 Security authority

| id | type | source → target | owner | evidence | status | verdict | rationale | action (provisional) |
|---|---|---|---|---|---|---|---|---|
| B-S1 | security authority | `require_platform_admin` (public in `sdk.web`) trusts role `admin` only when `"identity"` is in `claims_sources` and not in `claims_sources_failed` | identity | `shared/guards/platform.py:10-19`; export `sdk/web.py:55-56`, `:87`; used by `sandloom_identity/api/user_safety.py:21`, `sandloom_tenancy/api/platform.py:7`, `sandloom_billing/api/platform.py:12` | verified | **disputed** | platform guarantee (the platform-admin gate) vs module domain (trusts identity by name; section 14) | investigate |
| B-S2 | security authority | `platform_operator` gate of every `/api/v1/platform/<name>` router: same `"identity"` source test on the bearer claims | identity | `core/admin/api/platform.py:54-76`, `:272` | verified | **disputed** | platform guarantee (the platform-admin gate) vs module domain (trusts identity by name; section 14) | investigate |
| B-S3 | security authority | tenant allowlist from claim key `tenant_ids`, which `sandloom_tenancy` contributes; read by `TenantScope`, by `_resolve`, written by the test token helper | tenancy | `core/http/tenant_scope.py:5-6`, `:172`; `shared/authz/_resolve.py:68`; `core/testing/auth.py:49`; producer `sandloom_tenancy/claims.py:34` | verified | **disputed** | platform guarantee (tenant isolation: a security-claim format the kernel reads) vs module domain (tenancy produces it) | investigate |

Facts behind B-S1/B-S2 (verified): the source name stamped into `claims_sources` is the
contributor's `owner`: the `owner=` argument of `CoreFacade.add_claims_contributor`, else
the short name of the registering module (`core/facade/_claims.py:219-228`); the short
name is the entry-point name, else the last import segment
(`core/descriptors/module_names.py:95-104`). Correction (kb-baseline-close): the module
API `ModuleAPI.add_claims_contributor(fn, *, critical)` has no `owner` parameter
(`core/module_api/_registration.py:406-419`), so a module going through `define_module`
gets its own short name. The `roles` claim is contributed by
`sandloom_identity/claims.py:65-68`, registered `critical=True` at
`sandloom_identity/__init__.py:57`. Who sets and reads each reserved claim, and what any
contributor can write: section 14.

### 4.4 Configuration

| id | type | source → target | owner | evidence | status | verdict | rationale | action (provisional) |
|---|---|---|---|---|---|---|---|---|
| B-C1 | configuration | `ModulesConfig` ("Settings of the bundled business modules") mixed into `Settings`: `TWOFA_ENCRYPTION_KEY`, `ADMIN_CONSOLE_ENABLED`, `ADMIN_CONSOLE_DIST`, `BILLING_PROVIDER`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `BILLING_DENY_ON_PAST_DUE`, `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS`, `BILLING_INTENT_RECONCILE_AFTER_MINUTES`, `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS`, `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES`, `GOOGLE/GITHUB/MICROSOFT/LINKEDIN_CLIENT_ID/SECRET`, `TELEGRAM_BOT_TOKEN`, `SSO_ALLOWED_REDIRECT_URIS` (22 settings) | identity, billing, admin | `core/config/modules.py:1-190` (aliases at `:19`, `:30`, `:38`, `:48`, `:56`, `:61`, `:69`, `:82`, `:97`, `:109`, `:124`, `:135-170`, `:175`, `:184`); `core/config/settings.py:46`, `:100` | verified | violation | module domain | relocate |
| B-C2 | configuration | `TENANT_CLAIMS_MAX_ORGANIZATIONS` in the kernel auth config; read only by tenancy | tenancy | `core/config/auth.py:88-95`; tier `core/config/tiers.py:620`; readers `sandloom_tenancy/claims.py`, `sandloom_tenancy/contracts.py` | verified | violation | module domain (bounds tenancy's claim; read by tenancy only) | relocate |
| B-C3 | configuration | startup validation of `TWOFA_ENCRYPTION_KEY` key strength | identity | `core/config/settings.py:497-505` | verified | violation | module domain | relocate |
| B-C4 | configuration | `_billing_requirements`: refuses explicit `BILLING_PROVIDER=fake` in prod; requires Stripe keys for `stripe` | billing | `core/config/settings.py:623-660` | verified | violation | module domain | relocate |
| B-C5 | configuration | accepted risk `fake_psp_in_prod`, citing `sandloom_billing.psp.psp_configuration_error` | billing | `core/config/accepted_risks.py:56-62` | verified | violation | module domain (the risk registry `RISKS` is a closed kernel tuple: `core/config/accepted_risks.py:50`, `:145`) | generalize |
| B-C6 | configuration | tiers: `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` is a **semantic** setting (counts toward the ratchet of 16); the other module settings are ops; two removed `BILLING_*` settings, one citing `sandloom_billing.psp.sign_stripe_payload` | billing, identity, admin | `core/config/tiers.py:332-338`, `:396-406`, `:659-678` | verified | violation | module domain (the tier registry is kernel-owned) | generalize |
| B-C7 | configuration | deploy-profile check (SANDLOOM-T014): `BILLING_DENY_ON_PAST_DUE` in `SHARED_KEYS`; dev env generator writes `TWOFA_ENCRYPTION_KEY`, `BILLING_PROVIDER=fake` and the `fake_psp_in_prod` risk | billing, identity | `core/testing/deploy_profile.py:66`, `:87`, `:928`, `:935-942` | verified | violation | application composition | generalize |
| B-C8 | configuration | backup plan text lists `TWOFA_ENCRYPTION_KEY`, `STRIPE_*`, "operator reset-2fa" | identity, billing | `core/migrations/backup.py:252-259` | verified | violation | module domain | generalize |
| B-C9 | configuration | platform console settings view includes `billing_provider` | billing | `core/admin/api/platform.py:608` | verified | violation | module domain | relocate |
| B-C10 | configuration | `sandloom admin` reads `settings.admin_console_enabled` to choose the set-password link target | admin | `core/cli/admin.py:130-132`, `:145-160` | verified | violation | application composition (whether the console is deployed) | investigate |

Readers of B-C1 settings outside the kernel (verified by grep of field names):
`twofa_encryption_key` → `sandloom_identity/services/twofa_service.py`; `admin_console_*`
→ `sandloom_admin/api.py`; `billing_*`, `stripe_*` → `sandloom_billing/{psp,access,services,api}`;
SSO and Telegram fields → `sandloom_identity/{integrations,services/sso_service}.py`.
Kernel readers: `settings.py` (B-C3, B-C4), `admin/api/platform.py` (B-C9),
`cli/admin.py` (B-C10). Settings classes in the kernel: one `BaseSettings`
(`core/config/settings.py:113`); a search for `BaseSettings|module_settings|settings_model`
in `core`, `sdk`, `shared` finds no other.

### 4.5 Naming (code, data and generated text that carry a module's name or owned identifier)

| id | type | source → target | owner | evidence | status | verdict | rationale | action (provisional) |
|---|---|---|---|---|---|---|---|---|
| B-N1 | naming | `sandloom generate` writes the admin manifest only when `"sandloom_admin"` is composed | admin | `core/cli/generated.py:547-552` | verified | violation | module domain | generalize |
| B-N2 | naming | manifest file name `admin-manifest.json` defined in the kernel and again in the admin module | admin | `core/cli/generated.py:70`; `sandloom_admin/api.py:16` | verified | violation | module domain | relocate |
| B-N3 | naming | set-password link `{base}/admin/set-password#token=…`: a page of the admin console | admin | `core/cli/admin.py:154`; page `admin/src/features/auth/set-password-page.tsx` | verified | violation | module domain (a console page) | investigate |
| B-N4 | naming | start page probes for a route under `/admin` and links "Console" and `sandloom admin create` | admin | `core/app_factory/pages.py:134-139`, `:287-302` | verified | violation | application composition | generalize |
| B-N5 | naming | `sandloom new`: `_BUNDLED = ("identity","tenancy","billing","admin")`; admin implies identity; distribution name map; install check `find_spec(f"sandloom_{name}")` | all four | `core/cli/project.py:54`, `:1134-1142`, `:1225`, `:1410-1419` | verified | **disputed** | application composition (the starter offers first-party distributions) vs module domain; CLI/starter is a later phase of `kb-assess` | investigate |
| B-N6 | naming | `sandloom new` text blocks keyed on bundled modules: `_IDENTITY_NOTE`, `_LOG_LINKS`, `_IDENTITY_ENV`, `_BILLING_ENV` | identity, billing | `core/cli/project.py:120`, `:129`, `:970`, `:977`, `:1319-1339` | verified | **disputed** | application composition vs module domain; CLI/starter phase | investigate |
| B-N7 | naming | first-run wizard `MODULES` with descriptions; identity forced when admin chosen | all four | `core/cli/wizard.py:55-62`, `:403-415`, `:441-446` | verified | **disputed** | application composition vs module domain; CLI/starter phase | investigate |
| B-N8 | naming | `sandloom add deploy`: `MODULE_ENV` keyed `sandloom_identity` / `sandloom_billing`; help text names bundled modules | identity, billing | `core/cli/scaffold_deploy.py:86-103`, `:860-865` | verified | violation | module domain | generalize |
| B-N9 | naming | deploy templates: `init-databases.sh` database list; compose comment "loading identity" holds `JWT_PRIVATE_KEY`; `sandloom keys` prints "issuer ONLY (identity)" | identity, tenancy, billing | `core/cli/templates/deploy/postgres/init-databases.sh:7`, `:20`; `core/cli/templates/deploy/compose/microservices.yml.tmpl:28`; `core/cli/keys.py:237` | verified | violation | application composition | generalize |
| B-N10 | naming | app skill template `sandloom-split-deploy` lists `add deploy identity/tenancy/billing` | identity, tenancy, billing | `core/cli/templates/skills/sandloom-split-deploy/SKILL.md:12-19` | verified | **disputed** | application composition (app agent documentation) vs module domain; CLI/starter phase | investigate |
| B-N11 | naming | recipes (`sandloom docs recipe …`, 9 recipes) import bundled stubs and DTOs (`TENANCY_MEMBERSHIP_V1`, `BILLING_CREDITS_V1`, `BILLING_ENTITLEMENTS_V1`, `MemberState`) and declare `tenancy.member_*.v2` views; 29 lines match `identity\|tenancy\|billing` | tenancy, billing | `sdk/_prim/recipes.py:18`, `:44`, `:326`, `:383`, `:485`, `:493-509`, `:571`, `:604-606` | verified | **disputed** | application composition (documentation recipes) vs module domain; recipes phase | investigate |
| B-N12 | naming | public authz atoms reading billing's claim and contracts: `Subscribed` (claim `subscription_status`, `ACTIVE_SUBSCRIPTION_STATUSES = {"active","trialing"}`), `Feature` (claim `features`), `WithinLimit` (claim `limits`), `SubscriptionActive`, `Credits` (via B-R1); `Account.user()/org()` "the billing account"; `TenantKind` | billing | `shared/authz/atoms_claims.py:32`, `:77-118`, `:304-310`, `:327-345`; `shared/authz/atoms_live.py:121`, `:143`; `shared/authz/account.py:1-20`, `:41`, `:54-90` | verified | violation | module domain (public API 1.0 surface: SANDLOOM-T008) | relocate |
| B-N13 | naming | `OrgRole` with roles `member/admin/owner` hard-coded (tenancy's role set), via B-R1 | tenancy | `shared/authz/atoms_live.py:168-197` | verified | violation | module domain | relocate |
| B-N14 | naming | beginner tier exports billing/tenancy atoms: `Credits`, `OrgRole`, `Subscribed`, `SubscriptionActive` in `sdk.authz.__beginner__` and in the root (`__beginner__ = tuple(__all__)`) | billing, tenancy | `sdk/authz.py:143-151`; `sdk/__init__.py:23-24`, `:33-37`, `:52-58`, `:75` | verified | violation | module domain (beginner tier: SANDLOOM-T009) | relocate |
| B-N15 | naming | `Permissions` (`perm.users.*`, `perm.roles.*`, …) and `Roles` (`admin/support/ops`): "Identity owns the authoritative rows and seeds these codes"; public in `sdk.authz` | identity | `shared/authz/constants.py:1-31`; `shared/authz/__init__.py:48`; `sdk/authz.py:57`, `:61` | verified | violation | module domain (no importer: section 5.4) | investigate |
| B-N16 | naming | `UserContext.roles` / `.permissions` first-class, commented "From 'identity' module"; docstring names billing and tenancy claims | identity | `shared/subject.py:60-77` | verified | **disputed** | platform guarantee (roles/permissions as the claim format `Role`, `Perm` and the platform gate read) vs module domain (identity produces them) | investigate |
| B-N17 | naming | `sandloom check` rules name tenancy's function `require_org_role` and constants `ROLE_ADMIN/ROLE_OWNER/ROLE_MEMBER`, the atom `OrgRole`, the role literals, and `BILLING_ATOMS` / `ORG_IF_ORGANIZATION` | tenancy, billing | `core/archcheck/api10_rules.py:167-178`, `:499-502`; `core/archcheck/module_rules.py:57-64`; tenancy defines `require_org_role` at `sandloom_tenancy/access/__init__.py:59` | verified | violation | module domain | generalize |
| B-N18 | naming | error-code texts for billing atoms (SANDLOOM-B017, C027 `account-route-mismatch`) | billing | `core/error_codes.py:513`, `:883-888`; `core/archcheck/runner.py:134` | verified | violation | module domain (follows B-N12) | relocate |
| B-N19 | naming | migration lint knows identity's index `external_identities.ix_external_identities_provider` | identity | `core/testing/static_checks.py:71` | verified | violation | module domain | relocate |
| B-N20 | naming | `contract_cache_shadow` documents `CONTRACT_CACHE_SHADOW` with `tenancy.membership.v1` and `tenancy.member_*.v1` as its configuration example; ADR-0003 motivates it with tenancy reads | tenancy | `core/contract_cache_shadow.py:1-21` | verified | allowed | platform guarantee (the mechanism takes any contract; the tenancy names are a docstring example, no default: `core/contract_cache_shadow.py:13-21`) | retain |

## 5. Class (c): the kernel ships module-owned code or artifacts

| id | type | source → target | owner | evidence | status | verdict | rationale | action (provisional) |
|---|---|---|---|---|---|---|---|---|
| C1 | package | generated contract stubs in the kernel package: `billing_credits_v1`, `billing_entitlements_v1`, `identity_contact_v1`, `identity_security_v1`, `tenancy_membership_v1` (import `sandloom.core.descriptors.contract_refs`) | billing, identity, tenancy | `shared/contracts/*_v1.py`; e.g. `shared/contracts/billing_credits_v1.py:4`, `:55` | verified | violation | module domain | relocate |
| C2 | package | DTO copies: `identity_dto` (`AccountStanding`, `PlatformAdmin`, `UserContact`), `tenancy_dto` (`MemberState`) | identity, tenancy | `shared/contracts/identity_dto.py:6-7`, `:24`, `:36`, `:48`; `shared/contracts/tenancy_dto.py:6`, `:22` | verified | violation | module domain | relocate |
| C3 | package | 34 event JSON schemas (billing 7, identity 15, tenancy 12) | billing, identity, tenancy | `shared/events/*.json` | verified | violation | module domain | relocate |
| C4 | package | generator and rules make `sandloom.shared` the artifact home: `shared_package: sandloom.shared`; `_shared_packages` always includes `"sandloom.shared"`; the kernel wheel ships `shared` whole | billing, identity, tenancy | `architecture_rules.yml:17`; `core/cli/generated.py:483-488`, `:576`; `packages/sandloom/pyproject.toml:129-140` | verified | **disputed** | application composition (the framework repository composes like an app with its own shared package; an app gets `<app>.shared`, section 13.2) vs module domain | investigate |
| C5 | import | `shared/membership.py`: hand-written consumer logic for `tenancy.member_*` ordering (`member_event_action`, `refreshed_state_wins`), imports tenancy's DTO copy | tenancy | `shared/membership.py:1-32` | verified (that the logic is tenancy-owned: inferred from its docstring `:1-25`) | violation | module domain | relocate |

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

Run in a throwaway worktree at `b8d8506` (`git worktree add --detach <scratch>/wt b8d8506`,
`uv sync` there); the working copy was not touched. Transcript:
`_evidence/graph-freshness-coverage.txt`.

```bash
uv run sandloom graph --format text                                     # before
uv run sandloom graph --format json --detail full --max-bytes 2000000   # before
uv run sandloom generate                                                # writes the receipt
uv run sandloom generate --check                                        # "generated files up to date (51 file(s))"
uv run sandloom graph --format text                                     # after
uv run sandloom graph --format json --detail full --max-bytes 2000000   # after
```

### 7.1 Freshness (is the index current for this source?)

- Freshness is a per-checkout receipt, `.sandloom/cache/inspection-receipt.json`
  (`core/cli/inspection/model.py:11`), git-ignored (`.gitignore:37`), compared with the
  hashes of the enumerated source (module files, `AGENTS.md`, `CLAUDE.md`,
  `pyproject.toml`, …) and the wire documents (`core/cli/inspection/read.py:493-535`).
- Fresh worktree, before generate: `missing_receipt` ("not verified on this checkout")
  for all three modules. After `sandloom generate`: `verified`.
- `sandloom generate` wrote no tracked file (`git status` empty; 51 generated files
  current) and the graph data are identical before and after: the committed index
  (`.sandloom/inspection.json`, last changed at `80d4cc7`) and every generated artifact
  were current at `b8d8506`.
- The `source_changed` state of the first pass (run in the working copy) came from that
  checkout's local receipt, written 2026-11-07 18:58 UTC, before `b8d8506`
  (2026-11-07 22:19 UTC); the working copy's committed index equals `b8d8506`'s
  (`git diff b8d8506 HEAD -- .sandloom` is empty). The stale thing was the receipt, not
  the index.

### 7.2 Coverage (what the index enumerates)

- `scope`: `authoring_composition`, modules `billing`, `identity`, `tenancy`: the
  installed `sandloom.modules` entry points (`core/cli/inspection/collect.py:242-250`).
  `sandloom_admin` is absent: the dev environment does not install it (`uv pip list`
  in the worktree; `pyproject.toml:11-14`); it is on the test `pythonpath` only
  (`pyproject.toml:185`).
- Nodes 49: module 3, contract 5, event 36, consumer 2, capability 3. Edges 51:
  `produces_event` 34, `requires_capability` 7, `provides_contract` 5, `owns_consumer` 2,
  `consumes_event` 2, `requires_contract` 1. `hidden` 0/0, no unresolved requirement,
  no cycle; 94 routes, 0 sagas.
- Every edge has `evidence_kind: registered_declaration`; `sync_coupling.basis` is
  `declared_requires`, "upper bound from declared contract requires; not measured calls".
  The graph is built from what modules declare, not from calls observed in code.
- One module relation: `billing → tenancy`, `optional_contract` (`tenancy.membership.v1`).
- The kernel appears only as the two events identity consumes,
  `core.auth.user_blocked.v1` and `core.auth.tokens_revoked.v1` (module → kernel).
- Contract nodes are the five declared contracts. The auto-registered `<owner>.claims.v1`
  (identity, tenancy, billing) and `billing.facts.v1` are not nodes, though they exist at
  run time (`_evidence/consumer-install.txt`, phase A: `billing.claims.v1`,
  `billing.facts.v1` are local providers). Occurrences in the full JSON: `claims.v1` 0,
  `facts.v1` 0.

### 7.3 What the graph cannot see

| not in the graph | rows | evidence in the JSON |
|---|---|---|
| kernel → module edges: the kernel is not a node; its contract calls by name | B-R1-B-R6, A19 | no kernel node; `live_adapter` 0 occurrences |
| string contract names in kernel code and data (live-adapter `service`, `WELL_KNOWN_FACT_OWNERS`, CLI calls) | B-R1, B-R3-B-R5 | contracts appear only as declared by their provider or consumer |
| auto-registered claims and facts contracts | A5, A6 | `claims.v1` 0, `facts.v1` 0 |
| settings, their tiers and startup validations | B-C1-B-C10 | `TWOFA` 0, `BILLING_PROVIDER` 0, `settings` 0 |
| claims authority: claim keys, `claims_sources`, which contributor writes `roles` / `tenant_ids` | B-S1-B-S3, section 14 | `tenant_ids` 0, `claims_sources` 0; `require_platform_admin` appears only as a route's `policy.risk.admin` tag (60 occurrences) |
| generated artifacts shipped in the kernel package, CLI and template naming | C1-C5, B-N1-B-N11 | not modelled |
| modules not installed in the environment that builds it | `sandloom_admin` | `sandloom_admin` 0 |

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

Run (section 13.2): every module wheel depends on plain `sandloom` (no extra), while
`sandloom_billing/tasks.py` and `sandloom_identity/tasks.py` import `sandloom.sdk.tasks`,
which imports `celery` (an optional kernel extra) at module level. Billing installed with
plain `sandloom` fails to load (`No module named 'celery'`); identity: inferred, not run.

## 9. Existing guards

| guard | what it checks (read in code) | catches | misses | run at b8d8506 |
|---|---|---|---|---|
| `tests/architecture/test_shared_purity.py` (G-KERNEL-10) | AST imports of `src/sandloom/shared/**` against `sandloom.<m>` for each subdirectory `m` of `src/sandloom` with `__init__.py`, except `core`, `shared`, `sdk` (`:20-39`); relative imports resolved (`:47-58`) | `shared` importing `sandloom.driverkit` (the only discovered "module": printed `{'driverkit'}`) | every business module: they are top-level `sandloom_<m>`, never `sandloom.<m>`; dynamic imports; `core` and `sdk`; non-import coupling | passes |
| G-KERNEL-13 `src/sandloom/core/tests/test_module_names.py:165-192` | exact string constants `identity`, `tenancy`, `billing`, `sandloom_<n>`, `sandloom.<n>` in `core/config/*.py`, `core/app_factory/*.py`, `core/loader/plugin_loader.py`, `main.py`; `sandloom_modules`, `module_required` defaults are `[]` | a default module list in settings, app factory, loader or `main` | `admin` / `sandloom_admin`; any substring (`"billing_provider"`, `"identity.security.v1"`, `"sandloom_billing.psp…"`); every other directory (`cli`, `shared`, `sdk`, `testing`, `admin`, `archcheck`, …). Lines that pass it inside its own scope: B-C1, B-C3, B-C4, B-C5, B-C6 | passes (called directly) |
| `.importlinter` + `tests/architecture/test_kernel_import_contracts.py` | 7 contracts: core layers, acyclic siblings, config leaf, sdk → public kernel surface, kernel never imports sdk/driverkit, `_decl` L1, sdk layers | import direction inside `sandloom` | anything outside `sandloom`: `root_package = sandloom`, no `include_external_packages`, so `sandloom_*` are not in the graph; no contract names `sandloom_*`; dynamic imports by computed name; string coupling | `lint-imports`: 7 kept, 0 broken (484 files, 1739 dependencies) |
| `tools/packaging/distribution_imports.py` (via `check_package_distributions.py` step 2, `:252-262`; unit test `tests/architecture/test_distribution_imports.py`) | every runtime import in each wheel's `.py` files, module-level and function-local, `TYPE_CHECKING` skipped (`:136-173`); a first-party import must resolve to a wheel in the importer's dependency closure (`:274-283`) | a kernel import of a module wheel **only when the kernel wheel does not depend on it**; dynamic `importlib.import_module("literal")` / `__import__("literal")` and any call whose name ends in `import`/`import_module` with a string literal (`:118-133`) | **extras rule**: `read_wheel` collects every `Requires-Dist` regardless of extra marker (`:68-71`) and `_closure` follows them all (`:202-204`), so `sandloom` may import `sandloom_identity/_tenancy/_billing/_admin` through `standard`/`admin`; a dynamic import whose argument is a name or f-string (`password_check.py:42` passes a variable); string coupling. The `try/except ImportError` exemption applies to third-party imports only (`:274-285`): a guarded first-party import is checked | probe (synthetic wheels, `check()`): static import with extra declared → passes; without extra → flagged; guarded `try/except ImportError` without extra → flagged; `import_module("literal")` without extra → flagged; `import_module(NAME)` and `import_module(f"…")` → pass |
| `tools/packaging/check_package_distributions.py` lean install (`check_lean_install`, `:307-379`) | clean venv with the `sandloom` wheel alone; fails if `redis`, `celery`, `sentry-sdk`, `sandloom-identity` got installed (`:324-328`); every subcommand `--help`, `sandloom docs`, `sandloom new`, battery handoff, inspection smoke, RPC transport smoke, and `create_app()` + `import sandloom.main` with `SANDLOOM_MODULES=[]` and no loaded modules (`:399-413`) | a module-level kernel import of a module on the paths those commands import; a kernel that cannot build an app with no modules | code paths not executed (a request through `require_platform_admin`, `sandloom admin create` beyond `--help`, live atoms); string and contract coupling (B-R*, B-S*, B-C*); leak check lists only `sandloom-identity` among module wheels, not tenancy, billing, admin | run at `b8d8506` (throwaway worktree): passes, with its standard and admin installs; it does not install a module wheel without the kernel's `redis,celery` extras (section 13.2) |
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
- ~~`check_package_distributions.py` lean and standard installs at `b8d8506`: read, not
  run.~~ Run (section 13).
- ADR-0009 and ADR-0010 line references in the inventory.
- `sandloom-identity` installed without the kernel's `celery` extra: not run; that it
  fails like billing is inferred (`sandloom_identity/tasks.py` imports
  `sandloom.sdk.tasks`, which imports `celery` at module level, `sdk/_prim/tasks.py:16`).
- A real tenancy provider over RPC (its own database): not run; phase B of section 13.2
  used a stand-in provider class. A real login through identity with an extra claims
  contributor: not run; section 14's probe composes the real merge, mint and gate by hand.
- The order in which a composed app registers claims contributors (which decides the
  last writer of a key, section 14): read for `sandloom.main` (sorted entry-point names,
  `main.py:27-35`) and the app template (`BUNDLED` then `app_modules(...)`), not run with
  a contributor registered after identity.
- Whether the `methods: []` recorded for a consumer that calls only through
  `Contract(REF, optional=True).client()` (section 13.2) is intended: inferred from the
  `Contract` docstring (methods from direct `x.<method>(` calls), not traced in the
  generator.

## 13. Installs run from wheels built at `b8d8506`

Wheels: built by `tools/packaging/check_package_distributions.py --dist-dir` in a throwaway
worktree at `b8d8506` (sdist round trip, license and layering checks included); sha256 in
`_evidence/package-distributions-check.txt`. Every venv below is fresh and outside the
repository; no database, Redis or broker was contacted (dummy URLs,
`EVENT_BUS_BACKEND=memory`).

### 13.1 Lean install (`sandloom` alone)

Transcripts: `_evidence/package-distributions-check.txt` (the repository's own check),
`_evidence/lean-install.txt` (the same steps by hand, plus the module-named paths).

| step | result |
|---|---|
| `check_package_distributions.py` (lean + standard + admin, layering) | **passed**: "Import layering OK across 5 distributions"; "lean sandloom OK: CLI, kernel factory, empty standard root"; "standard set OK: ['sandloom_billing', 'sandloom_identity', 'sandloom_tenancy']"; "admin console OK: ['sandloom_admin', 'sandloom_identity']"; wheel RPC transport smoke OK; "All package distribution checks passed." |
| installed distributions | `sandloom` only (no module wheel, no redis, celery, sentry) |
| `import sandloom, sandloom.sdk` | passes, from site-packages |
| `sandloom --help` | passes (25 subcommands); each subcommand `--help` passes (the check) |
| `sandloom docs define_module`, `docs sandloom.sdk.web` | pass |
| `sandloom new probe` | passes; the app depends on `sandloom[redis,celery]>=0.7.0a11,<0.8` |
| `create_app()` | passes: FastAPI, 20 routes |
| `import sandloom.main` | passes: `default_modules() == []`, `loaded_modules == []` |
| module-owned artifacts in the lean wheel | present: 5 stubs + 2 DTO modules (`billing_*`, `identity_*`, `tenancy_*`), 34 event schemas; `from sandloom.shared.contracts.tenancy_dto import MemberState` works with no tenancy installed (C1-C3) |
| B-I1 with identity absent | `rules()` → `Rules(min_length=8, max_length=100, check=None)` (the fallback) |
| B-R1 with billing and tenancy absent | the live-adapter defaults still name `billing.entitlements.v1`, `tenancy.membership.v1` |

Not run in the lean venv: `sandloom admin create` beyond `--help`, a request through
`require_platform_admin`, live atoms against a provider.

### 13.2 Consumer install (`sandloom` + `sandloom-billing`, tenancy absent)

Transcript: `_evidence/consumer-install.txt`. Billing is the consumer: it calls
`tenancy.membership.v1` (optional) and imports tenancy's DTO copy from the kernel wheel
(`sandloom_billing/services/tenant_resolver.py:56`).

| step | result |
|---|---|
| install the two wheels as published (billing depends on `sandloom>=0.7.0a11,<0.8`, no extra: `packages/sandloom-billing/pyproject.toml`) | installs |
| bootstrap (`import sandloom.main`) | **fails**: `ModuleLoadError: Required modules failed: sandloom_billing: No module named 'celery'`; `sandloom_billing/tasks.py:25` imports `sandloom.sdk.tasks`, which imports `celery` at module level (`sdk/_prim/tasks.py:16`); `celery` is the kernel's optional extra |
| install `sandloom[redis,celery]` + billing (what the `standard` extra and the `sandloom new` template depend on) | installs |
| bootstrap, contract registration (phase A) | passes: `loaded_modules ['sandloom_billing']`; local providers `billing.entitlements.v1`, `billing.credits.v1`, `billing.facts.v1`, `billing.claims.v1`; `tenancy.membership.v1` not local; warning "consumes optional contract 'tenancy.membership.v1', which is neither provided locally nor bound in SERVICE_BINDINGS"; `Contract(TENANCY_MEMBERSHIP_V1, optional=True).client()` → `None` |
| RPC call (phase B): a stand-in class registered as `tenancy.membership.v1` on a bare `CoreFacade`, served by the kernel's RPC routes (pair-MAC keys), bound in the billing process through `SERVICE_BINDINGS` | passes: `client()` is a typed proxy; `get_member_state` returns `sandloom.shared.contracts.tenancy_dto.MemberState` (the kernel wheel's copy); `has_role_at_least` admin → True, owner → False; a provider answer missing `organization_id`/`user_id` → `ValidationError` from the copy |
| DTO compatibility | the kernel wheel's `MemberState` and the provider class in the tenancy wheel (unpacked aside, not installed) have the same fields and JSON schema (descriptions aside), both frozen |
| generation in an app (`sandloom new shop`; module `orders` with `Contract(BILLING_ENTITLEMENTS_V1)` and `Contract(TENANCY_MEMBERSHIP_V1, optional=True)`, stubs imported from `sandloom.shared.contracts`) | composition root as generated (`BUNDLED: list[str] = []`): `sandloom generate` **fails** `ContractBindingError: … shop.modules.orders consumes 'billing.entitlements.v1'` (an installed module is not composed by the app root; `sandloom.main` composes installed entry points, the app root does not). With `BUNDLED = ["sandloom_billing"]`: passes; writes billing's two stubs into `shop/shared/contracts/` and billing's 7 event schemas into `shop/shared/events/`; no tenancy stub (provider absent); `orders` snapshot `consumes` = `billing.entitlements.v1` (required, `methods: ["check_subscription_active"]`), `tenancy.membership.v1` (optional, `methods: []`); `SANDLOOM-T015` warning; `generate --check` up to date |
| two copies of billing's stubs | the app's copies and the kernel wheel's differ only in the `ContractRef` import line (`sandloom.sdk.contracts` vs `sandloom.core.descriptors.contract_refs`) |
| bootstrap of the app (`import shop.asgi`) | passes: `['sandloom_billing', 'shop.modules.example', 'shop.modules.orders']`, tenancy absent |

## 14. Security authority model (facts)

Read at `b8d8506`; the merge, mint and gate were also run together from the lean wheel
(`_evidence/claims-authority-probe.txt`: the real `CoreFacade._merge_claim_outcomes`,
identity's mint dict copied from `token_service.py:144-152`, `create_access_token`,
`require_platform_admin`; no database, no login).

### 14.1 Who sets and who reads each reserved claim

| claim | set by | read by | does the reader check the producer? |
|---|---|---|---|
| `roles`, `permissions` | identity's contributor `contribute_identity_claims` (`sandloom_identity/claims.py:61-69`, `:78`), registered `critical=True` (`sandloom_identity/__init__.py:57`) | `require_platform_admin` (`shared/guards/platform.py:14-18`), `platform_operator` (`core/admin/api/platform.py:63-71`), `Role` atom (`shared/authz/atoms_claims.py:54-67`), `Perm` atom (`:35-48`), `UserContext.roles` / `.permissions` (`shared/subject.py:73-75`) | the two platform gates check that `identity` ran (`claims_sources`), not who wrote `roles`; `Role` and `Perm` check nothing |
| `tenant_ids` | tenancy's contributor `tenancy_claims` (`sandloom_tenancy/claims.py:20-34`), registered `critical=True` (`sandloom_tenancy/__init__.py:71`); bounded by `TENANT_CLAIMS_MAX_ORGANIZATIONS` (B-C2) | `TenantScope` (`core/http/tenant_scope.py:159-173`, with claim keys `tenant_id`, `org_id` at `:28`), `subject_tenant_allowlist` (`shared/authz/_resolve.py:61-75`, also `tenant_id`, `org_id`) | no: neither reads `claims_sources` |
| `claims_sources`, `claims_sources_failed` | the kernel's merge, after every contributor: owners that ran / failed (`core/facade/_claims.py:452-453`); a contributor's own value for these keys is overwritten (probe step 2: `["forged"]` → `["identity", "inventory"]`) | the two platform gates; claim-mode access facts (`shared/authz/facts.py:296-307`) | — |
| owner name stamped | `owner=` of `CoreFacade.add_claims_contributor`, else the registering module's short name (`_claims.py:225`); through `define_module` only the short name (`core/module_api/_registration.py:406-419`); remote owners are named by `SERVICE_BINDINGS` keys `<owner>.claims.v1` (`_claims.py:259-276`) | — | an app-local module whose last segment is `identity` is owner `identity` unless an installed package provides that name (SANDLOOM-B020 refuses only that collision: `core/descriptors/module_names.py:136-164`); probe step 1 |
| `sub`, `email`, `email_verified`, `token_type`, `auth_time` | identity's mint, written after the merged claims (`sandloom_identity/services/token_service.py:144-152`; `auth_time` only when set) | bearer decoding, `platform_operator` (`sub` must be a UUID) | — |
| `exp` | `create_access_token`, always overwritten (`core/auth/tokens.py:67`) | JWT verification | — |
| `iat`, `nbf`, `jti`, `iss`, `aud`, `iat_us` | `create_access_token` with `setdefault` (`tokens.py:81-87`) and `iat_us` only when absent (`:68`): a value already in the claims is kept | revocation compares `iat_us` with the subject's mark (`tokens.py:41-46`); `platform_operator` requires `iat` | — |

### 14.2 What any claims contributor can write today

- The merge applies every successful contributor's dict with `claims.update(value)`
  (`core/facade/_claims.py:442`) in plan order: local owners in registration order, then
  remote owners (`:325-365`). Any key, last writer wins; no reserved-key filter. Only
  `claims_sources` / `claims_sources_failed` (kernel), identity's five keys and `exp` are
  overwritten afterwards; `iat`, `nbf`, `jti`, `iss`, `aud`, `iat_us` from a contributor
  survive into the token (probe step 3: `jti: 'chosen'`, `iat_us: 1` kept).
- Probe step 2-4: a non-critical contributor of owner `inventory`, after identity,
  returning `roles: ["admin"]` and a `tenant_ids` entry yields a token whose `roles` is
  `["admin"]`, whose `tenant_ids` holds that entry, and which `require_platform_admin`
  allows (`identity` is in `claims_sources` because identity's contributor ran).

### 14.3 How the platform-admin gate decides

- `require_platform_admin` (public in `sdk.web`): allow iff `"admin" in roles` and
  `"identity" in claims_sources` and `"identity" not in claims_sources_failed`
  (`shared/guards/platform.py:14-18`).
- `platform_operator` (every `/api/v1/platform/<name>` router): a bearer access token
  with `iat`, `sub` a UUID, not revoked (`ensure_token_not_revoked`), `claims_sources`
  and `roles` lists, the same three conditions (`core/admin/api/platform.py:54-71`).
- Neither gate checks which owner wrote `roles`. Identity's own definition of a platform
  admin is a `UserRole` row whose `Role.code == "admin"` (`sandloom_identity/contracts.py:186-196`,
  `ADMIN_ROLE_CODE` at `sandloom_identity/services/role_service.py:39`); the token's
  `roles` claim is the mint-time copy of it, unless a later contributor overwrote it.
