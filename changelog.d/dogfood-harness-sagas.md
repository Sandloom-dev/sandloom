### Changed

- **A saga step that raises `MissingTenantError` parks the saga at once** (dogfood-harness-sagas). The error is now classed `misconfigured` with code `tenant_required`, where it used to be `transient`: a saga started with no tenant bound retried until its deadline and then compensated or parked as `deadline_exceeded`. A tenant-less saga now parks on the first attempt as `misconfigured`, and `sandloom sagas retry` re-drives it once the saga starts under a tenant. Upgrade: if you relied on the retries (a tenant bound by something that runs late), bind the tenant before the saga starts; tests pass `tenant_id=` to `saga_harness.run()`.
- **The saga sweep logs a missing lease at INFO outside production** (dogfood-harness-sagas). An unleased sweeper is the normal development shape; it stays a WARNING in production.

### Fixed

- **The `client` fixture binds the contract session for the whole request** (dogfood-harness-sagas). A route that called a contract without ever opening `db()` ran the contract on the provider's own unit and missed the rows the test had flushed, against `docs/reference/testing.md`. The session is now bound around every request.

### Added

- **The saga testing docs name `tenant_id=`** (dogfood-harness-sagas). The Testing section of the sagas page shows `saga_harness.run(..., tenant_id=...)` and the one-liners' `tenant_id=`.
