### Changed

- **`POST /api/v1/orgs` derives `slug` from `name`** (tenant-select-and-dev-verify). `slug` is optional; a derived slug that is taken answers `409` asking for `slug`.
