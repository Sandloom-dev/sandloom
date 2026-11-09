# Kernel boundary: review of the ChatGPT unified roadmap

Independent check of "Sandloom: unified target architecture and migration map" (ChatGPT,
received 2026-11-08; v1 and v2 as received: [`_inputs/chatgpt-roadmap-v1.md`](_archive/_inputs/chatgpt-roadmap-v1.md),
[`_inputs/chatgpt-roadmap-v2.md`](_archive/_inputs/chatgpt-roadmap-v2.md); v2 sections 14–21 supersede 1–13)
against the code and our documents. Task: tm `kb-roadmap-review` (parent `kb-assess`).

Commit: `arch/kernel-boundary` HEAD `86aed8b`, which contains `main` `fe24a79` (PR #70 and
PR #69). The kernel source at HEAD is `b8d8506` plus PR #70: `git diff --stat b8d8506 HEAD -- src/`
touches only `core/facade/_claims.py` and `core/tests/test_claims_key_ownership.py`. Every
`file:line` is at HEAD; paths are under `src/sandloom/` unless they start with `sandloom_<m>/`
or name a repository file.

**Blinding.** This file and the two `_archive/_inputs/chatgpt-roadmap-*` files carry proposed solutions.
The alternatives tasks (`kb-alt-boundary`, `kb-alt-contracts`, `kb-alt-authz`,
`kb-alt-settings`) stay blind: they read [problems.md](problems.md), goals.md and
requirements.md, not this file or those inputs. Section 6 proposes amendments for the owner;
nothing here changes goals.md or requirements.md.

## 1. Verdict

The roadmap points the same way we do, and it is mostly accurate about the code. Its picture of
the kernel (guarantees and generic mechanisms in the kernel, domain atoms and settings with their
owners, authorities assigned by the application, no business drivers, no universal registry)
matches goals.md and the owner decisions recorded on `kb-assess`. Of its 44 factual claims we
checked, 38 hold at HEAD, 1 is wrong (a module cannot pass `owner="identity"`; it gets the same
effect by naming itself `identity`), 4 are outdated, and 1 cannot be checked. The outdated ones
come from one cause: it reviewed the first draft of requirements.md (`4d982e3`: I-01..I-19,
52 scenarios, Q1–Q12). The amended file (`333abb8`: I-01..I-23, H-1..H-19, 62 scenarios, Q1–Q15)
already makes five of its eight proposed amendments (I-02, I-06, I-08/I-09, I-14, H-12). The roadmap adds things we had not
considered: settings validation runs before modules load, the authority policy can differ
between issuer and verifier, tokens minted under an older policy, all 16 module subsets, a
machine-readable edge register, generated consumer APIs needing an ADR-0010 amendment, and
alias-proof static rules. It misses most of what our experiments found, because it relied on
reading code (its authors ran nothing): fact owners can be spoofed, one bad contributor can block
every login, a provider grant can be missing and RLS opens anyway, there are remote `describe()`
calls on the request path, `bound_tenant()` makes 3 contract calls, a typed atom takes a 31-line
hand-written Requirement, and two silent degradations (D1, D4) remain. Our probe settles its main
open security question, and the answer is worse than it assumed: if the `identity` owner runs but
does not write `roles`, another module's `roles: ["admin"]` passes **the platform-admin gate
itself**, not only `Role`/`Perm` (§2.2). Its PR plan (PR-00..PR-10) is premature. It chooses
solutions (consumer-generated wrappers, two-phase settings) before the blind alternatives and
PoCs that our process requires. Use it as a source of scenarios and risks, not as the plan.

## 2. Claims

Status: **verified** (read at the cited line; "run" where a probe or experiment observed it),
**wrong**, **outdated** (true of an older commit or document, not of HEAD), **unverifiable**.

### 2.1 Table

| # | claim (roadmap section) | status | evidence |
|---|---|---|---|
| 1 | PR #70 added `RESERVED_CLAIM_KEYS` and a duplicate-key refusal in `_merge_claim_outcomes` (§3.1) | verified, run | `core/facade/_claims.py:62-87`, `:454-534`; validation A2 (mint 503) |
| 2 | Before PR #70 the merge was `claims.update(value)`, last writer wins (§3.1) | verified, run | baseline §14.2 (`_claims.py:442` at `b8d8506`); validation A2 pre-#70 column |
| 3 | Reserved: `iat`, `jti`, `iat_us`, `sub`, `tenant_id`, `org_id`, `claims_sources`, `claims` and the rest of the envelope and subject fields (§3.1) | verified | `_claims.py:62-87` (also `user_id`, `provenance_authoritative`, `iss`, `aud`, `exp`, `nbf`, `auth_time`, `token_type`); `tenant_id`/`org_id` by `e212893`, part of PR #70 |
| 4 | The owner of a key is its first writer in this token: `key_owner.setdefault(key, owner)` (§3.2) | verified | `_claims.py:525-534` |
| 5 | No pre-assigned map `roles → authority`, `tenant_ids → authority` (§3.2) | verified | guarantee-map §4 "authority"; no setting in `core/config/*` names one; requirements I-08 state |
| 6 | If the expected authority does not write a key, another contributor can become its only writer; to be checked whether a guard accepts it (§3.2) | verified, run | **gate probe G2–G4** (§2.2): `require_platform_admin` and `Role("admin")` allow; validation A3 (authority failed, non-critical): `Role`/`Perm` allow, platform 403; validation B6 (no grant provider): RLS opened |
| 7 | `CoreFacade.add_claims_contributor(..., owner=...)` takes the owner name from its argument, so a module can claim `owner="identity"` (§3.2 v2, §16.2 row 2) | **wrong** (premise) | the argument exists (`_claims.py:240-262`), but modules register through `ModuleAPI.add_claims_contributor(fn, *, critical)`, which has no `owner=` (`core/module_api/_registration.py:406-419`); the loader always passes a `ModuleAPI` (`core/loader/plugin_loader.py:305`, `:526`); `get_core()` returns the read-only `CoreRuntime` (`core/di.py:117-122`). The real path to the same effect is the module's short name: an app module named `identity` gets `claims_sources: ["identity"]` and the platform gates in a lean install (validation A6) |
| 8 | A contract name does not prove the owner; a remote owner is named by the binding (§3.3.5) | verified | `_claims.py:300-312` (owner = `SERVICE_BINDINGS` key minus `.claims.v1`); guarantee-map T6 |
| 9 | `require_platform_admin` and `platform_operator` are two implementations (§3.3.6) | verified | `shared/guards/platform.py:10-19`; `core/admin/api/platform.py:54-71`; they also differ: the operator gate checks `iat`, a UUID `sub` and revocation itself |
| 10 | Sandloom runs one app runtime per process ("Option A", agreed) (§3.3.6, §7, §17.1) | **unverifiable** | no record in `docs/adr/`, `design/` or tm (searched "Option A", "per process"). Code facts: the live-adapter table is a module global (`shared/authz/live_contracts.py:114`, reset for tests `:172-175`); claims contributors live on the `CoreFacade` instance (`_claims.py:268`) |
| 11 | Old signed tokens do not carry the new provenance model (§3.3.7) | verified | a token without `claims_sources_failed` is legacy (`shared/subject.py:117`) and claim-mode facts then trust `claims_sources` as written (`shared/authz/facts.py:300-307`); the platform gates read neither key's age nor `provenance_authoritative`; access tokens live 15 min by default (`core/config/auth.py:65-68`) |
| 12 | `create_app()` calls `enforce_configuration(settings)` before modules load (§7) | verified | `core/app_factory/__init__.py:166` before `load_modules_with_summary` at `:192`; `sandloom migrate` too (`core/cli/migrate.py:446`) |
| 13 | `Entitled` takes no fact parameters: `Entitled(fact_name, category, code, mode)` (§5, §17.2) | verified, run | `shared/authz/atoms_fact.py:48-55`; validation C gap 3 |
| 14 | `AccessFact` has claim / projection / live paths, no parameter schema (§17.2) | verified, run | `shared/authz/facts.py:177-198`; the remote `evaluate` carries path, query and `X-Tenant-Id` only (validation C1, `facts.py:422-447`) |
| 15 | `Or` allows on a definite branch even when another is indeterminate; `Not(indeterminate)` stays indeterminate (§5) | verified | `shared/authz/algebra.py:207-222`, `:259-262`; G-AUTHZ-13 law tests |
| 16 | Statuses 402/403/501/503 and the risk order are the observable contract (§5) | verified | `shared/authz/results.py:38-50`; `atoms_live.py:57-73` (501); validation C2 (503) |
| 17 | A19: `register_live_adapter` admits only `entitlements` and `membership`, one adapter per kind in the process-global `_ADAPTERS`; a second registration replaces the first (§17.1) | verified, run | `live_contracts.py:37-39`, `:114`, `:125-164`; baseline §3.1 probe (`_archive/_evidence/a19-live-adapter-probe.txt`) |
| 18 | B-R2: `tenant_kind` classifies a tenant by asking the membership adapter (§16.3) | verified, run | `shared/authz/account.py:153-198`; perf-dx §2.1 (the extra membership call) |
| 19 | `module_rules.py` and `api10_rules.py` recognise `OrgRole`, `Subscribed`, `Credits`, `require_org_role` by name, so aliases escape them (§17.3) | verified; alias bypass inferred | `core/archcheck/module_rules.py:57-64`, `:244-257` (bare call name); `api10_rules.py:167-178` (regex `\bOrgRole\(`), `:499-502`. Not run with an alias |
| 20 | 46 judged edges: A19 + 40 B + 5 C; 32 V, 13 D, 1 A; the matrix rows carry baseline's verdicts (§14) | verified | baseline §2 and §3–5; every row of the v2 matrix matches |
| 21 | The classification is at `b8d8506`, not automatically at `fe24a79` (§14 header) | verified; holds at HEAD | the only `src/` change since is the claims merge: no edge row moves; B-S3's neighbouring `tenant_id`/`org_id` are now reserved (I-10 state) |
| 22 | `sandloom-billing` on plain `sandloom` fails with `No module named 'celery'`; identity likewise (§7, §18.2) | verified (read at HEAD); identity inferred | `sdk/_prim/tasks.py:16-17`; `sandloom_billing/tasks.py:25`; `packages/sandloom-billing/pyproject.toml:14-16`; run at `b8d8506` (baseline §13.2); identity `sandloom_identity/tasks.py:19`, not run |
| 23 | The kernel hard-codes `sandloom.shared` as the artifact home in the generator and the DTO resolver (§4) | verified | `core/cli/generated.py:488`, `:576`; `core/module_api/_provider.py:113-114` |
| 24 | ADR-0010 is Proposed (§13) | verified | `docs/adr/0010-public-api-1.0.md:8` |
| 25 | ADR-0010 already allows modules to import generated contract stubs and DTOs besides `sandloom.sdk` (§17.4) | verified | `docs/adr/0010-public-api-1.0.md:56-57` |
| 26 | I-19 needs all 16 subsets of the four batteries (§15.1, §18.2) | verified, with a nuance | our I-19 says "every subset" (requirements.md:577), but no scenario enumerates them; A-B7 names four single wheels; the existing test boots 4 subsets in the repository environment, admin never (`core/tests/test_module_subset.py:40-51`) |
| 27 | The requirements are I-01..I-19 with 52 scenarios, Q1–Q12, H-1..H-13 (§15, §21) | **outdated** | first draft `4d982e3`; amended `333abb8`: I-01..I-23, H-1..H-19, 62 scenarios (17/14/18/13), Q1–Q15 |
| 28 | I-02, I-06, I-08/I-09, I-14 and H-12 need the amendments of §15.1 | **outdated** (made since) | requirements.md I-02 (loader allowed by descriptor origin), I-06 (equal capabilities, listed SQLite exception), I-08/I-09 (assigned authority, not first writer), I-14 (hard rule / measurement / threshold; a security fix not blocked), H-12 (removes a violation or provides an invariant, compared with existing mechanisms) |
| 29 | PR #70 is not on the inspected branch; the branches diverge (v1 header) | **outdated** | `fe24a79` merged into `arch/kernel-boundary` (`86aed8b`); `e212893` is in both |
| 30 | Owner question "may I-08/I-09 break apps" is open (old Q10) | **outdated** | answered: PR #70 shipped the one-owner rule with upgrade steps and a release note (`1702f68`); requirements §6 preamble |
| 31 | `claims_sources` lists owners that ran successfully, not owners of keys (§3.3.4, §16.1) | verified | `_claims.py:484-494` (`sources - failed`); docstring `:503-514` |
| 32 | In a split the issuer collects remote claims only through authenticated bindings (pair-MAC) (§3.3.5) | verified | `_claims.py:296-312`; ADR-0004; critical unless `"critical": false` |
| 33 | `Role`/`Perm` are claims-only and make no RPC (§3.3.4) | verified, run | `shared/authz/atoms_claims.py:35-67`; perf-dx §2.1 (0 calls, 0 DB) |
| 34 | Only the signing process holds the private key in an asymmetric split (§16.1) | verified | `core/auth/tokens.py:123-176`, called at `core/app_factory/lifecycle.py:370-373`; G-AUTH-05/-06 |
| 35 | `fake_access_fact` is billing-specific (B-R6) | verified | baseline B-R6 (`core/testing/authz.py:21-31`, unchanged) |
| 36 | `TENANT_CLAIMS_MAX_ORGANIZATIONS` is read only by tenancy (B-C2) | verified | baseline B-C2 (unchanged) |
| 37 | `shared/membership.py` holds tenancy's version-less event logic (`member_event_action`) (C5) | verified | baseline C5 (unchanged) |
| 38 | `Account.user()/.org()/.bound_tenant()`: unknown or canonical kind is indeterminate, never a user fallback (§16.3) | verified | `shared/authz/account.py:12-17`, `:86`, `:191-198` |
| 39 | Architecture-guard blind spots: import-linter outside `sandloom`, extras in the distribution closure (§9 item 10) | verified, run | baseline §9 (probe on synthetic wheels) |
| 40 | `sandloom graph` has no kernel node and shows declarations only (§18.3) | verified, run | baseline §7 (graph code unchanged since) |
| 41 | `uv run pytest -m guarantees src` and the split conformance suite exist (§18.4) | verified | `pyproject.toml:223`; `tests/split_conformance/test_split_conformance.py` |
| 42 | The proposed suites (`test_claim_authority_matrix`, …) are not in the repository (§18.4) | verified | none under `tests/architecture`, `src/sandloom/core/tests` |
| 43 | The +1 % instruction budget is ADR-0010's for request and write (§15 Q11) | verified | `docs/adr/0010-public-api-1.0.md:328-331`; requirements I-14 part 3 |
| 44 | Module code in the same process is not sandboxed; the kernel cannot defend against hostile installed Python (§3.2, §16.1) | verified | modules run in-process; any code holding the signing key mints anything (guarantee-map S5, T4); requirements Q13 asks the owner where the boundary is |

Counts: **38 verified** (10 of them also run), **1 wrong**, **4 outdated**, **1 unverifiable**.

### 2.2 The decisive probe: the authority ran but did not write the key

The roadmap's §3.2/§16.2 row 1 ("`identity` declared but does not return `roles`; `rogue` alone
returns `roles: ["admin"]`") was stated from reading the code. We ran it on HEAD with the real merge, the
real `require_platform_admin` and the real `Role` atom
([`_evidence/roadmap-review/gate-probe.txt`](_archive/_evidence/roadmap-review/gate-probe.txt)):

| case | merge | `require_platform_admin` | `Role("admin")` |
|---|---|---|---|
| G1 `identity` writes `roles: []`, `rogue` writes `roles: ["admin"]` | 503 (second writer) | — | — |
| G2 `identity` runs and writes only `permissions`, `rogue` writes `roles: ["admin"]` | token `roles: ["admin"]`, `claims_sources: [identity, rogue]` | **allow** | **allow** |
| G3 `identity` runs and returns `{}` | same | **allow** | **allow** |
| G4 `rogue` first, then `identity` without `roles` | same | **allow** | **allow** |
| G5 `rogue` writes `email`, `email_verified` | minted (keys not reserved; identity's mint overwrites them afterwards) | 403 | false |

The platform gates trust "`identity` ran", not "`identity` wrote `roles`"
(`shared/guards/platform.py:14-18`, `core/admin/api/platform.py:63-71`), so first-writer
ownership opens the gate whenever the `identity` owner omits the key. The bundled
`sandloom_identity` always writes both keys (`sandloom_identity/claims.py:61-69`), so the
standard deployment is not affected. A replacement issuer, an identity version that omits
`roles` for some users, or an app module named `identity` (validation A6) is. This is the gate
form of validation A3, which covered only the case where the authority failed.

## 3. Recommendations

Verdicts: **agree**, **disagree**, **covered** (already in goals.md, requirements.md or a tm
decision), **new** (not considered by us; where it belongs is in §4).

| # | recommendation (section) | verdict | reason and evidence |
|---|---|---|---|
| R1 | Kernel of guarantees + domain modules + consumer-owned interfaces; the application assigns authorities (§1) | covered | goals.md "Who owns what", "Guarantee, provider, trust" |
| R2 | No `IdentityDriver`/`TenancyDriver`/`BillingDriver`, no universal capability registry (§1, §11) | covered | goals.md limits; H-17 |
| R3 | Six-condition test of what stays in the kernel (§1.2) | covered | goals.md "platform capability rule"; H-18 |
| R4 | `sandloom.shared` removal is not a goal in itself (§2, §8) | agree; partly covered | Codex review and kb-assess description; goals.md does not say it (amendment G3) |
| R5 | Claims in three classes: envelope (kernel only), platform authority claims (assigned owner, mint refused for a foreign key even without collision), domain claims (owner schemas) (§3.3) | covered, with new detail | I-08 (assignment, verification, reserved keys), Q9 (which claims); new: "refuse even without a collision", which G2–G4 shows is needed |
| R6 | A generated default authority manifest for `sandloom new` that the app explicitly accepts (§3.3.2) | new | DX of I-22: no requirement says how a default app assigns authorities without hand-wiring each key |
| R7 | Guards check claim → authority, not "a known owner ran" (§3.3.4) | covered; evidence added | I-09; G2–G4 shows the platform gate fails it today |
| R8 | One platform admission predicate for both gates (§3.3.6) | covered | goals.md table; I-09 |
| R9 | Authority grants on an immutable runtime snapshot, not a process-global registry (§3.3.6) | new (partly) | I-22 T5 covers "first registrant"; the global `_ADAPTERS` and its test reset (`live_contracts.py:114`, `:172-175`) are not named as a problem; "Option A" is unrecorded (claim 10) |
| R10 | A policy for legacy tokens (minted before the provenance model) for privileged gates (§3.3.7, §16.2 row 6) | new | C-B3 covers tokens without `claims_sources_failed`; nothing covers tokens minted under an older authority assignment (they look identical, the per-key writer is not in the token) |
| R11 | Not acceptable: `config.get("admin_owner", "identity")` (§3.3) | covered | I-22 (a default by name is still trust by name); A-B3 |
| R12 | Contracts: consumer-generated copies from `sandloom generate`, no `*-api` wheels by default (§4) | defer (ADR-B) | a solution; `kb-alt-contracts` decides blind; consistent with Codex D2 |
| R13 | Each artifact carries owner, contract, version, schema fingerprint (§4) | covered (outcome) | B-F1 (stale copy refused), I-15; the fingerprint is a mechanism for ADR-B |
| R14 | Generation needs the provider's declaration in the build environment; do not promise generation from nothing (§4) | new | I-05 and B-H2 cover the runtime; no requirement covers where the consumer's copy comes from when the provider is not installed at build time (baseline §13.2: no tenancy stub was written when tenancy was absent) |
| R15 | Both upgrade directions, additive and breaking, `/ready` and 501 (§4, §18.1) | covered | I-12; B-B1..B-B4, B-F3 |
| R16 | Domain event JSON out of the kernel wheel (§4) | covered | I-11; C3 |
| R17 | Do not touch transport, codec or `CoreFacade` without a proven defect (§4, §11) | agree | H-12 |
| R18 | Keep the algebra; `Role`/`Perm` after provenance; domain atoms to owners; no `Account → Party` rename (§5, §11) | covered | goals.md "Authorization"; owner reflection on `kb-alt-authz`; Codex D4 |
| R19 | One PoC: (A) parameterised `AccessFact` vs (B) consumer `Requirement` wrappers; pick by cost (§5, §17.2) | covered | owner decision on `kb-poc-authz`: B first, then A, compare; C-B6, C-B7 |
| R20 | No new fact engine, keep adapters until parity (§5) | covered | H-12, H-16 |
| R21 | `describe()`/schema cached or loaded at startup, not per request (§5, §9) | covered; violated today | I-14 part 1; perf-dx §2.2: first request per bound contract does a `describe`, and `Subscribed` re-fetches facts metadata every 300 s (`facts.py:64`) |
| R22 | Policy metadata for `sandloom check`; no class names; a module cannot lower its risk (§5, §17.3) | covered | goals.md "three problems"; runtime metadata already exists (`Requirement.risks()`, `contracts()`, `org_params()`, `tier`; used at `shared/authz/deps.py:186-194` and by SANDLOOM-B007); only the static rules use names |
| R23 | Negative mutations for static rules: alias, qualified call, re-export, dynamic policy; unsupported shapes are diagnosed, not passed (§17.3) | new | no scenario covers rule evasion; `module_rules.py:244-257` matches bare names (claim 19) |
| R24 | A generated consumer policy API as a separate allowed import category, by an ADR-0010 amendment (§5, §15.1 I-17, §17.4) | new | ADR-0010:56-57 lists stubs and DTOs only; H-8 says "`sandloom.sdk` stays the only public surface", narrower than the ADR; Q5 asks about import lines, not about the category |
| R25 | Tenant: `TenantScope`/RLS kernel; `tenant_ids` a platform claim; `org_id` a transitional selector; single-tenant from config; no tenant from a header without a grant (§6) | covered | I-10, C-H6, goals.md; `org_id` is reserved since `e212893` |
| R26 | Platform guard stays kernel; without a trusted admin authority deny; incident command without identity gives a missing-provider message (§6) | covered | I-09, A-B1, A-B2 |
| R27 | Two-phase settings bootstrap: discover → validate → activate, one validation for app/worker/migrate/CLI (§7) | new | I-18 and D-H1 state the outcome; the ordering constraint (claim 12) is not stated; belongs to `kb-alt-settings` |
| R28 | No env read on import, no framework over pydantic (§7) | covered | D-F3, G-SDK-01; Codex "minimal ModuleSettings" |
| R29 | Keep today's env names in the first step; rename separately with a migration guide (§7) | new | H-9 forbids aliases but no criterion says renames are a separate, later change; migration-cost criterion |
| R30 | Celery: support billing on plain `sandloom` or declare the extra (§7, §18.2) | covered | I-19 P2, Q10; tm `billing-boots-without-celery` |
| R31 | CLI, starter, recipes after the four decisions; template words are not runtime edges; one minimal CLI extension (§8) | covered | kb-assess description; Q3; Codex |
| R32 | Fitness suite of 12 groups (§9) | covered, except two | covered by the requirements §5 scenarios; new: all 16 subsets (R33), rule mutations (R23) |
| R33 | All 16 subsets, each with an expected outcome or an owner-approved refusal (§9, §15.1 I-19, §18.2) | new (enumeration) | I-19 says "every subset"; scenarios test 4; admin needs identity's contract (`sandloom_admin/startup.py:14`), so some subsets must refuse by design |
| R34 | Performance contract: zero new hops, zero I/O on claims, same-hardware comparison (§9) | covered | I-14, H-10, perf-dx-baseline.md |
| R35 | PR-00..PR-10 plan with exit gates (§10, §19) | disagree (now) | it fixes solutions (consumer-generated wrappers, two-phase settings, removal of live adapters) before the blind alternatives and PoCs; usable later as a delivery checklist. Agree with two parts of it: security first (P0), and no single giant PR |
| R36 | Per-edge proposed decisions for all 46 edges (§14) | defer | solutions for the ADRs; must not reach the blind alternatives |
| R37 | A machine-readable edge register (`edges.yml`: id, status open/fixed/retained-approved, owner, decision, tests), a CI diff of new/removed/approved edges (§14.1, §19.1) | new | I-03 asks for "one approved place" without a status per baseline id; fits `kb-guards-honest` and ADR-A |
| R38 | Owner answers Q1–Q12 (§15) | outdated numbering; content mixed | see §3.1 |
| R39 | I-17/H-8, I-19, H-9 (rolling-upgrade wire compatibility separate from SDK aliases) amendments (§15.1) | new | the other §15.1 amendments are already made (claim 28); proposals in §6.1 (RA1, RA2, RA12) |
| R40 | Five trust roles: issuer, claim authority, binding identity, claim source, verifier (§16.1) | new | sharper than goals.md's "guarantee, provider, trust"; amendment G1 |
| R41 | An authority-policy version or fingerprint shared by issuer and verifier; a mismatch is a diagnosable incompatibility (§16.1) | new | in a split the verifier cannot tell under which assignment a token was minted; no requirement covers issuer/verifier policy skew |
| R42 | Negative tests of §16.2 | covered, one new | A-F2, A-F3, A-B3, A-F4, C-F4, C-B3; new: "the authority ran and omitted the key" (G2–G4), scenario A-F6 (§6.1 RA3) |
| R43 | B-R2: choose a platform tenant-kind port or a domain-only account resolver, not both; no hidden membership call (§16.3) | agree; evidence added | C-B4 covers the outcome; perf-dx: today `bound_tenant()` behind `OrgRole` already makes the extra membership call (3 calls, not 2) |
| R44 | A19: remove the live adapters after parity; a compatibility adapter only as an approved H-12 exception (§17.1) | defer (ADR-C) | consistent with baseline §3.1 (third-party checks already work through `Requirement` and `AccessFact`) |
| R45 | Old import → new import table, codemod, `public_api_baseline.json` review (§17.4) | covered, codemod new | I-17, H-9 (upgrade steps); a codemod is a migration-cost choice |
| R46 | Artifact release matrix incl. old version-less tenancy events and a byte-identical `sandloom` wheel when one module changes (§18.1) | covered, one new | B-H1, B-B2, I-15; new: C5's version-less event logic keeps its rolling-upgrade tests when it moves (scenario B-B7) |
| R47 | `sandloom graph` shows the kernel node and approved composition edges (§18.3) | covered | problems.md #11, "no change without its own ADR" |
| R48 | Go/no-go gates G0–G7; no "done" until all 46 edges have a status on one SHA (§19, §20) | agree (as release rule) | matches H-1; the edge status is R37 |

Counts over 48 recommendations: **agree 4** (R4, R17, R43, R48), **disagree 1** (R35 as a
plan now; its security-first order and small PRs agreed), **covered 26** (R42 adds one new
case), **new 13** (R6, R9, R10, R14, R23, R24, R27, R29, R33, R37, R39, R40, R41), **deferred
to the ADRs 3** (R12, R36, R44), **outdated 1** (R38).

### 3.1 The roadmap's owner answers, mapped to our current questions

Its Q-numbers are the first draft's. Mapping and our view (for the owner; no decision here):

| roadmap | ours (requirements §6) | its recommendation | our view |
|---|---|---|---|
| Q1, Q2 | Q1, Q2 | approve after amendments; no mechanism ceiling | the amendments are made; the ceiling is already dropped (H-12) |
| Q3 | Q3 | strict for shipped kernel; kernel tests may import batteries explicitly | consistent with baseline §6.2 |
| Q4 | Q4 | byte-identical within one generator version; a justified diff across versions | a usable answer to B-H3 |
| Q5 | Q5 | app gate code may change, the kernel may not; none if the declared API is equal | leaves Q5's second half (the import line, I-23/H-19) open |
| Q6 | Q6 | no warning by default | — |
| Q7 | Q7 | third-party semantic settings classified, not counted in the 16 | — |
| Q8 | Q8 | refuse an unknown/inactive risk id in every entry point | — |
| Q9 | Q9 | every security claim the kernel reads, under an assigned authority; domain claims by owner schemas | does not answer `email`/`email_verified` (G5) |
| Q10 | answered | — | (old: may I-08/I-09 break apps) |
| Q11 | Q14 | +1 % for request/write; authz and mint measured; no CI gate without a reproducible harness | perf-dx-baseline.md gives the harness and the noise (±4 %), which is above 1 % on the authz stand |
| Q12 | answered | — | (old: scope of CLI/starter) |
| — | Q10–Q13, Q15 | not addressed | P2 extras, D4 refusal, D1 duty, T4 mint scope, the backend exception list. Its threat model (§16.1: installed code is trusted) amounts to an answer to Q13: the signing key is the boundary |

## 4. New to us, and where each goes

| # | item | what to do | where |
|---|---|---|---|
| N1 | **Platform gate opens when the authority ran but omitted the key** (G2–G4; roadmap §16.2 row 1, now run) | add to problem #1/#6; scenario A-F6 | problems.md (done); requirements amendment RA3 |
| N2 | **Settings are validated before modules load** (claim 12): module settings must be declared before activation, with one validation for app, worker, migrate and CLI | an ordering constraint for every settings alternative | problems.md #7 (done); `kb-alt-settings`; scenario D-B6 |
| N3 | **Issuer/verifier authority-policy skew and tokens minted under an older assignment** | an invariant on issuer/verifier consistency; an owner question on the legacy-token policy | requirements I-08/I-22 amendment, Q16; ADR-C |
| N4 | **All 16 module subsets**, with the expected outcome per subset (some refuse by design: admin without identity) | enumerate in I-19 / A-B7 | requirements amendment; `billing-boots-without-celery`, `kb-guards-honest` |
| N5 | **Machine-readable edge register** with a status per baseline id and a per-change diff | the proof form of I-03 | `kb-guards-honest`, ADR-A |
| N6 | **Generated consumer interfaces as an import category** need an ADR-0010 amendment if decision C or B adopts them | the condition attached to such an alternative | H-8 wording; `kb-alt-contracts`, `kb-alt-authz` |
| N7 | **Static rules evaded by alias, re-export or a dynamic policy** | scenario C-F6 (a sensitive route behind an aliased atom is classified, or the shape is reported) | requirements amendment; ADR-C (policy metadata) |
| N8 | **Build-time source of a consumer's copy** when the provider is not installed where `sandloom generate` runs | scenario B-B6 | requirements amendment; `kb-alt-contracts` |
| N9 | **A default authority assignment for `sandloom new`** that the app accepts explicitly (no per-key hand wiring) | DX criterion for I-22 | requirements §4.2; ADR-C |
| N10 | **The five trust roles** (issuer, claim authority, binding identity, claim source, verifier) | vocabulary for goals.md and ADR-C | goals.md amendment G1 |
| N11 | **Process-global mutable state** (`_ADAPTERS`, reset for tests) vs one runtime per process: the decision is not recorded | ask the owner; record it | Q17 |
| N12 | **Env names unchanged in the first step**; renames are a separate change | migration-cost criterion | requirements §4.2 |
| N13 | **Rolling-upgrade wire/event compatibility** is a guarantee separate from SDK aliases (C5's version-less events) | scenario B-B7; H-9 note | requirements amendment |
| N14 | **Threat model:** in-process installed code is not sandboxed; protections target supported extension APIs and remote providers | a proposed answer to Q13 | goals.md amendment G2 (after the owner answers Q13) |

## 5. What the roadmap misses that our evidence shows

| # | finding | evidence | problems.md |
|---|---|---|---|
| M1 | A module can register a fact under another module's name and so serve `<that module>.facts.v1`; every SKU allowed, no log. Remotely the owner is the provider's own `describe()` | validation C4; `core/facade/_services.py:219-240`; guarantee-map T7 | new #13 |
| M2 | One non-critical contributor that writes a foreign or reserved key aborts **every** mint (503), and when it is registered first the refusal names the authority | validation A2, §1.3 gap 3; `_claims.py:525-533` | new #14 |
| M3 | Remote metadata on the request path: one `describe` per bound contract on first use, and facts metadata again every 300 s | perf-dx §2.2; `core/rpc/proxy.py:656`, `facts.py:64` | new #16 |
| M4 | `OrgRole & SubscriptionActive(of=Account.bound_tenant())` makes 3 contract calls (3 RPCs in a split), not 2: `tenant_kind` asks membership again | perf-dx §2.1; `account.py:153-168`, `:239` | new #16 |
| M5 | A combinator's tier is its most expensive operand at wiring time: `Role("admin") \| Credits(10)` opens a request DB session for an admin who never reaches `Credits` (≈ +0.5 M instructions) | perf-dx §2.1, §2.3; `algebra.py:209`; `deps.py:121-123` | #16 (context) |
| M6 | The only typed, parameterised atom today is a 31-line `Requirement` the consumer writes over the provider's stub, with four advanced-tier names; no doc shows it; its provider outage is a proxy 503, not an indeterminate decision (no deny log, no metric) | perf-dx §3; `_archive/_evidence/perf-dx/dx/files/reports_access.py.txt` | new #15 |
| M7 | A fact takes parameters only from the request (path, query, `X-Tenant-Id`); a handler-computed value cannot reach it | validation C gap 3 | #15 |
| M8 | Without a tenant-grant provider in the token, any contributor writing `tenant_ids` gets those tenants bound and Postgres RLS opened to their rows | validation B6 | #1 |
| M9 | D1: the durable applier of `core.auth.tokens_revoked.v1` and the refresh-session end on block exist only in identity; without identity a lost mark is only counted | guarantee-map D1, §3; I-21, Q12 | new #17 (silent degradations) |
| M10 | D4: in a consumer-only install the compatibility check is skipped with a WARNING, also under `strict`; seen again for app-local providers | guarantee-map D4; validation C gap 4 | #17 |
| M11 | D2, D3: `Subscribed` without billing and `Role`/`Perm` without identity trust any writer | guarantee-map D2, D3 | #17 |
| M12 | The mint authority is the signing key: any code in the issuer process calls `create_access_token` with any claims and provenance | guarantee-map S5, T4; Q13 | #1 |
| M13 | `email`, `email_verified` are not reserved; only identity's mint order protects them (G5) | guarantee-map §13; gate probe G5; Q9 | #1 |
| M14 | The duplicate-fact refusal names one module (C-F3 asks for both) | validation C gap 2 | #13 |
| M15 | `/ready` is 200 while the default taskqueue cannot open; a replacement serving `identity.security.v1` gets the 8/100 password policy silently | guarantee-map §9, §11 | #9, #10 |
| M16 | Our amended requirements: I-20 (no silent degradation), I-21 (the kernel keeps the general protocol), I-22 (T1–T10), I-23 (moving an atom changes no execution), H-14..H-19, Q10–Q15 | requirements.md (`333abb8`) | — |

## 6. Proposed amendments (for the owner; the files are not edited)

### 6.1 requirements.md

| id | change | why |
|---|---|---|
| RA1 | **H-8 / I-17:** "modules import `sandloom.sdk`, generated contract stubs and DTO copies (ADR-0010 decision 1) and their own package; any other generated consumer interface is allowed only after an ADR-0010 amendment" | H-8 is narrower than ADR-0010:56-57; R24 |
| RA2 | **I-19 / A-B7:** list the 16 subsets of identity, tenancy, billing, admin for P2 and P3, each with its expected outcome: boots and becomes ready, or refuses at startup with a code naming the missing contract or extra (for example admin without identity); a narrower guarantee is an owner exception in writing | R33; `test_module_subset.py` covers 4 |
| RA3 | **New scenario A-F6:** the assigned authority runs successfully but omits `roles` (or `tenant_ids`); another module writes it | `require_platform_admin`, `platform_operator`, `Role("admin")`, `TenantScope` do not trust it; the mint is refused or the gate denies; mono and split | N1, gate probe G2–G4 |
| RA4 | **I-09 state:** add "the platform gates trust `roles` from any writer when `identity` ran without writing them (gate probe G2–G4)" | evidence at HEAD |
| RA5 | **I-10 state:** replace "Open: who may write `tenant_ids` …" with the observed B6 (no grant provider → a contributor's `tenant_ids` binds the tenant and opens RLS; validation B6) | evidence at HEAD |
| RA6 | **I-22 T7:** "violated: in-process, any module registers a fact under another module's owner and serves its facts contract (validation C4); remote, the provider's `describe()`" (today: "read, not run") | M1 |
| RA7 | **I-14 state, part 1:** "violated for the remote tier: first use of a bound contract does a `describe` on the request, and facts metadata is re-fetched every 300 s (perf-dx §2.2)" (today: "no proof") | M3 |
| RA8 | **I-23:** "does not increase" the network calls and database queries (a decrease is allowed and reported); record the baseline counts of perf-dx §2.1, including 3 calls for `OrgRole & SubscriptionActive(of=Account.bound_tenant())` | M4: "does not change" would freeze a known extra call |
| RA9 | **New invariant or I-08 addition, issuer–verifier consistency:** in a split, a verifier and an issuer with different authority assignments are detected at startup or `/ready`; a token minted under an older assignment is handled by the owner's policy (Q16), never trusted for a privileged gate by default | N3 |
| RA10 | **New scenario D-B6:** an invalid module setting is refused before any of that module's routes, connections, migrations or startup tasks take effect, with the same code in the app, the worker, `sandloom migrate` and the CLI | N2; claim 12 |
| RA11 | **New scenario B-B6:** consumer generation with the provider not installed in the build environment: the copy is generated from a provider-published declaration, or `sandloom generate` refuses naming the missing source; never a silent absence (baseline §13.2) | N8 |
| RA12 | **New scenario B-B7:** rolling upgrade with old version-less `tenancy.member_*` events: the order/duplicate/late-event decisions of C5 are unchanged after the logic moves | N13 |
| RA13 | **New scenario C-F6:** a sensitive (admin, money, tenant) route gated by an aliased, re-exported or dynamically built atom: `sandloom check` classifies it, or reports the shape as unsupported with a code; the runtime admin risk tag still applies | N7 |
| RA14 | **I-03 proof:** the approved list records every baseline edge id with a status (open, fixed, retained-approved) and its decision; a change reports added, removed and approved edges | N5 |
| RA15 | **§4.2 criteria:** add "how the default app assigns authorities (no per-key hand wiring)" to Operations/DX, and "env names kept in the first step; a rename is a separate change with upgrade steps" to Migration cost | N9, N12 |
| RA16 | **New scenario A-F7 (availability):** a non-critical, non-authority contributor writes a foreign key: the owner decides (Q18) whether every mint fails (today) or the contribution is dropped and logged while the authority's keys stand | M2 |
| RA17 | **§6 questions:** Q16 legacy-token policy for privileged gates (refuse, re-issue, short rollout); Q17 is one app runtime per process the rule (then process-global tables are acceptable), or must two apps share a process; Q18 the availability trade-off of RA16 | N3, N11, M2 |
| RA18 | **Header and §1:** cite HEAD `86aed8b` for `_claims.py` (PR #70 is merged into `main` `e264b09` and this branch) | claim 29 |

### 6.2 goals.md

| id | change | why |
|---|---|---|
| G1 | Under "Guarantee, provider, trust", name the five roles: **issuer** (signs tokens), **claim authority** (assigned writer of a security claim, may differ from the issuer), **binding identity** (the service bound in trusted configuration, not a contract name), **claim source** (a contributor that ran; not proof it wrote a key), **verifier** (checks signature, issuer, audience, revocation and the authority policy) | N10; G2–G4 is "claim source taken for claim authority" |
| G2 | After the owner answers Q13: "Installed module code runs in the process and is not sandboxed. The kernel protects against wrong or unauthorised claims through its supported extension APIs and from remote providers; a process that holds the signing key is the trust boundary for minting." | N14 |
| G3 | Under "What the rule means": "Removing `sandloom.shared` is not a goal; the goal is that the framework owns no module's interface or artifact. Where the remaining platform primitives live is secondary." | R4 |
| G4 | Under "How to apply it": "A check judges shipped runtime code; a module's name in a template, a recipe, documentation or a kernel test is classified separately (Q3) and is not by itself a runtime edge." | roadmap §8; Codex |

## 7. The problems, and how each is solved, in plain words

The basis for a chat report. "How" names the direction the owner has adopted and the task that
will choose the mechanism; it is not a chosen design.

1. **Anyone can hand out admin rights when the real issuer stays silent.** If the identity
   module runs and does not write `roles`, another module's `roles: ["admin"]` passes `Role` and
   the platform-admin gate. Solve: the application names who may write `roles`, `permissions`,
   `tenant_ids`; the kernel refuses a mint where anyone else writes them, and gates check that
   the named authority wrote the key, not just that it ran (ADR-C, then a security PR).
2. **Without tenancy, any module can grant itself a tenant**, and RLS opens to that tenant's
   rows. Solve: the same assignment for `tenant_ids`; no assigned grant provider means no
   tenant (ADR-C).
3. **The platform-admin gate trusts the name `identity`.** Solve: one platform admission
   for both gates, trusting the assigned authority (ADR-C).
4. **Facts can be answered by an impostor.** A module can register a fact under another module's
   name. Solve: a fact's owner is the module that registers it, or the bound service, never its
   own word (ADR-C).
5. **One buggy module can stop every login** by writing a key it does not own. Solve: an
   owner decision between "fail every mint" and "drop that contribution and log" (Q18).
6. **The kernel carries billing and tenancy rules** (`Subscribed`, `Credits`, `OrgRole`,
   `member/admin/owner`, the two adapter kinds, `WELL_KNOWN_FACT_OWNERS`). Solve: the algebra
   stays; the atoms move to their owners and stay typed in a consumer process without the
   provider installed. The PoC compares consumer-written requirements with parameterised
   facts, at the same call counts and syntax (`kb-poc-authz`).
7. **A typed check with a parameter is hard to write today** (31 lines, four advanced names,
   no docs) and a fact cannot take a computed argument. Solve: the same PoC; measured by the
   DX task in perf-dx §3.
8. **Module interfaces ship in the kernel wheel** (stubs, DTOs, 34 event schemas). Solve: each
   module's artifacts are released with the module, and consumers get their copies without a
   kernel release (ADR-B, `kb-poc-contracts`).
9. **Module settings live in the kernel, and the kernel validates settings before it loads
   modules.** Solve: modules declare their settings; the kernel keeps tiers, risks, redaction
   and one validation for every entry point, run before the module takes effect (ADR-D).
10. **Guarantees vanish quietly without a battery**: the revocation applier without identity,
    the compatibility check without the provider's package, `Subscribed`/`Role` without
    billing/identity. Solve: each one either fails closed or the process refuses to start (I-20;
    Q11, Q12).
11. **Packages do not install as advertised**: billing on plain `sandloom` needs celery; only
    4 of 16 module combinations are tested. Solve: fix the import or declare the extra, and
    test all 16 subsets (`billing-boots-without-celery`, I-19).
12. **The guards cannot see the problems**: import-linter skips `sandloom_*`, the name test
    covers four directories, and static rules match class names (an alias escapes them). Solve:
    honest guards, an edge register with a status per edge, and mutation tests (`kb-guards-honest`).
13. **Hidden calls on the request path**: a `describe` on first use and every 5 minutes, and an
    extra membership call for `bound_tenant()`. Solve: metadata known at startup; the move must
    not add calls; removing the extra call is allowed (I-14, I-23 amendment).
14. **The CLI, templates and graph name the standard modules.** Solve: after A–D; template text
    is judged apart from runtime code.

## 8. Method and limits

- Read both roadmap versions in full; every claim was checked against HEAD source, not against
  the roadmap's links. Our documents were used where their probes settle a point (validation.md
  §1, guarantee-map §11–12, perf-dx-baseline §2–3).
- One probe was run (§2.2): `uv run` in the checkout, `SANDLOOM_TEST_OWNER=kb-roadmap`, no
  database, no Redis contacted (dummy URLs on unused ports), no containers.
- Not run: the alias evasion of the static rules (claim 19, read from `module_rules.py:244-257`),
  identity on plain `sandloom` (claim 22), the gate probe in a split (the gates read the
  token only, so inferred identical) and through a real login with a replacement issuer.
- The scratch proposals (`target-architecture.md`, `codex-review.md`, `reconciled.md`) were
  read for context. The roadmap's "8-phase Claude plan" is `target-architecture.md`, already
  replaced by `reconciled.md` and by the assessment process.
