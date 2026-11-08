# Kernel boundary: the problems, in short

Index of the architecture problems found by the kernel-boundary assessment (tm `kb-assess`). Evidence lives in [baseline.md](baseline.md) (commit b8d8506); invariants `I-xx` in [requirements.md](requirements.md). This file lists problems, not solutions: the solutions are decided in the ADRs.

| # | Problem | Priority | Root | Invariants | Status / tm |
|---|---|---|---|---|---|
| 1 | **Claims trust.** Claims contributors were merged last-writer-wins: any module could overwrite `roles`, `permissions`, `tenant_ids` and token fields (`iat_us`, `jti`, `auth_time`…); a source name in `claims_sources` did not prove who wrote a key (baseline §14). | P0 security | R3 | I-08, I-09 | Fixed for the monolith and split deploy by one owner per key and reserved keys (`roles-claim-trust`, PR #70, in review). Open: without the identity module the first writer of `roles` owns it; who assigns a claim's authority (ADR-C). |
| 2 | **The kernel names specific modules.** Names, contracts and rules of identity, billing, tenancy and admin are written into kernel code (40 class (b) edges; 32 violations, 13 disputed after verdicts). | P1 | R1 | I-02, I-03, I-13 | ADR-A (`kb-alt-boundary`) |
| 3 | **`sandloom.shared` mixes owners.** It holds platform mechanisms (subject, authz engine) next to domain authorization, module DTOs, contract stubs and event schemas. | P1 | R1, R2 | I-03, I-11 | ADR-A, ADR-B |
| 4 | **Contract ownership.** Module contracts, DTO copies and 34 event schemas ship in the `sandloom` wheel, though modules version independently; billing reads tenancy's `MemberState` through the kernel wheel. | P1 | R2 | I-05, I-11, I-12 | ADR-B (`kb-alt-contracts`, `kb-poc-contracts`) |
| 5 | **Authorization bound to the standard modules.** `Subscribed`, `SubscriptionActive`, `Credits`, `OrgRole`, `Account.org`, the two live-adapter kinds and `WELL_KNOWN_FACT_OWNERS` carry billing/tenancy semantics. | P1 | R1, R3 | I-07, I-13 | ADR-C (`kb-alt-authz`, `kb-poc-authz`) |
| 6 | **Platform authorization bound to identity.** The platform-admin gate trusts the literal source `"identity"`, not a mechanism that assigns the trusted source of a right. | P1 security | R3 | I-08, I-09 | ADR-C; partly mitigated by #1's fix |
| 7 | **Module settings in the kernel.** Stripe, 2FA, SSO, billing and admin settings, their validators and accepted risks live in kernel config; there is no way for a module to declare its own. | P1 | R2 | I-13, I-18 | ADR-D (`kb-alt-settings`) |
| 8 | **Blind guards.** import-linter does not analyse `sandloom_*`; the `shared` purity test sees only `sandloom.driverkit`; the wheel-import check accepts imports through extras; G-KERNEL-13 covers 4 directories. | P1 | R4 | I-02, I-03 | `kb-guards-honest` |
| 9 | **Infrastructure not optional in practice.** `sandloom` + `sandloom-billing` as published fails to boot without celery, an optional extra of the framework. | P1 | R2, R4 | I-19 | `billing-boots-without-celery` |
| 10 | **CLI and generators know the standard modules.** Identity/tenancy commands, deploy env, the admin manifest gate, templates and recipes name modules. | P2 | R1 | I-03, I-13 | After A–D |
| 11 | **Incomplete architecture graph.** `sandloom graph` is built from declarations only; it does not show kernel → module edges, contract names in kernel code, settings or claims authority. | P2 | R4 | — | Recorded in baseline; no change without its own ADR |
| 12 | **Deployment and version independence not proven.** No consumer-only install test, no mixed-version matrix, split-deploy scenarios partly covered. | P2 evidence gap | R4 | I-05, I-06, I-12 | `kb-poc-contracts`, `kb-poc-authz` |

## Root causes

- **R1 Responsibility boundary.** The kernel provides generic mechanisms and also owns part of the standard modules' domain model.
- **R2 Ownership under independent versioning.** Interfaces and settings of modules are owned and shipped by the kernel.
- **R3 Trust model.** The trust in security claims and in the sources of rights is not defined by a mechanism.
- **R4 Verifiability.** Some guarantees are written down but not enforced by tests or checks.

Not every problem needs a new mechanism: removing R1–R4 removes most of them. The most dangerous one is #1, fixed separately from the larger work; the most fundamental are #2–#5.
