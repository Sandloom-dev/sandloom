# Kernel boundary requirements: invariants, constraints, quality scenarios

Status: **draft, waiting for the owner's approval.** Alternatives start only after the owner
approves this file. Items the owner must decide or approve carry **[Owner]**.

Commit: `b8d8506` (branch `arch/kernel-boundary`). Every `file:line` is at that commit.
Task: tm `kb-requirements` (parent `kb-assess`). Edge ids (A1, B-R1, B-S1, C1, ...) are the
rows of [baseline.md](baseline.md). Guarantee ids (G-..., C-...) are entries of
`guarantees.yml`; their tests are listed there and rendered in `docs/reference/guarantees.md`.

This file states what any solution must satisfy and how a later PoC passes or fails. It
chooses no solution and names no mechanism to build.

## 1. Problem statement

**The rule.** The owner's rule: the kernel (`sandloom.core`, `sandloom.sdk`, `sandloom.shared`,
`sandloom.main`) knows no business module. The kernel contract repeats it
(`src/sandloom/core/AGENTS.md`, "The kernel names no module"), and so does a comment next to
code that breaks it (`core/config/settings.py:636-639`).

**What the code does at `b8d8506`** (baseline §2): no static import of a business module from
shipped kernel code, one dynamic import (B-I1), 40 edges where the kernel names a specific
module or something it owns (class b), and 5 where the kernel ships module-owned code or
artifacts (class c). The edges fall into five groups:

1. **Runtime calls to named contracts.** The kernel calls `billing.entitlements.v1`,
   `tenancy.membership.v1` and `identity.security.v1` by name (B-R1..B-R6) and maps two
   facts to the owner `billing` (B-R3).
2. **Security authority tied to a module name.** The platform-admin gates trust the `admin`
   role only when the string `"identity"` is in `claims_sources` (B-S1
   `shared/guards/platform.py:14-18`; B-S2 `core/admin/api/platform.py:54-76`). The tenant
   allowlist is the claim key `tenant_ids`, which tenancy writes (B-S3). Who may write a
   security claim is not enforced:
   - contributor results merge into one dict, later wins (`core/facade/_claims.py:442`), and
     identity spreads them under its own keys (`sandloom_identity/services/token_service.py:145`);
     only `sub`, `email`, `email_verified`, `token_type` are protected (G-AUTH-13);
   - the source name stamped into `claims_sources` is the `owner=` argument when given,
     unchecked (`core/facade/_claims.py:225`), else the module's short name;
   - `Role("admin")` reads `subject.roles` with no provenance check
     (`shared/authz/atoms_claims.py:54-67`).
3. **Module settings inside the kernel.** 22 settings of identity, billing and admin live in
   the kernel's only `BaseSettings` (B-C1, `core/config/settings.py:113`), with kernel
   validators, tiers, risks, deploy checks and texts for them (B-C2..B-C10). A module cannot
   add a setting without editing the kernel.
4. **Module-owned artifacts in the kernel wheel.** Generated stubs, DTO copies and 34 event
   schemas of identity, tenancy and billing ship in the `sandloom` wheel (C1-C4), plus
   tenancy-specific consumer logic (C5). `docs/contributing/versioning.md:76-83` says module
   packages version independently, yet a change to a module's contract artifact needs a
   `sandloom` release.
5. **Domain authorization in the kernel.** Public atoms encode billing's and tenancy's claim
   keys, statuses and roles (B-N12, B-N13), four of them in the beginner tier (B-N14); check
   rules name tenancy's function and constants (B-N17).

**Why it matters (observable consequences).**

- A third-party billing or tenancy module cannot replace a bundled one without the kernel
  knowing its contract names, claim keys and role sets (B-R1, B-R3, B-N12, B-N13).
- A contract change in a module forces a kernel release (C1-C4).
- Any module that registers a claims contributor can put `admin` into `roles`; `Role("admin")`
  then allows, and the platform-admin gate allows whenever identity also ran. A module
  that passes `owner="identity"`, or is itself named `identity` where `sandloom_identity`
  is not installed, gets the platform-admin trust.
- None of the existing guards sees groups 1-5 (baseline §9): `test_shared_purity.py` cannot
  see `sandloom_*`, `.importlinter` excludes them, `test_module_names.py:165` covers literals
  in four directories, the distribution check admits imports through the `standard` and
  `admin` extras.
- `sandloom graph` does not show the kernel as a node, so these edges are invisible to the
  tool agents use to read the architecture (baseline §7).

**What this file does not decide.** Which of the classes (a), (b), (c) are legitimate is
decision A. Where artifacts live is decision B. How a module extends authorization is
decision C. How a module owns settings is decision D. The CLI, the `sandloom new` starter,
recipes (B-N5..B-N11) and removing `sandloom.shared` come after A-D (kb-assess). Their
scenarios here check behaviour only, not where the code lives.

## 2. Decisions in scope

| id | question |
|---|---|
| A | Kernel boundary: which dependency of the kernel on business modules is legitimate, and how the remaining ones are kept out. |
| B | Contract ownership: where contract DTOs, stubs and event schemas live, who releases them, how a consumer gets them. |
| C | Authorization extension: how a module adds typed policies and facts for its domain without the kernel knowing that domain, and who is the authority for each security claim. |
| D | Module configuration: how a module owns its settings while the framework keeps tiers, validation at every entry point, accepted risks, diagnostics and secret handling. |

