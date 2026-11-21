# Ledger delta: the tenant guard on the legacy bulk and hidden-SQL read paths (tenant-guard-read-paths)

The legacy `Session.bulk_*` methods on a tenant-scoped mapper are refused outside
`unscoped()`. SQL the statement tree hides from the guard (loader options, multi-row INSERT
values, column defaults, bare functions) is scoped or refused. Two `sandloom check` rules keep
a generator from handing the bypass to its caller and a module from building its own engine.

| Id | Disposition |
| --- | --- |
| G-TENANT-21 | New: `Session.bulk_save_objects`, `bulk_insert_mappings` and `bulk_update_mappings` on a tenant-scoped mapper are refused with `UnsafeTenantOperationError` outside `unscoped()`, a non-tenant mapper stays allowed; enforced by `test_tenant_guard_read_paths.py`. |
| G-TENANT-22 | New: a statement that reads a tenant-scoped table through `with_expression`, `with_loader_criteria`, a `Load` extra criteria, INSERT values (multi-row too), a SQL-expression column default or onupdate, or a bare `func.*` executable is scoped or refused, and a flush whose column default reads a tenant table is refused; enforced by `test_tenant_guard_read_paths.py`. |
| G-TENANT-23 | New: `sandloom check` refuses a `yield` inside `with unscoped()` (`yield-inside-unscoped`, `SANDLOOM-C036`), a `@contextmanager` is exempt; enforced by `test_archcheck_unscoped_engine_rules.py`. |
| G-TENANT-24 | New: `sandloom check` refuses an engine a module builds itself (`engine-in-module`, `SANDLOOM-C037`); enforced by `test_archcheck_unscoped_engine_rules.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
