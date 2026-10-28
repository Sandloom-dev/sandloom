---
title: Modules
---

# Standard Modules

Sandloom ships three production-ready, **opt-in** SaaS modules, each as its own
distribution: `sandloom-identity`, `sandloom-tenancy` and `sandloom-billing`
(import packages `sandloom_identity`, `sandloom_tenancy`, `sandloom_billing`;
all three with `pip install "sandloom[standard]"`). Each registers a
`sandloom.modules` entry point whose name is its module name, depends on
`sandloom>=0.7.0a1,<0.8` and on no other module, and follows the same isolation
rules as a module you write yourself
([Module packages](../concepts/modules.md#module-packages)). Load any subset via
`SANDLOOM_MODULES` (`["identity", "tenancy"]`), or `sandloom new <app> --with
identity,tenancy,billing`.

| Module | Required | Provides | Publishes | Consumes |
| --- | --- | --- | --- | --- |
| [Identity](identity.md) | ✅ | `identity.claims.v1` (roles/permissions, critical), auth guards, token issuance | 15 `identity.*` events | every registered `<module>.claims.v1` at token mint |
| [Tenancy](tenancy.md) | ❌ | `tenancy.membership.v1` (read-only), `tenancy.claims.v1` (`tenant_ids`, critical) | 9 `tenancy.*` events | — |
| [Billing](billing.md) | ❌ | `billing.entitlements.v1`, `billing.credits.v1`, `billing.claims.v1`; access facts `subscription.active`, `organization.subscription.active` | 5 `billing.*` events | `tenancy.membership.v1` |

`<module>.claims.v1` contracts are registered automatically by
`add_claims_contributor`; a token issuer in another process collects them over
internal RPC when they are bound in `SERVICE_BINDINGS`.

Every service contract takes **no database session**: each call runs in the
provider's own Unit of Work on the provider's database, identically in-process
and over RPC. Command contracts (`billing.credits.v1.spend`) are idempotent on
their operation key and are **not** atomic with the caller's transaction.

These modules talk to each other only through versioned events and service
contracts — never direct imports — exactly like the
[isolation rules](../concepts/modules.md) require. Your app calls their
contracts through their stubs in `sandloom.shared.contracts` (shipped with
`sandloom`, with the generated copies of their contract DTOs; `sandloom
generate` also writes stubs and copies into the app's shared package).