## 3. Invariants

Each invariant has: the guarantee ids it ties to, the proof that exists today (a test or
check that fails if the invariant breaks), its state at `b8d8506`, and what proof is missing.
A proposed change later must cite the invariants it serves (kb-requirements reflection,
2026-11-08). **[Owner]** approves the list.

States: **holds** (proof exists and passes), **holds, partial proof** (holds where
measured; the proof misses cases listed), **violated** (code breaks it at `b8d8506`),
**no proof today**.

### I-01 The bare framework runs without business modules

`pip install sandloom` alone: every `sandloom` subcommand's `--help`, `sandloom new` with no
module, `create_app()` with `SANDLOOM_MODULES=[]`, `/health` and `/ready` work; no module
wheel gets installed.

- Guarantees: G-KERNEL-13.
- Proof today: `src/sandloom/core/tests/test_generated_project_packaging.py:148`
  (`test_minimal_app_uses_the_kernel_factory`); lean install in
  `tools/packaging/check_package_distributions.py:307-379`, `:399-413`, run in CI by
  `.github/workflows/packages.yml:53`.
- State: holds, partial proof. The lean install runs `--help` only for commands that call
  modules (`sandloom admin create`, `sandloom security ...`); its leak check lists only
  `sandloom-identity` among module wheels (`:324-328`).
- Missing proof: in the lean venv, run each command that reaches a module (B-I1, B-R4, B-R5)
  past `--help` and assert a defined exit code and message, no traceback; extend the leak
  check to every module wheel.

### I-02 Kernel code imports no business module implementation

No static, dynamic (`import_module`, `__import__`, `find_spec` by computed name) or
`try/except ImportError` import of a `sandloom_*` module, or of an app's module package,
from shipped kernel code.

- Guarantees: G-KERNEL-10 (narrower: `sandloom.shared` against `sandloom.<m>` only).
- Proof today: none for `sandloom_*`. `tests/architecture/test_shared_purity.py:64` sees only
  `sandloom.<m>` subpackages (its discovered set is `{'driverkit'}`, baseline §9);
  `tools/packaging/distribution_imports.py` admits imports reachable through extras
  (`:68-71`, `:202-204`) and misses non-literal dynamic imports (`:129-132`).
- State: violated by B-I1 (`core/cli/password_check.py:42`).
- Missing proof: an import scan over `src/sandloom/{core,sdk,shared,main.py}` that includes
  `sandloom_*` and app module packages, resolves dynamic import arguments or refuses
  non-literal ones, and fails on B-I1 today. **[Owner]**: kernel tests (not shipped,
  baseline §6.2) are in scope or not.

### I-03 Every remaining kernel-to-module edge is approved and checked

After decision A, each kernel edge that names a module or something it owns is either
absent or listed in one approved place with its reason; a new one fails a check with a
code. (The owner rule is the target; which edges are allowed is decision A.)

- Guarantees: G-KERNEL-13 (narrower).
- Proof today: `src/sandloom/core/tests/test_module_names.py:165`
  (`test_the_kernel_names_no_module`) covers exact literals in `core/config`,
  `core/app_factory`, `core/loader/plugin_loader.py`, `main.py` only; it misses `admin`,
  substrings and every other directory (baseline §9).
- State: violated: 40 class (b) and 5 class (c) edges, no check catches them.
- Missing proof: a check over contract names, claim keys, claim sources, setting names,
  module names and artifact files in kernel code and in the kernel wheel, fed by the
  approved list, failing with a code and `file:line`.

### I-04 Module A does not import module B

A business module reads another module only through events, contracts (generated stub
and DTO copy), projections or sagas.

- Guarantees: G-MODULE-01, G-MODULE-02, G-MODULE-13, G-CONTRACT-08.
- Proof today: `tests/architecture/test_module_boundaries.py:48`; `sandloom check` C001,
  C002, C005, C013, C021; `src/sandloom/core/tests/test_contract_dto_locality.py`.
- State: holds. Note: billing reads tenancy's DTO copy through the kernel wheel
  (`sandloom_billing/services/tenant_resolver.py:56`); it is allowed today because the copy
  lives in `sandloom.shared`.

### I-05 A consumer uses a contract without the provider installed or loaded

A process that holds a consumer module, and not the provider's package, imports the stub
and DTO copy, starts, calls the provider over a binding, and receives the copy's classes.

- Guarantees: G-CONTRACT-19, G-DECL-04, G-CONTRACT-12, C-SPLIT-01, C-SPLIT-02.
- Proof today: `src/sandloom/core/tests/test_contract_dto_locality.py:102`;
  `tests/sdk/test_sdk_contract_dtos.py:410`; `tests/sdk/test_sdk_contracts.py:137`;
  `src/sandloom/core/tests/test_consumes_preflight.py:61`;
  `tests/split_conformance/test_split_conformance.py:1620`.
- State: holds, partial proof. Every test runs in the repository environment, where all
  module packages are importable.
- Missing proof: a clean venv with `sandloom` and a consumer package only (no provider
  wheel), calling a provider in another process; assert the result's class is the copy.

### I-06 Local and remote calls keep the same semantics

Results, result types, errors and status codes, the provider's own unit of work, tenant
binding, and authorization decisions are equal in a monolith and in a split.

