### Added

- **`tenant_upsert(Model, rows, update=[...])` upserts a tenant model's rows for the bound tenant** (tenant-upsert-helper).
  `conflict_insert` on a `TenantScoped` model is refused outside `unscoped()`, because the cursor guard cannot tell a harmless `ON CONFLICT` from one that rewrites another tenant's row. `sandloom.sdk.data.tenant_upsert` is the way in.
  It takes rows of plain Python values and the names of the columns to refresh, fills `tenant_id` from the bound tenant, conflicts on a unique key that includes the tenant column, and writes `SET column = excluded.column WHERE tenant_id = <bound>` itself.
  A SQL expression as a value, a row that names the tenant column, a model in an inheritance hierarchy, a model whose tenant column is not named `tenant_id` in the database and a model without a unique key that includes the tenant column are refused with `SANDLOOM-R018` before anything is sent.
  It runs on PostgreSQL and SQLite and refuses a database without `ON CONFLICT`. It returns nothing, and an instance the session already holds keeps its old values until it is refreshed.
  Docs: [Multi-tenancy](docs/modules/database/multi-tenancy.md#insert-or-update-one-tenants-rows), [ADR-0018, decision 6](docs/adr/0018-tenant-guard-at-the-cursor.md).

### Security

- **The cursor guard admits one conflict insert on a tenant table, the one `tenant_upsert` builds** (tenant-upsert-helper).
  The helper marks the statement it runs, and the cursor admits it only when it has exactly the shape the helper builds and the bound tenant in every parameter set and in the `WHERE` of the update. The mark alone admits nothing, counts only at the cursor, and is removed when the call ends. Every other conflict insert on a tenant table, `DO NOTHING` included, stays refused outside `unscoped()` exactly as before (G-TENANT-27).
  **Upgrade:** code that ran `conflict_insert` on a tenant model inside `unscoped()` to upsert one tenant's rows moves to `tenant_upsert`. It needs a unique key that includes `tenant_id`; a model whose only unique key leaves the tenant out keeps `unscoped()` or gets such a key in a migration.
