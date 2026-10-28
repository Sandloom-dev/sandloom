# Admin console — agent contract

The platform-owner console: React 19, TypeScript (strict), Vite, Radix
primitives with Tailwind v4 design tokens, TanStack Query for all server
state, and TanStack Table for grids. It is independent of the Python kernel.
Full guide: `admin/README.md`, `docs/guides/admin.md` (developer) and
`docs/guides/admin-operations.md` (operator).

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

## Commands (run from `admin/`, Node >= 22.12)

```bash
npm ci
SANDLOOM_ADMIN_API_TARGET=http://127.0.0.1:2222 npm run dev   # proxy to a running backend
npm run typecheck && npm run lint && npm test            # gates
npm run build                                            # tsc + vite + license + bundle checks
npm run test:browser                                     # Playwright on the static build
npm run format:check
```

Stop the dev server before `npm ci` or any dependency change, then run
`npm run dev -- --force`. Stale Vite pre-bundles cause duplicate-runtime hook
errors.
