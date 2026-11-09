# Alternatives D: module-owned configuration

tm `kb-alt-settings` (parent `kb-assess`, decision D of [requirements.md](../requirements.md) §2).
Question: how the settings, validators, accepted risks, removed-setting entries, backup notes,
console settings list and deploy shared keys that the kernel holds for identity, tenancy,
billing and admin move to those modules, while tiers, the ratchet, doctor, diagnostics, secret
redaction, the `.env` / `SECRETS_DIR` sources and the refusal at every entry point keep working.
Also: how password hashing splits into a platform crypto mechanism and identity policy.

Written blind: this file does not use `roadmap-review.md`, the roadmap inputs, the
target-architecture draft or the reconciled review. Inputs: [goals.md](../goals.md),
[requirements.md](../requirements.md), [owner-answers.md](../owner-answers.md),
[baseline.md](../baseline.md), [guarantee-map.md](../guarantee-map.md),
[research-settings.md](research-settings.md) (cited as "research"), and the code.

Code is cited at `f40cb7f` (`arch/kernel-boundary`); `git diff --stat 44451e4 f40cb7f -- src`
is empty, so requirements' `44451e4` citations hold. Paths are relative to `src/` unless they
start with a top-level directory. Open PR Sandloom-dev/sandloom#71 (`8d3413a`, branch
`claude/module-prod-checks-everywhere-139f61`, not merged here) is cited as "PR #71" and treated
as step 0 of every alternative (§4).

This file compares alternatives. It decides nothing: the ADR for decision D decides, after the
PoC of §11.

## 1. Summary

| | alternative | in one line |
|---|---|---|
| D1 | **module-local pydantic** (minimal; no framework over pydantic) | each module builds its own `BaseSettings` in `setup` and refuses with PR #71's channel; the kernel only deletes the fields it held |
| D2 | **declared model, validated from the kernel's one read at registration, one post-load check** (research O2, the owner-accepted basis) | the module hands its model to `define_module(settings=…)`; the loader validates it from the kernel's per-process source snapshot before `setup`; one check after the load pass covers collisions, risk ids, removed settings, production checks and overrides; kernel tools read the declarations. Two authoring forms: **D2a** pydantic-native (plain `BaseModel`, existing kernel dataclasses as class attributes, production checks as pydantic validators reading the validation context) and **D2b** a `ModuleSettings` base with `setting()`, `@prod_check` and `.current()` |
| D3 | **static metadata, validated before loading** (research O3) | each module publishes its settings declaration as entry-point metadata or a generated JSON file; the kernel reads and validates the selected modules' declarations before importing them |
| D4 | **one composed `Settings`** (research O4) | a bootstrap subset is read first, the modules are imported, the kernel builds one dynamic `Settings` class from kernel and module mixins and validates once |

**What the evidence favours: D2, in the D2a form**, as the minimal sufficient alternative. D1 has
the fewest mechanisms but fails 8 of the 16 decision-D scenarios, passes 6 only by module discipline, and leaves I-18 violated, so
H-1 disqualifies it. D3 and D4 pass the scenarios but each adds mechanisms that remove no
violation D2 leaves (H-12). D2b runs the same mechanisms as D2a with three to four more SDK names;
whether those names pay for themselves is a DX question for the PoC (§11, P12), not an
architecture one.

**Password hashing: the evidence favours H1** (§7): the hashing mechanism, its work factor
(`PASSWORD_HASH_*`), the OWASP floor and the risk `weak_password_hash` stay in the kernel as a
platform crypto capability. The identity policy (password rules, the login timing equaliser,
rehash on login, re-authentication) is already in identity; the one piece of identity policy the
kernel still holds is the CLI's import of identity's password rules (B-I1), which belongs to
decision A. H2 (work factor to identity, floor kept in the kernel mechanism) is the fallback if
the owner rules that the work factor is identity policy.

## 2. What moves, what stays

### 2.1 Settings

