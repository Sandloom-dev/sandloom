---
title: Route policy and sessions
description: Model E - deny by default, public(reason=), the live session check, the commit-time re-check, risk at its source, the idempotency default, the route snapshot.
type: concept
---

# Route policy and sessions

Every route gets its policy from the framework, derived from who calls it
and what it does. An ordinary route declares **nothing**: it is
authenticated, its session is checked live, a write re-checks the session
before it commits, a retried write is deduplicated, and a sensitive effect is
audited. The one deviation an ordinary route author needs is
`public(reason=...)` for a route without a caller. This is model E of
[ADR-0009](../../adr/0009-route-policy-and-session-freshness.md).

## What you get by default, and what you declare

| Every route gets | Unless it declares |
| --- | --- |
| authentication: `module_router()` adds the user authenticator per route; a route with neither an authenticator nor `public()` is refused at startup | `public(reason=...)`; `public(reason=..., issues_tokens=True)` for a token issuer |
| tenant binding: required when the module declares a `TenantScoped` model | `tenant="optional"` or `"none"` on the router or the route |
| a live session check: one read of the subject record per request; a revoked or blocked token is `401` | nothing (there is no per-route freshness setting) |
| on a write: the commit-time re-check before the COMMIT of every unit that commits something | `at_commit()` extends it to every unit, empty ones included |
| on a write with `Idempotency-Key`: the durable guard, replaying the status only (mode S) | `replay_safe(reason=)` for body replay (mode B); `no_idempotency(reason=)` to opt out; an explicit `idempotency_guard(...)` |
| `Cache-Control: no-store` on user routes | the route sets its own header |
| on a sensitive write: one audit row, money dedupe, step-up for security | the risk is declared once, at its source |

```python
from sandloom.sdk import CurrentUser, module_router, public

router = module_router()


@router.post("/", status_code=201)          # authenticated, tenant-bound, re-checked, deduplicated
async def create_note(body: NoteIn, user: CurrentUser) -> NoteOut: ...


@router.get("/status", dependencies=[public(reason="load balancer probe")])
async def status() -> dict[str, str]: ...
```

`CurrentUser` is the caller: a `UserContext` with `user_id`, `roles`,
`permissions`, the claims provenance and `claim(name)`. A route that
authorizes with a gate (`require(...)`) needs no parameter at all.

## Deny by default

