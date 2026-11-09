# Decision C: alternatives for claim trust and portable authorization atoms

tm task `kb-alt-authz` (parent `kb-assess`). Decision C has two parts that the owner requires
to be solved together (goals.md, "Three problems are solved together"): **(a) the trust model
for security claims** (who may issue `roles`, `permissions`, `tenant_ids` and access-granting
billing claims, and how the kernel checks it) and **(b) the portability of domain atoms**
(`Subscribed`, `SubscriptionActive`, `Credits`, `Feature`, `WithinLimit`, `OrgRole`,
`Account`/`TenantKind`, `Permissions`/`Roles`, the default live adapters, `fake_access_fact`)
so that the kernel holds none of their domain knowledge and a consumer never imports a provider.

This file compares alternatives by the scenarios of [requirements.md](../requirements.md) §5.A and
§5.C and the owner's answers ([owner-answers.md](../owner-answers.md), "answer #n"). It decides
nothing: the ADR decides, after `kb-poc-trust` and `kb-poc-authz`.

Inputs read: goals.md, requirements.md, owner-answers.md, problems.md, baseline.md,
guarantee-map.md, validation.md, perf-dx-baseline.md, research-trust.md,
research-authz-portability.md, the tm reflections on `kb-alt-authz`, `kb-poc-authz`,
`kb-poc-trust`, `kb-assess`, and the code at branch head `f40cb7f` (no file under `src/`
changed since `44451e4`; requirements.md header). Not read (blinding rule): roadmap-review.md,
the roadmap inputs, the scratchpad research.

Labels: **[read]** code read at the cited line; **[run]** a probe or PoC transcript cited by
the source document; **[measured]** perf-dx-baseline.md; **[inferred]**; **Unverified**.
Paths are under `src/sandloom/` unless they start with `sandloom_<m>/` or name a repository file.

## 0. Summary

| part | alternatives | what the evidence favours |
|---|---|---|
| (a) trust | **TA-0** mint-time refusal plus one platform-admission predicate (minimal); **TA-1** application-assigned authority map in the composition root, verified at mint, stamped policy marker `cpv`, trusted subject view, sealed mint (minimal sufficient); **TA-2** the same map as a deployment setting; **TA-3** per-authority signed claim blocks | **TA-1.** TA-0 fails answers #11 and #13 and I-09 on its own terms (old, hand-minted and skewed tokens are read without a check). TA-2 breaks answer #9. TA-3 buys protection against a hostile issuer, which the threat model leaves out, at the price of keys, signatures per request and new settings (H-12). |
| (b) portability | **PA-0** existing `AccessFact` / `Entitled` / `ClaimRule` with the smallest extension (minimal); **PA-1** consumer-owned declarative `Requirement` classes over the provider's stub, cross-checked against a provider declaration; **PA-2** contract-anchored access checks: the provider marks a boolean contract read, `sandloom generate` writes a typed atom next to the stub, one generic kernel evaluator; **PA-3** parameterized `AccessFact` with generated typed wrappers | **PA-2**, with PA-1 kept for a module's own one-off check. PA-0 changes the policy syntax and loses typing (I-23, H-16). PA-1 leaves risk and tier to the consumer unless the provider declares them, which is half of PA-2 with a forked vocabulary. PA-3 routes every typed atom through `<owner>.facts.v1`: a likely extra provider unit per call in a split and a configuration-chosen mode behind a fixed tier. PA-2 keeps calls, queries and order equal by construction. |

Two findings this work adds to the inputs:

1. **The test token helper mints without provenance.** `make_user_headers` (behind `as_user`)
   calls `create_access_token(data=data)` with any keyword as a claim
   (`core/testing/auth.py:37-55`) [read]; 127 lines in the test trees set `roles`, `subscription_status` or
   provenance by hand (grep; `roles` alone in 19 test files). Under any read-side check (TA-1, TA-3) such tokens carry
   untrusted protected claims. Answer #13 keeps "supported SDK scenarios"; so the sealed mint
   needs a test authority that only a test composition may assign (§2.2, M5b). `kb-poc-trust`
   must prove it.
2. **A default authority map in `sandloom.main` would be a kernel edge.** `sandloom.main` is
   part of the kernel (goals.md, "The goal"). If it passes `claim_authorities={"roles":
   "sandloom_identity", ...}`, as research-trust §5.1 a-2 suggests, the kernel names a module
   (I-02). Where the bundled default lives is decision A's question; the generated app's
   `asgi.py` is safe (§2.2, M1).

## 1. What both parts must keep