Counted in code (`python -c "len(ModulesConfig.model_fields)"` → 21). The baseline (B-C1) and the
task text say 22, but B-C1 lists 21 names. With `TENANT_CLAIMS_MAX_ORGANIZATIONS` the kernel holds
**22** module-owned env names today, not 23 (owner answer #30 and research §7.5 inherit the
off-by-one; see open point O-1). Every name is kept byte-identical in every alternative (#30).

| owner | settings | today | readers outside tests | verdict |
|---|---|---|---|---|
| identity (11) | `TWOFA_ENCRYPTION_KEY`; `GOOGLE`, `GITHUB`, `MICROSOFT`, `LINKEDIN` × `_CLIENT_ID`/`_CLIENT_SECRET` (8); `TELEGRAM_BOT_TOKEN`; `SSO_ALLOWED_REDIRECT_URIS` | `core/config/modules.py:17-189` (B-C1) | identity only | move (B-C1) |
| billing (8) | `BILLING_PROVIDER`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `BILLING_DENY_ON_PAST_DUE`, `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS`, `BILLING_INTENT_RECONCILE_AFTER_MINUTES`, `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS`, `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES` | `modules.py:46-131` (B-C1) | billing; kernel: `platform.py:608` (`billing_provider`), `deploy_profile.py:87` (`BILLING_DENY_ON_PAST_DUE`) | move (B-C1, B-C7, B-C9) |
| admin (2) | `ADMIN_CONSOLE_ENABLED`, `ADMIN_CONSOLE_DIST` | `modules.py:28-44` (B-C1) | `sandloom_admin/api.py:32-50`; kernel: `core/cli/admin.py:129-131` (B-C10) | move; the CLI read is decision A (§3.3) |
| tenancy (1) | `TENANT_CLAIMS_MAX_ORGANIZATIONS` | `core/config/auth.py:88-95` (B-C2) | `sandloom_tenancy/claims.py:25`, `contracts.py:133` | move (B-C2) |
| identity, candidate (2) | `REFRESH_TOKEN_EXPIRE_DAYS`, `MAX_SESSION_DAYS` | `auth.py:70-86` | `sandloom_identity/services/token_service.py:165`, `:167`, `:247`, `:424`; kernel: `platform.py:603` (console list), `deploy_profile.py` `SHARED_KEYS` (`REFRESH_TOKEN_EXPIRE_DAYS`) | **not a baseline row.** Refresh sessions are the issuer's (answer #16), and no kernel code reads them except the console list and the shared keys. Move only if the owner agrees (O-2); the D2 mechanisms carry them at no extra cost |
| kernel | `JWT_*`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `AUTH_REVOCATION_*`, `TENANCY_MODE`, `SINGLE_TENANT_ID`, `FRONTEND_URL`, `NOTIFIER_LOG_LINKS` (+ risk `log_links_in_prod`) | kernel config | kernel readers (`core/auth/tokens.py`, `marks.py:170`, `core/rpc/*`, `core/http/tenant_scope.py`) | **stay**: signing, verification, revocation and tenant isolation are the general protocol of I-21 (H-14). `JWT_SECRET` is issuer-only in practice but the kernel signs with it (`core/auth/tokens.py:114-266`) |
| kernel | `PASSWORD_HASH_*` (5) | `auth.py:234-292` | `core/auth/hashing.py`, `core/config/settings.py:557-590` | §7 decides; stays under H1 |

### 2.2 Everything else that names a module setting

| item | today | where it goes (D2) | invariant |
|---|---|---|---|
| TOTP key strength in prod (B-C3) | inside `_jwt_secret_ok`, `core/config/settings.py:497-505` | identity production check, with presence (PR #71 already moved presence to registration) | I-02, I-18 |
| `_billing_requirements` (B-C4) | `settings.py:623-660` (explicit `fake` refused; Stripe keys required) | billing: Stripe keys as a model validator (every env); the fake-PSP refusal as a production check. One message replaces two (`settings.py:641-645` and `sandloom_billing/psp/__init__.py:61-66`) | I-02, I-18 |
| risk `fake_psp_in_prod` (B-C5) | closed `RISKS`, `core/config/accepted_risks.py:56-62`, `:145` | billing's declaration, id `billing.fake_psp_in_prod` (#28) | I-13, I-18, T10 |
| removed `BILLING_FAKE_ALLOW_UNSIGNED_WEBHOOKS`, `BILLING_ALLOW_FAKE_PSP_IN_PROD` (B-C6) | `core/config/tiers.py:396-406` | billing's declaration; refused with today's message where billing is loaded (D-B4) | I-18 |
| semantic `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` (B-C6) | `tiers.py:332-338`; counted in `SEMANTIC_BASELINE`, `core/tests/test_config_tiers.py:51-71` | billing's declaration and billing's own ratchet; kernel `SEMANTIC_BASELINE_COUNT` 13 → 12, `SEMANTIC_COUNT_CEILING` 15 → 14 (#7, #26) | I-18, D-B3 |
| ops entries of the 22 names (B-C6) | `tiers.py:620`, `:659-678` | undeclared tier of a module field = ops; listed per module | I-18 |
| backup plan text (B-C8) | `core/migrations/backup.py:244-262` names `TWOFA_ENCRYPTION_KEY`, `STRIPE_*`, "operator reset-2fa" | each loaded module's backup note on its secret fields; the kernel item keeps JWT, RPC, `INTERNAL_API_TOKEN` | I-02 |
| console settings list (B-C9) | `_SAFE_SETTINGS`, `core/admin/api/platform.py:593-613` (`billing_provider`; also `refresh_token_expire_days`, `password_hash_profile`) | kernel names stay; module fields marked console-visible are appended under the same key (D-B5) | I-03, D-B5 |
| deploy shared keys and dev env (B-C7) | `SHARED_KEYS`, `core/testing/deploy_profile.py:66-95`; dev env `:928-942` writes `TWOFA_ENCRYPTION_KEY`, `BILLING_PROVIDER=fake`, the risk | T014 unions the shared fields of the modules each service loads (it already loads their topology, `deploy_profile.py:356-358`); the dev env asks each module for dev values of its required-in-prod fields | D-B2, G-OPS-14 |
| scaffold env text (B-N6, B-N8) | `core/cli/scaffold_deploy.py:87-103` (`MODULE_ENV`), `core/cli/project.py:970-982` | generated from the declarations of the scaffolded modules (`${VAR:?…}` for required-in-prod fields). The starter is after A-D (requirements §1); D only makes the data available | I-03 |
| docs reference and tier test | `tools/docs/gen_config_reference.py:36`, `:218`; `test_config_tiers.py:92-107` iterate `Settings.model_fields` | kernel table plus one section per documented module; a `sandloom.core.testing` helper each module's tests run over its declaration | I-18 |
| set-password link target (B-C10) | `core/cli/admin.py:124-162` reads `admin_console_enabled` | not a settings question: decision A (a link-target provider the admin module registers; tm decision on the admin battery, item 4) | I-03 |

### 2.3 Findings while reading

- **F-a. The log redactor misses `TWOFA_ENCRYPTION_KEY`.** Two name heuristics disagree:
  `is_secret_name` (`core/cli/config.py`) marks it secret, `is_sensitive_key`
  (`core/observability/guardrails.py:27-46`, substring list without "key") does not
  [verified: `uv run python -c` on both functions]. A dict or `extra=` field named
  `twofa_encryption_key` is logged in clear. A declared secret flag (D2, D3, D4) can feed both
  paths; D-H2 only names `STRIPE_SECRET_KEY`, which both catch.
- **F-b. identity imports `argon2` without declaring it** (`sandloom_identity/services/auth_service.py:13`;
  `packages/sandloom-identity/pyproject.toml` lists no `argon2-cffi`; it comes through
  `sandloom`'s own dependency, `packages/sandloom/pyproject.toml:45`). It works in every install
  profile because `sandloom` is a dependency; the packaging check ignores third-party imports
  (baseline §427). Relevant to H3 (§7).
- **F-c. An optional module cannot swallow a configuration refusal.** `ConfigurationError`
  subclasses `SystemExit` (PR #71, `core/config/common.py:108`), and the loader's per-module
  handler catches `Exception` only (`core/loader/plugin_loader.py:555-558`). Any alternative that
  raises a `ConfigurationError` subclass for a module settings error gets D-F5 (#29) without a
  loader change.

## 3. Fixed points every alternative must keep

### 3.1 From the owner's answers

| rule | source | check |
|---|---|---|
| a module's settings are validated before its `setup` takes effect; cross-module checks after loading; no separate two-phase bootstrap | preamble, research §5, RA10 | D-B6 |
| production refusals reached with the same reason by API, worker, beat, migrations and operational commands; static CLIs not forced to build the app | #B | D-H3, D-F1 |
| variables of a module absent from the process are neither validated nor shown and do not interfere; `sandloom check`/`test` still report wrong values and wrong active module names for the chosen topology | #25 | D-B1 |
| kernel ratchet 15 today, recounted down; per-module semantic ratchets; the kernel checks that every setting is classified | #7, #26 | D-B3 |
| risk ids namespaced (`billing.fake_psp_in_prod`), one-time env migration, no permanent alias; an absent module's id inert only for a namespace known in the deployment; unknown prefix refused; unknown id of an active module refused | #27, #28 | D-F4 |
| a settings error of an explicitly selected optional module is fatal | #29 | D-F5 |
| keep the existing env names; forbid collisions; new modules get prefixed names, checked by `sandloom check` | #30 | D-F2 |
| `config dump` may be slower if measured and it does no activation, lifespan or module I/O; `--kernel-only` keeps today's output; redaction, provenance, diagnostics stay | #31 | D-H5 |
| one source read per process; no environment read at module import | research F6; G-SDK-01 | D-F3, P7 |

### 3.2 Entry points

From research §1.2. "Builds the composition" means it reaches `create_app` steps 0-7 or
`load_composition_core`.

| class | entry points | builds the composition | what decision D must give it |
|---|---|---|---|
| composed | API, Celery worker and beat, `sandloom migrate …`, configured operator CLIs (`projection outbox streams contracts routes backup security sagas`), `doctor` (after its config section), `check --boot`, `generate`, tests | yes | the same refusal as the API, in step 6-7 |
| `config dump` | `core/cli/config.py:160-245` iterates `type(cfg).model_fields` | no (research F2) | module fields listed with provenance and redaction; `--kernel-only` = today (#31) |
| deploy-profile tooling | `sandloom add deploy`, T014 via `load_module_topology` (`core/testing/deploy_profile.py:356-358`, `core/testing/boot.py:126-145`) | no; runs `register`/`setup` on a bare facade with the developer's `.env.test` | the shared fields and dev values of each service's modules; validation there must not run production checks (they are no-ops outside prod) |
| admin CLI | `core/cli/admin.py`: reads `settings.is_prod`, `frontend_url`, `admin_console_enabled` before or without the app (`:99`, `:118-131`); `create`/`reset` build it (`:83`) | partly | no kernel read of an admin field (decision A provider for B-C10) |
| static | `new add check explain docs context graph keys test` | never (`sandloom/__init__.py:74-82`) | nothing; `check`/`test` import the app's modules for the static gate (D-B1, D-F2) |

### 3.3 Out of decision D, touched by it

- B-C10, B-N3 (admin console link target): decision A. D only stops the kernel from holding
  `ADMIN_CONSOLE_ENABLED`; until A lands, `core/cli/admin.py` must read it through whatever the
  admin module exposes, which is still a named edge.
- B-I1 (`core/cli/password_check.py` imports `sandloom_identity.password_policy`): decision A /
  the identity CLI move (tm decision on the admin battery, item 3).
- Condition-4 defects (D1 revocation applier, D4 compat check, billing without celery,
  `/ready` with an unusable taskqueue): no alternative here fixes or worsens them. They stay on
  the list.

## 4. Step 0, common to every alternative: PR #71

PR #71 (`8d3413a`) moves identity's TOTP-key refusal and billing's implicit-fake-PSP refusal
from critical lifespan tasks to registration: the module raises `SystemExit(reason)` from
`_setup`, and `_invoke_register` turns it into
`ConfigurationError("[core.config] Invalid configuration: module <name>: <reason>")`
(`plugin_loader.py` `_configuration_refusal`). Its G-CONFIG-07 harness covers app, worker, beat,
`migrate databases` and five composed CLIs.

What it gives every alternative: research F1 is closed for the two refusals; a refusal channel
from `setup` exists and is not skippable for optional modules (F-c); compose files already pass
module env to the migrate one-shot.

What it leaves: the fields are still kernel fields (B-C1..B-C9); the explicit-`fake` kernel
validator (B-C4) still runs in processes without billing; identity's `JWT_SECRET` refusal is still
lifespan-only (PR #71 "Not in scope"); `config dump` still loads no module; risk ids are flat and
closed. Each alternative below starts from PR #71 merged. If PR #71 is not merged, D2's production
checks replace it entirely (same entry-point effect).

## 5. Alternatives

### 5.1 D1: module-local pydantic (minimal; no framework over pydantic)

**Shape.** The kernel deletes `ModulesConfig` and `tenant_claims_max_organizations`, B-C3 and B-C4.
Each module writes `class BillingSettings(BaseSettings)` with the kernel's source configuration
copied (`env_file=".env"`, `case_sensitive=False`, the `SECRETS_DIR` hook of
`settings.py:113-143`), builds it in `setup` (never at import), keeps the instance on a module
global, and raises `SystemExit` on a production problem (PR #71). A pydantic `ValidationError` in
`setup` must also be turned into `SystemExit` by the module, or it becomes a `ModuleLoadError`.

**Where things go.** Settings: the module. Validators: module code in `setup`. Accepted risk:
nowhere new. The kernel registry stays closed, so either `fake_psp_in_prod` stays in the kernel
(B-C5 stays a violation, no namespacing) or the kernel accepts any id with a prefix (a new rule,
and typos pass). Removed settings: each module hand-codes its scan. Tiers, backup text, console
list, shared keys: the kernel loses sight of module fields; T014 can keep
`BILLING_DENY_ON_PAST_DUE` only by naming it (B-C7 stays).

**New mechanisms.** None beyond PR #71.

**Scenarios.** Passes D-H4 and D-F1; D-F3, D-H3 and D-B6 pass only by module discipline (a module
that forgets to convert `ValidationError` gives a different code). Fails D-H1 (tier test and dump
blind), D-H2 (dump does not list the secret at all), D-H5, D-B1's static part, D-B2, D-B3 (a
module's semantic setting is unclassified), D-B5 (the console list loses `billing_provider` in
mono), D-F2 (pydantic does not detect one name read by two models, research F5), D-F4, D-F5 (by
convention only). D-B4 partially (per-module copies of the scan).

**Cost.** Every module re-reads its sources: ~0.4 ms per module env-only, ~17 ms per module with
`.env` and `SECRETS_DIR`, `.env` parsed N+1 times (research F6). Reader churn as D2.

**Verdict.** Fewest mechanisms, but I-18 stays violated for module settings (tiers, risk, dump,
shared keys) and I-13 for risks. H-1 disqualifies it. It is useful as the measure of what D2's
mechanisms buy.

### 5.2 D2: declared model, validated from one read at registration; one post-load check

**Shape.** Research O2 (§6-§7), adjusted to PR #71 and the owner's answers.

1. **One read per process.** `_load_into` (`settings.py:765-782`) first builds a frozen,
   case-insensitive source snapshot with pydantic-settings' own sources and precedence (env >
   `.env` > `SECRETS_DIR`), keeping each key's origin. Kernel `Settings` reads through a source
   over the snapshot; `SECRETS_DIR` files are looked up only for declared aliases (kernel plus
   loaded modules), never listed.
2. **Declaration on registration.** `define_module(…, settings=BillingSettings)` (or
   `api.settings(BillingSettings)` in a hand-written `register`). The loader, inside the module's
   registration draft and before `setup`, validates the model from the snapshot with a validation
   context (`environment`, the accepted-risk set, a read-only view of kernel `Settings`), stores
   the instance on the facade, then runs `setup`. A failure raises `ModuleSettingsError`, a
   `ConfigurationError` subclass, so it is fatal for an optional module too (F-c, #29).
3. **One post-load check**, right after `load_modules_with_summary`
   (`core/app_factory/__init__.py:192`), the same function in every entry point that builds the
   composition:
   - one env name declared twice across the kernel and the loaded modules (D-F2, a new B-code);
   - accepted-risk ids by the namespace rule of §6.1;
   - each loaded module's removed settings against the snapshot (D-B4);
   - production checks that need more than one module (none today);
   - the prod typo check (`settings.py:208-235`) over the union of kernel and loaded names;
   - override logging and the `sandloom_accepted_risk_active` metric for module semantic
     settings and risks.
4. **Kernel tools read declarations** of loaded modules: tiers and `/diagnostics/core`
   (`tiers.diagnostics`), `config dump`, doctor, the backup plan, the console settings list, T014
   shared keys and the dev env, the docs generator.
5. **`config dump`** builds the composition with `load_composition_core` (no lifespan) in a
   collect mode that reports a module's settings error as a diagnosed entry instead of exiting;
   `--kernel-only` keeps today's path. Provenance reads the snapshot's per-key origin, which
   removes today's second read (`cli/config.py:136-157`).
6. **Static gate** in `sandloom check` / `sandloom test`: import every module of the app (import
   is side-effect free, G-SDK-01), compare declared names (collisions also across modules not
   loaded together), check the prefix rule for new names (§6.4), validate the declarations of the
   modules each service of the chosen topology loads against that service's env (D-B1), and run
   each module's ratchet helper.
7. **Reads** go through the active core: `settings_of(BillingSettings)` (D2a) or
   `BillingSettings.current()` (D2b). A read with no app bound raises, so a module-level read fails
   the import audit (D-F3). Tests use a `sandloom.sdk.testing` override in place of `setattr` on
   the kernel singleton.

**Two authoring forms.**

| | D2a pydantic-native (minimal sufficient) | D2b `ModuleSettings` helpers (research §7.1) |
|---|---|---|
| model | plain `pydantic.BaseModel`, fields `Field(alias="BILLING_PROVIDER")` | `class BillingSettings(ModuleSettings)`, fields `setting(default, env=…, console=…, shared=…, backup=…, tier=…)` |
| secret | `SecretStr` type (readers call `.get_secret_value()`), the name heuristic as backstop | `SecretStr` or `secret=True` |
| tier, risk, removed, shared, console, backup | class attributes holding the kernel's existing dataclasses: `semantic = (SemanticSetting(…),)`, `risks = (Risk(…),)`, `removed = (RemovedSetting(…),)`, `shared = frozenset({...})`, `console = (...)`, `backup = {...}` | per-field keyword arguments plus `risks`, `removed` |
| production check | `@model_validator(mode="after")` reading `info.context["environment"]` and `info.context["accepts_risk"]`; raises, so the first problem per module refuses | `@prod_check` returns a problem string or `None`; the loader collects all, so doctor lists every problem without refusing (as `configuration_problems` does, `core/app_factory/config.py:316`) |
| read | `settings_of(BillingSettings)` | `BillingSettings.current()` |
| new SDK names (advanced tier, I-17 review) | `settings_of`, `SemanticSetting`, `RemovedSetting`, `Risk` (the last three exist in the kernel, newly exported), a testing override: **5** | `ModuleSettings`, `setting`, `prod_check`, `Risk`, `RemovedSetting`, a testing override (`current` is a method): **6**, with more concepts per name |

Runtime mechanisms are the same in both forms. D2a keeps "no framework over pydantic" for the
author: everything in the class is pydantic or a kernel dataclass that already exists. Its weak
points: a semantic setting's metadata sits apart from its field, and doctor sees one production
problem per module unless it validates each module separately (it can, since each model
validates on its own).

**New mechanisms** (H-12; §4.2 definition).

| # | mechanism | kind (§4.2) | replaces | removes / provides | compared against |
|---|---|---|---|---|---|
| M1 | settings declaration on registration (`settings=` / `api.settings`) | registration call | `ModulesConfig` mixin and `AuthConfig.tenant_claims_max_organizations` | B-C1, B-C2; I-13 | kernel fields (fail I-13); module-local `BaseSettings` (D1: no shared source, no collision check, kernel tools blind) |
| M2 | per-process source snapshot read by kernel and module models | settings source | the per-instance read of `Settings()` and `config dump`'s second read | one read per process (research F6, P7) | per-module `BaseSettings` reads (~17 ms each with `SECRETS_DIR`); nested models (rename of 22 names, research O5) |
| M3 | post-load configuration check | startup check family | B-C3, B-C4 kernel validators; the closed-registry check `settings.py:191-206`; the kernel-only typo check `:208-235`; the module rows of the removed scan `:167-189` | F4, F5, B-C3, B-C4; I-18 parity together with PR #71 | critical startup tasks (fail parity, research F1); kernel validators (fail I-02) |
| M4 | namespaced risk registry: kernel risks ∪ loaded modules' risks, with the known-namespace rule | registry | closed `RISKS` (`accepted_risks.py:50-145`) | B-C5, T10; D-F4 | flat ids (cannot tell an absent module's id from a typo); a kernel list of module ids (re-couples) |
| M5 | kernel tools read the loaded declarations | none new as a mechanism: existing tools change input | hard-coded names in tiers, backup, console list, `SHARED_KEYS`, dev env, docs generator | B-C6, B-C7, B-C8, B-C9 | each tool keeping its own list (today; fails I-13) |
| M6 | `sandloom check` / `test` settings gate | startup check family (static) | nothing; new coverage | D-F2 across modules not loaded together, D-B1 topology check, prefix rule (#30), per-module ratchet (#26) | a runtime warning for unloaded modules' variables (needs their declarations at runtime; #25 asks for no runtime warning) |
| M7 | `config dump` composition mode and `--kernel-only` | CLI command change (flag) | the kernel-only dump | F2; D-H5 | doctor alone (lists problems, not values and provenance) |

M1-M4 are research O2's four; M6 and M7 are what the owner's answers #25, #30 and #31 add. M5 is
a change of input, not a new mechanism. No mechanism is a new entry-point group, wheel, generated
artifact kind, process-wide default or wire format.

**Entry points after D2.** Composed entry points refuse in step 6 (registration) or step 7
(post-load) with `ConfigurationError`, same prefix and exit 1 as G-CONFIG-07. `config dump`:
collect mode, exit 0 with diagnosed entries, or `--kernel-only`. Deploy tooling: registration
validates from `.env.test`; production checks do not fire outside prod; T014 reads each service's
modules' `shared` fields. Admin CLI: no kernel read of admin fields once decision A's provider
exists. Static commands: unchanged; `check`/`test` run M6.

**Cost.** The snapshot replaces today's read (no extra I/O). A module model validates in about
1 µs over the snapshot (research F6). The post-load check is linear in declared fields. `config
dump` gains one composition build (research: about 0.9 s; must be measured, #31). Readers change
from `settings.billing_provider` to the accessor: 50 module files import the kernel singleton
(`grep -rl "sdk.infra import settings" src/sandloom_*`), not all of them for module fields; about
25 test files set module fields (research §7.6).

**Trade-offs.** A prod `sandloom migrate` one-shot without the module env now refuses (already
true after PR #71). A module setting set where the module is not loaded is silent at runtime
(#25) and caught only by M6. Four to six new SDK names under I-17.

**Verdict.** Passes all 16 scenarios (D-F4 depends on §6.1). Every mechanism maps to a baseline
violation or a required invariant.

### 5.3 D3: static metadata, validated before loading

**Shape.** Each module distribution publishes its declaration in an entry-point group
(`sandloom.settings = pkg.settings:BillingSettings`) or as a JSON file generated by `sandloom
generate`. Before any module loads, the kernel reads the declarations of the selected modules and
validates them in one pass with the kernel's snapshot; it then loads modules as today. Everything
else (risks, removed, tiers, tools) is as D2, read from the metadata.

**New mechanisms.** D2's M1-M7, plus an entry-point group, plus a generated metadata artifact (its
home is decision B), plus a drift check between the artifact and the class. App-local modules
(`LOCAL_MODULES`, `<app>/modules`) have no distribution metadata and need D2's path as well.

**What it buys over D2.** `config dump` and the docs page without building the composition; the
renamed-risk hint (`fake_psp_in_prod` → `billing.fake_psp_in_prod`, D-F4 d) available in every
process where billing is installed, loaded or not; collision detection against installed but
unloaded modules at every boot.

**What it does not buy.** No import is saved by an entry point: importing `pkg.settings` imports
`pkg/__init__.py` (research O3). Only the JSON form avoids import, and import is already
side-effect free and audited (G-SDK-01). Validation still happens before `setup`, as in D2.

**Verdict.** Passes all scenarios. Under H-12 the extra mechanisms remove no violation D2 leaves;
they buy speed of one CLI and a more precise message. Rejected unless the PoC shows D2's `config
dump` cost unacceptable or D-F4 (d) cannot be met by D2 (§6.1).

### 5.4 D4: one composed `Settings`

**Shape.** Read a bootstrap subset (`SANDLOOM_MODULES`, `MODULE_*`, `SANDLOOM_COMPOSITION_ROOT`),
import the selected modules, build `Settings` with `pydantic.create_model` over the kernel config
and the modules' mixins, validate once, swap the singleton (today's class swap,
`settings.py:793-816`), then load.

**What it buys.** No reader churn at runtime: `settings.billing_provider` keeps working. Kernel
tools keep iterating one model.

**What it costs.**
- Mechanisms: bootstrap settings class, dynamic composition, singleton rebuild after the module
  set is known, and still the collision check (pydantic does not detect two mixins reading one
  alias, F5), M4 and M6.
- Order change in every entry point: module import moves before `enforce_configuration`.
- Types: module fields are not on the static `Settings` class, so readers need casts under mypy;
  the churn moves from runtime to typing.
- Coupling: at runtime the kernel's `settings` carries `billing_provider` again, and nothing
  static stops kernel code from reading it (G-KERNEL-13 checks string constants only). That
  reopens the B-C9 kind of edge silently (I-02, I-03).
- Several apps in one process (tests one after another, `load_topology` beside `create_app`)
  share one singleton type; I-24 allows several test apps only with explicit cleanup.
- Per-module ratchets need the owner of each field tracked beside the composed class.

**Verdict.** Passes the scenarios but fails H-12 (the composition removes no violation D2 leaves)
and weakens the static guard against new kernel-to-module edges. Rejected.

### 5.5 Not separate alternatives

- **Nested env names** (`BILLING__PROVIDER`, research O5): renames 22 variables, against #30 and
  H-9. Rejected.
- **Validation in critical startup tasks** (today before PR #71): fails parity (research F1).

## 6. D2 design points the owner's answers leave open

### 6.1 Accepted-risk namespaces (#27, #28, D-F4)

Rule: an id `<ns>.<name>` is (1) checked against the module's declared risks when `<ns>` is a
loaded module, refused if absent (D-F4 c); (2) inert and listed as "not applicable here" in
diagnostics and doctor when `<ns>` is known in the deployment but not loaded; (3) refused when
`<ns>` is unknown (D-F4 b). A flat id that is not a kernel id is refused (D-F4 d). Kernel ids stay
flat (`relay_disabled`, `weak_password_hash`): #28 namespaces module ids; giving kernel ids a
`core.` prefix is a further migration nobody asked for (O-4).

Which namespaces are "known in the deployment" without a new setting:

| source | available in a process without the module | gap |
|---|---|---|
| K1 installed `sandloom.modules` entry-point names (metadata, no import) | yes, if the wheel is in the image | per-service images without the wheel |
| loaded modules | — | — |
| contract-name prefixes in `SERVICE_BINDINGS` and `RPC_ALLOWED_CALLS` (the latter is in `SHARED_KEYS`) | partly: only modules that are called | a module nobody calls |
| K2 an explicit deployment list in the shared env anchor | yes | a new setting (mechanism); drift against the profile |

Evidence: the shipped microservices profile runs every service from one image
(`deploy/compose/microservices.yml:57` `x-image`, every service `image: *image`), so K1 alone sees
every module there, and the tenancy service, which binds nothing (`SERVICE_BINDINGS: '{}'`), still
knows `billing`. With per-service images K1 refuses a foreign namespace: the safe direction (a
refusal, never a silent acceptance), and the operator moves that id out of the shared anchor. T014
can check statically that every namespace in a profile's `SANDLOOM_ACCEPTED_RISKS` is a module some
service loads. Recommendation for the PoC: K1 ∪ loaded ∪ app-local modules named by the
composition root; K2 only if the PoC shows a deployment K1 cannot serve.

D-F4 (d), the old flat id refused "naming the replacement": where billing is loaded, billing
declares the renamed id and the message names `billing.fake_psp_in_prod`. Where billing is not
loaded, D2 can (i) give a generic message ("module risk ids are namespaced since <version>:
`<module>.<id>`; see the upgrade notes"), which names no module, or (ii) import the installed
modules' declarations (K1, a few ms, side-effect free) to find the exact replacement. (ii) meets
D-F4 (d) exactly; (i) keeps the kernel free of module knowledge at the cost of a less specific
message. Owner question O-3.

### 6.2 Removed settings

Kernel removed entries stay in `tiers.REMOVED` and keep being checked from the snapshot by the
kernel validator. A module's removed entries are checked by M3 against the snapshot where the
module is loaded; elsewhere they are inert, like every other variable of an absent module (#25).
`PASSWORD_HASH_ALLOW_WEAK` stays kernel under H1.

### 6.3 Tiers and ratchets (#7, #26)

- Kernel: `SEMANTIC` loses `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS`; `SEMANTIC_BASELINE_COUNT`
  13 → 12, `SEMANTIC_COUNT_CEILING` 15 → 14 (`test_config_tiers.py:51-71`); `OPS` loses the 22
  module names (24 if O-2 moves the refresh-session pair).
- Module: undeclared fields are ops. A module's semantic settings carry the kernel's
  `SemanticSetting` (profile table, strength, risk). Each module's test runs a
  `sandloom.core.testing` helper that fails an unclassified field and pins the module's own
  semantic count; `sandloom test` runs it for every module of the app.
- "Completeness of classification" with ops as default is weak: a semantic setting can slip in as
  ops. Two options without a new mechanism: record each field's tier in the module's
  `module.snapshot.json` (written by `sandloom generate`, reviewed like the route snapshot), or
  require an explicit tier on every field. Owner question O-5.

### 6.4 Collisions and prefixes (#30)

- Runtime (M3): a name declared by the kernel and a loaded module, or by two loaded modules,
  refuses naming both.
- Static (M6): the same across every module of the app, loaded together or not.
- Prefix rule: a new name must start with the module's prefix (`INVENTORY_`); `sandloom add
  module` scaffolds it. The 22 existing names do not follow it (`STRIPE_*`, `GOOGLE_*`,
  `TWOFA_*`, `TENANT_*`). Grandfathering without a kernel list of module names: either a
  per-field exemption with a reason in the declaration (visible in review, also usable by a
  third party), or the module snapshot as baseline (a name already in the committed
  `module.snapshot.json` passes; a new unprefixed name fails). Owner question O-6.

### 6.5 Doctor, diagnostics, redaction

- Doctor: its config section stays kernel-only; its modules section already catches any
  `BaseException` from `load_composition_core` (`core/cli/doctor.py:544-550`) and prints it as a
  FAIL line. To list every module's problem rather than the first, doctor uses the same collect
  mode as `config dump` (D2b's `@prod_check` gives this per check; D2a per module).
- `/diagnostics/core`: tier counts per owner (kernel, then each module), overrides and accepted
  risks including module ones, and the "not applicable here" ids of §6.1.
- Redaction: `config dump` uses the declared secret flag and keeps `is_secret_name` as backstop;
  `--show-secrets` stays refused in prod (`cli/config.py:235-241`). F-a: the declared flag can also
  feed the log redactor's key list; not required by any scenario, recorded as a fix candidate.

### 6.6 Sources

`.env` parsed once, `SECRETS_DIR` looked up per declared alias, precedence unchanged. The pytest
plugin's `.env.test` load into `os.environ` (`core/testing/plugin.py:114-130`) keeps working
because the snapshot reads `os.environ` like today's source.

## 7. Password hashing: platform mechanism and identity policy

### 7.1 Every caller today

| layer | file:line | what it does |
|---|---|---|
| kernel config | `core/config/auth.py:234-292` | `PASSWORD_HASH_CONCURRENCY`, `_PROFILE`, `_TIME_COST`, `_MEMORY_KIB`, `_PARALLELISM` |
| kernel config | `core/config/password_params.py` (whole) | presets `strong`/`owasp_min`, resolution, `meets_owasp_minimum` (pure) |
| kernel config | `core/config/settings.py:48-51`, `:557-580`, `:583-590` | import; validator: argon2 memory bound, OWASP floor in prod unless `weak_password_hash`; `password_hash_params` |
| kernel config | `core/config/accepted_risks.py:96-100`; `core/config/tiers.py:430-434`, `:630-634` | risk `weak_password_hash`; removed `PASSWORD_HASH_ALLOW_WEAK`; ops tier of the five |
| kernel boot | `core/app_factory/config.py:15`, `:450-475`; `core/app_factory/__init__.py:47`, `:142`, `:275` | `warn_weak_password_hashing` at every start |
| kernel mechanism | `core/auth/hashing.py:52-67` (hasher with fixed 16-byte salt, 32-byte digest), `:73-84` default hasher, `:87-119` hash/verify/needs-rehash, `:140-185` bounded executor sized by `PASSWORD_HASH_CONCURRENCY` or CPU/cgroup, `:188-197` async wrappers, `:200-232` `rehash_password_if_needed` and metric | argon2id mechanism |
| kernel public | `core/di.py:27`, `:601-640`, `:793-795`; `sdk/security.py:10-12`, `:30-34`, `:61-67` | SDK `run_password_hashing`, `password_hasher`, `rehash_password_if_needed` (API 1.0) |
| kernel console | `core/admin/api/platform.py:605` | `password_hash_profile` in the settings list |
| kernel redaction | `core/cli/config.py` `_PUBLIC_SEGMENTS` (`HASH`) | `PASSWORD_HASH_*` are not credentials |
| kernel CLI | `core/cli/password_check.py`; `core/cli/admin.py:49`, `:243-262` | password **rules** for the prompt, imported from identity (B-I1); no hashing |
| identity | `sandloom_identity/services/auth_service.py:13` (imports `argon2`, F-b), `:30`, `:60-75` (`_ConfiguredHasher`), `:172-178`, `:189-300` (dummy hash, timing-equalised verify, needs-rehash, hash, async wrappers), `:313`, `:320` (register), `:443` (login verify), `:523` (rehash on login), `:725` (reset), `:797`, `:809`, `:816` (change) | the only caller of the mechanism |
| identity | `sandloom_identity/services/reauth.py:43`; `sandloom_identity/__init__.py:52` | re-authentication; warm-up of the dummy hash |
| identity policy | `sandloom_identity/password_policy.py:13-14`, `:76-93` | length bounds and rules |
| ledger | `guarantees.yml` G-AUTH-15, -16, -17, -18 | -15 presets and overrides, -16 OWASP floor in prod (kernel tests); -17 rehash on a successful login only, never blocking it (identity tests plus one kernel mechanism test); -18 login timing equalised by the dummy hash (identity) |
| tests | `core/tests/test_password_hashing.py`, `test_password_hash_config.py`, `test_security_review_regressions.py`, `test_ops_cli.py`; `sandloom_identity/tests/services/test_login_timing_equalizer.py` | |

### 7.2 Options

**H1. Split by responsibility; no setting moves.** Platform crypto mechanism (kernel): argon2id
with pinned salt and digest length, the work factor (`PASSWORD_HASH_PROFILE` and overrides), the
OWASP floor in prod with the kernel risk `weak_password_hash`, the bounded executor
(`PASSWORD_HASH_CONCURRENCY`, a process resource), rehash detection. Identity policy (identity,
already there): password rules, timing equalisation, when to rehash (on login), re-authentication,
reset and change flows. The one misplaced piece is B-I1 (the kernel CLI imports identity's rules),
handled with the identity CLI move under decision A.
- Platform capability rule (goals.md; H-18): platform guarantee (G-AUTH-15..17, a storage floor
  for whoever stores passwords); no domain specifics (no user model); testable; replaceable (a
  replacement identity uses the same SDK names and the same operator settings); defined absence
  (no caller: the settings are validated and inert); local only. All six hold.
- H-12: no baseline row names `PASSWORD_HASH_*`; moving them would add work without removing a
  confirmed violation.
- Cost: none. Fix F-b separately (declare `argon2-cffi` in identity, or type the attribute through
  the SDK).

**H2. Work factor to identity; floor kept in the kernel mechanism.** `PASSWORD_HASH_PROFILE`,
`_TIME_COST`, `_MEMORY_KIB`, `_PARALLELISM` move into `IdentitySettings` (names unchanged),
shown in the console list as console-visible. The kernel keeps `password_params.py`, the executor
and `PASSWORD_HASH_CONCURRENCY`; `password_hasher()` takes explicit `PasswordHashParams` (an SDK
signature change, I-17); the kernel refuses to build a hasher below the OWASP floor in prod unless
the kernel risk `weak_password_hash` is accepted, so G-AUTH-16 holds for every caller, including a
replacement identity.
- Gains: kernel `Settings` loses four fields only identity uses.
- Costs: an SDK change; a replacement identity cannot reuse `PASSWORD_HASH_*` (collision rule,
  #30) and declares its own names, so operators configure the work factor per issuer; the startup
  warning moves to identity; the floor check runs at hasher build, later than today's settings
  validator, unless identity's production check also calls `meets_owasp_minimum` (it should, for
  parity at registration).

**H3. All of it to identity.** Mechanism, settings, floor, risk (`identity.weak_password_hash`),
executor; `argon2-cffi` moves to identity's wheel.
- Removes the public SDK names `run_password_hashing`, `password_hasher`,
  `rehash_password_if_needed` (API 1.0): H-2 / I-16 exception needed. A replacement identity or any
  other module that stores a secret hash reimplements the bounded executor and the floor (I-20:
  the floor becomes optional per implementation). A second risk-id migration.
- Rejected on H-2 and I-20.

**Evidence favours H1**; H2 if the owner decides the work factor is identity policy. In both, the
floor stays a kernel guarantee.

## 8. Scenario matrix

✓ passes; ◐ passes only by module discipline or partly; ✗ fails. D2 covers D2a and D2b.

| scenario | D1 | D2 | D3 | D4 | note |
|---|---|---|---|---|---|
| D-H1 fixture ops setting | ✗ | ✓ | ✓ | ✓ | D1: tier test and dump blind |
| D-H2 module secret redacted | ✗ | ✓ | ✓ | ✓ | D1: dump does not list it; logs by name in all |
| D-H3 fake PSP in prod, every entry point | ◐ | ✓ | ✓ | ✓ | D1: refusal via PR #71, no namespaced id |
| D-H4 lean dump | ✓ | ✓ | ✓ | ✓ | |
| D-H5 dump with modules, `--kernel-only` | ✗ | ✓ | ✓ | ✓ | D2: +composition build, collect mode; D3: no build |
| D-B1 variable of an unloaded module | ◐ | ✓ | ✓ | ✓ | D1: runtime inert, no static report |
| D-B2 shared key across processes | ✗ | ✓ | ✓ | ✓ | D1: kernel must keep naming it |
| D-B3 module semantic setting, ratchet | ✗ | ✓ | ✓ | ◐ | D4: owner of each field tracked separately |
| D-B4 removed module setting | ◐ | ✓ | ✓ | ✓ | D1: hand-coded per module |
| D-B5 console list mono / without module | ✗ | ✓ | ✓ | ✓ | |
| D-B6 invalid module setting, same code everywhere | ◐ | ✓ | ✓ | ✓ | D1: a `ValidationError` not converted gives `ModuleLoadError` |
| D-F1 weak TOTP key in prod | ✓ | ✓ | ✓ | ✓ | strength check moves to identity in D2-D4 |
| D-F2 collision; unprefixed new name | ✗ | ✓ | ✓ | ◐ | D4: runtime check still needed (F5) |
| D-F3 import with empty env | ◐ | ✓ | ✓ | ✓ | D2: accessor raises with no app |
| D-F4 risk ids a-d | ✗ | ✓ / ◐ | ✓ | ✓ | D2: (d) exact only with option (ii) of §6.1 |
| D-F5 optional module, invalid setting | ◐ | ✓ | ✓ | ✓ | F-c |
| **passed fully** | **2** | **15-16** | **16** | **14** | |

| invariant | D1 | D2 | D3 | D4 |
|---|---|---|---|---|
| I-13 a module adds settings and risks with no kernel change | ✗ (risks) | ✓ | ✓ (app-local modules via the D2 path) | ✓ |
| I-18 every setting covered wherever declared | ✗ | ✓ | ✓ | ✓ |
| I-19 subsets boot; refusal only for a predefined reason | ✓ | ✓ (a selected module's settings error is the predefined reason) | ✓ | ✓ (order change to verify) |
| I-20 no silent degradation | ✓ | ✓ (an absent module's risk id is reported "not applicable", not dropped) | ✓ | ✓ |
| H-12 each mechanism justified | ✓ (none) | ✓ | ✗ | ✗ |
| I-02/I-03 no new kernel-to-module edge | ✗ (B-C5, B-C7 stay) | ✓ | ✓ | ◐ (runtime fields on the kernel singleton) |

## 9. Comparison

| criterion (§4.2) | D1 | **D2a** | D2b | D3 | D4 |
|---|---|---|---|---|---|
| new mechanisms | 0 | 6 (M1-M4, M6, M7) | 6 | 9 | 7 + entry-point order change |
| new SDK names | 0 | 5 | 6 | 6+ | 2-3 |
| baseline rows removed (B-C*) | C1, C2, C3, C4 | C1-C9 (C10 by A) | C1-C9 | C1-C9 | C1-C9 |
| extra startup work per process | 0.4-17 ms per module | ≈ 0 | ≈ 0 | +3.6 ms entry-point scan | ≈ 0 |
| `config dump` | kernel only | +~0.9 s (measure) | same | no build | bootstrap import |
| reader churn | per read | per read, typed | per read, typed | per read | none at runtime, casts for mypy |
| env names changed | 0 | 0 | 0 | 0 | 0 |
| operator migration | none | one risk id | one risk id | one risk id | one risk id |
| one source read | no | yes | yes | yes | yes |
| parity incl. worker, migrate, CLI | via PR #71, by discipline | by construction | by construction | by construction | by construction |
| collisions | none | loaded (runtime), all (static) | same | installed (runtime) | loaded |
| scenarios fully passed | 2/16 | 15-16/16 | 15-16/16 | 16/16 | 14/16 |
| hard constraints | fails H-1 | pass | pass | fails H-12 | fails H-12 |

Migration for running deployments (D2): no variable changes name. One value changes:
`fake_psp_in_prod` → `billing.fake_psp_in_prod` in `SANDLOOM_ACCEPTED_RISKS`, refused with the
replacement named (§6.1), with upgrade steps and a release note. The old id appears in about 30
repository files outside `design/` (compose files, examples, docs, CI, `.env.example`,
`tools/smoke/split_check.py`), all changed in the same PR. Only H3 would add a second rename
(`identity.weak_password_hash`); H1 and H2 keep `weak_password_hash`.

## 10. What the evidence favours

D2 in the D2a form, on top of PR #71, with password hashing under H1.

- It is the only alternative that passes the hard constraints and (with §6.1 option ii) all 16
  decision-D scenarios with mechanisms each tied to a baseline row or an owner answer.
- D2a over D2b: same runtime, fewer and simpler SDK names, nothing for the author beyond pydantic
  and existing kernel dataclasses. D2b's advantages (co-located field metadata, production checks
  that return all problems, a typed `.current()`) are real for DX and doctor output; the
  comparative DX task (§11, P12) should decide, not this document.
- D3 is the fallback if the PoC measures `config dump` or the docs build as too slow, or if D-F4
  (d) must be exact in processes without the module and importing installed declarations is
  refused.
- The ADR decides.

## 11. What a settings PoC must prove

Research §8 P1-P11 stand (fixture module `kb_inventory` outside `src/` with a secret, an ops
setting with a bound, a semantic setting, a namespaced risk, a production check and a removed
setting; billing ported as second subject; own Postgres and Redis, `SANDLOOM_TEST_OWNER=kb-settings`).
Adjusted and added:

| # | criterion | pass | fail |
|---|---|---|---|
| P1 | I-13 | adding the fixture changes no file under `src/sandloom` after the mechanism commit | any kernel edit |
| P2 | parity (D-H1, D-H3, D-B6, D-F1) | PR #71's harness extended: invalid fixture value, oversell in prod, `BILLING_PROVIDER` unset in prod, weak TOTP key, duplicate env name; app, worker, beat, `migrate databases`, the five composed CLIs exit 1 with the same `INVALID_CONFIGURATION` reason naming the setting; doctor shows it as FAIL | any start or different reason |
| P3 | accepted risk (T10) | `inventory.oversell_in_prod` accepted: all entry points start, WARNING logged, metric set, `/diagnostics/core` and doctor list it | refusal or silent start |
| P4 | D-F4 a-d in a split with one shared anchor | (a) inert and listed "not applicable" in the tenancy service; (b), (c) refused; (d) refused naming `billing.fake_psp_in_prod` in a process with billing and in one without; identical across entry points; with K1 only (one image) | any service refuses (a) or accepts (b)-(d) |
| P5 | D-F5 | fixture listed in `SANDLOOM_MODULES`, not in `MODULE_REQUIRED`, invalid value: startup refuses with the module settings error | the module is skipped |
| P6 | D-B1 | fixture variables set where it is not loaded: process starts, `config dump` omits them; `sandloom check`/`test` report the wrong value for the topology that loads it and a wrong module name in `SANDLOOM_MODULES` | runtime refusal, a listed value, or no static report |
| P7 | sources | env, `.env`, `SECRETS_DIR` each supply a value, precedence env > dotenv > secrets; a spy counts one `.env` parse and one secrets lookup per declared alias per process | a second parse or wrong source |
| P8 | tiers (D-B3, D-B4) | kernel ratchet passes at 12 / 14; standard-install semantic total unchanged (14 kernel + 1 billing); fixture semantic override logged at INFO in dev; removed fixture setting refused with today's message shape | drift or unclassified field |
| P9 | split (D-B2, D-F2 static) | two services loading `inventory` with different shared values fail T014; `sandloom check` flags a name declared by two modules never loaded together and an unprefixed new name | either passes |
| P10 | bundled port | billing and identity ported; G-BILLING-18, G-RISK-07, G-AUTH-19, G-CONFIG-07 pass; console list shows `billing_provider` in mono, omits it without billing (D-B5) | a ledger test weakened |
| P11 | `config dump` (D-H5) | lists module values with provenance and redaction, diagnoses an invalid module value without exiting 1; `--kernel-only` byte-identical to today's output; no lifespan, no network or database connection during the run (socket spy; includes billing's PSP client construction in `setup`, `sandloom_billing/psp/__init__.py:50-77`); wall time reported against today | a connection, a lifespan, or a crash on an invalid value |
| P12 | DX (§4.2 comparative task, settings part) | the fixture authored in D2a and in D2b by the DX eval agent: files touched, errors, documentation lookups, kernel files read; mypy passes on readers in both | — (decides D2a vs D2b) |
| P13 | cost | `create_app` with bundled modules plus fixture, 10 alternating base/candidate runs: median within base-against-base noise | beyond noise without approval |
| P14 | deploy tooling | `sandloom add deploy` and T014 run registration with `.env.test`: no production check fires; shared and dev values come from declarations; `deploy_profile.py` no longer names `BILLING_DENY_ON_PAST_DUE`, `TWOFA_ENCRYPTION_KEY`, `BILLING_PROVIDER` | a kernel name left, or a prod check in dev |
| P15 | import audit (D-F3) | `tools/checks/import_audit.py` under `python -I` with the fixture and ported modules: no violation; a module-level accessor call fails the audit | a spy hit |
| P16 | hashing (H1 or H2) | H1: G-AUTH-15..18 tests unchanged. H2: a below-floor `PasswordHashParams` passed by a non-identity fixture is refused in prod without `weak_password_hash`, and identity's production check refuses at registration in every entry point | floor bypassed |

## 12. Open points

| id | point | who |
|---|---|---|
| O-1 | counts: `ModulesConfig` has 21 fields, not 22; module-owned names today are 22, not 23 (24 with O-2). requirements.md I-18 / §4.2 / #30 and baseline B-C1 should be corrected, as F8 corrected the ratchet | requirements owner |
| O-2 | move `REFRESH_TOKEN_EXPIRE_DAYS` and `MAX_SESSION_DAYS` to identity (issuer-owned refresh sessions, answer #16)? Not a baseline row; the console list and `SHARED_KEYS` read them by name today | owner |
| O-3 | D-F4 (d) where the module is not loaded: a generic message, or import installed declarations to name the exact replacement | owner |
| O-4 | kernel risk ids stay flat, or get a `core.` prefix in the same migration | owner |
| O-5 | completeness of classification: tier recorded in `module.snapshot.json`, or an explicit tier on every field | owner, decision B for the snapshot |
| O-6 | grandfathering the 22 unprefixed names: per-field exemption with a reason, or the committed module snapshot as baseline | owner |
| O-7 | known namespaces: K1 (installed entry points ∪ loaded ∪ app-local) enough, or an explicit deployment list (K2) | PoC P4 |
| O-8 | password hashing: H1 or H2 | owner |
| O-9 | identity's `JWT_SECRET` refusal is still lifespan-only after PR #71; under D2 it becomes an identity check active in every environment | PR #71 follow-up or D2 PoC |
| O-10 | B-C10: until decision A provides the link-target provider, `core/cli/admin.py` reads an admin field by name | decision A |

## 13. Not verified

- That `load_composition_core` with the bundled modules opens no socket during `setup` (P11):
  inferred from research §2 (modules read settings, build clients), not run.
- The ~0.9 s `config dump` cost: research's `create_app` time (~440 ms with three modules), not
  measured for the dump.
- Beat parity: PR #71's harness runs beat; not rerun here.
- Per-service images for K1: reasoned from the compose file, no such deployment exists in the
  repository.
