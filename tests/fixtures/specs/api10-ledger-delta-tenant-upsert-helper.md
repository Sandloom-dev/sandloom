# Ledger delta: `tenant_upsert` builds a tenant-scoped upsert from plain rows (tenant-upsert-helper)

`conflict_insert` on a tenant-scoped table stays refused outside `unscoped()`. The one way to
run `INSERT ... ON CONFLICT DO UPDATE` there is `tenant_upsert(Model, rows, update=[...])` from
`sandloom.sdk.data`. It takes plain Python rows and builds the statement itself. The cursor
admits that statement only for the helper's private marker together with the exact shape the
helper builds, and verifies every row's tenant value and the tenant `WHERE` of the update
(ADR-0018, decision 6).

| Id | Disposition |
| --- | --- |
| G-TENANT-27 | New: `tenant_upsert` is the one way to run an upsert on a tenant table outside `unscoped()`; the cursor admits it only for the private marker plus the exact statement shape, with every row carrying the bound tenant and the `DO UPDATE` limited to `excluded` values under a tenant `WHERE`, and every other conflict insert on a tenant table stays refused; enforced by `test_tenant_upsert.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
