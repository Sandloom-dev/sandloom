---
search:
  exclude: true
---

# ADR-0014: Claims trust assigned by the application; domain authorization atoms generated from contracts

- **Status:** Proposed (2026-11-08). The owner accepts.
- **Date:** 2026-11-08
- **Deciders:** the owner, Sandloom maintainers
- **Decision id in the assessment:** decision C: part (a) trust, alternative TA-1; part (b)
  portability, alternative PA-2
- **Amends:** [ADR-0010](0010-public-api-1.0.md) (four beginner atoms move to the generated
  consumer API: the amendment of 2026-11-08 at the end of ADR-0010; an approved exception to
  public-API preservation for the live adapters, decision 14);
  [ADR-0009](0009-route-policy-and-session-freshness.md) (risk declared at its source now
  includes access checks)
- **Related:** [ADR-0012](0012-kernel-boundary.md) (the standard trust map lives in
  `sandloom-standard`; the kernel revocation applier), [ADR-0013](0013-contract-ownership.md)
  (where generated atoms live; expectations)
- **Research and evidence:** `design/kernel-boundary/research-trust.md`,
  `research-authz-portability.md`, `alternatives-c-authz.md`, `adversarial.md` §4,
  `poc/trust/results.md` (commit `627c9c9`), `poc/authz/results.md` (commit `ccc420e`),
  `perf-dx-baseline.md` (commit `162d84a`), `validation.md` §1, `guarantee-map.md`

## Context

**Trust.** Claims contributors were merged last-writer-wins until PR #70 (`e264b09`), which
refuses a second writer of a key and the reserved keys. What stays open is who the authority of
a key is:

- the authority ran and omitted `roles`, so another module's `roles: ["admin"]` passed `Role`
  and the platform-admin gate itself (gate probe G2-G4, `roadmap-review.md` §2.2);
- the authority failed non-critically, so `Role` and `Perm` trusted a forged writer
  (`validation.md` A3);
- with no tenant-grant provider in the token, a contributor's `tenant_ids` bound the tenant and
  opened Postgres RLS to its rows (`validation.md` B6);
- the platform-admin gates trust the literal source `"identity"`, that the module named
  identity ran, not that it wrote `roles` (B-S1, B-S2); an app module named `identity` gets
  admin where `sandloom_identity` is not installed (A6);
- any code holding the signing key mints any claims and provenance (guarantee-map S5); old,
  hand-minted and skewed tokens are read without a check.

PR #72 (`206fa2d`, small fix A) ties `AccessFact.owner_module`, `owner=` and the families
`<m>.claims.v1` / `<m>.facts.v1` to the registering module (SANDLOOM-B034).

**Portability.** The kernel holds billing and tenancy semantics: `Subscribed`
(`ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}`), `SubscriptionActive`, `Credits`,
`OrgRole` (`member/admin/owner`), `Feature`, `WithinLimit`, two process-global live-adapter
kinds (`register_live_adapter`, A19, B-R1), `WELL_KNOWN_FACT_OWNERS` (B-R3) and `tenant_kind`
asking tenancy's membership (B-R2). Four of these atoms are beginner names (B-N14). A typed,
parameterised atom over another module's contract costs a consumer a 31-line hand-written
`Requirement` with four advanced names, invisible to every static rule (perf-dx §3). Archcheck
rules match class names (B-N17).

**What must not move** (goals.md, H-4, H-16, I-23, answer #5): the algebra (`A | B` with `A`
indeterminate and `B` allow is allow; `~indeterminate` never allows), evaluation order and
short-circuit, the three tiers (claims in memory, local fact, remote fact), and for every policy
the number of network calls, database queries and the policy syntax. The baseline the move must
keep: `OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())` costs 3 contract calls
and 9 database round trips, mono and split (perf-dx §2.1).

## Decision, part (a): trust

### 1. The composition root assigns authorities; the kernel has no default

