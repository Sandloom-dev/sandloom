### Breaking changes

- **`SettingsFill.follows` is removed, no alias** (idk-flat-urls-no-follows). No instance takes an option from another instance any more: a driver's `settings_fill` fills an option from the flat setting it names or leaves it out, and an `INFRA` url states its own. `SettingsFill(follows=...)` raises `TypeError`; a driver package that declared it states the url as an option of its own instance, or drops the argument to take the flat setting (`CACHE_URL` / `BROKER_URL`, still filled until they go away). The kernel's fill no longer sorts the instances that follow after the ones that do not.

### Added

- **`sandloom.driverkit.shared_endpoints` and `InfraProvider.implicit`** (idk-flat-urls-no-follows). A driver package imports no other driver package, so the startup warning for a persistent instance on the cache's Redis database meets in driverkit: `register`, `unregister`, `reset`, `redis_endpoint` and `Role`. `InfraProvider.implicit` is `True` on a provider built for a `default` instance the kernel synthesized (the registry sets it after the build), so a driver words its message by whether anybody chose the instance. Both are additive.
