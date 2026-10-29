# ADR-0003: Fewer synchronous RPC calls in split deployments — membership reads first

- **Status:** Proposed
- **Date:** 2026-10-11, revised 2026-10-12 (state after #47–#67; operation
  classes; billing read/financial split; shadow measurement; gaps G1–G3
  addressed by #69: billing operation intents, cross-service commit
  re-check — see "Guarantees, precisely")
- **Deciders:** Sandloom maintainers (owner decisions under "Decisions so far";
  status stays Proposed until the open items are closed)
- **Supersedes:** —

## Context

In a split deployment every organization-scoped request that a non-tenancy
service serves pays at least one synchronous internal RPC to
`tenancy.membership.v1`:

| Caller | Call | When |
| --- | --- | --- |
| `billing/services/tenant_resolver.py` `resolve_tenant` | `get_role(org_id, user_id)` (+ `has_role_at_least` only for an unknown role) | every billing route with `org_id` |
| same | `list_user_organization_ids(user_id)` | every billing route **without** `org_id` (personal vs single-org vs 422) |
| `billing/access.py` fact `organization.subscription.active` | `get_role` | every `Entitled("organization.subscription.active")` gate |
| `sandloom.shared.authz` `OrgRole(minimum)` atom | `get_role` / `has_role_at_least` via the live adapter | every route gated with `require(OrgRole(...))` |
| `examples/helpdesk` tickets | membership contract | per request |

### Cost today (main `1898eaa`)

