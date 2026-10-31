---
title: Security Model
---

# Security Model

This page consolidates Sandloom's security mechanisms into one overview. Each links to
the concept page with the full detail.

## Authentication

- **JWT access/refresh tokens.** Access tokens are short-lived (default 15 min);
  refresh tokens are longer-lived (default 14 days) and stored **hashed**.
- **Password hashing.** argon2id with a configurable work factor
  (`PASSWORD_HASH_PROFILE` = `strong` default m=64 MiB/t=3/p=4, or
  `owasp_min` m=19 MiB/t=2/p=1, or explicit `PASSWORD_HASH_*` values). The
  trade-off: the work factor is what an attacker who steals the hashes pays
  per guess, and also what the deployment pays per login (~120 ms CPU for
  `strong` vs ~21 ms for `owasp_min` on an M1 Max). `owasp_min` is the OWASP
  Password Storage Cheat Sheet minimum and is a defensible choice when login
  CPU dominates; `strong` buys ~5–6× more attacker cost per guess. Below the
  OWASP minimum production refuses to start unless `SANDLOOM_ACCEPTED_RISKS` lists
  `weak_password_hash` (logged on every start). A changed setting migrates users on their next
  successful login (re-hash in the same UoW, never blocking the login); see
  `docs/operations/operator-reference.md` → *Password hashing*.
- **`token_type` discrimination.** Every access JWT must carry
  `token_type: "access"`; guards reject tokens without it, so a refresh token can
  never be used as an access token.
- **Algorithm.** `JWT_ALGORITHM` is `HS256` (default), `HS384`, `HS512`
  (shared secret) or `EdDSA` (Ed25519), `RS256` (RSA ≥ 2048 bit), `ES256`
  (EC P-256) — asymmetric. Anything else (`none`, …) is rejected at load.
  - **Symmetric (monolith / dev default).** `JWT_SECRET` signs *and*
    verifies: every process that verifies tokens can also mint them. Fine for
    one process (one trust domain).
  - **Asymmetric (required between services).** Only the **issuer** — the
    process loading a module declared `issues_tokens=True` (identity) — holds
    `JWT_PRIVATE_KEY`; every service verifies with `JWT_PUBLIC_KEYS`
    (`{"<kid>": "<PEM public key>"}`, identical everywhere) and **cannot
    mint**: compromising tenancy or billing no longer yields tokens for any
    user. Both are `SECRETS_DIR`-aware (`jwt_private_key`, `jwt_public_keys`
    files); `\n`-escaped PEMs are accepted in env vars. Generate a pair with
    `sandloom keys generate [--alg EdDSA|RS256|ES256] [--kid K] [--out-dir DIR]`.
  - **Algorithm confusion is impossible by construction.** Each asymmetric
    algorithm is bound to one key type (checked at boot by
    `sandloom.core.app_factory.enforce_configuration` — an RSA key under `EdDSA`, a 1024-bit RSA key, an EC key on
    another curve all fail startup); verification accepts exactly
    `JWT_ALGORITHM` (never `HS*` / `none`) with the ONE public key the
    token's `kid` header names — a token without a `kid`, with an unknown
    `kid`, or HMAC-signed with the public PEM is rejected. `JWT_PREVIOUS_SECRETS`
    and `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEYS` are mutually exclusive with the
    other family.
  - **Startup role check** (critical — the process refuses to serve): an
    issuer without `JWT_PRIVATE_KEY`; a verify-only process holding
    `JWT_PRIVATE_KEY` or `JWT_SECRET`; and, in **production**, a split process
    (one that loads no issuer or binds remote services) on a shared HS secret
    — unless `SANDLOOM_ACCEPTED_RISKS` lists `shared_hs_secret_split` (not
    recommended; refused under `multi_tenant_strict`). In
    asymmetric mode `JWT_SECRET` is issuer-only: identity keys its emailed
    verification/reset tokens with it and never signs or verifies a JWT.
- **Required claims.** Tokens must carry `exp` and `iat`.
- **Key rotation (symmetric).** Move the old secret into `JWT_PREVIOUS_SECRETS`
  (JSON list; still accepted for verification, never used to mint) when rotating
  `JWT_SECRET`, keep it for at least `ACCESS_TOKEN_EXPIRE_MINUTES`, then drop it.
  Optional `JWT_KID` is stamped as the `kid` header; a token whose `kid` matches
  is verified with `JWT_SECRET` only. In production previous secrets must be as
  strong as the current one, and the current one may not appear in the list.
- **Key rotation (asymmetric) — runbook, no forced logout.**
  1. `sandloom keys generate --kid jwt-2` (store the private key in the secret
     store of the issuer only).
  2. Add `"jwt-2"` to `JWT_PUBLIC_KEYS` **everywhere** next to `jwt-1` and
     roll every service (both kids now verify).
  3. Switch the issuer: `JWT_PRIVATE_KEY` = the new key, `JWT_KID=jwt-2`
     (startup checks the private key is the public half of
     `JWT_PUBLIC_KEYS["jwt-2"]`).
  4. After `ACCESS_TOKEN_EXPIRE_MINUTES` + `JWT_LEEWAY_SECONDS`, remove
     `jwt-1` from `JWT_PUBLIC_KEYS` everywhere. A compromised key is removed
     at once instead (its tokens stop verifying immediately).
  Migrating HS → asymmetric invalidates outstanding access tokens only
  (refresh tokens are opaque): clients refresh once.
- **Revocation.** `revoke_user_tokens(user_id)` (from `sandloom.sdk.security`)
  writes an `after` value into the framework-owned subject-state record
  `sandloom:auth:subject:{user_id}`; every access-token check then rejects a token
  whose issue instant (`iat_us`, microseconds) is `<=` that instant with `401`
  ("Token has been revoked"); a token without `iat_us` is revoked by any mark
  of its subject. It takes effect immediately (a cache write, not part of the
  UoW). When the cache is unreachable the check fails **closed** (`503`) or
  open per `AUTH_REVOCATION_FAIL_CLOSED` (unset = closed in prod, open
  elsewhere). Custom guards call `ensure_token_not_revoked(request, claims)`.
  The record holds one of two values, `v1:after:<seconds.micros>` (revoked up
  to that instant, written with a TTL) or `v1:blocked:<seconds.micros>`
  (blocked, no TTL). The order is total: every `blocked` value ranks above
  every `after` value, and within a kind the instant decides, compared
  exactly. The record is **monotonic**: it is written with the cache's
  `set_subject_state` (feature `subject_state`: one atomic Lua round trip on
  Redis; a read / `add` / `compare_and_set` loop on drivers without the fused
  op), so a later mark is never replaced by an earlier one, a revocation
  never lifts a block, and concurrent revocations of one subject never fail
  each other: the stored mark ends at the maximum and every writer reports
  success. Only a cache error fails a write. The TTL is set only when the
  value is written; an existing equal/later value keeps its own TTL. A
  security cache driver without `compare_and_set` is refused for these
  writes (the write fails → retried / durable applier, `503` for
  `require_immediate`): a plain overwrite could lower the mark or lift a
  block.
