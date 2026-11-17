### Changed

- **The `streams` bus declares `opens_after = ("cache",)`** (idk-low6-kernel). A bus with no `url` runs on the default cache client, which `cache.default` configures when it opens. The kernel used to open `cache` before every other capability for that one reason; it now reads this declaration, so a kernel from this change opens the bus after the cache only with this package version. An older package on that kernel opens a bus listed before the cache in the order `INFRA` gives. Upgrade the package with the kernel.
