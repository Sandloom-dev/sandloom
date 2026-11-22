# Ledger delta: the tenant guard on the legacy bulk and hidden-SQL read paths, at the cursor (tenant-guard-read-paths)

The legacy `Session.bulk_*` methods on a tenant-scoped mapper are refused outside
`unscoped()`. SQL the statement tree hides from the guard (loader options, multi-row INSERT
values, column defaults, bare functions) is scoped or refused. The cursor of every framework
engine refuses any compiled statement that names a tenant table unless the ORM guard approved
that statement (ADR-0018), and a flush writes only rows the unit of work verified and loads only
an instance that carries a stamp of the bound tenant. Two `sandloom check` rules keep the bypass
inside its `with` block and a module on the framework's engine; `unsafe-tenant-sql` also flags `DDL()`.

| Id | Disposition |
| --- | --- |
| G-TENANT-21 | New: `Session.bulk_save_objects`, `bulk_insert_mappings` and `bulk_update_mappings` on a tenant-scoped mapper are refused with `UnsafeTenantOperationError` outside `unscoped()`, a non-tenant mapper stays allowed; enforced by `test_tenant_guard_read_paths.py`. |
| G-TENANT-22 | New: a statement that reads a tenant-scoped table through `with_expression`, `with_loader_criteria`, a `Load` extra criteria, INSERT values (multi-row too), a SQL-expression column default or onupdate, or a bare `func.*` executable is scoped or refused, and a flush whose column default reads a tenant table is refused; enforced by `test_tenant_guard_read_paths.py`. |
| G-TENANT-23 | New: `sandloom check` accepts `unscoped` in a module only as the direct item of a `with` and refuses a `yield` inside it, a `@contextmanager` included, and every alias, variable, wrapper or string use (`yield-inside-unscoped`, `SANDLOOM-C036`); enforced by `test_archcheck_unscoped_engine_rules.py`. |
| G-TENANT-24 | New: `sandloom check` refuses a module that reaches a database around the framework's engine, by capability (engine and pool factories and classes, star import from sqlalchemy, `sqlalchemy.pool`, `sqlalchemy.engine` beyond result and URL types, DBAPI drivers, `raw_connection`, an engine's `.pool`) (`engine-in-module`, `SANDLOOM-C037`); enforced by `test_archcheck_unscoped_engine_rules.py`. |
| G-TENANT-25 | New: outside `unscoped()` a compiled statement that names a tenant table is refused at the cursor unless the ORM guard approved that statement or it is a verified flush write; the approval is private and keyed by the statement, a caller-supplied tenant criterion parameter is refused, every bare word of a raw fragment and a `DDL()` string is read whatever the quoting; enforced by `test_tenant_guard_cursor.py`. |
| G-TENANT-26 | New: a flush write is an allowlist of verified rows and a column load runs only for an instance that carries a stamp of the bound tenant (read from the database, or set by a successful flush insert and cleared by a rollback), an identity-map hit is honoured only for one, and a late flush listener cannot plant or retarget a row; a column load with no tenant bound is the documented exception; enforced by `test_tenant_guard_cursor.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
