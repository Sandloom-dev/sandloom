### Changed

- **`POST /api/v1/orgs` derives `slug` from `name`** (tenant-select-and-dev-verify). `slug` is optional; a derived slug that is taken answers `409` asking for `slug`, and a name that yields fewer than two ASCII `a-z0-9` characters answers `422` asking for an explicit `slug`.
