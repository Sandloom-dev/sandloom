# Ledger delta: KB-33, `Feature` and `WithinLimit` as billing's generated atoms

KB-33 (2026-11-09, ADR-0014 decision 15, owner answer #21) moves `Feature`
and `WithinLimit` to billing as generated claim atoms: billing declares them
with `claim_atom` above two access checks of `billing.entitlements.v1`
(`has_feature(tenant_type, tenant_id, key)`,
`is_within_limit(tenant_type, tenant_id, key, used)`), and `sandloom generate`
writes `Feature`, `WithinLimit`, `LiveFeature` and `LiveWithinLimit` beside a
consumer's stub. The kernel atoms stay (removal is KB-34, never automatic);
no caller changes.

Research decision: claim atoms, not access atoms. For `Account.user()` the
atom reads the token's `features` / `limits` claim with zero I/O, as the
kernel atom does (an access atom would add a call per decision and change the
freshness, I-23). For an organization account it asks billing's access check
live with its parameters as contract arguments, so they cross the internal RPC
unchanged; the kernel atom's remote path (`<owner>.facts.v1.evaluate`) sends
no parameters.

| Id | Disposition |
| --- | --- |
| G-AUTHZ-12 | Strengthened (the kernel atoms' statement unchanged): billing's generated atoms decide as the kernel atoms on every trust case of the claims (present, absent, authority failed at the mint 503, the view's 401 / 503, unassigned, legacy token; under `Not` and `Or`) with zero calls, and for an organization account ask billing's live check once with their parameters, mono and split; no provider is 501, never an allow. Enforced by `src/sandloom/core/tests/test_claim_read_atoms.py` and `tests/integration/with_batteries/test_plan_atoms.py`. |

Counts (steady state, `test_plan_atoms.py`): the claims tier 0 calls / 0
database round trips / 0 RPC; an organization account 1 call / 0 database
round trips (billing's plan reads touch none), split 1 RPC; a bound
organization tenant 2 calls / 3 database round trips (the tenant's kind
first), equal to the kernel atom on a test producer.

Billing's plans declare no features or limits: its methods answer `False`
for every account, the same "no" as the claims it contributes (none). A plan
entitlements source in billing is the owner's decision before KB-34 removes
the kernel atoms (today a custom producer registers `organization.features`
/ `organization.limits`).

New public names (advanced, `sandloom.sdk.authz`): `claim_atom`,
`ClaimReadCheck`. A claim atom's read (`read`, `entry`, `below`) is part of
the contract's describe document and snapshot: changing it is incompatible
(SANDLOOM-T004).

No guarantee, enforcing assertion, or residual-risk requirement is removed.
