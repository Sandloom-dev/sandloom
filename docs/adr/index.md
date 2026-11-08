# Architecture Decision Records

Architecture Decision Records (ADRs) capture significant, hard-to-reverse
decisions about the Sandloom framework — the context that forced the decision, the
decision itself, and its consequences. They are living documents: superseded
ADRs are kept for history and marked accordingly.

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-framework-distribution.md) | Framework distribution & packaging | Accepted |
| [0002](0002-framework-absorbs-module-boilerplate.md) | The framework absorbs per-module boilerplate | Accepted |
| [0003](0003-fewer-sync-rpc-membership-reads.md) | Fewer synchronous RPC calls in split deployments — membership reads first | Proposed |
| [0004](0004-rpc-pair-mac-request-authentication.md) | Internal RPC request authentication with per-pair HMAC keys | Accepted (draft) |
| [0005](0005-effect-model.md) | One effect model — guarantees, the dedupe record and the commit pipeline | Accepted |
| [0006](0006-accepted-risks.md) | One accepted-risks list instead of prod opt-out flags | Accepted |
| [0007](0007-kernel-layers.md) | Kernel layers and an acyclic kernel, enforced by import contracts | Accepted |
| [0008](0008-sagas.md) | Sagas — a small orchestrator over the existing effect primitives | Accepted |
| [0009](0009-route-policy-and-session-freshness.md) | Route policy follows authority and effect: deny-by-default, live sessions, no route classes (amends 0003) | Accepted |
| [0010](0010-public-api-1.0.md) | Public API 1.0 — one `sandloom.sdk` namespace, three API levels, derived declarations (amends 0005, 0007, 0009; amendment of 2026-11-08 accepted: generated consumer API) | Proposed |
| [0011](0011-driver-api.md) | The driver API — two doors for one contract, drivers found by name (amended 2026-11-05: one SQL driver for several databases) | Accepted |
| [0012](0012-kernel-boundary.md) | The kernel boundary — what the kernel may name, the standard composition outside it, one edge register (amends 0001, 0007) | Accepted |
| [0013](0013-contract-ownership.md) | Contract artifacts belong to the distribution that consumes them (amends 0010) | Accepted |
| [0014](0014-claims-trust-and-authorization-portability.md) | Claims trust assigned by the application; domain authorization atoms generated from contracts (amends 0009, 0010) | Accepted |
| [0015](0015-module-settings.md) | Modules declare their own settings; the kernel validates them from one read (amends 0006, 0010) | Accepted |
