# Ledger delta: DML in a CTE and unscoped() across tasks (tenant-guard-cte-dml)

Under a bound tenant, a statement whose compiled SQL carries an INSERT/UPDATE/DELETE
CTE that reaches a TenantScoped table is refused outside `unscoped()`, wherever the
statement tree hangs the CTE and whatever runs it. The `unscoped()` bypass is bounded
by its block: tasks, nested blocks and event deliveries.

| Id | Disposition |
| --- | --- |
| G-TENANT-19 | New: a DML CTE that reaches a tenant table (matched by schema and name) is refused with `UnsafeTenantOperationError` from the compiled statement, through a session, `session.connection()` or an engine; `text()` or loader options inside the DML are refused too; a SELECT-only CTE and a DML CTE on a non-tenant table stay allowed, `unscoped()` admits the DML CTE; enforced by `test_tenant_guard_cte_dml.py`. |
| G-TENANT-20 | New: the `unscoped()` bypass is bounded by its block (a task that outlives it is guarded again, a task created and finished inside keeps it, nested blocks hold it while any is open, a stray value does not crash the guard, an event delivery runs guarded); enforced by `test_tenant_guard_cte_dml.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
