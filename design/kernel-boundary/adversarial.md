# Kernel boundary: adversarial review of the alternatives

tm `kb-adversarial` (parent `kb-assess`). Reviews the four alternatives files against
[requirements.md](requirements.md) (invariants I-01..I-24, hard constraints H-1..H-20, scenarios
of §5), [goals.md](goals.md) and [owner-answers.md](owner-answers.md) ("answer #n"). It decides
nothing; the ADRs decide after the PoCs.

| alternatives file | favoured there | reviewed in |
|---|---|---|
| [alternatives-a-boundary.md](alternatives-a-boundary.md) | A1 (names rule + edge register, M1-M5), A4's checks as tests | §2 |
| [alternatives-b-contracts.md](alternatives-b-contracts.md) | B1 delivered as B4 | §3 |
| [alternatives-c-authz.md](alternatives-c-authz.md) | TA-1 (trust), PA-2 (portable atoms) | §4 |
| [alternatives-d-settings.md](alternatives-d-settings.md) | D2a on top of PR #71, hashing H1 | §5 |

**Method.** Phase 1, blind to the earlier proposals: requirements, goals, owner answers, the four
files, the evidence they cite (baseline, guarantee-map, validation, perf-dx-baseline,
research-*.md) and the code. Every alternative was read for invariant and hard-constraint
violations, counterexamples from the §5 scenarios, hidden mechanisms and states, claims without
evidence, and cross-decision conflicts. The favoured option of each decision was attacked
hardest. Phase 2, after phase 1 was written down: [roadmap-review.md](roadmap-review.md),
`_inputs/chatgpt-roadmap-v2.md` and the scratch proposals `target-architecture.md`,
`codex-review.md`, `reconciled.md` (§7).

**Code read** at `arch/kernel-boundary` HEAD `ceb3d66`; `git diff --stat 44451e4 ceb3d66 -- src`
is empty, so the `44451e4` citations of requirements.md hold. Paths are under `src/sandloom/`
unless they start with `sandloom_<m>/` (`src/`) or name a repository file. Labels: **[read]** code
read at the cited line in this pass; **[cited]** taken from the cited evidence file, not re-run;
**Unverified** a claim nobody ran or read.

**Severity.** **Blocker**: as written the alternative breaks a hard constraint, or two decisions
cannot both be adopted; the ADR cannot be accepted until it is resolved. **Major**: a scenario
or invariant fails or depends on an unstated mechanism; it changes the design or the PoC.
**Minor**: wording, counting or a latent case; it does not change the choice.

## 1. Summary

| decision | favoured | blockers | majors | still standing? |
|---|---|---|---|---|
| A | A1 | 0 (shares X1 with C) | 5 | **Yes, smaller.** M3 (contributed CLI commands) is not needed for I-02 and pulls the CLI phase into decision A; M2 must not make admin import other modules; M4 must pass the lean `/ready` scenario |
| B | B1 / B4 | 0 | 3 | **Yes.** Needs an exemption for kernel-owned contract families in B1-M3, the consumption set from declarations, and a ruling on contract-name ownership shared with C |
| C (a) trust | TA-1 | 1 (X1) | 4 | **Yes, if X1 is decided.** The map for the shipped default app (`sandloom.main`) has no home; `cpv` needs a rollout rule; the sealed mint must also seal `collect_claims`' inputs |
| C (b) atoms | PA-2 | 1 (C-1) | 5 | **Candidate, not yet favoured.** It removes a public capability without the H-2 exception, leaves the source of tenant-kind metadata open, widens the trust surface to every contract that carries an access check, and its "equal by construction" claim does not hold for `Credits` |
| D | D2a, H1 | 0 | 3 | **Yes.** `config dump` collect mode needs a process-wide switch; the topology check of `sandloom check` is Unverified; answer #31's "no activation" vs module `setup` must be settled |

Cross-decision: 1 blocker (X1) and 8 conflicts (§6). Consolidated owner decisions: §8.

## 2. Decision A

### 2.1 Findings on A1 (favoured)

