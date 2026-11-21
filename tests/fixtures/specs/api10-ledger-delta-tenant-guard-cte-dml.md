# Ledger delta: DML in a CTE and unscoped() across tasks (tenant-guard-cte-dml)

A statement whose compiled SQL carries an INSERT/UPDATE/DELETE CTE is refused outside
`unscoped()`, whatever table the DML targets, wherever the statement tree hangs the CTE
and whatever runs it. The `unscoped()` bypass is bounded
by its block: tasks, nested blocks and event deliveries.

| Id | Disposition |
| --- | --- |
| G-TENANT-19 | New: any DML CTE (no table matching) is refused with `UnsafeTenantOperationError` (`MissingTenantError` with no tenant bound) from the compiled statement, through a session, `session.connection()` or an engine; a SELECT-only CTE stays allowed and scoped, `unscoped()` admits the DML CTE; enforced by `test_tenant_guard_cte_dml.py`. |
| G-TENANT-20 | New: the `unscoped()` bypass is bounded by its block (a task that outlives it is guarded again, a task created and finished inside keeps it, nested blocks hold it while any is open, a stray value does not crash the guard, an event delivery runs guarded); enforced by `test_tenant_guard_cte_dml.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