- Guarantees: G-CONTRACT-01, -06, -07, -09, -10, -11, -17; G-AUTHZ-03, G-AUTHZ-04;
  C-SPLIT-01; C-RPC-05; G-RPC-10.
- Proof today: `src/sandloom/core/tests/test_contract_parity.py:298`, `:341`;
  `src/sandloom_tenancy/tests/test_contract_conformance.py:30`;
  `tests/integration/test_access_facts_split.py:200`;
  `tests/split_conformance/test_split_conformance.py:1620`.
- State: holds for the bundled contracts.

### I-07 Authorization stays fail-closed

Decisions are allow / deny / indeterminate. Indeterminate never becomes allow through `Or`
or `Not`; `Not` of indeterminate is indeterminate; a missing fact, owner or contract is
501, an unreachable owner 503, never allow; the algebra (value, atoms evaluated, order)
is unchanged.

- Guarantees: G-AUTHZ-01, -02, -07, -10, -11, -12, -13.
- Proof today: `tests/laws/test_authz_algebra_laws.py:204`, `:214`, `:230`;
  `tests/integration/test_shared_authz_review.py:169`, `:241`, `:389`;
  `tests/integration/test_access_facts_split.py:270`, `:284`.
- State: holds for the atoms the kernel defines. No proof for an atom or fact defined
  outside the kernel: the law tests use fixed-outcome test atoms.
- Missing proof: run the law properties over atoms registered by a test module from
  outside `src/sandloom`, including one that raises and one whose owner is absent.

### I-08 Each security claim has exactly one authority

For each reserved claim — `roles`, `permissions`, `tenant_ids`, plus `sub`, `email`,
`email_verified`, `token_type`, `claims_sources`, `claims_sources_failed` — exactly one
declared authority may set it. A value for a reserved key from any other contributor is
refused (at registration or at mint) and visible in logs. A module cannot become an
authority by its name or by an argument it passes.

- Guarantees: G-AUTH-11, G-AUTH-12, G-AUTH-13, C-MODULE-12.
- Proof today: for `sub`, `email`, `email_verified`, `token_type`:
  `src/sandloom_identity/tests/test_token_claims_and_revocation.py:38`. Provenance stamping:
  `src/sandloom/core/tests/test_ops_security_hardening.py:378`. A local module named like an
  installed one is refused (SANDLOOM-B020):
  `src/sandloom/core/tests/test_module_names.py:102`.
- State: **violated** for `roles`, `permissions`, `tenant_ids`: results merge flat, later
  wins (`core/facade/_claims.py:442`); `owner=` is unchecked (`:225`); B020 does not apply
  when `sandloom_identity` is not installed.
- Missing proof: a test module contributing `roles: ["admin"]`, one passing
  `owner="identity"`, one named `identity` with `sandloom_identity` absent; each gets a
  startup refusal or a dropped key with a log line, and the minted token carries no
  `admin` role from them. **[Owner]**: the reserved list. Is it these keys only, or every claim
  key a framework gate reads (`subscription_status`, `features`, `limits`, `plan_code`)?

### I-09 A gate that trusts a claim trusts it only from its authority

A gate (platform admin, `Role`, `Perm`, `OrgRole`, claim atoms) reads a claim only when its
authority is in `claims_sources` and not in `claims_sources_failed`. A deployment with no
authority loaded or bound denies (403) every platform-admin route. The same holds in a
monolith and a split.

- Guarantees: G-AUTH-12, G-OPCLASS-07, G-SENS-01, G-REVOKE-01, G-TENANT-13.
- Proof today: forged admin without the identity source is 403 on every platform router:
  `src/sandloom/core/tests/test_admin_operations.py:56` (line `:63`); provenance for facts:
  `tests/integration/test_claims_provenance_and_subscribed.py:77`, `:100`, `:161`;
  operator routes deny tenant accounts: `src/sandloom_billing/tests/test_platform_admin.py:38`.
- State: violated for `Role` and `Perm`: they read `subject.roles` / permissions with no
  provenance (`shared/authz/atoms_claims.py:35-67`). Holds for the platform gates, by the
  string `"identity"` (B-S1, B-S2).
- Missing proof: `require(Role("admin"))` with `roles: ["admin"]` and an empty or foreign
  `claims_sources` answers 403.

### I-10 The tenant is bound only from the signed token

`TenantScope` selects a tenant only inside the signed token's tenant claim or allowlist;
a malformed entry is 400; a revoked token is 401 before any tenant is selected; RPC binds
the tenant only from the MAC-covered context.

- Guarantees: G-TENANT-10, -11, -12, -13; G-RPC-10.
- Proof today: `src/sandloom/core/tests/test_tenant_isolation.py:236`, `:316`;
  `src/sandloom/core/tests/test_auth_rotation_revocation.py:279`.
- State: holds. Who may write `tenant_ids` is I-08.

### I-11 Contract artifacts update independently of the kernel

A provider module release can add a contract version, change a DTO additively or add an
event schema without a release of the `sandloom` wheel. A `sandloom` release changes no
module's contract artifact.

- Guarantees: none (versioning promise: `docs/contributing/versioning.md:76-83`).
- Proof today: none.
- State: violated: C1-C4 ship in the `sandloom` wheel
  (`packages/sandloom/pyproject.toml:129-140`; `architecture_rules.yml:17`).