- **Revocation durability: the commit → access-stops window.**
  `revoke_user_tokens_on_commit(user_id, reason=...)` ties a revocation to a
  database change. It always writes `core.auth.tokens_revoked.v1` into the
  change's transaction (identity's durable consumer `sessions@tokens_revoked`
  applies it in whatever service hosts identity — idempotent, monotonic) and
  writes the mark after commit (retried). On that **default** path the mark
  can trail the commit: while the cache is down tokens are refused (`503`,
  fail-closed) or accepted (fail-open, dev), and once it is back an old token
  is ACCEPTED until the applier runs. Worst case after recovery, with the
  default knobs: a crash between commit and dispatch ≈ relay grace 3 s + relay
  idle sleep ≤ 6.5 s; a bus outage (cache and bus on one Redis) ≈ relay
  circuit cool-down ≤ 30 s + idle sleep ≤ 6.5 s; a cache-only outage: the
  applier is registered `dead_letter=False`, so the event is retried FOREVER
  with the transport's capped backoff (1, 2, 4 … ≤ 60 s) and is never parked
  in the DLQ (before this change it was dead-lettered after 5 deliveries,
  ≈ 15 s, and lost until a manual replay) — so ≤ 60 s after recovery; no relay
  (`OUTBOX_RELAY_ENABLED=off` with the accepted risk `relay_disabled` and no
  relay worker) — unbounded. While a
  revocation is stuck the applier logs an ERROR per attempt,
  `auth_revocation_pending_seconds` reports the age of the oldest one and
  `/ready` answers `503 degraded` (`revocations_pending: true`; per process —
  under gunicorn the worker whose applier is retrying reports it, and the
  `livemax` gauge aggregates the workers); `/health`
  (liveness) is unaffected. Only a payload that can NEVER apply (malformed)
  is dead-lettered — at once, CRITICAL, `auth_revocation_malformed_total`.
  **`require_immediate=True` closes the window**: the mark is written
  synchronously BEFORE commit, and if that write fails the change is refused
  with `503` (`RevocationUnavailableError`, `auth_revocation_immediate_refused
  _total`) and rolls back — a committed change always has its mark, so the
  first request checked after the commit is rejected, in every process
  (measured: 0 ms in-process; split identity | tenancy with the tenancy write
  lost, the outbox path alone rejected the token everywhere in ≈ 0.2 s). After
  commit a second mark `max(mark, commit time + skew)` revokes a token an
  issuer minted between the early mark and the commit (its claims predate the
  change); tokens the revoking request itself mints afterwards are stamped
  past it as long as the UoW commits within one skew
  (`AUTH_REVOCATION_CLOCK_SKEW_SECONDS`) of the call. A slower commit revokes
  them too — an issuer's stale-claims token minted in that gap cannot be told
  apart from them at the subject mark, so the conflict is resolved fail-closed
  (the client re-authenticates), with a WARNING and
  `auth_revocation_remark_overtook_floor_total`. Trade-offs: during a cache outage these changes are refused (with
  fail-closed nobody can authenticate then anyway), and a rollback AFTER the
  early mark logs the user out (the safe direction) — call it as the last
  fallible step. The standard modules use it for every security event: member
  removal / leave, role change, ownership transfer (both parties),
  organization deletion, password change / reset, role revocation,
  deactivation and `block_user`. A process-local `/ready` gate or a
  drain-before-serving step was rejected: neither protects requests served by
  OTHER processes, which still read no mark. Residual: Redis losing an
  acknowledged write (failover / restart without persistence) re-opens the
  window until the outbox event is applied (bounded as above); keep the
  revocation keys on a persistent, non-evicting Redis.
- **Corrupt records fail closed.** A subject record that exists but is
  outside the value domain rejects every token of the subject
  (`auth_revocation_corrupt_mark_total`, logged) instead of being ignored;
  the issuer's live check (`ensure_user_not_blocked`) repairs it (an `after`
  value of `now + skew`, written with `set_subject_state`), so the next login
  works. The repair never lowers a readable mark, so every token issued
  before the repair stays revoked. "Corrupt" means "outside the value
  domain" for readers and the writer alike.
- **A live session check on every route.** Every authenticated request reads
  the subject record live (one `GET`, memoised per request), so a revocation
  or a block applies on the very next request in every process. There is no
  per-route freshness setting, no per-process copy of the record and no
  path-prefix setting
  ([ADR-0009](../adr/0009-route-policy-and-session-freshness.md)).
  Authenticated writes also re-check the record before COMMIT (see
  "Authority at commit" below). Each module's committed
  `routes.snapshot.json` (method, path, auth, policy; checked by
  `assert_route_snapshot`, SANDLOOM-T001) is the module's reviewed approval of
  its routes' resolved policy. See [Route policy](../concepts/route-policy.md).
- **Emergency user block.** `block_user(user_id, reason=..., actor_id=...)` /
  `unblock_user(...)` (from `sandloom.sdk.security`) — for a compromised account
  that must stop NOW. The block is a `blocked` value of the same subject
  record, which every authenticated request reads live; it rejects tokens
  minted after the block too (`401 "Account blocked"`). Both calls must run
  inside a Unit of Work: they write a mandatory audit record
  (`auth.user.blocked` / `auth.user.unblocked`, actor + reason,
  `required=True`) before touching the cache; a cache outage raises
  `BlockUnavailableError` (503) and the audit rolls back with the request.
  `block_user` writes the record LAST (after the audit, the immediate token
  revocation and the event), so a failing earlier step leaves no unaudited
  block; `unblock_user` replaces the `blocked` value by compare-and-set with
  an `after` value only after its UoW commits (`on_commit`), never by a
  delete — a rolled-back unblock, or a post-commit write that fails (logged
  as an error), leaves the user blocked (fail-safe). Keep Redis on a
  non-evicting policy for these keys. Issuance is refused as well:
  identity's password / 2FA login, SSO (and SSO second-factor) completion
  and the refresh-token exchange call `ensure_user_not_blocked(user_id)` — a
  live read — before any side effect (no token minted, no refresh rotation,
  the SSO challenge not consumed), answering the same
  `401 "Account blocked"`; a cache outage follows
  `AUTH_REVOCATION_FAIL_CLOSED` (503 when closed). A custom issuer must call
  it too. The block also ENDS the user's sessions once its UoW commits:
  every access token issued so far is revoked
  (`revoke_user_tokens_on_commit`) and `core.auth.user_blocked.v1` is
  published through the outbox; identity's durable consumer
  (`identity:sessions@user_blocked`, local or in its own service) revokes
  every refresh token. Lifting the block therefore requires a fresh login.
- **Cost per request.** One Redis round trip (a `GET` of the subject
  record). Several guards on one request share one check.
- **Cache outage.** `AUTH_REVOCATION_FAIL_CLOSED` governs the check (unset =
  closed in prod). Fail closed: the request answers `503`. Fail open
  (default outside prod): the token is accepted and a warning logged.
- **Issuer/audience binding.** Optional `JWT_ISSUER` / `JWT_AUDIENCE` — when set,
  tokens carry the claim and verification rejects another issuer/audience.