From the owner (tm reflection on `kb-alt-authz`; answers #5, #17-#24) and requirements.md §4.1:

- **Kernel keeps** (I-21, H-14): `Requirement`, `&` `|` `~`, allow / deny / indeterminate,
  `require`, `authorize`, evaluation order and short-circuit, fail-closed and denial categories,
  `AccessFact`, `Entitled`, `Role`, `Perm`, `Owner`, `TenantScope`, `require_platform_admin`,
  `Account` (`user()`, `org(param=)`, `bound_tenant()`), `resolve_account`, `resolve_org_id`.
- **Owners take**: `Credits`, `Subscribed`, `SubscriptionActive` (billing); `OrgRole` and its
  `member/admin/owner` vocabulary (tenancy; `require_org_role` already lives in
  `sandloom_tenancy/access/__init__.py:59` [read]); `Roles` / `Permissions` constants
  (identity; `shared/authz/constants.py:12-31`, no importer, baseline B-N15).
- **Algebra unchanged** (H-4): `A | B` with `A` indeterminate and `B` allow is allow;
  `~indeterminate` never allows; provider-down as indeterminate 503 only as a separate change
  (answer #19).
- **Three tiers** (H-10): claims (no I/O, no DB session), local fact (no RPC), remote fact (RPC
  only to another process; no `describe` on the request path when metadata is known).
- **Moving an atom adds no call** (I-23, answer #5): baseline per policy in perf-dx §2.1, incl.
  **3 contract calls / 9 DB round trips** for `OrgRole("admin") &
  SubscriptionActive(of=Account.bound_tenant())`; the third call goes only as its own change
  (answer #23).
- **Policy metadata a module cannot lower** (H-20, answer #22); `Feature` and `WithinLimit` stay
  public capabilities (H-2, answer #21).
- **One platform admission** shared by `require_platform_admin` (`shared/guards/platform.py:14-19`)
  and `platform_operator` (`core/admin/api/platform.py:55-71`), trusting `admin` only from the
  assigned authority (goals.md, table "Authorization: the mechanism stays in the kernel").

Mechanism counting follows requirements.md §4.2 ("Runtime complexity"). Every new mechanism
names the violation it removes or the invariant it provides (H-12).

---

# Part (a): the trust model

## 2. Alternatives

The threat model is goals.md "Threat model" and research-trust §2: installed module code is
buggy, not hostile; hostile in-process code, the signing-key holder and a compromised authority
are out of scope. Every alternative includes **small fix A** (owner-approved, separate PR):
`AccessFact.owner_module`, `owner=` and the reserved families `<m>.claims.v1` / `<m>.facts.v1`
are checked against the module actually registering (closes T7 and validation C4 locally).

### 2.1 TA-0 (minimal): refuse foreign keys at mint, one admission predicate

**Mechanisms**

| # | mechanism | replaces | serves |
|---|---|---|---|
| A0-1 | `create_app(claim_authorities={key: module path})` in the composition root, plus startup refusals for an unresolved path or an assigned authority that is non-critical (research-trust e-3, e-4) | trust by the name `identity`; PR #70's first writer as owner | I-08 assignment, answer #9, #14; T1-T3 |
| A0-2 | the merge accepts a protected key only from the step of its assigned module (module path locally, `<m>.claims.v1` binding remotely); the violator rule (answer #12) in the same pass | `_refuse_foreign_claim_keys` first-writer rule (`core/facade/_claims.py:497-534`) | I-08 verification, violator; A-F2, A-F6, A-F7 |
| A0-3 | one `platform_admission(subject)` used by both platform gates, testing `admin` in `roles` and the assigned authority of `roles` in `claims_sources` and not in `claims_sources_failed` | two copies of a gate trusting the string `"identity"` | I-09 for the platform gates; B-S1, B-S2 |

`Role`, `Perm`, `TenantScope`, the tenant allowlist (`shared/authz/_resolve.py:61-75`),
`Feature`, `WithinLimit` keep reading the token as today: TA-0 relies on "only the kernel's merge
mints".

**What is missing, exactly**

1. *A read-side check*: a token not built by the merge passes `Role`/`Perm` with no provenance
   (research-trust §3.3, R5 [run]). I-09 requires the gate itself to trust only the assigned
   authority; TA-0 delegates it to the mint.
2. *A policy marker in the token*: a token minted before the change (under PR #70's first-writer
   rule, possibly with a non-authority's `roles`) and a token minted by an issuer with a
   different map cannot be told from a current one. Answer #11 and A-B8 require untrusting them.
3. *A sealed mint*: `create_access_token` takes any dict (`core/auth/tokens.py:31-102`) [read]
   and is public in `sdk/security.py`. Answer #13 requires protected claims and provenance only
   from the verified `collect_claims` result.

### 2.2 TA-1 (minimal sufficient): assigned authority, policy-stamped token, trusted view

TA-0 plus exactly the three missing pieces. This is research-trust §6 (M1-M5), checked here
against the owner's answers, which came after it.

| # | mechanism | replaces | serves |
|---|---|---|---|
| M1 | **`claim_authorities` in the composition root** (`create_app(...)` argument in the app's `asgi.py`, written by `sandloom new`). Keys: every protected claim of answer #10 (`roles`, `permissions`, `tenant_ids`; `subscription_status`, `plan_code`, `features`, `limits` when an atom reads them; `email`, `email_verified` to the issuer). Startup refusals: path resolves to no module; assigned module neither loaded in the issuing process nor bound (`<m>.claims.v1` missing from `SERVICE_BINDINGS`); assigned authority non-critical (answer #14) | trust by name (`platform.py:16`, `admin/api/platform.py:67`); first writer as owner | I-08, I-22 T1-T3, T6; answers #9, #10, #14 |
| M2 | **mint check** in the existing merge pass: a protected key only from its authority's step; local identity = the import path of the module whose registration was in progress (`_claims.py:271-272` already records it); remote identity = the pinned, MAC-bound `<m>.claims.v1` binding; `owner=` refused when it differs from the registering module; violator rule: an optional violator loses its whole result, recorded in `claims_sources_failed`, logged with contributor and key, counted; a critical violator refuses the mint | `_refuse_foreign_claim_keys` (extended, same loop); `owner=` spoofing (`_claims.py:262`) | I-08 verification and violator; answers #9, #12; A-F2, A-F3, A-F6, A-F7, A-B6 |
| M3 | **contract-name ownership**: small fix A, plus *optionally* every contract name's first segment checked against the registering module's short name | `register_service` with any name (`core/facade/_services.py:54-69`), `register_access_fact` with any owner (`:219-240`) | T5 (locally, if widened), T7; problem 13 |
| M4 | **trusted subject view**: `UserContext`, built once per request (`shared/authz/deps.py:82`), exposes a protected key only when its assigned authority is in `claims_sources`, not in `claims_sources_failed`, and the token's `cpv` equals the verifier's; `Role`, `Perm`, `TenantScope`, the allowlist, `Feature`, `WithinLimit`, claim-mode facts and **one platform admission** read the view; a claim-mode fact is trusted by the authority of its `claim_key`, not by the `owner_module` it states | provenance-free reads (`atoms_claims.py:47-48`, `:66-67`, `:304-363`; `core/http/tenant_scope.py:159-172`); two gate copies; D2, D3 | I-09, I-10 (`tenant_ids`, validation B6), H-5 |
| M5a | **`cpv` claim** (policy version + short digest of the map) stamped by the merge; a verifier with another digest, or a token without `cpv`, treats protected keys as untrusted. An atom that needs an untrusted-by-policy key raises `PolicyIndeterminate(reason="token_policy_outdated", status=401)`: `A \| B` still allows on `B`, `~` keeps it indeterminate, and the response is 401 with that machine code only when no branch allowed (answer #11). A protected key with **no assignment** is a definite deny (403), not 401 | legacy-token trust, split skew | I-08 consistency, I-09 policy mismatch; answer #11; A-B8, C-B3 |
| M5b | **sealed mint**: `create_access_token` accepts protected keys, provenance and `cpv` only inside an object only `collect_claims` builds; a **service/test path** (`create_service_token`) refuses protected keys; the **test harness** assigns a kernel test authority in the test composition only (the `sandloom.sdk.testing` plugin), and `as_user(**claims)` mints through `collect_claims` with that authority's contribution; a production composition assigning the test authority is refused at startup | S5 for mistakes; `as_user` building provenance-free tokens (`core/testing/auth.py:42-55`) | I-08 minting, I-22 T4; answer #13; A-F8 |
| M6 (later) | declared claim keys in `define_module(claims=...)`, a startup comparison | runtime-only discovery | answer #15: after the PoC, never instead of M2 |

**Revocation duties (answer #16), identical in every alternative.** Two pieces, not part of the
claims map:

- **R1** the durable applier of `core.auth.tokens_revoked.v1` moves from identity
  (`sandloom_identity/__init__.py:73-84`) into the kernel, which owns the channel and the
  guarantee (G-REVOKE-07/08). Removes D1. No new mechanism: a kernel-registered durable
  consumer, like any `@on`.
- **R2** the issuer's obligation to end refresh sessions on `core.auth.user_blocked.v1`
  (today `sandloom_identity/__init__.py:63-71`): a process with an `issues_tokens=True` module
  (`sdk/_decl/module.py:515`) must have a durable consumer of that channel registered by that
  module; otherwise startup refuses (one new startup check family). Execution is the durable
  consumer's dead-letter and lag, surfaced in `/ready` (existing). T8 closes. Q-T8's option of
  putting the duty into the authority map is not needed: the duty follows `issues_tokens`, which
  already exists, and a map entry would be a second place to say the same.

**Costs.** Hot path: per check ≈ today (R6 [run, indicative]: 31 ns against 35 ns per role test);
per request: one string compare (`cpv`) and per protected key one dict lookup and two set
tests at `UserContext` construction; no I/O on any tier. Mint: one comparison per contributed key
in the existing loop; ≈ 25 bytes of `cpv`. DX: one generated argument in `asgi.py`; replacing
identity edits one value; module authors change nothing unless they write a protected key.
Migration: tokens lose protected claims until their next refresh (≤ 15 min default,
`core/config/auth.py:65-69`), one request with clients that act on `token_policy_outdated`;
apps not built by `sandloom new` add the argument or get fail-closed gates; a third-party
issuer must mint through `collect_claims`. No new setting (the semantic ratchet stays at 15,
answer #7).

**Mechanism count**: M1 (a composition argument + one startup check family), M5a (one claim:
a wire-format addition), M5b (a service-token path); M2, M3, M4 extend existing code. R2: one
startup check family. Total 4 new, each removing a named violation; 0 registries, 0 drivers,
0 settings, 0 network calls.

### 2.3 TA-2: the authority map as a deployment setting

As TA-1, but the map is `CLAIM_AUTHORITIES` (JSON env), a `SHARED_KEYS` entry so every service
holds the same value (`core/testing/deploy_profile.py:66`).

- Gains: an operator re-assigns without a release.
- **Disqualified by answer #9** ("set in the composition root code, not in ordinary
  settings") and therefore H-5. Also takes the last semantic-settings slot (15 of 16,
  `core/config/tiers.py:223`) and moves a security decision out of reviewed code.
- Kept in the table only to record why the flexibility is refused.

### 2.4 TA-3: per-authority signed claim blocks

Each authority signs its contribution with its own key (OIDC Core §5.6.2 aggregated claims;
Biscuit third-party blocks, research-trust §4); the issuer embeds the blocks; every verifier
checks each block against the authority's public key, which the composition root maps to the
claim keys it may carry. Legacy and skewed tokens fail verification by themselves (no `cpv`).

- **Gain**: a compromised *issuer* cannot forge a remote authority's claims. Out of scope by
  the threat model (goals.md; research-trust §2.3), and the issuer can still drop blocks.
- **No gain in-process**: a local authority's private key is readable by every module in the
  process, so against a mistaken module it protects exactly as M2 does.
- **Cost**: a key pair and a secret setting per authority, rotation procedure per key (skill
  `sandloom-key-rotation` grows), a signature per authority per mint, a signature verification
  per authority block per request (or a per-`jti` cache, a new process-wide state), +≈ 150 bytes
  per block. Instruction cost on the claims rung: **Unverified**; one Ed25519 verification per
  block against a claims-only request of ≈ 1.18 M Ir ([measured], `Role("admin")`) is likely
  well above the +1 % budget [inferred].
- **Disqualified by H-12**: its one gain lies outside the threat model, and TA-1 provides every
  required invariant with fewer mechanisms. Revisit only if process isolation (security-model.md
  "future capability") makes authorities mutually untrusted processes.

## 3. Scenario comparison, part (a)

"pass" = the expected outcome of requirements.md is observable with the mechanism; "fail" names
what is missing. All rows mono and split unless stated.

| scenario | TA-0 | TA-1 | TA-2 | TA-3 |
|---|---|---|---|---|
| A-H5 identity replaced by `test_identity`, assigned | pass | pass (P-1) | pass | pass (+ a key for `test_identity`) |
| A-B1 billing service, no identity authority bound | pass for platform gates (A0-3) | pass: unassigned/unbound `roles` → every platform route 403 | pass | pass |
| A-B3 app module named `identity`, unassigned | pass at mint (A0-2) | pass (mint + view) | pass | pass |
| A-B6 reserved key, optional then critical writer | pass (A0-2 violator rule) | pass | pass | pass |
| A-B8 issuer/verifier map skew | **fail**: undetectable | pass: `cpv` digest mismatch → untrusted, logged once | pass (same `cpv`); skew more likely (env per service) | pass (verification fails) |
| A-F2 optional non-authority writes `roles` | pass | pass | pass | pass |
| A-F3 `owner="identity"` | pass (startup refusal) | pass | pass | pass |
| A-F4 identity's critical contributor fails | pass (unchanged) | pass | pass | pass |
| A-F5 revocation applier without identity; block | R1/R2 (common) | R1/R2 | R1/R2 | R1/R2 |
| A-F6 authority ran, omitted the key, another wrote it | pass at mint | pass at mint and read | pass | pass |
| A-F7 `analytics` writes a foreign key: logins continue | pass | pass (P-13) | pass | pass |
| A-F8 hand-assembled `create_access_token` | **fail** (no sealed mint) | pass (M5b; P-11) | pass | partial: unsigned protected claims are not trusted, but the call is not refused |
| C-B3 legacy token; `Or` with another allowing branch | **fail**: legacy `roles` trusted until expiry | pass: protected keys untrusted; 401 `token_policy_outdated` only when no branch allows | pass | pass, but no 401 code without an extra marker |
| C-F3 two modules register one fact | pass (exists: validation C5) | pass | pass | pass |
| C-F4 foreign protected key (`tenant_ids` from billing) | pass at mint | pass | pass | pass |
| C-F5 battery absent, another module writes `roles` / `subscription_status` | pass for tokens from the merge; **fail** for any other token | pass: no assignment → no trusted value → deny | pass | pass |
| C-H6 independent tenant-grant provider assigned | pass at mint; `TenantScope` reads raw `tenant_ids` | pass (view feeds `TenantScope` and RLS binding) | pass | pass |
| I-10 / validation B6: no tenant provider, `forged` writes `tenant_ids` | pass at mint (unassigned → dropped) | pass | pass | pass |
| owner set: forged `Role("admin")` → deny; platform admin without trusted authority → deny; `Role("admin") \| Entitled("missing.fact")` with trusted role → allow | pass only for merged tokens | pass | pass | pass |
| `as_user(roles=[...])` in tests keeps working | pass (nothing reads provenance) | needs M5b's test authority (to prove) | same as TA-1 | needs a test key |
| answer #9 (composition root, not settings) | pass | pass | **fail** | pass |
| H-12 (no mechanism without a violation it removes) | pass | pass | — | **fail** |
| hot path (H-10) | 0 | ≈ 0 per check; one compare per request | ≈ 0 | signature verify per request (Unverified, likely > +1 %) |
| new mechanisms | 2 (+ A0-3 refactor) | 4 (+ M6 later) | 4 + a setting | ≥ 6 (keys, settings, wire format, verifier, rotation, cache) |

**T1-T10** (requirements.md I-22) under TA-1: T1 M1+M2+M4; T2 M1+M2+M4 (`tenant_ids` → view →
`TenantScope`, RLS); T3 M1 (billing keys assigned by the app, not the kernel; with no assignment
`Subscribed` denies, D2 closed); T4 M5b; T5 M3 only if widened to every contract prefix (else
remote holds and in-process stays first-registrant); T6 M2 (identity is the module path or the
binding, the short name is only the stamped value); T7 small fix A + M4 (claim-mode trust by
claim-key authority); T8 R1+R2; T9 (operator contract `identity.security.v1`, B-R4/B-R5) and T10
(third-party risk) are decision A and D questions, not touched here.

**Split**: the map comes from the same composition root in every service
(`SANDLOOM_COMPOSITION_ROOT`, `core/cli/project.py:875`); a verifier needs the map, not the
authority module (it checks `identity` in `claims_sources` without loading identity); `cpv`
catches a service that runs another map. Remote verification rests on the existing `service_id`
pin and pair MAC (security-model.md:382-462).

**Security-regression risk of TA-1**: (1) every token loses protected claims at upgrade until
refresh (accepted by answer #11's shape); (2) a wrong generated map is a silent lock-out, not an
escalation (fail closed, startup warning in the issuer naming unassigned protected keys); (3)
M5b's test authority must be impossible to assign in production (startup refusal, P-row below);
(4) f-2 trades "no token" for "no token with the violator's claims" for optional writers: rights
cannot rise either way.

---

# Part (b): portability of domain atoms

## 4. What an atom is, and the semantics that must not move

**Principal vs authorization target.** The principal is the authenticated subject
(`UserContext`: `user_id`, trusted claims via M4; a service subject for internal sources). The
target is an account or an organization, named by `Account`: `Account.user()` = the principal's
personal account; `Account.org(param=)` = the organization id read from the path and validated
against the trusted tenant allowlist (`account.py:213-218`, `validate_against_token=True`);
`Account.bound_tenant()` = the tenant `TenantScope` bound, whose kind decides user vs
organization (`account.py:231-250`). `OrgRole` reads its organization with
`validate_against_token=False` because membership itself is the authoritative check
(`atoms_live.py:207-215`) [read]. All three sources stay kernel concepts in every alternative; an
alternative changes only who states *which argument of which provider method* each source feeds.
After TA-1 the allowlist that validates `Account.org` is the trusted view's `tenant_ids`, so a
deployment with no assigned tenant authority makes `Account.org(...)` indeterminate (403/400),
never the raw token value.

**Order is semantic.** `And`/`Or` sort operands by `tier` (stable) and short-circuit
(`algebra.py:158-226`); `~(U & F)` with both live evaluates `U` only and is indeterminate; with
`F` re-tiered to claims it allows (research-authz-portability §1.1, probe P3 [run]). A moved
atom must keep its tier statically, or a decision changes, not only a cost.

**Tier also opens the DB session.** `require()` depends on `get_db` when `policy.tier >
TIER_CLAIM` (`deps.py:121-123`); a claims-only policy opens none (G-AUTHZ-02). Moving
`Subscribed` (claims tier) onto anything live would open a session and re-sort it.

**Common pieces, identical in every alternative** (each is its own change and baseline):

| id | piece | removes | note |
|---|---|---|---|
| K1 | **tenant kind** asks the *assigned tenant authority's declared membership check* (under TA-1, the authority of `tenant_ids`) instead of the `membership` adapter slot; no declaration → kind `unknown` → `bound_tenant()` indeterminate | B-R2 (`account.py:153-168`) | keeps 3 calls; C-B4 |
| K2 | `Roles`, `Permissions` move to identity (or are dropped as unused, an ADR-0010 surface change) | B-N15 | no importer (baseline §5.4) |
| K3 | `Feature` / `WithinLimit`: owner's research path (answer #21) moves them to billing with parameters carried; fallback keeps a generic claim atom in the SDK with `claim_key` required (no billing default) and the organization variant through the owner's declared check | B-N12 defaults; C-B10 parameter loss (`facts.py:466-473`) | H-2: no capability removed |
| K4 | `fake_access_fact` loses `LIVE_SUBSCRIPTION_FACTS` and the `billing.entitlements.v1` stub (`core/testing/authz.py:48`, `:120-128`) | B-R6 | tests of live atoms use `fake_contract(REF, Fake())` (exists) |
| K5 | provider down → `AccessDecision` indeterminate 503 `owner_unavailable` inside the algebra; a broken security invariant (MAC, `RpcAuthError`) stays an error | P1 probe: `Or` raises 503 today (research-authz-portability §1.7) | answer #19, separate change; C-F7 |
| K6 | per-atom `needs_request_session` instead of `tier > TIER_CLAIM`; seed the RPC proxy's describe cache from the boot readiness probe | ≈ +0.56 M Ir/req for `Role \| Credits` admin [measured]; first-request describe (perf-dx §2.2) | separate changes, decisions unchanged |
| K7 | claim-tier atoms (`Subscribed`, user-account `Feature`/`WithinLimit`) carry the owner's descriptor statically; a settings-dependent rule (`past_due` grace, `subscription_claim_rule`, `sandloom_billing/access.py:67-79`) is refreshed **off the request path** (boot + timer), not stamped at mint | describe every 300 s on the request path (`facts.py:64`) | answer #20: today's staleness — status ≤ token TTL, rule ≤ 300 s in a remote consumer — is kept; C-B8 table |
| K8 | archcheck rules read atom metadata, not `ORG_ATOMS` / `BILLING_ATOMS` (`core/archcheck/module_rules.py:59-64`, `api10_rules.py:500`) | class-name lists | H-20; how metadata is found differs per alternative |

## 5. Alternatives

### 5.1 PA-0 (minimal): existing `AccessFact` / `Entitled` / `ClaimRule`, smallest extension

The provider registers a fact per atom (`tenancy.org_role`, `billing.subscription.active`,
`billing.credits`), the consumer writes `Entitled(name, **params)`. Nothing is generated.

**What is missing today and the smallest extension that adds it**

| gap | today | extension |
|---|---|---|
| parameters | `facts.v1.evaluate(name, subject, request)`, no parameters (`facts_contract.py:73-108`; validation C gap) | optional `params` on `evaluate` and on `Entitled(name, **params)`; `AccessFact.params` declares their wire types (C-B1: an old owner refuses an unknown parameter → 501, never allow) |
| static tier | `Entitled.tier = TIER_LIVE` always (`atoms_fact.py:9-10`, `:46`) | `AccessFact.tier` (claim / live) carried in the descriptor; but the consumer learns it only from `describe()` at run time, while `require()` needs it at wiring (`deps.py:121-123`) |
| account / organization binding | the fact reads path, query, `X-Tenant-Id` itself; `org_params()` unknown to the consumer (gate order, B007) | `AccessFact.binds` (subject / org param / account) |
| risk | none on facts | `AccessFact.risk` (value table) |
| owner | first dotted segment or `WELL_KNOWN_FACT_OWNERS` (`facts.py:526-541`) | names prefixed by the owner; the kernel table goes (B-R3) |

**Verdict: disqualified.**

- **Policy syntax changes** (I-23, H-16, answer #18 "the policy expression stays the same"):
  `require(OrgRole("admin") & SubscriptionActive(of=...))` becomes
  `require(Entitled("tenancy.org_role", minimum="admin") & Entitled("billing.subscription.active", of=...))`.
- **No typing, no errors before run** (H-19): parameter names and values are strings checked at
  `describe()` time or by the owner.
- **Metadata is not static**: tier, binds and risk arrive by `describe()`; `require()` and archcheck
  need them before any request. Making them static means generating them, which is PA-3.
- **Calls**: same RPC count, but in a split the owner runs `FactsContract.evaluate` in its own
  read unit and the resolver calls the domain contract inside it; an extra `BEGIN`/`ROLLBACK`
  per atom is likely [inferred; research-authz-portability §4.2]. If measured, it breaks the hard
  rule of answer #5.
- What it does well: `fake_access_fact(name, value)` works for every atom unchanged; the
  provider-down path already returns indeterminate 503 for an unreachable owner
  (`atoms_claims.py:184`, `:267`) [read]; parameters reach the owner (C-B10).

### 5.2 PA-1: consumer-owned declarative `Requirement` classes

The owner's variant B (tm reflection on `kb-alt-authz`, "try first"). The kernel offers one
declarative base; each consumer writes the vocabulary it uses in its own package:

```python
class OrgRole(ContractCheck, contract=TENANCY_MEMBERSHIP_V1, method="has_role_at_least",
              binds={"organization_id": From.org("org_id"), "user_id": From.subject()}):
    minimum: Literal["member", "admin", "owner"]
```

Claim-tier atoms: `class Subscribed(ClaimFactCheck, fact="subscription.active")` with
`tier = TIER_CLAIM` and the owner's descriptor refreshed off the request path (K7).

- **Metadata**: tier and category are fixed by the base (`ContractCheck` is always live), which
  keeps order. **Risk is the consumer's word** unless the provider declares it: H-20 requires
  that a module cannot lower its own risk. So PA-1 needs a provider declaration on the contract
  method (today `risk=` exists for commands only, `core/module_api/_method.py:102-160`), carried
  in the stub, and an archcheck rule comparing the consumer's class with it. With that, the
  provider writes half of PA-2's declaration and every consumer still writes the class.
- **Vocabulary forks**: two consumers may give `OrgRole` different binds or parameter types; a
  provider rename breaks each consumer separately.
- **Kernel K1 cannot use a consumer class**: tenant kind still needs a provider-side
  declaration of the membership check.
- **DX**: ≈ 6 lines per atom per consuming app instead of 31 (perf-dx §3); four beginner names
  leave `sandloom.sdk`, and no generated replacement exists: every app (and every example)
  writes its own.
- **Third-party provider (C-H3)**: the consumer edits its class to the new contract; route code
  and imports unchanged (the class is the consumer's own). This is PA-1's one advantage over PA-2.

### 5.3 PA-2: contract-anchored access checks

research-authz-portability §4.4, the main candidate of answer #17 (not the approved
architecture). The provider marks one boolean contract read method:

```python
@contract.read(planned=True)
@access_check("OrgRole",
              bind={"organization_id": From.org(param="org_param", validate_against_token=False),
                    "user_id": From.subject_id()},
              category=Category.FORBIDDEN,
              risk={"admin": {"minimum": ["admin", "owner"]}},
              witnesses_membership=("organization_id", "user_id"))   # K1, and the later 3rd-call fix
async def has_role_at_least(self, organization_id: UUID, user_id: UUID,
                            minimum: Literal["member", "admin", "owner"]) -> bool: ...
```

`sandloom generate` writes, next to the existing `Protocol` stub, a typed class with its
metadata as a literal (`__sandloom_atom__ = {"contract", "method", "tier", "category",
"binds", "risk"}`) and `__init__(self, minimum: Literal[...], *, org_param: str = "org_id")`;
an account-bound check gets `*, of: Account` with no default (a missing `of=` becomes a type
error, today only runtime B017, `account.py:112-123`). The kernel's one `ContractCheck`
evaluator binds subject / org / account through `resolve_org_id` / `resolve_account`, calls
`resolve_contract(...)` and the one method; absent → 501 as today.

Claim-tier atoms: the owner's existing `AccessFact` (`sandloom_billing/access.py:141-160`) is
generated into a typed claim class (`tier = TIER_CLAIM`, descriptor literal: claim key, owner,
floor, deny category), its settings-dependent rule refreshed off the request path (K7).
`Credits` moves onto a boolean `has_credits_at_least(tenant_type, tenant_id, minimum)` read,
answer #24: same query, same account binding, never a substitute for the atomic spend.

- **Removes**: `register_live_adapter`, the adapter table and its two kinds (A19, B-R1,
  `live_contracts.py:107-164`); the kernel's `OrgRole`, `SubscriptionActive`, `Credits`,
  `Subscribed`, `ACTIVE_SUBSCRIPTION_STATUSES`; `WELL_KNOWN_FACT_OWNERS`; the class-name lists in
  archcheck (K8).
- **Adds**: one declaration field on contract read methods (`access_check`, next to the existing
  `risk=`), one new kind of generated content (atom classes in the stub module), one generic
  evaluator (`Requirement` subclass, kernel).
- **H-20**: archcheck resolves an imported name to the generated file and reads the literal
  (AST, no import); a hand-written `Requirement` that calls a method named in a stub literal is
  recognised and its tier, risk and parameters checked (answer #22); a lowered `risk` is a
  `contracts.snapshot.json` change classified as breaking (T004); hand-editing the literal fails
  T007.
- **Limits**: one boolean method per atom; where bundled stubs live is decision B's question
  (generating tenancy's atoms into `sandloom/shared/contracts/` would keep the kernel shipping a
  module artifact); the import of four beginner atoms changes (answer #18, ADR-0010 amendment).

### 5.4 PA-3: parameterized `AccessFact` with generated typed wrappers

PA-0 plus generation: `sandloom generate` writes `class OrgRole(Entitled)` with a typed
`__init__` and the fact's descriptor as a literal (tier, binds, risk, rule), so syntax, typing
and static metadata hold. The owner's variant A.

- **Call path**: every typed atom goes through `<owner>.facts.v1.evaluate(name, params, …)`, a
  generic string-keyed second contract in front of the domain contract. Mono: the local resolver
  calls the contract directly (equal). Split: likely an extra provider unit per call [inferred;
  must be measured]. Consumers bind `<owner>.facts.v1` instead of `tenancy.membership.v1`
  (`SERVICE_BINDINGS`, `RPC_ALLOWED_CALLS` of existing split deployments change).
- **Mode by configuration**: the fact's mode (`ACCESS` setting, `facts.py:255-277`) can move a
  live check to projection behind a fixed generated tier: a deploy setting changes the freshness
  of `OrgRole`, which today is fixed (I-23 freshness).
- **Type systems**: parameter types must be expressible in the fact descriptor, a second schema
  beside contract wire types.
- **DX**: `fake_access_fact(name, value)` keeps working for every atom (best test ergonomics of
  the four).
- **Mechanisms**: parameter wire format, generated wrapper artifact, static descriptor
  generation (+3), minus the adapter table.

### 5.5 Not pursued

- **A light client package per provider** (research-authz-portability §4.5): competes with
  decision B (where stubs live), adds a wheel per provider and manual version alignment.
- **A provider-written atom source copied into consumers** (§4.3 iii-b): ships provider code into
  consumers, against the DTO copier's rule "never methods or validators"
  (`core/cli/dto_copies.py:9-17`).
- **A policy schema interpreted by the kernel** (Cedar / caveat-like): a runtime engine, refused
  by the owner (tm reflection on `kb-poc-authz`).

## 6. Comparison, part (b)

### 6.1 Criteria

| criterion | PA-0 | PA-1 | PA-2 | PA-3 |
|---|---|---|---|---|
| RPC / DB vs perf-dx §2.1 (hard, answer #5) | split: likely +1 provider unit per atom (measure) | equal if the consumer calls the same method | **equal by construction** (same method, same arguments) | split: likely +1 provider unit (measure) |
| 3 calls for `bound_tenant` shape | ≥ 3 (K1 through a fact) | 3 | 3; 2 with the witness fix as its own change | ≥ 3 |
| order, short-circuit, tier | tier only from `describe()` at run time: `require()` cannot use it at wiring | base fixes live; claim class fixes claims | **generated literal** (live for contract checks, claims for claim classes) | generated literal; mode by config behind it |
| `~(U & F)` tier effect | moving `Subscribed` onto `Entitled` re-tiers it: decision change | none if the consumer uses the right base | none | none |
| G-AUTHZ-02 (claims policy: no session, no I/O) | fails for `Subscribed` on `Entitled` | holds | holds | holds |
| typing, errors before run | **fail** | pass (consumer's own types) | pass, better than today (`Literal`, required `of=`) | pass |
| policy syntax (I-23, H-16) | **fail** (`Entitled("…")`) | pass | pass | pass |
| import of the atom | `Entitled` from `sdk.authz` | consumer's own module | generated stub module (or a generated per-app re-export) | generated module |
| metadata a module cannot lower (H-20) | not static | needs a provider declaration + cross-check | **generated + snapshot (T004/T007)** | generated + snapshot |
| archcheck on hand-written `Requirement` calling a declared check (answer #22) | n/a | the cross-check is the rule | stub literal names the method | wrapper literal names the fact |
| describe on the request path (I-14 remote tier) | yes (300 s) | contract describe only (K6 fixes) | none after K6/K7 | none if the descriptor is static |
| provider down in `Or` (C-F7) | indeterminate 503 already on the facts path | one place in the base (K5) | one place in the evaluator (K5) | facts path |
| beginner tier / ADR-0010 | 4 names leave; replacement is advanced `Entitled` | 4 names leave; no replacement | 4 names leave; generated replacement, ADR amendment (answer #18) | as PA-2 |
| DX task (31 consumer lines, 4 advanced names) | 0 class lines, a string per use; provider ≈ 10 lines | ≈ 6 lines per atom per consumer | **0 consumer lines; 1 provider decorator** | 0 consumer lines; provider ≈ 10 lines |
| tests (`fake_access_fact`) | unchanged | `fake_contract` | `fake_contract` | unchanged |
| third-party billing (C-H3) | fact names change in every policy | consumer edits its classes | import lines change; expressions stay | import lines change |
| new mechanisms (§4.2) | +2 (param wire, static tier/binds fields), −1 | +1 base, +1 provider field, +1 check rule; −1 | **+1 field, +1 generated content kind; −2** (adapter table, `register_live_adapter`) | +3; −1 |
| kernel keeps domain knowledge | none | none | none | none |

### 6.2 Decision-C scenarios

| scenario | PA-0 | PA-1 | PA-2 | PA-3 |
|---|---|---|---|---|
| C-H1 fixture module adds a typed policy, used in `And`/`Or`/`Not` | pass, untyped | pass | pass (P12: no `src/sandloom` diff) | pass |
| C-H2 existing billing gates, statuses 402/403, claims-only no session | **fail** for `Subscribed` (session, re-tier) | pass | pass | pass |
| C-H3 third-party billing package | expressions change | pass | pass (imports change only) | pass (imports) |
| C-H4 org membership mono = split | pass | pass | pass | pass |
| C-H5 `inventory.stock.available`; `OrgRole` in a consumer without tenancy | pass | pass | pass | pass |
| C-H6 independent tenant-grant provider | needs K1 + TA-1 | needs K1 + TA-1 | K1 via `witnesses_membership` + TA-1 | K1 + TA-1 |
| C-B1 owner adds an optional parameter; old owner in a split | new param → 501 at old owner | stub-driven; old provider rejects unknown argument (RPC) | same as PA-1; regenerated atom gains an optional keyword | 501 at old owner |
| C-B2 owner neither local nor bound: 501, also at bootstrap (H-19) | 501; preflight from `contracts()` | 501; preflight | 501; preflight from the literal's contract | 501; preflight |
| C-B4 `bound_tenant` without tenancy | K1 | K1 | K1 | K1 |
| C-B5 org `Feature` with no org fact: 501 | pass | pass | pass (no declared check → 501) | pass |
| C-B6 one atom moved, mono and split, counts and order equal | at risk (split unit) | pass if copied faithfully | **pass by construction** | at risk (split unit) |
| C-B7 tiers, no describe on request | fail (300 s describe) | pass with K6 | pass with K6/K7 | pass if static |
| C-B8 `past_due` freshness table | K7 | K7 | K7 | K7 |
| C-B9 `Credits` comparison moves to `has_credits_at_least` | n/a (fact) | consumer may keep comparing | pass if the boolean read runs the same query (measure: 1 call, 4 DB) | via fact |
| C-B10 `Feature`/`WithinLimit` parameters reach the owner | pass | pass | pass (K3, declared check with `key`, `used`) | pass |
| C-F1 billing down, live and claims | K5 | K5 | K5; claims decided from claims if billing trusted (TA-1 view) | K5 |
| C-F2 a policy raises | as today | base wraps | evaluator wraps | as today |
| C-F3 two modules, one name | fact duplicate refusal (exists) | consumer classes are local; contract name ownership (M3) | generated class per contract; M3 | fact duplicate refusal |
| C-F6 aliased / re-exported / hand-written atom | strings: name resolution needed | cross-check rule | literal resolution; hand-written call of a declared method recognised | literal resolution |
| C-F7 provider down inside the algebra | pass (facts path) | K5 | K5 | pass (facts path) |
| owner set: `OrgRole("admin")` in a consumer without tenancy works remotely; `Subscribed` with billing down denies without another branch; `~Entitled("missing.fact")` never allows; a third-party atom with tenant scope recognised by archcheck; a new atom needs no kernel change | syntax fails | archcheck needs the provider field | pass | pass |

## 7. Which alternatives the evidence favours, and why

### 7.1 Part (a): TA-1

1. It is the smallest set that meets answers #9-#14 and I-09: TA-0 is exactly TA-1 minus the
   read-side check, the policy marker and the sealed mint, and each of those three is required by
   a named answer (#11, #11/A-B8, #13). Nothing smaller passes.
2. Every precedent studied binds a claim to a verified identity through an operator-owned
   registry and refuses self-description (research-trust §4.1); TA-1 does the same with the
   import path (local) and the pinned binding (remote).
3. Its hot-path cost is not above today's per check (R6, indicative), and it adds no setting,
   registry, driver or network call.
4. TA-3's strength (hostile issuer) is outside the threat model; TA-2's flexibility is refused by
   answer #9.

Open inside TA-1, for the PoC or the ADR: M3 widened to every contract prefix (closes T5
in-process; makes a third-party billing serve its own contract name, which PA-2 handles with an
import change); M6 after the PoC (answer #15); where the bundled default map lives (decision A).

### 7.2 Part (b): PA-2, PA-1 as the control

1. PA-2 is the only alternative where equal calls, queries, tier and order hold **by
   construction**, not by author discipline (PA-1) or measurement hope (PA-0, PA-3 in a split).
2. It answers the metadata problem with the same artifact: the generated literal is what
   `require()` reads at wiring, what archcheck reads statically, and what the snapshots guard; the
   consumer cannot edit it, the provider lowers it only through a breaking snapshot change.
3. It removes two mechanisms (adapter table, `register_live_adapter`) and adds two, and turns the
   DX task's 31 consumer lines into one provider decorator.
4. PA-1 converges on PA-2 as soon as H-20 is applied (a provider declaration is needed anyway),
   and keeps the forked vocabulary. It stays the documented form for a module's own one-off check.
5. PA-3 is worth one count measurement on the decisive scenario (answer #17 asks for the
   comparison): if the split shows no extra provider unit, its remaining costs are the binding
   change for existing deployments and the configuration-chosen mode.

Accepted costs of PA-2: four beginner names change import (ADR-0010 amendment, answer #18); one
boolean method per atom (`has_credits_at_least`); tests of live atoms use `fake_contract`;
stub location waits for decision B.

### 7.3 How (a) and (b) meet

- TA-1's trusted view is what claim-tier atoms (`Subscribed`, `Feature`, `WithinLimit`) read in
  every PA alternative: a billing claim is trusted only from the authority the app assigned, so
  without billing assigned they deny (C-F5) and no kernel default names billing (answer #10).
- K1 needs the tenant authority: TA-1 assigns it (`tenant_ids`), PA-2 says which of its methods
  confirms membership (`witnesses_membership`).
- A live check's provider is trusted through the binding (remote) and, in-process, through M3:
  PA-2 does not make whoever serves `tenancy.membership.v1` a trusted authority
  (research-authz-portability §6.2).
- One platform admission (TA-1 M4) is the only place `admin` is read for platform routes; it is
  unrelated to `OrgRole`'s `admin` value table.

## 8. What the PoCs must build and prove

### 8.1 `kb-poc-trust` (TA-1)

**Build**, in `design/kernel-boundary/poc/trust/`, extending `poc/replace/` (validation.md §1.1:
mono and two-process split over real RPC, `SANDLOOM_TEST_OWNER=kb-trust-poc`), on a kernel wheel
patched with: M1 (`claim_authorities` in the PoC app's composition root, startup refusals e-2,
e-3, e-4); M2 (mint check by module path / binding, `owner=` refusal, violator rule); M3 (small
fix A, plus the widened prefix check behind a flag); M4 (trusted view, one platform admission
used by `require_platform_admin` and `platform_operator`, claim-mode trust by claim-key
authority); M5a (`cpv`, `token_policy_outdated` as indeterminate 401); M5b (sealed mint,
`create_service_token`, test authority for `as_user`, refused in a production composition). The
TA-0 subset (M1, M2, A0-3 only) runs as the control on the same fixture by switching M4/M5 off.

**Prove** (pass/fail per row, mono and split, commands and output into validation.md):

| # | scenario | pass |
|---|---|---|
| T-1 | assigned `test_identity`, `sandloom_identity` not installed | every gate 200 for `ADMIN`; no kernel file changed after M1-M5 (A-H5) |
| T-2 | foreign-only claim: `forged` (optional, unassigned) writes `roles`, `permissions`, `tenant_ids` | token has the authorities' values only; `forged` in `claims_sources_failed`; ERROR log names `forged` and the key; metric +1 (A-F2, C-F4) |
| T-3 | `forged` critical | mint 503 naming `forged`, not the authority, in either registration order (A-F7 attribution) |
| T-4 | optional violator, 100 logins | 100 tokens, 0 × 503, 100 ERROR lines, counter = 100 (A-F7, answer #12) |
| T-5 | assigned authority missing in the issuer, or registered non-critical; path typo | startup refusal with a code naming key and module (answer #14) |
| T-6 | no assignment for `roles` / `tenant_ids`; `forged` writes them | every guarded gate 403, `TenantScope` 400/403, no RLS rows (old B6, C-F5) |
| T-7 | local spoofing: app module named `identity` (lean); `owner="identity"`; `forged` registered first | nothing trusted from any of them; `owner=` startup refusal (A-B3, A-F3) |
| T-8 | remote squat: provider process hosts `forged` registering `identity.claims.v1`; binding of an assigned module's claims contract to a process without it | refused at provider startup; mint 503 for the critical binding (T6, T7) |
| T-9 | legacy tokens (three shapes of research-trust §3.3) at the new verifier | plain authenticated routes 200; `Role("admin")` alone → 401 `token_policy_outdated`; `Role("admin") \| <allowing atom>` → 200; `~Role("admin")` → 401, never allow; after refresh, real rights (C-B3, answer #11) |
| T-10 | issuer map A, verifier map B | verifier treats protected keys as untrusted, logs the `cpv` mismatch once; skew visible at startup or `/ready` (A-B8) |
| T-11 | `create_access_token({"roles": ["admin"], "claims_sources": [...]})` in the issuer process; then `create_service_token` with a protected key | first refused with a code; second refused; a service token grants no `Role`, `Perm`, platform admission or tenant (A-F8, answer #13) |
| T-12 | `as_user(roles=["admin"])` in a test app; the same test authority assigned in a production composition | test passes unchanged; production composition refused at startup |
| T-13 | owner regression set: forged `Role("admin")` → deny; platform admin without trusted authority → deny; `Role("admin") \| Entitled("missing.fact")` with trusted role → allow; `~Entitled("missing.fact")` never allow; same decisions mono vs split | all as stated |
| T-14 | `Role`/`Perm` I/O | 0 DB, 0 Redis beyond the revocation `GET`, 0 RPC (call counters as perf-dx §1.2) |
| T-15 | performance on a reproducible bench, alternating base/candidate, noise stated | request rung (`Role & Perm`, `TenantScope` route) within +1 % cachegrind instructions; mint latency with 3 contributors within +2 %; p50/p95/p99 reported (answer #5: a security fix is reported, not blocked) |
| T-16 | revocation R1/R2 (may be a separate PoC row set) | without identity, a failing post-commit mark is applied by the kernel's durable consumer or `/ready` reports it; an issuer without a `user_blocked` consumer is refused at startup (A-F5) |
| T-17 | conformance | split conformance suite and `sandloom test --full` green; route and API snapshot diffs only the reviewed additions |
| T-18 | TA-0 control | records which of T-9, T-10, T-11 fail without M4/M5 (evidence for §7.1 point 1) |

Exit: T-1 to T-14 and T-16 pass mono and split, T-15 within budget or reported to the owner,
T-17 green. A failure names the mechanism that is missing or too weak.

### 8.2 `kb-poc-authz` (PA-2, with PA-1 and PA-3 as comparisons)

**Build**, in `design/kernel-boundary/poc/authz/`:

1. *PA-1 control*: the DX task's provider `quota` and consumer `reports`; the consumer's atom
   as today (31 lines) and on a ≈ 20-line PoC `ContractCheck` base; record lines, advanced names,
   archcheck visibility.
2. *PA-2 minimal*: a PoC `access_check` field on `@contract.read`; the stub generator emitting
   `__sandloom_atom__` and a typed class; the generic `ContractCheck` evaluator importing only
   `sandloom.sdk.authz` names; applied to `tenancy.membership.v1.has_role_at_least` (`OrgRole`),
   `billing.entitlements.v1.check_subscription_active` (`SubscriptionActive`) and a new
   `has_credits_at_least` (`Credits`); `Subscribed` as a generated claim class from billing's
   `AccessFact`; K1 through `witnesses_membership`.
3. *Decisive scenario* (owner, tm reflection on `kb-poc-authz`):
   `require(OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant()))` behind
   `TenantScope()`, two processes, the consumer venv cannot import `sandloom_tenancy` or
   `sandloom_billing` (asserted with `python -I -c`), tenancy and billing in the provider
   process; the same route in the monolith.
4. *PA-3 count comparison* on the decisive scenario only: `Entitled` with `params` over a patched
   `facts.v1.evaluate`, counting provider statements (answer #17).
5. *Separate changes on top*, each its own run and baseline: K5 (provider down as
   indeterminate), K6 (session flag, describe seeding), the witness 3→2 call fix (answer #23).

**Prove** (pass/fail, commands and output into validation.md):

| # | criterion | pass |
|---|---|---|
| Z-1 | decisions | equal to today for admin+active (200), member (403 `OrgRole`), admin+inactive (402), outsider (403, `SubscriptionActive` not evaluated), no bound tenant (400 indeterminate), mono and split |
| Z-2 | counts, steady state (exact) | mono 3 calls / 9 DB; split 3 RPC / 9 provider DB / 0 consumer DB; outsider 1 / 3; `Credits` 1 call / 4 DB (C-B9). PA-3 row reported beside it. Witness fix: 2 / 6 as its own row |
| Z-3 | order and laws | atoms evaluated and order equal to today (counting wrapper); G-AUTHZ-13 law properties pass with generated atoms in the trees, incl. one raising and one with an absent owner (I-07 missing proof); `~(U & F)` decisions equal |
| Z-4 | tier pins | claims-only policy with the generated `Subscribed`: no DB session, no I/O (G-AUTHZ-02); live policies as today |
| Z-5 | metadata on the request path | with K6: 0 describe `GET`s on the first request; without: not more than today |
| Z-6 | instructions / latency | cachegrind mono: bound shape ≤ 5,827,384 Ir/req + 1 %, `OrgRole` ≤ 2,624,547 + 1 %, `SubscriptionActive` ≤ 4,140,788 + 1 %; rusage split within +3 %; p50/p95/p99 against a same-session base |
| Z-7 | typing | mypy rejects `OrgRole("admn")` and `SubscriptionActive()` without `of=` |
| Z-8 | archcheck (H-20, answer #22) | a third-party fixture atom with an organization bind is found by the gate-order and C027-equivalent rules, an admin-risk atom by the sensitive-route rule, with no class name in a list; a hand-written `Requirement` calling a declared check is recognised and its tier/risk/parameters checked; hand-editing the literal fails T007; lowering the provider's `risk` fails generate (T004) |
| Z-9 | fail-closed | provider absent 501; provider down 503, never allow, incl. under `Not`; with K5, `OrgRole \| SubscriptionActive` with tenancy down allows only on an active subscription, with a deny-log line and metric for the indeterminate branch (C-F7); an RPC authentication failure stays an error |
| Z-10 | owner absent in a split; stale consumer declaration | consumer with no binding: 501 and reported at preflight (H-19); consumer generated against an older provider declaration: the contract-hash check reports it, decisions never allow on a mismatch; a consumer passing a new optional parameter to an old provider gets 501/503 (C-B1) |
| Z-11 | generation | `sandloom generate` writes nothing when only a provider body changes; rewrites stub + atom when the decorated declaration changes; byte-identical under two `PYTHONHASHSEED`s (I-15) |
| Z-12 | kernel boundary | adding `QuotaAvailable` via `access_check`: `git diff --stat -- src/sandloom` empty; no tenancy/billing name left in `shared/authz` after the move (grep) |
| Z-13 | `Feature` / `WithinLimit` | moved to billing with parameters: user and organization accounts, mono and split, parameters reach the owner (C-B10, answer #21) |
| Z-14 | `past_due` | semantic-equivalence table (case, today's decision and staleness, candidate's) for a status change and a `BILLING_DENY_ON_PAST_DUE` change during a token's life, mono and split (C-B8, answer #20) |
| Z-15 | DX task re-run | consumer atom lines 31 → 0, consumer advanced names 4 → 0, framework/doc errors ≤ 6, hand-edited files ≤ 7; the same for the PA-1 control |
| Z-16 | full suite | `sandloom test --full` and the split conformance suite green |

Exit: Z-1 to Z-5, Z-7 to Z-12 and Z-16 pass; Z-6 within budget or reported to the owner; Z-13,
Z-14 and Z-15 recorded. A PA-2 failure on Z-2 or Z-3 sends the comparison to PA-3's numbers from
step 4.

## 9. Open items for the ADR

| item | where it is decided |
|---|---|
| M3 widened to every contract prefix (T5 in-process) | `kb-poc-trust` flag run, then ADR-C |
| where the default authority map of the bundled modules lives (`sandloom.main` is kernel) | decision A |
| where bundled modules' generated atoms live | decision B |
| ADR-0010 amendment: four beginner atoms from the generated consumer API, migration of docs and examples | ADR-C with the owner (answer #18) |
| declared claim keys (M6) | after `kb-poc-trust` (answer #15) |
| `Feature` / `WithinLimit` final home | Z-13, then the owner (answer #21) |
| `past_due` freshness | Z-14 table (answer #20) |
