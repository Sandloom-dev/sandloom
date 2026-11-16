### Added

- **`LeaseConformance.pass_time`** (ci-dedup-2b). The lease conformance kit takes its TTL waits from an overridable `async def pass_time(self, seconds)`. The default is `asyncio.sleep`, so every existing driver suite behaves as before (Redis and the database drivers keep their backend's real clock); a driver with a clock of its own overrides it to advance that clock instead of waiting. Additive: no existing name or signature changes, no upgrade step.

### Changed

- **A faster test suite** (ci-dedup-2b). The in-memory lease runs the kit on a fake clock (about 8 s less per run). The four failure lanes of the infrastructure module-subset matrix boot five subsets that stand for every class (none, identity, billing, identity with billing, tenancy with admin) instead of all sixteen. A pull request and a local run use a `ci` Hypothesis profile with a quarter of each property's examples (the authz laws keep 400); `HYPOTHESIS_PROFILE=full` (set by the full validation, and by `ci_local` through the workflow-level env) runs them all.
