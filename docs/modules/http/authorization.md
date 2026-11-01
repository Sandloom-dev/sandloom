---
title: Authorization and billing subjects
description: The composable authorization algebra, its tiers and three-valued decisions, access facts, and explicit billing subjects with of=Account.user() / Account.org() / Account.bound_tenant().
type: concept
---

# Authorization and billing subjects

Authorization in Sandloom is one composable algebra. A policy is a requirement
built from atoms with `&`, `|` and `~`; `Depends(require(policy))` turns it
into a route dependency. The same policy runs in HTTP routes, in services, in tasks
and in consumers. Billing atoms always name **whose** account they check
(`of=`), because the tenant a request is bound to does not tell you which
billing account pays.

## What you get by default, and what you declare

| You write | Sandloom does for you |
| --- | --- |
| `dependencies=[Depends(require(Role("admin")))]` | evaluates the policy before the handler; a deny answers with the decision's status |
| `require(Subscribed(of=Account.user()) \| Role("admin"))` | evaluates the cheapest tier first and short-circuits: an admin never costs a billing call |
| `of=Account.user()`, `Account.org(param="org_id")` or `Account.bound_tenant()` on every billing atom | resolves exactly that account; never infers it from the tenant |
| `authorize_resource(loader=..., by="note_id", policy=Owner(...))` | loads the resource once, authorizes it and hands it to the endpoint |
| nothing | the framework's admin gates carry the `admin` risk: an automatic audit row ([Route policy](route-policy.md)) |

```python
from fastapi import Depends

from sandloom.sdk import OrgRole, Subscribed, require
from sandloom.sdk.authz import Account  # sandloom: advanced billing subject

ORG = Account.org(param="org_id")


@router.get(
    "/orgs/{org_id}/reports/export",
    dependencies=[Depends(require(OrgRole("member") & Subscribed(of=ORG)))],
)
async def export(org_id: UUID) -> ExportOut: ...
```

## The algebra

A requirement is a predicate. `&` is and, `|` is or, `~` is not. Atoms
evaluate **cheapest tier first**, then in declaration order, with short
circuit, so a cheap claim check decides before any resource load or live
call.

| Tier | Cost | Reads |
| --- | --- | --- |
| claim (1) | zero I/O | the token's claims |
| resource (2) | loads the resource once, lazily | the request's resource |
| live (3) | a live contract call | another module's contract |

| Atom | Tier | Reads |
| --- | --- | --- |
| `Perm(code)`, `Role(code)` | claim | the `permissions` / `roles` claims |
| `Subscribed(statuses=None, *, of, claim_key="subscription_status")` | claim with `of=Account.user()`; live with an organization | the `subscription_status` claim (`active` / `trialing` pass; `past_due` passes during the grace period unless `BILLING_DENY_ON_PAST_DUE`), or billing live |
| `SubscriptionActive(*, of, adapter=None)` | live | billing's entitlements, authoritative |
| `Credits(minimum, *, of, adapter=None)` | live | the real-time credit balance (never a token claim) |
| `Feature(key, *, of, claim_key="features")`, `WithinLimit(key, used, *, of, claim_key="limits")` | claim with `Account.user()`; live with an organization | the `features` / `limits` claims, or the owner's `organization.features` / `organization.limits` facts |
| `Owner(get_owner)` | resource | the loaded resource's owner id |
| `OrgRole(minimum, *, org_param="org_id", adapter=None)` | live | tenancy's membership contract |
| `Entitled(fact_name)` | by the fact's mode | a registered access fact |

Live atoms resolve their contract through the service registry (a local
provider, else the RPC binding) and never pass the request's session. The
contract an atom talks to is a pluggable adapter
(`register_live_adapter(ENTITLEMENTS | MEMBERSHIP, adapter)`), defaulting to
billing's entitlements and tenancy's membership contracts. Cross-cutting atoms
reference other modules by string, never by import; a module's own
domain-specific atoms live in its package.

## Three-valued decisions