- Missing proof: a packaging test that lists every file of the built `sandloom` wheel whose
  owner is a business module (expected: none, or the approved list from I-03), and a test
  that changes a module contract additively, regenerates and finds the `sandloom` wheel
  contents unchanged.

### I-12 Consumer and provider upgrade independently, in both directions

- Old consumer + new provider (additive change): calls succeed, `/ready` passes.
- New consumer + old provider: methods the provider has work; a method it lacks is 501
  at the call and fails `/ready` with `contract_incompatible` when the consumer declares
  it; never a 500 and never wrong data.
- Provider implementation absent: a required contract refuses startup
  (`ContractBindingError`, SANDLOOM-B005); an optional one returns `None`.
- Generated artifacts regenerated with an unchanged contract: see I-15.

- Guarantees: G-COMPAT-02, -04, -05, -06, -07; G-CONTRACT-10, -12, -16; G-DECL-04.
- Proof today: `src/sandloom/core/tests/test_contract_compat.py:145`, `:303`, `:322`, `:368`;
  `src/sandloom/core/tests/test_contract_parity.py:426`;
  `src/sandloom/core/tests/test_contract_codec_v2.py:331`;
  `src/sandloom/core/tests/test_consumes_preflight.py:61`, `:111`;
  `tests/sdk/test_sdk_contracts.py:137`.
- State: holds, partial proof. G-COMPAT-03 records a gap: no test drives
  `sandloom contracts check --against http://<provider>` or the snapshot CLI exit codes.
  No test runs two released versions of one module package in two processes.
- Missing proof: a matrix test with module package versions N and N+1 installed in
  separate processes, both orders, over the split conformance script.

### I-13 A new module needs no kernel change

Adding a module — including one with its own contract, events, claims contributor,
access facts, typed authorization policies, settings and admin resources — changes no file
under `src/sandloom/` except generated artifacts whose home decision B sets.

- Guarantees: C-GEN-08, G-MODULE-18, G-MODULE-19, C-MODULE-12; ADR-0010 consequence
  "framework edits 0" (`docs/adr/0010-public-api-1.0.md:312-314`, `:332-334`).
- Proof today: `tests/sdk/test_sdk_generators.py:124`;
  `src/sandloom/core/tests/test_module_generation.py:267`; DX eval metric
  `framework_edits` (`benchmarks/dx/run_eval.py:368`).
- State: holds for contracts, events, consumers, facts and claims contributors. Violated
  for settings (one `BaseSettings`, `core/config/settings.py:113`) and for typed policies
  with domain parameters, which today live in the kernel (B-N12, B-N13).
- Missing proof: a fixture module package outside `src/` with a setting, a claim, a fact,
  a typed policy and an admin resource; it boots, its tests and the ledger pass, and
  `git diff --stat -- src/sandloom` is empty.

### I-14 The hot path is not slower

Request path, unit of work, eventing, authorization evaluation and token minting keep
their instruction counts within the budget.

- Guarantees: G-AUTHZ-02 (claims-only policy opens no DB session, does no I/O); budget:
  at most +1 % instructions per operation on the request and write rungs unless the owner
  approves more (`docs/adr/0010-public-api-1.0.md:328-331`).
- Proof today: `tests/integration/test_shared_authz_review.py:426` for G-AUTHZ-02.
  Instruction counts are measured by hand (`benchmarks/ladder/cg/run_cg.sh`, procedure in
  `benchmarks/README.md:280-291`); no CI job runs it.
- State: no automated proof today.
- Missing proof: cachegrind runs of the request and write rungs, plus an authorization rung
  (claims-only and live policy) and a mint rung, on the base and the candidate build,
  reported per operation. **[Owner]**: does the +1 % budget also apply to authorization
  evaluation and token minting, and must it become a CI gate?

### I-15 Generation is deterministic

The same declarations give byte-identical generated files, whatever the hash seed, module
discovery order and number of runs. `sandloom test` never writes. A stale or hand-edited
generated file fails `sandloom test` with its code.

- Guarantees: C-GEN-15, G-COMPAT-01, G-CONTRACT-07, C-CONSOLE-03.
- Proof today: `src/sandloom/core/tests/test_run_tests_cli.py:131`;
  `tests/sdk/test_sdk_generated.py:832`, `:883`;
  `src/sandloom/core/tests/test_generated_admin_manifest.py:26`;
  `src/sandloom/core/tests/test_contract_parity.py:544` (digest stable across hash seeds).
- State: holds, partial proof. No test runs `sandloom generate` twice under different
  `PYTHONHASHSEED` values and module orders and compares bytes. No test covers a
  generator change with the contract unchanged.
- Missing proof: that test, plus one that upgrades the generator with unchanged
  declarations and asserts the outcome the owner chooses in Q4.

### I-16 Every existing guarantee keeps its tests

No guarantee weakens and no capability is removed; each `guarantees.yml` entry stays live
with its enforcing tests, or is recorded under `removed:` with the owner's approval.

- Guarantees: the whole ledger; overriding rule `docs/adr/0010-public-api-1.0.md:22-23`.
- Proof today: `tests/architecture/test_guarantee_ledger.py:61` (`test_ledger_is_green`);
  `uv run pytest -m guarantees src`.
- State: holds.

### I-17 The public surface changes only by review

