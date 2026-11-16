### Fixed

- **A url-less `streams` event bus no longer fails startup when `INFRA` lists it before the cache** (idk-low5-after-z). The bus runs on the default cache client, which `cache.default` configures when it is built, and `InfraRegistry.aopen_all` opened the capabilities in the order `INFRA` lists them. It now opens `cache` first. No upgrade step.

### Changed

- `sandloom.core.infra.tool_infra.TOOL_CAPABILITIES` is public (it was `_TOOL_CAPABILITIES`): `sandloom.driverkit.process_config` reads it instead of keeping a copy. The upgrade page has a section for the task API, and the driver guide explains `InfraProvider.implicit`.