A decision is **allow**, **deny** or **indeterminate** (the policy could not
be evaluated: a fact is unregistered, a contract is absent from the
deployment, the tenant context is missing). Indeterminate always denies with
its own status: `~` never inverts it, and `|` allows only when some operand
genuinely allows.

| Category | Status |
| --- | --- |
| `unauthenticated` | 401 |
| `forbidden`, `tenant_inactive`, `resource_forbidden` | 403 |
| `payment_required`, `quota_exceeded` | 402 |
| `policy_misconfigured` | 500 (unregistered fact); 501 when a contract or fact source is absent from the deployment |

Subscription and credit gates answer `402`. `AccessDenied` is an
`HTTPException`, so the response carries the decision's status and a
`{"detail", "code"}` body. `require(..., status_code=...)` overrides only a
definite deny.

!!! note "Known behaviour, kept"
    Inside `~(A & B)` evaluation stops at the first indeterminate operand (the
    result is indeterminate) or at the first definite deny seen before any
    indeterminate. So `~(F & U)` allows while `~(U & F)` does not. The order
    is deterministic (cheapest tier first, then declaration order). Changing
    it would cost either an allow or extra live calls.

## One chokepoint: `enforce` and `authorize`

HTTP guards, use-case calls inside a service and background work evaluate
the same policy:

```python
from sandloom.sdk.authz import enforce, system_subject  # sandloom: advanced non-HTTP authorization

await enforce(Role("admin"), subject=user, source="internal")
await enforce(policy, subject=system_subject(), source="task")
```

`enforce` builds the context, decides, and on denial records a structured
deny log and the `access_denied_*` metric, then raises `AccessDenied`.
`require`, `authorize` and `authorize_resource` are thin wrappers over it. A
denial is logged, not audited: the request rolls back, and its audit rows
with it ([Audit](audit.md)).

## Billing subjects: `of=` is required

Three notions are separate:

| Notion | What it is | Source |
| --- | --- | --- |
| tenant identity | the bound tenant id; scopes data | the signed token via the tenant binding |
| tenant kind | `personal`, `organization`, `canonical` or `unknown` | authoritative only: `canonical` from `TENANCY_MODE=single`; `personal` when it equals the caller's user id; `organization` when the membership contract confirms it; else `unknown` (`await tenant_kind(tenant_id)`, memoised per request) |
| authorization subject | the billing account an atom checks | stated in code with `of=` |

