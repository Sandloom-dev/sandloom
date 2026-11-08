# Kernel boundary requirements: invariants, constraints, quality scenarios

Status: **approved by the owner on 2026-11-08, conditionally** (answer #1 of
[owner-answers.md](owner-answers.md)): I-01..I-23 and H-1..H-19 are the target requirements
after the refinements the answers make, which are applied below and listed in "Changed by the
owner's answers". The approval covers the requirements, not any proposed mechanism or
exception. The A-D target is decided after the alternatives and PoCs. Items still waiting for
the owner carry **[Owner]**; §6 lists them.

Commit: `b8d8506` (branch `arch/kernel-boundary`). Every `file:line` is at that commit,
except `core/facade/_claims.py` and `core/tests/test_claims_key_ownership.py`: PR #70
(`roles-claim-trust`, merged into `main` as `e264b09` and into this branch as `44451e4`)
changed them, and they are cited at `44451e4`. No other file under `src/` changed since
`b8d8506` (`git diff b8d8506 44451e4 --stat -- src/`), and nothing under `src/` changed from
`44451e4` to `de53875` (`git diff --stat 44451e4 de53875 -- src/` is empty), so the
`44451e4` citations hold at the branch head (roadmap-review RA18).

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

**Amended again 2026-11-08** (tm `kb-requirements-answers`): the owner's answers to the 31
consolidated questions ([owner-answers.md](owner-answers.md), cited below as "answer #n"; the
small fixes as "#A", "#B"; the conditions to continue as "condition n") and the roadmap-review
amendments that agree with them (RA1-RA18, [roadmap-review.md](roadmap-review.md) §6.1; the RA
items not applied are listed in §6.3).

## Changed by the owner's answers (2026-11-08)

| item | change | answer / amendment |
|---|---|---|
| status, §6 | approved conditionally; the questions replaced by the answers; open items listed | #1 |
| §1 | one active composition per production process; the entry points that do not build the app composition named | #8, #B, preamble |
| I-02, I-03 | kernel tests are outside the shipped boundary; kernel unit and architecture tests run without batteries; templates, recipes and docs judged apart from runtime code; edge register with a status per baseline id | #2; RA14; goals.md |
| I-06 | backend exception list approved (SQLite nested write units only); a new one needs its own justification and tests | #6 |
| I-07 | target: an unavailable provider is an indeterminate decision (503) inside the algebra, as a separate change; not every security exception becomes indeterminate | #19 |
| I-08 | authority assigned in composition root code, not in ordinary settings; local owner verified by the module actually registered, remote by the trusted bound provider; protected claims widened to access-granting billing claims; `email`, `email_verified` only from the trusted issuer; the authority is critical; violator rule; protected claims minted only from the verified `collect_claims` result; issuer-verifier policy consistency | #9, #10, #12, #13, #14, #15; RA9 |
| I-09 | state adds gate probe G2-G4; protected claims under a policy-version mismatch are untrusted; `token_policy_outdated` only where needed | #11; RA4 |
| I-10 | open part restated from validation B6 | RA5 |
| I-12 | D4 kept as a defect to fix; either outcome of B-F5 passes, a WARNING alone never | condition 4 |
| I-14 | call counts, absence of I/O and semantics checked in CI; instructions and latency on a reproducible bench with measured noise; +1 % only where the bench resolves it; security fixes not blocked; remote-tier state | #5; RA7 |
| I-15 | byte-identical for the same generator version and declaration; a diff across generator versions needs an explanation, a compatibility check and snapshot review | #3 |
| I-16 | `Feature` and `WithinLimit` stay public capabilities; removal only as an approved exception with a ledger change | #21 |
| I-17 | a generated consumer interface (domain atoms) only through an ADR-0010 amendment with a formally approved migration | #18; RA1 |
| I-18 | module prod refusals at every entry point that builds the composition; static CLIs not forced to build it; absent module's variables inert, topology errors found by `sandloom check`/`test`; optional module's settings error fatal; env names kept; risk ids namespaced; per-module semantic ratchets; ratchet 15; `config dump` | #7, #25-#31, #B |
| I-19 | all 16 subsets with an expected outcome; a refusal only for a predefined reason | #4; RA2 |
| I-20 | D1, D4, billing without celery and `/ready` 200 with an unusable taskqueue kept as defects | condition 4 |
| I-21 | revocation duties split: the kernel guarantees access-token revocation and a durable applier; the issuer ends refresh sessions and the kernel verifies the obligation | #16 |
| I-22 | T3, T4 in scope; T7 state from validation C4; T8, T10 restated | #10, #13, #16, #26-#28; RA6 |
| I-23 | "does not increase"; baseline counts recorded; the third `bound_tenant` call removed only as a separate change; freshness and comparison equivalence | #5, #20, #23, #24; RA8 |
| I-24 (new) | one active application composition per production process | #8 |
| H-1 | range I-01..I-24 | #8 |
| H-2 | names `Feature` and `WithinLimit` | #21 |
| H-4 | adds the provider-down rule of I-07 | #19 |
| H-5 | covers access-granting plan claims and `email`, `email_verified` | #10 |
| H-8 | a generated consumer interface only after an ADR-0010 amendment | #18; RA1 |
| H-9 | env names kept; the risk-id rename is a one-time migration with no alias; wire and event compatibility is separate from SDK aliases | #28, #30; roadmap-review R39 |
| H-10 | as I-14 | #5 |
| H-20 (new) | risk metadata: a declared access check is recognised inside hand-written code; a warning alone is not enough | #22 |
| §4.2 | default authority assignment (DX, operations); env-name and risk-id migration (migration cost) | RA15; #28, #30 |
| scenarios | changed: A-B6, A-B7, A-F2, A-F5, B-H3, B-F5, C-H3, C-B3, C-B6, C-F4, D-B1, D-B3, D-F2, D-F4, D-H3; added: A-B8, A-F6, A-F7, A-F8, B-B6, B-B7, C-B8, C-B9, C-B10, C-F6, C-F7, D-B6, D-F5, D-H5 | the answers; RA3, RA10-RA13, RA16 |

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