`sandloom.sdk` stays the only public surface for modules; every name and signature is in
`public_api_baseline.json`; the beginner tier stays at its reviewed size (30 names);
importing `sandloom.sdk` or a bundled module reads no environment and does no I/O; the
kernel never imports `sandloom.sdk`.

- Guarantees: G-KERNEL-11, G-SDK-01, G-SDK-02, G-SDK-04, G-SDK-05.
- Proof today: `src/sandloom/core/tests/test_review_tooling_checks.py:18`;
  `tests/architecture/test_sdk_import_isolation.py:37`;
  `tests/architecture/test_kernel_import_contracts.py:24`;
  `tests/sdk/test_sdk_generators.py:362`.
- State: holds. Four beginner names are billing and tenancy atoms (B-N14); changing them is
  an API decision (`src/sandloom/core/AGENTS.md`, "Adding a name is an API decision").

### I-18 Configuration guarantees cover every setting, wherever it is declared

Every setting, including one a module owns: classified in exactly one tier with a value
per profile; the semantic tier ratchet holds; a production weakening is an accepted risk
id; boot refusals are identical in the app, the worker, `sandloom migrate` and the CLI;
read only through validated settings; secrets redacted in `sandloom config dump` and logs;
security-relevant settings equal across processes of a deploy profile.

- Guarantees: G-CONFIG-01, -02, -03, -06, -07; G-RISK-03; G-PROFILE-02; G-OPS-11, G-OPS-14;
  G-HTTP-11; G-AUTH-19; G-BILLING-18; G-RISK-07.
- Proof today: `src/sandloom/core/tests/test_config_tiers.py:92`, `:109`, `:131`, `:256`,
  `:339`; `src/sandloom/core/tests/test_boot_configuration_checks.py:95`;
  `tests/architecture/test_ledger_static_pins.py:110`;
  `src/sandloom/core/tests/test_ops_cli.py:206`;
  `src/sandloom/core/tests/test_deploy_profiles.py:446`;
  `src/sandloom_identity/tests/test_token_claims_and_revocation.py:98`;
  `tests/integration/test_resilience_config.py:30`;
  `src/sandloom_billing/tests/test_webhook_hardening.py:196`.
- State: holds for the 22 module settings only because they are kernel fields (B-C1).
  No proof for a setting declared outside the kernel.
- Missing proof: the fixture module of I-13 declares an ops setting, a semantic setting, a
  secret and a production-only refusal; each tier, redaction, entry-point and profile test
  sees them.

### I-19 Every module subset boots

Every bundled module, and every subset, boots and becomes ready on its own; the only
allowed refusal is `ContractBindingError` for a required consumed contract.

- Guarantees: G-MODULE-15, C-SPLIT-03, C-MODULE-06, C-MODULE-07, G-OPS-21.
- Proof today: `src/sandloom_identity/tests/test_module_wiring.py:26` (and tenancy,
  billing); `src/sandloom/core/tests/test_module_boots_alone.py:68`;
  `src/sandloom/core/tests/test_module_subset.py:54`;
  `src/sandloom/core/tests/test_split_readiness.py:231`.
- State: holds.

## 4. Hard constraints and trade-off criteria

### 4.1 Hard constraints

An alternative that breaks one is disqualified; no score compensates.

| id | constraint | source |
|---|---|---|
| H-1 | Every invariant I-01..I-19 holds after the change, or the owner accepted the exception in writing. An invariant that is violated today (I-02, I-03, I-08, I-09, I-11, I-13) must hold after the decision that owns it. | this file; ADR-0010:22-23 |
| H-2 | No guarantee weakens and no capability is removed (I-16). | ADR-0010:22-23 |
| H-3 | Modules stay isolated: no module-to-module import, cross-module work only through events, contracts, projections, sagas. | `AGENTS.md` "The one rule" |
| H-4 | The authorization algebra is unchanged and fail-closed (I-07). | G-AUTHZ-13; ADR-0010 owner decision 4 |
| H-5 | No gate trusts a security claim from a weaker source than today (I-08, I-09, I-10). | G-AUTH-12, G-OPCLASS-07, G-TENANT-10 |
| H-6 | The same module code runs as a monolith and as any split, by configuration only. | C-SPLIT-01; positioning row 9 |
| H-7 | Business code stays independent of infrastructure: no backend SDK or driver in modules; capabilities stay contracts. | positioning row 10; C003, C004 |
| H-8 | `sandloom.sdk` stays the only public surface for modules; its import has no side effects; the kernel never imports it (I-17). | ADR-0010 decision 1; G-SDK-01, G-SDK-02 |
| H-9 | The framework carries no deprecated API and no compatibility alias. A breaking change in a prerelease ships with upgrade steps. | G-KERNEL-12; ADR-0010:75; `versioning.md:50-54` |
| H-10 | Instruction budget of I-14: at most +1 % per operation on the request and write rungs, unless the owner approves more. | ADR-0010:328-331 |
| H-11 | Generated files stay generated: never hand-edited, written only by `sandloom generate`, checked by `sandloom test` (I-15). | `AGENTS.md` golden path |
| H-12 | Each new mechanism is justified by the baseline edges or special cases it removes (by id). A mechanism that removes none disqualifies the alternative until it is dropped. | kb-requirements reflection (4) |
| H-13 | Each proposed change cites the invariants it serves. | kb-requirements reflection (1) |

