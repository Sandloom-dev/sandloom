# Kernel boundary: goals

What the kernel-boundary work (tm `kb-assess`) is for, agreed by the owner on 2026-11-08. The problems are in [problems.md](problems.md), the invariants in [requirements.md](requirements.md).

## The goal

The kernel (`sandloom.core`, `sandloom.sdk`, `sandloom.shared`, `sandloom.main`) is independent of the **implementations** of identity, tenancy, billing and admin. It owns the **general protocol of security and isolation** that every module and every service relies on.

## What the rule "the kernel knows no business module" means

It is a rule about **source dependencies and shipped code**: the kernel does not import a business module, does not name one (its contracts, events, settings, claim keys, commands), and does not ship a module's code or artifacts.

It is **not** an absolute semantic requirement. The kernel must understand these concepts, even though identity and tenancy are what implement them today:

- the authenticated subject;
- trusted claims and who may issue them;
- rights (roles, permissions);
- the tenant and tenant isolation;
- an administrative operation;
- token revocation.

Having these concepts in the kernel is not an architecture error.

## What is an error

The kernel holding a **specific module's model or binding** of such a concept, for example:

- a tenant is necessarily an organization with the roles `member` / `admin` / `owner`;
- access is granted only through the contract `billing.entitlements.v1`;
- a right is trusted only when the source is named `"identity"`;
- a subscription has the statuses `active` / `trialing`.

## How to apply it

For any concept in the kernel, ask:

1. Is this the general protocol every module and service must agree on (token format, claim trust, tenant scope, revocation, the authorization algebra)? Then it belongs to the kernel, stated in kernel terms, with no module's names or data model.
2. Is this one module's model, policy, data or binding? Then it belongs to that module, and the kernel at most offers a port the module fills.
3. Could a third-party module replace the first-party one without a kernel change? If not, the kernel holds a specific implementation.

Both kinds of mistake are possible: leaving a module's model in the kernel, and pushing a general protocol out of it. The second weakens the guarantees the kernel exists to give.
