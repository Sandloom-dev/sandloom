# Kernel boundary requirements: invariants, constraints, quality scenarios

Status: **draft, amended after the Codex review; waiting for the owner's approval.**
Alternatives start only after the owner approves this file. Items the owner must decide or
approve carry **[Owner]**.

Commit: `b8d8506` (branch `arch/kernel-boundary`). Every `file:line` is at that commit,
except `core/facade/_claims.py` and `core/tests/test_claims_key_ownership.py`: PR #70
(`roles-claim-trust`, merged into `main` as `e264b09` and into this branch as `44451e4`)
changed them, and they are cited at `44451e4`. No other file under `src/` changed since
`b8d8506` (`git diff b8d8506 44451e4 --stat -- src/`).

Task: tm `kb-requirements`, amended by `kb-requirements-amend` (parent `kb-assess`). Edge ids
(A1, B-R1, B-S1, C1, ...) are the rows of [baseline.md](baseline.md). Guarantee ids (G-...,
C-...) are entries of `guarantees.yml`; their tests are listed there and rendered in
`docs/reference/guarantees.md`. D1-D4 (silent degradations), T1-T10 (trust decisions the
application cannot make) and the probes S0-S7 / boot probe runs 1-3 are from
[guarantee-map.md](guarantee-map.md) §11-12 and `_evidence/guarantee-map/`. Owner principles
are from [goals.md](goals.md).

This file states what any solution must satisfy and how a later PoC passes or fails. It
chooses no solution and names no mechanism to build.

**Amended 2026-11-08** (Codex review of this file and owner decisions recorded in tm on
`kb-requirements-amend` and `kb-assess`): the rule reads as a rule about source dependencies
and shipped code (§1, I-02, I-03); I-02 separates the generic loader from a hard-coded module
import; I-06 is scoped to equal database capabilities with listed exceptions; I-08 and I-09
say who assigns a claim's authority and record PR #70; I-10, I-12, I-19 states corrected;
I-14 split into a hard rule, a measurement and a threshold; new I-20 (no silent degradation),
I-21 (the kernel keeps the general protocol), I-22 (the application assigns trust), I-23
(moving an atom changes no execution); H-10 and H-12 rewritten, H-14..H-19 added; scenarios
and owner questions updated.

## 1. Problem statement

**The rule.** The owner's rule: the kernel (`sandloom.core`, `sandloom.sdk`, `sandloom.shared`,
`sandloom.main`) knows no business module. The kernel contract repeats it
(`src/sandloom/core/AGENTS.md`, "The kernel names no module"), and so does a comment next to
code that breaks it (`core/config/settings.py:636-639`).