**[Owner]**: approve H-1..H-13; decide whether H-12 also carries a ceiling on the number of
new mechanisms per decision.

### 4.2 Trade-off criteria

Alternatives that pass every hard constraint are compared on these, by scenario (pass /
fail / at what cost), not by weighted score.

| criterion | measured as |
|---|---|
| Correctness margin | scenarios of section 5 passed with no extra operator or author step |
| Coupling | baseline edges left, by class (a/b/c) and type (import, package, runtime call, configuration, security authority, naming); edges added |
| Runtime complexity | **count of new mechanisms**, each with the edges it removes (H-12). A mechanism is any one of: a new registry or registration call, a new entry-point group, a new kind of generated artifact, a new contract kind or wire format, a new settings source, a new startup check family, a new package or wheel, a new CLI command, a new process-wide default. Also: new startup work and new per-request work. |
| Developer and agent experience | beginner names (30 today); concepts on the golden path (9-10, ADR-0010:312); files an author edits to add a module; kernel files an agent reads in the DX eval (target 0, `benchmarks/dx/README.md:207`); new error codes, each with `sandloom explain` |
| Packaging and versioning | wheels released for one contract change; dependency edges between wheels; extras that pull module wheels |
| Migration cost | files changed in bundled modules, examples, generated apps, docs; upgrade steps an app owner runs |
| Performance | instructions per operation (I-14); added round trips per request, mint and authorization |
| Operations | settings an operator sets for monolith and for split; new startup refusals an operator can hit |

## 5. Quality scenarios

Each scenario: id, stimulus and environment, expected observable outcome, invariants. A PoC
passes a scenario only when the stated outcome is observed. Environments:

- **mono**: one process, every bundled module.
- **split**: the shipped microservices profile (C-DEPLOY-03), one service per module.
- **lean**: clean venv with the `sandloom` wheel only.
- **fixture module**: a module package outside `src/`, registered through the
  `sandloom.modules` entry point.

### 5.A Kernel boundary

**Happy**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| A-H1 | Bare `sandloom` install (lean): `sandloom --help` and each subcommand's `--help`, `sandloom new app` with no module, `create_app()` with `SANDLOOM_MODULES=[]` | all exit 0; `/health` and `/ready` 200; `pip list` shows no `sandloom-*` module wheel | I-01, I-02 |
| A-H2 | Full standard install, mono and split | `sandloom test --full`, the split conformance suite and the ledger pass with no change to their expectations | I-06, I-16, I-19 |
| A-H3 | A fixture module with a contract, a claims contributor, an access fact and a setting, installed beside the bundled modules (mono) | boots; its routes and contract answer; `git diff --stat -- src/sandloom` is empty | I-13 |
| A-H4 | The kernel edge check of I-03 runs on the candidate tree | exit 0; it prints the remaining edges by class and type; each is in the approved list | I-03 |

**Boundary**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| A-B1 | Split deploy without the owner module: a service loads billing only, with bindings to identity and tenancy; then a service with no binding to identity | every request of the conformance script answers as in mono, or with an explicit 404/501/503; never 500, never a different 2xx; `/api/v1/platform/billing/*` answers 403 to every caller when no identity authority is bound | I-06, I-09, I-19 |
| A-B2 | `sandloom admin create` and `sandloom security revoke-sessions` with identity not installed (lean) | non-zero exit with a message naming the missing module or contract; no traceback; the output names the password policy it applied | I-01, I-02 |
| A-B3 | An app module named `identity` (not `sandloom_identity`), `sandloom_identity` not installed, contributes `roles: ["admin"]` | the minted token carries no `admin` role from it, or startup refuses it with a code; every platform route answers 403 to that user | I-08, I-09 |
| A-B4 | `sandloom` upgraded by one release while module wheels stay (mono and split); then one module wheel upgraded while `sandloom` stays | each combination inside the declared version ranges boots and passes the conformance script | I-11, I-12 |
| A-B5 | `SANDLOOM_MODULES` names the bundled modules in a different order | same app, same routes snapshot, same generated files | I-15, I-19 |

**Failure**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| A-F1 | A kernel change adds `import sandloom_billing`, `import_module(f"sandloom_{n}")`, or the literal `"billing.entitlements.v1"` in `core/` | `sandloom test` fails with a code and the `file:line` | I-02, I-03 |
| A-F2 | A non-identity module's contributor returns `{"roles": ["admin"]}` (mono and split) | refused at registration or dropped at mint with a log line; `require(Role("admin"))` and `require_platform_admin` answer 403 for that user | I-08, I-09 |
| A-F3 | A module calls `add_claims_contributor(fn, owner="identity")` | startup refuses with a code naming the module and the claimed owner | I-08 |
| A-F4 | Identity's critical contributor fails at mint | mint answers 503 `ClaimsUnavailable`; no token issued; a token carrying `identity` in `claims_sources_failed` gets 403 on platform routes | I-09; G-AUTH-11 |

### 5.B Contract ownership

