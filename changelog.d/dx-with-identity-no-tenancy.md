### Changed

- `sandloom new --with identity` (and `--with billing`, `--with admin`, any mix) without `tenancy` now writes `TENANCY_MODE=single` and a generated `SINGLE_TENANT_ID` into `.env` (commented in `.env.example`), as `--no-batteries` does: without the tenancy module no token names a tenant, so under `multi` the generated route answered 400 `tenant_required` for every token, real logins included. With `tenancy` bundled `.env` stays on `multi`. The README says which mode the app starts in. Existing apps keep their `.env`; add the two lines (or the tenancy module) by hand.
- Startup logs one WARNING when a module is the tenant authority (`tenant_ids`) while `TENANCY_MODE=single`: the configured tenant binds every request, so the tenants that module grants are never selected.