| id | sev | finding | evidence | resolves it |
|---|---|---|---|---|
| A-1 | major | **M3 hides a new entry-point group or imports every module for `--help`.** A1 says a module "declares commands on its descriptor; `sandloom` reads the declarations from entry-point metadata and imports the module only when the command runs" (§3.2 M3). The `sandloom.modules` entry point's value is the descriptor object (`sandloom_identity:register`); its command list is Python data, readable only by importing the module. Reading names without import needs a separate entry-point group (one entry per command), which is a new mechanism by requirements §4.2 ("a new entry-point group") and is not counted. App-local modules (`LOCAL_MODULES`, `<app>/modules`) have no distribution metadata at all, so their commands are found only by importing the composition root, which a static command must not build (requirements §1, "Process model"). D's own analysis says the same about settings: "No import is saved by an entry point" (alternatives-d §5.3, research O3). | `src/sandloom/__init__.py:88`, `:144-147` [read]; `main.py` (descriptors discovered through `installed_modules()`) [read]; reconciled.md §7 counts it honestly as "one `sandloom.cli` entry-point group" | Count M3 as a new entry-point group (or drop it, A-2); state the outcome for app-local modules; PoC: `sandloom --help` in P2 with one module whose import fails (A-B7) |
| A-2 | major | **M3 is not the minimal fix for I-02 and moves the CLI phase into decision A.** requirements §1 and problems.md #10 put the CLI, starter and recipes after A-D. B-I1 (the only I-02 violation) can be removed without moving any command: drop the kernel-side password-rule check and let the provider's `create_admin` refuse a weak password (it must validate anyway), or ask the contract the CLI already calls. B-R4/B-R5 then stay `deferred` rows of the register until the CLI phase, which the register is designed to carry. H-12 asks for the mechanism with the lowest cost that removes the violation. Codex (codex-review "CLI via new entry points: partly; separate task") and roadmap-review R31 say the same. | `core/cli/password_check.py:21`, `:40-44` [cited baseline B-I1]; requirements §1 "What this file does not decide"; problems.md #10 | Owner: CLI move inside A or in the CLI phase (§8 #A3). If deferred, A1 has 4 mechanisms |
| A-3 | major | **M2 makes `sandloom_admin` import other modules' code.** A1 moves `core/admin/compiler.py` into `sandloom_admin`; AD-5 says "admin's compiler imports `<module>.admin` of every loaded module, as `compiler.py:462-470` does today". Today the importer is the kernel's generic loader (class (a), allowed by I-02 as a descriptor-derived submodule). Moved into a business module, it is module A importing module B (I-04, H-3, "The one rule"), dynamically. | alternatives-a §2 row "manifest compiler", §5 AD-5; I-04; AGENTS.md "The one rule" | The kernel collects each module's resource declarations (it already imports `<module>.admin` as loader) and hands admin's renderer data, not modules; count that as part of M2. Check: `sandloom check` and `test_module_boundaries.py` see no admin → module import |
| A-4 | major | **M4 (a kernel durable consumer "in every composition") vs I-01 / A-H1.** A durable consumer is registered `required=True` by default, which "gates application readiness on the live subscription" (`core/module_api/_registration.py:444-470`). A lean `create_app()` with `SANDLOOM_MODULES=[]` must answer `/ready` 200 (I-01, A-H1); with M4 it needs an event bus and a durable inbox, or M4 must register only when something can publish `core.auth.tokens_revoked.v1`. Group placement in split: today a consumer group is `<module>:<name>`; a kernel consumer in N services needs one shared group (the marks live in the shared `CACHE_URL`, `deploy/compose/microservices.yml:92-96`), so the group name must not depend on the service. A1 marks feasibility Unverified (§9.2); the lean-ready and group cases are not in its PoC list. C counts the same piece (R1) as "no new mechanism"; A counts it as one (minor, A-8). | `sandloom_identity/__init__.py:73-84` [read]; `core/module_api/_registration.py:458-466` [read]; validation §1.1 ran with `EVENT_BUS_BACKEND=memory` | PoC rows: A-H1 lean `/ready` with M4; split with M4 in 3 services, one mark lost after commit, applied exactly once; a service whose eventbus capability is absent |
| A-5 | major | **The console's issuer test does not work in split, and its "operator-login interface" inverts contract ownership.** A1 replaces `"identity" not in modules` with "a module of the composition declares `issues_tokens=True`" plus "an operator-login interface that admin documents and an issuer serves". In split the admin service does not compose the issuer, so the test sees no issuer. An interface named and documented by the consumer (admin) and served by the provider (identity) is a consumer-defined contract: B generates consumer copies from a provider-declared contract, and C's widened M3 refuses a contract whose first segment is not the registering module. Both break for `admin.<x>.v1` served by identity. Unverified (A1 says so, AD-3). | alternatives-a §3.2 "Admin tooling", §9.4; owner decision on `kb-alt-boundary` gap (1) "check existing token/auth contracts first" | Owner and AD-3 PoC: identity declares its own login contract and admin consumes it optionally (no inversion), or admin reads the token issuer from the access token (`iss`) and the existing login routes |
| A-6 | minor | M5 checks that an `issues_tokens=True` module registers a `user_blocked` consumer. A stateless issuer (the PoC's `test_identity`, an OIDC bridge) has no refresh sessions and must register an empty consumer to start. "Executed" (answer #16) stays open (A §9.3). | `poc/replace/app/pocapp/modules/test_identity/__init__.py:29` (`issues_tokens=True`) [read] | Let the issuer declare "no refresh sessions" explicitly; PoC T-16 / A-F5 with `test_identity` |
| A-7 | minor | A-B2 wording assumes a kernel-hosted command; A1 flags it (§9.8). Under A-2 (commands stay until the CLI phase) the wording holds unchanged. | requirements A-B2 | Owner, with §8 #A3 |
| A-8 | minor | Mechanism counting is not shared across files: A counts the kernel applier as M4, C counts R1 as "no new mechanism"; A counts M5, C counts R2. The ADR needs one ledger (H-12). | alternatives-a §3.2; alternatives-c §2.2 "Revocation duties" | One mechanism table in the ADRs, keyed by violation id |
| A-9 | minor | The register's literal scan misses names built at run time (`f"{m}.entitlements.v1"`); A1 states it and relies on the replaceability fixtures. After C, the kernel builds `<m>.claims.v1` / `<m>.facts.v1` names from descriptors by design; the scan must treat those templates as protocol, not findings. | alternatives-a §4 "known limits" | Register rows for the protocol templates (class a, retained) |

**Claims checked.** "A1 is the only alternative that satisfies I-02 and I-03 with the fewest
mechanisms": holds for I-03 (the register is the proof form RA14 asks for); for I-02 a smaller A1
without M3 also passes (A-2). "The replaceability PoC shows the existing mechanisms carry the
replacements with no kernel change": holds as cited (validation §1.2: every failure is a trust gap
or a name in kernel code). "M4 needs no other change": Unverified (A-4).

### 2.2 The other alternatives of A

| alt | finding | sev |
|---|---|---|
| A0 | Fails I-03, I-13, I-20, I-22 as A states; keeping its guards as a layer is right | — |
| A2 | P1 (`platform.sessions.v1`) is the only port that passes H-18, and only if the owner rules incident revocation a platform guarantee. A kernel-owned contract family `platform.*` then needs the same ownership check as `<m>.claims.v1` (A notes it). Codex ("D10 partial: keep platform incident mechanisms in core") leans to A2's position on this one point | minor |
| A3 | Its startup gate overlaps small fix #A and answer #15 defers claim-key manifests; A's rejection holds | — |
| A4 | Cannot express I-03 (A-F1 passes it); its checks are the missing proof of I-13, I-19, I-20, I-22 in every alternative; A's use of it holds | — |

## 3. Decision B

### 3.1 Findings on B1 / B4 (favoured)