`Subscribed`, `SubscriptionActive`, `Credits`, `Feature` and `WithinLimit`
take a keyword-only, required `of=`. Without it the atom refuses to construct
and startup fails ([SANDLOOM-B017](../../reference/error-codes.md#sandloom-b017)).

| Value | Resolves to |
| --- | --- |
| `Account.user()` | the caller's personal account. `Subscribed(of=Account.user())` stays claims-only with zero I/O |
| `Account.org(param="org_id")` | the organization in the path or query parameter `param`, else `X-Tenant-Id`, else the bound tenant, each validated against the signed allowlist and membership. No organization, or the caller's own personal tenant, is indeterminate: never the caller's user id |
| `Account.org(param=None)` | the header or the bound tenant only |
| `Account.bound_tenant()` | by tenant kind: personal → the user's account; organization → the organization's account; canonical or unknown → indeterminate |

- **A route under `/orgs/{org_id}` with `of=Account.user()` is refused**
  statically unless the atom says why:
  `Account.user(reason="the export is billed to the requester")`
  ([SANDLOOM-C027](../../reference/error-codes.md#sandloom-c027)).
  `Account.org(param="x")` without `x` in the path or query is an error too.
- **Non-HTTP use** (`authorize(policy, subject=...)` in tasks and consumers,
  service and system subjects) resolves from the authorization context and the
  bound tenant, not from a request.
- Custom live adapters (`adapter=`) are kept.

### Account selection in billing itself

The same rule applies where billing chooses an account:

- **The billing API** (`/subscription`, `/credits`, checkout, portal):
  without `org_id` the account is the caller's personal account; an
  organization account needs `org_id`. There is no inference from "member of
  exactly one organization". See [Billing](../standard/billing.md).
- **`billing.credits.v1.spend` / `get_balance`**: when the call carries a
  user subject, a personal `tenant_id` must equal that subject, else
  `BillingTenantMismatchError` (`403`). System and service callers are
  unchanged.
- **Operation intents** (checkout, portal) pass the resolved account to
  `release_check`, which requires the authority re-read for an organization
  ([Lifecycle](../guarantees/lifecycle.md#operation-intent-mode)).

## Access facts and resolution modes

`Entitled(fact_name)` resolves a named `AccessFact`, published by its owner as
part of its public contract (`<owner>.facts.v1`; a process without the owner
binds it in `SERVICE_BINDINGS` and gets the same decision). The mode is set
per fact with the `ACCESS` setting: `claim` (from the token), `projection` (a
local read model), `live` (an authoritative call) or `auto`. A configured mode
never weakens the fact's `min_freshness` floor. A claim is trusted only when
its owning module is in the token's `claims_sources` and not in
`claims_sources_failed`; otherwise claim mode escalates to live. Claim
interpretation is declared with `AccessFact(claim_rule=ClaimRule.one_of(...))`
and travels over RPC.

Billing registers `subscription.active` (default `auto`) and
`organization.subscription.active` (live only, membership-checked); a false
fact denies `402`, a non-member is refused `403`. In tests,
`fake_access_fact(name, value)` answers a fact without loading its owner;
the two subscription facts also answer the live `SubscriptionActive` /
`Subscribed(of=Account.org(...))` atoms.

A route's gate names the contracts it calls, and they join the module's
derived `consumes` as optional requirements: `SubscriptionActive` / `Credits`
add `billing.entitlements.v1`, `Entitled(fact)` adds the fact owner's
`<owner>.facts.v1`, `Subscribed` adds `billing.facts.v1`, `OrgRole` adds
`tenancy.membership.v1`. The startup preflight warns when one is unbound,
and `sandloom add deploy` binds them in the microservices profile.

## What breaks it

| Code | What it catches |
| --- | --- |
| [SANDLOOM-B017](../../reference/error-codes.md#sandloom-b017) | a billing atom without `of=` |
| [SANDLOOM-C027](../../reference/error-codes.md#sandloom-c027) | an organization route gating billing with `of=Account.user()` and no reason |
| [SANDLOOM-B007](../../reference/error-codes.md#sandloom-b007), [SANDLOOM-C018](../../reference/error-codes.md#sandloom-c018) | a gate that needs an organization runs before the tenant binding |
| [SANDLOOM-R012](../../reference/error-codes.md#sandloom-r012) | an organization release check without the authority re-read |

??? note "Guarantees, precisely"

    - The algebra is unchanged: three-valued statuses, fail-closed, `~U = U`,
      `|` allows only on a genuine allow, cheapest tier first with short
      circuit and zero I/O for claims-only policies (G-AUTHZ-01, G-AUTHZ-02,
      G-AUTHZ-03). A probe pins value, evaluation count and tier order
      against the implementation.
    - `of=` is required (G-AUTHZ-09, with SANDLOOM-C027); `Account.org()` never
      returns the caller's user id and no resolution returns a foreign account
      (G-AUTHZ-10); `bound_tenant()` resolves by tenant kind with its test
      table (G-AUTHZ-11); organization features and limits need the owner's
      fact (G-AUTHZ-12).
    - A personal spend or balance call must be the caller's own
      (G-BILLING-20). The billing API account is chosen explicitly; no ledger
      entry pins the removed single-organization inference.

    The ledger with the enforcing tests: [Guarantees & capabilities](../../reference/guarantees.md).

## Design rationale

- [ADR-0010: Public API 1.0](../../adr/0010-public-api-1.0.md), "Explicit authorization subjects"
- [ADR-0003: fewer sync RPC membership reads](../../adr/0003-fewer-sync-rpc-membership-reads.md)
- [ADR-0009: route policy](../../adr/0009-route-policy-and-session-freshness.md) (admin risk on gates)
