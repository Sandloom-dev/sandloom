# Ledger delta: KB-34, the kernel ships no domain atoms or live adapters

KB-34 (2026-11-09, ADR-0014 decisions 8, 14 and 15; KB-34g and KB-34h) deletes
the kernel's billing and tenancy atoms, the live-adapter registry and the
kernel's table of well-known fact owners. Every removed name has a generated or
module-owned replacement, or none because the mechanism is gone; no alias stays.
A consumer's imports change, the names do not.

Removed public names (`sandloom.sdk`, `sandloom.sdk.authz` and
`sandloom.shared.authz`; the root re-exported four of them):

| Removed name | Replacement |
| --- | --- |
| `OrgRole` | the atom generated from `tenancy.membership.v1.has_role_at_least`: `<app>.shared.contracts.tenancy_membership_v1.OrgRole` |
| `SubscriptionActive`, `Credits` | the atoms generated from `billing.entitlements.v1`: `<app>.shared.contracts.billing_entitlements_v1` |
| `Subscribed` | billing's generated claim fact check, same module |
| `Feature`, `WithinLimit` | billing's generated claim read checks, same module |
| `Roles`, `Permissions` | `sandloom_identity.rbac` (no `sandloom.sdk` alias) |
| `ACTIVE_SUBSCRIPTION_STATUSES` | none: billing declares its statuses; `Subscribed(statuses=...)` names them |
| `register_live_adapter`, `ENTITLEMENTS`, `MEMBERSHIP`, `EntitlementsAdapter`, `MembershipAdapter` | none: an atom asks the provider's contract named by its `access_check` declaration; a third party serves that contract (`access_providers`, `SERVICE_BINDINGS`) |
| `WELL_KNOWN_FACT_OWNERS` (not public) | the module that registers an `AccessFact` is its owner (`owner_module`); `Entitled(..., owner=)` and `fake_access_fact(..., owner=)` state it |

`Entitled` and `fake_access_fact` gain the keyword `owner=` (a policy that
names the owner accepts only that module's fact). The `adapter=` argument of
the removed atoms is gone with them. The `sandloom.sdk` root re-exports 20
code names (26 with the five `sandloom.sdk.testing` names and the `client`
fixture); it was 24 (30) before. The upgrade table is
`docs/operate/upgrade-1.0.md`.

| Id | Disposition |
| --- | --- |
| G-AUTHZ-05 | Restated, not weakened: a third-party provider's access checks (the `SubscriptionActive`, `Credits` and `OrgRole` shapes) decide what the standard contracts' generated atoms decide on the same inputs, in-process under `access_providers` and over `SERVICE_BINDINGS`; the claim is no longer made against kernel atoms, which no longer exist. Enforced by `tests/integration/test_access_providers_split.py` and `tests/integration/test_shared_authz_review.py`. The two tests that registered a kernel live adapter are removed with the adapter registry. |
| G-AUTHZ-09 | Restated: billing's generated `Subscribed`, `SubscriptionActive`, `Credits`, `Feature` and `WithinLimit` require `of=` (keyword-only in the generated signature); a non-`Account` value still raises `AuthzSubjectMissingError` (SANDLOOM-B017). Tests unchanged. |
| G-AUTHZ-12 | Restated: the kernel ships no `Feature` / `WithinLimit`; billing's generated ones keep the claim and live behaviour (zero I/O with `Account.user()`, one live check for an organization, 501 without a source). Tests unchanged. |
| G-SDK-04 | Restated: generated code and examples use only the 26 beginner names (30 before KB-34); a name outside them needs `# sandloom: advanced <reason>` (SANDLOOM-T009). Enforced by the same checks. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
