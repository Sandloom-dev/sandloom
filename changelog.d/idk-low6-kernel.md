### Added

- **A driver can declare which capabilities open before it** (idk-low6-kernel). `InfraProvider.opens_after` is a tuple of capability names; startup opens their instances first, whatever order `INFRA` lists them in, and refuses two drivers that wait for each other. A driver that declares nothing keeps the order of `INFRA`. A driver for a url-less bus that runs on the default cache client declares `("cache",)`.

### Changed

- **The kernel no longer opens `cache` before every other capability** (idk-low6-kernel). That order was the durable bus's private dependency on `cache.default`; it is now the bus driver's own `opens_after`. A driver of another package that relied on the cache opening first declares `opens_after = ("cache",)`. Upgrade `sandloom-redis` together with the kernel: with `sandloom-redis` 0.7.0a14 on this kernel, a url-less `streams` bus that `INFRA` lists before the cache is refused with a "no url" error that names the bus, although the cache has one. The release raises the package floors (`sync_release`).
- **`sandloom generate` says what to do when the composition cannot be built from `INFRA`** (idk-low6-kernel). An `INFRA` instance that names a url and no driver, run against a composition that selects none (the default `sandloom.main`), ended in a traceback; it now exits 2 with a message that names `SANDLOOM_COMPOSITION_ROOT`, and the refusal of such an instance names it too.
