---
title: Billing
type: concept
---

# Billing Module (`sandloom_billing`)

Billing is an **optional** monetization module (monolith-first,
microservice-ready): a product/price catalog, subscriptions, a materialized
credit balance plus an append-only credit ledger, and Stripe (or a fake PSP)
integration including webhook ingestion with idempotency.

It contributes subscription/plan claims to access tokens (`billing.claims.v1`,
non-critical) and registers two access facts for the
[authorization algebra](../http/authorization.md):
`subscription.active` (personal tenant; claim / live / auto per `ACCESS`) and
`organization.subscription.active` (the request's organization; live only).
Both deny `402` (`deny_category=PAYMENT_REQUIRED`, consistent with
`Subscribed` / `SubscriptionActive` / `Credits`); the org fact refuses a
non-member or a request without an organization with `403`.

`subscription.active` declares `absent_claim="deny"`. For `Account.user()`,
a `subscription_status` that billing proved absent is its own definite `402`.
Billing ran at the mint under the intact claim policy and wrote none, as for
a user with no customer. `Subscribed()` and `Subscribed(statuses=...)` agree.
Billing failed at the mint is `503 claim_authority_failed`, never asked live.
A token without provenance or `cpv` is `501 access_unknown`. The table is in
[Authorization](../http/authorization.md#three-valued-decisions).

Billing declares its four claims fail-safe with
`add_claims_contributor(..., fail_safe=[...])`, so a billing outage does not
break a login. An issuer that binds billing remotely declares them in its
root with `Authority("sandloom_billing", fail_safe=True)`.

## Domain entities

| Model | Table | Description |
| --- | --- | --- |
| `BillingProduct` | `billing_products` | Catalog product. |
| `BillingPrice` | `billing_prices` | Price for a product; `credits` = credits granted per purchase of a credit pack. |
| `BillingCustomer` | `billing_customers` | Tenant (user XOR org) ↔ PSP customer link. |
| `BillingSubscription` | `billing_subscriptions` | Subscription record (`last_event_at` / `last_event_id` ordering guard). |
| `CreditBalance` | `billing_credit_balances` | Materialized current credit balance. |
| `CreditLedgerEntry` | `billing_credit_ledger` | Append-only ledger, `UNIQUE(customer_id, operation_key)`. |
| `StripeEvent` | `billing_stripe_events` | Stripe webhook idempotency journal. |

## Service contracts

All contracts take **no session**: each call runs in billing's own Unit of Work
on billing's database, identically in-process and over internal RPC.

| Contract | Methods / purpose |
| --- | --- |
| `billing.entitlements.v1` | read: `check_subscription_active(tenant_type, tenant_id)`, `has_credits_at_least(tenant_type, tenant_id, minimum)`, `has_feature(tenant_type, tenant_id, key)`, `is_within_limit(tenant_type, tenant_id, key, used)`, `get_credit_balance(tenant_type, tenant_id)`, `subscription_claims(tenant_type, tenant_id)` |
| `billing.credits.v1` | command: `spend(tenant_type, tenant_id, amount, operation_key, *, reason="") -> int`; read: `get_balance(tenant_type, tenant_id)` |

### Features and limits

`Feature(key, of=...)` and `WithinLimit(key, used, of=...)` are supported
Sandloom capabilities. Billing declares both as the generated atoms of
`billing.entitlements.v1`'s `has_feature` and `is_within_limit`; import them
from the contract's generated stub. With `Account.user()` they read the
token's `features` and `limits` claims with zero I/O. With an organization
account they ask billing live.

With `Account.user()` the token decides in three ways:

- A `features` / `limits` mapping in the token is the account's complete
  entitlements. An entry it lists decides, and an entry it does not list
  denies (a flag off, a limit of 0).
- No mapping in the token is unknown: an indeterminate `501` with code
  `access_unknown`. When billing failed while the token was minted, it is a
  `503` with code `claim_authority_failed`.
- A mapping that is malformed is also `501 access_unknown`.

None of the unknowns is a deny: `Not` over them never allows. Billing
writes `subscription_status` and `plan_code` but no `features` or `limits`
claim until the catalogue ships. Until then both atoms answer `501` for a
personal account. A module that writes such a claim writes the whole
mapping or none.

Billing does not have a per-plan features and limits catalogue yet; it is
planned as a later task. Until it ships, billing cannot answer either check
for an organization, and both methods raise `AccessUnknown`. The atom then
answers an indeterminate `501` with code `access_unknown` and a reason that
names the missing catalogue. It never answers a deny: no `Not` or `Or` over
such atoms allows. An `Or` still
allows when one of its other branches allows.

A module that keeps its own catalogue provides the same atoms from its own
contract. It declares `@claim_atom` above `@access_check` methods that return
`True` or `False`, and raises `AccessUnknown` when it has no answer. Nothing
in the kernel changes, in-process or over internal RPC
([authorization](../http/authorization.md)).

The PSP client (fake or Stripe, by `BILLING_PROVIDER`) is billing-internal and
is **not** a service contract: no other module (and no RPC caller) can reach it.
Billing's endpoints use `sandloom_billing.psp.active_psp_client()`; tests swap it
with `install_psp_client(fake)`.

`billing.credits.v1.spend` commits in billing's own UoW and is **not atomic with
the caller's transaction** (saga): if the caller later rolls back, the spend
stays committed and the caller compensates. `operation_key` is mandatory and is
the idempotency key — a retry returns the original balance; reusing the key for
a different amount is `409`; insufficient balance or no billing customer is
`402` (nothing written). Granting is not part of any contract: credits are
minted only by billing (paid checkout webhooks).

The command dedupe scope includes `tenant_type` and `tenant_id`: two customers
may independently use the same operation key. Invalid credit-operation values
answer `422` on both local and RPC paths.

- **Account binding** (`spend` and `get_balance`). When the caller has a
  tenant bound (`TenantScope` / `bind_tenant`; over RPC the signed context
  re-binds it), an `"organization"` account must be that organization:
  another org is `403`. A personal (`"user"`) account is not checked against
  the bound tenant (a user's wallet is not a tenancy tenant) but against the
  end user the call runs for: when the call context carries one (the calling
  request's subject locally, the signed caller facts over RPC, through any
  number of hops), `tenant_id` must be
  that user, else `403` (`BillingTenantMismatchError`). System and service
  callers (no end user in the context) may name any account.
  `TENANCY_MODE=single` is exempt from the organization check (its canonical
  tenant is not an organization selector).
- **Framework dedupe.** As a command contract, `spend` is also deduplicated by
  the contract runtime on `operation_key`: the record commits in billing's
  own transaction together with the debit (replay of the stored result even
  after a crash past the commit; `409` for the same key with other arguments;
  a concurrent duplicate waits and replays). The ledger's `UNIQUE` operation
  key stays as defence in depth.
- **Errors.** Billing services raise domain errors (`sandloom_billing.exceptions`:
  `InsufficientCreditsError` 402, `CreditOperationConflictError` 409,
  `BillingTenantMismatchError` 403); the contract converts them to
  `HTTPException` with the same status, identically for local and RPC callers.

```python
from sandloom.sdk.contracts import service

credits = service("billing.credits.v1")
balance = await credits.spend("user", user.user_id, 10, f"report:{report_id}", reason="report export")
```

Billing **consumes** `tenancy.membership.v1` (via `services/tenant_resolver.py`
and the `organization.subscription.active` fact) to resolve org membership,
through its contract, never an import.

## HTTP API

| Router | Prefix | Key endpoints |
| --- | --- | --- |
| Billing | `/api/v1/billing` | `GET /plans`, `GET /subscription`, `GET /credits`, `POST /checkout-session`, `POST /customer-portal` |
| Webhooks | `/api/v1/billing/webhooks` | `POST /stripe` |

Billing account: without `org_id` the account is always the caller's
personal account; an organization's account needs `org_id`. Membership of
exactly one organization never selects that organization's account. An
`org_id` in a deployment without the `tenancy.membership.v1` contract
answers `501`. Reading an organization's billing (`GET /subscription`,
`GET /credits` with `org_id`) needs membership (`403` otherwise);
**`POST /checkout-session` and `POST /customer-portal` need the org role
`admin` or `owner`** (`403` for plain members).

`GET /subscription` and `GET /credits` are pure, declared reads (execution
planner, a read-only transaction): they never write. A tenant without a
billing customer answers what a fresh customer answers (`{"status": "none",
…}` / balance `0`) and no customer row is created; the customer is created by
`POST /checkout-session` / `POST /customer-portal` (the operation intent),
which every credit purchase, subscription and webhook goes through. (Before
the change the two `GET`s created the customer on first use.)

Both run under an **operation intent** (`billing_operation_intents`,
ADR-0003 G1; an external call, so the route uses `operation_intent`): the
authorized intent is committed before the PSP call and provides its idempotency key, so a timeout (`503`) is retried with the same
`Idempotency-Key` and gets the same session; a definitive PSP rejection is
`502`. Right before the URL is returned the membership (`403` role lost, `409`
changed) and the caller's session (`401` revoked / blocked) are re-checked; a
refusal revokes the intent and expires the checkout session at the PSP (a
portal session cannot be expired — it is just never handed out). A retry of
an already issued checkout returns its stored URL after the same re-check, or
`410` once the session has expired (start a new checkout with a new key).

**Reconciler** (`billing.tasks.reconcile_operation_intents`, worker beat every
5 min, idempotent): an intent still `pending` after
`BILLING_INTENT_RECONCILE_AFTER_MINUTES` (default 30; its client never
retried) is looked up at the PSP by its marker — an open session is expired
and the intent `revoked`, no session → `failed`, a completed one is flagged
for an operator. A refused checkout whose expire failed in the request is
retried. Failed PSP steps are bounded by
`BILLING_INTENT_RECONCILE_MAX_ATTEMPTS` (default 5); then the intent gets
`followup_at` for an operator. An `issued` intent is never touched. Details:
[ADR-0003, "Intent reconciliation"](../../adr/0003-fewer-sync-rpc-membership-reads.md#intent-reconciliation-72).

The reconciler needs a worker and one beat; billing checks that they run it.
A reachable broker is not enough, because the broker can answer while no
worker or beat is running:

- every completed run writes its end time to `billing_reconciler_runs`. A run
  that raises writes nothing;
- the `billing_reconciler` health check fails when no run completed in the
  last 15 minutes (three periods). Its `state` says why: `never_ran` (no
  worker or no beat), or `overdue` (the worker, the beat or the broker
  stopped, or every run fails). The check is part of `/health` and `/ready`
  in every process that loads billing, so those processes stop being ready;
- a process waits 15 minutes after it starts before it judges
  (`startup_grace`), because a fresh or restarted deployment has not run the
  reconciler yet. In that time it reads nothing. After that it reads one row
  per health TTL;
- `sandloom doctor`'s `periodic` section fails when no beat holds the lease for
  this module set.

In development without a worker, `/ready` turns 503 on `billing_reconciler`
after 15 minutes. Start `celery -A sandloom.core.worker worker -B`, or ignore
that one check.

## Webhooks

- Signature is always verified; a bad or missing signature is `400`. Without
  `STRIPE_WEBHOOK_SECRET` webhooks are **rejected — also on the fake PSP**.
  Unsigned webhooks are rejected on the fake PSP too, in every environment
  (sign test payloads with `sandloom_billing.psp.sign_stripe_payload`).
- Duplicates (same event id) answer `{"status": "duplicate"}`.
- Subscriptions apply **monotonically**: an event older than the last applied
  one (Stripe `created`) is skipped; `customer.subscription.created` never
  overwrites a known subscription; `customer.subscription.deleted` always
  applies; `canceled` is terminal (a resubscription gets a new id).
  **Same-second limitation:** `created` has 1-second resolution, so events of
  the same second cannot be ordered by timestamp. On such a tie billing
  re-fetches the subscription from Stripe (`retrieve_subscription`) and applies
  its current state; if the PSP client lacks it or the fetch fails, ties apply
  in delivery order.
- `invoice.paid` only recovers `past_due` / `unpaid` → `active` and moves
  `current_period_end` forward; it never touches `trialing` or a terminal
  subscription, and the invoice's PSP customer must own the subscription.
- **Syncing is not granting.** Every subscription event's PSP state is
  stored, but entitlements are granted only for a subscription whose price
  is in the catalog — an unknown price (also after a change from a known
  one) grants nothing, is audited (`billing.subscription.unknown_price`) and
  flagged for an operator — and that was created through an operation intent,
  or existed before the policy, or is allowed by
  `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS=true` (default `false`) for
  intent-less subscriptions (operator-created, migrated, restored). A row
  inserted without an origin is intent-less (fail closed). Subscription
  events carry `data.grants_access` — derive entitlements from it (or the
  live contracts), never from `status` — and `data.plan` (`product`,
  `price_id`, `interval`; `null` without a catalog price). Upgrade: rows with no catalog price
  lose access (see ADR-0003 item 11, upgrade note).
- Only Stripe's subscription statuses are stored (anything else is journaled
  `failed`); `canceled` and `incomplete_expired` are terminal.
- Credit packs: `checkout.session.completed` with `payment_status=paid`, or
  `checkout.session.async_payment_succeeded`, grants the purchased price's
  `credits` (never an amount from metadata), with operation key
  `checkout:<session_id>`; `async_payment_failed` grants nothing. The session
  must carry `metadata.customer_id` matching its PSP customer, and its
  `amount_total` + `currency` must equal the catalog price (credit-pack
  checkouts are created without discounts or tax) — otherwise it is journaled
  `failed` and nothing is granted.
- **Intent gate.** A session / subscription billing created carries
  `metadata.sandloom_intent_id`; a credit grant or a new subscription applies only
  when that intent is `issued` for the event's customer — and only for the
  intent's own price, at most one subscription per intent. A completion for a
  revoked / unknown intent (or another price, or a second subscription) is
  journaled `failed`, audited
  (`billing.intent.unauthorized_completion`) and flagged for an operator
  (refund manually). Objects without the marker apply as before.
- **Refunds / disputes** of credit packs (`charge.refunded`,
  `charge.dispute.created` / `.updated` / `.closed`) reverse the purchase's
  credits with an append-only ledger entry: proportional to the refunded plus
  (while open or lost) disputed amount, rounded up, never more than granted;
  restored when the dispute is won (`billing.credits.restored.v1`); an
  inquiry reverses nothing (audited). A refund / dispute delivered before the
  purchase's completion is recorded and applied when the grant is created.
  The balance **may go negative** (credits already spent are owed; spending
  is refused until purchases cover it, `Credits(n)` answers `402`).
  `GET /credits` returns `{"balance": -60, "debt": 60, "in_debt": true}` —
  show it as a debt, not an error. Subscription access is not changed
  by a dispute; a dispute with no matching purchase is audited
  (`billing.charge.disputed`). Purchases from before this policy are not
  reversed automatically.
- **Portal changes** are not gated (Stripe does not link them to a portal
  session), but a plan / cancel-at-period-end change with no portal intent of
  the customer in the last `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES`
  (default 60) is audited `billing.subscription.unattributed_change`.
- Permanent data problems (unknown customer/price, bad metadata) are journaled
  as `failed` and acknowledged with `200` (`{"status": "failed"}`) so Stripe
  does not retry for days; transient errors raise, roll back and are
  redelivered.
- Outgoing Stripe requests pin `Stripe-Version: 2025-03-31.basil`; both the
  legacy and basil webhook payload shapes are read.

## Read and PSP behavior

Billing reads do not create customers: a tenant without billing state receives
subscription status `none` and a zero credit balance. Checkout and explicit
write paths create billing state when needed.

The Stripe adapter reuses a bounded HTTP connection pool and closes it at module
shutdown. HTTP 409 and 429 are retryable with the same idempotency key; exhausted
attempts remain ambiguous and eligible for intent reconciliation. Definitive
errors contain only bounded provider machine codes, never the raw error body.
One payment intent cannot create credit grants under multiple session IDs;
conflicts are journaled without a second grant or an endless 500 retry loop.

The webhook event-ID journal is a permanent dedupe tombstone store. Do not
delete it as routine log retention: that changes the replay guarantee. Storage
capacity planning and a future compact/archive policy must preserve event IDs.

## Published events

```
billing.subscription.created.v1
billing.subscription.updated.v1
billing.subscription.canceled.v1
billing.credits.added.v1
billing.credits.spent.v1
billing.credits.reversed.v1
billing.credits.restored.v1
```

All share one payload: `event_id`, `occurred_at`, `customer_id`, `tenant_type`
(`user` | `organization`), `tenant_id`, `data`. `credits.*` are emitted from the
billing service; the `subscription.*` channel is chosen by the Stripe
subscription status in the webhook service.

## Capabilities & configuration

- **Requires:** `database`, `eventbus`. Consumes `tenancy.membership.v1`
  as an optional contract: a personal-only deployment runs without tenancy.
- `BILLING_PROVIDER` — `fake` (default `FakeStripeClient`) or `stripe`.
  `BILLING_PROVIDER=stripe` **fails fast at startup** unless `STRIPE_SECRET_KEY`
  and `STRIPE_WEBHOOK_SECRET` are set.
- With `ENVIRONMENT=prod` the fake PSP is refused unless
  `SANDLOOM_ACCEPTED_RISKS=["billing.fake_psp_in_prod"]`. An explicit
  `BILLING_PROVIDER=fake`, like the implicit default, is refused by billing's
  own settings before it registers. The app, the worker, beat, `sandloom
  migrate` and the operator commands all stop. A process that does not load
  billing does not check it. These are billing's declared settings
  (`sandloom_billing.config.BillingSettings`): read them with
  `settings_of(BillingSettings)`.
- `BILLING_DENY_ON_PAST_DUE` — `past_due` loses access immediately (default:
  access kept until `current_period_end`).

## Notable conventions

- **Idempotency** on the PSP session endpoints with `fail_closed=True` —
  `idempotency_guard(scope="billing.checkout_session", fail_closed=True)`. See
  [Make a write idempotent](../http/idempotent-write.md#idempotency-modes).
- **Webhook idempotency** via the `billing_stripe_events` journal; tests sign
  payloads with `sandloom_billing.psp.sign_stripe_payload`.
- Credits modeled as a materialized balance plus an append-only ledger;
  overdraft is rejected by a guarded `UPDATE` and the ledger row rolled back.

## Gating an endpoint on billing

A billing gate is an authorization policy whose atom names the paying account
with a required `of=`. `sandloom generate` writes billing's atoms from billing's
declarations. A route imports them from the consumer's
`<app>.shared.contracts.billing_entitlements_v1`; they are not `sandloom.sdk`
names. The atoms, the `Account` values and the `402` / `501`
answers are in
[Authorization](../http/authorization.md#billing-subjects-of-is-required).

<!-- code-ok: D1 fragment, a route decorator without its handler -->
```python
from fastapi import Depends

from myapp.shared.contracts.billing_entitlements_v1 import Subscribed
from sandloom.sdk import require
from sandloom.sdk.authz import Account  # sandloom: advanced account target

@router.get("/report", dependencies=[Depends(require(Subscribed(of=Account.user())))])
```

## Platform owner operations

The owner console uses `/api/v1/platform/billing/*`, guarded by the shared
platform-admin policy (successful identity provenance plus global `admin`) and
the live session check. Read APIs expose bounded views of customers,
subscriptions, products/prices, balances, ledger, credit grants, webhook journal,
operation intents and refund/dispute state. Filters reference billing-owned UUIDs;
there is no request-time join to identity or tenancy tables.

`PATCH /products/{record_id}` (admin risk) changes only display name / availability,
requires a reason, and records an audit. Prices remain immutable in the console.
`POST /customers/{record_id}/adjust-credits` (money risk) takes direction, positive integer
amount, reason and UUID operation reference. It locks the customer, binds the
reference to delta and reason, and commits the existing ledger effect and audit
in one UoW. Replays return the original resulting balance; mismatched payloads
return `409`. Both writes re-check the session before commit. Deductions never
create debt; verified PSP reversals retain their separate debt behavior.

`GET /provider` exposes only provider/mode and a fixed dashboard URL. No API keys,
checkout bearer URLs, request digests or raw PSP errors are serialized. Monetary
refunds, invoice operations and provider subscription changes use the separately
authenticated Stripe dashboard, followed by verified webhook synchronization.
The local refund/dispute view is event-derived and is not a complete PSP feed.
See [operator guide](../../admin-console/operations.md) for retry and follow-up handling.
