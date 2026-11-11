# Ledger delta: KB-11, the trusted claims view, one platform admission, per-key `cpv`

KB-11 (2026-11-09) builds ADR-0014 decisions 3 and 4 and the owner's
amendment of 2026-11-09 on the `cpv` identity of an authority.

| Id | Disposition |
| --- | --- |
| G-AUTH-23 | New: every reader of a verified access token sees only its trusted view (one condition per key: allowed authority, per-key `cpv` digest of the assigned import path, kept source); 401 `token_policy_outdated` / 503 `claims_policy_mismatch` / 403; one platform admission for `require_platform_admin` and the platform console's operator gate, never trusting a source named `identity`; enforced by `test_claims_view.py` and trust row T-1. |
| G-AUTH-24 | New (owner decision 2026-11-09): the `cpv` digest is (key, assigned import path), local or remote; a remote authority is checked independently by its pinned `service_id` (pair MAC) and by the authority binding of its `<m>.claims.v1` answer; enforced by `test_claims_cpv_identity.py`. Gap (S): an assignment revision that invalidates old tokens without a new import path is not built. |
| G-RISK-14 | Retain, statement unchanged: the window-end, window and expiry tests replace most of the gap; the gap narrows (M to S) to the `/diagnostics/core` and `sandloom doctor` display and the one-mode-per-profile check (T014). |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
