### Changed

- **The admin console's npm audit gate checks runtime dependencies only** (admin-npm-audit-picomatch). `npm run audit` (and the `admin.yml` CI step that runs it) is now `npm audit --audit-level=moderate --omit=dev`. The advisory GHSA-fc5q-39xc-vjpf lists every released picomatch version, so it failed every pull request with no fixed version to take, though picomatch is only reached through vite, vitest and typescript-eslint and the console ships none of them; the local picomatch guard in `admin/patches` and its regression stay. No upgrade step.
