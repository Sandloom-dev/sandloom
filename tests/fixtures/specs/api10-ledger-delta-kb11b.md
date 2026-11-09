# Ledger delta: KB-11b, the legacy_tokens mode shown and kept one per deployment

KB-11b (2026-11-09) finishes KB-11 (ADR-0014 decision 4): the `legacy_tokens`
mode in `/diagnostics/core` and `sandloom doctor`, one mode per deploy
profile (SANDLOOM-T014) with the adoption release gate, the issuer x
verifier matrix across processes and the T-14 I/O counts.

| Id | Disposition |
| --- | --- |
| G-RISK-14 | Gap closed, statement unchanged: `/diagnostics/core` (`claims.legacy_tokens`) and `sandloom doctor` (section `claims`) show the mode (`on` with `until`, the window end and the time left; `ended`; `off`), diagnostics with the per-key acceptances of `claims_policy_legacy_tokens_total`; the deploy-profile boot check refuses services that disagree on `legacy_tokens` and, once one enforces, a service below the adoption release; the initial enablement stages 1-3 and rollback run on real tokens, split and mono with mixed replicas. Enforced by `test_claims_legacy_mode.py` and `test_claims_split_matrix.py`. |
| G-AUTH-23 | Tests added, statement unchanged: the identity -> acme_identity key change (steps 0-3, rollback a-c, negative rows, a previous-release token without `cpv`), the change without `previous=` answering 503 in either order, and the one-service sequence, on real signed tokens between separate issuer and verifier processes with the grant's authority over the keyed RPC (`test_claims_split_matrix.py`); T-14: `Role`, `Perm`, `TenantScope` cost DB 0, one cache round trip, RPC 0 per request, mono and split (`test_claims_io_counts.py`). |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