| id | sev | finding | evidence | resolves it |
|---|---|---|---|---|
| B-1 | major | **B1-M3 refuses startup for kernel-owned contract families.** "With no expectation for a required remote contract, `strict` refuses startup naming the contract" (§3 B1 step 4). The kernel calls `<m>.claims.v1` remotely for every claims binding, critical by default (`core/facade/_claims.py:296-312`), and `<owner>.facts.v1` for `Entitled`. Neither has a stub, so neither has an expectation file beside a stub. Under `strict` every split with a remote claims contributor would refuse. B's open point 1 covers "hand-written `ContractRef`", not these families. | `core/facade/_claims.py:296-312` [read]; alternatives-b §10.1 | Rule: a kernel-owned family is compared against the kernel's own schema (shape is the kernel's, same for every owner); PoC item 3 adds a split with a remote `grants.claims.v1` under `strict` |
| B-2 | major | **The consumed set is read from imports, while consumption is already declared.** B1-M1 derives which stubs a module gets "by AST, from the module's imports of its own `_generated.contracts.<stem>`". Modules already declare what they consume (`consumes` in `module.snapshot.json`; billing `required=False`, admin `optional=True`). Two sources can disagree: a `consumes` entry with no import (no copy, so no expectation, so B-1's refusal), or an import of a stub for atoms only (C's PA-2 atoms live in the stub module). | alternatives-b §1.2 "Cross-module consumption … their `module.snapshot.json` `consumes`"; `sandloom_admin/startup.py:18` [cited requirements I-19] | Derive the set from the declared `consumes` (one source), import-driven only as a check that a declared contract is used |
| B-3 | major | **Contract-name ownership decides whether S6a passes.** B scores S6a "pass+": `acme_orgs` serves `tenancy.membership.v1` and consumers' copies decode it. C's M3 widened (every contract's first segment checked against the registering module) refuses exactly that registration unless the application assigns the replacement, and PA-2 makes `tenancy.membership.v1` an access-bearing contract whose in-process provider must be trusted (C-4). B leaves the rule to A/C (§10.5); C leaves it to its PoC flag (§9). Nobody owns it. | alternatives-b §4.2 S6a, §10.5; alternatives-c §2.2 M3, §9 | One owner decision (§8 #X3): replacement under the original name with an application assignment, or replacement under its own name with import changes |
| B-4 | minor | **Closed DTOs.** B's S1 says an old copy ignores an added field "(pydantic default `extra="ignore"`)". The copier copies `extra` from the provider's `model_config` (`core/cli/dto_copies.py:414-418`, `"extra"` in `_CONFIG_COPIED`), so a provider DTO with `extra="forbid"` makes an old copy reject an additive field, which `compare_contract` classifies as compatible. No bundled DTO is closed today (reconciled.md §3 gap 3). | `core/cli/dto_copies.py:414-418` [read] | Record `extra` in the describe and classify an added field on a closed result DTO as breaking, or render copies with `extra="ignore"`; PoC item 4 |
| B-5 | minor | **"The generator reads the provider's L1 declarations without composing it"** (step 5) is Unverified: today it renders from the registered implementation (`facade.service_registry.try_get`, `core/cli/generated.py:514`), which needs registration. B lists it as PoC item 7. | alternatives-b §1.1 | PoC item 7 |
| B-6 | minor | B and C propose two different fixes for the first-request `describe` (I-14 remote tier): B's local expectation describe confirmed by fingerprint (§7.4), C's K6 seeding from the boot readiness probe (needs the provider reachable at boot). | alternatives-b §7.4; alternatives-c §4 K6 | Choose one in ADR-C; B's needs no network at boot |
| B-7 | minor | In mono, B1 adds more processes holding two copies of one DTO (`<app>.shared` and `sandloom_billing/_generated`); a hand-written `ContractRef(name)` decodes into the first loaded copy. B states it (§4.4). | `core/contract_wire.py:765-767` [cited] | PoC item 8 records the class; consider refusing an untyped `ContractRef` when two copies exist |

**Claims checked.** "B1 fits ADR-0010 decision 1 as written": holds; decision 1 allows "generated
contract stubs and contract DTOs" and the ADR's note puts ecosystem copies in the consumer's own
package (`docs/adr/0010-public-api-1.0.md:56-57`, `:309-311`) [read]. "No new wheel, no new import
category": holds. "Removes D4": holds for stubbed contracts, not for the kernel families (B-1).
"No request-path work": holds.

### 3.2 The other alternatives of B

B0 fails the goal ("the framework owns no module's interface or artifact"), as B says. B2's gains
depend on a consumer that cannot install its provider; none exists in the repository (B4's trigger
is the right gate). B3 needs an ADR-0010 amendment and makes a replacement depend on the replaced
module's contracts package; B's rejection holds.

## 4. Decision C

### 4.1 Part (a): findings on TA-1 (favoured)

| id | sev | finding | evidence | resolves it |
|---|---|---|---|---|
| X1 | **blocker** | **The shipped default app has no authority map.** TA-1 M1 puts `claim_authorities` in the app's `asgi.py`, written by `sandloom new`, and notes that a map in `sandloom.main` would be a kernel edge (C §0 finding 2) and leaves its home to A. A says nothing about it. `sandloom.main` is what the shipped image runs (`deploy/docker/Dockerfile:30`, `:32`), the default composition root of worker, beat and `sandloom-migrate` (`core/composition.py:24-27`), and the root of `sandloom[standard]` (`packages/README.md:43`). Without a map there, every protected claim is untrusted under TA-1 and every platform route and `Role("admin")` denies in the standard deployment. The three ways out each break something: (1) `sandloom.main` names `sandloom_identity` / `sandloom_tenancy` / `sandloom_billing`: an I-02/I-03 edge, and it must vary with the installed subset or startup refuses ("assigned module neither loaded nor bound") in 15 of the 16 subsets (I-19); (2) a default derived from installed distributions: research-trust e-5 rejects it as trust by name; (3) `sandloom.main` leaves the kernel wheel (a `sandloom-standard` root): a new package. | `main.py:1-48` [read]; `core/composition.py:24-27` [read]; `deploy/docker/Dockerfile:30-32` [read]; research-trust §5.1 a-2, e-5 | Owner decision (§8 #X1) before ADR-A and ADR-C: (1) as a `retained` register row with an ADR id and a per-subset rule, (2) as an explicit exception to answer #9 limited to installed first-party distributions verified by B020, or (3) a new root package. PoC: the shipped image boots with each of the 16 subsets and the platform gate allows the bundled admin |
| C-a1 | major | **`cpv` (one digest of the whole map) turns every map change into a fleet-wide loss of protected claims, and rolling deploys flap.** Adding one assignment (`features` to an app module) changes the digest; every live token loses `roles` too until refresh. In a split rolled verifier-first, new verifiers reject tokens from the not-yet-upgraded issuer, the client refreshes, the old issuer mints the old digest again: a 401 loop until every issuer replica is upgraded. With several issuer replicas at different versions the digest alternates per request. TA-1 states "tokens lose protected claims at upgrade" (accepted by answer #11's shape) but no rollout order and no window. | alternatives-c §2.2 M5a, "Security-regression risk"; research-trust d-2 | A per-key digest (a change to `features` leaves `roles` trusted) and a stated order (issuer last) or an accepted-previous-digest window (a new state: count it). PoC T-10 extended to a rolling deploy with 2 issuer replicas |
| C-a2 | major | **The sealed mint (M5b) seals `create_access_token` but not `collect_claims`' inputs.** `collect_claims(user_id, issuer_session, issuer="identity", precomputed={fn: claims})` takes the issuer as a caller-supplied string compared with owner short names, and `precomputed` lets the caller supply the result of the issuer's contributor (`core/facade/_claims.py:315-345`, `:404-417`). An object "only `collect_claims` builds" therefore carries whatever the caller passed as the issuer's own contribution. Identity also adds `sub`, `email`, `email_verified`, `auth_time`, `token_type` after `collect_claims`, outside the merge (`sandloom_identity/services/token_service.py:142-153`); answer #10 makes `email`/`email_verified` trusted only from the issuer, so the sealed mint needs an issuer-claims channel bound to a verified issuer, which TA-1 does not describe. | `core/facade/_claims.py:315-417` [read]; `token_service.py:125-153` [read] | M5b spec: `issuer` resolved from the registering module of the calling contributor (not a string), `precomputed` limited to the verified issuer's assigned keys, an issuer-claims field set by the kernel. PoC T-11 adds the `precomputed` and `issuer=` cases |
| C-a3 | major | **Two sources say who the issuer is.** M1 assigns `email`/`email_verified` "to the issuer" in the map; R2 (and A's M5) key the refresh-session obligation on `issues_tokens=True`, which a module declares about itself and which today only drives key placement (`core/descriptors/__init__.py:143-147`). A module declaring `issues_tokens=True` that the map does not name, or a mapped issuer without the flag, is not covered. Self-declaration is what answer #9 rules out for authority. | alternatives-c §2.2 M1, R2; alternatives-a M5 | The map names the issuer; `issues_tokens` is checked against it at startup (refusal on mismatch) |
| C-a4 | major | **A new indeterminate status inside the algebra.** M5a raises `PolicyIndeterminate(status=401)`. Today indeterminate carries 501/503. With `Role("admin") \| SubscriptionActive(...)`, roles untrusted by policy (401) and billing down (503), which status the response carries is unspecified; H-4 keeps the algebra unchanged and answer #19 makes provider-down-inside-the-algebra a separate change. | alternatives-c §2.2 M5a; requirements H-4, I-07 | A status precedence table in ADR-C (proposal: 503 over 401, since a refresh cannot fix an outage); law tests over the new status (I-07 missing proof) |
| C-a5 | minor | `as_user` mints without provenance and 127 test lines set protected keys by hand (C finding 1). The test authority is refused only in a "production composition"; a staging deployment with `ENVIRONMENT` other than `prod` could assign it. Outside the threat model for hostile code, inside it for mistakes. | `core/testing/auth.py:37-55` [read] | Refuse the test authority wherever the `sandloom.sdk.testing` plugin is not active, not only in prod |
| C-a6 | minor | TA-1 closes T4 for protected claims only. Any module with the core runtime can still call `collect_claims` for an arbitrary `user_id` and mint a valid token with that user's real rights (impersonation, not a rights raise). Requirements scope T4 to protected claims (answer #13), so this is a recorded limit, not a violation. | `core/facade/_runtime.py:41-49` [read] | State it in ADR-C's threat model |
| C-a7 | minor | Hot-path cost "≈ today" rests on R6 (31 ns against 35 ns, indicative); the claims rung sits inside the ±4.3 % noise band, so the +1 % budget cannot be resolved there (I-14 part 4). | perf-dx-baseline §2.3 [cited] | T-15 reports; a security fix is not blocked (answer #5) |

**TA-0, TA-2, TA-3.** C's verdicts hold: TA-0 fails A-B8, A-F8, C-B3 by construction; TA-2 breaks
answer #9; TA-3's one gain is outside the threat model and its per-request signature check is
likely beyond the budget (Unverified, C says so).

### 4.2 Part (b): findings on PA-2 (favoured)

| id | sev | finding | evidence | resolves it |
|---|---|---|---|---|
| C-1 | **blocker** | **PA-2 removes a public capability without the H-2 exception.** It removes `register_live_adapter` and the adapter table and counts that as "−2 mechanisms". These are public SDK names (`sdk/authz.py:11-18`, `:46-51`, `:72`: `register_live_adapter`, `EntitlementsAdapter`, `MembershipAdapter`), documented as the way to point the live atoms at another contract (`docs/modules/http/authorization.md:66-71`), and a ledger guarantee: G-AUTHZ-05 "Live adapters are consumer-side configuration: a registered adapter overrides the owner's contract" (`guarantees.yml:2514-2520`). Today it is the only in-process way for a third-party billing to serve the live atoms (guarantee-map §5). H-2 and answer #21 require a separately approved exception and a capability-ledger change for such a removal; C records it for `Feature`/`WithinLimit` but not here. reconciled.md §4.2 retires G-AUTHZ-05 openly. | `sdk/authz.py` [read]; `guarantees.yml:2514-2520` [read] | Owner exception (§8 #C6) with a `removed:` ledger entry and upgrade steps (H-9), or keep a consumer-side override in the PA-2 evaluator (an import change replaces it) |
| C-2 | major | **K1's metadata source is undefined, and K1 merges trust and provider.** K1 has the kernel ask "the assigned tenant authority's declared membership check" (`witnesses_membership` on a contract method). The kernel ships no stub (B), so in a process without tenancy the declaration must come from `describe()` (remote metadata, which I-14 forbids on the request path), from the app's generated files (the kernel reading a module's generated artifact), or from the composition root. None is chosen. K1 also makes the authority of `tenant_ids` (a claims writer) the trusted provider of tenant kind; goals.md keeps guarantee, provider and trust separate. An independent tenant-grant provider (C-H6) without a membership method makes `bound_tenant()` indeterminate everywhere, a capability limit not stated. The alternative both roadmap-review R43 and chatgpt-roadmap-v2 §16.3 ask to compare, a domain-only account resolver (the owner classifies the bound tenant; billing already checks membership through `tenancy.membership.v1`, `sandloom_billing/access.py:117-129` [cited reconciled]), is not evaluated: C lists K1 as "identical in every alternative". | `shared/authz/account.py:153-168` (`_is_member` through the membership adapter) [read]; alternatives-c §4 K1 | `kb-poc-authz` runs both K1 forms on the decisive scenario (counts, decisions, C-B4, C-H6); owner picks one (§8 #C3) |
| C-3 | major | **"Equal calls, queries and order by construction" does not hold for `Credits`.** Today `OrgRole` and `SubscriptionActive` call `has_role_at_least` and `check_subscription_active`, the same methods PA-2 uses; `Credits` calls `get_credit_balance` and compares in the kernel (`shared/authz/live_contracts.py:79-86`). PA-2 moves it to a new `has_credits_at_least`: a new method and query, which is answer #24's equivalence case, not construction. C's table marks C-B9 "measure", but its summary and §7.2 point 1 claim construction for all atoms. | `shared/authz/live_contracts.py:72-104` [read] | Claim narrowed to `OrgRole`, `SubscriptionActive`; Z-2 measures `Credits` (1 call / 4 DB) |
| C-4 | major | **PA-2 moves the security-bearing call from `<m>.facts.v1` to domain contracts, so small fix #A no longer covers it.** Fix #A protects the reserved families `<m>.claims.v1` / `<m>.facts.v1`. Under PA-2 an access decision depends on whoever serves `tenancy.membership.v1` in-process; with tenancy absent, the first module to register that name answers `OrgRole` (T5, "first registrant", `core/facade/_services.py:53-69` [cited]). The goals threat model names "serving another module's claims or facts contract"; PA-2 makes every contract with an `access_check` one of those. C keeps M3 widening optional (§7.1, §9). | alternatives-c §2.2 M3, §7.3 "A live check's provider is trusted … in-process, through M3" | M3 widened becomes required with PA-2 (ownership of every contract that carries an `access_check`); decide with B-3 (§8 #X3) |
| C-5 | major | **K7 adds a background refresher with an undefined start state.** The billing rule (`past_due` grace) is "refreshed off the request path (boot + timer)". That is a per-process background task (a process-wide default, §4.2), not counted. With the provider down at boot, the decision before the first successful fetch is not stated (fail closed? today's fallback?). I-20/H-15 need it stated. | alternatives-c §4 K7 | K7 spec: before the first fetch, claim-mode facts escalate to live (as with billing in `claims_sources_failed`, C-F1) or are indeterminate 503; count the refresher; Z-14 adds "provider down at boot" |
| C-6 | major | **The ownership of `Account` is attributed to the owner without support.** C §1 lists `Account` (`user()`, `org(param=)`, `bound_tenant()`), `resolve_account`, `resolve_org_id` under "Kernel keeps … From the owner". The owner's list (tm reflection on `kb-alt-authz`, goals.md "Authorization" table) names `Requirement`, the combinators, `require`, `authorize`, `AccessFact`, `Entitled`, `Role`, `Perm`, `Owner` if generic, `require_platform_admin`, `TenantScope`; not `Account`. chatgpt-roadmap-v2 §16.3 puts the billing account target with billing; Codex and reconciled keep `Account` in the kernel with changed semantics. | goals.md table; tm reflection 01a1ba9b | Owner decision (§8 #C4); until then C-B4, C-B6 and K1 depend on it |
| C-7 | minor | **Risk metadata is the provider's word.** PA-2's `risk=` is declared by the provider; "a lowered `risk` is a `contracts.snapshot.json` change classified as breaking (T004)" is a new T004 rule (T004 classifies wire shape today). A third-party provider can declare no risk from the start; H-20 then relies on independent route classification. The access metadata also extends the describe document (a format addition the §4.2 count does not list). | alternatives-c §5.3 "H-20"; requirements H-20 | Count the describe extension; derive part of the risk from `binds` (an organization bind implies a tenant need) in the kernel, not from the provider |
| C-8 | minor | PA-3's "likely extra provider unit per call in a split" is inferred, not measured; it is the main argument against PA-3 and against reconciled.md's `Entitled(of=, args=)` (§7). | alternatives-c §5.4 | `kb-poc-authz` step 4 must run before PA-2 is called favoured (answer #17 asks for the comparison) |

**PA-0, PA-1, PA-3.** PA-0's disqualification on policy syntax holds after answer #18 ("the policy
expression stays the same"). PA-1's need for a provider declaration under H-20 holds. PA-3 is not
disqualified: its two costs are Unverified (C-8) or a configuration question (mode behind a fixed
tier, which PA-3 can forbid).

## 5. Decision D

### 5.1 Findings on D2a (favoured)

| id | sev | finding | evidence | resolves it |
|---|---|---|---|---|
| D-1 | major | **`config dump` collect mode needs a process-wide switch.** D2 step 5 builds the composition "with `load_composition_core` (no lifespan) in a collect mode". `load_composition_core` imports the composition root, and the root builds the app at import time (`main.py:37`, `app = create_app()`; generated `asgi.py` the same), so no argument reaches the loader: collect mode must be a process-wide flag read during `create_app`. That is a new process-wide default (§4.2) not in D's mechanism table, and state that must never be on in a serving process. | `core/composition.py:55-68` [read]; `main.py:37-38` [read] | Count it; a test that the flag cannot be set by the environment in a serving process; or D3 for `config dump` only |
| D-2 | major | **"No activation, no module I/O" vs module `setup`.** Building the composition runs every module's `register` and `setup`; billing builds its PSP client in setup (`sandloom_billing/psp/__init__.py:69-77`, D cites `:50-77`). Answer #31 forbids activation and module I/O. Whether `setup` is activation is not defined; D marks the no-socket claim Unverified (§13). | `sandloom_billing/psp/__init__.py:69-77` [read] | Owner: define "activation" for #31 (§8 #D5); P11 socket spy decides; D3 is the fallback D already names |
| D-3 | major | **The topology check of `sandloom check` is Unverified.** M6 "validates the declarations of the modules each service of the chosen topology loads against that service's env (D-B1)". `sandloom check` is a static command; a deploy profile's env holds `${VAR:?…}` placeholders and no production secrets, so value validation can only cover what the profile states. D-B1's "report a wrong value for the chosen topology" may hold for names and literal values only. | alternatives-d §5.2 step 6; requirements D-B1 | P6 states which values `check` can validate; requirements D-B1 wording if needed |
| D-4 | minor | D-B2 says T014 "still requires equal values in every process" for `BILLING_DENY_ON_PAST_DUE`. Only billing reads it (`sandloom_billing/access.py:77`; the kernel only lists it, `core/testing/deploy_profile.py:87`) [read]. After D2 a process without billing neither validates nor holds it (answer #25), so "every process" becomes "every process that loads billing". | grep of `deny_on_past_due` [read] | Requirements D-B2 wording (owner) |
| D-5 | minor | D says 22 module-owned env names, not 23 (O-1); a counting correction to requirements I-18 / answer #30 / baseline B-C1. | alternatives-d §2.1 | Owner, requirements |
| D-6 | minor | H1 (hashing stays kernel) argues "a platform guarantee: a storage floor for whoever stores passwords"; reconciled.md §6 shows identity is the only caller in `src/` and `examples/`. H1 is supported by H-2 and I-20 (H3 removes three public names and makes the floor optional per issuer), not by a second consumer. | alternatives-d §7.2; reconciled.md §6 | Owner O-8 / §8 #D4 |

**Claims checked.** "D2 passes all 16 scenarios": D-F4 (d) depends on O-3, D-B1's static part on
D-3, D-H5 on D-2; the honest count is 13 by construction, 3 pending. "PR #71 gives every
alternative the refusal channel": PR #71 is not merged on this branch (D says so). "F-c: an
optional module cannot swallow a `ConfigurationError`": as cited (`core/config/common.py:108`,
`core/loader/plugin_loader.py:555-558`) [cited]. D1, D3, D4 verdicts hold.

## 6. Cross-decision conflicts

| id | sev | decisions | conflict | resolves it |
|---|---|---|---|---|
| X1 | **blocker** | C × A | Where the default authority map of the shipped app lives (§4.1 X1). C defers it to A; A does not address it; `sandloom.main` is kernel and is what the image runs | Owner decision §8 #X1 |
| X2 | major | A × C | **Who is the issuer**: A's M5 and C's R2 key on `issues_tokens=True` (self-declared); C's M1 assigns `email`/`email_verified` to "the issuer" in the map (C-a3) | The map names the issuer; `issues_tokens` checked against it |
| X3 | major | B × C | **Contract-name ownership.** B's S6a "pass+" needs a replacement serving `tenancy.membership.v1`; C's M3 widened refuses it unless assigned; PA-2 makes that contract access-bearing (B-3, C-4) | One rule in ADR-C, used by B's S6a diagnostics |
| X4 | major | B × C | **Kernel-owned families and B1-M3.** `<m>.claims.v1` and `<m>.facts.v1` have no stub, so no expectation; B1-M3 refuses them under `strict` (B-1). PA-2 adds no family but keeps `facts.v1` for claim-tier atoms | Exemption rule (compare against the kernel's schema) |
| X5 | major | A × B × C | **The admin console's operator-login interface** is consumer-defined (A-5), which B's provider-declared copies and C's widened M3 do not allow | Identity declares a login contract admin consumes optionally, or `iss`-based discovery (AD-3) |
| X6 | major | A × D | **Contributed CLI commands need settings.** Under D2 a module setting is read only through a bound app (`settings_of`, "a read with no app bound raises"). Identity's commands moved by M3 must therefore build the composition (as `core/cli/security.py:110` does today), so every module production refusal applies to incident commands; with A-2 (no move) the question disappears until the CLI phase | State it in ADR-A; or defer M3 (A-2) |
| X7 | major | A × D | **B-C10 (set-password link target)**: A proposes an identity setting written by `sandloom new`; D (§2.2, O-10) says "a link-target provider the admin module registers". requirements §6.2 lists it as an owner question | Owner §8 #A5 |
| X8 | minor | B × C | **Generated atoms land in B's generated location**: C's PA-2 atoms go next to the stub, which B1 places in `<app>.shared/contracts/` or `<module>/_generated/contracts/`. Consistent, but the import-driven set (B-2) and the H-20 metadata reader must include atom-only imports; B §7.2 says the ADR-0010 amendment can be narrow ("stubs, DTO copies and generated atoms in the consumer's generated package") | Write the narrow amendment once, in ADR-C, referenced by ADR-B |
| X9 | minor | B × C | Two fixes for the first-request `describe` (B-6) | One in ADR-C |
| X10 | minor | A × C | Mechanism counting differs (A-8) | One ledger |

**Checked, no conflict.** A's edge register vs B's provenance: A's class (c) wheel scan needs to
know a generated file's owner, which B's provenance header gives; complementary. M4/M5
(revocation) vs TA-1: independent of the map, except the issuer identity (X2). D's settings vs C's
map: answer #9 keeps the map out of settings, and D adds no security assignment to settings. RPC
does not forward protected claims (`core/rpc/context.py:87-115`: `sub`, `iat_us`, `auth_time` and
flags only) [read], so TA-1's trusted view needs no RPC change.

## 7. The earlier proposals against the alternatives

Sources: `target-architecture.md` (TA), `codex-review.md` (CX), `reconciled.md` (RC),
`_inputs/chatgpt-roadmap-v2.md` (GPT), [roadmap-review.md](roadmap-review.md) (RR). "Evidence"
means the owner's answers, the probes and the code, in that order.

| # | topic | earlier proposal | alternatives | evidence supports |
|---|---|---|---|---|
| 1 | Claim authority | RC §2.3, D15: contributors declare `claims=("roles", …)`; mint refuses undeclared or duplicate | C TA-1: the application assigns in the composition root; a module's declaration never grants | **Alternatives.** Answer #9 ("a module's self-registration never makes it the authority"); gate probe G2-G4 (the authority ran, another wrote) |
| 2 | Default map | GPT §3.3 item 2 and RR R6: a generated default manifest the app accepts; research-trust a-2: `sandloom.main` passes the bundled map | C: `asgi.py` only; `sandloom.main` would be a kernel edge | **Neither yet** (X1). GPT and RR do not see that `sandloom.main` is kernel; C sees it and stops |
| 3 | Domain atoms | TA D3: parameterized facts + generated wrappers (≈ PA-3); RC D3: `Entitled(of=, args=)` + optional `FactRef` sugar (≈ PA-0/PA-3); CX: "do not accept wrappers yet; check `Entitled` first"; GPT §17.2: compare exactly two (consumer `Requirement` wrappers vs parameterized `AccessFact`) | C: PA-2 (contract-anchored generated atoms), PA-1 control, PA-3 counted | **Open.** Answer #18 rules out PA-0/RC's string syntax. Answer #17 makes PA-2 the "main candidate", compared with PA-1 and PA-3. PA-3's split cost is unmeasured (C-8), PA-2's has C-1..C-5. The PoC decides |
| 4 | Live adapters | RC §4.2: delete, retire G-AUTHZ-05 openly; GPT §17.1: remove after parity, a compatibility adapter only as an approved H-12 exception | C: removed, counted as −2 mechanisms, no exception recorded | **RC and GPT** on procedure (C-1: H-2 needs the exception) |
| 5 | `Feature`, `WithinLimit` | RC D14: delete (no producer) | C K3: move to billing with parameters, fallback generic claim atom | **Alternatives.** Answer #21 |
| 6 | Tenant kind (B-R2) | RC §4.2.5: the owner classifies the bound tenant, the kernel stops asking tenancy; GPT §16.3 and RR R43: pick a platform kind port or a domain-only resolver, not both | C K1: the kernel asks the tenant authority's declared check | **Open; C misses an option** (C-2). Call counts: RC says equal (nested call), C keeps 3; both unmeasured |
| 7 | `Account` | TA D4: rename to `Party`; CX, RC, GPT: no rename | C: keep in kernel | **Alternatives on the name** (CX, GPT §11 item 7). **Ownership open** (C-6): GPT puts the billing account target with billing |
| 8 | Boundary rule and check | TA: vocabulary scan over all text; RC K3: literal scan with a file → count baseline; CX: criteria, not word bans; GPT §14.1, RR R37: edge register with status | A1: register by baseline id with status, literal scan, composition text apart | **A1.** RA14 / I-03 ask for a status per baseline id; CX's objection to prose bans is met by scoping |
| 9 | CLI | TA, RC P6: one `sandloom.cli` entry-point group, identity owns its commands, after contracts; CX, GPT §8, RR R31: separate task after A-D, one minimal extension | A1 M3 inside decision A, counted as a declaration on the descriptor | **Earlier proposals** on scope and counting (A-1, A-2); A1 on the destination of the commands |
| 10 | Incident commands | TA D10, RC D10: identity owns `revoke-sessions`; CX "D10 partial: keep platform incident mechanisms in core" | A1 to identity; A2 kernel over a P1 port | **Open** (owner #A1). A's analysis that P1 is the only port passing H-18 is sound |
| 11 | Admin manifest compiler | RC §7: "always compile the admin manifest; it is a kernel artifact like OpenAPI"; GPT §8: "generic manifest compiler stays Core" | A1 M2: compiler to admin, a generation extension | **A1.** The owner decision on `kb-alt-boundary` (2) puts compiler and schema in admin tooling; A-3 still applies |
| 12 | Contracts | TA D2, RC D2, CX, GPT §4: consumer-side generation for every consumer, framework modules included, provenance in the copy, describe beside the stub | B1/B4 | **Agreement.** RC adds closed-DTO `extra` (B-4, verified in code); RC records the provider version in the header, B does not (B's churn argument and answer #3 support B) |
| 13 | Settings order | GPT §7, RR R27: two-phase bootstrap (discover, validate, activate) | D2: validate at registration, one post-load check (research O2) | **D2.** Owner preamble accepts O2 and rejects a separate two-phase bootstrap |
| 14 | Settings scope | RC §5: ops tier only, semantic module settings refused, unknown dotted risk id of an absent module ignored with an info log, prod refusals by critical startup tasks | D2: per-module semantic ratchets; unknown prefix refused; refusals at registration (PR #71) | **D2.** Answers #26, #27; research F1 (startup tasks run only in the API lifespan) |
| 15 | Env names | TA D5: rename to module prefixes | D: keep the 23 (22) names | **D.** Answer #30 |
| 16 | Password hashing | TA D12, RC §6: argon2, its settings, floor and risk to identity; three SDK names leave; CX: classify first, platform may own crypto mechanisms | D H1: stays kernel; H2 fallback | **D on constraints** (H-2, I-20), **RC on facts** (one caller). Owner O-8 (D-6) |
| 17 | `sandloom.shared` | TA D9: fold into core | A1: kernel package of platform primitives, removal not a goal | **A1.** goals.md (G3), CX |
| 18 | Legacy tokens | GPT §3.3 item 7, RR R10/R41: a policy and an authority-policy fingerprint | C M5a `cpv` | **Agreement** on the mechanism; C-a1 on its rollout |
| 19 | Process model | GPT §3.3 item 6, RR R9: authority grants on an immutable per-app snapshot, one runtime per process | C: map resolved on the facade; I-24 | **Agreement** |
| 20 | Mechanism count | RC: six additions; TA: about fifteen; CX: "biggest worry: many new universal mechanisms" | A 5, B 3, C(a) 4 + R2, C(b) +2/−2, D 6 | Within RC's range per decision; the uncounted ones found here: A-1 entry-point group, C-5 refresher, D-1 process-wide flag, C-7 describe extension, C-a1 digest window if adopted |

## 8. Owner decisions needed for the ADRs

Deduplicated from the open points of the four files (A §9, B §10, C §9, D §12), requirements §6.2
and the findings above. "Blocks" names the ADR that cannot be accepted without the answer.

### 8.1 Cross-decision

| # | decision | options | blocks | from |
|---|---|---|---|---|
| X1 | Default authority map of the shipped app (`sandloom.main`, the image, `sandloom[standard]`) | (1) `sandloom.main` names the bundled modules, as an approved register row with a per-subset rule; (2) a default limited to installed first-party distributions verified by B020, as an explicit exception to answer #9; (3) `sandloom.main` leaves the kernel wheel into a root package | ADR-A, ADR-C | §4.1 X1; C §9; research-trust e-5 |
| X2 | Who names the issuer | the map names it and `issues_tokens` is checked against it; or `issues_tokens` alone | ADR-A, ADR-C | C-a3 |
| X3 | Contract-name ownership | every contract's first segment owned by the registering module unless the application assigns a replacement; or only the reserved families (fix #A) and contracts carrying an `access_check` | ADR-B (S6a), ADR-C (T5, PA-2) | B §10.5, C §9 "M3 widened", B-3, C-4 |
| X4 | Admin console's operator login | identity declares a login contract the admin consumes optionally; or the console discovers the issuer from the token | ADR-A | A §9.4, A-5 |

### 8.2 Decision A

| # | decision | options | from |
|---|---|---|---|
| A1 | Issuer-agnostic incident revocation | a platform guarantee (A2's P1 port, kernel keeps `security revoke-sessions`); or the issuer's command (A1) | A §9.1; CX D10 |
| A2 | Kernel-owned revocation applier (M4) | accepted if the PoC passes A-H1 lean `/ready`, split group placement and the loss path; else another form | A §9.2, A-4 |
| A3 | Scope of the CLI move | identity's commands move inside decision A (M3, a new entry-point group); or B-I1 is removed without a move and B-R4/B-R5 stay `deferred` until the CLI phase | A-1, A-2; requirements §1 |
| A4 | Operators in split | identity's commands only from identity's image; acceptable or not | A §9.6 |
| A5 | B-C10 set-password link target | identity's own setting written by `sandloom new`; or a provider the admin module registers | A §9.5, D O-10, requirements §6.2, X7 |
| A6 | `Permissions`, `Roles` (B-N15) | remove under I-17 with upgrade steps; or move to identity | A §9.7, C K2 |
| A7 | A-B2 wording | keep (commands stay) or update (commands absent without identity) | A §9.8, A-7 |
| A8 | Issuer without refresh sessions | may declare "none" instead of an empty `user_blocked` consumer | A-6 |
| A9 | What observes "executed" for the issuer obligation (answer #16) | consumer lag and redelivery in `/ready`; or a stronger check | A §9.3 |

### 8.3 Decision B

| # | decision | options | from |
|---|---|---|---|
| B1 | No expectation for a required remote contract (hand-written `ContractRef`, kernel families) | refuse under `strict` / fail `/ready` under `ready`; kernel families compared against the kernel's schema; or the application lists unchecked contracts | B §10.1, B-1 |
| B2 | Provider version in provenance | omit (fingerprint + lock file); or record and accept a rewrite per provider release | B §10.2 |
| B3 | Applications: generate for every composed provider or only consumed ones | today's set; or the declared `consumes` set | B §10.3, B-2 |
| B4 | Name and protection of the module-local generated package | `_generated/contracts`, `_generated/events`, `sandloom check` rules | B §10.4 |
| B5 | C-GEN-03 ledger text (drops the kernel-home clause) | approve | B §10.7 |
| B6 | C5 generic versioned-event primitive | only with a second producer | B §10.6 |
| B7 | Closed DTOs (`extra="forbid"`) | record `extra` and treat an added field as breaking; or render copies open | B-4 |

### 8.4 Decision C

| # | decision | options | from |
|---|---|---|---|
| C1 | ADR-0010 amendment for generated domain atoms and the four beginner names | the narrow form ("stubs, DTO copies and generated atoms in the consumer's generated package") with a migration of docs and examples | C §9, answer #18, X8 |
| C2 | Which atom form | PA-2, PA-3 or PA-1, after `kb-poc-authz` including PA-3's split count | C §9, answer #17, C-8 |
| C3 | Tenant kind (B-R2) | the kernel asks a declared membership check of the tenant authority (K1); or a domain-only resolver where the owner classifies the bound tenant | C-2, RR R43, GPT §16.3 |
| C4 | Ownership of `Account` | kernel concept with neutral semantics; or billing's account target | C-6 |
| C5 | Legacy and skewed tokens | `cpv` per key or for the whole map; rollout order or accepted-previous-digest window | C-a1 |
| C6 | Live adapters (`register_live_adapter`, G-AUTHZ-05) | approved removal with a `removed:` ledger entry and upgrade steps; or a consumer-side override kept in the evaluator | C-1 |
| C7 | Status precedence of indeterminates (401 `token_policy_outdated` vs 503) | proposal: 503 first | C-a4 |
| C8 | Declared claim keys (M6) | after `kb-poc-trust` | C §9, answer #15 |
| C9 | `Feature`, `WithinLimit` final home | Z-13, then the owner | C §9, answer #21 |
| C10 | `past_due` freshness | Z-14 table; K7's start state when the provider is down at boot | C §9, answer #20, C-5 |
| C11 | Test authority scope | refused wherever the testing plugin is inactive, not only in prod | C-a5 |

### 8.5 Decision D

| # | decision | options | from |
|---|---|---|---|
| D1 | Counts in requirements (22 module-owned names, 21 `ModulesConfig` fields) | correct requirements I-18, #30, baseline B-C1 | D O-1, D-5 |
| D2 | `REFRESH_TOKEN_EXPIRE_DAYS`, `MAX_SESSION_DAYS` to identity | move or keep | D O-2 |
| D3 | D-F4 (d) where the module is not loaded | generic message; or import installed declarations | D O-3 |
| D4 | Password hashing | H1 (kernel) or H2 (work factor to identity, floor in the kernel) | D O-8, D-6 |
| D5 | What "activation" means for `config dump` (answer #31) | module `setup` allowed if no socket opens; or not, then D3 for the dump | D-2 |
| D6 | Kernel risk ids | stay flat; or `core.` prefix in the same migration | D O-4 |
| D7 | Completeness of classification | tier recorded in `module.snapshot.json`; or an explicit tier per field | D O-5 |
| D8 | Grandfathering the unprefixed names | per-field exemption with a reason; or the committed module snapshot as baseline | D O-6 |
| D9 | Known namespaces | K1 (installed entry points, loaded, app-local); or an explicit deployment list (K2) | D O-7 |
| D10 | Identity's `JWT_SECRET` refusal | an identity check in every environment under D2; or a PR #71 follow-up | D O-9 |
| D11 | D-B2 wording | "every process that loads billing" | D-4 |

### 8.6 Already open in requirements §6.2, not duplicated above

Latency threshold (I-14 part 4, with a quieter bench).

## 9. Verdicts

| decision | verdict | conditions |
|---|---|---|
| **A** | **A1 still stands**, as a smaller A1 | (1) M3 dropped from decision A or counted as a new entry-point group with an app-local outcome (A-1, A-2, owner A3); (2) M2 passes resource declarations to admin instead of admin importing modules (A-3); (3) M4 passes A-H1 lean `/ready`, split group placement and the loss path (A-4); (4) X1 and X2 answered; (5) the console's login check works in split without a consumer-defined contract (A-5, X4) |
| **B** | **B1 delivered as B4 still stands** | (1) kernel-owned families exempt from B1-M3 (B-1, X4); (2) the consumed set from declared `consumes` (B-2); (3) contract-name ownership decided with C before S6a is scored "pass+" (B-3, X3); (4) PoC item 7 shows generation without composing the provider; (5) closed DTOs handled (B-4) |
| **C (a)** | **TA-1 still stands** once X1 is decided | (1) X1 (blocker); (2) `cpv` granularity and rollout order (C-a1); (3) the sealed mint covers `issuer=`, `precomputed` and the issuer's own claims (C-a2); (4) the map names the issuer (C-a3, X2); (5) status precedence of indeterminates (C-a4) |
| **C (b)** | **PA-2 is the leading candidate, not yet the favoured architecture** | (1) owner exception for removing the live adapters and G-AUTHZ-05, or a kept override (C-1, blocker); (2) K1's metadata source, and the domain-only resolver compared in the PoC (C-2); (3) "equal by construction" narrowed and `Credits` measured (C-3); (4) contract ownership for access-bearing contracts made required (C-4, X3); (5) K7 refresher counted and its start state defined (C-5); (6) PA-3's split count measured (C-8); (7) `Account` ownership decided (C-6) |
| **D** | **D2a with hashing H1 still stands** | (1) the collect-mode switch counted and made unreachable in serving processes, or D3 for the dump (D-1); (2) answer #31's "activation" defined and P11 run (D-2); (3) what `sandloom check` can validate per topology stated (D-3); (4) requirements wording D-B2 and counts (D-4, D-5) |

## Sources

- Requirements, goals, owner answers: [requirements.md](requirements.md) §3-§6,
  [goals.md](goals.md), [owner-answers.md](owner-answers.md); tm reflections on `kb-assess`,
  `kb-alt-boundary` (admin battery decision), `kb-alt-authz` (owner constraints).
- Alternatives: the four files above at `ceb3d66`.
- Evidence cited through them: [baseline.md](baseline.md), [guarantee-map.md](guarantee-map.md),
  [validation.md](validation.md) §1, [perf-dx-baseline.md](perf-dx-baseline.md),
  [research-trust.md](research-trust.md) §5-§6, research-authz-portability.md,
  research-settings.md.
- Code read in this pass: `main.py:1-48`; `core/composition.py:24-27`, `:55-85`;
  `deploy/docker/Dockerfile:30-32`; `deploy/compose/microservices.yml:91-96`;
  `core/facade/_claims.py:240-417`; `core/facade/_runtime.py:41-49`;
  `core/facade/_registries.py:228-262`; `core/module_api/_registration.py:443-500`;
  `core/rpc/context.py:1-145`; `sandloom_identity/__init__.py:50-110`;
  `sandloom_identity/services/token_service.py:95-175`; `core/auth/revocation.py:453-483`;
  `shared/authz/live_contracts.py:40-110`; `shared/authz/account.py:140-170`; `sdk/authz.py:1-140`;
  `guarantees.yml:2500-2525`; `docs/modules/http/authorization.md:60-80`;
  `core/cli/dto_copies.py:405-440`; `core/cli/security.py:54-110`; `src/sandloom/__init__.py:88-147`;
  `sandloom_billing/psp/__init__.py:45-80`; grep of `deny_on_past_due`; `docs/adr/0010-public-api-1.0.md:50-62`,
  `:300-312`; `poc/replace/app/pocapp/modules/*/__init__.py`.
- Phase 2: [roadmap-review.md](roadmap-review.md) §1, §3-§5; `_inputs/chatgpt-roadmap-v2.md` §3.3,
  §4-§8, §11, §16.3, §17; scratch `target-architecture.md` §8, `codex-review.md`, `reconciled.md`.
