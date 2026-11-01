# Sandloom Admin (optional)

The platform owner console: React 19 + TypeScript (strict) + Vite, a small
component library on Radix primitives styled with Tailwind v4 design tokens,
TanStack Query for all server state and TanStack Table for data grids. It is
independent of the Python kernel and talks only to module-owned public APIs —
no ORM connections, internal RPC credentials or security bypasses in the browser.

## Brand identity

The console shares the website’s approved caracal mark and outlined Sandloom wordmark,
graphite and sand palette, Manrope interface text and Hack Nerd Font Mono technical text.
Light, dark and system themes are available from **Appearance**, including before sign-in.
Brand vectors and icons are generated from `docs/assets/sandloom-mark.svg` through
`tools/docs/brand_assets.py` and `tools/docs/render_brand_assets.cjs`; regenerate them
together with the website assets rather than redrawing the console logo. Font licenses
and source records ship in `public/`.

## Documentation

- **Operators:** [operator guide](../docs/admin-console/operations.md) — audience, permissions, common
  tasks, sessions, billing, errors, and current limits.
- **Developers:** [developer guide](../docs/admin-console/guide.md) — generation pipeline, resource language,
  module integration, deployment, and troubleshooting.
- **In the panel:** open **Help** in the top bar for built-in English operator help.

The UI, built-in declarations, and help use English. The console requires the
identity-contributed platform `admin` role and provides global owner APIs.
Organization roles alone do not grant console access. New declarations should
supply English labels and messages.

## Run locally

Requires Node >=22.12 and a Sandloom backend with identity enabled:

```sh
cd admin
npm ci
SANDLOOM_ADMIN_API_TARGET=http://127.0.0.1:2222 npm run dev
```

Open http://127.0.0.1:5173. Press ⌘K (Ctrl+K) anywhere for the command palette. The dev proxy keeps `/auth` and `/api/v1` on the
same browser origin. Set the target to the monolith or the split gateway;
no screen source changes. Use a backend built from the matching source revision,
including the module-owned owner-console APIs and generated manifest.

Use an existing Sandloom account (email/password, optional 2FA code). A platform
`admin` role grants access to the platform-owner APIs. Global organization and
billing directories do not require membership; this does not grant organization
membership or access to customer self-service operations. This UI neither creates a default password nor grants
a platform role during signup.

## Stable local preview

For an operator click-through, serve a completed build rather than a live editing
session. The preview uses the same public API proxy target as development:

```sh
npm run build
SANDLOOM_ADMIN_API_TARGET=http://127.0.0.1:2222 npm run preview -- --port 5187 --strictPort
```

Changes appear after rebuilding and reloading. Do not replace dependencies while
people are using a Vite development session: its cached pre-bundles and already
loaded browser modules can diverge. Stop the dev server before `npm ci` or dependency
changes, then restart with `npm run dev -- --force` and reload open tabs. This avoids
stale-module/duplicate-runtime hook errors; it does not require weakening any API
security settings. Browser acceptance tests build and serve the static app too.

## Included

- Platform users: list/details, allowed profile/status updates, role assignment,
  account blocking/unblocking and session revocation, with reason and audit trail.
  Account safety commands can process a selection of users independently.
- Platform roles: create/edit/delete and attach/detach existing permissions;
  read-only permission catalog. Server rules protect system roles and the last admin.
- Global organization and member directories, with filters and links to billing.
  The console does not impersonate tenants or expose cross-tenant membership
  changes, ownership transfer or organization deletion.
- Billing customer, subscription, balance, ledger, purchase and operation-intent
  views; admitted credit adjustments require a reason and operation reference.
  This is not arbitrary ledger or subscription editing.
- Overview with per-process health, recent operator activity, failed webhooks and
  pending intents; follow-up actions are limited to the APIs explicitly admitted
  by the backend declarations.

See the [operator guide](../docs/admin-console/operations.md) for command conditions
and current limits. This capability list is not a claim of production readiness.

## Deployment and topology

`npm run build` creates static files in `dist/`. Node is only needed for the
build, not to serve the app. A runtime `/config.json` enables available module
sections; identity remains required for authentication:

```json
{ "modules": ["identity", "tenancy", "billing"] }
```

Mount a replacement config at `/usr/share/nginx/html/config.json` to disable
absent tenancy/billing without rebuilding the UI. This setting controls
screens, not server authorization. Module-owned `admin.py` declarations compile into `public/admin-manifest.json`.
The shared renderer builds resource screens from that static artifact; complex
domain workflows remain explicit components in `src/features/`. See below.

