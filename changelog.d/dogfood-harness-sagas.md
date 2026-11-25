### Changed

- **A saga step that raises `MissingTenantError` parks the saga at once** (dogfood-harness-sagas). The error is now classed `misconfigured` with code `tenant_required`, where it used to be `transient`: a saga started with no tenant bound retried until its deadline and then compensated or parked as `deadline_exceeded`. A remote provider's `MissingTenantError` now answers `400 tenant_required` over RPC (it was `500 provider_error`), and a step reads that code as `misconfigured` too. Upgrade: a tenant-less instance cannot be re-driven. It holds no tenant, so `sandloom sagas retry` parks it again, and starting the same business id under a tenant raises `SagaInputConflict`. Steps applied before the park stay applied, because the park does not compensate them: establish each applied step's outcome and reconcile it (compensate it or account for it), then start the work again under a tenant with a new business id, which derives new step keys. The parked row is kept. If you relied on the retries (a tenant bound by something that runs late), bind the tenant before the saga starts; tests pass `tenant_id=` to `saga_harness.run()`.
- **The saga sweep logs a missing lease at INFO outside production** (dogfood-harness-sagas). An unleased sweeper is the normal development shape; it stays a WARNING in production.

### Fixed

- **A contract call in a `client` request runs on the test's session even when the route never opened `db()`** (dogfood-harness-sagas). Such a route ran the contract on the provider's own unit and missed the rows the test had flushed, against `docs/reference/testing.md`. The `client` fixture now binds the contract session for every request at the router, inside the app's request boundary; the production request path is untouched. A `client` request that runs in another context (a concurrent actor of the test) now shares the session with the others that overlap it and gives it back to the test's task when the last one ends.

### Added

- **The saga testing docs name `tenant_id=`** (dogfood-harness-sagas). The Testing section of the sagas page shows `saga_harness.run(..., tenant_id=...)` and the one-liners' `tenant_id=`.