- Every API route resolves an authenticator (the user authenticator that
  `module_router()` adds, `CurrentUser`, `CurrentService` for an internal
  service call, or a custom dependency marked `@authenticates`) **or**
  declares `public(reason=...)`. `create_app` refuses a route that is neither,
  one that is both, an empty reason, or two different public reasons
  ([SANDLOOM-B008](../../reference/error-codes.md#sandloom-b008)).
- `module_router()` adds no router-level authentication; it adds the
  authenticator and `TenantScope(required=...)` per route at decoration time,
  and adds nothing to a route that declares `public()` or a service
  authenticator. So a route never has to remove an inherited authenticator.
- **Token issuers** (login, refresh, SSO callbacks) declare
  `public(reason=..., issues_tokens=True)`. Their live issuance check (a
  blocked user is refused before any side effect) is unchanged.
- **Webhooks** are `public(reason=...)` and verify the provider's signature
  inside the handler. They are not service principals.
- **Service routes** take `CurrentService` (the internal service principal,
  `404` for anyone else).
- A plain FastAPI `APIRouter` gets the same refusal: the policy is the
  kernel's, not the router's.

## Tenant binding

Each route of a module with a `TenantScoped` model binds the caller's tenant
from the signed token, and a missing tenant is `400`. How the binding is
derived and overridden: [Multi-tenancy](../database/multi-tenancy.md#bind-the-tenant).

## The live session check

Every user authenticator reads the **subject record** of the caller live:
one cache `GET`, memoised per request.

- The record is `sandloom:auth:subject:{user_id}`. Its value is either
  `v1:after:<us>` (tokens issued at or before that instant are revoked) or
  `v1:blocked:<since_us>` (every token is refused; it never expires and
  outranks any `after:`).
- A revoked or blocked token answers
  `401 WWW-Authenticate: Bearer error="invalid_token"`. A cache outage answers
  `503` when `AUTH_REVOCATION_FAIL_CLOSED` is on (the production default). A
  corrupt record fails closed; the next issuance check repairs it.
- Access tokens must carry `token_type: "access"` and `iat`; tokens without
  them are refused.

**Revoking a user's sessions** after a security event (password change,
membership removal, role change):

```python
from sandloom.sdk.security import revoke_user_tokens_on_commit  # sandloom: advanced security event

await revoke_user_tokens_on_commit(user_id, reason="tenancy.member.removed", require_immediate=True)
```

- It runs after the unit commits, so a rolled-back change never logs anyone
  out. With `require_immediate=True` (every bundled security event uses it)
  the record is also written before the commit, or the change is refused
  with `503`; call it as the last fallible step.
- The record is written at `now + AUTH_REVOCATION_CLOCK_SKEW_SECONDS`
  (default 2 s), monotonically, and lives
  `AUTH_REVOCATION_MARK_TTL_SECONDS` (at least the access-token lifetime;
  keep `ACCESS_TOKEN_EXPIRE_MINUTES` identical in every service). Delivery is
  at-least-once: the revocation is also written to the outbox
  (`core.auth.tokens_revoked.v1`) and applied by identity's durable consumer
  if the post-commit write fails (`auth_revocation_deferred_total`).
- **Emergency stop:** `block_user(user_id, reason=, actor_id=)` and
  `unblock_user(...)` (`sandloom.sdk.security`, inside a unit, audited). A block
  ends every session, also for tokens minted later; token issuers refuse a
  blocked user live (`ensure_user_not_blocked`). An unblock replaces the
  block by compare-and-set after the commit and never deletes the record, so
  tokens issued before the block stay revoked.

## The commit-time re-check

A write could pass the session check at the start of the request and commit
after the session was withdrawn. So on every authenticated write the kernel
re-reads the subject record **immediately before the COMMIT of every unit of
the request that commits a write or a queued effect**: the request unit,
named-database units, local command units (in the provider's unit) and remote
command units (propagated in the MAC-covered RPC context, re-checked by the
provider before its COMMIT). A refused re-check answers `401` and rolls the
unit back.

- A unit that commits nothing skips the automatic re-check.
- A GET whose unit actually wrote is re-checked too; its `on_commit` effects
  are kept.
- `at_commit()` (advanced) checks before the COMMIT of **every** unit the
  request opens, empty ones included, for a non-write route with an explicit
  commit obligation.
- **Self-revocation is "check, then revoke".** When code in a unit calls
  `revoke_user_tokens_on_commit(S, ...)` or `block_user(S, ...)` for the
  request's own subject `S`, the kernel first checks `S` live, then lets the
  revocation proceed and skips `S` at that unit's COMMIT. So a password
  change, leaving an organization or an ownership transfer works with no
  declaration, while a withdrawal by someone else that landed earlier is
  still seen. A self-revocation in a rolled-back savepoint, a failed command
  or a lost RPC response contributes nothing.
- The guarantee is serializable, not linearizable: it narrows the window to
  "re-read, then COMMIT"; it does not close it.

## Risk is declared at its source

A route is sensitive because of what it calls, not because of a tag on the
route. The owner of the effect declares the risk once:

| Risk | Declared by | Examples |
| --- | --- | --- |
| `admin` | the framework's admin gates | `require(Role("admin"))`, `require(OrgRole("admin"))`, `require_platform_admin`, tenancy's org-admin gate |
| `money` | the effect owner | `@command(..., risk="money")` on a contract command; the `MoneyLedger` model mixin (names its unique operation-key column); `@sensitive_effect("money")` on an effect service |
| `security` | the credential owner | `@sensitive_effect("security", step_up=300)` on credential-changing services |

A route whose authority check is hand-rolled declares its risk with
`sensitive("admin" | "money" | "security")`. An effect that looks sensitive
but carries no tag is reported as a warning
([SANDLOOM-C022](../../reference/error-codes.md#sandloom-c022)).

The route marker already returns a dependency:

```python
@router.put("/users/{user_id}", dependencies=[sensitive("admin")])
async def update_user(user_id: UUID, user: CurrentUser):
    ...
```

`admin` classifies an authority-changing operation for automatic audit; it
**does not grant or require a role**. The operation must still authorize the
caller. Built-in profile updates, invitation acceptance and member removal
use this classification, including their self-service branches. Membership
role changes, removal, organization deletion and ownership transfer also
carry `@sensitive_effect("admin")` at their owning operation. A route and its
effect declaring the same risk still produce one automatic audit row.

Shared token-revocation helpers do not impose credential step-up on all
callers. Password changes declare `security` at the password-changing
operation; organization administration and operator revocation declare `admin`;
a public password reset validates its own reset token. The built-in revocation
helper has a local, reasoned C022 waiver for this distinction. Immediate
revocation, domain audits and commit-time session checks still apply.


**What risk triggers**, applied by the framework:

- **Audit.** One automatic audit row (`sandloom.sensitive_write`) per sensitive
  write, in the request's writing unit, required: the actor, the route
  template, the method, the path parameters, the risks and their sources, the
  status. Never the body.
- **Money dedupe is mandatory.** A money effect must sit under a dedupe
  record: its own operation key, the request's `request_operation_key()`, a
  command key or the ledger row's key. A developer omission is refused before
  COMMIT ([SANDLOOM-R009](../../reference/error-codes.md#sandloom-r009)); a client that
  sent no `Idempotency-Key` gets `428`
  ([SANDLOOM-R010](../../reference/error-codes.md#sandloom-r010)).
- **Step-up** where an effect declares `step_up=N` (security: 300 s): the
  token's `auth_time` (the primary sign-in time, never `iat`) must be at most
  N seconds old, or the request must call `mark_reauthenticated()` after a
  successful password check. Otherwise
  `401 WWW-Authenticate: Bearer error="insufficient_user_authentication", max_age=N`
  ([SANDLOOM-R011](../../reference/error-codes.md#sandloom-r011), RFC 9470).

## Idempotency

A write that receives an `Idempotency-Key` is deduplicated with no
declaration: a retry gets the stored status and an empty body (mode S). A
route opts in to body replay with `replay_safe(reason=)` (mode B) or opts out
with `no_idempotency(reason=)`. Every mode and when body replay is safe:
[Make a write idempotent](idempotent-write.md#idempotency-modes).

## Token claims

A module contributes claims to the access token with
`core.add_claims_contributor(fn, critical=...)` in `setup(core)`. A
contributor runs in the module's own unit and is exposed as
`<module>.claims.v1`, so an issuer in another process collects it over RPC. A
failing critical contributor aborts minting (`ClaimsUnavailable`, `503`).
Every token carries `claims_sources` and `claims_sources_failed`: trust a
module's claim only if that module is in `claims_sources`. Identity stamps
`auth_time`, carried unchanged through refresh.
Each claim key has one owner. A second module writing a key another module
wrote (identity's `roles`, say) aborts minting (`503`), local or remote. So
does a contributor writing a key the kernel stamps: `claims_sources`, `claims_sources_failed`,
`sub`, `iss`, `aud`, `exp`, `nbf`, `iat`, `jti`, `iat_us`, `auth_time`,
`token_type`.

## The route snapshot

Each module's `routes.snapshot.json` records the resolved policy of every
route and method:
`auth`, `effect`, `session`, `risk` with its sources, `idempotency` (the mode),
`audit`, `step_up_max_age_s`, `public_reason`, `issues_tokens`,
`operation_intent`, `write_store`, `multi_db_write`. `sandloom generate` writes it
and `sandloom test` fails on drift ([SANDLOOM-T001](../../reference/error-codes.md#sandloom-t001)); a
weakening (authentication removed, idempotency removed, risk removed) fails
before anything is written: approve it with `sandloom routes snapshot --write`. Review its diff like code. The platform's routes
and their policy: [Routes](../../reference/routes.md).

## What breaks it

| Code | What it catches |
| --- | --- |
| [SANDLOOM-B008](../../reference/error-codes.md#sandloom-b008) | a route neither authenticated nor `public()`, both, or without a reason |
| [SANDLOOM-B002](../../reference/error-codes.md#sandloom-b002), [SANDLOOM-C018](../../reference/error-codes.md#sandloom-c018) | an explicit guard or a tenant-dependent gate resolved before what it needs |
| [SANDLOOM-B019](../../reference/error-codes.md#sandloom-b019) | an issuer or intent route with a guard other than mode X |
| [SANDLOOM-R009](../../reference/error-codes.md#sandloom-r009), [SANDLOOM-R010](../../reference/error-codes.md#sandloom-r010) | a money effect without a dedupe record; no `Idempotency-Key` on a money write (`428`) |
| [SANDLOOM-R011](../../reference/error-codes.md#sandloom-r011) | step-up required (`401`, RFC 9470) |
| [SANDLOOM-C022](../../reference/error-codes.md#sandloom-c022) | an untagged effect that looks sensitive (warning) |
| [SANDLOOM-T001](../../reference/error-codes.md#sandloom-t001) | the route snapshot differs from the resolved policy |
| [SANDLOOM-R001](../../reference/error-codes.md#sandloom-r001) | a missing tenant (`400`) |

??? note "Guarantees, precisely"

    - Authenticated xor `public(reason=)`, refused at startup (G-ROUTE-01,
      G-ROUTE-02); webhooks public with a verified signature (G-ROUTE-03).
    - One live subject-record `GET` per authenticated request, memoised
      (G-REVOKE-21); one record for revocation and block (G-BLOCK-06, 07).
    - The re-check before the COMMIT of every committing unit of an armed
      request, writing GETs included, propagated to remote commands; a
      foreign revocation between two commits of one request refuses the second
      (G-REVOKE-22, G-REVOKE-24); "check, then revoke" (G-REVOKE-23).
    - `no-store` on user routes (G-HTTP-12).
    - Automatic idempotency is mode S and never returns a body; `replay_safe`
      opts in to B; replay after every route dependency (G-IDEM-17, G-IDEM-18).
    - Risk at its source, automatic audit, mandatory money dedupe, step-up
      (G-SENS-01 to G-SENS-06).
    - The snapshot pins the resolved policy; a weakening fails (G-ROUTE-04).
    - Serializable, not linearizable: the re-check narrows the window to
      "re-read, then COMMIT" ([ADR-0003](../../adr/0003-fewer-sync-rpc-membership-reads.md),
      "Guarantees, precisely").

    The ledger with the enforcing tests: [Guarantees & capabilities](../../reference/guarantees.md).

## Design rationale

- [ADR-0009: route policy follows authority and effect](../../adr/0009-route-policy-and-session-freshness.md)
- [ADR-0010: Public API 1.0](../../adr/0010-public-api-1.0.md), "Idempotency modes"
- [ADR-0005: the effect model](../../adr/0005-effect-model.md)
