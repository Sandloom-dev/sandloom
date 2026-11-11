# Ledger delta: KB-proto-operator, operator commands resolve their owners

The operator commands stop naming `identity.security.v1` and
`tenancy.membership.v1` (edges B-R4, B-R5). Each operation resolves its owner
from the composition's claim-authority map (the issuer, the roles owner, the
tenant_ids owner) and confirms the method by describe before any side effect,
a dry run included (SANDLOOM-B039 / B040). `sandloom security revoke-sessions`
is a platform guarantee for any issuer (owner decision 2026-11-09).

| Id | Disposition |
| --- | --- |
| C-OPS-08 | Restated and strengthened: incident response still runs in keyed, deduplicated batches through the owners' contracts, now resolved from the claim map (issuer, tenant_ids owner) and confirmed by describe before any side effect; the kernel revokes every access token itself for any issuer in one durable unit (audit row, mark before commit, `core.auth.tokens_revoked.v1` applied by every service); the issuer ends its refresh sessions first; the report names each kind; a forced reset where the issuer has no passwords is refused with no audit row and no mark. One test renamed (`test_tenant_targets_page_through_membership` -> `test_tenant_targets_page_through_the_tenant_ids_owner`), ten added. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