An optional, non-root, digest-pinned nginx image proxies public API requests:

```sh
# Existing deployment environment/secrets are required as usual.
docker compose -f deploy/docker-compose.monolith.yml \
  -f deploy/docker-compose.admin.yml --profile admin up -d --build admin

# Same admin image, split deployment gateway instead of the app:
SANDLOOM_ADMIN_API_UPSTREAM=http://gateway:8080 docker compose \
  -f deploy/compose/microservices.yml \
  -f deploy/docker-compose.admin.yml --profile admin up -d --build admin
```

Run these from the repository root after the backend is running. Defaults:
`127.0.0.1:8081`, upstream `http://app:2222`. Use the trusted **internal HTTP**
origin on the deployment's Docker network; put public HTTPS in front of admin
and set `SANDLOOM_ADMIN_PUBLIC_SCHEME=https` so backend-generated links see the
external scheme. This is operator configuration, not a client-supplied header.
If publishing via another proxy, forward the same hostname and preserve the
trusted forwarding chain. Configure backend `TRUSTED_PROXY_IPS` for the admin
proxy address/network as well as the split gateway as appropriate; do not
trust arbitrary internet-provided forwarding headers. Otherwise all operators
may share the proxy's rate-limit identity. Internal endpoints are never routed,
and `X-Internal-Token` is stripped. The proxy never retries mutations.

## Generated resources

The UI now generates navigation, dashboard resource cards, lists, detail pages,
create/edit forms and explicitly admitted record commands from module declarations.
It works with an empty table. Field types and constraints come from public
OpenAPI DTOs, not from guessing the first record or reading ORM tables.

Each loaded module can export `RESOURCES` (plain dictionaries) in its own
`admin.py`. Bundled examples: `src/sandloom_identity/admin.py` and
`src/sandloom_tenancy/admin.py`. The framework compiler validates API ownership,
field names, separate request/response schemas, pagination and supported types.
Unknown declaration keys, missing routes/required fields and unsupported inputs
fail export rather than becoming partially functional forms.

```sh
# Platform contracts, generated TS types and UI manifest; no DB/lifespan.
cd admin
npm run contracts
npm run contracts:check
```

For a downstream application, run in its installed backend environment:

```sh
SANDLOOM_COMPOSITION_ROOT=myapp.asgi sandloom admin export \
  --output /path/to/admin/public/admin-manifest.json
SANDLOOM_COMPOSITION_ROOT=myapp.asgi sandloom admin export \
  --output /path/to/admin/public/admin-manifest.json --check
```

Use a build-time composition containing all modules to be shown, even when
production splits them into separate processes. Export imports trusted app/module
code and builds the app, but never enters its lifespan, opens database connections
or starts consumers. It does not fetch production OpenAPI. Configure the usual
build environment; a failed module/import aborts export.

Enable the exported module short names in `public/config.json`. New ordinary
resources require **no frontend routing or resource-registry edits**. Development reloads
the manifest on page reload. A deployed image can mount both the new manifest at
`/usr/share/nginx/html/admin-manifest.json` and config at
`/usr/share/nginx/html/config.json`, read-only, without rebuilding the UI. New
specialized widgets require a frontend build. Module keys and resource names must
be unique; declarations are an admission list, not a source of API permissions.

The full declaration reference and example are in the [developer guide](../docs/admin-console/guide.md).

## Session and data boundaries

Access and refresh tokens live only in memory, never localStorage or
sessionStorage. localStorage holds only per-operator preferences (theme,
density, visible columns, saved views) and is optional. Reloading/closing the tab
requires login; tabs do not share refresh tokens. Login supports the existing
password/OTP flow; SSO UI is not part of this version.

Refresh is proactive and single-flight. A revoked/blocked token's HTTP 401
clears local authority and requires login; it is not silently refreshed.
Logout clears local state even if server logout fails; in that case the
server-side refresh token expires according to backend policy. Password or
security changes and admin commands retain the backend's normal revocation.

Every session context gets a new QueryClient. Logout,
revocation and changed refresh authority cancel requests and discard cached
records; late responses from the prior session are rejected. Organization
queries include both subject and organization ID, and switching orgs unmounts
the previous workspace. These UI boundaries complement server authorization.

Writes use pessimistic completion, no automatic retries and explicit domain
commands. A lost response/502 means **unknown outcome**, not permission to
repeat the operation. Inspect current server state before retrying. Frontend
fields are allowlisted by the provider; `is_superuser`, roles and other
response-only fields are not submitted by generic user edits.