| Quantity | Then (#40/#42, httpx) | Now | Source |
| --- | --- | --- | --- |
| one membership RPC, total CPU (caller + provider) | ≈ 1.4 ms | **≈ 0.36 ms** (aiohttp, conc 64) | #62, `benchmarks/ladder/RESULTS.md` "RPC transport A/B" |
| — caller side of it | ≈ 0.83 ms | **≈ 0.115 ms** | #62 |
| — provider side (`C_call` share + `W_provider`) | — | ≈ 0.25 ms (planned read, optimized executor) | #60/#62 |
| split premium, login-free mix, at saturation | +22 % (with login) | **+21 % mixed, +24 % read-heavy, +11 % write-heavy** (only +2…3.5 % in instructions) | #65 |

The saving of avoiding one membership call stays

```text
saving_per_call = C_call + W_provider − C_replacement      (≤ ≈ 0.36 ms today)
```

- `C_call` — call infrastructure (proxy, codec, signed context, HTTP,
  response binding, provider fast path) without the provider's work;
- `W_provider` — the planned membership read;
- `C_replacement` — what an option costs instead (dict lookup, local
  `SELECT`, claim parse, nothing).

After #62 the whole call is ≈ 0.36 ms, so the **absolute** prize of any
option is at most `calls_per_op × hit × 0.36 ms`, and the remaining split
premium is mostly per-request overhead that is not membership RPC (the
instruction counts barely move; #65). Whether a cross-request cache is worth
its moving parts is therefore an **empirical** question: it depends on the
hit rate on the real mix, which nobody has measured. This revision prepares
that measurement (a shadow counter, "Then" below) instead of deciding it.

### Facts every option depends on (verified in code after #47–#67)

1. **Membership is not in the token beyond `tenant_ids`.** `tenancy.claims.v1`
   contributes only `tenant_ids`; roles are not claims (decided). Every org
   role gate reads the role live.
2. **Every membership change revokes, immediately (#47).** `remove_member` /
   leave, `change_role`, both parties of `transfer_ownership` and
   organization deletion call `revoke_user_tokens_on_commit(...,
   require_immediate=True)`: the mark is written **before** commit, or the
   change is refused with `503` (`RevocationUnavailableError`) and rolls
   back — a committed change always has its mark, so the first request
   checked after the commit is `401` in every process. Identity uses the same
   path for password change/reset, role revocation, deactivation and
   `block_user`.
3. **Corrupt marks fail closed (#47).** An unparsable mark rejects every
   token of the subject; the issuer's live check repairs it by
   compare-and-set to `now + skew` (never lowers a mark).
4. **The durable revocation path cannot lose an event (#47).** The applier of
   `core.auth.tokens_revoked.v1` is registered without a DLQ
   (`dead_letter=False`): retried forever with capped backoff; while one is
   stuck `/ready` answers `503 degraded` (`revocations_pending`) and
   `auth_revocation_pending_seconds` reports its age. Only a malformed payload
   is dead-lettered (CRITICAL).
5. **Member events are versioned with a rolling-upgrade rule (#45/#47).**
   `tenancy.member_added/removed/role_changed.v1` carry a per-(org, user)
   `version` (monotonic across removal and re-add); consumers apply with a
   version guard and **refresh** via `get_member_state` on a version-less
   event (an instance from before the upgrade).
6. **Contract commands are deduped durably (#50)**, atomically with the
   command's own UoW (`contract_command_log`), fail-closed when the store is
   down — exactly-once *effect* of `billing.credits.v1.spend` per key.
7. **Internal RPC authenticates the calling service (#57)**:
   each call carries the caller's own identity (introduced as per-call
   Ed25519 signatures; since ADR-0004 the only scheme is the pair MAC, with
   commands also Ed25519-signed) and the provider authorizes per contract/method
   (`RPC_ALLOWED_CALLS`, deny by default). A compromised billing can call
   only what it is wired to call.
8. **Responses are bound to their request (#64/#67)**: nonce + MAC over body,
   status and request (keyed per service pair with X25519), so a
   mixed-up or injected response is never accepted as a membership answer.
9. **`multi_tenant_strict` (#54)** makes the multi-tenant trust model
   non-optional in prod: RLS everywhere, fail-closed revocation, a separate
   non-evicting security cache, asymmetric JWT and contract-compat checks in
   a split.
10. **Revocation freshness per route.** Strict by default; a `GET`/`HEAD`
    route may declare `revocation_freshness(max_staleness_s<=5)`; a route
    marked `strict_revocation()` (or under
    `AUTH_REVOCATION_STRICT_PATH_PREFIXES`) ignores a budget. The emergency
    block is read live on every request regardless.
11. Contract reads are memoised **per request** (`ContractReadScope`, local
    path). Nothing caches across requests.

## Operation classes (normative)

The TOCTOU question ("is strict RPC enough?") has no single answer: it
depends on what a wrong *allow* costs. Every route and contract method of the
platform belongs to exactly one class.

**The route classes are machine-checked.** Each route declares its class next
to it — `dependencies=[Depends(operation_class("R"))]` (`sandloom.core.di`; on the
route or its router) — and two checks read the declarations:

- `create_app` refuses to start on a declared class that contradicts its
  route: two different classes, a `revocation_freshness` budget on anything
  but class R, or a class RS / W / F / A route that is not always-strict
  (`strict_revocation()` or `AUTH_REVOCATION_STRICT_PATH_PREFIXES`);
- CI: every route must declare exactly one class
  (`sandloom.core.testing.assert_operation_classes(app)`),
  and each module approves its routes' classes in its own committed
  `routes.snapshot.json` (`sandloom routes snapshot --write`; the module's tests
  call `assert_route_snapshot`, and
  `tests/architecture/test_operation_classes.py` checks every
  bundled module) — a route added, removed or re-classified shows up as a
  diff of that module's snapshot, reviewed with the change. Routes the kernel
  mounts (ops endpoints, the platform console's per-module views) are
  classified by the kernel and pinned by that test. The complete route map is
  **generated** (`sandloom routes reference`, published as
  `docs/reference/routes.md`); the tables below explain the policy and its
  enforcement per route family, they are not the authoritative list.

The marker enforces nothing by itself; the mechanisms in the last column
below do. It is a no-op dependency, but FastAPI still resolves it on every
request: **≈ 40 k instructions per request** (cachegrind,
`benchmarks/ladder/cg/marker_cost.py`: a bare in-process `GET` with a
router-level `strict_revocation()` costs 273.9 k instr, 314.0 k with the class
marker added; the `strict_revocation()` marker itself costs ≈ 35 k). At the
ladder's 0.146–0.20 µs per 1 k instructions that is ≈ 6–8 µs, ≈ 1.4–1.7 % of
a typical platform read (2.3–2.8 M instr, 433–497 µs CPU). It can be made
free at request time without changing the declaration: `create_app` already
reads every marker at startup, so it could record the class (and the
strict / budget flags) on the route and remove the marker-only sub-dependants
from `route.dependant` (FastAPI builds the request handler from that object),
with a test that responses and OpenAPI are unchanged — not done here. Contract methods are not covered by
the check (their class is the table below).

| Class | What it is | Max freshness of the authorization input | Strict read enough? | Commit-time re-check / owner execution | Sandloom mechanism that enforces it |
| --- | --- | --- | --- | --- | --- |
| **P** — public | no user subject (catalog, health, login form; the kernel's ops routes, which a system credential gates) | n/a | n/a | no | no auth dependency |
| **R** — ordinary read | the subject's own data, or org data every member may see | **0 today**; may opt in to ≤ N s (N ≤ 5, rollout 1 s) per route, by recorded decision | yes | no (nothing commits) | default strict (no budget); opt-in `revocation_freshness(...)` (+ future contract-read budget) on that `GET`/`HEAD` route only |
| **RS** — read gating sensitive disclosure | data of *other* users, admin listings, secrets (backup codes, invitation lists, RBAC tables) | **0, always** | yes (fresh at read; nothing commits) | no | `strict_revocation()` on the router (budget ignored) + live role check (`require_org_role*`, `session_not_revoked` + `Role` claim) |
| **W** — ordinary write | a member's mutation of their own scope (profile, create an org) | **0 at read**; TOCTOU (≤ request duration) accepted | yes | no — but authorization runs in the owner's transaction where the owner is local | `strict_revocation()`; the owning service performs the write |
| **F** — financial / irreversible | money or an external, non-rollbackable effect: checkout, PSP customer portal, credit spend, PSP webhooks | **0** | **no** — fresh at read only | **required**: re-check an authority version when the effect commits, or run the check and the effect in the owning service's transaction | `strict_revocation()`; fail-closed `idempotency_guard`; durable command dedupe (#50); PSP operations run under a committed **operation intent** (authority = `get_member_state` role + version, re-verified with the caller's session marks right before the intent is issued; refusal revokes it and expires the checkout session; the webhook applies only issued intents — G1, resolved) |
| **A** — administrative / security | membership and role management, ownership, invitations, org settings/deletion, RBAC, user admin, credentials, 2FA, SSO link, token issuance | **0** | **no** | **required**: authorize *inside* the owner's write transaction under a row lock; the change revokes with `require_immediate=True`; an authority owned by ANOTHER service is re-read right before commit | tenancy: target `FOR UPDATE` + actor `FOR SHARE` (`lock_actor_role`, user_id lock order) / `_get_active_for_update` in the same UoW (G3 — resolved); identity: `session_not_revoked` + immediate revocation + `admin_in_transaction` (RBAC advisory lock + actor role `FOR SHARE`, G2 — resolved); cross-service authority: `revocation_at_commit()` (live revocation / block marks as a `before_commit` check — G2 cross-service, resolved); issuance: live `ensure_user_not_blocked`, critical claims contributors never cached |

Rules that follow from the table:

- Only class **R** may ever use bounded staleness (revocation copy or a
  future contract-read cache), and only by an explicit, recorded per-route
  opt-in. Every other class is staleness 0 and `strict_revocation()`.
- Strict RPC is enough for **P/R/RS/W**. For **F/A** it is necessary but not
  sufficient: the decision must hold *at commit*, which a read cannot
  guarantee. The remedy is one of (1) re-check an authority version at
  commit, (2) execute in the owning service — see "TOCTOU" below.
- A class-F effect outside any database (a PSP call) cannot be made atomic
  with a check; the best available is to re-check live *immediately before*
  the effect, keep the PSP session short-lived, and treat the PSP webhook
  (signature-verified, deduped) as the authority for the money movement.

### Platform routes → classes

**Billing** (`/api/v1/billing`; split by this ADR into `billing_read_router`
and `billing_router`, no observable change):

| Route | Class | Today |
| --- | --- | --- |
| `GET /plans` | P | public |
| `GET /subscription` | R | strict (no budget); membership via `resolve_tenant` (live RPC) |
| `GET /credits` | R | strict (no budget); same |
| `POST /checkout-session` | F | always-strict, `idempotency_guard(fail_closed)`, live `get_member_state` (role ≥ admin + version); operation intent committed before the PSP call (PSP key = intent), authority + session marks re-checked before it is issued, refusal → revoked + session expired (G1) |
| `POST /customer-portal` | F | same (the portal can change payment methods / cancel); a refused portal session cannot be expired at Stripe — never handed out |
| `POST /webhooks/stripe` | F (system) | no subject; PSP signature + durable dedupe; router marked always-strict like every F route (no bearer is read); credit grants / new subscriptions only for an issued intent (unauthorized completion → not applied, audited, operator follow-up) |
| contract `billing.credits.v1.spend` | F | command, durable dedupe (#50), ledger in billing's UoW; the *caller* authorizes the subject |
| contract `billing.entitlements.v1`, `billing.facts.v1` | R (as inputs) | read; consumers gating money use `min_freshness="live"` |

**Tenancy** (`/api/v1/orgs`, `/api/v1/invitations`; all `strict_revocation()`):

| Route | Class | Enforcement |
| --- | --- | --- |
| `GET /orgs`, `GET /orgs/{id}`, `GET /orgs/{id}/members`, `GET /orgs/{id}/membership` | R | `require_org_role_reads(member)` (planned read, live) |
| `GET /orgs/{id}/invitations` | RS | `require_org_role(admin)` (live) |
| `POST /orgs` | W | creator becomes owner in one UoW |
| `PATCH /orgs/{id}`, `DELETE /orgs/{id}` | A | `require_org_role(admin/owner)` + org row `FOR UPDATE` (+ actor `FOR SHARE` on PATCH, G3); delete revokes immediately |
| `POST /orgs/{id}/transfer-ownership/{uid}` | A | org row `FOR UPDATE`, actor re-verified as owner under the lock; both parties revoked immediately |
| `PATCH /orgs/{id}/members/{uid}`, `DELETE …` (incl. leave) | A | target row `FOR UPDATE` + actor row `FOR SHARE` in the same UoW, `user_id` lock order (G3); immediate revocation |
| `POST /orgs/{id}/invitations`, `DELETE …/invitations/{iid}` | A | `require_org_role(admin)` + actor row `FOR SHARE` in the write's UoW (G3) |
| `POST /invitations/accept` | A | grants membership; tenancy UoW, `member_added`; authority (verified e-mail claim) is identity's → `revocation_at_commit()` (G2 cross-service) |
| contract `tenancy.membership.v1` reads | input to R/RS/W/F | planned read; strict |
| contract `tenancy.claims.v1` | A (issuance) | critical contributor, never cached |

**Identity** (all routers `strict_revocation()`):

| Route | Class | Enforcement |
| --- | --- | --- |
| `GET /auth/health`, `GET /auth/sso/providers`, `GET /auth/sso/{p}/redirect` | P | — |
| `POST /auth/register`, `/verify-email`, `/resend-verification`, `/request-password-reset` | P (unauthenticated writes) | — |
| `POST /auth/login`, `/refresh`, `GET /auth/sso/{p}/callback`, `POST /auth/sso/mfa` | A (issuance) | live `ensure_user_not_blocked` before any side effect |
| `POST /auth/logout`, `/reset-password`, `/change-password` | A | password change/reset revoke immediately |
| `GET /auth/me`, `GET /auth/2fa/status`, `GET /auth/sso/me/linked` | R | strict; `/me` adds `session_not_revoked` |
| `PATCH /auth/me` | W | `session_not_revoked` |
| `GET /auth/2fa/backup-codes` | RS | strict |
| `POST /auth/2fa/init`, `/enable`, `/disable`, `/verify` (consumes a code, lockout), `/backup-codes/regenerate`, `GET /auth/sso/{p}/link/redirect`, `POST /auth/sso/{p}/link`, `DELETE …/unlink` | A | strict; identity-owned write |
| `GET /api/v1/users`, `/users/{id}`, `/users/{id}/roles`, `/users/{id}/block`, `/roles`, `/roles/{id}`, `/permissions` | RS | `Role("admin")` claim (or self) + `session_not_revoked` |
| `PUT` / `DELETE /api/v1/users/{id}`, `POST /users/{id}/block`, `/unblock`, `/revoke-sessions` (platform admin, fail-closed `Idempotency-Key`), roles / permissions / user-roles mutations | A | `Role("admin")` claim + `session_not_revoked` + `admin_in_transaction` (in-UoW re-check, G2); role revocation revokes immediately |

**Kernel** (`create_app`): `GET /health/simple`, `/health`, `/ready`,
`/diagnostics/*`, `/contracts/*` (incl. `POST /contracts/diff`), `/metrics` —
class P (no user subject; the internal / metrics token gates the non-probe
ones). The kernel's non-API routes (OpenAPI / docs, `/_sandloom/rpc`, authorized per
contract by `RPC_ALLOWED_CALLS`) are outside the route check.

### Gaps the classification exposed — resolved

All three were addressed during internal development (one commit each, failing-first
tests with two real transactions; split conformance green), and then
strengthened in the same PR on review: G1 became an explicit
operation-authorization model, G2 got its cross-service counterpart.

- **G1 — billing F routes decided on a read. Resolved: operation intents**
  (`billing/services/operation_intents.py`, model `OperationIntent`, table
  `billing_operation_intents` in billing's own chain, tests
  `billing/tests/test_operation_intents.py` and `test_f_class_authority.py`).
  `checkout-session` / `customer-portal`:
  1. *authorize* on `get_member_state` (role + membership `version`, one
     call) — or, for the personal tenant, on the caller's own session;
  2. *reserve* an intent (org/tenant, actor, operation, params digest,
     authorized `version`, status `pending`) and **commit it before the PSP
     call** — a crash or timeout leaves a record to reconcile with; a retry of
     the same request (same `Idempotency-Key` + parameters) finds the same
     live intent;
  3. call the PSP with the **intent id as the idempotency key**
     (`sandloom:intent:<id>`): a timeout leaves the intent `pending` (`503`) and the
     retry re-sends the same key → the same session, never a blind new call; a
     definitive `4xx` marks it `failed` (`502`) and frees the key;
  4. *finalize*: the final re-read — the membership state
     (`reverify_authority`, contract-read memo cleared: role lost `403`, other
     change `409`), then the caller's revocation / block marks (`401`) — runs
     with NO billing connection or row lock held, immediately followed by a
     one-statement issue transaction (`UPDATE … SET status='issued' WHERE
     status='pending'; COMMIT`); the URL is returned after it. A refusal
     marks the PENDING intent `revoked` (audited `billing.intent.revoked`) and
     **expires the checkout session** (`expire_checkout_session`, Stripe
     `POST /checkout/sessions/{id}/expire`). A portal session cannot be
     expired at Stripe: it is never handed out again (its URL is not stored
     in the database, and the routes' idempotency guard stores no response —
     see 6).
  6. *Retries.* The F routes' `idempotency_guard` runs with
     `store_response=False`: it dedupes concurrent requests only (`409` /
     `422`) and, when a request finishes, releases the key — no response body
     (a checkout or portal URL) is stored in the cache or replayed without a
     fresh authorization (scopes renamed `billing.checkout_intent` /
     `billing.customer_portal_intent`, so records stored by the previous
     version are never read). Durable idempotency is the intent's: a retry
     with the same key re-authorizes, then
     - finds a `pending` intent → reconciles with the same PSP key;
     - finds an `issued` **checkout** intent → **no PSP call**; the final
       re-read runs again and the stored URL of the same session is returned
       (a failed re-read refuses this request only);
     - finds an `issued` **portal** intent → `409` (its URL is not stored and a
       new PSP call would be a new session; use a new key);
     - a `revoked` / `failed` intent frees the key → a new intent.
     **Nothing revokes, fails or rewrites an `issued` intent**: issue,
     revoke and fail all update `WHERE status = 'pending'`.
  5. The **webhook** applies a credit grant or a new subscription only for an
     object whose `sandloom_intent_id` names an `issued` intent of that customer
     (and, for a session, that session). A completion for a revoked / pending /
     failed / unknown intent is not applied: journaled `failed`, audited
     `billing.intent.unauthorized_completion` (`required=True`), intent flagged
     `followup_at` for an operator. Refunding is a manual operator step (out
     of scope). Objects without the marker (sessions created before intents,
     operator-created subscriptions) apply as before — cheap backward
     compatibility; Stripe checkout sessions live ≤ 24 h, so the unmarked
     window closes by itself.

  *Transaction choice.* Each step is its own short `db_session()` Unit of
  Work — the intent must outlive the failed request (it is the record the
  webhook gate and the operator need), so it cannot live in the request
  transaction, which rolls back on the very `HTTPException` that reports the
  refusal. The routes take **no** request `DbSession`, so no transaction is
  nested in another (no second pooled connection while one is held — the
  AGENTS.md rule) and none spans the PSP call. No `db.commit()` in services.

  *Guarantee.* Billing and tenancy may live in different databases, so the
  intent can never be atomic with the membership row. What the version (and
  session-mark) re-check right before the issue COMMIT gives is a
  **serializable** history, **not** a strictly serializable / linearizable
  one, and the window is **narrowed, not closed** — see "Guarantees,
  precisely" below for the exact statement, the behaviour per step and per
  outage. Residual effects: (i) a payment can complete between a session's
  creation and its expiry if its URL leaked (the URL itself is only returned
  after the re-check); the gate catches it (not applied, audited, operator
  refund) rather than preventing it; (ii) an `issued` intent's session stays
  payable until Stripe expires it (≤ 24 h) even if the authority is withdrawn
  later — nothing revokes issued intents; (iii) a portal session cannot be
  revoked at Stripe; (iv) expiring is best effort (logged on failure; the
  gate is the backstop).
  *What is enforced vs what is compensated.* There is **no atomicity
  between PostgreSQL and the PSP**: no transaction spans both, and none can.
  Two different properties must not be confused:
  - **(a) "The URL is never handed to the user unless the intent is issued"
    — a guarantee Sandloom enforces.** The URL leaves billing only after the issue
    COMMIT succeeded; every refusal, re-check failure or outage before it
    answers an error without the URL (a portal URL is stored neither in the
    database nor in the idempotency cache).
  - **(b) "No external PSP session exists" — NOT guaranteed** once the PSP
    call happened. Expiring a refused checkout session is a **compensation**,
    run after the revocation committed, and it can itself fail. On failure it
    is logged (error), counted
    (`sandloom_facade_counter_total{name="billing_intent_expire_failed"}` — alert
    on it) and **recorded on the intent** (`reconcile_attempts`,
    `reconcile_error`; success sets `psp_expired_at`); the intent stays
    `revoked` (the refusal stands) and the **periodic reconciler** retries
    the expire, bounded by `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS`, then
    leaves `followup_at` for an operator (#72, see "Intent reconciliation").
    Until then the session remains payable at the PSP (Stripe: ≤ 24 h) —
    only by someone who has its URL, which billing never returned; if it is
    paid anyway, the **webhook gate** is the backstop (not applied, audited,
    `followup_at` for an operator, manual refund). A portal session has no
    compensation at all (Stripe cannot expire it).

  *PSP timeout and abandoned retries.* A timeout leaves the intent `pending`
  and answers `503`; a retry with the same `Idempotency-Key` re-sends the
  intent's PSP key and gets the same session. **If the client never retries**
  (or its retry is refused before it reaches the intent, e.g. the authority
  was lost — open item 15), the **periodic reconciler** closes it once it is
  older than `BILLING_INTENT_RECONCILE_AFTER_MINUTES`: it looks the session
  up at the PSP by the intent marker, expires it and revokes the intent (or
  fails it when no session exists) — see "Intent reconciliation" (#72).
  Until then the URL was never handed out and the webhook gate refuses a
  completion for a `pending` intent (audited, operator follow-up).

- **G2 — identity admin actions authorized on a claim. Resolved** (commit
  "re-check the admin role inside the write transaction";
  `identity.access.admin_in_transaction`, `RoleService.holds_admin_in_tx`,
  tests `identity/tests/api/test_admin_in_transaction.py`). Every RBAC /
  user-administration mutation takes a transaction-scoped advisory lock (one
  global order, so two admins demoting each other serialise rather than
  deadlock) and reads the actor's `admin` assignment and active user row
  `FOR SHARE` in the request's UoW. Identity owns RBAC, so it is local in
  every deployment shape; the claim gate stays as the cheap pre-check.

  *Cross-service audit.* Admin authority (RBAC `admin`) lives only in
  identity; no other platform route is gated by `Role("admin")`. Every class
  A / F route outside identity was checked for an authorizing fact owned by
  another service:

  | Route | Authority from | Result |
  | --- | --- | --- |
  | tenancy `PATCH /orgs/{id}`, `DELETE /orgs/{id}`, `POST …/transfer-ownership/{uid}`, `PATCH`/`DELETE …/members/{uid}`, `POST`/`DELETE …/invitations` | tenancy's own membership / org rows | same database → row locks (G3); no change |
  | tenancy `POST /invitations/accept` | **identity**: the token's verified `email` claim | **cross-service** → `revocation_at_commit()` |
  | billing `POST /checkout-session`, `POST /customer-portal` | tenancy (`get_member_state`) and **identity** (caller's session) | **cross-service** → version re-check + `subject_commit_check` before the intent is issued (G1) |
  | billing `POST /webhooks/stripe` | PSP signature (no subject) | n/a; intent gate (G1) |
  | contract `billing.credits.v1.spend` | the *caller* authorizes | durable command dedupe; the calling route classifies itself |
  | contract `tenancy.claims.v1` (issuance) | tenancy's own rows | critical contributor, never cached |

  No row lock can be atomic across databases, so the cross-service routes use
  a reusable, opt-in kernel hook: `before_commit(callback)` (a check queued on
  the Unit of Work, run right before `COMMIT`; a failure rolls back) and, on
  top of it, `revocation_at_commit()` / `subject_commit_check(request)`, which
  re-read the caller's revocation and block marks LIVE. Every authority
  withdrawal — tenancy role change / removal, identity role revocation,
  `block_user`, password change — writes its mark BEFORE it commits
  (`require_immediate=True`). A mark seen rolls our write back (`401`); no
  mark seen means the withdrawal had not started when we read, so the history
  can be serialized with our action first — serializable, NOT linearizable:
  the window shrinks from the whole request to our final read → COMMIT, it is
  not closed (see "Guarantees, precisely"). Tests:
  `core/tests/test_before_commit.py`,
  `tenancy/tests/test_accept_authority_at_commit.py` (a block written while
  the acceptance runs → `401`, no membership; real request UoW).
- **G3 — tenancy read the actor's own row without a lock. Resolved**
  (commit "lock the acting member's row in class-A writes";
  `MembershipService.lock_actor_role`, tests
  `tenancy/tests/test_actor_row_lock.py`). `FOR SHARE` (not `FOR KEY SHARE`:
  a role change is a non-key update that must conflict) on the actor row in
  role change, removal, org update and invitation create/revoke; member rows
  are locked in `user_id` order (org row first), also in ownership transfer.
  These are request-UoW writes, not contract calls, so the contract pool's
  `lock_timeout` does not apply to them; contract reads take no row locks
  and are not blocked (MVCC).

### Guarantees, precisely

"Authority" below is the fact the action was authorized on (a role, a
membership, a non-revoked session); a *withdrawal* is the change that removes
it (demotion, removal, role revocation, block, password change). Statements
are about that one conflict, not about the whole system.

| Mechanism | Where | Guarantee |
| --- | --- | --- |
| In-transaction row locks (G3 tenancy `FOR SHARE` actor / `FOR UPDATE` target; G2 identity advisory lock + `FOR SHARE`) | authority and action in ONE database, one transaction | **Strictly serializable** for the conflict: the withdrawal needs a lock the action holds until its COMMIT (or vice versa), so the one that commits second also *starts its decisive step* after the first committed — real-time order and serial order agree. No window. |
| `revocation_at_commit()` / `subject_commit_check` (cross-service) | authority in another service (identity's marks), action in ours | **Serializable, not strictly serializable / linearizable.** The withdrawal writes its mark before it commits and reads nothing our action writes, so "our action, then the withdrawal" is a valid serial order (no cycle) whenever we saw no mark. But a withdrawal can write its mark, commit and answer its caller **between our final read and our COMMIT**: in real time our action then becomes visible *after* the withdrawal completed. The window is **narrowed** — from the whole request (including the PSP call) to final read → COMMIT (one cache round trip + the commit) — **not closed**. |
| G1 version re-check + operation intent | authority in tenancy (+ identity's marks), action = billing's issue commit + an external PSP session | Same as the row above (serializable, not linearizable; window = final re-read → issue COMMIT). Authority is checked against **tenancy's committed state as of the final re-read**; the PSP effect is deduplicated by the intent's key. |

**What would close the window** (NOT implemented — a recorded option): a
*fence* — the withdrawal waits for, or invalidates, in-flight authorized
actions of the subject before it commits; e.g. a short lease per (subject,
action) taken before the final read and released after COMMIT, which the
withdrawal must acquire (or break and revoke) first; or a withdrawal-side
sweep that revokes the subject's `issued` intents and expires their sessions.
Both make the withdrawal depend on the action's service, which is what the
split was meant to avoid; the residual window is accepted for now.

**Authorization point (G1).** The operation counts as authorized at billing's
**issue COMMIT** (the transaction that marks the intent `issued`); the URL is
returned only after it. Precisely: *authority is checked against tenancy's
state as of the final re-read.* The authoritative membership `version` lives
in tenancy's database. Billing re-reads it LIVE right before the issue
transaction (a separate read of tenancy's committed state, with no billing
connection or lock held and no lock in tenancy) and compares it with the
`version` captured at authorize in this request, then re-reads the caller's
revocation / block marks — the later of the two reads is the final re-read —
and then runs `UPDATE … WHERE status = 'pending'` + COMMIT. The window is
final re-read → that COMMIT (one statement + the commit).
- A change committed in tenancy **before** the final re-read is seen — any
  change since authorize, including a promotion or an ownership transfer:
  `403` / `409` (a tenancy role change also writes the subject's revocation
  mark before it commits: `401` if only the mark read sees it); the intent is
  revoked and the session expired (test
  `test_role_change_committed_before_the_final_reread_is_refused`).
- A change committed in tenancy **after** the final re-read and before
  billing's issue COMMIT is **not seen**: nothing ties tenancy's later
  commits to billing's, so the intent is issued (the acknowledged window;
  test `test_window_role_change_after_the_final_reread_is_not_seen`).
A retried request re-authorizes on its own fresh read (and a retry of an
already-issued checkout re-reads again before returning the stored URL; it
never calls the PSP or touches the intent). A PSP retry cannot
duplicate the session: every call for one intent carries the same key
(`sandloom:intent:<id>`), and a new intent (new key) is only created when the old
one is closed (`revoked` / `failed`) or for a new `Idempotency-Key`. Limit:
Stripe keeps idempotency keys ~24 h; a `pending` intent retried after that
could get a second session (both are tied to the same intent; only the one
recorded at issue is honoured by the gate's session match).

**Concurrent role change, per step (G1, organization checkout):**

| Withdrawal commits… | Outcome |
| --- | --- |
| before authorize | `403` at authorize; no intent, no PSP call |
| between authorize and reserve, or during the PSP call | the re-check sees a moved version → `403` / `409`; intent `revoked` (audited), checkout session **expired**, no URL |
| between the final re-check and the issue COMMIT | **not detected**: intent `issued`, URL returned (the residual window above); serializable as "checkout, then demotion" |
| after the issue COMMIT | ordered after the action; the session stays payable until Stripe expires it; the webhook honours it (issued). Nothing revokes issued intents (fence option) |

**Owner unavailable:**

| Outage | Outcome |
| --- | --- |
| tenancy unreachable at authorize | `503`; no intent, no PSP call |
| tenancy unreachable (or failing) at the re-check | fail closed: `503` / `500`; intent `revoked`, checkout session **expired**, no URL; a retry gets a new intent (test `test_owner_unreachable_at_the_recheck_revokes_and_expires`) |
| Redis (revocation store) down at the mark re-check | `AUTH_REVOCATION_FAIL_CLOSED` (default **on in prod**): `503`, intent `revoked`, session expired, no URL. **Fail-open (the default outside prod): the re-check accepts** (logged), exactly like the request-start check — the cross-service guarantee does not hold in that mode |
| PSP unreachable / timeout | intent stays `pending`, `503`; the retry re-sends the same key (same session) |
| billing database down at finalize | the issue cannot commit and the revocation cannot be recorded: the intent stays `pending` (never issued; the gate refuses it), the session is expired best effort, error answered |

For `POST /invitations/accept` (`revocation_at_commit()`): a block /
revocation before the final mark read → `401`, nothing committed; between that
read and COMMIT → the membership commits (residual window); Redis down → as
above (prod `503`, dev accepted).

### Intent lifecycle (state transitions)

| From → to | Triggered by | Condition |
| --- | --- | --- |
| (none) → `pending` | reserve (request) | authorized at authorize; committed before any PSP call |
| `pending` → `issued` | finalize (request) | final re-read passed; `UPDATE … WHERE status='pending'` |
| `pending` → `revoked` | finalize refusal / re-check failure (request) | `WHERE status='pending'`; audited; checkout session expired (compensation) |
| `pending` → `failed` | definitive PSP `4xx` (request) | `WHERE status='pending'` |
| `pending` → `pending` | PSP timeout / crash; a retry reconciles | same PSP key |
| `pending` → `revoked` | reconciler (#72): stale, a session with its marker exists | `WHERE status='pending'`; audited (system actor); open sessions expired; a completed one → `followup_at` |
| `pending` → `failed` | reconciler (#72): stale, no session with its marker at the PSP | `WHERE status='pending'` |
| `issued` → *anything* | — | **forbidden.** No HTTP retry, refusal or PSP error changes an issued intent; `psp_session_id` / `psp_session_url` are never replaced |
| `revoked`, `failed` → *anything* | — | forbidden (terminal) |

Invariants per state: `pending` — never handed out, the webhook gate refuses
it; closed by the reconciler once stale; `issued` — **terminal**: its session
may be paid and the gate honours it (same customer, same session, and — for a
new subscription — the intent's price, at most one subscription); the only
later write is the webhook's `followup_at` flag, which is not a status change
(no completion / expiry state exists, and none is added); the reconciler never
selects it; a retry past `psp_session_expires_at` answers `410` (open item 13);
`revoked` / `failed` — terminal, free the request key, the gate refuses them.
Non-status bookkeeping (never a transition): `psp_expired_at` (the session can
no longer be paid), `reconcile_attempts` / `reconcile_error` (failed
reconciler steps), `followup_at` (operator). Pinned by
`test_closing_an_issued_intent_is_a_no_op` (revoked / failed),
`test_issuing_an_issued_intent_again_is_refused`,
`test_failed_recheck_on_retry_never_revokes_an_issued_intent`,
`test_retry_of_an_issued_intent_returns_its_url_without_a_psp_call`.

**Idempotency of the effect is not idempotency of handing out a secret.** A
checkout or portal URL is a bearer secret. It is never replayed from the
idempotency cache (`store_response=False`), and when it is reproduced from a
durable source (the issued intent's stored checkout URL) that path passes the
same current-authority check first — the membership state and the caller's
session marks. A retry of an issued checkout by a caller who has since lost
billing authority gets `403` and no URL; the intent stays issued and the PSP
is not called (`test_demoted_manager_retrying_an_issued_checkout_gets_no_url`).
A portal URL is never stored, so a portal retry needs a fresh authorized
portal intent (new key).

### Intent ownership (application-level isolation)

`billing_operation_intents` — like every billing table — has **no RLS** and
is not `TenantScoped`: isolation here is **application-level, not
database-level**. Every path constrains by the owner, never by an intent id
alone (and never by an id a user or the PSP supplied):

| Path | Who supplies the id | Constrained by |
| --- | --- | --- |
| retry lookup (reserve) | none — looked up by the request | `actor_id` + operation + sha256(`Idempotency-Key`), live statuses; then `customer_id` = this tenant's customer and the params digest must match (else `422`) — one actor's key on another tenant is refused, another actor's key is never seen |
| insert (reserve) | server-generated id | the tenant's customer, the authorized actor |
| issue (finalize) | id held by the request since reserve | `id` + `customer_id` + `actor_id` + live status |
| revoke / fail (`_close`) | id held by the request | `id` + `customer_id` + live status |
| expire (compensation) | PSP session id from the PSP call of this request | — (PSP call, no DB read) |
| webhook gate | **PSP metadata** (`sandloom_intent_id`) | `id` + `customer_id` = the customer resolved from the event's **PSP customer**; must be `issued`, a `checkout` intent, and — for a checkout completion — its stored `psp_session_id` must equal the event's session id. Another customer's intent reads as unknown: refused, audited, and that intent is NOT flagged |
| follow-up flag (webhook) | the intent found above | `id` + `customer_id` |
| reconciler (#72, system code, scans across tenants) | none — selected by status / age | every PSP lookup lists the intent's OWN PSP customer's sessions and matches the marker; every write is `id` + `customer_id` + the status it was selected in |
| operator / admin read | — | none exists |

Tests: `test_reconciler_scopes_every_write_to_the_intents_customer`,
`test_another_tenants_idempotency_key_is_never_reused`,
`test_same_key_for_another_tenant_of_the_same_actor_is_refused`,
`test_webhook_naming_another_customers_intent_is_not_applied`,
`test_checkout_completion_must_name_the_intents_own_session`.

### Intent reconciliation (#72)

`billing.tasks.reconcile_operation_intents` (Celery beat, every 5 min;
`@shared_task(acks_late=True, reject_on_worker_lost=True)` — it is
idempotent) runs `IntentReconciler`
(`billing/services/intent_reconciler.py`). It closes the two loose ends no
request closes, using ONLY the existing `pending`-guarded transitions:

| Candidate | PSP shows (sessions of the intent's own PSP customer carrying its marker) | Action |
| --- | --- | --- |
| `pending`, older than `BILLING_INTENT_RECONCILE_AFTER_MINUTES` (default 30, min 5) | none | → `failed` |
| | open session(s) | → `revoked` (audited, system actor), then each is expired (outcome recorded) |
| | only expired session(s) | → `revoked`, `psp_expired_at` |
| | a completed session | → `revoked` + `followup_at` (paid without an issued URL; the gate refuses it; manual refund) |
| | (portal intent: cannot be listed or expired) | → `revoked` (its URL was never handed out) |
| | lookup fails | stays `pending`; `reconcile_attempts + 1`, `reconcile_error` |
| `revoked` checkout whose request-path expire failed (`reconcile_attempts > 0`, `psp_expired_at` NULL) | open | expire; success → `psp_expired_at` |
| | expired, or past its `psp_session_expires_at` | `psp_expired_at` (nothing owed) |
| | completed | `followup_at`, no more retries |
| | lookup / expire fails | `reconcile_attempts + 1`, `reconcile_error` |

Bounds: at `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS` (default 5) failed steps
the intent gets `followup_at` and is never selected again — an operator takes
over. No transaction spans a PSP call (candidates are read in one short UoW;
every write is its own UoW guarded by `id` + `customer_id` + the selected
status + `psp_expired_at IS NULL` — a session another run or the request
path already expired is never written again, and a late failure of a
concurrent attempt is neither recorded nor counted, not escalated).
*Staleness is measured from `created_at`*, not from the last activity: a
client retry of a pending intent writes nothing to the row (it re-sends the
same PSP key), so there is no activity timestamp to use; the threshold
(minimum 5 min) only has to exceed the longest request (PSP timeout ×
retries ≈ 1 min). A retry arriving after the reconciler closed the intent
gets a new intent (the closed one freed its key). The lookup is `GET /v1/checkout/sessions?customer=…&created[gte]=…`
(Stripe's search API does not cover checkout sessions), matched on
`metadata.sandloom_intent_id` client-side; a new port method
`list_checkout_sessions` (fake + Stripe client). Revoked intents from before
`0007_billing` have `reconcile_attempts = 0` and are not retried (their
outcome was never recorded; the PSP expires them within 24 h).

*What it guarantees:* an abandoned `pending` intent is closed within
threshold + one interval (while the PSP answers); a session found open is
expired; every failure is persisted and bounded and ends with `followup_at`.
*What it does not:* it cannot close the window between the session's
creation and the reconciler's expire (a leaked URL can be paid in it — the
gate refuses the completion); a lookup only sees sessions created within the
listing window of the intent's own PSP customer; concurrent runs may count
one failure twice; a `pending` intent whose request is still running past the
threshold (≥ 5 min, i.e. a stuck process) can be failed under it — its late
finalize then refuses to issue (`WHERE status='pending'`) and expires the
session it created. *Signals:* the task result / one log line (WARNING on
`still_failing` / `escalated`) and the table itself (`followup_at`,
`reconcile_attempts`) — the framework's Celery worker exports no Prometheus
endpoint, so the reconciler increments no counter (the request path's
`billing_intent_expire_failed` remains). Tests:
`billing/tests/test_intent_reconcile.py`.

### Webhook matrix

Every Stripe event type billing handles (anything else is journaled
`skipped`). Class (i): user-initiated through Sandloom; class (ii): PSP lifecycle.
All are PSP-signed and deduped by event id; ownership is resolved from the
event's PSP customer (`billing_customers.stripe_customer_id`).

| Event | Class | Current check | Gap |
| --- | --- | --- | --- |
| `checkout.session.completed` / `checkout.session.async_payment_succeeded` (mode `payment`, credit pack) | (i) checkout | PSP customer known; `metadata.customer_id` = that customer; price from the catalog; `amount_total` + currency = price; `payment_status=paid`; intent gate (issued checkout intent of this customer, same session); credits once per session (ledger key) | unmarked (pre-intent) sessions apply — Stripe sessions live ≤ 24 h, so it closes by itself |
| `checkout.session.async_payment_failed` | (i) checkout | as above, grants nothing | — |
| `checkout.session.completed` (mode `subscription`) | (i) checkout | skipped — the subscription events carry the intent marker (`subscription_data.metadata`) | — |
| `customer.subscription.created` (new id) | (i) checkout | PSP customer known; status must be a Stripe status (**fixed**); NEW row only for an issued checkout intent of this customer when the marker is present, with exactly one item priced at the intent's price, and only one subscription per intent (`billing_subscriptions.intent_id`, partial UNIQUE) — resolved (item 16) | resolved (item 9): a subscription WITHOUT the marker is stored and synced as `origin='psp'` but grants entitlements only when its price is in the catalog AND `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS=true` (default `false`) |
| `customer.subscription.updated` — portal-driven (plan change, `cancel_at_period_end`, resume) | (i) portal | existing row must belong to the event's customer; ordering guard (`created`, same-second re-fetch); terminal statuses never revived; valid Stripe status (**fixed**) | NOT tied to a portal intent: Stripe does not send the portal session with the change, and a portal change is indistinguishable from a lifecycle update. What ties it to Sandloom's authority: Stripe accepts portal actions only within a portal session, and billing hands out a portal URL only for an issued portal intent (after the re-check). Not gated (gating would desynchronize billing from the PSP); a plan / `cancel_at_period_end` change with no issued portal intent of the customer in the attribution window is audited `billing.subscription.unattributed_change` — resolved as far as Stripe allows (item 10). |
| `customer.subscription.updated` — lifecycle (renewal period, `past_due`, `unpaid`, `paused`, `incomplete` → `active`) | (ii) | as above | resolved (item 11): an unknown price grants nothing — the state is stored, the price becomes NULL (never the previous one), audit `billing.subscription.unknown_price`, operator follow-up |
| `customer.subscription.deleted` (cancel, dunning end) | (ii) | owner check on a known row; always applies (terminal) | — |
| `invoice.paid` (renewal, dunning recovery) | (ii) | subscription found by id; **its owner must be the invoice's PSP customer (fixed — it was found by id alone)**; only `past_due`/`unpaid` → `active`, never `trialing` / terminal; period only moves forward; ordering guard | — |
| `charge.refunded` | (ii) | state recorded per `payment_intent` (also before the grant exists, applied at the grant's creation); the charge's PSP customer must own the grant; currency = grant's; credits reversed in proportion to the cumulative `amount_refunded` (rounded up) — resolved (item 12) | a refund of a subscription invoice or a pre-`0008_billing` purchase reverses nothing |
| `charge.dispute.created` / `.updated` / `.closed` | (ii) | state recorded per payment intent (also before the grant exists); a dispute carries no customer: the grant's link, recorded from the customer's own signed checkout, is the ownership; open / lost → the disputed share reversed (partial disputes proportional, refund + dispute capped at the grant); `won` → restored; inquiry (`warning_*`) → audit only — resolved (item 12) | subscription access is not changed; a dispute of a payment with no grant is audited `billing.charge.disputed` |
| `invoice.payment_failed`, `customer.updated`, `payment_method.*` | (ii) | not handled (`skipped`); `past_due` / cancellation arrive as subscription events | — |

Fixed here, with tests (`test_webhook_ownership.py`): `invoice.paid`
ownership; unknown subscription statuses refused; `incomplete_expired`
terminal like `canceled`.

## Options

The question for each option: **how stale can a membership answer be, which
classes may use it, how does it fail, does the monolith still answer like the
split, and what does it save** (at ≈ 0.36 ms per avoided call, × hit rate).

### (a) Caller-side, per-process TTL cache declared by the provider contract

The provider marks a read method cacheable and names the events that
invalidate it; the contract runtime (not the module) keeps a bounded
per-process LRU on the caller side.

```python
# tenancy (provider) — declaration only
@classmethod
@contract_method(
    planned=True,
    cache=ContractCache(
        max_ttl_s=5,
        key=("organization_id", "user_id"),
        invalidated_by={
            "tenancy.member_added.v1": ("organization_id", "user_id"),
            "tenancy.member_removed.v1": ("organization_id", "user_id"),
            "tenancy.member_role_changed.v1": ("organization_id", "user_id"),
            "tenancy.organization_deleted.v1": ("organization_id",),
        },
    ),
)
async def get_role(cls, organization_id: UUID, user_id: UUID) -> str | None: ...
```

Runtime rules (unchanged from the first draft): reads only; effective TTL
`min(provider max_ttl_s, route budget)`, hard cap 5 s, rollout 1 s; no
negative caching; class-R `GET`/`HEAD` routes only (`create_app` refuses a
budget elsewhere); key = (contract, hash, method, bound tenant, args); an
entry is stamped with the time its read started and not stored if an
invalidation raced it; never stale-if-error; invalidation by Redis pub/sub
broadcast + hard TTL + whole-cache drop on subscription loss.

| Property | Value |
| --- | --- |
| Worst-case staleness | broadcast latency; ≤ TTL when a broadcast is lost. A removal or a role change also revokes the token immediately (#47), so the old token is `401` everywhere — a cached positive membership is reachable only by a *refreshed* token, and a refreshed token after removal no longer lists the org in `tenant_ids` |
| May serve | class R only |
| Saving | `hit × 0.36 ms − C_lookup` per membership call — **hit unknown → measure (shadow)** |
| New parts | broadcast channel, LRU, contract declaration |

### (b) Signed claims in the access token

After #47 every role change revokes immediately, so the earlier blocker
("a demoted admin keeps the claim for the token lifetime") is gone: a role
claim would be at most as stale as the revocation path (≈ 0, residuals: skew
above `AUTH_REVOCATION_CLOCK_SKEW_SECONDS`, a Redis that loses an
acknowledged write). It still cannot serve F/A (a JWT field cannot be
re-checked at commit), token size grows with organizations, and **roles are
not put in the JWT** (decided) — kept shelved.

| Property | Value |
| --- | --- |
| Worst-case staleness | ≈ 0 via immediate revocation (#47); residuals as above |
| May serve | classes R (and membership-only gates) |
| Saving | `0.36 ms − C_claim_parse` per call, 100 % "hit" |

### (c) Local membership projection per consuming service

`<module>_membership_projection` fed by the versioned member events
(`register_authz_projection`), with the rolling-upgrade refresh rule (#47) and
a lag watermark that turns reads indeterminate past N s. Prerequisites still
missing: an org-wide tombstone for `organization_deleted` and a paged
"all memberships of an org" read for the rebuilder. Serves class R only;
costs a table, consumers and a rebuilder per consuming module. Sanctioned for
lists/joins that need membership at write time; not the default gate.

### (d) Keep strict RPC, make each call cheaper

Done in large part: the aiohttp transport (#62) cut the call from ≈ 1.4 ms to
≈ 0.36 ms total (caller 0.115 ms); keyed response binding (#64/#67) and
service identity (#57) added security at small cost. Remaining levers:
batched `get_role` + `list_user_organization_ids` for `resolve_tenant`,
single-flight of identical concurrent calls, prepared requests (the "Prepared
HTTP Route Plan" A/B being measured now). Serves every class unchanged.

## Summary

| | (a) TTL cache | (b) claims | (c) projection | (d) cheaper RPC |
| --- | --- | --- | --- | --- |
| Staleness at read | ≤ N (1 s, cap 5 s) | ≈ 0 (#47) | event lag; deny past watermark | 0 |
| Classes it may serve | R | R | R | all |
| Holds at commit (F/A)? | no | no | no | no — needs the TOCTOU remedy |
| Saving per call | `hit × 0.36 ms` | ≈ 0.36 ms | ≈ 0.36 ms − local `SELECT` | part of 0.36 ms |
| Fails on Redis outage as | strict RPC | 503 (prod) | deny past watermark | unchanged |
| Monolith vs split | identical after invalidation barrier | identical | converge after drain | identical |

## Failure rules (fail-closed defaults)

| Situation | Rule |
| --- | --- |
| Cache miss / expired and provider down | `503 RemoteServiceUnavailable` (authz: indeterminate → deny). Never stale-if-error, never a default role. |
| Cache hit and provider down | served only while younger than its TTL (≤ N); then as a miss. |
| Invalidation broadcast lost / subscription dropped or re-established | drop the **whole** process cache. |
| Projection lag above N | the fact is indeterminate (deny, 503); rebuild via `sandloom projection rebuild`. |
| Revocation-mark cache down | reads: `AUTH_REVOCATION_FAIL_CLOSED` (closed in prod → 503); membership/role changes: refused (`require_immediate`, #47). |
| Unknown role on the ladder | defer to the live `has_role_at_least`. |
| Negative answer | deny; never cached. |

## Strict RPC is not zero staleness: the TOCTOU window

A strict read is fresh **at read time only**. Between the read and the
caller's commit the membership can change; no read-side option — strict RPC,
(a), (b) or (c) — closes that window. Per the class table it is accepted for
P/R/RS/W and must be closed for F/A by one of:

1. **Re-check an authority version at commit.** The read returns the
   membership `version` (maintained per (org, user) since #45); the effect
   re-checks it — as an argument of the owner's command contract, or a
   conditional write that fails when the version moved — and aborts with
   `409` on a mismatch.
2. **Execute in the service that owns the invariant.** The check and the
   effect run in one transaction there (tenancy's A-class writes already do,
   with row locks).
3. **Re-read a cross-service authority right before commit** when neither is
   possible (the fact is another service's and the effect is ours):
   `before_commit` + the live revocation / block marks
   (`revocation_at_commit()`), which every authority withdrawal writes before
   it commits — G2 cross-service. Serializable, not linearizable: it narrows
   the window to final read → COMMIT, it does not close it (closing needs a
   fence, not implemented — see "Guarantees, precisely").
4. **External effects** (a PSP call) cannot be inside any transaction: persist
   an authorized intent first, key the external call by it, re-check before
   issuing, undo what the external system allows (expire), and gate the
   external system's completion callback on the intent — G1.

## Decisions so far

The ADR stays **Proposed**; these are recorded, the open items are not.

1. **Default staleness is 0.** Nothing is cached unless a route opts in.
2. **Class R only, explicit per-route opt-in, max 5 s, rollout 1 s.**
3. **No negative caching.**
4. **One mechanism for monolith and microservices.**
5. **Invalidation = Redis pub/sub + hard TTL + whole-cache flush on
   subscription loss.**
6. **Revoke tokens on role change** — **done (#47)**, immediately
   (`require_immediate=True`), also on ownership transfer and org deletion.
7. **No roles in the JWT.**
8. **Versioned membership events** — done (#45), with the rolling-upgrade
   refresh rule (#47).
9. **Operation classes** (this revision): the table above is normative; every
   platform route declares its class (`operation_class(...)`), checked at
   startup (contradictions) and in CI (completeness + this route map); F/A
   need the commit-time remedy.
10. **Billing split** (this revision): `billing_read_router` (P/R: plans,
    subscription, credits) and `billing_router` (F: checkout, portal, always
    strict). Every route stays at staleness 0 — the split only makes the class
    explicit. Opting a read in later is a recorded decision that changes
    `tests/test_billing_route_classes.py` on purpose.

## Now / Then / Later

**Now (this revision).** Update the ADR to the state after #47–#67; the
operation classes and the route map; the billing read/financial split with
no behaviour change (identical responses, identical strictness, OpenAPI
unchanged, split conformance green).

**Then (prepared, to be measured).** `sandloom.core.contract_cache_shadow`: a
shadow of option (a) for `tenancy.membership.v1` — **OFF by default, never
serves from a cache**. When `CONTRACT_CACHE_SHADOW` is set it records, for
each actual provider read (local and RPC; per-request memo hits excluded),
whether a cache keyed by (caller service, contract, method, bound tenant,
args) with TTL 1 s / 5 s *would* have answered it, whether that answer would
have been stale, and how many entries the member events would have dropped
while still live. Invalidations travel exactly as decided for (a) (post-commit
in the provider's process, Redis pub/sub to the others, whole flush on
subscription loss), so the shadow measures their cost too. Metrics:
`sandloom_contract_cache_shadow_total{caller,contract,method,ttl,outcome}` with
outcomes `hit`, `miss`, `stale_hit`, `negative`, `unkeyable`,
`invalidated_live`, `invalidated_expired`, plus
`sandloom_contract_cache_shadow_flushes_total{reason}`; an optional JSON report per
process at exit (`CONTRACT_CACHE_SHADOW_REPORT_DIR`). Unit tests prove results,
errors and their order are identical with the shadow on and off, locally and
over RPC.

A per-method hit rate alone cannot decide it (a high rate on a rarely called
method is a small saving; a hit that serves a revoked role is a security
cost), so the shadow also records:

- **benefit per request** — the share of HTTP requests that make at least one
  watched read, reads and would-be hits per request, and the share of
  requests with a hit (`…_requests_total{with_reads}`,
  `…_request_reads_total`, `…_request_hits_total{ttl}`,
  `…_requests_with_hit_total{ttl}`; per route in the report). Kernel ops
  paths are excluded; the middleware exists only while the shadow is on;
- **time avoided** — the measured duration of each read that would have been
  a hit, summed per method (`…_saved_seconds_total`) and per request
  (`…_request_saved_seconds_total`), next to the mean of every observed read
  (`…_read_seconds_total`). It is wall time of the call (an upper bound of
  the caller's latency saving, not CPU); the CPU saving is still read from
  the on/off control runs;
- **potentially stale decisions** — the shadow still makes the live read, so
  every hit is compared with it; a mismatch (`stale_hit`) is counted by kind
  (`…_stale_total{…,kind}`): `removed` (live answer negative — the member was
  removed), `downgraded` / `upgraded` (by the contract's `rank` ladder,
  `{"member": 1, "admin": 2, "owner": 3}` for membership), `narrowed` (a
  shorter organization list), `changed`. `removed`, `downgraded` and
  `narrowed` would have over-granted; with invalidation on they must be ≈ 0.

Cost of the shadow (`benchmarks/ladder/micro_shadow.py`, local read-contract
call on a fake database, interleaved rounds, idle machine): **off** (the
default, pinned by a test) a read pays one `for_contract()` check, ≈ 73 ns,
and no middleware is installed; **on** it adds ≈ 9.6–10.1 µs per watched read
(≈ 9.8 µs → 19.4 µs for that I/O-free call) — measurement-mode cost, to be
read against the on/off control runs, never left on in production.

**Later.** Whether a cache is needed **at all** is decided on those
measurements (open item 4), not before. Monotonic security versions
(`auth_version`) are a **separate research item** —
`docs/design/auth-version-research.md` (internal research, retained in the original private repository)
(problem, open questions, evaluation criteria; no implementation).

### Measuring the shadow hit rate (to schedule after the running benchmark)

Run only when no other measurement is running, from `benchmarks/`, on the
login-free stand of #65 (`run_owasp.py nologin`; `$INT_SP` as there, with
`$INT_SP/b/main` a `git archive` export of a commit that contains this
change):

```bash
# 0. nothing else may be measuring
pgrep -f "rpc_stand|server_bench|run_int|run_rpctx|run_owasp|stage_ab|route_plan" \
  && { echo "busy"; exit 1; }
cd benchmarks
export INT_SP=<stand root>
SHADOW='{"tenancy.membership.v1":{"ttls":[1,5],
"rank":{"member":1,"admin":2,"owner":3},"invalidated_by":{
"tenancy.member_added.v1":["organization_id","user_id"],
"tenancy.member_removed.v1":["organization_id","user_id"],
"tenancy.member_role_changed.v1":["organization_id","user_id"],
"tenancy.organization_deleted.v1":["organization_id"]}}}'
SHADOW=$(printf '%s' "$SHADOW" | tr -d '\n')
OUT=$PWD/ladder/results/shadow_$(git rev-parse --short HEAD)
for P in mixed-nologin read-heavy write-heavy-nologin; do
  RATE=$(python -c "import sys; sys.path.insert(0,'ladder'); import run_int as r; print(r.RATES['$P'])")
  rm -rf "$OUT/$P" && mkdir -p "$OUT/$P"
  # same dataset reset + Redis flush + row-count check as the #65 runs
  python -c "import sys; sys.path.insert(0,'ladder'); import run_owasp as o; \
o._prepare('split', '$OUT/prepare.jsonl', 'owasp_min')"
  python ladder/split_bench.py \
    --topology "$INT_SP/int/topo_split.json" --mix "$INT_SP/int/prof/$P.json" \
    --env-file "$INT_SP/int/main.env" \
    --env PLANNER_OPTIMIZED_EXECUTOR=true --env RPC_TRANSPORT=aiohttp \
    --env "CONTRACT_CACHE_SHADOW=$SHADOW" \
    --env "CONTRACT_CACHE_SHADOW_REPORT_DIR=$OUT/$P" \
    --header "Authorization: Bearer $(python -c 'from benchsuite import mix; print(mix.mint(hours=24, tenant=True))')" \
    --python "$INT_SP/b/main/benchmarks/.venv/bin/python" \
    --cwd "$INT_SP/b/main/benchmarks" \
    --rate "$RATE" --seconds 60 --label "shadow|$P" >> "$OUT/runs.jsonl"
  python ladder/agg_shadow.py "$OUT/$P" --routes   # per method + per request
done
```

Read the tables per caller service (billing, apps). Per method: `hit_pct` is
the share of membership calls a cache would have answered, `stale_pct` the
share of those hits that would have been wrong and `over_grant` how many of
them would have granted more than the live answer, `read_ms` the mean
measured read, `saved_ms` the summed duration of the would-be hits,
`inv_live` the entries the member events would have dropped while live. Per
request: `reads_pct` the share of requests making a membership read,
`hits_per_req` / `hit_req_pct` how many would have hit, `saved_ms_per_req`
the read time a cache would have saved per request — the number to compare
with the split premium per request (`--routes` breaks it down per route). The same run with the shadow unset is the control: CPU per op
must match within noise (the shadow's own bookkeeping cost is part of the
result). **Limitation:** `split_bench.py` sends one bearer token, i.e. one
user — that hit rate is an **upper bound**. The deciding run needs a
many-user / many-org mix with realistic think time (open item 6).

## Open items (owner questions)

1. ~~**G1 (billing F routes).**~~ **Resolved** — operation intents: committed
   before the PSP call, PSP key = intent, re-check before issue, revoke +
   expire on refusal, webhook gate; see "Gaps … resolved".
2. ~~**G2 / G3.**~~ **Resolved** — in-transaction admin re-check in identity,
   `FOR SHARE` on the actor row in tenancy, `revocation_at_commit()` for the
   cross-service routes; see "Gaps … resolved".
3. **Which billing reads may opt in** to a budget, once a cache exists
   (`GET /subscription`, `GET /credits`)? Both are class R today.
4. **Is a cache needed at all?** Threshold on the measured shadow on the real
   mix (e.g. keep (a) only if `saved_ms_per_req` — equivalently
   `hits_per_req × 0.36 ms` — is a material share of the remaining split
   premium per request, and `over_grant` is 0 with invalidation on); what
   would justify a budget above 1 s.
5. **`auth_version`** — see the research note; separate decision.
6. **A realistic multi-user load mix** (token per virtual user, think time)
   for the deciding shadow run.
7. ~~**Stale `pending` intents** (#72).~~ **Resolved** — the periodic
   reconciler (`billing/tasks.py`, `billing/services/intent_reconciler.py`;
   see "Intent reconciliation") closes them through the `pending`-guarded
   transitions and retries failed expire compensations, bounded, then
   `followup_at`. Tests: `billing/tests/test_intent_reconcile.py`
   (`test_stale_pending_intent_session_is_found_by_marker_and_expired`,
   `test_failed_compensation_is_persisted_and_retried`,
   `test_psp_lookup_outage_is_bounded_then_left_for_an_operator`,
   `test_reconciler_never_touches_an_issued_intent`).
8. **Test-harness isolation** (#71): class-F route tests share the process's
   active core and default database with other suites.
9. ~~**Intent-less subscriptions.**~~ **Resolved** (branch
   `fix/billing-entitlement-policy`): *syncing the PSP's truthful state is
   separate from granting entitlements.* A subscription created without an
   operation intent (operator-created in the PSP, migrated, restored) is
   stored and synced like any other (`billing_subscriptions.origin='psp'`),
   but grants entitlements only when it maps to a known catalog price AND the
   explicit policy `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` is `true`.
   **Default `false`** — fail closed: an entitlement nobody authorized through
   Sandloom must be an operator's explicit decision (e.g. during a migration
   window), never a side effect of a dashboard click; a wrong deny is visible
   and reversible (flip the flag; the stored state is complete), a wrong
   grant is not. Upgrade safety: every row that existed before the policy is
   backfilled `origin='legacy'` (migration `0005_billing`) and keeps granting
   (still subject to a known price); intent-backed rows (`origin='intent'`)
   never depend on the flag. After the backfill the column's default is
   `psp` (ORM and server) — **fail closed**: any later insert without an
   origin (old code during a rolling deploy, a manual `INSERT`, future code)
   is intent-less. A downgrade past `0005_billing` would erase the marker (a
   re-upgrade relabels every row `legacy` = granting), so it refuses while
   `psp` rows exist unless `SANDLOOM_BILLING_DROP_ORIGIN=1`. Tests:
   `test_entitlement_intentless.py`, `test_migration_subscription_origin.py`.
10. ~~**Portal-driven subscription changes.**~~ **Resolved as far as the
    PSP allows — attributed and audited, not gated.** Stripe sends no portal
    reference with a change, so billing cannot tell a portal change from an
    operator's or a lifecycle update; gating on it would either drop the
    PSP's truthful state (billing then disagrees with what Stripe charges)
    or block legitimate operator actions. What holds:
    - every update applies only to the customer that owns the subscription
      (existing owner check) and is evaluated by the entitlement policy (a
      price outside the catalog grants nothing — item 11; origin rules —
      item 9). A portal change cannot create entitlements the catalog does
      not define;
    - billing hands out a portal URL only for an issued portal intent (after
      the authority re-check), and records `issued_at` (migration
      `0009_billing`);
    - a **user-actionable** change (price / plan or `cancel_at_period_end`)
      of an existing subscription for which the customer has **no portal
      intent issued** in the preceding
      `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES` (default 60; +5 min clock
      skew) is audited `billing.subscription.unattributed_change`
      (`required=True`, before/after values) and logged — never blocked.

    What this does NOT guarantee: that an attributed change was made in the
    portal (an operator change inside the window looks the same), that an
    unattributed one was unauthorized (operator / API / dashboard actions
    are legitimate), or anything about status-only lifecycle updates (not
    audited). Owner-scoped: another customer's portal intent never
    attributes. Tests: `billing/tests/test_portal_attribution.py`.
11. ~~**Subscription to an unknown price.**~~ **Resolved — fail closed**: a
    subscription whose PSP price is not in the catalog grants NO
    entitlements. The webhook is not rejected and the row is not deleted: the
    PSP state is stored, the price becomes NULL (on a change from a known
    price the previous — possibly higher — entitlements are withdrawn, not
    retained), an audit record `billing.subscription.unknown_price`
    (`required=True`) and an error log flag it for an operator. Tests:
    `test_entitlement_unknown_price.py`.

    **Upgrade note (operator action).** Items 9 and 11 change WHO is entitled
    on deploy: a row that existed before with `price_id` NULL (a price the
    catalog never knew) loses access at once — list them before upgrading
    (`SELECT … FROM billing_subscriptions WHERE price_id IS NULL AND status IN
    ('active','trialing','past_due')`) and add the missing catalog prices (the
    next subscription event re-resolves the price) or accept the loss.

    **Caveats common to items 9 and 11.**
    - *Claims lag.* Access-token claims (`subscription_status`, `plan_code`)
      minted before a price → unknown change, or before the deploy, keep the
      claim-cheap `Subscribed()` passing until the access token expires
      (`ACCESS_TOKEN_EXPIRE_MINUTES`). This is the documented limit of claim
      gates; the live gates (`SubscriptionActive()`, `Entitled(...)` with
      `min_freshness="live"`, the billing contracts) are authoritative and
      fail closed at once.
    - *Events.* `billing.subscription.created/updated/canceled.v1` carry the
      PSP `status` and — additive within `.v1` — `data.grants_access` (this
      subscription's own grant, by the same predicate as the live check).
      Consumers must use `grants_access` (or the live contract), never derive
      entitlements from `status`; an event from an instance that predates the
      field lacks it — treat it as unknown and ask the live contract.
12. ~~**Refunds / disputes** of credit packs do not reverse the credits.~~
    **Resolved** (`WebhookService._apply_charge_refunded` / `_apply_dispute` /
    `_converge_payment`, `BillingService.reverse_credits`, tables
    `billing_credit_grants` + `billing_psp_payment_states`, migration
    `0008_billing`). Policy:
    - *Link.* Every credit-pack grant records its checkout session, payment
      intent, credits and the price paid (same transaction as the ledger
      entry). Refunds and disputes are matched by `payment_intent`, so
      granting credits for a paid purchase **requires** one: a paid
      credit-pack completion without `payment_intent` grants nothing (no
      credits, no grant row — they could never be reversed), is journaled
      `failed`, audited `billing.credits.grant_refused` (`required=True`),
      logged at ERROR and its intent flagged `followup_at` (test
      `test_paid_credit_pack_without_a_payment_intent_grants_nothing`).
      Subscriptions need no such link: they are not credit grants, a refund
      / dispute of a subscription invoice reverses nothing, and access
      follows Stripe's subscription events.
    - *Out of order.* Every refund / dispute event first records the
      payment's latest state (`billing_psp_payment_states`, keyed by the
      payment intent: cumulative refunded amount (max), the refunding
      charge's PSP customer, currency, dispute state and amount; fields only
      move forward, a closed dispute never reopens) — **whether or not the
      grant exists yet**. Without a grant the event is journaled `processed`
      ("recorded"); when the purchase's completion later creates the grant,
      it applies the recorded state **in the grant's transaction**. Both
      paths take the same lock order — the state row (created-or-locked in
      one statement, `INSERT … ON CONFLICT DO UPDATE SET updated_at = now()
      RETURNING`), then the grant (inserted by the completion, locked by
      refunds / disputes), then the ledger / balance — and the completion
      creates-or-locks the state row BEFORE inserting the grant: its insert waits for a
      concurrent refund's still-uncommitted insert of the same row, so it
      then sees what the refund recorded (a plain `SELECT … FOR UPDATE`
      would miss the uncommitted row under READ COMMITTED and both
      transactions could commit without the reversal; test
      `test_concurrent_first_refund_and_completion_both_converge`, two real
      transactions). A refund delivered
      before the grant is therefore never lost to the event journal's
      dedupe, and Stripe's ~3-day retry window does not matter. A recorded
      state that names another PSP customer / currency than the grant is
      not applied (audited `billing.credits.reversal_refused`).
    - *Ownership.* `charge.refunded`: the charge must name its PSP customer
      and a currency (a charge without either is journaled `failed`; a
      dispute without a currency too), and it must be the
      grant's (like `invoice.paid`) and the one already recorded for the
      payment, else journaled `failed`. A dispute object carries no
      customer; the payment-intent link, recorded from that customer's
      signed checkout, is the ownership.
    - *What is due.* money = cumulative refunded amount + the disputed
      amount while the dispute is `open` (`needs_response` /
      `under_review`) or `lost`, capped at what was paid; credits due =
      ceil(credits × money / paid), capped at the credits granted. So a
      **partial dispute** reverses a proportional share like a partial
      refund, a **refund plus a dispute never reverse more than was
      granted**, and a dispute closed `won` gives its share back. An
      **inquiry** (`warning_needs_response` / `warning_under_review` /
      `warning_closed`: no funds withdrawn) reverses nothing — audited
      `billing.charge.disputed` (`inquiry: true`) for an operator; an
      inquiry that escalates arrives as `charge.dispute.updated` with
      `needs_response` and then reverses. `charge.dispute.updated` is
      handled like `.created` (state only moves forward).
    - *Convergent, append-only.* Each event moves the grant's `reversed` to
      what is due with ONE ledger entry keyed by the PSP event id
      (`reversal:<evt>` / `restore:<evt>`: durable dedupe in the webhook's
      transaction, on top of the event journal); repeated / out-of-order
      events converge (a no-op is journaled `skipped`). Audited
      `billing.credits.reversed` / `.restored` (`required=True`); events
      `billing.credits.reversed.v1` and — for a restoration, distinct from a
      purchase — `billing.credits.restored.v1` (same `data` shape as
      `credits.added.v1`).
    - *Negative balance — decided: allowed.* Credits already spent cannot be
      taken back, so a reversal larger than the balance leaves it NEGATIVE
      (owed). Chosen over clamp-and-record-debt because it keeps ONE truth:
      `balance == SUM(ledger.delta)`, no second debt state that every grant
      would have to settle, and later purchases net the debt with no extra
      code. Spending is unchanged (`amount >= :n`), so a negative balance
      refuses every spend until purchases cover it; only a reversal can
      produce one. The `amount >= 0` CHECK is dropped (its downgrade refuses
      while a negative balance exists). Approved with conditions, each
      pinned by a test (`billing/tests/test_credit_reversal_invariants.py`,
      `test_guards_and_contracts.py`):
      - **Invariant `balance == SUM(ledger.delta)`** per customer, always
        (every balance change writes exactly one ledger row in the same
        transaction; asserted after reversals, restores and spends);
      - spending stays atomic (`UPDATE … WHERE amount >= :n`) and is refused
        while the balance is short — concurrent spends from a negative
        balance all fail, the balance does not move;
      - no overflow: balance, `delta` and `balance_after` are BIGINT
        (9.2·10¹⁸) against at most 10¹² credits per operation
        (`MAX_CREDIT_OPERATION`); an overflow fails the transaction, never
        wraps;
      - the API presents a debt explicitly, not as an error:
        `GET /credits` → `{"balance": -60, "debt": 60, "in_debt": true}`
        (`debt` / `in_debt` additive; UIs should show "you owe N credits");
        the contract and events carry the (negative) balance;
      - `Credits(n)` keeps denying with the usual `402`.
    - *Subscription access on a dispute — unchanged. TEMPORARY policy for
      the first release, not universally right* (see open item 17).
      A dispute does not say which subscription or period it concerns
      without a PSP fetch (the basil API dropped `charge.invoice`), and
      Stripe does not cancel a subscription on a dispute; access keeps
      following Stripe's subscription events (cancellation, `unpaid`). A
      dispute of a payment with no grant (at its creation) is audited
      `billing.charge.disputed`. Refunds of subscription invoices likewise
      do not change access (their state row is recorded and never matched).
    - *Invariants the policy guarantees* (each pinned with real concurrent
      transactions where it matters, `test_credit_reversal_invariants.py`):
      1. **A payment's state is never applied to another customer's grant**:
         a state row is keyed by the payment intent (unique per grant), and
         it is applied only when its recorded PSP customer / currency are
         the grant's (`test_foreign_payment_state_is_never_applied_to_another_grant`,
         `test_refund_from_another_psp_customer_reverses_nothing`,
         `test_early_refund_from_another_psp_customer_is_never_applied`).
      2. **Reordering never lets an older state overwrite a newer one**: the
         state is decided by monotone fields (refunded amount = max) and
         terminal dispute statuses, never by delivery order — `won`
         delivered before `created` / `updated` stays won; a late
         `needs_response` after `won` is ignored; a smaller cumulative
         refund after a bigger one changes nothing
         (`test_won_delivered_before_created_and_updated_reverses_nothing`,
         `test_late_needs_response_after_won_does_not_reopen`).
      3. **Concurrent webhooks never double-reverse, double-restore or
         deadlock**: one lock order (state → grant → ledger) in every path,
         the state row created-or-locked before the grant, one ledger entry
         per event moving `reversed` to what is due
         (`test_concurrent_refund_deliveries_reverse_once`,
         `test_concurrent_won_deliveries_restore_once`,
         `test_concurrent_refund_and_dispute_converge_without_deadlock`,
         `test_concurrent_first_refund_and_completion_both_converge`).

    - *Never reopened, never lowered.* A closed dispute is terminal for the
      payment: a NEW dispute after a closed one is ignored (no dispute id
      is stored — Stripe allows one dispute per charge). `amount_refunded`
      only grows: a lower value (a failed / cancelled refund) never
      restores credits.
    - *Retention.* A payment-state row that never matched a grant
      (subscription-invoice refunds / disputes, pre-`0008_billing`
      purchases) is purged after 90 days (`PAYMENT_STATE_RETENTION`) by
      billing's periodic task (`reconcile_operation_intents`, reported as
      `purged_payment_states`); a completion arrives within days of its
      payment, so such an orphan can never be matched any more. Rows of a
      grant stay (one per purchase). The purge deletes only rows with no
      grant for their payment AND untouched for 90 days; every touch (the
      completion included) bumps `updated_at` in the state-row upsert, so a
      purge that waited for a completion's row lock re-checks the new row
      version and keeps it (test
      `test_purge_never_deletes_the_state_a_concurrent_completion_holds`).
      **Limit:** a completion (or its replay) arriving more than 90 days
      after an orphan refund / dispute finds the early state purged — the
      grant is then made without the reversal (operator follow-up; Stripe
      does not redeliver that old).

    Not guaranteed: grants from before `0008_billing` are not reversed;
    a refund rounds credits up (a 1-cent refund of a 100-credit pack
    reverses 1 credit); a chargeback fee is not modelled. `amount_refunded`
    is kept as a MAX, so a refund that later fails or is cancelled
    (`refund.failed`, `charge.refund.updated`) does NOT give the credits
    back — conservative, an operator restores them by hand;
    `charge.dispute.funds_withdrawn` / `funds_reinstated` are not handled
    (the dispute status drives the reversal). Tests:
    `billing/tests/test_refunds_disputes.py`.
13. ~~**Stored checkout URL has no expiry check.**~~ **Resolved** — the
    PSP's `expires_at` is stored at issue
    (`billing_operation_intents.psp_session_expires_at`, migration
    `0007_billing`); a retry of an issued checkout re-checks the authority,
    then answers `410` (no URL; start a new checkout with a new key) when the
    session expires within a minute. Intents issued before it count as
    expiring 24 h (Stripe's default lifetime) after creation. The intent
    stays `issued`. Tests: `test_issued_intent_stores_the_session_expiry`,
    `test_retry_of_an_issued_checkout_after_its_session_expired_is_410`,
    `test_retry_of_a_legacy_issued_checkout_uses_the_psp_default_lifetime`.
14. **Checkout URL at rest — decided: keep it** (no change). Justification:
    the URL only lets its holder pay that one session — for the tenant's own
    customer, at the catalog price — and the gate honours the payment only
    for the issued intent's own session; it is returned only after the
    authority re-check and never past its expiry (item 13), and it is in no
    log, audit row or cache. Whoever can read `billing_operation_intents`
    already reads far more valuable billing data. The alternatives cost more
    than they protect: re-fetching from the PSP on an issued retry adds a PSP
    call (and a failure mode) to every retry and still hands out the same
    secret; encrypting needs key management for a low-value, ≤ 24 h secret.
    Revisit if checkout URLs ever grant more than "pay this session".
15. ~~**Pending intent after an ambiguous PSP failure + authority loss.**~~
    **Resolved by the reconciler** (#72): the intent stays `pending` (the
    retry is refused before it reaches the intent), and once stale the
    reconciler finds the session by the intent marker, expires it and
    revokes the intent. Test:
    `test_ambiguous_psp_failure_then_authority_loss_is_reconciled`.
16. ~~**Subscription gate does not compare price or count.**~~ **Resolved** —
    no PSP-side fetch is needed: the subscription event carries its items.
    A NEW subscription for an issued checkout intent must have exactly one
    item priced at the intent's catalog `price_id` with `quantity` 1
    (billing creates every line item with quantity 1; a missing quantity is
    read as 1), and the intent must not
    have yielded a subscription yet (`billing_subscriptions.intent_id`,
    migration `0006_billing`, partial UNIQUE index — two concurrent deliveries cannot both insert; the
    loser rolls back and its redelivery is refused). A credit-pack completion
    must name the intent's price too. A mismatch is handled like every other
    unauthorized completion: not applied, journaled `failed`, audited
    `billing.intent.unauthorized_completion` (`intent_status` e.g.
    `issued (price mismatch)`), intent flagged `followup_at` (still
    `issued`). Tests: `billing/tests/test_subscription_intent_match.py`.

17. **Fraud / security hold (production billing).** Item 12 leaves
    subscription access unchanged on a dispute — a TEMPORARY first-release
    policy. Production billing needs a separate **hold**: driven by a
    confirmed risk signal (a lost dispute, a fraud review, an early fraud
    warning confirmed by an operator), kept apart from the PSP subscription
    status (which must stay the PSP's truthful state, items 9 / 11), and
    applied by the entitlement predicate. Not built. Today's emergency stop
    is `block_user` (`sandloom.core.di`): it revokes the user's sessions and
    blocks every authenticated request at once.

## What must stay strict (always a live RPC / local call)

- classes RS, W, F and A — every `command=True` contract method and every
  non-`GET`/`HEAD` route;
- `BILLING_MANAGER_ROLE` gates (checkout, customer portal), role management,
  ownership transfer, member removal, invitations;
- money: `Credits(n)`, `SubscriptionActive()` and any fact declaring
  `min_freshness="live"`;
- token issuance: `tenancy.claims.v1` (critical contributor) is never cached;
- tenancy's own write-side authorization (`lock_actor_role`, actor row
  `FOR SHARE`), local and inside the write transaction;
- routes under `strict_revocation()` / `AUTH_REVOCATION_STRICT_PATH_PREFIXES`.

## Consequences

- The class of every platform route is explicit; billing's read routes can be
  opted in individually later without touching the financial routes, and a
  test makes any such change deliberate.
- Measuring a cache costs nothing by default (the shadow is off; one global
  check per contract read) and changes no answer when on.
- Three authorization gaps (G1–G3) were named instead of hidden behind
  "strict", and are now addressed (#69): strictly serializable where the
  authority and the action share a database; serializable with a narrowed
  (not closed) window across services — see "Guarantees, precisely".
- If (a) is adopted later: the contract runtime gains a cross-request cache
  and a broadcast channel, the split conformance matrix an invalidation
  barrier, and the security model a second bounded-staleness axis under the
  same ≤ 5 s, class-R-only rules as revocation freshness.

## Alternatives rejected

- **Stale-if-error** (serve expired entries while tenancy is down): turns an
  outage into an unbounded staleness window for authorization.
- **Consumer-side cache with no provider declaration:** each caller would
  guess TTLs and invalidation keys for data it does not own.
- **Durable consumer group for invalidation:** replicas of one service share
  the group, so only one replica would drop its entry.
- **Deciding the cache now on the old 1.4 ms figure:** the call costs ≈ 0.36 ms
  since #62; without a measured hit rate the saving is unknown.


### Platform owner console routes

The explicit owner directory `/api/v1/platform/tenancy/{organizations,members}`,
all `/api/v1/platform/billing/*` reads, and the kernel's per-module `audit`,
`services`, `outbox/dead`, `streams/dlq`, `consumers`, `projections`,
`contracts`, `modules` and `settings` reads are **RS**, always strict.
The kernel's per-module `POST outbox/requeue` and `POST streams/replay` are
**A**: fail-closed `Idempotency-Key`, required audit (in the requeue's own
transaction; written before the non-transactional Streams replay, which runs
last) and the before-commit revocation check. They require the platform `admin` role
from a successful identity claims contributor; tenant membership grants nothing.
`PATCH /api/v1/platform/billing/products/{record_id}` is **A** and
`POST /api/v1/platform/billing/customers/{record_id}/adjust-credits` is **F**.
Both attach the before-commit revocation check. The adjustment's unique ledger
operation key, balance effect and required audit share one UoW; payload-mismatched
replays conflict. Provider money/subscription operations are not local row edits.

## Related reading

- [Current route policy](../concepts/route-policy.md)
- [Contracts](../concepts/contracts.md)