- **Clock skew.** `JWT_LEEWAY_SECONDS` tolerates minor drift on `exp`/`nbf`/`iat`.
- **2FA.** TOTP plus backup codes; TOTP secrets are **encrypted at rest** with
  `TWOFA_ENCRYPTION_KEY` (comma-separated, newest first, for rotation). It is
  required in production when identity is loaded; only outside production does
  an unset key fall back to one derived from `JWT_SECRET`.
- **SSO.** OAuth2 with CSRF-protecting one-time state tokens stored in Redis with
  a TTL. See the [SSO guide](../guides/sso-setup.md).

## Authorization

The composable [authz algebra](../concepts/authorization.md) (`require(...)`,
`enforce(...)`) is the single enforcement path. Policies evaluate cheapest tier
first (claims → resource → live), short-circuiting. RBAC roles/permissions are
JWT claims contributed by identity; billing entitlements resolve via the
`billing.entitlements.v1` contract.

Every minted token records claim provenance in `claims_sources` /
`claims_sources_failed`; a claim whose owner is in `claims_sources_failed` is
treated as unknown and escalates to a live check, and a failing *critical*
contributor aborts minting (`503`) — see
[Claims contributors](../concepts/route-policy.md#token-claims).
Contributors cannot override the identity claims (`sub`, `email`,
`email_verified`, `token_type`).

### Authority at commit (admin and money effects)

A live read is fresh at read time only. Every authenticated write therefore
re-checks the caller's session record before the COMMIT of every unit it
commits ([ADR-0009](../adr/0009-route-policy-and-session-freshness.md)).
Where a wrong *allow* is not recoverable (admin and money effects), the
standard modules also make the authorization itself hold when the effect
commits:

- **Tenancy (admin writes).** Every write that authorizes on the ACTING member —
  role change, member removal, organization update, invitation create /
  revoke — reads the actor's membership row `FOR SHARE` in the write's own
  transaction (`MembershipService.lock_actor_role`); the target row is
  `FOR UPDATE`. A concurrent demotion / removal of the actor either committed
  first (the locking read sees it: `403` / `404`) or waits for the write to
  commit. Member rows are locked in `user_id` order (organization row first
  where one is locked), so actors acting on each other wait instead of
  deadlocking. Ownership transfer and deletion re-check the owner under the
  organization row lock.
- **Identity (admin writes).** RBAC and user-administration mutations keep the
  claim-cheap `require(Role("admin"))` and add `admin_in_transaction`: in
  the request's own Unit of Work it takes the RBAC administration lock
  (`pg_advisory_xact_lock`, one global order for admin mutations) and reads
  the actor's `admin` assignment and active user row `FOR SHARE`. A token
  that still claims `admin` after the role was revoked is refused (`403`)
  even before its revocation mark is seen; a revocation committing while an
  admin request runs is serialised behind it.
- **Billing (money effects) — operation intents.** Checkout and customer portal
  call an external PSP that no transaction can undo. They authorize on
  `get_member_state` (role AND the per-(org, user) membership `version`) and
  then run under an authorized **operation intent**
  (`billing_operation_intents`): the intent is committed (`pending`) BEFORE
  the PSP call and provides its idempotency key (`sandloom:intent:<id>`), so a
  timeout is reconciled by a retry with the same key (same session), never by
  a blind new call. Right before the one-statement issue transaction (no
  billing connection held during it) the final re-read checks the membership
  state (`reverify_authority`, contract-read memo cleared: role lost → `403`,
  other change → `409`) and the caller's revocation / block marks (`401`).
  Any refusal marks the PENDING intent `revoked` (audited), **expires** a
  checkout session at the PSP and never returns the URL (a portal session
  cannot be expired at Stripe; its URL is never handed out again). These
  routes' idempotency guard stores no response (`store_response=False`): a
  retry goes through authorization and the intent — an issued checkout
  returns its stored URL after a fresh re-read, an issued portal request is
  `409`; nothing revokes or rewrites an issued intent. The webhook applies a credit grant or a new
  subscription only for an `issued` intent; a completion for a revoked /
  unknown one is not applied, audited (`billing.intent.unauthorized_completion`)
  and flagged for an operator. Each step is its own short `open_uow()`
  transaction (the intent must survive the failed request); the routes hold
  no request session, so nothing is nested and nothing spans the PSP call.
  The operation counts as authorized at the issue COMMIT, against tenancy's
  committed state **as of the final re-read** (a live read of the membership
  `version`, compared with the one captured at authorize, then of the
  session marks); a tenancy change committed after that read and before
  billing's COMMIT is not seen. Every PSP call of one intent reuses its key. Billing
  and tenancy may be separate databases, so the intent is not atomic with
  the membership row: the guarantee is **serializable, not linearizable** —
  a withdrawal committing between the final re-check and the issue COMMIT is
  not detected (the window is narrowed from the whole request, PSP call
  included, to that gap; it is not closed). A payment completed between a
  session's creation and its expiry (leaked URL) is caught by the gate, not
  prevented; refunding is manual. Sandloom guarantees the URL is never handed out
  unless the intent is issued; it does NOT guarantee that no PSP session
  exists — expiring is a compensation that can fail (counted as
  `billing_intent_expire_failed`, recorded on the intent and retried by the
  periodic reconciler, bounded, then left to an operator; the gate is the
  backstop meanwhile), and a `pending` intent whose client never retries is
  closed by the reconciler only after a threshold (its session, if any, is
  then expired). Owner outages fail closed (intent revoked,
  session expired) except the revocation store in fail-open mode (the
  default outside prod), where the mark re-check accepts
  ([ADR-0003, "Guarantees, precisely"](../adr/0003-fewer-sync-rpc-membership-reads.md#guarantees-precisely)).
- **Cross-service authority.** Where the authorizing fact is owned by
  ANOTHER service — tenancy's `POST /invitations/accept` grants membership
  on identity's verified e-mail claim; billing's intent routes on the
  caller's own session — no row lock can hold it until the commit. The
  automatic commit-time re-check covers them: the caller's subject record is
  re-read LIVE right before COMMIT of every committing unit, and a mark seen
  rolls the write back (`401`). A route with a commit obligation beyond that
  (a read that must still hold when an empty unit commits, for example)
  declares `at_commit()` (from `sandloom.sdk.web`), which checks every unit the
  request opens; code that opens its own unit registers
  `subject_commit_check(request)` with `before_commit`. Operation intents
  call `release_check(...)` before handing out the result. Every authority
  withdrawal writes its mark BEFORE it commits
  (`revoke_user_tokens_on_commit(require_immediate=True)`, `block_user`) and
  reads nothing the action writes, so no mark seen means the history is
  serializable with the action first. It is **not** linearizable: a
  withdrawal that writes its mark and commits between our final read and our
  COMMIT is not seen — the window is narrowed to that gap, not closed
  (a fence that makes the withdrawal wait for in-flight actions would close
  it; not implemented). With the revocation store fail-open (the default
  outside prod) an outage makes the re-check accept. Same-database authority
  (tenancy G3, identity G2 row / advisory locks) is strictly serializable.
  A route that revokes its own caller needs no opt-out: self-revocation is
  "check, then revoke" (the live check runs at the self-revoking call, and
  the COMMIT then skips that subject; a foreign revocation landing before the
  call is still seen).
  The re-check follows local request, named-database and local command UoWs.
  A remote command under an armed re-check carries the subject in the
  MAC-covered RPC context, and the provider runs the same check before its
  own COMMIT; reads are unchanged.
- **RPC parity: what crosses a call, both ways.** A contract call made for
  an end user carries the *caller facts* in its MAC-covered context (`usr`:
  the subject, `iat_us`, `auth_time`, whether the request re-authenticated,
  whether the commit-time re-check reaches command units, the request's
  operation key and whether its client sent an `Idempotency-Key`, and
  whether a committed unit of the request already revoked its own subject,
  so the provider's re-check skips it exactly as a local unit's does). The
  provider binds them as the call's request scope, so every check reads
  them exactly as for a local call (billing's personal account, the command
  dedupe scope, step-up, money dedupe, the re-check), and its own nested
  calls carry them on, hop after hop. No facts means a system call, never a
  lost user. Every response the provider produced after running the call,
  success or error, carries the *call effects* (`fx`, MAC-bound with the
  body): the risks the execution observed, its own remote calls left in
  doubt (at any depth), the subjects it self-revoked, a re-authentication
  and the step-ups it left pending. The caller merges them before it decodes
  the result or raises. A command replay (its dedupe record keeps the first
  execution's effects) marks the caller alike: risks, the subject's
  self-revocation and pending step-ups, never a re-authentication (a replay
  proves none) nor a call in doubt (a fact about a unit that was running,
  not an effect of the committed command). The facts are asserted by the
  calling service (signed with its Ed25519 key on commands), the same trust
  as the tenant it asserts.
- **A remote call in doubt closes the intent windows.** An attempt the
  provider never answered (a timeout, a transport failure) may still be
  running its unit there: the provider does not abort a unit whose client
  went away. On an operation-intent route, `external_call()` and
  `release_check()` then refuse to open (503 SANDLOOM-R008, fail closed); the
  client retries the request with its `Idempotency-Key`. This holds at any
  depth: a provider that made the unanswered call (and caught its failure)
  reports it back in `fx`, and every caller up the chain merges it.
  Each HTTP request starts a fresh authority/UoW context, including nested
  in-process ASGI requests and internal RPC. On return, the caller's context
  is restored. Local contract calls stay within their caller's request context.
  Issuance timestamp floors are likewise request-local.

## Service-to-service trust (internal RPC)

There is exactly one internal-RPC authentication, the pair MAC of
[ADR-0004](../adr/0004-rpc-pair-mac-request-authentication.md)
(`sandloom.core.rpc_pair_mac`, `sandloom.core.rpc_identity`):

| Caller proves | Provider allows | A compromised service can… |
| --- | --- | --- |
| its own identity: an HMAC under the per-pair key only it and the provider can derive (X25519 + HKDF), over caller, audience, kids, method, target, nonce, context, body digest; **commands** also carry its Ed25519 signature (`RPC_COMMANDS_REQUIRE_SIGNATURE`, on in prod) | only `RPC_ALLOWED_CALLS[svc]` (deny by default) | call only what it is wired to call; as a provider, forge "calls from C" **to itself only**; it cannot derive another pair's key |

`INTERNAL_API_TOKEN` guards only the ops endpoints (`/diagnostics`,
`/contracts`, `/metrics`, `/health` details) and never authenticates
`/_sandloom/rpc`; `INTERNAL_ENDPOINTS_OPEN_IN_DEV` never opens it.

- **Keys.** Every service that serves or calls RPC holds its own DEDICATED
  X25519 key `RPC_DH_PRIVATE_KEY` (`SECRETS_DIR` file `rpc_dh_private_key`)
  with kid `RPC_DH_KEY_ID`, as `RPC_SERVICE_ID` (default `SANDLOOM_SERVICE_NAME`;
  set it explicitly so a service's worker/beat share its identity); the
  public halves (`RPC_SERVICE_DH_KEYS`, `{"<service>": {"<kid>": "<PEM>"}}`)
  are identical everywhere. A process with `SERVICE_BINDINGS` refuses to
  start without its X25519 key; a provider without one refuses every RPC
  call; a monolith (no bindings) needs no RPC keys. Each calling service also
  holds its Ed25519 `RPC_PRIVATE_KEY` (`SECRETS_DIR` file `rpc_private_key`)
  with kid `RPC_KEY_ID` to sign its commands; `RPC_SERVICE_KEYS` — public,
  identical everywhere — is what providers verify with. A pure provider
  holds no signing key. Every `SERVICE_BINDINGS` entry pins its provider
  with `"service_id"`. Generate: `sandloom keys generate --purpose rpc --service
  <id>` (Ed25519 + X25519) or `--purpose rpc-dh` (X25519 only). These checks
  run at boot (`sandloom.core.app_factory.enforce_configuration`; `sandloom doctor`
  check `config/boot`).
- **Authorization.** `RPC_ALLOWED_CALLS` (`{"<service>": ["<contract>", …]}`
  or per-method `{"<service>": {"<contract>": "*" | ["<method>", …]}}`),
  identical everywhere, deny by default. It is DERIVED from the wiring: a
  caller's entry is exactly the contracts its `SERVICE_BINDINGS` bind —
  `sandloom keys allowlist --bindings <svc>='<SERVICE_BINDINGS>' …` prints it, a
  caller whose own entry misses one of its bindings refuses to start, and
  `test_deploy_profiles` pins it for the shipped profile. Describe and
  readiness probes are authenticated too (allowed when any method of the
  contract is).
- **Outcomes.** No RPC credential → `404` (hidden); a request without the
  pair MAC → `401 rpc_auth_required`; a MAC that does not verify (unknown
  service or kid, wrong audience, a tampered body, target or context) →
  `401 rpc_auth_invalid`; a context outside `RPC_AUTH_MAX_SKEW_SECONDS` →
  `401 rpc_auth_expired`; a replayed nonce → `401 rpc_auth_replayed`; a
  verified caller not allowed the contract/method → `403
  rpc_caller_forbidden` (logged); a command without a valid signature → `401
  rpc_auth_signature_required` / `rpc_auth_invalid`.
- **Rotation — per service, independent.** X25519: publish the next kid in
  `RPC_SERVICE_DH_KEYS` everywhere, switch `RPC_DH_PRIVATE_KEY` +
  `RPC_DH_KEY_ID` (the previous private key may stay in
  `RPC_DH_EXTRA_PRIVATE_KEYS` until callers address the new kid), then drop
  the old kid. Ed25519: 1) add the next kid to `RPC_SERVICE_KEYS`
  everywhere; 2) switch that service's `RPC_PRIVATE_KEY` + `RPC_KEY_ID`;
  3) remove the old kid. No other service changes keys.
- **Pair MAC properties.** Audience-bound by construction (the key and the
  tag name the provider), 128-bit per-attempt nonce with a replay guard
  (`RPC_REPLAY_GUARD`), a 60 s window (`RPC_AUTH_MAX_SKEW_SECONDS`), verified
  over the raw bytes before anything is decoded. What it gives up versus a
  per-call Ed25519 signature: third-party non-repudiation (unused in Sandloom),
  and the X25519 key becomes a signing-grade secret (its leak lets the holder
  forge that service's read requests to all its peers — store and rotate it
  like `RPC_PRIVATE_KEY`).
- **Key-compromise impersonation (normative).** A leak of a **provider's**
  X25519 private key = forged requests **from any caller** to that provider:
  the pair key is symmetric, so the holder derives every caller's key
  towards it. A provider's X25519 key is an **"all inbound rights"** secret.
  Sandloom limits the damage to reads: COMMAND calls also need the caller's own
  Ed25519 signature (`RPC_COMMANDS_REQUIRE_SIGNATURE`; `auto` = on in prod,
  off in dev/test; `off` refused under `multi_tenant_strict`), which the
  provider never holds; the signature covers the attempt's nonce, so a
  captured signed command cannot be replayed under a fresh MAC. Forged reads
  remain possible (any caller, any tenant) unless
  `RPC_COMMANDS_REQUIRE_SIGNATURE=all` (every call signed, Ed25519 cost).
  Retired keys left in `RPC_DH_EXTRA_PRIVATE_KEYS` after they are
  unpublished keep this exposure alive: they are warned about at startup
  (refused under `multi_tenant_strict`); finish a rotation within 24 h.
  Every provider-side rejection is a `503` at the caller (never the
  provider's 401/403). Full threat model, rotation and startup checks:
  ADR-0004.
- **Residual trust.** The identity is per *service*, not per module or
  request: the code inside one service is one trust domain (see the
  normative trust model under Tenant RLS below). The tenant in a context is
  asserted by the (authenticated) caller; a caller allowed a tenant-scoped
  contract can name any tenant — scope sensitive contracts by method.

### Response binding — threat model

Normative statement: **a caller accepts a response only when it answers THE
request it sent and is keyed by the provider the binding pins; the binding
authenticates the response.** Mechanism and wire format:
`docs/SERVICE_RPC.md`, "Response binding" (and the
[Service RPC guide](../guides/service-rpc.md)).

| Threat | Outcome |
| --- | --- |
| A client / pool / pipelining bug hands one call another call's response | excluded (nonce + MAC over body, status, request) |
| An on-path party injects a forged response (no key) | rejected (`unbound` / `unkeyed` / `mac_mismatch`) |
| A peer replays a request under the caller's next nonce | its answer never verifies (the MAC covers the request's own authentication) |
| Another registered service answers in the provider's place | rejected: keyed by the pinned `service_id` only (`service_mismatch`) |
| Reading request/response contents | not prevented |

- **Keys.** A per-pair key = HKDF over the X25519 ECDH of the two services'
  DEDICATED static X25519 keys (`RPC_DH_PRIVATE_KEY`, public halves in
  `RPC_SERVICE_DH_KEYS`) — separate from their Ed25519 signing keys (no
  cross-protocol key reuse), all crypto from the `cryptography` library,
  domain-separated from the request MAC key. It never travels, so it
  authenticates responses even on a plaintext network. The provider keys a
  response only for a caller whose request MAC verified; a request that did
  not authenticate is answered unkeyed, which the caller always refuses (an
  unkeyed error answer surfaces as `503 RemoteServiceUnavailable` carrying
  the provider's error code). No non-secret value (service id, kid, nonce)
  is ever used as a key.
- **Not a substitute for TLS/mTLS.** Binding gives no confidentiality.
  Production deployments run internal RPC over TLS (HTTPS `base_url`s or an
  mTLS mesh) or on a trusted, isolated network segment; binding is the
  protocol-level guarantee that holds *in addition*, independently of the
  HTTP client's connection handling.

## Tenant isolation

[Fail-closed tenant scoping](../concepts/multi-tenancy.md): `TenantScoped` models
are auto-filtered to the bound tenant; the explicit helpers
(`scoped`, `tenant_filter`, `current_tenant_id`) raise `MissingTenantError` if no
tenant is bound. Deliberate cross-tenant access requires an explicit
`with unscoped():`. The ORM guard also covers joins, subqueries, relationship
loads and `Session.get()`; optional PostgreSQL Row-Level Security
(`tenant_rls` database option + `enable_tenant_rls` in migrations) enforces the
same rule for raw SQL — within the limits of the
[RLS threat model](#tenant-rls-threat-model) below. Internal RPC binds a tenant
only from a signed caller context — a bare `X-Tenant-Id` on `/_sandloom/rpc` is
rejected.

## Tenant RLS threat model

Row-Level Security is opt-in (`INFRA={"database": {"<name>": {"tenant_rls":
true}}}`); nothing in the framework or the standard modules enables it by
default — except the `multi_tenant_strict` production profile, which requires
it on every database and checks every tenant table at startup (see
[Production profile](#production-profile-the-multi-tenant-trust-model-as-a-contract)). This section states precisely what it does and does not guarantee.

### Two trust levels — the deployment decides which one applies

**(a) Business-module code is trusted (it may be buggy, not hostile).** The
ORM guard (`TenantScoped` auto-filtering, fail-closed helpers) plus correct
tenant propagation (`TenantScope`, `bind_tenant`, signed RPC context) protect
against *accidental* mistakes. RLS adds a second, independent net for the
mistakes the ORM guard cannot see (raw `text()`, Core statements, a forgotten
filter in hand-written SQL): the database itself scopes them to the bound
tenant. At this level the design below is a complete answer.

**(b) Some code path can issue arbitrary SQL as the application role** (SQL
injection through string-built `text()`, a compromised dependency, a hostile
module). Such code needs a trust boundary it cannot modify. RLS with a shared
application role does **not** provide one for tenant identity: the tenant is a
transaction-local setting (`sandloom.tenant_id`) that the application role sets
itself, so the database cannot know *who* set it. Code at level (b) can
`SELECT set_config('sandloom.tenant_id', '<any tenant uuid>', true)` and then read
and write that tenant's rows. This is a limitation of the trust model (one
shared role, a mutable context), not an implementation bug — no policy on a
setting the caller controls can authenticate the caller.

### Trust model of this Sandloom version (normative)

- **Business modules are trusted code.** Sandloom runs every loaded module in one
  process, under one database role per database. Level (a) is the model Sandloom
  is designed for and the one its guarantees are stated against.
- **Trust is granted by admission, not by origin.** Code — including
  AI-generated code — is not trusted automatically. It becomes trusted when it
  passes admission into the codebase *before* it executes: the architecture
  checks (`sandloom check`, the architecture tests, `public_api` boundaries) and
  human review. Code that has not passed admission must not be loaded.
- **Sandloom protects against developer mistakes, not malice in-process.** It does
  not promise safe execution of malicious arbitrary Python or SQL inside an
  Sandloom process. Such code can reach anything the process can (settings, the
  system DSN, every connection), and can forge the tenant context.
- **Isolating untrusted modules is a separate, future capability.** It needs a
  trust boundary at the process / network / infrastructure level (see the
  options at the end of this section), not a library-level check.
- **Fixed RLS is not absolute protection against arbitrary SQL.** It removes
  every privileged bypass from the application role, but forging
  `sandloom.tenant_id` under the shared application role remains possible (pinned
  by the strict xfail `test_forged_tenant_setting_cannot_read_other_tenant`).

### SQL paths available to business-module code

All of these run as the application role on a `tenant_rls` engine:

| Path | ORM guard | RLS hook syncs `sandloom.tenant_id` | Can set GUCs itself |
|---|---|---|---|
| ORM statements on `db()` / `DbSession` / `open_uow()` / `Database(name)` | yes | yes | — |
| Core `select()/insert()/update()/delete()` via `session.execute` | no | yes | — |
| `session.execute(text(...))` | no | yes | yes |
| `(await session.connection()).exec_driver_sql(...)` | no | yes | yes |
| `(await session.connection()).get_raw_connection()` (asyncpg) | no | **no** (bypasses SQLAlchemy events; the transaction keeps whatever setting was last synced) | yes |
| `db()` inside a contract method | as above | yes (contract pool has the hook) | yes |
| Framework-generated SQL (outbox, audit, durable inbox, `monotonic_upsert`) | n/a | yes | — |

There is **no** path from business-module code to the system channel: it lives
in the driver tree (`sandloom.core.drivers.system_db`), which the
`infra-imports` architecture rule forbids to import statically or dynamically;
it is not in the public core API; the system engine is not an attribute of the
provider, `db()`, `DbSession`, `open_uow()`, `Database(name)`, `core.database(...)`
or any capability. This is an architecture boundary inside one Python process,
not a sandbox: code with arbitrary *Python* execution can read the system DSN
from settings.

### What is guaranteed (tables with `enable_tenant_rls`, startup verification passed)

For any statement the application role sends, by any of the paths above:

- it sees and changes only rows whose `tenant_id` equals the canonical UUID in
  `sandloom.tenant_id` at that moment — `SELECT`, `UPDATE` and `DELETE` match no
  other row, `INSERT`/`UPDATE` of a row for another tenant is refused
  (`WITH CHECK`);
- there is **no "all tenants" value**: the policy has no bypass branch
  (`sandloom.tenant_bypass` and `unscoped()` no longer open anything);
- an unset, empty or malformed `sandloom.tenant_id` (anything that is not a
  canonical lowercase UUID) yields zero rows and refused writes — never a cast
  error, never a fail-open;
- the role cannot leave the policies: the API process refuses to start
  (`TenantRLSConfigError`, in every environment) when the application role is a
  superuser, has `BYPASSRLS`, is a member of such a role, owns (directly or by
  membership) an RLS table, is covered by an unconditional policy (i.e. is or
  belongs to the system role);
- the setting is transaction-local: a pooled connection never carries one
  request's tenant into the next transaction.

### What is NOT guaranteed (residual risks)

- **Tenant-context forgery (level b).** `set_config('sandloom.tenant_id', ...)` with
  another tenant's UUID gives full access to that tenant's rows. Tenant UUIDs
  are not secrets (they travel in tokens, URLs, events). Pinned by the strict
  xfail `test_forged_tenant_setting_cannot_read_other_tenant`.
- **Tables without `enable_tenant_rls`** and non-tenant tables (users,
  `outbox_messages`, `audit_log`, `consumer_inbox`) are protected only by grants.
  In particular the application role can write `outbox_messages`; SQL at level
  (b) can forge events that consumers will process (including
  `bind_tenant_from` payload tenants).
- **Process compromise.** Arbitrary Python execution in a Sandloom process reaches
  everything that process holds, including the system DSN.
- **Raw driver connections** skip the hook: the statement runs under whatever
  setting the transaction last had (fail-closed when none). Not an escalation,
  but honest code must not use them.
- **Verification scope.** Role checks run in the API lifespan (`aopen_all`,
  which also covers `sandloom projection rebuild`) and at Celery worker boot
  (`verify_worker_infra`). The `sandloom outbox` / `sandloom streams` ops CLIs do not run
  them (they share the role, so an API that started proves the role);
  `sandloom migrate` deliberately does not — it runs as the migration role, which
  owns the tables.

### The system channel (privileged work)

Cross-tenant work goes through `sandloom.core.drivers.system_db.system_session(
actor=..., reason=..., database=..., tenant_id=None)`: a separate engine logged
in as a separate login role (default `sandloom_system`, `NOSUPERUSER NOBYPASSRLS`,
not granted to the application role). It is least privilege by construction:
the role reaches other tenants **only** on tables migrated with
`enable_tenant_rls(op, table, system_access=True)` (an explicit permissive
policy `TO sandloom_system` + the DML grants for that table); on every other RLS
table it is tenant-scoped like the app role. Each session requires an actor and
a reason, logs a structured start/end record (`sandloom.system_db`, `op_id`,
outcome, duration) and writes an `sandloom.system_db.session` row to `audit_log` in
the same transaction (no audit row, no commit). The framework itself has no
production caller today; it is the sanctioned path for ops tooling and future
framework maintenance jobs. A separate role fixes *privileged* `unscoped()`
access; it does **not** stop the ordinary role from forging the tenant id.

### Options for level (b) — not implemented; cost and consequences

| Option | What it buys | Cost |
|---|---|---|
| **Signed tenant context** (a `SECURITY DEFINER` verifier recomputing an HMAC over `sandloom.tenant_id` with a key in an owner-only table; the policy calls it via an `InitPlan`) | Forged ids rejected *only if* the app role can neither read the key nor call any function that mints a signature for an arbitrary tenant. The app process must hold the key to sign — so injected SQL cannot sign, but compromised app *code* can. Signatures are replayable for as long as they are valid unless bound to txid/backend pid | Core migration (schema, key table, function, likely `pgcrypto`), key provisioning/rotation in both app and DB, one hash per statement, no extra round trip; does nothing at the process-compromise level |
| **Trusted DB proxy / data-access service** | The tenant comes from an authenticated request, not from SQL; untrusted code never holds a DB connection | A new network hop on every query, a query API narrower than SQL, its own deployment and HA |
| **Per-tenant database roles** (`SET ROLE` is impossible without membership; connect as `tenant_<id>`) | The database authenticates the tenant; RLS keyed on `current_user` | One role per tenant, a pool per tenant (or a non-forgeable role switch), role provisioning in onboarding, connection count grows with tenants |
| **Separate pools per trust level** (untrusted modules get a role with grants only on their own tables) | Limits blast radius by table, not by tenant | Per-module roles and grants in migrations; no tenant guarantee by itself |
| **Sandboxed module execution** (untrusted modules out of process, talking to the core only through contracts) | Untrusted code never holds SQL at all | Process isolation per module, contract-only data access, latency; effectively the microservices profile with per-service databases |

## Production profile: the multi-tenant trust model as a contract

The sections above describe mechanisms; this one states what a production
deployment **promises its tenants**, and how the framework enforces it. Set it
with `PRODUCTION_PROFILE` (enforced only with `ENVIRONMENT=prod`).

### The contract (what Sandloom guarantees, and under which conditions)

1. **Isolation scope.** Tenant data is isolated against *developer mistakes in
   trusted code* (level (a) above): a forgotten filter, raw SQL, a wrong join.
   It is **not** isolated against hostile in-process code (level (b)) — that
   needs process / database-role boundaries (see the options table above).
2. **Defence in depth, not one layer.** Every tenant row is guarded by the ORM
   tenant guard (fail-closed) **and** by PostgreSQL Row-Level Security with an
   application role that cannot bypass it. Either layer alone catching a bug
   is the design; one layer being "optional in prod" is not.
3. **Revocation is authoritative.** A revoked token or blocked user is refused
   by every service on its next request (the session record is read live); when
   the revocation store is unreachable, requests are refused (503), never accepted.
4. **Security state is durable.** Revocation marks, user blocks and
   idempotency reservations are never lost to cache pressure (eviction) and
   survive a cache node failure. (Contract-command dedupe is not cache state
   at all: it is recorded in the provider's database, atomically with the
   command — `contract_command_log`.)
5. **Split deployments are checked.** Services agree on the contracts they
   call (rolling-upgrade compatibility check) and only the token issuer can
   mint tokens (asymmetric signing).

### `PRODUCTION_PROFILE=multi_tenant_strict`

`standard` (default) keeps every knob as configured — appropriate for a
single-customer deployment or a deployment that documents its own deviations.
`multi_tenant_strict` makes the contract above non-optional. In prod it
enforces the table below. The configuration rows are boot checks in
`sandloom.core.app_factory.enforce_configuration`, which `create_app` calls first
(API, worker, `sandloom migrate` and the ops CLIs alike; `sandloom doctor` reports the
profile's problems in its `config` section):

| Requirement | Enforced by | Failure |
| --- | --- | --- |
| `TENANCY_MODE=multi` | boot check (`enforce_configuration`) | process refuses to start |
| `tenant_rls: true` on **every** database instance (`INFRA.database.<name>`) | boot check (`enforce_configuration`) | refuses to start |
| app role is not superuser / `BYPASSRLS` / table owner / system-role member (existing role verification) | database driver at startup (API + worker) | `TenantRLSConfigError` |
| every `TenantScoped` table carries RLS **enabled + forced** and its `<table>_tenant_isolation` policy | critical startup task (API + worker) | `ProductionProfileError` naming each table and the fix (`enable_tenant_rls(op, table)`) |
| `AUTH_REVOCATION_FAIL_CLOSED` not `false` (turned **on** when unset) | boot check (`enforce_configuration`) | refuses to start |
| `SECURITY_CACHE_INSTANCE` names a declared cache instance on a **different Redis server** than the default cache | boot check (`enforce_configuration`) | refuses to start |
| `IDEMPOTENCY_CACHE_INSTANCE`, when set (opt-in) to another instance than the security one, names a declared cache instance on a **different Redis server** than the default cache (same server as the security cache: warning) | boot check (`enforce_configuration`) | refuses to start |
| the security cache — and a separate idempotency cache — reach a different Redis **process** than the default cache (`INFO server` identity) | critical startup task | `ProductionProfileError` |
| split (`SERVICE_BINDINGS` set): `CONTRACT_COMPAT_CHECK` not `off` | boot check (`enforce_configuration`) | refuses to start |
| split (`SERVICE_BINDINGS` set): asymmetric JWT — `JWT_ALGORITHM` EdDSA / RS256 / ES256, `JWT_PRIVATE_KEY` on the issuer only, `JWT_PUBLIC_KEYS` everywhere; the accepted risk `shared_hs_secret_split` refused | boot check (`enforce_configuration`) | refuses to start |
| a process loading no token issuer (split even without bindings) on an HS secret; a verify-only process holding `JWT_PRIVATE_KEY` / `JWT_SECRET` | kernel startup role check (`token_signing_problem`, no opt-out under the profile) | refuses to serve |

Rate limiting, strict event-schema validation, secret strength and the
internal-token rules are already unconditional in prod.

The effective value of every guarantee-changing (**semantic**) setting in
each profile — `dev`, `test`, `prod`, `prod` + `multi_tenant_strict` — is
one table: [Configuration Tiers](configuration-tiers.md) (generated from
`sandloom.core.config.tiers`). A setting moved towards its weaker end in prod is
an *override*: refused where the table says *refused*, otherwise logged as a
WARNING at every start and listed by `/diagnostics/core`
(`configuration.overrides`), `sandloom doctor` and
`sandloom_config_override_active{setting,kind}`.

### Security-critical vs best-effort cache

Two kinds of state live in Redis:

- **best-effort** — response caches, rate-limit counters, event-inbox dedupe
  hints, module `cache()` data: losing it costs a recomputation or, at worst,
  a duplicate a durable inbox already absorbs; an eviction policy
  (`allkeys-lru`) is fine;
- **security-critical** — access-token revocation marks, emergency user
  blocks, `idempotency_guard` reservations / replay records: losing one re-admits a revoked token or re-executes a
  request. Sync contract commands do not depend on it — their dedupe is
  durable in the provider database (`contract_command_log`); only the cache
  fallback for a database without that log goes through the security cache.

The framework routes the second kind to `SECURITY_CACHE_INSTANCE` (default
`default`, i.e. shared; idempotency records can opt into their own instance,
see below). Map it to its own Redis:

```bash
INFRA='{"cache": {"security": {"driver": "redis", "url": "rediss://:<pw>@security-redis:6379/0"}}}'
SECURITY_CACHE_INSTANCE=security
CACHE_URL=redis://cache-redis:6379/0        # best-effort cache (may evict)
```

Recommended configuration of the security Redis:

- `maxmemory-policy noeviction` (a full Redis must refuse writes — the
  framework then fails closed — rather than silently evict marks);
- persistence (`appendonly yes`, `appendfsync always`; `everysec` trades up to
  one second of marks on a crash for write latency) and backups;
- **high availability**: a managed Redis with automatic failover (AWS
  ElastiCache / MemoryDB, GCP Memorystore, Azure Cache — use its primary
  endpoint) or self-hosted Redis + Sentinel. The built-in `redis` driver takes
  a single URL: put Sentinel behind a failover-aware endpoint (the managed
  primary endpoint, or a TCP proxy / HAProxy that tracks the Sentinel master),
  or install a Sentinel-aware cache driver package. Redis
  Cluster is not supported by the built-in driver (no cluster client ships);
- the same `SECURITY_CACHE_INSTANCE` and URL in **every** service (revocation
  marks written by one service must be read by all of them), like `CACHE_URL`
  today; the built-in driver reads from the URL it is given, so point it at
  the primary, not a read replica.

**Separate idempotency cache (opt-in).** `IDEMPOTENCY_CACHE_INSTANCE`
(default empty) moves the idempotency records — `idempotency_guard`
reservations / replay records, task-enqueue dedupe and the cache fallback of
contract-command dedupe — out of the security cache into their own instance;
revocation marks, blocks and the RPC replay guard stay where they are. It
exists for latency, not isolation: on a security Redis with
`appendfsync always` the idempotency writes' fsyncs queue the revocation-mark
reads on Redis's single thread. The idempotency Redis is security-critical
in the same sense (a lost reservation re-executes a request), so it gets the
same configuration as the security Redis — `noeviction`, the same
persistence (never weaker), backups, HA, the same name and URL in every
service. `fail_closed=True` guards fail closed on it exactly as before.

```bash
INFRA='{"cache": {"security": {...}, "idempotency": {"driver": "redis", "url": "rediss://:<pw>@idempotency-redis:6379/0"}}}'
IDEMPOTENCY_CACHE_INSTANCE=idempotency
```

## Idempotency

[Idempotency guards](../concepts/route-policy.md#idempotency) de-duplicate retries on mutating
endpoints. Keys are scoped per caller and tenant; reservations carry an owner
token and every transition is a compare-and-set; a response body is never
replayed to an anonymous caller. Money/irreversible operations use
`fail_closed=True` (cache outage → `503`, never a possible duplicate).
Exactly-once *effects* use a durable inbox.

A valid access token's revocation and emergency-block state is checked before
reserving or replaying a response, even when `idempotency_guard` appears before
the endpoint's usual auth dependency. The request memo prevents duplicate mark
reads by later guards. This does not replace endpoint authorization: keep the
normal authentication, tenant and permission dependencies on protected routes.
Error responses are sent before cache release retries; release still uses an
owner-token compare-and-delete after the final body, with jittered retry and the
pending TTL as the failure backstop.

## Audit trail

[`record_audit`](../concepts/audit.md) writes security-sensitive actions to
`audit_log` in the current Unit of Work (commits iff the change commits). Keep PII
out of `data`. Authorization **denials** are recorded via the structured deny log
+ `access_denied_*` metrics (a denied request rolls back, so it cannot be
audited).

## Transport & request hardening

- **Rate limiting.** A sliding-window limiter, always enforced in prod
  (`RATE_LIMIT_ENABLED` forces it elsewhere). Sensitive auth endpoints carry
  tighter per-endpoint limits. A `429` includes a conservative `Retry-After`.
- **Proxy trust / client IP.** `X-Forwarded-For` is honored only with
  `TRUST_PROXY_HEADERS=true` **and** an immediate peer inside `TRUSTED_PROXY_IPS`
  (JSON list of CIDRs); the chain is then walked right-to-left skipping trusted
  hops, and the first untrusted hop is the client. With trust on but no CIDRs,
  production ignores XFF (uses the peer) and dev uses the rightmost hop.
  `get_client_ip(request)` (from `sandloom.sdk.web`) applies the same rule, combining
  duplicate XFF field lines in wire order. Forwarding headers with trust disabled
  produce one configuration diagnostic and remain ignored.
- **CORS.** `CORS_ORIGINS` (defaults to `FRONTEND_URL`).
- **Body limits.** `MAX_REQUEST_BODY_BYTES` (global `413` cap, chunked bodies
  included) and `MAX_UPLOAD_SIZE_BYTES`. Internal RPC additionally uses
  `RPC_MAX_REQUEST_BYTES` (default 4 MiB); the smaller positive cap wins.
  Credential presence is checked before an RPC handler reads its body.
- **API response headers.** JSON responses receive CSP `default-src 'none';
  frame-ancestors 'none'`, restricted camera/microphone/geolocation permissions
  and COOP `same-origin`, preserving explicitly supplied endpoint values.
  HTML/Swagger keeps its own asset policy; HSTS preload is an operator decision.
- **Internal endpoints.** `/metrics`, `/diagnostics/*`, `/contracts/*` and
  `/health` details are gated by
  `INTERNAL_API_TOKEN` (sent as `X-Internal-Token`). Fail closed in **every**
  environment: without a token they answer `404`, unless
  `INTERNAL_ENDPOINTS_OPEN_IN_DEV=true` outside production. `/metrics` also
  accepts the scrape-only `METRICS_TOKEN` (must differ from the internal token).
  Production requires both tokens, when set, to be long and random.
- **Internal RPC** (`/_sandloom/rpc/*`) is never opened by `INTERNAL_API_TOKEN` or
  `INTERNAL_ENDPOINTS_OPEN_IN_DEV`: it accepts only pair-MAC authenticated
  requests carrying a MAC-covered caller context (tenant, issue time within
  `RPC_AUTH_MAX_SKEW_SECONDS`) — see [Service RPC](../guides/service-rpc.md)
  and the service trust model above.
  Both HTTP transports enforce `RPC_MAX_RESPONSE_BYTES` while reading a response.
  Automatic fallback to httpx ignores ambient proxy settings while retaining
  custom CA trust; explicitly choosing `RPC_TRANSPORT=httpx` opts into its
  documented environment proxy support. Calls have an overall deadline covering
  discovery and the configured transport-attempt budget. A timeout can follow
  a committed effect; retry with the same operation key.
  Describe requests are coalesced per provider/contract and failures are cached
  for five seconds to bound outage amplification and warnings. The RPC context
  decoder rejects duplicate keys in the JSON context.
- **Health.** Public `/health` / `/ready` return statuses only and hide the
  module/consumer topology; results are cached for `HEALTH_CACHE_TTL_SECONDS`
  (default 3) so anonymous traffic cannot fan out probes.
- **Interactive docs** (`/docs`) are off in production by default (`DOCS_ENABLED`).

The original 22 October remediation report remains in the private project
history. Public regression coverage lives in the
[security suite](https://github.com/Sandloom-dev/sandloom/tree/main/benchmarks/tests)
and the [integration tests](https://github.com/Sandloom-dev/sandloom/tree/main/tests/integration).

## Secrets

Loaded by `pydantic-settings` in priority order: process environment → `.env`
→ a secrets directory (`SECRETS_DIR`, e.g. `/run/secrets` for Docker/Kubernetes
file-based secrets). Logs redact sensitive keys (substring match on `extra`
keys), scrub credential-shaped values from messages, and Sentry events pass the
same redaction. Production fails fast on a weak or placeholder `JWT_SECRET` /
`JWT_PREVIOUS_SECRETS` / `TWOFA_ENCRYPTION_KEY` / `INTERNAL_API_TOKEN` /
`METRICS_TOKEN`, on `BILLING_PROVIDER=stripe` without its keys, and on
`BILLING_PROVIDER=fake` unless `SANDLOOM_ACCEPTED_RISKS` lists `fake_psp_in_prod`
(every accepted risk is logged at each start and exported as
`sandloom_accepted_risk_active{risk}`). See the
[Operations runbook](../operations/runbook.md#secrets) for secret-manager
integration.

No secret file enters an `sandloom new` application's image: its `.dockerignore`
excludes env files (all but `.env.example`) and the key files of `sandloom keys
generate --out-dir` at any depth, including the split's
`deploy/.env.microservices`, and its Dockerfile copies only `pyproject.toml`,
`uv.lock` and the package (nothing of the build context reaches the runtime
stage). `sandloom add deploy` patches an older `.dockerignore`.

## Dependency & supply chain

The production image builds from the locked `uv.lock` (reproducible) and runs as
a non-root user. CI runs a dependency vulnerability audit (`pip-audit`,
blocking CI gate).
