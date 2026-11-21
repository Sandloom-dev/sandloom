# Ledger delta: the tenant guard on the legacy bulk and hidden-SQL read paths, at the cursor (tenant-guard-read-paths)

The legacy `Session.bulk_*` methods on a tenant-scoped mapper are refused outside
`unscoped()`. SQL the statement tree hides from the guard (loader options, multi-row INSERT
values, column defaults, bare functions) is scoped or refused. The cursor of every framework
engine refuses any compiled statement that names a tenant table without the ORM guard's stamp
(ADR-0018), and flush writes need an instance read under the bound tenant. Two `sandloom check`
rules keep the bypass inside its `with` block and a module on the framework's engine.

| Id | Disposition |
| --- | --- |
| G-TENANT-21 | New: `Session.bulk_save_objects`, `bulk_insert_mappings` and `bulk_update_mappings` on a tenant-scoped mapper are refused with `UnsafeTenantOperationError` outside `unscoped()`, a non-tenant mapper stays allowed; enforced by `test_tenant_guard_read_paths.py`. |
| G-TENANT-22 | New: a statement that reads a tenant-scoped table through `with_expression`, `with_loader_criteria`, a `Load` extra criteria, INSERT values (multi-row too), a SQL-expression column default or onupdate, or a bare `func.*` executable is scoped or refused, and a flush whose column default reads a tenant table is refused; enforced by `test_tenant_guard_read_paths.py`. |
| G-TENANT-23 | New: `sandloom check` accepts `unscoped` in a module only as the direct item of a `with` / `async with` and refuses a `yield` inside it, a `@contextmanager` included, and every alias, variable, wrapper or string use (`yield-inside-unscoped`, `SANDLOOM-C036`); enforced by `test_archcheck_unscoped_engine_rules.py`. |
| G-TENANT-24 | New: `sandloom check` refuses a module that reaches a database around the framework's engine, by capability (engine and pool factories and classes, star import from sqlalchemy, `sqlalchemy.pool`, `sqlalchemy.engine` beyond result and URL types, DBAPI drivers, `raw_connection`, an engine's `.pool`) (`engine-in-module`, `SANDLOOM-C037`); enforced by `test_archcheck_unscoped_engine_rules.py`. |
| G-TENANT-25 | New: outside `unscoped()` a compiled statement that names a tenant table is refused at the cursor unless the ORM guard stamped it or it is a verified flush write; the stamp cannot be forged and a caller-supplied tenant criterion parameter is refused; enforced by `test_tenant_guard_cursor.py`. |
| G-TENANT-26 | New: a flush UPDATE or DELETE and a column load run only for an instance read under the bound tenant, an identity-map hit is honoured only for one, and a late flush listener cannot plant or retarget a row; enforced by `test_tenant_guard_cursor.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