## Contracts, checks and extension

`contracts/openapi.json` and `src/api/generated.ts` are checked-in snapshots
of public DTOs. Regenerate with the backend virtualenv installed:

```sh
npm run contracts
npm run contracts:check
npm test
npm run build
npm run audit
npx playwright install chromium
npm run test:browser
```

The exporter constructs the app without running its lifespan, migrations or
DB connections. Production `/openapi.json` stays closed. When adding a screen,
use an explicit module declaration and the owner's DTO/command; do not reflect
all response properties into forms. Users use offset + lookahead partial
pagination; global organization/member directories use offset pagination too.
Resource declarations select the supported pagination strategy. Cursor resources
reject unknown page jumps. Unsupported sorting/filtering is not fabricated client-side.

Unit tests cover refresh concurrency, revocation, late-response isolation,
mutation failures, field allowlists and pagination. Browser tests use synthetic
HTTP fixtures and cover real compiled UI behavior; they do **not** establish
backend authorization. Backend membership tests and the existing backend suite
cover that boundary separately. CI runs both contract drift and frontend tests.

## Platform owner scope

The bundled console requires the platform `admin` role from identity. It exposes
global customers, organizations and users; subscriptions, catalog and credit
operations; refund/dispute state, webhook journal and intents; audit metadata and
per-API health. Organization membership alone is insufficient. The operator guide
in `docs/admin-console/operations.md` is the behavioral reference.

Credit adjustments have DB-atomic idempotency and required audit. Monetary refunds,
invoices and subscription changes remain in the separately authenticated Stripe
dashboard; the UI never simulates them by changing local records. Prices are read
only, while product names/availability are editable with a reason.

## Design system and code structure

```
src/styles/tokens.css     every color, radius, shadow, type size, motion and z-index
                          (light-dark() pairs; data-theme="light|dark" forces a theme)
src/components/ui/        primitives: Button, Badge, Input, Checkbox, Dialog, Menu, Toast…
src/components/patterns/  DataTable, FilterBar, SavedViews, ConfirmDialog, EntityHeader,
                          Timeline, CommandPalette, Empty/Error/Loading/UnknownOutcome states
src/hooks/use-command.ts  the ONLY write path: pessimistic, no auto-retry, Idempotency-Key
                          = operation reference, unknown-outcome handling
src/api/                  ApiClient (memory-only session), typed.ts (OpenAPI-typed calls)
src/features/resources/   manifest renderer: list, detail, forms, actions, bulk actions
src/features/<module>/    compiled widgets a manifest may reference by name
```

Rules enforced in CI (`npm run lint`):

- No inline `style`, no arbitrary Tailwind values (`w-[13px]`), no raw hex/rgb,
  px or ms outside `tokens.css` — add a token instead (`scripts/check-tokens.mjs`).
- ESLint `strictTypeChecked` + `jsx-a11y` strict; TypeScript `strict` with
  `noUncheckedIndexedAccess`; no `any`; no `window.confirm`; pages never call `fetch`.
- `npm run build` fails above the gzip bundle budget (`scripts/check-bundle.mjs`).

Every component and state is catalogued in Storybook (light and dark, with the
a11y addon): `npm run storybook`. Playwright covers the main flows and runs axe
on each screen in both themes; Vitest + Testing Library cover components and hooks.

### Widget slots

A manifest names compiled widgets in `show_widgets`. A widget may fill three
slots of the generated detail page: `actions` (header toolbar), `banner` (above
the content) and `body` (a card). Example: `identity.account-safety` shows the
blocked banner and offers Block/Unblock/Revoke sessions based on the live block
state; the commands themselves are declared manifest actions with
`placement: "widget"`, `tone: "danger"` and `typed_confirmation: "email"`.

## Real backend acceptance

`npm run test:live` runs a separate Playwright scenario against the actual
backend, rather than the browser suite's API fixtures. First set
`SANDLOOM_TEST_DATABASE_URL`, `SANDLOOM_TEST_CACHE_URL` and
`SANDLOOM_TEST_BROKER_URL` to your isolated PostgreSQL/Redis services (never
production). The database service must allow creating test databases.
The runner creates a uniquely named `sandloom_admin_acceptance_test_*`
database, applies real migrations, seeds only its own fixtures, and starts
loopback backend `:2289` plus static admin `:5189`. Both ports must be free.
It removes its database and backend on exit; existing preview data is untouched.
The log is in `admin/.cache/live-acceptance/backend.log`. Browser traces are
disabled and the temporary password is redacted from retained error contexts.
