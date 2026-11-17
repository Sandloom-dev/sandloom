# Admin console — agent contract

The platform-owner console: React 19, TypeScript (strict), Vite, Radix
primitives with Tailwind v4 design tokens, TanStack Query for all server
state, and TanStack Table for grids. It is independent of the Python kernel.
Full guide: `admin/README.md`, `docs/admin-console/develop.md` (developer) and
`docs/admin-console/operations.md` (operator).

## Rules

- **Public module APIs only.** The browser never holds ORM or RPC
  credentials, never calls `/_sandloom`, `/diagnostics` or `/metrics`, and never
  sends `X-Internal-Token`. Authorization is server-side; `/config.json`
  toggles screens only.
- **Types are generated, never hand-written.**
  - `npm run contracts` regenerates `contracts/openapi.json` →
    `src/api/generated.ts`.
  - `npm run contracts:check` fails when they drift from the backend.
  - Run both after any backend DTO or route change the console uses.
- **Resources.** Lists, details, forms and record commands come from module
  `admin.py` `RESOURCES` declarations, compiled into
  `public/admin-manifest.json`. Fix a declaration; do not special-case it in
  the UI. Complex workflows are explicit components in `src/features/`.
- **English only** for the UI, built-in declarations and help text.
- **Design tokens only, no raw colours.** `npm run lint` runs ESLint and
  `scripts/check-tokens.mjs`.
- **Mutations** are never retried by the proxy, and the UI must not
  auto-retry them either.

## Commands (run from `admin/`, Node 22.21.1 from `.nvmrc`)

```bash
npm ci
SANDLOOM_ADMIN_API_TARGET=http://127.0.0.1:2222 npm run dev   # proxy to a running backend
npm run typecheck && npm run lint && npm test            # gates
npm run build                                            # tsc + vite + license + bundle checks
npm run test:browser                                     # Playwright on the static build
npm run format:check
```

After changing anything the build uses, run `npm run package` and commit
`src/sandloom_admin/console`: the wheel ships that copy, so applications need no Node, and
a test fails when it no longer matches the source. The copy is served at `/admin/`, so
paths go through `import.meta.env.BASE_URL`; API calls stay absolute.

**One Node packages the console.** The `.gz` and `.br` files are bytes of the zlib and
brotli inside Node. Only `npm run package` needs the exact version in `.nvmrc` (a full
`x.y.z`): it refuses any other Node before the build, and so does `package-console.mjs`.
`npm run build` on another Node only warns. `engines.node` must equal `.nvmrc`, which a test
in `npm test` checks. The `admin.yml` job runs `npm run package` last and fails when the
committed console changes. The CI `setup-node` steps read `.nvmrc`. The image
`admin/Dockerfile` (`node:22-alpine`, also built by `scripts/smoke-container.py`) is exempt:
its `dist/` is not committed.

To bump the pin, for a Node security release or a new 22.x, change `.nvmrc` and
`engines.node` together, run `npm run package` on the new Node and commit the console if its
bytes changed. Add a changelog fragment. Do it with the other dependency bumps. Nothing
proposes it automatically.

Stop the dev server before `npm ci` or any dependency change, then run
`npm run dev -- --force`. Stale Vite pre-bundles cause duplicate-runtime hook
errors.