**Happy**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| B-H1 | Provider publishes a new DTO version: billing adds an optional field to a DTO of `billing.credits.v1` and releases `sandloom-billing` only | `sandloom generate` passes the compatibility check and writes billing's artifacts; no new `sandloom` release is needed (the built `sandloom` wheel's file list and hashes are unchanged); an old consumer's call returns 200 with its copy's fields | I-11, I-12, I-15 |
| B-H2 | Provider absent from the consumer process: an app module calls `billing.credits.v1` over a binding; `sandloom-billing` not installed in that process | process starts; the call returns an instance of the consumer's DTO copy class; `pip list` in that venv shows no `sandloom-billing` | I-05, I-06 |
| B-H3 | Generator updated, contract unchanged: new `sandloom` release, `sandloom generate` on unchanged declarations | **[Owner]** chooses (Q4): zero diff, or a diff that `sandloom test` reports by file with SANDLOOM-T007 while the old artifacts keep working at run time | I-15 |
| B-H4 | A module adds an optional field to one of its event channels `.v1` and releases only its own package | existing views and `@on` consumers keep consuming (unknown field ignored); SANDLOOM-T005 passes; no `sandloom` release needed | I-11; G-DECL-01, C025 |

**Boundary**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| B-B1 | Identity and billing upgraded at different times (split): identity at N+1 with an additive contract change while billing stays N; then the reverse | in both orders the conformance script answers as in mono; `/ready` 200 on every service; no 500 | I-12, I-06 |
| B-B2 | Consumer holds two contract versions: an app module calls `billing.credits.v1` and `billing.credits.v2` while the provider serves both | both stubs import in one process; the DTO copies are distinct classes with no name clash; each call reaches its version | I-05, I-15 |
| B-B3 | Old consumer sends to new provider (additive) | call 200; `/ready` passes (G-COMPAT-04) | I-12 |
| B-B4 | New consumer + old provider: the consumer calls a method the deployed provider lacks | 501 `ServiceUnavailableInDeployment` at the call; `/ready` fails with `contract_incompatible` when the method is declared in the consumer's requirement; never 500, never a default value | I-12 |
| B-B5 | Two applications compose the same installed provider module, each generating its own copies | in each process exactly one copy class per DTO is used for decoding; the provider package carries no app-specific file | I-05, I-15 |

**Failure**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| B-F1 | Stale generated wrapper: a consumer holds an outdated or hand-edited stub or DTO copy | `sandloom test` fails with SANDLOOM-T007 or C012 naming the file; at run time an older copy decodes a newer additive result without error | I-15, I-12 |
| B-F2 | Provider implementation absent: required contract, no local provider and no binding; then the same with `optional=True` | required: startup refuses with `ContractBindingError` (SANDLOOM-B005); optional: `client()` is `None` and the module boots | I-12, I-19 |
| B-F3 | Provider makes a breaking change inside `.v1` | `sandloom generate` refuses and writes nothing (SANDLOOM-T004); a provider deployed anyway fails its callers' `/ready` with `contract_incompatible`, and startup with `CONTRACT_COMPAT_CHECK=strict` | I-12, I-15 |
| B-F4 | Two installed packages both ship artifacts for one contract name | a check or startup refusal with a code naming both packages; never two importable shapes for one contract | I-05, I-11 |

### 5.C Authorization extension

**Happy**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| C-H1 | A fixture module adds a typed policy for its own domain and an app route uses it inside `And`, `Or`, `Not` | no file under `src/sandloom` changes; the law properties of I-07 pass over the new policy; the route snapshot records it | I-07, I-13 |
| C-H2 | Existing billing gates, mono and split: `Subscribed(of=Account.user())`, `Credits(1, of=Account.org(param="org_id"))`, `OrgRole("admin")` | same statuses as today: 402 on a definite billing denial, 403 on a role denial; claims-only policy opens no DB session; live policies call the owner locally or over the binding | I-06, I-07, I-14 |
| C-H3 | A third-party alternative billing package (not `sandloom_billing`) answers subscription and credit questions; `sandloom_billing` not installed | the billing gates of the conformance script give the same HTTP outcomes as with `sandloom_billing`; no kernel file changes; **[Owner]** decides (Q5) whether app gate code may change when the billing package is swapped | I-03, I-13 |
| C-H4 | Organization membership gate with tenancy, mono and split | same decision in both | I-06 |

**Boundary**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| C-B1 | A fact gains a parameter: the owner adds an optional parameter in a new release | callers without the parameter get the same decision as before; a caller that passes it to an old owner (split) gets 501 or 503, never allow | I-07, I-12 |
| C-B2 | The kernel lacks the fact owner: a policy names a fact whose owner is neither loaded nor bound | 501; inside `Not` or `Or` the route never allows on that branch | I-07 |
| C-B3 | JWT from an old identity: a token minted before the upgrade, without a claim the new version reads, or without `claims_sources_failed` | the missing claim is unknown: the policy escalates to a live check or is indeterminate; never allow on a missing claim; platform routes answer 403 to a token without the authority in provenance | I-07, I-09 |
| C-B4 | `Account.bound_tenant()` with tenancy not deployed | tenant kind unknown, decision indeterminate (501), never the user's account | I-07; G-AUTHZ-11 |
| C-B5 | Organization `Feature` or `WithinLimit` with no organization fact registered | 501 (G-AUTHZ-12) | I-07 |

