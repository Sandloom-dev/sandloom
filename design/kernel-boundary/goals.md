# Kernel boundary: goals

What the kernel-boundary work (tm `kb-assess`) is for, agreed by the owner on 2026-11-08. The problems are in [problems.md](problems.md), the invariants in [requirements.md](requirements.md). Amended the same day with the owner's answers to the 31 questions ([owner-answers.md](owner-answers.md), cited as "answer #n") and the roadmap-review amendments G1-G4 ([roadmap-review.md](roadmap-review.md) §6.2), which agree with them.

## The goal

The kernel (`sandloom.core`, `sandloom.sdk`, `sandloom.shared`, `sandloom.main`) is independent of the **implementations** of identity, tenancy, billing and admin. It owns the **general protocol of security and isolation** that every module and every service relies on.

## Who owns what

| Party | Owns |
|---|---|
| **Kernel** | The universal guarantees and protocols every Sandloom application needs. |
| **Batteries** (identity, tenancy, billing, admin, and any third-party module) | Implementations of those protocols, and their own domain models. |
| **Application** | The choice of implementations, and the assignment of trusted authorities (which module is trusted to issue which right or claim). |

**No platform guarantee may silently disappear because an optional module is absent.** Without the module that implements a protocol, the guarantee still holds (fail closed) or the application refuses to start and says what is missing; it never degrades quietly.

## Guarantee, provider, trust

Three things that must stay separate:

1. **Guarantee:** what Sandloom must ensure whichever battery is chosen.
2. **Provider:** who supplies the data or performs the domain operation.
3. **Trust:** why the kernel may use that provider's result.

A contract that returns data does not make its provider a trusted source of rights. A security authority (who may issue `roles`, `permissions`, `tenant_ids`, and every billing claim used to grant access) is assigned in the application's composition root code, not in ordinary settings, and the kernel verifies that the claimed owner is the module actually registered or the remote service bound to it in trusted configuration (answers #9, #10). A module naming itself `identity` is not proof.

Five trust roles, kept apart (roadmap-review G1, N10):

| role | what it is | what it is not |
|---|---|---|
| **Issuer** | signs tokens | not, by signing, the authority of every claim in them |
| **Claim authority** | the writer the application assigned for a security claim; may differ from the issuer | not whoever registered first, or under a matching name |
| **Binding identity** | the service bound in trusted configuration (`SERVICE_BINDINGS`, pinned `service_id`) | not a contract name or a binding key's text |
| **Claim source** | a contributor that ran, recorded in provenance | not proof that it wrote a given key |
| **Verifier** | checks signature, issuer, audience, revocation and the authority policy | not a judge of claims the policy does not cover |

The platform-gate defect found by the gate probe is "claim source taken for claim authority": the gate trusts that `identity` ran, not that it wrote `roles` (roadmap-review §2.2).

## Threat model

Installed module code runs in the process and is not sandboxed. The kernel protects against wrong or unauthorised claims through its supported extension APIs and from remote providers: a module cannot raise a user's rights by writing a claim it is not assigned, by its name, by an `owner=` argument, by serving another module's claims or facts contract, or by registering first. Minting is one of those APIs: `create_access_token` takes protected claims and provenance only from the verified result of `collect_claims`, the kernel forms the system fields, and an allowed service or test token takes a limited path that grants no user rights (answer #13). Out of scope, as in `docs/advanced/security-model.md:548-566`: hostile code inside a process, which can read the signing key and sign anything; the holder of the signing key; a compromised authority, whose own claims are wrong (research-trust.md §2.2-§2.3). These checks make mistakes loud and composition explicit; process isolation is the only answer to hostile code.

## The platform capability rule

The kernel defines a port only when it must keep a guarantee of its own and does not have the data. All of these must hold:

- the capability serves a platform guarantee, not one business function;
- its contract can be stated without identity, billing or tenancy specifics;
- the kernel defines a testable semantics of execution and failure;
- an implementation can be replaced without a kernel change;
- the absence of an implementation has explicitly defined behaviour;
- it works locally and, where the guarantee requires it, over RPC.

Otherwise it is a module's domain contract, or a wrongly drawn abstraction. Applied today: claims ownership and module settings need the existing mechanisms strengthened; authorization uses access facts; contracts, tenant isolation and JWT need no new port. Billing is not a platform port at all: it publishes its contracts and facts, and convenient names like `Subscribed` belong to billing or to generated consumer wrappers (developer experience, not platform architecture).

Limits:

- **No second driver system for business modules.** No mandatory `IdentityDriver`, `TenancyDriver`, `BillingDriver`, `AdminDriver`: that would make four standard batteries part of the kernel. Infrastructure drivers replace a technical implementation; batteries register only the capabilities they implement.
- **No universal capability registry** until an existing mechanism (module registration, contracts, claims contributors, access facts, drivers) shows a proven limit. Otherwise Sandloom gets three competing systems.

## Optional package, optional capability, security guarantee

- **Optional package:** a module may be absent from a deployment.
- **Optional capability:** an application may do without the function.
- **Security guarantee:** never optional, and never weakened silently.

Identity may be an optional package, but an application with authenticated routes must have a working authentication scheme. Tenancy may be absent, but declared tenant isolation keeps holding: no tenant is accepted from a header without a trusted grant. Billing may be absent, but a policy that needs its fact denies instead of allowing. Without a battery, either the capability is not used, another provider supplies it, or the configuration does not start.

## Replaceability decides the scope

Before moving code, replace each standard battery with a test implementation outside the kernel (identity: trusted roles through the claims mechanism; tenancy: an independent provider of tenant grants; billing: an unrelated fact such as `inventory.stock.available`). Where a replacement works without a kernel change, no new mechanism is needed; where it does not, the failure names the real abstraction gap.

## Authorization: the mechanism stays in the kernel

Moving batteries out does not move the mechanism that makes the final access decision. The algebra is not rewritten; only the source of the atoms changes.

| Component | Where |
|---|---|
| `Requirement`, `&` `\|` `~`, allow / deny / indeterminate, evaluation order and short-circuit, fail-closed and denial categories | kernel |
| `require`, `authorize`, `AccessFact`, `Entitled`, `TenantScope`, `Owner` (if generic) | kernel / SDK |
| `Role`, `Perm` | kernel / SDK, once claim provenance is enforced |
| `require_platform_admin` | kernel / SDK; one platform admission shared with `platform_operator`, trusting `admin` only from the assigned authority |
| `Credits`, `Subscribed`, `SubscriptionActive` | billing |
| `OrgRole`, `require_org_role`, the role vocabularies (`member/admin/owner`, `admin/support/ops`, `perm.users.*`) | tenancy / identity |

Three problems are solved together, never one alone: **claim provenance** (who may issue a claim, and how it is checked); **policy portability** (a typed domain atom works in a consumer process without the provider's implementation installed); **policy metadata** (architecture checks read verifiable metadata — needs a tenant, needs an authority, live check, administrative action, money — not class names, and a module cannot lower its own risk class).

**Moving a domain atom out of the kernel must not by itself change the number of network calls, the number of database queries, the evaluation order or the policy syntax.** The three execution tiers stay: claims in memory, a local fact in the same process, a remote fact only when its owner is in another process.

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

Removing `sandloom.shared` is not a goal. The goal is that the framework owns no module's interface or artifact; where the remaining platform primitives live is secondary (roadmap-review G3, R4).

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

A check judges shipped runtime code. A module's name in a template, a recipe, documentation or a kernel test is classified separately and is not by itself a runtime edge; tests are outside the shipped kernel boundary, and the kernel's unit and architecture tests run without batteries (roadmap-review G4; answer #2).