**Process model the requirements assume** (owner, answer #8 and preamble of
[owner-answers.md](owner-answers.md)). A production process holds one active application
composition (I-24); tests may build several apps one after another with explicit state cleanup.
Not every entry point builds the composition through `create_app`: the static commands (`new
add check explain docs context graph keys test`) build no settings by design, the deploy-profile
tooling (`sandloom add deploy`, the T014 check) runs `register`/`setup` on a bare facade, and
part of the admin CLI reads settings directly before or without building the app
([research-settings.md](research-settings.md) §1.2, F7). A requirement stated "at every entry
point" means every entry point that builds the composition, plus the stated outcome for those
that do not (I-18).

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
The owner approved I-01..I-23 on 2026-11-08 with the refinements recorded here (answer #1);
I-24 records answer #8.

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
**Tests** (owner, answer #2). Tests are outside the shipped kernel boundary: I-02 and I-03 judge
shipped kernel code. Kernel unit and architecture tests run without batteries (no module wheel
installed); an integration test may load real modules, naming them explicitly.

- State: violated by B-I1 (`core/cli/password_check.py:21`, `:40-44`). Kernel tests without
  batteries: no proof today; every kernel test runs in the repository environment, where all
  module packages are importable (baseline §6.2).
- Missing proof: an import scan over `src/sandloom/{core,sdk,shared,main.py}` that includes
  `sandloom_*` and app module packages, separates descriptor-sourced loader imports from fixed
  names as above, and fails on B-I1 today; a run of the kernel unit and architecture tests in a
  venv with no module wheel, where a test that needs a battery is marked as an integration test
  naming the module.

### I-03 Every remaining kernel-to-module edge is approved and checked

After decision A, each kernel edge that names a specific module or something it owns (its
contracts, events, settings, claim keys, commands, artifacts) is either absent or listed in one
approved place with its reason; a new one fails a check with a code. A concept of the general
security and isolation protocol stated in kernel terms (I-21) is not such an edge; the same
concept bound to one module's names or data model is (goals.md, "What is an error"). A module
name in a template, a recipe, documentation or a kernel test is classified apart from shipped
runtime code and is not by itself a runtime edge (answer #2; goals.md, "How to apply it").

- Guarantees: G-KERNEL-13 (narrower).
- Proof today: `src/sandloom/core/tests/test_module_names.py:165`
  (`test_the_kernel_names_no_module`) covers exact literals in `core/config`,
  `core/app_factory`, `core/loader/plugin_loader.py`, `main.py` only; it misses `admin`,
  substrings and every other directory (baseline §9).
- State: violated: 40 class (b) and 5 class (c) edges, no check catches them.
- Missing proof: a check over contract names, claim keys, claim sources, setting names,
  module names and artifact files in kernel code and in the kernel wheel, fed by the
  approved list, failing with a code and `file:line`. The approved list records every baseline
  edge id with a status (open, fixed, retained-approved) and its decision, and a change reports
  the edges it adds, removes and approves (roadmap-review RA14, N5).

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

(Codex review of this file, 2026-11-08: equivalence was stated without its scope.) The owner
approved this list (answer #6): SQLite nested write units are the only backend exception; a new
one enters only with its own justification and tests.

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

**Provider down** (owner, answer #19; a separate change from any move). An unavailable provider
becomes an indeterminate decision with reason 503 inside the algebra: `A | B` allows only if
the other branch reliably allowed; `~indeterminate` never allows; the indeterminate branch is
recorded like any denial. Not every security exception becomes an ordinary indeterminate: an
error that signals a broken security invariant stays an error.

- Guarantees: G-AUTHZ-01, -02, -07, -10, -11, -12, -13.
- Proof today: `tests/laws/test_authz_algebra_laws.py:204`, `:214`, `:230`;
  `tests/integration/test_shared_authz_review.py:169`, `:241`, `:389`;
  `tests/integration/test_access_facts_split.py:270`, `:284`.
- State: holds for the atoms the kernel defines. No proof for an atom or fact defined
  outside the kernel: the law tests use fixed-outcome test atoms. Provider down: a hand-written
  `Requirement` over a provider's stub surfaces the outage as a proxy 503 outside the algebra,
  with no deny log and no metric ([research-authz-portability.md](research-authz-portability.md)
  §1.7; roadmap-review M6), so `A | B` with `B`'s provider down never reaches `A`'s allow.
- Missing proof: run the law properties over atoms registered by a test module from
  outside `src/sandloom`, including one that raises and one whose owner is absent; the
  owner's regression set (tm reflection on `kb-alt-authz`, 2026-11-08): `~Entitled("missing.fact")`
  never allows; `Role("admin") | Entitled("missing.fact")` with a trusted role allows; scenario
  C-F7 for the provider-down rule.

### I-08 Each security claim has one authority, assigned by the application and verified by the kernel

- **Protected claims** (answer #10). Always `roles`, `permissions`, `tenant_ids`. Also every
  billing claim used to grant access (`subscription_status`, `plan_code`, `features`,
  `limits`): it must have an assigned authority too. `email` and `email_verified` are accepted
  only from the trusted issuer. The kernel does not hard-code billing, or any module, as the
  owner of a claim.
- **Assignment** (answer #9). The authority of a protected claim is set in the application's
  composition root code, not in ordinary settings. A module's self-registration never makes it
  the authority: not its name, not an `owner=` argument, not the text of a binding key, not a
  `describe()` answer. A string name is not proof (goals.md: "A module naming itself `identity`
  is not proof").
- **Verification** (answer #9). At mint the kernel accepts a protected claim only from its
  assigned authority: a local owner is verified by the module actually registered, a remote one
  by the trusted bound RPC provider (the service bound to it in `SERVICE_BINDINGS` and pinned by
  `service_id`).
- **The authority is critical** (answer #14). An assigned authority that is absent or fails
  refuses the mint.
- **Ownership.** Every other claim key has exactly one writer per token. Reserved keys
  (provenance, token envelope, `UserContext` structure, the bound tenant) have no contributor
  writer at all.
- **Violator** (answer #12). An optional contributor that writes a key it does not own (a
  reserved key, another module's key, a protected claim it is not assigned) loses its whole
  result; the failure is recorded in `claims_sources_failed`, logged with the contributor and
  the key, and counted in a metric; the mint goes on without it. A required authority's failure
  always blocks the mint. A violator's partial result is never kept.
- **Minting** (answer #13). `create_access_token` does not accept arbitrarily assembled
  protected claims or provenance: they come from the verified result of `collect_claims`. The
  system fields (provenance, token envelope, `UserContext` structure) are formed and controlled
  by the kernel. An allowed service or test token gets an explicitly limited path that cannot
  grant user rights. Supported SDK scenarios keep working.
- **Issuer-verifier consistency** (answer #11; roadmap-review RA9, N3). A verifier trusts a
  token's protected claims only when the token was minted under the authority policy the
  verifier holds; on a mismatch, including a token minted before the policy existed, the
  protected claims are untrusted (I-09 states what the gate then answers). A split whose
  services hold different policies is diagnosable.
- **Declared claim keys** (answer #15). A manifest of the keys a module writes may report errors
  at startup; whether to have one is decided after the PoC, and it never replaces the runtime
  ownership check.
- No authority assigned for a claim that a gate reads: I-09 and I-20 apply.

(Codex review 2026-11-08; owner, goals.md "Who owns what" and tm reflection on `kb-assess`
2026-11-08: guarantee, provider and trust are separate; a data contract does not make a
provider a trusted authority. Owner's answers #9-#15, 2026-11-08.)

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
    (guarantee-map §13; gate probe G5, roadmap-review §2.2);
  - a non-critical contributor that writes a reserved or foreign key refuses **every** mint with
    503, so one optional module stops every login (research-trust §3.4, R4; validation A2), the
    opposite of the violator rule;
  - a token not minted by the merge passes `Role`/`Perm` with no provenance, and the platform
    gate when it carries the string `identity` in `claims_sources`; nothing tells a token minted
    under an older policy from a new one (research-trust §3.3, R5);
  - an authority registered `critical=False` that fails leaves its keys absent for the token's
    life (research-trust §5.5 e-4).
- Missing proof: an application assigns the authority of `roles` to a test identity module
  outside `src/`; a second module contributing `roles: ["admin"]`, one passing
  `owner="identity"`, one named `identity`, and a remote binding named `identity.claims.v1` to
  an unassigned service are each refused (startup refusal, or the violator rule at mint) with a
  log line, with and without `sandloom_identity` installed, mono and split; scenarios A-F6
  (authority ran and omitted the key), A-F7 (optional violator), A-F8 (minting outside
  `collect_claims`), A-B8 (issuer-verifier policy skew), C-B3 (legacy token).

### I-09 A gate trusts a claim only from its assigned authority

A gate (platform admin, `Role`, `Perm`, `OrgRole`, `Subscribed`, `Feature`, `WithinLimit`,
claim-mode facts) reads a security claim only when the authority the application assigned
(I-08) is in `claims_sources` and not in `claims_sources_failed`. With no authority assigned,
loaded or bound, the gate denies (403) or is indeterminate, never allows. Every platform-admin
entry point gives the same decision for the same token (goals.md: one platform admission
shared by `require_platform_admin` and `platform_operator`). The same holds in a monolith and a
split. A gate trusts that the authority wrote the key, not only that the authority ran.

**Policy mismatch and old tokens** (owner, answer #11). There is no global 401 for every old
token: that would break ordinary authentication and could break `Or`. When the token's authority
policy does not match the verifier's (I-08), its protected claims are untrusted. If a policy
really needs such a claim and no other sufficient allowing branch exists, the answer is 401 with
the machine code `token_policy_outdated`, so the client refreshes. An old token keeps working on
routes that need no protected claim until it expires.

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
  - at `44451e4` the platform gates and `Role("admin")` allow `roles: ["admin"]` from any writer
    when `identity` ran without writing `roles` (it wrote only `permissions`, or `{}`, in either
    order): the gates check that `identity` ran, not that it wrote the key (gate probe G2-G4,
    roadmap-review §2.2, `_evidence/roadmap-review/gate-probe.txt`; `shared/guards/platform.py:14-18`,
    `core/admin/api/platform.py:63-71`). The bundled identity always writes both keys
    (`sandloom_identity/claims.py:61-69`), so the standard deployment is not affected;
  - the two platform gates are separate code (B-S1, B-S2); no test asserts they decide alike.
- Missing proof: `require(Role("admin"))` with `roles: ["admin"]` from an unassigned writer
  answers 403; `require_platform_admin` and `platform_operator` give the same answer over a
  table of tokens; `Subscribed` with billing absent and `subscription_status: "active"` from
  another module does not allow without another sufficient condition; A-F6; C-B3 (a token under
  an older policy: 401 `token_policy_outdated` only where no other branch allows).

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
  (`tenant_id`, `org_id` reserved, commit `e212893`). **Violated for `tenant_ids`**: with no
  tenant-grant provider deployed, a contributor writing `tenant_ids: [T_FOREIGN, T_B]` mints,
  both headers are accepted, and Postgres RLS opens to `T_B`'s rows (validation B6; roadmap-review
  RA5, M8). Nobody assigns the tenant authority (I-08, T2).
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
  No test runs two released versions of one module package in two processes. D4 stays on the
  list of defects to fix (owner, condition 4): either outcome of B-F5 passes (the comparison
  runs and `/ready` fails, or startup refuses naming the contract), a WARNING alone never.
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
2026-11-08; owner, answer #5):

1. **Hard: no new I/O on the claims tier, the tiers stay, and moving an atom adds no call.** A
   claims-only policy opens no database session and makes no network call. A local fact
   (provider in the same process) makes no RPC. A remote fact makes an RPC only when its owner
   is in another process. No discovery or `describe()` call runs on the request path when the
   fact's metadata is already known. Moving an atom causes zero extra RPC and database calls
   (I-23).
2. **Checked in CI.** Call counts (RPC, database, contract calls) per tier and per policy shape,
   the absence of I/O where part 1 forbids it, and the semantics (decision, atoms evaluated,
   order) are asserted by tests that run in CI.
3. **Measured on a bench.** Every alternative that touches the request, unit-of-work, eventing,
   authorization or mint path reports, base build against candidate build, per operation: CPU
   instructions (cachegrind) and latency p50/p95/p99, on a reproducible bench (fixed commits,
   scripted runs, alternating base/candidate rounds) that states its noise, measured from
   base-against-base runs. These are not a CI gate.
4. **Threshold.** The budget of at most +1 % instructions per operation stays
   (`docs/adr/0010-public-api-1.0.md:328-331`), and applies only where the bench measures with
   enough precision to resolve it: today the live atoms (run spread ±0.3 % or less) but not the
   claims atoms, which sit inside the control's ±4.3 % band
   ([perf-dx-baseline.md](perf-dx-baseline.md) §2.3). A change beyond the noise and the
   threshold fails unless the owner approves it. **A security fix is not blocked by the
   budget**: its cost is measured and reported, and the owner decides whether to optimise
   later. The latency threshold is open (§6.2).

- Guarantees: G-AUTHZ-02 (claims-only policy opens no DB session, does no I/O); G-AUTHZ-03.
- Proof today: `tests/integration/test_shared_authz_review.py:426` for G-AUTHZ-02 (part 1,
  claims tier). Instruction counts and latency are measured by hand
  (`benchmarks/ladder/cg/run_cg.sh`; procedure with alternating rounds, p50/p99 at fixed rates,
  `benchmarks/README.md:270-291`); no CI job runs them; no rung for authorization or mint.
- State: part 1 holds for the claims tier and the local fact tier's counts as measured
  (perf-dx §2.1); **violated for the remote tier**: the first use of a bound contract in a
  process does a `describe` on the request, and facts metadata is fetched again every 300 s
  (`core/rpc/proxy.py:656`, `shared/authz/facts.py:64`; perf-dx §2.2; roadmap-review RA7, M3).
  Part 2: no CI test asserts the counts per tier. Part 3: measured by hand once
  (perf-dx-baseline.md: cachegrind noise ±4.3 % on the control, latency p50 moving by up to
  40 % between runs on a shared laptop); no rung for mint.
- Missing proof: call-count assertions per tier (claims, local fact, remote fact) in mono and
  split, in CI; bench runs of the request and write rungs plus an authorization rung
  (claims-only, local fact, remote fact) and a mint rung, base and candidate, with the noise
  and p50/p95/p99.

### I-15 Generation is deterministic

The same generator version and the same declarations give byte-identical generated files,
whatever the hash seed, module discovery order and number of runs. `sandloom test` never
writes. A stale or hand-edited generated file fails `sandloom test` with its code. After a
generator update, any diff on unchanged declarations needs an explanation, a compatibility
check and a snapshot review (owner, answer #3).

- Guarantees: C-GEN-15, G-COMPAT-01, G-CONTRACT-07, C-CONSOLE-03.
- Proof today: `src/sandloom/core/tests/test_run_tests_cli.py:131`;
  `tests/sdk/test_sdk_generated.py:832`, `:883`;
  `src/sandloom/core/tests/test_generated_admin_manifest.py:26`;
  `src/sandloom/core/tests/test_contract_parity.py:544` (digest stable across hash seeds).
- State: holds, partial proof. No test runs `sandloom generate` twice under different
  `PYTHONHASHSEED` values and module orders and compares bytes. No test covers a
  generator change with the contract unchanged.
- Missing proof: that test, plus one that upgrades the generator with unchanged
  declarations and requires every diff to be reported by file for review (B-H3).

### I-16 Every existing guarantee keeps its tests

No guarantee weakens and no capability is removed; each `guarantees.yml` entry stays live
with its enforcing tests, or is recorded under `removed:` with the owner's approval.

- Guarantees: the whole ledger; overriding rule `docs/adr/0010-public-api-1.0.md:22-23`.
- Proof today: `tests/architecture/test_guarantee_ledger.py:61` (`test_ledger_is_green`);
  `uv run pytest -m guarantees src`.
**Public capabilities without a bundled producer** (owner, answer #21). `Feature` and
`WithinLimit` are public capabilities even though no bundled module produces `features` or
`limits` (guarantee-map row "claim atoms Feature, WithinLimit"); not being used does not license
breaking H-2 or this invariant. Moving them to billing is researched first, with correct
parameter passing and split behaviour (today the remote fact path drops their parameters,
`shared/authz/facts.py:466-473`; research-authz-portability §4.6). Removing them is possible only
as a separately approved exception with a change to the capability ledger.

- State: holds. Note: some ledger entries hold only while a battery is present (D1: G-REVOKE-07,
  -08; D2: G-AUTH-12 for `Subscribed`); I-20 covers them.

### I-17 The public surface changes only by review

`sandloom.sdk` stays the only public surface of the framework for modules; every name and
signature is in `public_api_baseline.json`; the beginner tier stays at its reviewed size (30
names); importing `sandloom.sdk` or a bundled module reads no environment and does no I/O; the
kernel never imports `sandloom.sdk`.

**Allowed imports** (ADR-0010 decision 1, `docs/adr/0010-public-api-1.0.md:56-57`; owner,
answer #18; roadmap-review RA1). A module imports `sandloom.sdk`, generated contract stubs and
DTO copies, and its own package. Any other generated consumer interface, such as domain atoms
imported from a generated consumer API, is allowed only after an ADR-0010 amendment, and the
change to the public `sdk` contract and to beginner DX goes through a formally approved
migration. The policy expression stays the same.

- Guarantees: G-KERNEL-11, G-SDK-01, G-SDK-02, G-SDK-04, G-SDK-05.
- Proof today: `src/sandloom/core/tests/test_review_tooling_checks.py:18`;
  `tests/architecture/test_sdk_import_isolation.py:37`;
  `tests/architecture/test_kernel_import_contracts.py:24`;
  `tests/sdk/test_sdk_generators.py:362`.
- State: holds. Four beginner names are billing and tenancy atoms (B-N14); changing them is
  an API decision (`src/sandloom/core/AGENTS.md`, "Adding a name is an API decision"), and
  moving `from sandloom.sdk import OrgRole` to another import is a DX change to be judged, not
  assumed better (owner, tm reflection on `kb-requirements-amend` 2026-11-08; H-19); it needs
  the ADR-0010 amendment and the approved migration above (answer #18).

### I-18 Configuration guarantees cover every setting, wherever it is declared

Every setting, including one a module owns: classified in exactly one tier with a value
per profile; the semantic tier ratchet holds; a production weakening is an accepted risk
id; boot refusals are identical in the app, the worker, `sandloom migrate` and the CLI;
read only through validated settings; secrets redacted in `sandloom config dump` and logs;
security-relevant settings equal across processes of a deploy profile.

Refined by the owner's answers (2026-11-08):

- **Entry points** (#B; preamble). A production refusal, including a module's (identity's TOTP
  key, billing's fake PSP), is reached with the same refusal reason by the API, the worker,
  beat, migrations and the operational commands. A static CLI that needs no application
  composition is not forced to build it (§1, "Process model").
- **Absent module** (#25). The variables of a module absent from the process are not validated
  and do not interfere. `sandloom check` and `sandloom test` still detect configuration errors
  for the chosen topology and wrong names of active modules.
- **Optional module** (#29). A configuration error of an explicitly selected module is fatal,
  also when the module is optional; the loader does not treat it like an optional import
  failure.
- **Env names** (#30). The existing module-owned env names stay: **22** today, counted in code
  at `c9b9b05` (the 21 fields of `ModulesConfig` in `core/config/modules.py` plus
  `TENANT_CLAIMS_MAX_ORGANIZATIONS` in `core/config/auth.py`), **24** once owner decision #13
  moves `REFRESH_TOKEN_EXPIRE_DAYS` and `MAX_SESSION_DAYS` to identity. The 23 of the answer
  and of research-settings §7.5 was an off-by-one (adversarial D-5). Collisions are
  forbidden. A new module's names carry its prefix, and `sandloom check` checks them.
- **Semantic ratchet** (#7, #26). The kernel's ratchet is 15, the current value
  (`SEMANTIC_COUNT_CEILING = 15`, `core/tests/test_config_tiers.py:52-71`; research-settings
  F8). When a domain field leaves the kernel (today one: `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS`,
  `core/config/tiers.py:334`), the ceiling is recounted downward, not kept. Each third-party
  module has its own semantic ratchet; the kernel checks that every setting is classified, not
  a sum of third-party semantic settings.
- **Accepted-risk ids** (#27, #28). Risk ids carry the owning module's namespace
  (`billing.fake_psp_in_prod`), introduced by one explicit environment migration with
  documentation and no permanent compatibility alias. A risk id of an absent module is inert
  only when its namespace is known in the deployment; an unknown prefix is refused, so a typo in
  a module or risk name cannot bypass the check. For an active module, an unknown risk id is
  refused.
- **`sandloom config dump`** (#31). It may become slower if the cost is measured and the command
  does no activation, no lifespan and no module I/O. A `--kernel-only` mode keeps today's
  kernel-only output.
  Secret redaction, provenance of each value and the diagnostics of wrong settings stay.
- **Order** (roadmap-review RA10, N2; answer preamble). A module's settings are validated before
  that module's setup takes effect, and the checks that span modules run after loading; no
  separate two-phase bootstrap is required (research-settings §5).

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
  accepted (T10). **Violated for module production refusals**: with `ENVIRONMENT=prod` and
  `TWOFA_ENCRYPTION_KEY`, `BILLING_PROVIDER` unset, `create_app()` only logs, the worker app
  builds, and the refusals run only in the ASGI lifespan, so worker, beat, `sandloom migrate`
  and the operator CLIs start (research-settings F1, probe 1; G-AUTH-19, G-RISK-07 and the prod
  part of G-BILLING-18 hold in the API process only). Fix tracked as small fix B (approved,
  separate PR). `sandloom config dump` loads no module (research-settings F2). Accepted-risk ids
  are flat and checked against a closed kernel registry (`core/config/settings.py:191-206`,
  `core/config/accepted_risks.py:50-145`; F4). Two models reading one env name are not detected
  by pydantic-settings (F5).
- Missing proof: the fixture module of I-13 declares an ops setting, a semantic setting, a
  secret, an accepted risk and a production-only refusal; each tier, redaction, entry-point
  and profile test sees them; a process matrix (API, worker, beat, migrate, operational CLI)
  asserts the same refusal reason for each module production refusal (#B); scenarios D-B6,
  D-F4, D-F5, D-H5.

### I-19 Every allowed install profile and every module subset boots

Allowed install profiles:

- **P1** `sandloom` alone (I-01);
- **P2** `sandloom` plus any subset of module wheels, each with only the dependencies its
  published metadata declares;
- **P3** the published extras (`sandloom[standard]`, `sandloom[admin]`) and the `sandloom new`
  template's dependencies.

In each profile, in clean environments, each of the 16 subsets of {identity, tenancy, billing,
admin} has an expected outcome: it boots and becomes ready, or it refuses at startup only for a
predefined reason, with a defined code and a message naming what is missing (owner, answer #4;
roadmap-review RA2, R33). A refusal is never caused by an undeclared dependency. The predefined
reasons today: a required consumed contract with no provider and no binding
(`ContractBindingError`, SANDLOOM-B005), and a configuration error of a selected module (I-18,
answer #29). A narrower guarantee for a subset is an owner exception in writing.

On today's declarations no bundled module requires another's contract: billing consumes
`tenancy.membership.v1` with `required=False` (`sandloom_billing/__init__.py:76-84`), admin
consumes `identity.security.v1` with `optional=True` (`sandloom_admin/startup.py:18`); identity
and tenancy consume none. So every subset is expected to boot and become ready:

| # | subset | expected | proved today (repository environment) |
|---|---|---|---|
| 1 | none (P1) | boots, ready | lean install (I-01) |
| 2 | identity | boots, ready | `test_module_subset.py:44`; `sandloom_identity/tests/test_module_wiring.py:26` |
| 3 | tenancy | boots, ready | `test_module_subset.py:43`; tenancy's `test_module_wiring.py` |
| 4 | billing | boots, ready (billing answers 501 for organization context, `sandloom_billing/__init__.py:77-80`) | `test_module_subset.py:51`; billing's `test_module_wiring.py`; **fails in P2 without celery** |
| 5 | admin | boots, ready (no admin hint without identity, `sandloom_admin/startup.py:1-7`) | none |
| 6 | identity + tenancy | boots, ready | none |
| 7 | identity + billing | boots, ready | none |
| 8 | identity + admin | boots, ready | none |
| 9 | tenancy + billing | boots, ready | `test_module_subset.py:45-50` |
| 10 | tenancy + admin | boots, ready | none |
| 11 | billing + admin | boots, ready | none |
| 12 | identity + tenancy + billing | boots, ready | none |
| 13 | identity + tenancy + admin | boots, ready | none |
| 14 | identity + billing + admin | boots, ready | none |
| 15 | tenancy + billing + admin | boots, ready | none |
| 16 | all four | boots, ready | `test_module_subset.py` (`test_full_monolith_set_loads`) |

(Boot of the full set and of single modules is also covered by the probes of
`test_module_boots_alone.py`.) A subset whose expected outcome changes after a decision (a
consumed contract made required) updates this table with the predefined reason.

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
  kb-baseline-close). This is a refusal for an undeclared dependency, which answer #4 forbids;
  it stays on the list of defects (condition 4). Identity: inferred, not run (baseline §12).
  Holds in P1 (baseline §13.1) and P3. Packaging fix tracked as tm `billing-boots-without-celery`.
  Of the 16 subsets, 6 are proved in the repository environment, none in clean P2 venvs.
- Missing proof: a packaging check that installs each of the 16 subsets of module wheels on
  plain `sandloom` (P2) and through the extras (P3) in clean venvs, boots it, and asserts the
  expected outcome of the table above.

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
  The owner keeps D1, D4, billing failing to boot without celery (I-19) and `/ready` 200 with an
  unusable taskqueue on the list of defects every decision must not forget (condition 4).
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

**Revocation duties** (owner, answer #16). The kernel guarantees reliable access-token
revocation and the presence of a durable revocation applier. Ending refresh sessions is
implemented by the issuer, which owns those sessions; the kernel checks that this obligation is
registered and executed. A missing implementation is never hidden behind a warning: it fails
closed or refuses startup (I-20).

- Guarantees: G-AUTH-*, G-REVOKE-*, G-BLOCK-*, G-TENANT-*, G-RLS-*, G-AUTHZ-*, G-OPCLASS-07.
- Proof today: the provider column of guarantee-map §2-§6 names kernel code for each of them.
  No check asserts it.
- State: holds, except two duties of the revocation protocol that only identity performs: the
  durable applier of `core.auth.tokens_revoked.v1` (`sandloom_identity/__init__.py:73-84`) and
  ending refresh sessions on `core.auth.user_blocked.v1` (`:63-71`); nothing names or checks
  either duty (guarantee-map §3, D1, T8). Under answer #16 the first is a kernel guarantee that
  is not kept without identity (D1), and the second is the issuer's obligation that the kernel
  does not verify.
- Missing proof: each ADR lists, per component above, where it lives after the change; a PoC
  that moves one is disqualified (H-14); A-F5 for both revocation duties.

### I-22 The application assigns every trust decision; the kernel verifies it

For each trust decision of guarantee-map §12, the application can state its choice in trusted
configuration or composition, and the kernel checks that the party acting is the one assigned.
A module's own name, argument, binding key text or `describe()` answer is never the proof
(goals.md "Guarantee, provider, trust"; owner, tm reflection on `kb-assess` 2026-11-08).

| # | trust decision (guarantee-map §12) | state |
|---|---|---|
| T1 | authority of `roles` / `permissions` | violated: platform gates by the name `identity`; `Role`/`Perm` any writer; one writer per token since PR #70 (I-08, I-09) |
| T2 | tenant authority (`tenant_ids`) | violated: no assignment; `tenant_id`/`org_id` reserved since `e212893` (I-10) |
| T3 | authority of access-granting plan claims (`subscription_status`, `plan_code`, `features`, `limits`) and of `subscription.active` | in scope (answer #10); violated: kernel defaults, `WELL_KNOWN_FACT_OWNERS` maps the fact to `billing` (`shared/authz/facts.py:526-541`) |
| T4 | who may mint tokens | in scope for protected claims (answer #13): they come only from the verified `collect_claims` result, service and test tokens take a limited path that grants no user rights; violated: `issues_tokens=True` drives key placement only; `create_access_token` has no caller check (S5) |
| T5 | which provider answers a domain contract in-process | violated: first registrant of the name, process-wide live-adapter slot (`core/facade/_services.py:53-69`; baseline A19); remote holds (`SERVICE_BINDINGS`) |
| T6 | the name a claims source carries in provenance | violated: short name, `owner=`, or binding key text (I-08) |
| T7 | the owner of an access fact | violated: in-process, a module registers a fact under another module's owner and serves its facts contract, every SKU allowed, no log (validation C4); remotely, the provider's own `describe()` (`facts.py:486`, `:503`); roadmap-review RA6. Small fix #A (approved) checks `AccessFact.owner_module`, `owner=` and the reserved families `<m>.claims.v1` / `<m>.facts.v1` against the registering module |
| T8 | who applies durable revocations and ends refresh sessions on block | violated: identity's consumers, unnamed duty (D1, I-21). Answer #16: the durable applier is a kernel guarantee; ending refresh sessions is the issuer's obligation, which the kernel verifies |
| T9 | the operator contract for revocation and admin bootstrap | violated: bound to `identity.security.v1` by name (B-R4, B-R5) |
| T10 | accepting a third-party module's production risk | violated: closed kernel risk registry (I-13, I-18). Answers #26-#28: namespaced risk ids, per-module ratchets, the kernel checks completeness of classification |

Holds today: which service answers which contract remotely, and who may call what
(`SERVICE_BINDINGS`, `service_id` pin, `RPC_ALLOWED_CALLS`; guarantee-map §7).

- Guarantees: G-RPC-* (the remote assignment); the rest have none.
- Proof today: for the remote assignment only (G-RPC ledger tests).
- State: violated for T1-T10 as in the table.
- Missing proof: one test per row (T4 for protected claims, answer #13): an unassigned party
  acting is refused or ignored; the assigned one works; mono and split.

### I-23 Moving a domain atom changes no execution

Moving a domain atom (`OrgRole`, `Subscribed`, `SubscriptionActive`, `Credits`, or any other)
out of the kernel does not by itself increase, for the same policy and the same deployment, the
number of network calls or the number of database queries (zero extra calls is a hard rule,
answer #5), and does not change the evaluation order including short-circuit, the decision, the
freshness of the data a decision reads, or the user-facing policy syntax
(`require(OrgRole("admin") & SubscriptionActive(of=...))`). (Owner, tm reflection on
`kb-requirements-amend` 2026-11-08; goals.md, "Authorization: the mechanism stays in the
kernel"; roadmap-review RA8.) The import line of an atom may change only as I-17 allows
(answer #18).

**Baseline counts** (perf-dx-baseline.md §2.1, steady state). `Role`, `Perm`, `Subscribed`: 0
calls; `SubscriptionActive`: 1 call, 3 DB round trips; `Credits`: 1 call, 4; `OrgRole`: 1 call,
3; `OrgRole & SubscriptionActive(of=Account.org(...))`: 2 calls, 6 (outsider 1, 3);
`OrgRole & SubscriptionActive(of=Account.bound_tenant())`: **3** calls, 9 (the second
membership call comes from `tenant_kind`, `account.py:153-168`, `:239`). A move does not exceed
these numbers.

**A decrease is its own change** (answer #23). Removing the third `bound_tenant` call is a
separate change with its own baseline, not part of a move; it does not weaken membership
freshness or tenant isolation to save an RPC.

**Freshness of money-sensitive decisions** (answer #20). A change that moves a settings- or
status-dependent rule (billing's `past_due` grace, `AccessFact(claim_rule=factory)`,
`shared/authz/facts.py:195-198`) into a claim stamped at mint changes behaviour when the
settings or the subscription status change during the token's life; it is not approved without
a freshness comparison. A candidate keeps the freshness that money-sensitive operations need,
and records a semantic-equivalence table (case, today's decision and staleness, the
candidate's).

**Equivalence when a comparison moves** (answer #24). Where a predicate moves between consumer
and provider (billing may answer `has_credits_at_least` as a read contract), the comparison, the
account binding, the consistency and the result equal today's check; such a read never replaces
the atomic credit-spend check.

- Guarantees: G-AUTHZ-02, -03, -13.
- Proof today: none comparing two placements of one atom.
- State: no proof today (nothing has moved).
- Missing proof: C-B6: the same route before and after the move, mono and split, with call
  counts, query counts, the atoms evaluated in order, and the decision recorded and not worse;
  C-B8 (freshness), C-B9 (credits comparison).

### I-24 One active application composition per production process

A production process holds exactly one active application composition (owner, answer #8;
roadmap-review N11, RA17). Process-global state of the kernel (for example the live-adapter
table, `core/facade/_services.py:53-69`; baseline A19) serves that one composition. Tests may
build several apps one after another in one process, with explicit state cleanup between them.

- Guarantees: none recorded.
- Proof today: test fixtures reset process-global tables between apps (roadmap-review R9:
  `live_contracts.py:114`, `:172-175`); no test asserts that a second app built without cleanup
  is refused or isolated.
- State: holds by practice; no proof today.
- Missing proof: building two apps in one test process without cleanup is detected (refused,
  or every process-global table is shown isolated); with cleanup, the second app sees none of
  the first's registrations.

## 4. Hard constraints and trade-off criteria

### 4.1 Hard constraints

An alternative that breaks one is disqualified; no score compensates.

| id | constraint | source |
|---|---|---|
| H-1 | Every invariant I-01..I-24 holds after the change, or the owner accepted the exception in writing. An invariant violated today (I-02, I-03, I-08 assignment, verification, violator rule and minting, I-09, I-10 for `tenant_ids`, I-11, I-12 consumer-only, I-13, I-14 remote tier, I-18 module refusals, I-19 P2, I-20, I-22; I-21 for D1) must hold after the decision or task that owns it. | this file; ADR-0010:22-23; owner answer #1 |
| H-2 | No guarantee weakens and no capability is removed (I-16). `Feature` and `WithinLimit` are such capabilities; removing them needs a separately approved exception and a capability-ledger change. | ADR-0010:22-23; owner answer #21 |
| H-3 | Modules stay isolated: no module-to-module import, cross-module work only through events, contracts, projections, sagas. | `AGENTS.md` "The one rule" |
| H-4 | The authorization algebra is unchanged and fail-closed (I-07): `A \| B` with `A` indeterminate and `B` allow stays allow; `~indeterminate` never allows. An unavailable provider becomes indeterminate (503) inside the algebra only as a separate change, and `A \| B` then allows only when the other branch reliably allowed; not every security exception becomes an ordinary indeterminate. | G-AUTHZ-13; ADR-0010 owner decision 4; tm reflection on `kb-alt-authz`; owner answer #19 |
| H-5 | No gate trusts a security claim from a weaker source than today; after decision C a gate trusts a protected claim (`roles`, `permissions`, `tenant_ids`, and every billing claim used to grant access) only from the authority the application assigned in its composition root and the kernel verified, and `email`, `email_verified` only from the trusted issuer (I-08, I-09, I-10, I-22). | G-AUTH-12, G-OPCLASS-07, G-TENANT-10; goals.md; owner answers #9, #10 |
| H-6 | The same module code runs as a monolith and as any split, by configuration only. | C-SPLIT-01; positioning row 9 |
| H-7 | Business code stays independent of infrastructure: no backend SDK or driver in modules; capabilities stay contracts. | positioning row 10; C003, C004 |
| H-8 | Modules import `sandloom.sdk`, generated contract stubs and DTO copies (ADR-0010 decision 1) and their own package; any other generated consumer interface is allowed only after an ADR-0010 amendment with a formally approved migration of the public `sdk` contract and beginner DX. `sandloom.sdk` stays the only public surface of the framework; its import has no side effects; the kernel never imports it (I-17). | ADR-0010 decision 1, `:56-57`; G-SDK-01, G-SDK-02; roadmap-review RA1; owner answer #18 |
| H-9 | The framework carries no deprecated API and no compatibility alias. A breaking change in a prerelease ships with upgrade steps. The existing env names stay; the namespacing of accepted-risk ids is a one-time environment migration with documentation and no permanent alias. Rolling-upgrade compatibility of wire formats and events (old version-less events, B-B7) is a guarantee separate from SDK aliases and is kept. | G-KERNEL-12; ADR-0010:75; `versioning.md:50-54`; owner answers #28, #30; roadmap-review R39, N13 |
| H-10 | (a) No new network call or database session on the claims-only authorization path, the three execution tiers stay, and moving an atom adds no RPC or database call (I-14 part 1). (b) Call counts, absence of I/O and semantics are checked in CI (I-14 part 2). (c) Instructions and latency are measured on a reproducible bench with measured noise (I-14 part 3). (d) A change beyond noise and the threshold of I-14 part 4 fails unless the owner approves it; the +1 % instruction budget applies only where the bench resolves it; a security fix is never blocked by (c) or (d). | ADR-0010:328-331; Codex review 2026-11-08; owner, tm reflection on `kb-requirements-amend`; owner answer #5 |
| H-11 | Generated files stay generated: never hand-edited, written only by `sandloom generate`, checked by `sandloom test` (I-15). | `AGENTS.md` golden path |
| H-12 | Each new mechanism (definition: §4.2 "Runtime complexity") either removes a confirmed violation (a baseline edge, D1-D4, T1-T10, by id) or provides a required invariant (by id) that no existing mechanism provides at lower cost; the alternative names the existing mechanisms it compared against. A mechanism that does neither disqualifies the alternative until it is dropped. | Codex review 2026-11-08; kb-requirements reflection (4); tm reflection on `kb-assess` (minimal sufficient alternative) |
| H-13 | Each proposed change cites the invariants it serves. | kb-requirements reflection (1) |
| H-14 | No general protocol of I-21 leaves the kernel. | goals.md "How to apply it"; owner, tm reflection on `kb-requirements-amend` |
| H-15 | No silent degradation (I-20): without a battery, each guarantee fails closed or the process refuses to start naming what is missing. | goals.md "Who owns what"; owner, tm reflection on `kb-requirements-amend` |
| H-16 | Moving a domain atom changes no network calls, database queries, evaluation order or policy syntax (I-23). | owner, tm reflection on `kb-requirements-amend` |
| H-17 | No second driver system for business modules (no mandatory identity, tenancy, billing or admin driver); no universal capability registry until an existing mechanism (module registration, contracts, claims contributors, access facts, drivers) shows a proven limit, shown by a failing test or replaceability experiment. | goals.md "The platform capability rule", limits; tm reflection on `kb-assess` |
| H-18 | The kernel defines a new port only when all six conditions of the platform capability rule hold (platform guarantee, no domain specifics, testable semantics of execution and failure, replaceable without a kernel change, defined absence, local and RPC where needed). | goals.md "The platform capability rule" |
| H-19 | Developer experience: one authoring API for local and remote atoms and contracts; a route's code unchanged from monolith to split; a policy whose fact or contract owner is neither local nor bound is reported at bootstrap or preflight, not first found by a request; generation runs only when a public declaration changes, not on every business change. | owner, tm reflection on `kb-requirements-amend` (DX) |
| H-20 | Risk metadata: a sensitive route (administrative, money, tenant) is classified from verifiable metadata (needs a tenant, needs an authority, live check, administrative action, money), not class names, and a module cannot lower its own risk class. `sandloom check` recognises calls of declared access checks inside hand-written `Requirement` classes and checks their metadata, risk, tier and parameters. Where static proof is impossible, independent route classification and runtime checks apply. A warning alone is not enough, and a hand-made atom is not safe because the analyser did not find it. | goals.md "three problems" (policy metadata); owner answer #22; roadmap-review R22, R23 |

State of H-19 today: one API and unchanged routes hold (H-6, C-SPLIT-01); a required consumed
contract is found at startup (B-F2), but a live atom's absent owner is found only at the
request (501, `shared/authz/atoms_live.py:57-73`); generation runs when declarations change
(`sandloom generate`, I-15).

The owner approved H-1..H-19 on 2026-11-08 with the refinements above (answer #1); H-20 records answer #22.

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
| Migration cost | files changed in bundled modules, examples, generated apps, docs; upgrade steps an app owner runs; the existing 22 module-owned env names (24 after owner decision #13) kept in the first step, a rename only as a separate change with upgrade steps (answer #30); the one-time accepted-risk id migration (answer #28) |
| Performance | by I-14 part 2: instructions, p50/p95/p99 latency, RPC and database call counts per operation (request, write, authorization per tier, mint), base against candidate, with the noise tolerance |
| Operations | settings an operator sets for monolith and for split, including trust assignments (I-22); how the default app (`sandloom new`) assigns claim authorities in its composition root without per-key hand wiring (roadmap-review RA15, N9; answer #9); new startup refusals an operator can hit, per entry point |

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
| A-B2 | The identity user commands (`admin create`, `security revoke-sessions`) invoked with identity not installed (lean), wherever they are hosted: in the kernel CLI until the CLI phase (ADR-0012 decision 6), afterwards in identity's or the standard composition's CLI | the command either is absent with a message naming the module that provides it, or exits non-zero naming the missing module or contract (`identity.security.v1`); no traceback; no password policy is applied without the provider (the kernel holds no fallback policy, B-I1) | I-01, I-02, I-20 |
| A-B3 | An app module named `identity` (not `sandloom_identity`), `sandloom_identity` not installed, contributes `roles: ["admin"]` without being assigned as the authority | the minted token carries no trusted `admin` role from it, or startup refuses it with a code; every platform route answers 403 to that user | I-08, I-09, I-22 |
| A-B4 | `sandloom` upgraded by one release while module wheels stay (mono and split); then one module wheel upgraded while `sandloom` stays | each combination inside the declared version ranges boots and passes the conformance script | I-11, I-12 |
| A-B5 | `SANDLOOM_MODULES` names the bundled modules in a different order | same app, same routes snapshot, same generated files | I-15, I-19 |
| A-B6 | A contributor writes a reserved key (`tenant_id`, `org_id`, `user_id`, `iat_us`, `claims_sources`), mono and split; the contributor optional, then a critical one | the token carries none of the reserved values. Optional: its whole result dropped, recorded in `claims_sources_failed`, a log line naming the contributor and the key, a metric; the mint goes on. Critical: mint refused 503 with the log line. At `44451e4` every case refuses the mint (research-trust §3.4, R4) | I-08, I-10 |
| A-B7 | Each of the 16 subsets of identity, tenancy, billing, admin installed on plain `sandloom` with only the declared dependencies (P2), then through the extras (P3), in clean venvs | the outcome in I-19's table: boots and becomes ready, or a predefined refusal with its code; never a refusal for an undeclared dependency, never a `ModuleLoadError` traceback | I-19 |
| A-B8 | Split: the issuer and a verifier run with different authority policies (a rolling deploy, a forgotten service); then a token minted before the policy existed | the mismatch is diagnosable (startup, `/ready` or the verifier's log names it); the verifier does not trust the token's protected claims; routes that need none answer as before; a route that needs one with no other allowing branch answers 401 `token_policy_outdated` | I-08, I-09 |

**Failure**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| A-F1 | A kernel change adds `import sandloom_billing`, `import_module(f"sandloom_{n}")`, a constant `"sandloom_billing.x"` passed to `import_module`, or the literal `"billing.entitlements.v1"` in `core/` | `sandloom test` fails with a code and the `file:line`; the generic loader's descriptor-sourced imports (A2, A7, A10-A16) pass | I-02, I-03 |
| A-F2 | A non-authority module's optional contributor returns `{"roles": ["admin"]}` (mono and split, with and without identity) | refused at startup, or at mint the violator rule: its whole result dropped, recorded in `claims_sources_failed`, logged and counted, the token minted without it; `require(Role("admin"))` and `require_platform_admin` answer 403 for that user | I-08, I-09 |
| A-F3 | A module calls `add_claims_contributor(fn, owner="identity")` without being the assigned authority | startup refuses with a code naming the module and the claimed owner | I-08, I-22 |
| A-F4 | Identity's critical contributor fails at mint | mint answers 503 `ClaimsUnavailable`; no token issued; a token carrying `identity` in `claims_sources_failed` gets 403 on platform routes | I-09; G-AUTH-11 |
| A-F5 | Identity loaded nowhere (tenancy alone, or a replacement issuer), a member is removed, and the revocation mark's post-commit write keeps failing; then a user is blocked | the mark is applied durably, or `/ready` reports it stuck, or startup refuses because no durable applier is present; never only `auth_revocation_deferred_total` (D1). On block the issuer ends its refresh sessions; an issuer that registered no such obligation, or did not execute it, is reported by the kernel (startup refusal or a failing check), never a warning alone (answer #16) | I-20, I-21 |
| A-F6 | The assigned authority of `roles` (or `tenant_ids`) runs successfully but omits the key; another module writes it (mono and split, either registration order) | `require_platform_admin`, `platform_operator`, `Role("admin")` and `TenantScope` do not trust it: the other module's result is dropped by the violator rule, or the gate denies. Today: allow (gate probe G2-G4; roadmap-review RA3) | I-08, I-09, I-10 |
| A-F7 | A non-critical, non-authority contributor (`analytics`) writes a foreign or a reserved key | logins and refreshes continue; that contributor's whole result is dropped and recorded in `claims_sources_failed`, logged with the key, counted; whatever the registration order, the log names the violator, not the authority. Today: every mint 503 (research-trust §3.4; validation A2; roadmap-review RA16; answer #12) | I-08 |
| A-F8 | Code in the issuer process calls `create_access_token` with hand-assembled `roles`, `tenant_ids` or provenance; then a service or test token through the allowed path | protected claims and provenance are accepted only from the verified `collect_claims` result: the call is refused, or the token carries no trusted protected claim; the service or test token cannot grant user rights (no `Role`, `Perm`, platform admission or tenant from it). Out of scope: hostile code that signs with the key itself (goals.md, "Threat model") | I-08, I-22 (T4) |

### 5.B Contract ownership

**Happy**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| B-H1 | Provider publishes a new DTO version: billing adds an optional field to a DTO of `billing.credits.v1` and releases `sandloom-billing` only | `sandloom generate` passes the compatibility check and writes billing's artifacts; no new `sandloom` release is needed (the built `sandloom` wheel's file list and hashes are unchanged); an old consumer's call returns 200 with its copy's fields | I-11, I-12, I-15 |
| B-H2 | Provider absent from the consumer process: an app module calls `billing.credits.v1` over a binding; `sandloom-billing` not installed in that process | process starts; the call returns an instance of the consumer's DTO copy class; `pip list` in that venv shows no `sandloom-billing` | I-05, I-06 |
| B-H3 | Generator updated, contract unchanged: new `sandloom` release, `sandloom generate` on unchanged declarations | same generator version: zero diff. New generator version: every diff is reported by file (SANDLOOM-T007) with an explanation, passes the compatibility check and goes through snapshot review; the old artifacts keep working at run time (answer #3) | I-15 |
| B-H4 | A module adds an optional field to one of its event channels `.v1` and releases only its own package | existing views and `@on` consumers keep consuming (unknown field ignored); SANDLOOM-T005 passes; no `sandloom` release needed | I-11; G-DECL-01, C025 |

**Boundary**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| B-B1 | Identity and billing upgraded at different times (split): identity at N+1 with an additive contract change while billing stays N; then the reverse | in both orders the conformance script answers as in mono; `/ready` 200 on every service; no 500 | I-12, I-06 |
| B-B2 | Consumer holds two contract versions: an app module calls `billing.credits.v1` and `billing.credits.v2` while the provider serves both | both stubs import in one process; the DTO copies are distinct classes with no name clash; each call reaches its version | I-05, I-15 |
| B-B3 | Old consumer sends to new provider (additive) | call 200; `/ready` passes (G-COMPAT-04) | I-12 |
| B-B4 | New consumer + old provider: the consumer calls a method the deployed provider lacks | 501 `ServiceUnavailableInDeployment` at the call; `/ready` fails with `contract_incompatible` when the method is declared in the consumer's requirement; never 500, never a default value | I-12 |
| B-B5 | Two applications compose the same installed provider module, each generating its own copies | in each process exactly one copy class per DTO is used for decoding; the provider package carries no app-specific file | I-05, I-15 |
| B-B6 | Consumer generation with the provider not installed in the build environment where `sandloom generate` runs | the copy is generated from a declaration the provider published, or `sandloom generate` refuses naming the missing source; never a silent absence (baseline §13.2: no tenancy stub was written when tenancy was absent; roadmap-review RA11) | I-05, I-15 |
| B-B7 | Rolling upgrade with old version-less `tenancy.member_*` events in flight after the logic that handles them (C5) moves | the order, duplicate and late-event decisions are unchanged; the existing rolling-upgrade tests still pass (roadmap-review RA12) | I-11, I-12; H-9 |

**Failure**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| B-F1 | Stale generated wrapper: a consumer holds an outdated or hand-edited stub or DTO copy | `sandloom test` fails with SANDLOOM-T007 or C012 naming the file; at run time an older copy decodes a newer additive result without error | I-15, I-12 |
| B-F2 | Provider implementation absent: required contract, no local provider and no binding; then the same with `optional=True` | required: startup refuses with `ContractBindingError` (SANDLOOM-B005); optional: `client()` is `None` and the module boots | I-12, I-19 |
| B-F3 | Provider makes a breaking change inside `.v1` | `sandloom generate` refuses and writes nothing (SANDLOOM-T004); a provider deployed anyway fails its callers' `/ready` with `contract_incompatible`, and startup with `CONTRACT_COMPAT_CHECK=strict` | I-12, I-15 |
| B-F4 | Two installed packages both ship artifacts for one contract name | a check or startup refusal with a code naming both packages; never two importable shapes for one contract | I-05, I-11 |
| B-F5 | Consumer-only install (provider's package absent), `CONTRACT_COMPAT_CHECK=strict`, the deployed provider lacks a method the consumer declares | the comparison runs and `/ready` fails with `contract_incompatible`, or startup refuses naming the contract it cannot check; either passes; never a start with a WARNING only (D4; condition 4) | I-12, I-20 |

### 5.C Authorization extension

**Happy**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| C-H1 | A fixture module adds a typed policy for its own domain and an app route uses it inside `And`, `Or`, `Not` | no file under `src/sandloom` changes; the law properties of I-07 pass over the new policy; the route snapshot records it | I-07, I-13 |
| C-H2 | Existing billing gates, mono and split: `Subscribed(of=Account.user())`, `Credits(1, of=Account.org(param="org_id"))`, `OrgRole("admin")` | same statuses as today: 402 on a definite billing denial, 403 on a role denial; claims-only policy opens no DB session; live policies call the owner locally or over the binding | I-06, I-07, I-14 |
| C-H3 | A third-party alternative billing package (not `sandloom_billing`) answers subscription and credit questions; `sandloom_billing` not installed | the billing gates of the conformance script give the same HTTP outcomes as with `sandloom_billing`; no kernel file changes; the policy expressions in the app's gate code stay the same, only import lines may change, as I-17 allows (answer #18) | I-03, I-13, I-17 |
| C-H4 | Organization membership gate with tenancy, mono and split | same decision in both | I-06 |
| C-H5 | Billing replaced by an unrelated fact: a fixture module owns `inventory.stock.available`, used by a route locally (mono) and remotely (split) | same decisions in both; no kernel file changes; `OrgRole("admin")` in a consumer process without the tenancy implementation installed works through the remote provider (goals.md "Replaceability decides the scope"; tm reflection on `kb-alt-authz`) | I-06, I-07, I-13 |
| C-H6 | Tenancy replaced by an independent tenant-grant provider assigned as the tenant authority, mono and split | RLS isolation holds; a revoked grant denies; the provider down fails closed; no tenant accepted from `X-Tenant-Id` without a grant; no kernel file changes | I-10, I-13, I-20, I-22 |

**Boundary**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| C-B1 | A fact gains a parameter: the owner adds an optional parameter in a new release | callers without the parameter get the same decision as before; a caller that passes it to an old owner (split) gets 501 or 503, never allow | I-07, I-12 |
| C-B2 | The kernel lacks the fact owner: a policy names a fact whose owner is neither loaded nor bound | 501 at the request; inside `Not` or `Or` the route never allows on that branch; the missing owner is also reported at bootstrap or preflight (H-19) | I-07 |
| C-B3 | A token minted before the upgrade or under another authority policy: without a claim the new version reads, without `claims_sources_failed`, or without the current policy | its protected claims are untrusted; a policy that needs one and has no other sufficient allowing branch answers 401 `token_policy_outdated`; an `Or` whose other branch allows still allows; routes needing no protected claim work until expiry; never allow on a missing or untrusted claim (answer #11) | I-07, I-08, I-09 |
| C-B4 | `Account.bound_tenant()` with tenancy not deployed | tenant kind unknown, decision indeterminate (501), never the user's account | I-07; G-AUTHZ-11 |
| C-B5 | Organization `Feature` or `WithinLimit` with no organization fact registered | 501 (G-AUTHZ-12) | I-07 |
| C-B6 | One atom moved out of the kernel: `require(OrgRole("admin") & SubscriptionActive(of=Account.org(param="org_id")))` before and after the move, mono and split, first atom denying and then allowing | equal decision; RPC and database call counts not above today's (perf-dx §2.1: member 2 calls / 6 DB, outsider 1 / 3); equal atoms evaluated in equal order (short-circuit on the first deny); policy expression text unchanged. The `bound_tenant` shape (3 calls today) is measured too; a decrease ships only as its own change with its own baseline (answer #23) | I-14, I-23 |
| C-B7 | Execution tiers: a claims-only policy, a local-fact policy (owner in process), a remote-fact policy (owner in another process) with known metadata | claims: 0 database sessions, 0 network calls; local fact: 0 RPC; remote fact: only the fact call, no `describe()` or discovery call on the request | I-14 |
| C-B8 | A decision that reads billing's `past_due` grace: the subscription turns `past_due`, or `BILLING_DENY_ON_PAST_DUE` changes, while a token is live; base and candidate, mono and split | a semantic-equivalence table lists, per case, today's decision and staleness and the candidate's; a money-sensitive operation sees the change no later than today (answer #20) | I-23 |
| C-B9 | `Credits(n, of=...)` before and after its comparison moves between consumer and provider: balance below, equal and above `n`, personal and organization accounts, mono and split | equal decisions and account binding, consistency as today, call count not above today's (1 call, 4 DB); spending credits still goes through the atomic spend command (answer #24) | I-23 |
| C-B10 | `Feature` and `WithinLimit` with parameters, owner local (mono) and remote (split) | same decisions in both; the parameters reach the owner; no capability removed (today the remote fact path drops them, `shared/authz/facts.py:466-473`; answer #21) | I-06, I-16 |

**Failure**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| C-F1 | Billing down during authorization (split, billing bound but unreachable) | live policy: 503; `Not(live policy)`: 503, not allow; claims-only policy with billing trusted in provenance: decided from claims, no call; with billing in `claims_sources_failed`: escalates to live, 503 | I-07, I-09 |
| C-F2 | A module's policy raises an exception during evaluation | indeterminate or 5xx, never allow; the denial is recorded in the deny log and `access_denied_*` metrics | I-07; G-AUTHZ-08 |
| C-F3 | Two modules register a fact or policy under the same name | startup refuses with a code naming both modules; no last-wins | I-07, I-08 |
| C-F4 | A module contributes a claim key owned by another module (`tenant_ids` from billing, `subscription_status` from a non-billing module), with and without the owner loaded | refused at startup, or the violator rule at mint (whole contribution dropped, logged, counted); a gate reading that key trusts only its assigned authority's provenance | I-08, I-09, I-10 |
| C-F5 | Battery absent: billing not loaded and not bound, another module writes `subscription_status: "active"`; identity not loaded, another module writes `roles: ["admin"]` | `Subscribed` and `Role("admin")` do not allow without another sufficient condition (D2, D3) | I-09, I-20 |
| C-F6 | A sensitive (administrative, money, tenant) route gated by an aliased, re-exported or dynamically built atom, or by a hand-written `Requirement` that calls a declared access check | `sandloom check` classifies it from metadata and checks its risk, tier and parameters, or reports the shape as unsupported with a code, and the route is then classified independently and checked at run time; a warning alone fails the scenario; the runtime admin risk tag still applies (roadmap-review RA13; answer #22) | H-20; I-09 |
| C-F7 | Provider down inside the algebra: `OrgRole("admin") \| SubscriptionActive(of=...)` with tenancy unreachable, for a user with and without an active subscription; `~OrgRole("admin")` with tenancy unreachable | active subscription: allow (the other branch reliably allowed); without: 503 indeterminate, never allow; `~indeterminate`: 503; each indeterminate branch is in the deny log and the metrics; an error that signals a broken security invariant stays an error (answer #19) | I-07; H-4 |

### 5.D Module configuration

**Happy**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| D-H1 | A fixture module adds an ops setting | no file under `src/sandloom` changes; the tier test lists it; `sandloom config dump` shows it; an invalid value is refused identically by the app, the worker, `sandloom migrate` and the CLI | I-13, I-18 |
| D-H2 | A module secret (`STRIPE_SECRET_KEY`) set | `sandloom config dump` and logs show it redacted; `--show-secrets` refused in prod | I-18; G-OPS-11 |
| D-H3 | `BILLING_PROVIDER=fake` with `ENVIRONMENT=prod` | refused with the same reason by every entry point that builds the composition (API, worker, beat, `sandloom migrate`, operational CLIs) unless `billing.fake_psp_in_prod` is accepted (the namespaced id, answer #28). Today: id `fake_psp_in_prod`, refused in the API lifespan only (research-settings F1) | I-18; G-BILLING-18, G-RISK-07 |
| D-H4 | Bare install (lean) | `sandloom config dump` lists no setting of an uninstalled module; no module environment variable is required | I-01, I-18 |
| D-H5 | `sandloom config dump` with module settings set, then `sandloom config dump --kernel-only` | module settings listed with each value's provenance and secrets redacted; wrong settings diagnosed; no activation, no lifespan, no module I/O; the added cost measured; `--kernel-only` gives today's kernel-only output (answer #31) | I-18 |

**Boundary**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| D-B1 | A module's environment variable is set in a process that does not load the module (split) | the process starts; the value is neither validated nor shown by that process and does not interfere; no runtime warning; `sandloom check` and `sandloom test` report a wrong value and a wrong active module name for the chosen topology (answer #25) | I-18, I-19 |
| D-B2 | Split deploy: a security-relevant setting shared across processes (`BILLING_DENY_ON_PAST_DUE`, B-C7) | the deploy-profile check (SANDLOOM-T014) still requires equal values in every process that loads billing; a process without billing neither validates nor holds the value (answer #25) | I-18; G-OPS-14 |
| D-B3 | A module's semantic setting (`BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS`, `core/config/tiers.py:332-338`) | the kernel's ratchet is 15 (`core/tests/test_config_tiers.py:71`) and still fails a new unreviewed semantic setting; when this field leaves the kernel, the ceiling is recounted down, not kept at 15; a third-party module's semantic settings count in its own ratchet, and the kernel checks only that every setting is classified (answers #7, #26) | I-18; G-CONFIG-02 |
| D-B4 | A removed module setting (`core/config/tiers.py:659-678`) set to a non-inert value | startup refuses with the same message as today | I-18; G-CONFIG-03 |
| D-B5 | Kernel output that shows a module setting (platform settings view `billing_provider`, B-C9; set-password link target, B-C10) in mono, then in a process without that module | mono: output unchanged; without the module: the field is absent, no error | I-03, I-18 |
| D-B6 | An invalid module setting (wrong type, a failed module validator) in the app, the worker, `sandloom migrate` and an operational CLI | refused before any of that module's routes, connections, migrations or startup tasks take effect, with the same code in every entry point (roadmap-review RA10) | I-18 |

**Failure**

| id | stimulus and environment | expected observable outcome | invariants |
|---|---|---|---|
| D-F1 | Weak `TWOFA_ENCRYPTION_KEY` in prod with identity loaded | refused at every entry point with a code and the fix (G-AUTH-19, G-CONFIG-07) | I-18 |
| D-F2 | Two modules declare the same environment variable name; a new module declares a name without its prefix | startup refuses with a code naming both modules, and `sandloom check` reports the collision also when one of them is not loaded in the process; `sandloom check` reports the new module's unprefixed name (answer #30) | I-18 |
| D-F3 | Importing a module with an empty environment (`python -I`) | reads no environment variable and constructs no settings (G-SDK-01) | I-17, I-18 |
| D-F4 | `SANDLOOM_ACCEPTED_RISKS` names (a) `billing.fake_psp_in_prod` where billing is not loaded but its namespace is known in the deployment, (b) `biling.fake_psp_in_prod` (unknown prefix), (c) `billing.fake_psp_in_prd` where billing is loaded, (d) the old flat id `fake_psp_in_prod` | (a) inert, reported as not applicable here; (b) and (c) refused at startup as unknown ids; (d) refused naming the replacement (one-time migration, no alias); identical in every entry point (answers #27, #28) | I-18 |
| D-F5 | A module selected in `SANDLOOM_MODULES` but not required (`optional`) has an invalid setting | startup refuses with the module's settings error; the module is not skipped like an optional import failure (answer #29) | I-18, I-20 |

**Scenario counts**

| decision | happy | boundary | failure | total |
|---|---|---|---|---|
| A | 5 | 8 | 8 | 21 |
| B | 4 | 7 | 5 | 16 |
| C | 6 | 10 | 7 | 23 |
| D | 5 | 6 | 5 | 16 |

## 6. The owner's answers (2026-11-08)

The owner answered the 31 consolidated questions on 2026-11-08 (Codex-reviewed wording; the
decision record as received is [owner-answers.md](owner-answers.md)). The answer is not "yes to
all": the overall goal is confirmed, and every decision must keep the guarantee ledger, the
existing authorization algebra, DX, public capabilities and monolith/split behaviour. The A-D
target is decided after the alternatives and PoCs.

### 6.1 Answers and where they are applied

"Asked as" names the earlier question: this file's Q1-Q15, roadmap-review's Q16-Q18 (RA17),
research-trust's Q-T1..Q-T8, research-authz-portability §9 (authz 1-8), research-settings §9
(QS1-QS7).

| # | asked as | answer, in short | applied in |
|---|---|---|---|
| 1 | Q1, Q2 | yes, conditionally: I-01..I-23, H-1..H-19 approved after the refinements; not an approval of any mechanism or exception | status; H-1 |
| 2 | Q3 | tests are outside the shipped boundary; kernel unit/architecture tests run without batteries; integration tests load modules explicitly | I-02, I-03 |
| 3 | Q4 | same generator version and declaration: byte-identical; on a generator update any diff needs an explanation, a compatibility check and snapshot review | I-15, B-H3 |
| 4 | Q10, RA2 | an expected outcome for all 16 subsets in clean environments; a refusal only for a predefined reason, never an undeclared dependency | I-19, A-B7 |
| 5 | Q14 | zero extra RPC/DB calls from moving an atom is hard; +1 % instructions only where measured precisely enough; CI checks call counts, absence of I/O, semantics; latency and instructions on a reproducible bench with measured noise; security fixes not blocked | I-14, I-23, H-10 |
| 6 | Q15 | the only backend exception is SQLite nested write units; new ones with their own justification and tests | I-06 |
| 7 | QS7 | the ratchet is 15, the actual value; recount downward when a domain field leaves the kernel | I-18, D-B3 |
| 8 | Q17 | one active application composition per production process; tests build apps sequentially with explicit cleanup | §1, I-24 |
| 9 | Q-T1 | authorities set in composition root code, not ordinary settings; local owner verified by the module actually registered, remote by the trusted bound provider; a string name is not proof | I-08, H-5, §4.2 |
| 10 | Q9, Q-T2 | always protect `roles`, `permissions`, `tenant_ids`; every billing claim used to grant access needs an assigned authority; `email`, `email_verified` only from the trusted issuer; billing not hard-coded as owner | I-08, I-22 T3, H-5 |
| 11 | Q16, Q-T3 | no global 401 on old tokens; on a policy-version mismatch protected claims are untrusted; 401 `token_policy_outdated` only when a policy needs such a claim and no other allowing branch exists | I-08, I-09, A-B8, C-B3 |
| 12 | Q18, Q-T4 | an optional violator loses its whole result (`claims_sources_failed`, log, metric); a required authority's failure blocks minting; no partial result kept | I-08, A-B6, A-F2, A-F7, C-F4 |
| 13 | Q13, Q-T5 | `create_access_token` takes protected claims and provenance only from the verified `collect_claims` result; system fields formed by the kernel; a limited service/test path that grants no user rights | I-08, I-22 T4, A-F8; goals.md threat model |
| 14 | Q-T6 | the authority of a protected claim is critical; absent or failing refuses the mint | I-08 |
| 15 | Q-T7 | a declared-keys manifest after the PoC; it never replaces the runtime check | I-08; open (§6.2) |
| 16 | Q12, Q-T8 | the kernel guarantees access-token revocation and a durable applier; the issuer ends refresh sessions and the kernel checks the obligation is registered and executed; never hidden behind a warning | I-21, I-22 T8, A-F5 |
| 17 | authz 1 | the comparative PoC goes ahead; the candidate studied first is not the approved architecture; it is compared with consumer-owned `Requirement` and a parameterised `AccessFact` | §6.2 (decided by the PoC) |
| 18 | Q5, authz 2, RA1 | domain atoms may come from a generated consumer API, with an ADR-0010 amendment and a formally approved migration of the public `sdk` contract and beginner DX; the policy expression stays | I-17, I-23, H-8, C-H3 |
| 19 | authz 3 | as a separate change, an unavailable provider is indeterminate (503) inside the algebra; `A \| B` allows only if the other branch reliably allowed; not every security exception becomes indeterminate | I-07, H-4, C-F7 |
| 20 | authz 4 | moving `past_due` into a mint-time claim without a freshness check is not approved; compare with a descriptor refreshed off the hot path; keep the freshness money-sensitive operations need; a semantic-equivalence table | I-23, C-B8; open (§6.2) |
| 21 | authz 5 | `Feature` and `WithinLimit` stay public capabilities; research moving them to billing first; removal only as an approved exception with a ledger change | I-16, H-2, C-B10; open (§6.2) |
| 22 | authz 6 | archcheck must recognise declared access checks inside hand-written `Requirement`s and check metadata, risk, tier, parameters; otherwise independent route classification and runtime checks; a warning is not enough | H-20, C-F6 |
| 23 | authz 7 | remove the third `bound_tenant` call as a separate change with its own baseline; do not weaken membership freshness or tenant isolation | I-23, C-B6 |
| 24 | authz 8 | billing may answer `has_credits_at_least` as a read; comparison, binding, consistency and result as today; it does not replace the atomic spend | I-23, C-B9 |
| 25 | Q6 | an absent module's variables are not validated and do not interfere; `sandloom check`/`test` detect errors for the chosen topology and wrong active module names | I-18, D-B1 |
| 26 | Q7 | each third-party module has its own semantic ratchet; the kernel checks completeness of classification | I-18, D-B3, I-22 T10 |
| 27 | Q8 | an absent module's risk id is inert only for a namespace known in the deployment; an unknown prefix is refused; an unknown id of an active module is refused | I-18, D-F4 |
| 28 | QS1 | namespaced risk ids, one explicit environment migration, documentation, no permanent alias | I-18, H-9, D-H3, D-F4, §4.2 |
| 29 | QS2 | a settings error of an explicitly selected optional module is fatal | I-18, I-19, D-F5 |
| 30 | QS4 | keep the existing module-owned env names (22 counted in code, 24 after owner decision #13; the answer's 23 was an off-by-one); forbid collisions; prefixed names for new modules, checked by `sandloom check` | I-18, H-9, D-F2, §4.2 |
| 31 | QS6 | a slower `config dump` is acceptable if measured and it does no activation, lifespan or module I/O; add `--kernel-only`; keep redaction, provenance, diagnostics | I-18, D-H5 |
| A | — | small fix approved now, separate PR: ownership of `AccessFact.owner_module`, `owner=` and the reserved `<m>.claims.v1` / `<m>.facts.v1` checked against the registering module; negative tests; no hard-coded battery list | I-22 T7 |
| B | QS3 | small fix approved now, separate PR: identity/billing prod refusals in a common check reached by API, worker, beat, migrations and operational commands; a process matrix asserts the same reason; static CLIs not forced to build the app | I-18, D-H3, §1 |

Earlier questions answered without a numbered answer: Q11 (D4) — either outcome of B-F5 passes,
a WARNING alone never, and `/ready` 200 with an unusable taskqueue stays a defect (condition 4;
I-12, I-20).

Conditions to continue (owner-answers.md): record the answers here and in the related ADR and
ledger, naming the changed invariants (this file; the ADR and ledger follow); A and B as separate
small PRs with tests; no mass refactoring of batteries before the alternatives and PoCs for
authority, portable atoms and module settings; D1, D4, billing without celery and `/ready` with
an unusable taskqueue stay on the list; every decision keeps no extra I/O, monolith/split
parity, the guarantee ledger and the DX baseline.

### 6.2 Still open

| item | what is open | decided by |
|---|---|---|
| A-D target | which alternative for each decision | alternatives and PoCs (preamble of owner-answers.md) |
| authorization atoms (#17) | which of the compared candidates becomes the architecture | `kb-poc-authz` comparison |
| `past_due` freshness (#20) | claim stamped at mint with a freshness comparison, or a descriptor refreshed off the hot path | the semantic-equivalence table (C-B8) |
| `Feature`, `WithinLimit` (#21) | the move to billing with parameters and split behaviour; any removal is a separate exception | research, then the owner |
| declared claim keys (#15) | whether a startup manifest is added | after the PoC |
| latency threshold (I-14 part 4) | the allowed p50/p95/p99 regression; perf-dx-baseline.md §2.4 measured p50 moving up to 40 % between runs on a shared machine, so a threshold needs a quieter bench | **[Owner]**, with the bench |
| B-C10 (QS5) | whether the admin console link target belongs to decision A (a provider the admin module registers) or to settings | **[Owner]** |

### 6.3 Roadmap-review amendments (RA1-RA18)

Applied: RA1 (I-17, H-8), RA2 (I-19, A-B7), RA3 (A-F6), RA4 (I-09 state), RA5 (I-10 state), RA6
(I-22 T7), RA7 (I-14 state), RA8 (I-23), RA9 (I-08 issuer-verifier consistency, A-B8), RA10
(I-18 order, D-B6), RA11 (B-B6), RA12 (B-B7), RA13 (C-F6), RA14 (I-03 proof), RA15 (§4.2),
RA16 (A-F7, settled by answer #12 rather than left to the owner), RA17 (its three questions are
answered by #11, #8 and #12, so they are recorded in §6.1, not added as questions), RA18 (header:
`src/` is unchanged from `44451e4` to `de53875`, so the citations are kept at `44451e4` and stated
to hold at the head; HEAD `86aed8b` is no longer the head).

Not applied: none of RA1-RA18 conflicts with the answers. RA9 is applied in the owner's form:
roadmap-review asked for a token under an older assignment never to be trusted "by default" for
a privileged gate; answer #11 makes its protected claims untrusted and adds
`token_policy_outdated` only where needed, instead of a global refusal.