**Failure**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| C-F1 | Billing down during authorization (split, billing bound but unreachable) | live policy: 503; `Not(live policy)`: 503, not allow; claims-only policy with billing trusted in provenance: decided from claims, no call; with billing in `claims_sources_failed`: escalates to live, 503 | I-07, I-09 |
| C-F2 | A module's policy raises an exception during evaluation | indeterminate or 5xx, never allow; the denial is recorded in the deny log and `access_denied_*` metrics | I-07; G-AUTHZ-08 |
| C-F3 | Two modules register a fact or policy under the same name | startup refuses with a code naming both modules; no last-wins | I-07, I-08 |
| C-F4 | A module contributes a claim key owned by another module (`tenant_ids` from billing, `subscription_status` from a non-billing module) | refused or dropped with a log line; a gate reading that key trusts only its authority's provenance | I-08, I-09, I-10 |

### 5.D Module configuration

**Happy**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| D-H1 | A fixture module adds an ops setting | no file under `src/sandloom` changes; the tier test lists it; `sandloom config dump` shows it; an invalid value is refused identically by the app, the worker, `sandloom migrate` and the CLI | I-13, I-18 |
| D-H2 | A module secret (`STRIPE_SECRET_KEY`) set | `sandloom config dump` and logs show it redacted; `--show-secrets` refused in prod | I-18; G-OPS-11 |
| D-H3 | `BILLING_PROVIDER=fake` with `ENVIRONMENT=prod` | refused at every entry point unless `fake_psp_in_prod` is accepted; same message and code as today | I-18; G-BILLING-18, G-RISK-07 |
| D-H4 | Bare install (lean) | `sandloom config dump` lists no setting of an uninstalled module; no module environment variable is required | I-01, I-18 |

**Boundary**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| D-B1 | A module's environment variable is set in a process that does not load the module (split) | the process starts; the value is neither validated nor shown by that process; **[Owner]** decides (Q6) whether a warning is logged | I-18, I-19 |
| D-B2 | Split deploy: a security-relevant setting shared across processes (`BILLING_DENY_ON_PAST_DUE`, B-C7) | the deploy-profile check (SANDLOOM-T014) still requires equal values in every process | I-18; G-OPS-14 |
| D-B3 | A module's semantic setting (`BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS`, `core/config/tiers.py:332-338`) | the semantic count with the bundled modules is unchanged and the ratchet (16) still fails a new unreviewed semantic setting; **[Owner]** decides (Q7) whether a third-party module's semantic settings count toward the 16 | I-18; G-CONFIG-02 |
| D-B4 | A removed module setting (`core/config/tiers.py:659-678`) set to a non-inert value | startup refuses with the same message as today | I-18; G-CONFIG-03 |
| D-B5 | Kernel output that shows a module setting (platform settings view `billing_provider`, B-C9; set-password link target, B-C10) in mono, then in a process without that module | mono: output unchanged; without the module: the field is absent, no error | I-03, I-18 |

**Failure**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| D-F1 | Weak `TWOFA_ENCRYPTION_KEY` in prod with identity loaded | refused at every entry point with a code and the fix (G-AUTH-19, G-CONFIG-07) | I-18 |
| D-F2 | Two modules declare the same environment variable name | startup refuses with a code naming both modules | I-18 |
| D-F3 | Importing a module with an empty environment (`python -I`) | reads no environment variable and constructs no settings (G-SDK-01) | I-17, I-18 |
| D-F4 | `ACCEPTED_RISKS` names a risk owned by a module that is not installed (`fake_psp_in_prod` without billing) | **[Owner]** decides (Q8): refused as an unknown id, or accepted and inert; either way identical in every entry point | I-18 |

**Scenario counts**

| decision | happy | boundary | failure | total |
|---|---|---|---|---|
| A | 4 | 5 | 4 | 13 |
| B | 4 | 5 | 4 | 13 |
| C | 4 | 5 | 4 | 13 |
| D | 4 | 5 | 4 | 13 |

## 6. Questions the owner must answer

1. Approve the invariant list I-01..I-19, including the states recorded for each.
2. Approve the hard constraints H-1..H-13. Is there a ceiling on new mechanisms per
   decision, beyond the justification H-12 asks for?
3. Kernel tests (`src/sandloom/core/tests`, not shipped): are they inside I-02 and I-03, or
   may they import bundled modules (baseline §6.2)?
4. B-H3: after a generator change with the contract unchanged, must the generated files stay
   byte-identical, or is a reported diff acceptable as long as older artifacts keep working
   with the newer kernel inside one minor version?
5. C-H3: when an app swaps the billing package for a third-party one, may the app's gate
   code change, or must it stay as written?
6. D-B1: when a module's environment variable is set in a process without that module, log
   a warning or stay silent?
7. D-B3: do a third-party module's semantic settings count toward the framework's ratchet of
   16?
8. D-F4: an accepted-risk id owned by a module that is not installed: an unknown id
   (startup refusal) or inert?
9. I-08: the reserved claim list. Only `roles`, `permissions`, `tenant_ids` (plus the
   identity and provenance claims), or every claim key a framework gate reads
   (`subscription_status`, `features`, `limits`, `plan_code`)?
10. I-08 and I-09 tighten behaviour that apps can rely on today: a non-identity contributor's
    `roles` value, and `Role` / `Perm` without provenance. Accept this as a breaking change
    in `0.x` with upgrade steps?
11. I-14: does the +1 % instruction budget also cover authorization evaluation and token
    minting, and must the measurement become a CI gate?
12. Scope: confirm that the CLI, `sandloom new`, recipes and `sandloom.shared` removal are
    decided after A-D, so A-B2 and D-B5 check behaviour only.
