---
search:
  exclude: true
---

# ADR-0014: Claims trust assigned by the application; domain authorization atoms generated from contracts

- **Status:** Accepted (2026-11-08, by the owner, after the Codex reviews of `c5efeac` and
  `787ca60`; tm `kb-adrs-accept`). The staged initial `cpv` enablement
  (decision 4) is accepted on its conditions: the `legacy_tokens` compatibility mode is an
  explicitly accepted temporary security risk (guarantee ledger G-RISK-14), bounded to the
  transition, one access-token lifetime and the clock-skew allowance, never extended without a
  separate operator decision; 503 `claims_policy_mismatch` (decision 3) is accepted.
  Amended 2026-11-09 by the owner (KB-11; tm reflection `01a1bdeb` on `kb-11-trusted-view`):
  the `cpv` identity of an authority is its import path, local or remote (see
  [the amendment](#amendment-2026-11-09-the-cpv-identity-of-an-authority)).
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
- **Research and evidence:** `design/kernel-boundary/_archive/research-trust.md`,
  `_archive/research-authz-portability.md`, `_archive/alternatives-c-authz.md`, `adversarial.md` §4,
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
  (ADR-0004), whose answer names the assigned import path (the authority binding, amendment
  2026-11-09). A module naming itself `identity` is not proof.
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
the policy marker in the cache key. A protected key K is in the view only when **one condition**
holds for a single authority A of K, the same A in all three parts:

1. **allowed:** A is K's current authority in the verifier's map, or the one `previous=`
   authority the map declares for K (decision 4);
2. **policy version:** the token's `cpv` entry for K equals the digest of (K, A's canonical
   identity): the full import path `claim_authorities` assigns, whether A is loaded locally or
   reached over RPC (amendment 2026-11-09). A `previous=` authority is declared with that
   identity in full, never by short name, and need not be loaded or bound in the verifying
   process;
3. **verified source:** A's short name is in the token's `claims_sources` and not in
   `claims_sources_failed`: A ran at mint and its result was kept. The issuer's mint check
   (decision 2) accepted K only from the step of the authority its own map assigned, and part 2
   proves which authority that map named.

A token whose parts name different authorities does not trust K: the `cpv` digest of the
previous authority while only the current one is in `claims_sources`, the reverse, or an
app-local module named like A whose import path gives another digest. A source name alone or a
digest alone never trusts a key. The one exception is a token without `cpv` while a verifier
runs the bounded compatibility mode of the initial enablement (decision 4), and it is checked
against parts 1 and 3. Every reader sees only the view: `UserContext`, `Role`,
`Perm`, `TenantScope`, the tenant allowlist that validates `Account.org`, claim-mode facts
(trusted by the authority of their claim key and by the owner check), the idempotency
fingerprint, and **one platform admission** used by both `require_platform_admin` and
`platform_operator`, trusting the platform role `admin` only when `roles` is in the view. `UserContext.untrusted_claims` lists `{key: reason}`.

- An **unassigned** protected key, or a current authority absent from the composition, is a
  definite deny (403); a `previous=` authority need not be present; `TenantScope` answers 400/403 as today.
- A key untrusted because the token predates the policy (no `cpv`, or another digest for that
  key on a token issued before the verifier's map took effect; see "One controlled refresh")
  raises `PolicyIndeterminate(status=401, code="token_policy_outdated")` inside the
  algebra: `A | B` still allows on `B`, `~` never allows. `TenantScope` and the platform
  admission map it to 401 `token_policy_outdated`. Old tokens keep working on routes that need
  no protected claim until they expire (answer #11). The first foreign `cpv` value per key is
  logged once.
- **Status of an indeterminate decision**, when several branches are indeterminate: 401
  `token_policy_outdated` (a refresh can make that branch allow), then 503 `owner_unavailable`
  (a retry later can), then 501 (capability absent, `contract_mismatch`: neither fixes it). The
  adversarial review proposed 503 first (C-a4); this ADR orders by what the client can fix
  without an operator. Law tests cover the new status.
- **One controlled refresh** (owner's confirmation C7, with its condition). 401 is used for
  `token_policy_outdated` only, never for another indeterminate, and only where a refresh can fix
  it: the token has no `cpv` (outside the compatibility mode), or names for K a digest the
  verifier does not accept and was issued before the verifier's map took effect. A token issued
  after that and still refused means issuer and verifier disagree: it answers 503
  `claims_policy_mismatch` and logs ERROR, because a refresh would loop. A client refreshes once
  per 401 `token_policy_outdated` and does not retry a second one; the test kit's client and the
  docs state it, and the key-change and enablement tests count refreshes.

### 4. `cpv` per key, with an issuer × verifier rollout and rollback matrix

Owner decision #10. The token carries
`cpv = {"v": 1, "k": {<key>: <digest of (key, authority identity)>}}`, about 10 bytes per
assigned key, stamped by the merge and by `create_service_token`. Changing one key's authority
changes only that key's digest; trust in the other keys is untouched. A verifier compares per
key and fails closed on any digest it does not accept.

A key's entry may name one other authority the verifier accepts during a rollout, declared with
its verified identity: `claim_authorities={"roles": Authority("acme_identity",
previous=Authority("identity", module="sandloom_identity"))}` (a remote one by its import path
too, amendment 2026-11-09). A verifier then trusts the key under decision 3's one condition with either
authority, the same authority in every part; an issuer always stamps the current one. The entry
exists only for one rollout and is removed by the next deploy. It is a composition argument, not
a setting. With the enablement's `legacy_tokens` below, it is one of the two pieces of rollout
state this decision adds, both temporary (adversarial C-a1).

**Initial enablement** (old code O stamps and checks nothing; new code N always stamps `cpv` as
an issuer and checks it as a verifier). Issuers first cannot be ordered wherever one service is
both issuer and verifier (a monolith, identity's own service): during the rolling deploy an N
replica verifies an O replica's tokens, and a refresh can land on an O replica again, a loop.
The enablement is therefore staged, with a compatibility mode that only the verifier side has:

```python
create_app(claim_authorities=..., legacy_tokens=LegacyClaimsTokens(until="2026-12-01T00:00Z"))
```

`until` is the end of the transition: the time by which the last O issuer has stopped. The
window during which a token without `cpv` can give rights ends at
`until + ACCESS_TOKEN_EXPIRE_MINUTES + JWT_LEEWAY_SECONDS` (the window end), and never later.

| stage | issuers | verifiers | token without `cpv` | token with `cpv` |
|---|---|---|---|---|
| 0 | O | O | today | — |
| 1 verifiers learn | O | N, compatibility mode | trusted per key by parts 1 and 3 of decision 3 (an allowed authority ran and was kept), counted | — (none minted yet) |
| 2 issuers switch | O → N | N, compatibility mode | as stage 1 | the full condition of decision 3 |
| 3 enforce | N | N, no `legacy_tokens` | not trusted: 401 `token_policy_outdated` only where a policy needs the key and no branch allows; one refresh at an N issuer returns a token with `cpv` | the full condition |

In a monolith stages 1 and 2 are one deploy (the N release learns and stamps); stage 3 is a later
deploy that removes the argument. Stage 3 starts only when no O issuer remains and at least one
access-token lifetime (`ACCESS_TOKEN_EXPIRE_MINUTES`) has passed since the last O issuer
stopped, so a refresh after the 401 always reaches an N issuer. Rollback: stage 3 → 2 by
redeploying the verifiers with `legacy_tokens`; stage 2 → 1 by returning issuers to O (the
compatibility-mode verifiers accept both); stage 1 → 0 by returning verifiers to O. An O issuer
behind a verifier without `legacy_tokens` is never a supported state.

The compatibility mode is an **explicitly accepted temporary security risk** (owner, 2026-11-08,
after the Codex review of `787ca60`), recorded in the guarantee ledger as G-RISK-14: a bounded
window of today's trust for tokens without `cpv`, with these protections:

- **bounded to the actual transition, one token lifetime and the clock-skew allowance:** `until`
  is required, an absolute UTC time, set to the end of the actual transition (the last O issuer
  stopped), never to a later date chosen for convenience. A token without `cpv` is accepted only
  if its `iat` is before `until` and its `exp - iat` does not exceed `ACCESS_TOKEN_EXPIRE_MINUTES`,
  with expiry checked under `JWT_LEEWAY_SECONDS` as for every token; so no token without `cpv`
  gives rights after the window end. From the window end on, the mode is off by itself (fail
  closed) whether or not the argument is still written, and startup logs a WARNING asking to
  remove it;
- **no extension without a separate operator decision:** `until` has no default, no setting and
  no environment variable, and the framework never derives, moves or extends it. A later `until`
  is a code change in the composition root that an operator decides on its own, as a new
  acceptance of the risk, with the reason recorded alongside the deployment; the planned
  migration never includes one;
- **narrower than today:** parts 1 and 3 of decision 3 still apply, so a token whose `roles`
  authority did not run, or failed, gives no rights. What the window keeps open is what O tokens
  carry today: no per-key policy version (T-10) and, for a token an O issuer minted while a
  foreign writer filled a key its authority omitted, that value (gate probe G2-G4). Hand-minted
  tokens (T-11) stay within the signing-key line of the threat model;
- **visible:** `/diagnostics/core` and `sandloom doctor` show the mode and the time left;
  `claims_policy_legacy_tokens_total{key}` counts every acceptance; the first per key is logged;
- **one mode per deployment:** the deploy-profile check (T014) and the split conformance suite
  refuse a profile whose services disagree on `legacy_tokens`;
- **never by default:** `sandloom new` and `sandloom_standard.main` do not set it; it is a
  composition argument written for one upgrade, like `previous=`, not a setting.

The deploy-profile check (T014) and the split conformance suite require every service of a
profile at or above the adoption release before stage 3.

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
unconditional "verifiers last" rule is not adopted. Where one service is issuer and verifier,
step 1 is a deploy of map M1 whose `previous=` names M2's authority (every replica accepts the M2
digest before any replica stamps it), step 2 a deploy of M2 with `previous=` naming M1's, step 3
drops it; during each rolling deploy every replica accepts both digests, so no refresh loops.

**Test of the key change: identity → acme_identity** (KB-11, split conformance and mono). Roles:
`identity` (`sandloom_identity`) is the authority of `roles`, `permissions` and the issuer under
M1; `acme_identity` (a replacement issuer outside `src/`, from the replaceability fixtures) under
M2. Tokens: T1, minted by a real M1 issuer built from the previous release's wheels before step 0
ends and replayed unchanged at every step; T2, minted by the M2 issuer at step 2. At every step and
rollback step, a verifier process (mono and split) evaluates `Role("admin")`, `Perm(...)`,
`require_platform_admin`, `platform_operator` and `TenantScope` for both tokens:

| step | maps (issuers / verifiers) | T1 | T2 |
|---|---|---|---|
| 0 | M1 / M1 | trusted | — |
| 1 | M1 / M2 + `previous=` identity | trusted (A = previous) | — |
| 2 | M2 / M2 + `previous=` identity | trusted (A = previous) | trusted (A = current) |
| 3 | M2 / M2 | 401 `token_policy_outdated`; one refresh yields a T2-like token | trusted |
| rollback a | M2 / M1 + `previous=` acme_identity | trusted | trusted (A = previous) |
| rollback b | M1 / M1 + `previous=` acme_identity | trusted | trusted |
| rollback c | M1 / M1 | trusted | 401; one refresh yields a T1-like token |

Negative rows at step 2: a token with the identity digest and only `acme_identity` in
`claims_sources`, and the reverse, are untrusted; an app-local module named `identity` with
another import path gives another digest and is untrusted; `identity` in
`claims_sources_failed` is untrusted. Every client refreshes at most once per step change, and
no step answers 401 to a token that a refresh would not fix.

**Test of the initial enablement** (KB-11): stages 0 → 3 and the rollback 3 → 0 with a real token
from the previous release (no `cpv`) and tokens minted at each stage, mono (one rolling deploy of
three replicas, O and N replicas mixed) and split: no 401 in stages 1 and 2; the legacy token
without its authority in `claims_sources` gives no rights in stage 1; at stage 3 one 401 and one
refresh per client; a token without `cpv` issued after `until` untrusted; `exp - iat` above the
lifetime untrusted; T014 refuses mixed `legacy_tokens` in one profile.

**Test of the window end** (KB-11, mono and split, `legacy_tokens` still written in the
composition, the clock controlled). Tokens without `cpv`, signed with the deployment's key, whose
`roles` hold `["admin"]` and whose `tenant_ids` name a tenant the user has no grant for, written
by a non-authority contributor while the authority ran and omitted the key (the gate probe G2-G4
shape), and the same claims with the authority absent from `claims_sources`: (a) inside the
window, the first token's values are what the accepted risk keeps open (counted and logged, as
today), while the source check gives the second token no rights; (b) a token with `iat` at or
after `until`, and one with `exp - iat` above `ACCESS_TOKEN_EXPIRE_MINUTES`, give no rights at
any time; (c) at and after the window end no token without `cpv` gives rights:
`Role("admin")`, `Perm(...)`, `require_platform_admin`, `platform_operator` and `TenantScope` on
the forged tenant each refuse (401 `token_policy_outdated` only where a refresh fixes it, else
403), `Role("admin") | AlwaysAllow` still allows, `~Role("admin")` never allows, and
diagnostics show the mode as off.

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

The owner confirms this scope with one condition (decision.md, X3): **the provider is checked by
its real identity**, as an authority is in decision 1. In-process, the registering module's short
name and its loaded import path both match the assignment (an app-local module named
`acme_orgs`, or one named after the first segment from another path, is refused); remotely, the
binding's pinned `service_id`, pair-MAC authenticated. A first-segment match counts only for the
module actually loaded under that name from its own distribution or the app's declared module
path. Before access checks exist (KB-13), which contracts are access-bearing is read from the
consumer side without a domain constant in the kernel (migration plan, KB-13).

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
              witnesses_membership={"minimum": "member"})
async def has_role_at_least(self, organization_id: UUID, user_id: UUID,
                            minimum: Literal["member", "admin", "owner"]) -> bool: ...
```

`witnesses_membership` names no parameter. The bound `From.org()` and `From.subject_id()` are
the organization and the caller. The mapping gives each of the method's own parameters the
value that asks about membership alone (decision 13).

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
`consumes`, so the copy is generated. The entry is optional, lists only the witness method and
is derived at boot once the witness is resolved. The declaration is in the generated atom
literal and the contract snapshot. Removing it or changing its values is incompatible
(SANDLOOM-T004). With no witness the kind is `unknown`, `bound_tenant()` is
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
  KB-33 chose claim atoms: billing declares `Feature` and `WithinLimit` with `claim_atom` above
  its access checks `has_feature(tenant_type, tenant_id, key)` and
  `is_within_limit(tenant_type, tenant_id, key, used)`. For `Account.user()` the generated atom
  reads the `features` / `limits` claim with zero I/O, as the kernel atom does, with the same
  indeterminates. For an organization account it asks that check live, with its parameters as
  contract arguments, so they reach billing over internal RPC too. Billing's plans declare no
  features or limits yet, so both checks answer `False`. Giving billing a plan entitlements
  source is the owner's decision before KB-34.
- `Roles` and `Permissions` constants (no importer, B-N15) move to identity, the owner of the rows
  they name. The owner confirms (decision.md, A6) on the condition of an I-17 review in KB-34: the
  public API baseline and beginner tier diff, the upgrade step naming the new import, no alias
  left in the kernel (H-9).
- `fake_access_fact` keeps generic fact-by-name stubbing; its billing branch goes (B-R6).
  Tests of live atoms use `fake_contract(REF, Fake())`.
- `Account` (`user()`, `org(param=)`, `bound_tenant()`) stays a kernel concept with neutral
  semantics: the target of an access check, not "the billing account" (adversarial C-6). The
  owner confirms it with a condition (decision.md, C4): neutrality is proven, not stated. A
  third-party access check (`acme.seats.v1`'s `has_seat`, from the replaceability fixtures)
  targets `Account.org(param=)` and `Account.bound_tenant()` with billing absent from the
  composition and from the environment, mono and split, with the same decisions and counts as the
  billing check on the same targets; `Account`'s code, docstrings and error texts name no billing
  term (the kernel-edge check covers the literals).
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
| C-M2 | per-key `cpv` claim; `previous=` and the bounded `legacy_tokens` compatibility mode as temporary rollout state | wire-format addition; two temporary composition arguments | legacy and skewed tokens (I-08 consistency, I-09); the loop of an issuer-first enablement where one service issues and verifies | none cheaper: TA-0 fails T-9, T-10; issuer-first enablement loops in a monolith's rolling deploy |
| C-M3 | sealed mint and `create_service_token` | changed public signatures; one runtime code | I-08 minting, T4 for mistakes | trusting `create_access_token` callers (TA-0 fails T-11) |
| C-M4 | `access_check` declaration, generated atom classes, one generic evaluator | declaration field; new kind of generated content; describe extension | A19, B-R1, B-R3, B-N12, B-N13, B-N14 | PA-0 (changes syntax, loses typing), PA-1 (risk and tier left to the consumer), PA-3 (+20 %/+29 % instructions in split, string parameters) |
| C-M5 | `handwritten-access-check` rule | static rule | H-20 gap for hand-written requirements | warnings only (answer #22: not enough) |
| C-M6 | per-deployment kernel revocation applier, issuer obligation | see ADR-0012 A-M3, A-M4 | D1, T8 | |

Removed: `register_live_adapter` and its table (after decision 14), `WELL_KNOWN_FACT_OWNERS`, the
kernel's domain atoms, class-name lists in archcheck. Extended, not new: the merge pass (mint
check), `decode_token` (view), B034 (ownership).

### Amendment 2026-11-09: the `cpv` identity of an authority

Owner decision (tm reflection `01a1bdeb` on `kb-11-trusted-view`), replacing "the pinned
`service_id` of a remote authority" in decisions 3 and 4:

> The cpv fingerprint of a protected key is computed from its name and the canonical identity of
> the authority assigned in `claim_authorities` (the full import path). This identity does not
> depend on whether the module is loaded locally or reached over RPC. For a remote authority the
> pinned `service_id` and RPC authentication are checked independently and do not replace the
> authority identity in cpv.

The import path is the identity of the business module trusted with the claim; the
`service_id` is the identity of the remote service allowed to provide it over authenticated
RPC; `cpv` fingerprints the authority assignment, not the deployment shape. A digest of the
`service_id` would differ between an issuer reaching the authority over RPC and a verifier that
runs it, so mono and split would disagree on one map.

- **Two independent checks for a remote authority, at the mint.** The pinned `service_id` and
  the pair MAC prove which service answered (ADR-0004; a call pinned to one service and answered
  by another is refused). The **authority binding** proves which module that service runs under
  the short name: every `<m>.claims.v1` answer names the import path of the module registered
  under `<m>` (`sandloom.authority`, written by the kernel, never kept in a token), and the
  issuer refuses an answer that names another path, or none, for an owner its map assigns a
  protected key (the step fails; a critical authority refuses the mint, 503). A provider older
  than this check names none: upgrade every `<m>.claims.v1` provider before the issuer. The
  path is never taken as proof on its own: the pin stays mandatory (KB-10).
- **Moving an authority between services** (a new `service_id`, the same import path) no
  longer changes `cpv`: tokens stay trusted, which is right when the same authority moves.
- **A real change of security authority behind the same import path** (a new implementation
  shipped under the old path) is not invalidated by `cpv` either. Today's invalidation paths:
  assign the new authority under a new import path (a key change with `previous=`, one refresh
  per client), or end every session (a JWT key rotation without the old key,
  `sandloom-key-rotation`). **Recorded gap ("authority revision"):** a revision of the
  assignment that changes the key's digest without a new import path, written in the
  composition root like `previous=` (no new registry), is not built.

Tests (`src/sandloom/core/tests/test_claims_cpv_identity.py`): mono and split stamp and accept
the same `cpv` for one assignment; a service other than the pinned one is refused while running
the assigned module; another module behind the assigned short name on the pinned service is
refused by the binding; the same-path rule; a `previous=` rollout reads alike with the new
authority local, remote or absent.

### Amendment 2026-11-09: W2+W3 review fixes (kb-rf-trust)

- **The mint issuer binding (decision 5).** `collect_claims(precomputed=...)` and
  `collect_claims(issuer_claims=...)` hand the mint values no contributor computed in that pass,
  so they are the token issuer's only: the module that registered the `issuer=` contributor must
  be the one registered under the map's issuer short name **from the map's issuer import path**
  (`claim_authorities["issuer"]`). Another authority (which could otherwise precompute its own
  keys, e.g. a tenant grant its contributor never read), a module merely named like the issuer,
  or a process whose map names no issuer here is refused before anything runs (SANDLOOM-R017).
  Two dictionary reads, no call. Tests: `test_sealed_mint.py` (the issuer binding). Superseded
  by the next amendment: both inputs are removed, the binding now guards `issuer_session`.
- **Recorded limit: code holding the issuer's contributor.** Closed by the next amendment
  (kb-sec2-issuer): no entry takes a claim value any more, so holding the contributor no longer
  lets code choose the claims it mints.

- **Claim atoms and the view (decision 3).** `Subscribed`, `Feature` and `WithinLimit` answer the
  view's indeterminate for their key (`token_policy_outdated` 401, `claims_policy_mismatch` 503)
  as `Role` and `Perm` do, never a 402/403 for the token's life that `~` would invert. "The
  authority failed at the mint" (decision 7) is read from the verifier's map
  (`verified_owners`, and the `previous=` owner), so a split verifier running no plans authority
  answers 503, not a deny.
- **`previous=` names its module.** An installed `previous=` module must carry the declared name
  (its entry-point name); one no longer installed keeps the declared name, which cannot be read
  back. The verifier's cache marker covers the owner names it reads provenance under.
- **The window end at the gates** (`test_claims_window_end.py`): real signed tokens without `cpv`
  in the G2-G4 shape, the view's clock at and after `until + lifetime + leeway` while PyJWT keeps
  the real clock (so the view, not expiry, refuses): every gate 401 `token_policy_outdated`,
  `Role | AlwaysAllow` 200, `~Role` never allows, diagnostics `ended`. It found `TenantScope`'s
  per-token `tenant_ids` parse memo answering a grant the view had since dropped; an absent grant
  now reads as none whatever the memo holds.
- **T-9's previous-release token is simulated, not built from wheels.** The kernel suite mints it
  with the current merge and drops `cpv` (`test_claims_split_matrix.py`, `old=True`): the release
  before KB-11 differs from it on the wire by `cpv` alone (KB-11 added `cpv`; KB-12 sealed the
  mint's inputs, not its output), and the token is signed by the same `create_access_token`. A
  token built from the previous release's wheels needs that release's source tree, a wheel build
  and a venv per run, and expires within `ACCESS_TOKEN_EXPIRE_MINUTES`, so it cannot be checked
  in (a long-lived one is refused by the view on its lifetime, another case). The trust PoC ran
  it once (`poc/trust/out/t9-base-token.txt`, L0). **Gap:** an install-matrix lane that builds the
  previous release's wheel, mints with it and replays the token against the current verifier.

### Amendment 2026-11-09: no claim value from the caller (kb-sec2-issuer)

Closes the last mint input of decision 5 by removing it; no public name is added.

- **`collect_claims(user_id, issuer_session, *, issuer=)` takes no claim value.**
  `precomputed=` and `issuer_claims=` are removed from `CoreFacade.collect_claims` and the
  `CoreRuntime` view, without an alias (passing one is a `TypeError`). Every claim of a sealed
  result is what a registered contributor returned in that pass, checked against its
  authority. `precomputed` had no production caller since the refresh reads its claims after
  the issuance check; identity's `TokenService.create_tokens(identity_claims=...)` is removed
  with it.
- **The issuer's fields come from the issuer's contributor.** Identity's contributor reads
  `email` and `email_verified` (`is_verified is True`) as two scalar subqueries in its one
  statement, in the issuer's session (pending rows are flushed and seen); a missing user still
  answers one row. No statement is added (a refresh stays at six). Another module writing them
  is a violator. The issuer's contributor must be critical: the issuer fields have no other
  writer and no fallback, so an issuer that registers none, or registers it
  `critical=False`, is refused at startup (SANDLOOM-B035, kb-proto-b035). A token is never
  minted without them.
- **The issuer session is the map's issuer's.** The binding of the previous amendment moves to
  `issuer_session`: only the module registered from `claim_authorities["issuer"]` runs its
  contributors in a caller's session (its uncommitted rows, e.g. roles assigned during
  registration); another module, or a process whose map names no issuer here, is refused
  (SANDLOOM-R017). Two dictionary reads, no call.
- **The facade behind the runtime is a kernel handle.** `_facade` (the full facade behind
  `CoreRuntime`) and `claims_contributors` (every module's registered contributor, so another
  module's issuer) join the `kernel-handles` rule (`sandloom check`, by attribute and by name).
- **Recorded limit, narrowed.** What is left needs deliberate forgery or write access to the
  issuer's tables: code that reaches the issuer's contributor (by importing its package, refused
  by the isolation gate, or through kernel private state, refused by `kernel-handles`) and passes
  a forged duck-typed session, or uncommitted rows in the issuer's own tables, still yields
  those rows' claims. That is the bar of reading the JWT signing key: hostile in-process code,
  outside the threat model's line (C-a6), which Python cannot enforce. Refusing a session not
  bound to the issuer database's engine is not done: the provider's engines are its own
  (a request pool and a dedicated contract pool), and a forged session reports whatever bind it
  is given, so the check would stop no forgery the line above leaves open.
- Tests: `test_sealed_mint.py` (no claim value, the session binding, the issuer's fields from
  its contributor, another module writing them), `test_claims_trust.py` (the startup check of
  the issuer's contributor), `test_claims_read_unit.py`, identity's
  `test_token_claims_and_revocation.py` and `test_refresh_identity_claims.py` (the fields in the
  contributor's statement, six refresh statements), `test_no_kernel_handle_access.py`.

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
authority for one rollout. The enablement's compatibility mode keeps today's trust of tokens
without `cpv` (narrowed by the source check): an accepted temporary security risk (G-RISK-14),
bounded to the transition, one access-token lifetime and `JWT_LEEWAY_SECONDS`, ended by itself,
never extended without a separate operator decision. The status order of decision 3 differs from the adversarial proposal.
A provider can declare no risk from the start; route classification remains the backstop. A
consumer that imports an atom before its first generation cannot generate until KB-20's
pre-composition planner lands (Z-15).

**Migration.** Upgrade steps: write `claim_authorities` (or use `sandloom_standard.main`); enable
in decision 4's stages (`legacy_tokens` for the first deploy, removed one token lifetime after the
last old issuer stopped); clients refresh once on 401 `token_policy_outdated`; tests mint through `as_user` / `mint_test_token`;
deployments bind identity and tenancy claims critical (billing may stay non-critical); imports of
`OrgRole`, `SubscriptionActive`, `Credits`, `Subscribed` change to the generated package; custom
live adapters become an `access_providers` assignment or a binding.

**Conditions to revisit.** A third-party issuer cannot mint through `collect_claims`. The per-key
`cpv` costs more than the +1 % budget where the bench resolves it. An access check needs more than
one boolean method. A real deployment needs a status order other than decision 3's. A failing
scenario shows non-access contracts need name ownership (decision 6).

## Evidence

The PoCs ran on the bases named below. The migration PRs measure against the merged baseline
`c9b9b05`: `main` with PRs #71-#74 merged into `arch/kernel-boundary` (tm `kb-adrs-revise`);
the merge changed registration and boot (PRs #71, #72: refusals at registration, owner checks),
packaging (#73) and log redaction (#74), and added one owner comparison when a remote facts
descriptor is read (#72); it added no call, query or RPC on a request path. The PoC numbers stay
the reference for decisions; each PR re-measures base against candidate on `c9b9b05` or later
(M-boot in particular).

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
| issuer × verifier matrix (key change with and without `previous=`, rollback) in split conformance | decision 4; owner #10 | new (KB-11) |
| identity → acme_identity key change with a real previous-release token through steps 0-3 and rollback a-c, plus the negative rows (mixed authorities in `cpv` and `claims_sources`, app-local `identity`, failed source), mono and split | decisions 3, 4; owner's confirmation of `previous=` | new (KB-11) |
| initial enablement stages 0-3 and rollback, mono rolling deploy with mixed replicas and split, `until` and lifetime bounds, T014 mode parity | decision 4 | new (KB-11) |
| window end: tokens without `cpv` carrying forged `roles` / `tenant_ids` give no rights at or after `until + ACCESS_TOKEN_EXPIRE_MINUTES + JWT_LEEWAY_SECONDS`, nor with `iat` at or after `until`, with `legacy_tokens` still written, mono and split | decision 4; G-RISK-14 | new (KB-11) |
| controlled refresh: 401 only for `token_policy_outdated` before the map took effect, 503 `claims_policy_mismatch` after, at most one refresh per client | decision 3; C7 | new (KB-11) |
| access provider by real identity: app-local `acme_orgs` and a same-named module from another path refused, pinned `service_id` remotely | decision 6; X3 | new (KB-13) |
| `Account` neutrality: third-party `has_seat` on `Account.org(param=)` and `bound_tenant()` without billing, decisions and counts equal to the billing check | decision 15; C4 | new (KB-30) |
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