`create_app(claim_authorities={key: authority, ...})` in the application's composition root
code, never in settings (answer #9). The kernel holds no default and names no module; the
standard map lives in `sandloom-standard` (ADR-0012, owner #1), and `sandloom new` writes the
app's map into `<app>/asgi.py`.

- **Protected keys** (answer #10): `roles`, `permissions`, `tenant_ids` always; `tenant_id` and
  `org_id` with the authority of `tenant_ids`; every billing claim used to grant access
  (`subscription_status`, `plan_code`, `features`, `limits`) and every key a kernel atom or a
  generated claim atom reads; every key the map names. `email` and `email_verified` are issuer
  fields.
- **The map names the issuer** (adversarial X2): `claim_authorities` has an `issuer` entry. A
  module that declares `issues_tokens=True` and is not the map's issuer, or the reverse,
  refuses startup.
- **An authority is verified, never named into trust.** Local: the module actually registered
  from the assigned import path (short name and loaded import path both match). Remote: the
  `<m>.claims.v1` binding in `SERVICE_BINDINGS`, pinned `service_id`, pair-MAC authenticated
  (ADR-0004). A module naming itself `identity` is not proof.
- **Startup refusals** (SANDLOOM-B035 proposed), in the issuing process: an assigned path that
  names no module (a typo); an authority neither loaded nor bound; a non-critical authority
  outside decision 7's billing rule (answer #14); the kernel test authority in a non-test
  composition; an issuer mismatch.

### 2. Mint: who may write a protected key

In the existing merge pass: a protected key is accepted only from its assigned authority's
step. The kernel forms the system fields (`sub`, `user_id`, `iat_us`, `jti`, `auth_time`,
provenance, `cpv`) and refuses them from any contributor.

**Violator rule** (answer #12). An optional contributor that writes a protected key it is not
assigned, or a reserved key, loses its whole result: it is recorded in
`claims_sources_failed`, logged at ERROR naming the contributor and the key, and counted in
`claims_contributor_violations_total`; its partial result is never kept. A critical violator
refuses the mint naming itself, never the authority. A required authority's failure refuses the
mint (answer #14). This replaces PR #70's "any foreign key aborts every mint with 503", so one
buggy optional module no longer denies every login (problem #14).

### 3. Read: one trusted view per verified token

The trusted view is computed once per verified token inside `decode_token` and cached with it,
the policy marker in the cache key. A protected key is in the view only when its assigned
authority is in `claims_sources`, not in `claims_sources_failed`, and the token's `cpv` entry
for that key equals the verifier's. Every reader sees only the view: `UserContext`, `Role`,
`Perm`, `TenantScope`, the tenant allowlist that validates `Account.org`, claim-mode facts
(trusted by the authority of their claim key and by the owner check), the idempotency
fingerprint, and **one platform admission** used by both `require_platform_admin` and
`platform_operator`, trusting the platform role `admin` only from the assigned authority of
`roles`. `UserContext.untrusted_claims` lists `{key: reason}`.

- An **unassigned** protected key, or an authority absent from the composition, is a definite
  deny (403); `TenantScope` answers 400/403 as today.
- A key untrusted because the token predates the policy (no `cpv`, or another digest for that
  key) raises `PolicyIndeterminate(status=401, code="token_policy_outdated")` inside the
  algebra: `A | B` still allows on `B`, `~` never allows. `TenantScope` and the platform
  admission map it to 401 `token_policy_outdated`. Old tokens keep working on routes that need
  no protected claim until they expire (answer #11). The first foreign `cpv` value per key is
  logged once.
- **Status of an indeterminate decision**, when several branches are indeterminate: 401
  `token_policy_outdated` (a refresh can make that branch allow), then 503 `owner_unavailable`
  (a retry later can), then 501 (capability absent, `contract_mismatch`: neither fixes it). The
  adversarial review proposed 503 first (C-a4); this ADR orders by what the client can fix
  without an operator, and the owner confirms (decision.md, C7). Law tests cover the new status.

### 4. `cpv` per key, with an issuer × verifier rollout and rollback matrix

Owner decision #10. The token carries
`cpv = {"v": 1, "k": {<key>: <digest of (key, authority identity)>}}`, about 10 bytes per
assigned key, stamped by the merge and by `create_service_token`. Changing one key's authority
changes only that key's digest; trust in the other keys is untouched. A verifier compares per
key and fails closed on any digest it does not accept.

A key's entry may name a previous authority: `claim_authorities={"roles": Authority("acme",
previous="identity")}`. A verifier then accepts either digest for that key; an issuer always
stamps the current one. The previous entry exists only for one rollout and is removed by the
next deploy. It is a composition argument, not a setting; it is the one new state this decision
adds (adversarial C-a1).

**Adoption** (old code O stamps and checks nothing; new code N stamps and checks):

| issuer \ verifier | O | N |
|---|---|---|
| O | today | no `cpv`: protected keys untrusted; 401 `token_policy_outdated` only where a policy needs them and no branch allows; refresh returns to an O issuer: a loop **to avoid** |
| N | the O verifier ignores `cpv` and trusts as today: no outage, today's weakness for the window | trusted per key |

Order: issuers first, then verifiers; the adoption completes only when no O verifier remains.
The deploy-profile check (T014) and the split conformance suite require every service of a
profile at or above the adoption release. Rollback: verifiers back first, then issuers.

**Changing the authority of key K** (map M1 → M2; other keys equal in both):

| step | issuers | verifiers | tokens with K's M1 digest | tokens with K's M2 digest |
|---|---|---|---|---|
| 0 | M1 | M1 | trusted | — |
| 1 | M1 | M2 with `previous=` M1's authority | trusted | trusted |
| 2 | M2 | M2 with `previous=` | trusted | trusted |
| 3 | M2 | M2 | K untrusted: 401 on policies needing K, one refresh | trusted |

Rollback from step 2 or 3: verifiers to M1 with `previous=` M2's authority, then issuers to M1,
then drop `previous=`. Without step 1 either order loops on K during the window (verifier-first:
old issuer keeps minting M1; issuer-first: old verifiers reject M2), which is why the
unconditional "verifiers last" rule is not adopted.

**Assigning a previously unassigned key**: issuers first (an O-map verifier treats the key as
unassigned: 403 as before), then verifiers. **Removing an assignment**: verifiers first.

### 5. Sealed mint, service tokens, test tokens

Answer #13, adversarial C-a2, C-a5.

- `collect_claims` returns a sealed, read-only result. `create_access_token(data, claims=sealed)`
  refuses protected keys, provenance and `cpv` in `data` (SANDLOOM-R017 proposed). The issuer
  passed to `collect_claims` is resolved from the module whose contributor is calling, never a
  caller-supplied string; `precomputed` contributions are accepted only for keys assigned to that
  verified issuer.
- Issuer fields (`email`, `email_verified`) are protected at the read: a token without the
  sealed `cpv` has them dropped from the view. Refusing them at the mint broke identity's
  no-core path (trust PoC finding 8).
- `create_service_token` refuses every protected key, the tenant selectors and the subject
  structure, and stamps the current `cpv`, so its absent protected claims read as a definite
  403, never as "refresh".
- The kernel test authority (`sandloom-test`, not a valid module short name) exists only while
  the `sandloom.sdk.testing` plugin or harness is active, and is refused in any other
  composition, not only in production. `as_user` and `mint_test_token` mint through it; a test
  that states `claims_sources` gets exactly that provenance checked (trust PoC finding 7).
- Recorded limit (adversarial C-a6): any module with the core runtime can still call
  `collect_claims` for an arbitrary `user_id` and mint that user's real rights. That is
  impersonation, not a rights raise, and inside the threat model's out-of-scope line (hostile
  in-process code; goals.md "Threat model").

### 6. Contract-name ownership for access-bearing contracts

Adversarial C-4, X3. A contract that carries an `access_check` (decision 8) decides access, so
its in-process provider is a trust decision (I-22). It may be registered only by the module its
first segment names, or by the module the composition root assigns as its provider:
`create_app(access_providers={"tenancy.membership.v1": "acme_orgs"})`. Remotely,
`SERVICE_BINDINGS` is the assignment. Otherwise the load refuses (B034's family, widened). This
is what keeps a third-party implementation able to serve a live check in-process (owner #2).
Other contract names keep the first-registrant rule until a failing scenario shows a need (H-17).
The owner confirms the scope (decision.md, X3).

### 7. Billing claims: option (c)

Owner decisions #3 and #4. The billing claims authority may be non-critical for minting, so an
ordinary login does not break when billing is down. The startup rule of decision 1 accepts a
non-critical authority only when every reader of its keys has a verifiable live fallback or
returns indeterminate: claim-mode facts that escalate to the live check when the authority is in
`claims_sources_failed` (they do today). `Role`, `Perm` and `TenantScope` have no live fallback,
so the authorities of `roles`, `permissions` and `tenant_ids` stay critical.

- An absent billing value never means allow.
- Capability absent from the composition (billing neither loaded nor bound): 501.
- Provider temporarily unavailable: 503.
- A subscription that does not grant access: 402 (payment required) or 403, as the provider's
  access check declares its denial category.
- Money operations keep their freshness and atomicity: the atomic credit spend is never
  replaced by a claim or a read check (answer #24).

## Decision, part (b): portability

### 8. Contract-anchored access checks

A domain atom is declared by its owner on one boolean contract read method:

```python
@contract.read(planned=True)
@access_check("OrgRole",
              bind={"organization_id": From.org(param="org_param", validate_against_token=False),
                    "user_id": From.subject_id()},
              category=Category.FORBIDDEN,
              values={"minimum": ["member", "admin", "owner"]},
              risk={"admin": {"minimum": ["admin", "owner"]}},
              witnesses_membership=("organization_id", "user_id"))
async def has_role_at_least(self, organization_id: UUID, user_id: UUID,
                            minimum: Literal["member", "admin", "owner"]) -> bool: ...
```

The declaration is part of the method's describe document and `contracts.snapshot.json`.
`sandloom generate` writes, next to the consumer's stub (ADR-0013's generated package), one typed
`ContractCheck` subclass per declared method with the declaration as a literal
(`__sandloom_atom__`), and one `ClaimFactCheck` subclass per owner `AccessFact(atom=...)` for
claim-tier atoms. One generic kernel evaluator binds the subject, organization
(`resolve_org_id`) or account (`resolve_account`), calls `resolve_contract(...)` and the one
method with the arguments in signature order. The kernel names no module, contract, method or
role word. A consumer process needs neither the provider's package nor a kernel change. The
route expression is unchanged; only the import changes:

```python
from <app>.shared.contracts.billing_entitlements_v1 import SubscriptionActive
from <app>.shared.contracts.tenancy_membership_v1 import OrgRole

BOUND_ADMIN_AND_SUB = OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())
```

`Credits` moves onto a new boolean `has_credits_at_least` read with the same query and account
binding as `get_credit_balance` (answer #24); it never replaces the atomic spend.

### 9. The equality invariant

A generated atom calls the same provider method with the same arguments in signature order
through the same provider unit as the kernel atom it replaces. For every policy the number of
contract calls, RPC requests, database round trips, the call order and the decision equal today's,
mono and split (H-16, I-23). The authorization stand of the PoC becomes the regression check in
CI (call counts, absence of I/O on the claims tier, order, decisions); instructions are measured
on the cachegrind ladder with its noise band (H-10).

### 10. The literal is the single metadata source

`require()` (tier, `contracts()`, `org_params()`, `risks()`, sensitive effect), the static rules
(AST of the imported name's generated file, no import) and the snapshot read the literal. Lowering
a declared risk is a T004 incompatibility; a hand edit of the literal is a stale T007 file.
Archcheck rules read the literal, not class-name lists (`ORG_ATOMS`, `BILLING_ATOMS`,
`require_org_role`, removing B-N17, B-N18). The rule `handwritten-access-check`
(SANDLOOM-C032 proposed) recognises a hand-written `Requirement` that calls a declared access
check and checks its tier, category, risk and parameters (answer #22, H-20). What it cannot see
(a call through `getattr`, an authority checked in a handler body) stays covered by independent
route classification and runtime checks; a hand-made atom is not safe because the analyser did
not find it.

### 11. `Subscribed` keeps its parameters

Owner decision #5. The generated `Subscribed` keeps `statuses=` and `claim_key=`. The provider's
`AccessFact` declaration lists the allowed statuses (typed as a `Literal` in the generated class)
and the claim keys the fact may read; each such key must be assigned to a trusted authority in the
composition root, or startup refuses. An arbitrary claim name cannot bypass decision 1: it is a
type error at generation and a refusal at startup. Without an installed or bound billing provider,
`Subscribed` answers 501 instead of today's 200 (owner #4: the silent path C-F5 / D2 closed, an
approved behaviour change).

### 12. Incompatible contract shape is an indeterminate, user input stays 422

Owner decision #8. A shape mismatch between the generated atom's literal and the provider (a
required argument the atom does not pass, an argument it passes the provider no longer takes, a
result that is not a boolean) is mapped by the evaluator to indeterminate 501 `contract_mismatch`
inside the algebra: `Or` may still allow on another branch, `Not` never allows. Invalid user
input, raised while binding request values (path, query, header) to the atom's declared parameter
types before any call, stays 422. A required remote access-check contract gets the strict
compatibility preflight of ADR-0013 (expectation beside the stub; `strict` refuses startup).
Fixes the PoC's Z-10 failure (422 `contract_argument_invalid` raised out of an `Or`).

### 13. Tenant kind from the assigned tenant authority

Owner decision #7 (K1). `tenant_kind` keeps its four values: `canonical` under
`TENANCY_MODE=single`, `personal` when the tenant is the caller's user id, `organization` when the
assigned tenant authority's declared membership witness confirms the caller's membership, else
`unknown`. `tenant_ids` alone never proves an organization. The witness is the
`witnesses_membership` declaration of an access check on a contract owned by the authority of
`tenant_ids` (or its assigned provider, decision 6), read from the consumer's generated copy;
never found through import order or a process-global table filled at import (the PoC's form,
rejected). A route policy using `Account.bound_tenant()` adds that contract to the module's
`consumes`, so the copy is generated; with no witness the kind is `unknown`, `bound_tenant()` is
indeterminate, and startup reports the policy (H-19). No hidden RPC: the call is today's
`has_role_at_least(org, user, "member")`. Freshness and membership guarantees are unchanged.
Removing the third call (witness 3 → 2, answer #23) is its own change (KB-37) with proven
equivalence.

### 14. The live adapters: an approved exception, after equivalence

Owner decision #2 (C-1). `register_live_adapter`, `EntitlementsAdapter` and `MembershipAdapter`
are replaced by access checks as an explicitly approved exception to public-API preservation.
They are removed only after:

1. the G-AUTHZ-05 scenarios pass against the new mechanism: a third-party provider answers the
   live checks in-process (decision 6's assignment) and remotely (`SERVICE_BINDINGS`), and a
   consumer process chooses the provider by configuration, with the same decisions as today;
2. the ledger records G-AUTHZ-05's statement re-expressed over access checks (the guarantee is
   kept, the API is removed) with a `removed:` record for the three names;
3. this ADR, the docs and the migration guide are updated.

### 15. What else moves, and what stays

- `Feature` and `WithinLimit` stay public capabilities (answer #21, H-2). They move to billing as
  generated claim or access atoms with their parameters carried locally and over RPC (K3,
  KB-33); the kernel keeps them until that move passes, and removal is never automatic.
- `Roles` and `Permissions` constants (no importer, B-N15) move to identity, the owner of the rows
  they name, under I-17 review.
- `fake_access_fact` keeps generic fact-by-name stubbing; its billing branch goes (B-R6).
  Tests of live atoms use `fake_contract(REF, Fake())`.
- `Account` (`user()`, `org(param=)`, `bound_tenant()`) stays a kernel concept with neutral
  semantics: the target of an access check, not "the billing account" (adversarial C-6; the
  owner confirms, decision.md C4).
- `past_due`: the generated `Subscribed` reads today's fact path, so today's staleness holds
  (status at most the token TTL; a rule change at most `REMOTE_FACT_TTL_SECONDS` in a split
  consumer). Moving the descriptor refresh off the request path (K7) is optional; if built,
  before the first successful fetch claim-mode facts escalate to live or answer 503.
- Declared claim keys (M6, answer #15) are not adopted now; the runtime ownership check stays the
  guarantee.

### 16. Separate changes, each with its own baseline

| id | change | owner answer | PR |
|---|---|---|---|
| K1 | tenant kind from the assigned tenant authority | #7 | KB-31 |
| K5 | provider down → indeterminate 503 `owner_unavailable` inside the algebra; a log line and `access_indeterminate_branch_total` per indeterminate branch; RPC authentication and allowlist failures stay errors | #19 | KB-35 |
| K6 | per-atom `needs_request_session()` instead of `tier > TIER_CLAIM`; the boot probe describes **every** remote binding and seeds the proxy's describe cache | I-14 | KB-36 |
| witness | an allowing check that declares `witnesses_membership` records `organization` in the request's tenant-kind memo (3 → 2 calls) | #23 | KB-37 |

### Mechanisms

| # | mechanism | kind | removes | compared against |
|---|---|---|---|---|
| C-M1 | `claim_authorities` (with `issuer`, `previous=`) and `access_providers` in the composition root, with one startup check family | composition argument; startup check family | T1-T3, T5 (access-bearing), T6; B-S1, B-S2 trust by name | TA-2 (a setting: answer #9); trust by name |
| C-M2 | per-key `cpv` claim | wire-format addition | legacy and skewed tokens (I-08 consistency, I-09) | none cheaper: TA-0 fails T-9, T-10 |
| C-M3 | sealed mint and `create_service_token` | changed public signatures; one runtime code | I-08 minting, T4 for mistakes | trusting `create_access_token` callers (TA-0 fails T-11) |
| C-M4 | `access_check` declaration, generated atom classes, one generic evaluator | declaration field; new kind of generated content; describe extension | A19, B-R1, B-R3, B-N12, B-N13, B-N14 | PA-0 (changes syntax, loses typing), PA-1 (risk and tier left to the consumer), PA-3 (+20 %/+29 % instructions in split, string parameters) |
| C-M5 | `handwritten-access-check` rule | static rule | H-20 gap for hand-written requirements | warnings only (answer #22: not enough) |
| C-M6 | per-deployment kernel revocation applier, issuer obligation | see ADR-0012 A-M3, A-M4 | D1, T8 | |

Removed: `register_live_adapter` and its table (after decision 14), `WELL_KNOWN_FACT_OWNERS`, the
kernel's domain atoms, class-name lists in archcheck. Extended, not new: the merge pass (mint
check), `decode_token` (view), B034 (ownership).

## Alternatives considered

| alternative | why not |
|---|---|
| **TA-0** mint-time refusal and one admission predicate | fails on running code: a real base-wheel token passes `Role`, `Perm`, `require_platform_admin` and the platform API (T-9); a verifier with another map trusts even the platform gate (T-10); a hand-minted `roles: ["admin"]` passes (T-11) |
| **TA-2** the authority map as a deployment setting | answer #9: trust is assigned in code, not settings |
| **TA-3** per-authority signed claim blocks | protects against a hostile issuer, outside the threat model; keys and per-request signatures (H-12) |
| `cpv` as one digest of the whole map | every map change drops every protected key fleet-wide and rolling deploys flap (adversarial C-a1); owner #10 |
| unconditional "verifiers last" | loops on the changed key in either order without a previous-authority window (decision 4) |
| billing critical (option a) | a billing outage refuses every login and refresh |
| billing claims unassigned (option b) | `Subscribed` escalates to live, changing call counts (I-23) |
| a global 401 on old tokens | breaks ordinary authentication and `Or` (answer #11) |
| **PA-0** existing `Entitled` with the smallest extension | changes the policy syntax and loses typing (answer #18, H-16) |
| **PA-1** consumer-owned declarative `Requirement` classes | risk and tier left to the consumer unless the provider declares them (half of PA-2 with a forked vocabulary); the control was invisible to every static rule (Z-8, Z-15) |
| **PA-3** parameterised `AccessFact` with generated wrappers | equal in calls, but +20 % consumer and +29 % provider instructions on the decisive shape in a split, binding to `<owner>.facts.v1` instead of the domain contract, string-keyed parameters, mode chosen by configuration behind a fixed tier (Z-2, Z-6) |
| witness found from a process-global table filled at import (as prototyped) | depends on import order; owner #7 requires the assigned authority |
| keeping the live adapters beside access checks | two mechanisms for one capability; kept only until decision 14's equivalence passes |
| removing `Feature`, `WithinLimit` (no bundled producer) | public capabilities (answer #21, H-2) |
| a domain-only account resolver for tenant kind | the owner chose the assigned tenant authority (owner #7); the resolver would move tenant isolation semantics to a battery |

## Consequences

**Benefits.** An application names who may issue each security claim, and a gate trusts nothing
else: the forged `roles`, the missing grant authority and trust by the name `identity` all close
(T-1..T-13). A buggy optional contributor loses its own contribution instead of every login. Old
and skewed tokens fail closed per key with a refresh-aware 401. A typed domain atom works in a
consumer process without the provider installed, with zero consumer lines instead of 31 and no
advanced names; the static rules see third-party atoms with no class name in a list. The kernel
stops holding billing and tenancy vocabulary.

**Costs.** One composition argument per application (`sandloom new` writes it; `sandloom-standard`
for the shipped image). About 40 hand-minting test call sites migrate to the test kit; 52 tests of
PR #70's 503 rule rewrite to the violator rule. Four beginner imports change (ADR-0010 amendment);
51 code files and 13 doc pages use the kernel atoms. One new public decorator and three advanced
names for providers (`access_check`, `From`, `Category`). Request rung +0.57 % instructions, mint
+0.68 % (whole-map `cpv`; per-key re-measured in KB-11).

**New risks.** A deployment that forgets its map denies every protected claim, or refuses every
mint with a critical issuer: loud, never silent. The previous-authority window trusts the old
authority for one rollout. The status order of decision 3 differs from the adversarial proposal.
A provider can declare no risk from the start; route classification remains the backstop. A
consumer that imports an atom before its first generation cannot generate until KB-20's
pre-composition planner lands (Z-15).

**Migration.** Upgrade steps: write `claim_authorities` (or use `sandloom_standard.main`); clients
refresh on 401 `token_policy_outdated`; tests mint through `as_user` / `mint_test_token`;
deployments bind identity and tenancy claims critical (billing may stay non-critical); imports of
`OrgRole`, `SubscriptionActive`, `Credits`, `Subscribed` change to the generated package; custom
live adapters become an `access_providers` assignment or a binding.

**Conditions to revisit.** A third-party issuer cannot mint through `collect_claims`. The per-key
`cpv` costs more than the +1 % budget where the bench resolves it. An access check needs more than
one boolean method. A real deployment needs a status order other than decision 3's. A failing
scenario shows non-access contracts need name ownership (decision 6).

## Evidence

**Trust** (`poc/trust/results.md`, base `ceb3d66` plus PR #72's `a51a59d`, `971f80a`, candidate
`ta1-kernel.patch`, commit `627c9c9`; real `uvicorn` processes, mono and split over real RPC, EdDSA
tokens, lean wheels without identity, tenancy or billing):

| row | result |
|---|---|
| T-1 replacement issuer assigned, identity not installed | pass mono and split: `ADMIN` 200 on `Role`, `Perm`, both platform gates; `USER` 403 |
| T-2, T-4 optional violator | pass: authorities' values only, `claims_sources_failed: ["forged"]`; 100 logins → 100 × 200, 0 tokens with `admin`, counter 100 |
| T-3 critical violator | pass: 503 naming `forged`, both registration orders |
| T-5 typo, absent, non-critical authority | pass: startup refused with the named reason |
| T-6 no assignment | pass: both writers dropped, every gate 403; critical issuer → every mint 503 |
| T-7, T-8 local and remote spoofing | pass: app-local `identity` gets no rights; `owner=` and contract squat refused (B034) |
| T-9 legacy tokens incl. a real base-wheel token | pass: `/plain` 200, `Role` 401 `token_policy_outdated`, `Role \| AlwaysAllow` 200, `~Role` never allows |
| T-10 issuer and verifier maps differ | pass: `cpv=1:8264c48c7229` vs `1:aa015457855a` → 401 at the verifier |
| T-11 hand mint, service token | pass: `UnsealedClaimsError`; service token 403 on every gate |
| T-12 test authority | pass in a test app; refused outside a test composition |
| T-14 I/O | `Role`, `Perm`, `TenantScope`: DB 0, Redis 1 `GET`, RPC 0, mono and split |
| T-15 cost | request rung +0.57 % Ir (micro rung 152,6k → 159,7k Ir/op), mint +0.68 % Ir, latency within noise |
| T-17 full suite | 6504 passed, 6 failed: `test_the_kernel_names_no_module` by design (ADR-0012) and 5 Redis-environment failures equal on base |
| TA-0 control | fails exactly T-9, T-10, T-11 |

**Portability** (`poc/authz/results.md`, base `ceb3d66`, `patches/kb-poc-authz.patch`, commit
`ccc420e`; consumer venv with only `sandloom` and `sandloom-identity`, providers in another
process):

| row | result |
|---|---|
| Z-1 decisions | 20/20 equal mono, 20/20 split, incl. the five decisive subjects |
| Z-2 counts | every case equal: decisive 3 calls / 9 DB mono, 3 RPC / 9 provider DB / 0 consumer DB split; `Credits` 1 / 4; claims tier 0 / 0 |
| Z-3 order and laws | provider call order equal; 6 algebra laws plus 5 generated-atom laws pass |
| Z-5 metadata | with K6 0 describe `GET`s on a first request |
| Z-6 instructions | decisive ≤ 5,865,621 Ir/req against a budget of 5,885,658 (+0.6 %, inside repeat noise); split rusage +0.1 %; K6 −8.9 %, witness −11.7 %; PA-3 +1.8 % mono, +20 % / +29 % split |
| Z-7 typing | mypy finds 3 errors on the generated atoms where today's atoms give 1 |
| Z-8 archcheck from metadata | third-party atom found by gate order, C027 and the sensitive-route rule; hand-written `Requirement` checked (C032); risk lowering → T004; hand edit → T007 |
| Z-9 fail-closed | absent 501, down 503, never allow under `Not`; with K5 `OrgRole \| SubscriptionActive` allows on the subscription with tenancy down; `Subscribed` with billing nowhere 200 → 501 |
| Z-10 shape mismatch | **failed in the PoC** (422 out of an `Or`): fixed by decision 12 |
| Z-12 kernel boundary | `QuotaAvailable` added with zero framework files edited; the move itself not done in the PoC (KB-34) |
| Z-14 `past_due` | 200/200/200/200 grace, 402/402/402/402 deny, base and candidate, mono and split |
| Z-15 DX | consumer atom lines 31 → 0, advanced names 4 → 0, hand-edited files 7 → 5, framework errors 6 → 1 |
| Z-16 full suite | 6458 passed, 14 gates PASS; split conformance 27 passed |
| adversarial verdict | `adversarial.md` §4, §9 (commit `53701d9`): TA-1 stands once X1 is decided; PA-2 leading with C-1..C-8, each answered above |

## Fitness tests

| test | enforces | exists / new |
|---|---|---|
| trust rows T-1..T-14 as integration tests on the replaceability fixtures, mono and split | decisions 1-5; I-08, I-09, I-10, I-22 | new (KB-10, KB-11, KB-12), from `poc/trust/run.py` |
| issuer × verifier matrix (adoption, key change with and without `previous=`, rollback) in split conformance | decision 4; owner #10 | new (KB-11) |
| owner regression set: forged `Role("admin")` deny; platform admin without trusted authority deny; `Role("admin") \| Entitled("missing.fact")` allow; `~Entitled("missing.fact")` never allow; same decisions mono and split | decisions 2, 3; tm reflection on `kb-alt-authz` | new (KB-11) |
| algebra laws over generated atoms and over the 401 status | H-4, decision 3 | extends `tests/laws/test_authz_algebra_laws.py` |
| authorization stand in CI: counts, order, decisions per policy, base vs candidate | decision 9; I-23, H-16 | new (KB-30), from `poc/authz/stand.py` |
| cachegrind rungs for request, mint and the decisive shape | H-10 | extends `benchmarks/ladder` |
| shape mismatch → 501 inside `Or`/`Not`; user input → 422 | decision 12 | new (KB-30) |
| `Subscribed` without provider → 501; `statuses=`/`claim_key=` outside the declaration refused | decision 11 | new (KB-30) |
| non-critical authority accepted only for keys with a live fallback; billing down → login 200, `Subscribed` live or 503 | decision 7 | new (KB-10) |
| `bound_tenant()` with and without the authority's witness; four kinds | decision 13 | new (KB-31) |
| G-AUTHZ-05 equivalence scenarios before the adapters are removed | decision 14 | new (KB-34) |
| `handwritten-access-check` on the Z-8 fixture | decision 10; H-20 | new (KB-32) |
| typing test of the generated atoms | ADR-0010 amendment | new (KB-30), from `poc/authz` Z-7 |
