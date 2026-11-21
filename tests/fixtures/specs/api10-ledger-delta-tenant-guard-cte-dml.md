# Ledger delta: DML in a CTE and unscoped() across tasks (tenant-guard-cte-dml)

Under a bound tenant, a statement carrying an INSERT/UPDATE/DELETE CTE that
touches a TenantScoped table is refused outside `unscoped()`, wherever the CTE
is attached. A task created inside `unscoped()` does not keep the bypass after
the block exits.

| Id | Disposition |
| --- | --- |
| G-TENANT-19 | New: every CTE shape holding DML on a tenant table is refused with `UnsafeTenantOperationError`, a SELECT-only CTE and a DML CTE on a non-tenant table stay allowed, `unscoped()` admits the DML CTE, and a task that outlives the `unscoped()` block is guarded again; enforced by `test_tenant_guard_cte_dml.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
