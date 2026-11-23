# Ledger delta: a tenant-scoped model whose plain insert reaches another tenant is refused when it is defined (tenant-orm-insert-gaps)

The cursor guard sees the values and the filters of a statement, not what the database does with an
insert whose every value is the bound tenant's. A `TenantScoped` model that declares a `REPLACE`
conflict clause, a foreign key to another tenant table that does not pair `tenant_id` on both sides
(a joined subclass table included), or a tenant column that is not one plain mapped `Column` of
exactly SQLAlchemy's `Uuid` is refused when it is defined, with `UnsafeTenantModelError`
(`SANDLOOM-R019`, `SANDLOOM-R020`, `SANDLOOM-R021`; ADR-0018, decision 7).

| Id | Disposition |
| --- | --- |
| G-TENANT-28 | New: a `TenantScoped` model is refused at definition for a `REPLACE` conflict clause, for a foreign key between tenant tables that does not pair `tenant_id` on both sides (only the inherit key of a joined subclass is exempt), and for a tenant column that is not one plain mapped `Column` of exactly SQLAlchemy's `Uuid`; enforced by `test_tenant_orm_insert_gaps.py`. |
| G-TENANT-27 | Changed: the statement names the options the model declares, never the live schema, and relies on G-TENANT-28 for the tenant column type; enforced by `test_tenant_upsert.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