**What the rule means** (goals.md, "What the rule means", owner 2026-11-08). It is a rule
about **source dependencies and shipped code**: the kernel does not import a business module,
does not name one or something it owns (contracts, events, settings, claim keys, commands),
and does not ship a module's code or artifacts. It is not an absolute semantic requirement.
The kernel **owns the general protocol of security and isolation** that every module and
service relies on: the authenticated subject, trusted claims and who may issue them, rights
(roles, permissions), the tenant and tenant isolation, an administrative operation, token
revocation, and the authorization mechanism (goals.md, "Authorization: the mechanism stays in
the kernel"). What is an error is the kernel holding a specific module's model or binding of
such a concept (a tenant is an organization with `member/admin/owner`; a right is trusted only
when its source is named `"identity"`). Both mistakes are possible: leaving a module's model in
the kernel, and pushing a general protocol out of it; the second weakens the guarantees the
kernel exists to give (goals.md, "How to apply it").

**Who owns what** (goals.md). The kernel owns the universal guarantees and protocols; batteries
(identity, tenancy, billing, admin, any third-party module) implement them and own their domain
models; the application chooses the implementations and assigns trusted authorities. No
platform guarantee silently disappears because an optional module is absent.

**What the code does at `b8d8506`** (baseline §2): no static import of a business module from
shipped kernel code, one dynamic import that names one (B-I1), 40 edges where the kernel names
a specific module or something it owns (class b), and 5 where the kernel ships module-owned
code or artifacts (class c). The edges fall into five groups:

1. **Runtime calls to named contracts.** The kernel calls `billing.entitlements.v1`,
   `tenancy.membership.v1` and `identity.security.v1` by name (B-R1..B-R6) and maps two
   facts to the owner `billing` (B-R3).
2. **Security authority tied to a module name.** The platform-admin gates trust the `admin`
   role only when the string `"identity"` is in `claims_sources` (B-S1
   `shared/guards/platform.py:14-18`; B-S2 `core/admin/api/platform.py:54-76`). The tenant
   allowlist is the claim key `tenant_ids`, which tenancy writes (B-S3). Who may write a
   security claim:
   - at `b8d8506` contributor results merged into one dict, later wins; any module could
     replace `roles`, `user_id`, `tenant_id`, `iat_us` (baseline §14; guarantee-map S2, S3,
     S7);
   - since PR #70 (`44451e4`) a key has one owner per token and a second writer refuses the
     mint (503), and the reserved keys (provenance, token envelope, `UserContext` structure,
     `tenant_id`, `org_id`) are nobody's (`core/facade/_claims.py:62-87`, `:497-535`);
   - still open: nothing the application declares assigns an authority to a key. Without
     identity the first writer of `roles` owns it (S1); the source name in `claims_sources` is
     the `owner=` argument when given, unchecked (`_claims.py:262`), else the module's short
     name, or the binding key text for a remote contributor (T6); `Role("admin")` reads
     `subject.roles` with no provenance check (`shared/authz/atoms_claims.py:54-67`).
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
  knowing its contract names, claim keys and role sets (B-R1, B-R3, B-N12, B-N13;
  guarantee-map §5: a replacement must serve the contract under billing's or tenancy's name or
  take the process-wide adapter slot).
- A contract change in a module forces a kernel release (C1-C4).
- Without identity, the first module that writes `roles: ["admin"]` gets `Role("admin")`
  (S1, PR #70 included). A module that passes `owner="identity"`, or is itself named
  `identity` where `sandloom_identity` is not installed, gets the platform-admin trust (S0, S4,
  boot probe run 2). Code in a process holding the signing key mints any claims and provenance
  (S5).
- Four guarantees weaken silently when a battery is absent (guarantee-map §11): D1 durable
  revocation applier (identity), D2 `Subscribed` provenance (billing), D3 `Role`/`Perm`
  provenance (identity), D4 contract compatibility check (consumer-only install).
- Ten trust decisions cannot be stated by the application (guarantee-map §12, T1-T10); the one
  explicit assignment today is remote: `SERVICE_BINDINGS`, `service_id`, `RPC_ALLOWED_CALLS`
  (guarantee-map §7).
- `sandloom` + `sandloom-billing` as published fails to boot without celery, an optional
  extra of the framework (baseline §13.2, `_evidence/consumer-install.txt`).
- None of the existing guards sees groups 1-5 (baseline §9): `test_shared_purity.py` cannot
  see `sandloom_*`, `.importlinter` excludes them, `test_module_names.py:165` covers literals
  in four directories, the distribution check admits imports through the `standard` and
  `admin` extras.
- `sandloom graph` does not show the kernel as a node, so these edges are invisible to the
  tool agents use to read the architecture (baseline §7).

**What this file does not decide.** Which of the classes (a), (b), (c) are legitimate is
decision A. Where artifacts live is decision B. How a module extends authorization, and how
the application assigns security authorities, is decision C. How a module owns settings is
decision D. The CLI, the `sandloom new` starter, recipes (B-N5..B-N11) and removing
`sandloom.shared` come after A-D (kb-assess description). Their scenarios here check behaviour
only, not where the code lives.

## 2. Decisions in scope

| id | question |
|---|---|
| A | Kernel boundary: which dependency of the kernel on business modules is legitimate, and how the remaining ones are kept out. |
| B | Contract ownership: where contract DTOs, stubs and event schemas live, who releases them, how a consumer gets them. |
| C | Authorization extension: how a module adds typed policies and facts for its domain without the kernel knowing that domain; how the application assigns the authority of each security claim and the kernel verifies it. |
| D | Module configuration: how a module owns its settings while the framework keeps tiers, validation at every entry point, accepted risks, diagnostics and secret handling. |

## 3. Invariants

Each invariant has: the guarantee ids it ties to, the proof that exists today (a test or
check that fails if the invariant breaks), its state, and what proof is missing. A proposed
change later must cite the invariants it serves (kb-requirements reflection, 2026-11-08).
**[Owner]** approves the list.

States: **holds** (proof exists and passes), **holds, partial proof** (holds where
measured; the proof misses cases listed), **violated** (code breaks it), **no proof today**.
A state is at `b8d8506` unless it names `44451e4` (PR #70).

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

### I-02 Kernel code has no hard-coded dependency on a specific business module

Shipped kernel code holds no static or dynamic dependency fixed on a specific business module:
no `import sandloom_<m>`, no `import_module` / `__import__` / `find_spec` / `try/except
ImportError` whose target is a module name written into kernel code (a literal, a constant, a
default value, or a name computed from them).

**Allowed:** the generic loader importing a module named by an explicitly declared module
descriptor that the application or a distribution supplies at run time: `SANDLOOM_MODULES`,
`LOCAL_MODULES`, an installed `sandloom.modules` entry point, or a submodule derived from such a
descriptor (`f"{descriptor.name}.admin"`). These are the class (a) edges A1-A3, A7, A10-A17
(baseline §3); the owner rule reads as a rule about source dependencies (goals.md), and the
loader depends on no specific module.

**How a check tells the two apart:** by the origin of the imported name. A dynamic import is
allowed only when its argument flows from a descriptor read at run time, at a listed loader
call site; it is refused when the argument is, or is built from, a module name fixed in kernel
source. B-I1 is refused: the name is the constant `IDENTITY_POLICY =
"sandloom_identity.password_policy"` (`core/cli/password_check.py:21`), passed through a
variable (`:42`). A check that cannot resolve an argument's origin refuses it.

- Guarantees: G-KERNEL-10 (narrower: `sandloom.shared` against `sandloom.<m>` only).
- Proof today: none for `sandloom_*`. `tests/architecture/test_shared_purity.py:64` sees only
  `sandloom.<m>` subpackages (its discovered set is `{'driverkit'}`, baseline §9);
  `tools/packaging/distribution_imports.py` admits imports reachable through extras
  (`:68-71`, `:202-204`) and misses non-literal dynamic imports (`:129-132`; baseline §9
  probe: `import_module(NAME)` passes).
- State: violated by B-I1 (`core/cli/password_check.py:21`, `:40-44`).
- Missing proof: an import scan over `src/sandloom/{core,sdk,shared,main.py}` that includes
  `sandloom_*` and app module packages, separates descriptor-sourced loader imports from fixed
  names as above, and fails on B-I1 today. **[Owner]** (Q3): kernel tests (not shipped,
  baseline §6.2) are in scope or not.

### I-03 Every remaining kernel-to-module edge is approved and checked

After decision A, each kernel edge that names a specific module or something it owns (its
contracts, events, settings, claim keys, commands, artifacts) is either absent or listed in one
approved place with its reason; a new one fails a check with a code. A concept of the general
security and isolation protocol stated in kernel terms (I-21) is not such an edge; the same
concept bound to one module's names or data model is (goals.md, "What is an error").

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
  module packages are importable. Observed once by hand from wheels (baseline §13.2 phase B:
  the call returns the kernel wheel's `MemberState` copy).
- Missing proof: a clean venv with `sandloom` and a consumer package only (no provider
  wheel), calling a provider in another process; assert the result's class is the copy.

### I-06 Local and remote calls keep the same semantics

Results, result types, errors and status codes, the provider's own unit of work, tenant
binding, and authorization decisions are equal in a monolith and in a split, **for the same
declared database capabilities and execution conditions** (the same backend renderer
capabilities, isolation level and deploy-profile settings). A backend-specific difference is
allowed only when it is listed here with its guarantee and tests, and has a computed label;
an unlisted difference is a violation.

Listed exceptions:

| backend | difference | guarantee and tests |
|---|---|---|
| SQLite (`serial_writers`) | a write unit opened inside a writing unit of the same task on the same database, after the caller ran a statement (a local contract call, a saga step, a child `open_uow()`), runs as a SAVEPOINT of the caller and commits with it; on PostgreSQL it commits on its own (`core/sql_alchemy/nesting.py:14-15`, `:318-338`); label "commits with the caller" | G-UOW-09; `src/sandloom/core/tests/test_sqlite_nested_units.py`, `test_nesting_policy.py::test_nested_units_match_the_oracle` |

(Codex review of this file, 2026-11-08: equivalence was stated without its scope.)

- Guarantees: G-CONTRACT-01, -06, -07, -09, -10, -11, -17; G-AUTHZ-03, G-AUTHZ-04;
  C-SPLIT-01; C-RPC-05; G-RPC-10; G-UOW-09 (the listed exception).
- Proof today: `src/sandloom/core/tests/test_contract_parity.py:298`, `:341`;
  `src/sandloom_tenancy/tests/test_contract_conformance.py:30`;
  `tests/integration/test_access_facts_split.py:200`;
  `tests/split_conformance/test_split_conformance.py:1620`.
- State: holds for the bundled contracts, on the listed exception.
- Missing proof: none for the bundled contracts. A new backend-specific difference found by a
  PoC is added to the table with its test, or the PoC fails.

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
  outside `src/sandloom`, including one that raises and one whose owner is absent; the
  owner's regression set (tm reflection on `kb-alt-authz`, 2026-11-08): `~Entitled("missing.fact")`
  never allows; `Role("admin") | Entitled("missing.fact")` with a trusted role allows.

### I-08 Each security claim has one authority, assigned by the application and verified by the kernel

- **Assignment.** The authority of a security-sensitive claim (`roles`, `permissions`,
  `tenant_ids`; goals.md "Guarantee, provider, trust"; the full list is Q9) is assigned by
  trusted application configuration or composition, or by a platform protocol. A module's
  self-registration never makes it the authority: not its name, not an `owner=` argument, not
  the text of a binding key, not a `describe()` answer (goals.md: "A module naming itself
  `identity` is not proof").
- **Verification.** The kernel verifies at mint, for local and remote contributors, that the
  writer of such a claim is the assigned authority, or, for a remote one, the service bound to
  it in trusted configuration.
- **Ownership.** Every other claim key has exactly one writer per token. Reserved keys
  (provenance, token envelope, `UserContext` structure, the bound tenant) have no contributor
  writer at all.
- **Failure.** A violation refuses the mint (fail closed) and logs the contributor and the key.
  No authority assigned for a claim that a gate reads: I-09 and I-20 apply.

(Codex review 2026-11-08; owner, goals.md "Who owns what" and tm reflection on `kb-assess`
2026-11-08: guarantee, provider and trust are separate; a data contract does not make a
provider a trusted authority.)

- Guarantees: G-AUTH-11, G-AUTH-12, G-AUTH-13, C-MODULE-12.
- Proof today (at `44451e4`): `src/sandloom/core/tests/test_claims_key_ownership.py`:
  one owner per key in a monolith (`test_monolith_module_cannot_write_identitys_claims`,
  `:148`), whatever the order (`test_monolith_order_does_not_matter`, `:170`), and from a
  remote owner (`test_split_remote_owner_cannot_write_identitys_roles`, `:187`); reserved keys
  refused (`test_no_owner_writes_a_reserved_key`, `:233`;
  `test_reserved_keys_cover_the_envelope_and_provenance`, `:248`); the reserved set is
  `claims_sources`, `claims_sources_failed`, `user_id`, `provenance_authoritative`, `claims`,
  `tenant_id`, `org_id`, `sub`, `iss`, `aud`, `exp`, `nbf`, `iat`, `jti`, `iat_us`,
  `auth_time`, `token_type` (`core/facade/_claims.py:62-87`). Identity's own `sub`, `email`,
  `email_verified`, `token_type`: `src/sandloom_identity/tests/test_token_claims_and_revocation.py:38`.
  A local module named like an installed one is refused (SANDLOOM-B020):
  `src/sandloom/core/tests/test_module_names.py:102`.
- State: **holds** for one writer per key and the reserved keys (`44451e4`); **violated** for
  assignment and verification:
  - nothing the application declares names the authority of any key (guarantee-map §4
    "authority", T1-T3); without identity the first writer of `roles` owns it (S1), without
    tenancy the first writer of `tenant_ids`;
  - the owner name is self-declared: `owner=` unchecked (`_claims.py:262`), an app module named
    `identity` (boot probe run 2), a binding key text (T6); B020 applies only when an installed
    package provides the name (`core/descriptors/module_names.py:136-164`);
  - the mint authority is the signing key, not the issuer declaration (S5, T4): code in a
    process holding the key calls `create_access_token` with any claims and provenance;
  - `email`, `email_verified` are protected only by identity's mint order, not by the kernel
    (guarantee-map §13).
- Missing proof: an application assigns the authority of `roles` to a test identity module
  outside `src/`; a second module contributing `roles: ["admin"]`, one passing
  `owner="identity"`, one named `identity`, and a remote binding named `identity.claims.v1` to
  an unassigned service each get a startup refusal or a refused mint with a log line, with and
  without `sandloom_identity` installed, mono and split.

### I-09 A gate trusts a claim only from its assigned authority

A gate (platform admin, `Role`, `Perm`, `OrgRole`, `Subscribed`, `Feature`, `WithinLimit`,
claim-mode facts) reads a security claim only when the authority the application assigned
(I-08) is in `claims_sources` and not in `claims_sources_failed`. With no authority assigned,
loaded or bound, the gate denies (403) or is indeterminate, never allows. Every platform-admin
entry point gives the same decision for the same token (goals.md: one platform admission
shared by `require_platform_admin` and `platform_operator`). The same holds in a monolith and a
split.

- Guarantees: G-AUTH-12, G-OPCLASS-07, G-SENS-01, G-REVOKE-01, G-TENANT-13.
- Proof today: forged admin without the identity source is 403 on every platform router:
  `src/sandloom/core/tests/test_admin_operations.py:56` (line `:63`); provenance for facts:
  `tests/integration/test_claims_provenance_and_subscribed.py:77`, `:100`, `:161`;
  operator routes deny tenant accounts: `src/sandloom_billing/tests/test_platform_admin.py:38`.
- State: violated (unchanged by PR #70, which touches only the merge):
  - `Role`, `Perm` read `subject.roles` / `permissions` with no provenance
    (`shared/authz/atoms_claims.py:35-67`; D3, S1);
  - `Subscribed(of=Account.user())` with no `subscription.active` fact anywhere reads
    `subscription_status` from any writer (`atoms_claims.py:184-188`; D2, S1, S6);
  - `Feature`, `WithinLimit` read `features` / `limits` from any writer
    (`atoms_claims.py:304-363`; guarantee-map §5);
  - the platform gates trust the name `"identity"` (B-S1, B-S2), which an app module named
    `identity` (boot probe run 2) or `owner="identity"` (S0, S4) satisfies;
  - the two platform gates are separate code (B-S1, B-S2); no test asserts they decide alike.
- Missing proof: `require(Role("admin"))` with `roles: ["admin"]` from an unassigned writer
  answers 403; `require_platform_admin` and `platform_operator` give the same answer over a
  table of tokens; `Subscribed` with billing absent and `subscription_status: "active"` from
  another module does not allow without another sufficient condition.

### I-10 The tenant is bound only from a trusted grant in the signed token

`TenantScope` selects a tenant only inside the signed token's tenant grant written by the
tenant authority (I-08); a malformed entry is 400; a revoked token is 401 before any tenant is
selected; RPC binds the tenant only from the MAC-covered context; no tenant is accepted from a
header without a trusted grant (goals.md, "Optional package, optional capability, security
guarantee").

- Guarantees: G-TENANT-10, -11, -12, -13; G-RPC-10.
- Proof today: `src/sandloom/core/tests/test_tenant_isolation.py:236`, `:316`;
  `src/sandloom/core/tests/test_auth_rotation_revocation.py:279`; at `44451e4` a contributor
  writing `tenant_id` or `org_id` refuses the mint
  (`test_claims_key_ownership.py::test_no_owner_writes_a_reserved_key`, `:233`).
- State: at `b8d8506` **violated**: any contributor writing `tenant_id` got that tenant bound
  with no header and accepted for `X-Tenant-Id` (guarantee-map §6, S3, boot probe run 2;
  `core/http/tenant_scope.py:28`, `:111-119`). At `44451e4` **holds** for contributors
  (`tenant_id`, `org_id` reserved, commit `e212893`). Open: who may write `tenant_ids` is
  assigned by nobody (I-08, T2); without tenancy the first writer owns it.
- Missing proof: with tenancy absent, a module writing `tenant_ids` is not trusted unless the
  application assigned it as the tenant authority (C-H6).

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
- The compatibility comparison runs in every install that has a consumer, including one
  without the provider's package; where it cannot run, I-20 applies.
- Generated artifacts regenerated with an unchanged contract: see I-15.

- Guarantees: G-COMPAT-02, -04, -05, -06, -07; G-CONTRACT-10, -12, -16; G-DECL-04.
- Proof today: `src/sandloom/core/tests/test_contract_compat.py:145`, `:303`, `:322`, `:368`;
  `src/sandloom/core/tests/test_contract_parity.py:426`;
  `src/sandloom/core/tests/test_contract_codec_v2.py:331`;
  `src/sandloom/core/tests/test_consumes_preflight.py:61`, `:111`;
  `tests/sdk/test_sdk_contracts.py:137`.
- State: holds, partial proof, in the repository environment. **Violated in a consumer-only
  install** (D4): the provider's snapshot is looked up in the provider's package
  (`core/contract_compat.py:139-153`); without it the consumer logs a WARNING, compares
  nothing, and starts also under `CONTRACT_COMPAT_CHECK=strict` (compat probe:
  `expected_contracts -> {}`). G-COMPAT-03 records a gap: no test drives
  `sandloom contracts check --against http://<provider>` or the snapshot CLI exit codes.
  No test runs two released versions of one module package in two processes.
- Missing proof: a matrix test with module package versions N and N+1 installed in
  separate processes, both orders, over the split conformance script; a consumer-only install
  where the provider lacks a declared method fails `/ready` (B-F5).

### I-13 A new module needs no kernel change

Adding a module — including one with its own contract, events, claims contributor,
access facts, typed authorization policies, settings, accepted risks and admin resources —
changes no file under `src/sandloom/` except generated artifacts whose home decision B sets.

- Guarantees: C-GEN-08, G-MODULE-18, G-MODULE-19, C-MODULE-12; ADR-0010 consequence
  "framework edits 0" (`docs/adr/0010-public-api-1.0.md:312-314`, `:332-334`).
- Proof today: `tests/sdk/test_sdk_generators.py:124`;
  `src/sandloom/core/tests/test_module_generation.py:267`; DX eval metric
  `framework_edits` (`benchmarks/dx/run_eval.py:368`).
- State: holds for contracts, events, consumers, facts and claims contributors. Violated
  for settings (one `BaseSettings`, `core/config/settings.py:113`; settings probe 3), accepted
  risks (closed registry, settings probe 2, T10) and typed policies with domain parameters,
  which today live in the kernel (B-N12, B-N13).
- Missing proof: a fixture module package outside `src/` with a setting, an accepted risk, a
  claim, a fact, a typed policy and an admin resource; it boots, its tests and the ledger pass,
  and `git diff --stat -- src/sandloom` is empty.

### I-14 Authorization and the hot path keep their cost

Three parts (Codex review 2026-11-08; owner, tm reflection on `kb-requirements-amend`
2026-11-08):

1. **Hard: no new I/O on the claims tier, and the tiers stay.** A claims-only policy opens no
   database session and makes no network call. A local fact (provider in the same process)
   makes no RPC. A remote fact makes an RPC only when its owner is in another process. No
   discovery or `describe()` call runs on the request path when the fact's metadata is already
   known.
2. **Measured.** Every alternative that touches the request, unit-of-work, eventing,
   authorization or mint path reports, base build against candidate build, per operation:
   CPU instructions (cachegrind), latency p50/p95/p99, and RPC and database call counts.
   Instructions alone are not enough for distributed authorization. The method is
   repeatable (fixed commits, scripted runs, alternating base/candidate rounds) and states
   its noise tolerance, measured from base-against-base runs.
3. **Threshold.** A change beyond the noise tolerance and beyond the regression threshold
   fails, unless the owner approves it. The threshold today: at most +1 % instructions per
   operation on the request and write rungs (`docs/adr/0010-public-api-1.0.md:328-331`);
   thresholds for authorization, mint and latency: **[Owner]** (Q14). **A security fix is not
   blocked by the budget**: its cost is measured and reported, and the owner decides whether
   to optimise later.

- Guarantees: G-AUTHZ-02 (claims-only policy opens no DB session, does no I/O); G-AUTHZ-03.
- Proof today: `tests/integration/test_shared_authz_review.py:426` for G-AUTHZ-02 (part 1,
  claims tier). Instruction counts and latency are measured by hand
  (`benchmarks/ladder/cg/run_cg.sh`; procedure with alternating rounds, p50/p99 at fixed rates,
  `benchmarks/README.md:270-291`); no CI job runs them; no rung for authorization or mint.
- State: part 1 holds for the claims tier, no proof for the local and remote fact tiers' call
  counts or for the absence of `describe()` on the request path; parts 2 and 3: no automated
  proof today, no stated noise tolerance, p95 not reported by the procedure.
- Missing proof: call-count assertions per tier (claims, local fact, remote fact) in mono and
  split; cachegrind and latency runs of the request and write rungs plus an authorization rung
  (claims-only, local fact, remote fact) and a mint rung, base and candidate, with the noise
  tolerance and p50/p95/p99.

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
- State: holds. Note: some ledger entries hold only while a battery is present (D1: G-REVOKE-07,
  -08; D2: G-AUTH-12 for `Subscribed`); I-20 covers them.

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
  an API decision (`src/sandloom/core/AGENTS.md`, "Adding a name is an API decision"), and
  moving `from sandloom.sdk import OrgRole` to another import is a DX change to be judged, not
  assumed better (owner, tm reflection on `kb-requirements-amend` 2026-11-08; H-19).

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
  No proof for a setting declared outside the kernel: a third-party module reads the
  environment itself, so tiers, overrides, risk logging, `config dump` and the profile-equality
  check do not cover it (guarantee-map §10, settings probe 3); its production risk cannot be
  accepted (T10).
- Missing proof: the fixture module of I-13 declares an ops setting, a semantic setting, a
  secret, an accepted risk and a production-only refusal; each tier, redaction, entry-point
  and profile test sees them.

### I-19 Every allowed install profile and every module subset boots

Allowed install profiles:

- **P1** `sandloom` alone (I-01);
- **P2** `sandloom` plus any subset of module wheels, each with only the dependencies its
  published metadata declares;
- **P3** the published extras (`sandloom[standard]`, `sandloom[admin]`) and the `sandloom new`
  template's dependencies.

In each profile every bundled module, and every subset, boots and becomes ready, or refuses at
startup with a defined code and a message naming what is missing; the only refusal allowed
today is `ContractBindingError` for a required consumed contract. **[Owner]** (Q10): whether a
module wheel in P2 may refuse with a message naming a missing extra, or must boot.

- Guarantees: G-MODULE-15, C-SPLIT-03, C-MODULE-06, C-MODULE-07, G-OPS-21.
- Proof today: `src/sandloom_identity/tests/test_module_wiring.py:26` (and tenancy,
  billing); `src/sandloom/core/tests/test_module_boots_alone.py:68`;
  `src/sandloom/core/tests/test_module_subset.py:54`;
  `src/sandloom/core/tests/test_split_readiness.py:231`. All run in the repository
  environment, where the `celery` extra is installed (P3).
- State: **violated in P2.** `pip install sandloom sandloom-billing` installs, then bootstrap
  fails: `ModuleLoadError: Required modules failed: sandloom_billing: No module named
  'celery'` (`sandloom_billing/tasks.py:25` imports `sandloom.sdk.tasks`, which imports
  `celery` at module level, `sdk/_prim/tasks.py:16`; billing depends on plain `sandloom`,
  `packages/sandloom-billing/pyproject.toml`) (baseline §13.2, `_evidence/consumer-install.txt`;
  kb-baseline-close). Identity: inferred, not run (baseline §12). Holds in P1 (baseline §13.1)
  and P3. Packaging fix tracked as tm `billing-boots-without-celery`.
- Missing proof: a packaging check that installs each module wheel on plain `sandloom` (P2)
  in a clean venv and boots it, for billing, identity, tenancy and admin.

### I-20 No platform guarantee degrades silently

When the module that implements or supplies a platform protocol is absent (not installed, not
loaded, not bound), each guarantee either still holds (fail closed: deny, indeterminate, 501,
503) or the process refuses to start naming what is missing. A warning, a log line or a metric
alone is not enough. An optional package is not an optional capability, and a security
guarantee is never optional (goals.md, "Who owns what" and "Optional package, optional
capability, security guarantee"; owner, tm reflection on `kb-requirements-amend`).

- Guarantees: G-REVOKE-07, G-REVOKE-08, G-AUTH-12, G-COMPAT-04, G-COMPAT-06 (the ones that
  degrade today); every guarantee whose provider column in guarantee-map §2 names a battery.
- Proof today: fail-closed paths are tested where they exist: live atoms 501 without the owner
  (G-AUTHZ-01, -07); platform gate 403 without identity
  (`src/sandloom/core/tests/test_admin_operations.py:56`); required contract refuses startup
  (G-CONTRACT-12). No test enumerates the guarantees per absent battery.
- State: **violated** (guarantee-map §11):
  - D1: identity loaded nowhere → no durable applier of `core.auth.tokens_revoked.v1` and no
    `/ready` "revocation stuck" signal; a mark lost after the post-commit retries is only
    counted (`auth_revocation_deferred_total`; `core/auth/revocation.py:502-511`,
    `core/app_factory/ops.py:206`). Consumer absence verified, loss path inferred;
  - D2: billing absent → `Subscribed` reads `subscription_status` from any writer (S1, S6);
  - D3: identity absent → `Role`/`Perm` trust the first writer of `roles`/`permissions` (S1);
  - D4: provider package absent → no compatibility comparison, also under `strict` (compat
    probe); a WARNING only.
  Not a guarantee loss but recorded (guarantee-map §11): `/ready` 200 while the default
  taskqueue cannot open (tasks probe); a replacement serving `identity.security.v1` gets the
  fallback password policy silently (B-I1, inferred).
- Missing proof: for each battery (identity, tenancy, billing, admin) and for a consumer-only
  install, a test per guarantee of guarantee-map §2 that observes fail-closed or a startup
  refusal, never a warning alone (A-F5, B-F5, C-F5).

### I-21 The kernel keeps the general security and isolation protocol

No alternative moves a general protocol out of the kernel into a battery: the authenticated
subject; trusted claims and the verification of their issuers; rights (`Role`, `Perm`); the
tenant, `TenantScope` and tenant isolation; the administrative operation and the platform
admission; token minting, verification, revocation and blocking; the authorization mechanism
(`Requirement`, `&` `|` `~`, allow / deny / indeterminate, evaluation order and short-circuit,
fail-closed and denial categories, `require`, `authorize`, `AccessFact`, `Entitled`). Each is
stated in kernel terms, with no module's names or data model (goals.md, "What the rule means",
"Authorization: the mechanism stays in the kernel", "How to apply it": pushing a general
protocol out weakens the guarantees).

- Guarantees: G-AUTH-*, G-REVOKE-*, G-BLOCK-*, G-TENANT-*, G-RLS-*, G-AUTHZ-*, G-OPCLASS-07.
- Proof today: the provider column of guarantee-map §2-§6 names kernel code for each of them.
  No check asserts it.
- State: holds, except two duties of the revocation protocol that only identity performs: the
  durable applier of `core.auth.tokens_revoked.v1` (`sandloom_identity/__init__.py:73-84`) and
  ending refresh sessions on `core.auth.user_blocked.v1` (`:63-71`); nothing names or checks
  the duty (guarantee-map §3, D1, T8). **[Owner]** (Q12): kernel duty or issuer duty the
  kernel verifies.
- Missing proof: each ADR lists, per component above, where it lives after the change; a PoC
  that moves one is disqualified (H-14).

### I-22 The application assigns every trust decision; the kernel verifies it

For each trust decision of guarantee-map §12, the application can state its choice in trusted
configuration or composition, and the kernel checks that the party acting is the one assigned.
A module's own name, argument, binding key text or `describe()` answer is never the proof
(goals.md "Guarantee, provider, trust"; owner, tm reflection on `kb-assess` 2026-11-08).

| # | trust decision (guarantee-map §12) | state |
|---|---|---|
| T1 | authority of `roles` / `permissions` | violated: platform gates by the name `identity`; `Role`/`Perm` any writer; one writer per token since PR #70 (I-08, I-09) |
| T2 | tenant authority (`tenant_ids`) | violated: no assignment; `tenant_id`/`org_id` reserved since `e212893` (I-10) |
| T3 | authority of plan claims (`subscription_status`, `features`, `limits`) and of `subscription.active` | violated: kernel defaults, `WELL_KNOWN_FACT_OWNERS` maps the fact to `billing` (`shared/authz/facts.py:526-541`); scope: Q9 |
| T4 | who may mint tokens | violated: `issues_tokens=True` drives key placement only; `create_access_token` has no caller check (S5); scope: Q13 |
| T5 | which provider answers a domain contract in-process | violated: first registrant of the name, process-wide live-adapter slot (`core/facade/_services.py:53-69`; baseline A19); remote holds (`SERVICE_BINDINGS`) |
| T6 | the name a claims source carries in provenance | violated: short name, `owner=`, or binding key text (I-08) |
| T7 | the owner of a remote access fact | violated: the provider's own `describe()` (`facts.py:486`, `:503`); read, not run |
| T8 | who applies durable revocations and ends refresh sessions on block | violated: identity's consumers, unnamed duty (D1, I-21) |
| T9 | the operator contract for revocation and admin bootstrap | violated: bound to `identity.security.v1` by name (B-R4, B-R5) |
| T10 | accepting a third-party module's production risk | violated: closed kernel risk registry (I-13, I-18) |

Holds today: which service answers which contract remotely, and who may call what
(`SERVICE_BINDINGS`, `service_id` pin, `RPC_ALLOWED_CALLS`; guarantee-map §7).

- Guarantees: G-RPC-* (the remote assignment); the rest have none.
- Proof today: for the remote assignment only (G-RPC ledger tests).
- State: violated for T1-T10 as in the table.
- Missing proof: one test per row in scope (Q13 for T4): an unassigned party acting is refused
  or ignored; the assigned one works; mono and split.

### I-23 Moving a domain atom changes no execution

Moving a domain atom (`OrgRole`, `Subscribed`, `SubscriptionActive`, `Credits`, or any other)
out of the kernel does not by itself change, for the same policy and the same deployment: the
number of network calls, the number of database queries, the evaluation order including
short-circuit, the decision, or the user-facing policy syntax
(`require(OrgRole("admin") & SubscriptionActive(of=...))`). (Owner, tm reflection on
`kb-requirements-amend` 2026-11-08; goals.md, "Authorization: the mechanism stays in the
kernel".) Whether the import line of an atom may change is a DX question (H-19, Q5).

- Guarantees: G-AUTHZ-02, -03, -13.
- Proof today: none comparing two placements of one atom.
- State: no proof today (nothing has moved).
- Missing proof: C-B6: the same route before and after the move, mono and split, with call
  counts, query counts, the atoms evaluated in order, and the decision recorded and equal.

## 4. Hard constraints and trade-off criteria

### 4.1 Hard constraints

An alternative that breaks one is disqualified; no score compensates.

| id | constraint | source |
|---|---|---|
| H-1 | Every invariant I-01..I-23 holds after the change, or the owner accepted the exception in writing. An invariant violated today (I-02, I-03, I-08 assignment, I-09, I-11, I-12 consumer-only, I-13, I-19 P2, I-20, I-22; I-21 for D1) must hold after the decision or task that owns it. | this file; ADR-0010:22-23 |
| H-2 | No guarantee weakens and no capability is removed (I-16). | ADR-0010:22-23 |
| H-3 | Modules stay isolated: no module-to-module import, cross-module work only through events, contracts, projections, sagas. | `AGENTS.md` "The one rule" |
| H-4 | The authorization algebra is unchanged and fail-closed (I-07): `A \| B` with `A` indeterminate and `B` allow stays allow; `~indeterminate` never allows. | G-AUTHZ-13; ADR-0010 owner decision 4; tm reflection on `kb-alt-authz` |
| H-5 | No gate trusts a security claim from a weaker source than today; after decision C a gate trusts it only from the authority the application assigned and the kernel verified (I-08, I-09, I-10, I-22). | G-AUTH-12, G-OPCLASS-07, G-TENANT-10; goals.md |
| H-6 | The same module code runs as a monolith and as any split, by configuration only. | C-SPLIT-01; positioning row 9 |
| H-7 | Business code stays independent of infrastructure: no backend SDK or driver in modules; capabilities stay contracts. | positioning row 10; C003, C004 |
| H-8 | `sandloom.sdk` stays the only public surface for modules; its import has no side effects; the kernel never imports it (I-17). | ADR-0010 decision 1; G-SDK-01, G-SDK-02 |
| H-9 | The framework carries no deprecated API and no compatibility alias. A breaking change in a prerelease ships with upgrade steps. | G-KERNEL-12; ADR-0010:75; `versioning.md:50-54` |
| H-10 | (a) No new network call or database session on the claims-only authorization path, and the three execution tiers stay (I-14 part 1). (b) Every alternative reports its measured cost by the method of I-14 part 2. (c) A change beyond noise and the regression threshold of I-14 part 3 fails unless the owner approves it; a security fix is never blocked by (b) or (c). | ADR-0010:328-331; Codex review 2026-11-08; owner, tm reflection on `kb-requirements-amend` |
| H-11 | Generated files stay generated: never hand-edited, written only by `sandloom generate`, checked by `sandloom test` (I-15). | `AGENTS.md` golden path |
| H-12 | Each new mechanism (definition: §4.2 "Runtime complexity") either removes a confirmed violation (a baseline edge, D1-D4, T1-T10, by id) or provides a required invariant (by id) that no existing mechanism provides at lower cost; the alternative names the existing mechanisms it compared against. A mechanism that does neither disqualifies the alternative until it is dropped. | Codex review 2026-11-08; kb-requirements reflection (4); tm reflection on `kb-assess` (minimal sufficient alternative) |
| H-13 | Each proposed change cites the invariants it serves. | kb-requirements reflection (1) |
| H-14 | No general protocol of I-21 leaves the kernel. | goals.md "How to apply it"; owner, tm reflection on `kb-requirements-amend` |
| H-15 | No silent degradation (I-20): without a battery, each guarantee fails closed or the process refuses to start naming what is missing. | goals.md "Who owns what"; owner, tm reflection on `kb-requirements-amend` |
| H-16 | Moving a domain atom changes no network calls, database queries, evaluation order or policy syntax (I-23). | owner, tm reflection on `kb-requirements-amend` |
| H-17 | No second driver system for business modules (no mandatory identity, tenancy, billing or admin driver); no universal capability registry until an existing mechanism (module registration, contracts, claims contributors, access facts, drivers) shows a proven limit, shown by a failing test or replaceability experiment. | goals.md "The platform capability rule", limits; tm reflection on `kb-assess` |
| H-18 | The kernel defines a new port only when all six conditions of the platform capability rule hold (platform guarantee, no domain specifics, testable semantics of execution and failure, replaceable without a kernel change, defined absence, local and RPC where needed). | goals.md "The platform capability rule" |
| H-19 | Developer experience: one authoring API for local and remote atoms and contracts; a route's code unchanged from monolith to split; a policy whose fact or contract owner is neither local nor bound is reported at bootstrap or preflight, not first found by a request; generation runs only when a public declaration changes, not on every business change. | owner, tm reflection on `kb-requirements-amend` (DX) |

State of H-19 today: one API and unchanged routes hold (H-6, C-SPLIT-01); a required consumed
contract is found at startup (B-F2), but a live atom's absent owner is found only at the
request (501, `shared/authz/atoms_live.py:57-73`); generation runs when declarations change
(`sandloom generate`, I-15).

**[Owner]** approves H-1..H-19 (Q2).

### 4.2 Trade-off criteria

Alternatives that pass every hard constraint are compared on these, by scenario (pass /
fail / at what cost), not by weighted score.

| criterion | measured as |
|---|---|
| Correctness margin | scenarios of section 5 passed with no extra operator or author step |
| Coupling | baseline edges left, by class (a/b/c) and type (import, package, runtime call, configuration, security authority, naming); edges added |
| Runtime complexity | **count of new mechanisms**, each with the violation it removes or the invariant it provides, and the existing mechanism it was compared against (H-12, H-17). A mechanism is any one of: a new registry or registration call, a new entry-point group, a new kind of generated artifact, a new contract kind or wire format, a new settings source, a new startup check family, a new package or wheel, a new CLI command, a new process-wide default. Also: new startup work and new per-request work. |
| Developer and agent experience | beginner names (30 today); concepts on the golden path (9-10, ADR-0010:312); files an author edits to add a module; kernel files an agent reads in the DX eval (target 0, `benchmarks/dx/README.md:207`); new error codes, each with `sandloom explain`; typed atoms with autocompletion and errors before run; whether the CLI and generators maintain consumer interfaces for agents; import changes for existing policies (judged, not assumed better). **Comparative DX task**, run on the base and each alternative: create a module with a typed `QuotaAvailable` atom, wire it to a route, test it, split provider and consumer; count files, manual steps, errors, documentation lookups and kernel changes. |
| Packaging and versioning | wheels released for one contract change; dependency edges between wheels; extras that pull module wheels; install profiles of I-19 that boot |
| Migration cost | files changed in bundled modules, examples, generated apps, docs; upgrade steps an app owner runs |
| Performance | by I-14 part 2: instructions, p50/p95/p99 latency, RPC and database call counts per operation (request, write, authorization per tier, mint), base against candidate, with the noise tolerance |
| Operations | settings an operator sets for monolith and for split, including trust assignments (I-22); new startup refusals an operator can hit |

## 5. Quality scenarios

Each scenario: id, stimulus and environment, expected observable outcome, invariants. A PoC
passes a scenario only when the stated outcome is observed. Environments:

- **mono**: one process, every bundled module.
- **split**: the shipped microservices profile (C-DEPLOY-03), one service per module.
- **lean**: clean venv with the `sandloom` wheel only.
- **P2**: clean venv with `sandloom` and chosen module wheels, each with only its declared
  dependencies (I-19).
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
| A-H5 | Identity replaced: a test identity module outside `src/` issues roles through the claims mechanism, and the application assigns it as the authority of `roles`; `sandloom_identity` not installed (mono, then split) | its `admin` passes `require_platform_admin`, `platform_operator` and `Role("admin")`; a second module contributing `roles: ["admin"]` cannot raise anyone's rights; no kernel file changes (goals.md "Replaceability decides the scope") | I-08, I-09, I-13, I-22 |

**Boundary**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| A-B1 | Split deploy without the owner module: a service loads billing only, with bindings to identity and tenancy; then a service with no binding to identity | every request of the conformance script answers as in mono, or with an explicit 404/501/503; never 500, never a different 2xx; `/api/v1/platform/billing/*` answers 403 to every caller when no identity authority is bound | I-06, I-09, I-19 |
| A-B2 | `sandloom admin create` and `sandloom security revoke-sessions` with identity not installed (lean) | non-zero exit with a message naming the missing module or contract; no traceback; the output names the password policy it applied | I-01, I-02, I-20 |
| A-B3 | An app module named `identity` (not `sandloom_identity`), `sandloom_identity` not installed, contributes `roles: ["admin"]` without being assigned as the authority | the minted token carries no trusted `admin` role from it, or startup refuses it with a code; every platform route answers 403 to that user | I-08, I-09, I-22 |
| A-B4 | `sandloom` upgraded by one release while module wheels stay (mono and split); then one module wheel upgraded while `sandloom` stays | each combination inside the declared version ranges boots and passes the conformance script | I-11, I-12 |
| A-B5 | `SANDLOOM_MODULES` names the bundled modules in a different order | same app, same routes snapshot, same generated files | I-15, I-19 |
| A-B6 | A contributor writes a reserved key (`tenant_id`, `org_id`, `user_id`, `iat_us`, `claims_sources`), mono and split | mint refused 503 with a log line naming the contributor and key (holds at `44451e4`) | I-08, I-10 |
| A-B7 | Each module wheel installed on plain `sandloom` with only its declared dependencies (P2): billing, identity, tenancy, admin | each boots and becomes ready, or refuses at startup with a code naming the missing dependency (Q10); never a `ModuleLoadError` traceback | I-19 |

**Failure**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| A-F1 | A kernel change adds `import sandloom_billing`, `import_module(f"sandloom_{n}")`, a constant `"sandloom_billing.x"` passed to `import_module`, or the literal `"billing.entitlements.v1"` in `core/` | `sandloom test` fails with a code and the `file:line`; the generic loader's descriptor-sourced imports (A2, A7, A10-A16) pass | I-02, I-03 |
| A-F2 | A non-authority module's contributor returns `{"roles": ["admin"]}` (mono and split, with and without identity) | refused at registration, or the mint is refused 503 with a log line; `require(Role("admin"))` and `require_platform_admin` answer 403 for that user | I-08, I-09 |
| A-F3 | A module calls `add_claims_contributor(fn, owner="identity")` without being the assigned authority | startup refuses with a code naming the module and the claimed owner | I-08, I-22 |
| A-F4 | Identity's critical contributor fails at mint | mint answers 503 `ClaimsUnavailable`; no token issued; a token carrying `identity` in `claims_sources_failed` gets 403 on platform routes | I-09; G-AUTH-11 |
| A-F5 | Identity loaded nowhere (tenancy alone, or a replacement issuer), a member is removed, and the revocation mark's post-commit write keeps failing | the mark is applied durably, or `/ready` reports it stuck, or startup refused because no applier is configured; never only `auth_revocation_deferred_total` (D1) | I-20, I-21 |

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
| B-F5 | Consumer-only install (provider's package absent), `CONTRACT_COMPAT_CHECK=strict`, the deployed provider lacks a method the consumer declares | the comparison runs and `/ready` fails with `contract_incompatible`, or startup refuses naming the contract it cannot check (Q11); never a start with a WARNING only (D4) | I-12, I-20 |

### 5.C Authorization extension

**Happy**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| C-H1 | A fixture module adds a typed policy for its own domain and an app route uses it inside `And`, `Or`, `Not` | no file under `src/sandloom` changes; the law properties of I-07 pass over the new policy; the route snapshot records it | I-07, I-13 |
| C-H2 | Existing billing gates, mono and split: `Subscribed(of=Account.user())`, `Credits(1, of=Account.org(param="org_id"))`, `OrgRole("admin")` | same statuses as today: 402 on a definite billing denial, 403 on a role denial; claims-only policy opens no DB session; live policies call the owner locally or over the binding | I-06, I-07, I-14 |
| C-H3 | A third-party alternative billing package (not `sandloom_billing`) answers subscription and credit questions; `sandloom_billing` not installed | the billing gates of the conformance script give the same HTTP outcomes as with `sandloom_billing`; no kernel file changes; **[Owner]** decides (Q5) whether app gate code may change when the billing package is swapped | I-03, I-13 |
| C-H4 | Organization membership gate with tenancy, mono and split | same decision in both | I-06 |
| C-H5 | Billing replaced by an unrelated fact: a fixture module owns `inventory.stock.available`, used by a route locally (mono) and remotely (split) | same decisions in both; no kernel file changes; `OrgRole("admin")` in a consumer process without the tenancy implementation installed works through the remote provider (goals.md "Replaceability decides the scope"; tm reflection on `kb-alt-authz`) | I-06, I-07, I-13 |
| C-H6 | Tenancy replaced by an independent tenant-grant provider assigned as the tenant authority, mono and split | RLS isolation holds; a revoked grant denies; the provider down fails closed; no tenant accepted from `X-Tenant-Id` without a grant; no kernel file changes | I-10, I-13, I-20, I-22 |

**Boundary**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| C-B1 | A fact gains a parameter: the owner adds an optional parameter in a new release | callers without the parameter get the same decision as before; a caller that passes it to an old owner (split) gets 501 or 503, never allow | I-07, I-12 |
| C-B2 | The kernel lacks the fact owner: a policy names a fact whose owner is neither loaded nor bound | 501 at the request; inside `Not` or `Or` the route never allows on that branch; the missing owner is also reported at bootstrap or preflight (H-19) | I-07 |
| C-B3 | JWT from an old identity: a token minted before the upgrade, without a claim the new version reads, or without `claims_sources_failed` | the missing claim is unknown: the policy escalates to a live check or is indeterminate; never allow on a missing claim; platform routes answer 403 to a token without the authority in provenance | I-07, I-09 |
| C-B4 | `Account.bound_tenant()` with tenancy not deployed | tenant kind unknown, decision indeterminate (501), never the user's account | I-07; G-AUTHZ-11 |
| C-B5 | Organization `Feature` or `WithinLimit` with no organization fact registered | 501 (G-AUTHZ-12) | I-07 |
| C-B6 | One atom moved out of the kernel: `require(OrgRole("admin") & SubscriptionActive(of=Account.org(param="org_id")))` before and after the move, mono and split, first atom denying and then allowing | equal decision, equal RPC and database call counts, equal atoms evaluated in equal order (short-circuit on the first deny), policy expression text unchanged | I-14, I-23 |
| C-B7 | Execution tiers: a claims-only policy, a local-fact policy (owner in process), a remote-fact policy (owner in another process) with known metadata | claims: 0 database sessions, 0 network calls; local fact: 0 RPC; remote fact: only the fact call, no `describe()` or discovery call on the request | I-14 |

**Failure**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| C-F1 | Billing down during authorization (split, billing bound but unreachable) | live policy: 503; `Not(live policy)`: 503, not allow; claims-only policy with billing trusted in provenance: decided from claims, no call; with billing in `claims_sources_failed`: escalates to live, 503 | I-07, I-09 |
| C-F2 | A module's policy raises an exception during evaluation | indeterminate or 5xx, never allow; the denial is recorded in the deny log and `access_denied_*` metrics | I-07; G-AUTHZ-08 |
| C-F3 | Two modules register a fact or policy under the same name | startup refuses with a code naming both modules; no last-wins | I-07, I-08 |
| C-F4 | A module contributes a claim key owned by another module (`tenant_ids` from billing, `subscription_status` from a non-billing module), with and without the owner loaded | refused or the mint refused with a log line; a gate reading that key trusts only its assigned authority's provenance | I-08, I-09, I-10 |
| C-F5 | Battery absent: billing not loaded and not bound, another module writes `subscription_status: "active"`; identity not loaded, another module writes `roles: ["admin"]` | `Subscribed` and `Role("admin")` do not allow without another sufficient condition (D2, D3) | I-09, I-20 |

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
| A | 5 | 7 | 5 | 17 |
| B | 4 | 5 | 5 | 14 |
| C | 6 | 7 | 5 | 18 |
| D | 4 | 5 | 4 | 13 |

## 6. Questions the owner must answer

Answered since the first draft, and dropped: the ceiling on new mechanisms (replaced by the
rule of H-12 and the limits of H-17; Codex review and goals.md); whether I-08/I-09 may break
apps in `0.x` (PR #70 shipped the one-owner rule with upgrade steps and a release note,
`1702f68`; goals.md requires provenance before `Role`/`Perm` stay in the kernel); the scope of
the CLI, starter, recipes and `sandloom.shared` removal (after A-D, kb-assess description).
The reserved-claim question (old Q9) is answered for the reserved keys by PR #70 and remains
for plan claims as Q9; the budget question (old Q11) is now Q14.

1. Approve the invariant list I-01..I-23, including the state recorded for each.
2. Approve the hard constraints H-1..H-19.
3. Kernel tests (`src/sandloom/core/tests`, not shipped): are they inside I-02 and I-03, or
   may they import bundled modules (baseline §6.2)?
4. B-H3: after a generator change with the contract unchanged, must the generated files stay
   byte-identical, or is a reported diff acceptable as long as older artifacts keep working
   with the newer kernel inside one minor version?
5. C-H3 and I-23: when an app swaps the billing package for a third-party one, may the app's
   gate code change? May the import of an atom (`from sandloom.sdk import OrgRole`) change
   when the atom moves, if the policy expression stays the same?
6. D-B1: when a module's environment variable is set in a process without that module, log
   a warning or stay silent?
7. D-B3: do a third-party module's semantic settings count toward the framework's ratchet of
   16?
8. D-F4: an accepted-risk id owned by a module that is not installed: an unknown id
   (startup refusal) or inert?
9. I-08 and T3: which claims are security-sensitive and need an application-assigned
   authority: `roles`, `permissions`, `tenant_ids` only (goals.md), or also
   `subscription_status`, `plan_code`, `features`, `limits`? Should the kernel also reserve
   `email` and `email_verified` for the issuer (today protected by identity's mint order only)?
10. I-19: in install profile P2, must a module wheel boot with only its declared
    dependencies, or may it refuse at startup with a message naming a missing extra?
11. I-20 and D4: in a consumer-only install, when the compatibility comparison cannot run,
    must startup refuse (at least under `strict`) or `/ready` fail? Is `/ready` 200 with an
    unusable default taskqueue acceptable?
12. I-21 and D1: is the durable application of revocation marks, and ending refresh sessions
    on block, a kernel duty, or a duty of the token issuer that the kernel verifies is
    fulfilled?
13. I-22 and T4: is minting by other code inside a process that holds the signing key in
    scope (the kernel checks who mints), or is the signing key the trust boundary?
14. I-14: the regression thresholds beyond ADR-0010's +1 % instructions on the request and
    write rungs: for authorization evaluation and minting, and for p95/p99 latency; the
    accepted noise tolerance; a CI gate or a run per ADR?
15. I-06: approve the backend exception list (today SQLite nested write units only).
