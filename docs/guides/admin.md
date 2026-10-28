# Generated admin UI

Sandloom includes an optional owner console in the repository's `admin/` directory:
React + TypeScript, a token-based component library on Radix primitives with
Tailwind v4, TanStack Query/Table. It uses module-owned public APIs and adds no
UI dependency to the Python framework. Component structure, design tokens and
the CI rules that keep them consistent are described in `admin/README.md`.

## Audience and architecture

For everyday tasks and permissions, read the [operator guide](admin-operations.md).
The panel serves the SaaS owner and trusted operations team with platform `admin`
access. It includes global user/organization directories, billing customers and
history, audited credit adjustments, catalog availability, audit metadata and
service health. Organization roles alone do not grant console access. Read the
operator guide for precise financial and provider boundaries.

The generation pipeline has four stages:

1. A business module provides its normal public HTTP API, DTOs, and domain logic.
2. Its `admin.py` explicitly admits resources, fields, operations, and visibility.
3. `sandloom admin export` validates these declarations against the composition's
   OpenAPI and writes a versioned static JSON manifest.
4. The shared browser renderer loads that manifest and builds standard screens.

The exporter generates **data**, not per-resource React source files or backend
CRUD handlers. The browser needs no runtime ORM access and no production OpenAPI
endpoint. A module's permissions, UoW, RLS, audit, and events remain in its API.
Unsupported declarations fail export; the UI does not guess from the first record.

The bundled UI and module-owned labels are English. Use English labels,
confirmation text, and descriptions in new declarations; user-entered data is not
translated. Dates and numbers use `en-US` formatting. Operator help is under
**Operator guide** in the navigation; ⌘K (Ctrl+K) opens the command palette.

## Local development

With a Sandloom backend running, install Node >=22.12 and run:

```bash
cd admin
npm ci
SANDLOOM_ADMIN_API_TARGET=http://127.0.0.1:2222 npm run dev
```

Open `http://127.0.0.1:5173` and sign in with an existing platform administrator
account. Password login supports an optional 2FA code. The platform role grants
explicit global operator APIs; it does not create tenant membership.

For a stable local operator preview, run `npm run build` followed by
`SANDLOOM_ADMIN_API_TARGET=http://127.0.0.1:2222 npm run preview -- --port 5187 --strictPort`.
Use the development server for editing only. After changing dependencies, stop it,
restart with `npm run dev -- --force`, and reload tabs to discard stale pre-bundles.
The browser test harness builds and serves the static app rather than testing a
long-lived hot-reload session.

## Available screens

- Customers: global users, organizations, memberships and billing customers.
- Access control: platform roles, assignments and permission catalog.
- Billing: subscriptions, products/prices, balances, immutable ledger, purchases,
  refunds/disputes, webhook journal and operation intents. Product display name /
  availability and confirmed credit adjustments have explicit audited commands.
- Account safety: user search, emergency block/unblock and revoke-sessions
  commands (reason required, typed confirmation for blocks, audited).
- Platform: an audit explorer (source switch, time range, action prefix,
  actor/resource filters, keyset paging, actor names), live service health,
  event delivery (outbox dead letters with requeue, Streams DLQ with targeted
  replay, consumers, projections), contracts & modules, redacted runtime
  settings. Every record page shows its audit trail as an activity timeline.

The platform views are kernel routes installed once per loaded module at
`/api/v1/platform/<module>/`: `audit` (keyset: `cursor` + `X-Next-Cursor`,
`since`/`until`, `action`/`action_prefix`, `actor_id`, `resource_type`,
`resource_id`), `services`, `outbox/dead`, `streams/dlq?channel=`, `consumers`,
`projections`, `contracts`, `modules`, `settings` (platform admin, live
session check) and the commands `POST outbox/requeue` and
`POST streams/replay` (platform admin, an admin-risk write: fail-closed
`Idempotency-Key`, required audit, commit-time session re-check; the same store/transport operations as the `sandloom outbox` / `sandloom streams`
CLIs). In a split deployment the gateway routes each prefix to the service
hosting that module, so each view reports on that process and database.

Monetary refunds, invoices and provider subscription changes open the independently
authenticated Stripe dashboard. No local row edit simulates a PSP operation.
The [operator guide](admin-operations.md) documents limitations and retry handling.

## Operator API and topology

Tenancy owns `/api/v1/platform/tenancy/organizations` and `/members`; billing owns
`/api/v1/platform/billing/*`. They use the public shared platform-admin guard:
identity must be a successful claims source and the subject must hold `admin`.
Every read checks the session live. Catalog changes carry the admin risk and
credit adjustments the money risk; both re-check the session before commit. Services query only
their own tables; the UI follows UUID references instead of runtime SQL joins.

The kernel adds `/api/v1/platform/<loaded-module>/audit` and `/services`. Audit
reads the module descriptor's hosting database through the optional
`AuditStore.query` contract; the SQLAlchemy implementation contains the relational
query and explicitly allowlists metadata. Unsupported drivers return `501`.
Health exposes statuses only through a separately authenticated owner surface.
Existing internal diagnostics remain internal. The split gateway routes each
platform prefix to its owner, so the static UI needs no topology-specific logic.

Core views are included automatically for module sections selected in
`config.json`. A shared database yields the same audit journal under multiple
source views. Health covers that API process and configured peers, not workers
or the whole container fleet.

## Deployment

`npm run build` produces static files. The optional
`deploy/docker-compose.admin.yml` overlay runs a non-root nginx image; it binds
`127.0.0.1:8081` by default. Use the same image for both topologies:

| Configuration | API upstream |
| --- | --- |
| Monolith | `SANDLOOM_ADMIN_API_UPSTREAM=http://app:2222` |
| Split | `SANDLOOM_ADMIN_API_UPSTREAM=http://gateway:8080` |

Put HTTPS in front of the UI, set `SANDLOOM_ADMIN_PUBLIC_SCHEME=https`, and configure
the trusted proxy chain as described in the repository's `admin/README.md`.
Never expose the internal RPC token to the UI.

Runtime `config.json` selects exported module sections without rebuilding the
static application. Identity is required for authentication. An offline compiler
collects each successfully loaded module's `admin.RESOURCES`, validates its public
OpenAPI bindings, and exports `admin-manifest.json`. The frontend generates menu,
resource cards, lists, details, simple forms and record commands from this manifest.
Changing the API upstream switches monolith/split topology without screen edits.

## Add a module without writing React screens

Create `admin.py` inside the module. For an existing API returning `id`, `title`
and `created_at`, a resource can look like this (replace paths/fields with your
actual module API):

```python
RESOURCES = [{
    "name": "notes",
    "label": "Notes",
    "access": {"authenticated": True},
    "list": {"path": "/api/v1/notes", "method": "GET"},
    "pagination": {"mode": "offset", "offset": "skip", "limit": "limit"},
    "columns": ["title", "created_at"],
    "detail": {"path": "/api/v1/notes/{note_id}", "method": "GET"},
    "show": ["id", "title", "created_at"],
    "create": {"path": "/api/v1/notes", "method": "POST", "fields": ["title"]},
    "edit": {"path": "/api/v1/notes/{note_id}", "method": "PATCH", "fields": ["title"]},
    "labels": {"title": "Title", "created_at": "Created", "id": "ID"},
    "scope": {"parameter": None, "label": "Organization"},
}]
```

Here `scope` means a required, explicitly selected tenant UUID. The top bar
provides its selector; requests send `X-Tenant-Id`. The API must use `TenantScope`
and authorize the caller; the selector does not authenticate membership. If the
API also puts that tenant in the path, set `parameter` to its actual path parameter
name. Without a selected scope the resource makes no requests. Changing scope
cancels reads and replaces the query cache, cursor chains and UI store. Unscoped
resources such as identity administration omit `scope` entirely.

Then export using the application's installed backend environment:

```bash
SANDLOOM_COMPOSITION_ROOT=myapp.asgi sandloom admin export --output /path/to/admin/public/admin-manifest.json
# The same command with --check is suitable for CI.
```

Add the module's short name to `config.json`, then reload the browser. No resource
switch, field component or API adapter in the frontend needs editing. A deployed
UI can mount a replacement manifest/config read-only at its static document root;
there is no need to rebuild for ordinary resources. Export the complete desired
module set in a build composition before deploying a split topology. The compiler
imports trusted application code but does not enter lifespan or run migrations,
consumers, startup tasks or DB queries. Production docs can remain disabled.

## Declaration reference (manifest version 1)

| Declaration | Meaning / constraints |
| --- | --- |
| `name`, `label` | Unique resource name and human menu/page label. |
| `group`, `icon` | Optional navigation section label and icon name (from the UI's fixed icon set; unknown names fall back to a generic icon). Presentation only. |
| `access` | Exactly one of `authenticated: true` or a nonempty `roles` allowlist. Visibility only; every API call is independently authorized. |
| `list` | Explicit `GET` path owned by this module under `/api/v1/`. Response is an array with string/integer `id` on every row. `items_field` names a top-level array in an envelope. |
| `pagination` | `offset` uses the named offset/limit with `limit+1` lookahead; `cursor` uses its opaque query cursor and `next_header`; `all` is only for genuinely unpaged APIs. No invented COUNT or page tokens. `max_page_size` defaults to 100 and is checked against API limits. |
| `columns`, `show`, `labels` | Explicit read-field lists and optional human labels. A missing `show` list defaults to the columns. Nested/object read fields render as JSON. |
| `detail`, `create`, `edit`, `delete` | Operations are opt-in; only supported buttons/routes appear. Detail/edit/delete have one record path parameter. Create/edit return a JSON record with `id`. Forms specify an input allowlist in `fields`; full output records are never sent back. |
| `filters` | Names of actual list query parameters. Input types derive from their API schema; no undeclared filters are sent. |
| `sort_parameter`, `sort_fields` | Optional sorting only when the API explicitly enumerates both `field` and `-field` query values. Otherwise column sorting is disabled. |
| `actions` | Named record commands with explicit path/method, `fields`, label and mandatory confirmation text. Optional `access` further controls visibility. A successful command may have an empty/204 response. `tone: "danger"` styles it as destructive; `typed_confirmation` names a displayed (`show`) field the operator must retype; `placement: "widget"` hands rendering to a declared widget that knows live state (default `"toolbar"`). Every command also sends an `Idempotency-Key` header equal to its operation reference. Actions without `idempotency_field` are offered as bulk commands over selected rows (one request per record, key `reference:id`). |
| `audit` | Explicit `{resource_type, id_field}` identity for record Activity. The ID field must exist in the public detail DTO. No binding means no guessed record history. Subscription resources can use their PSP ID instead of local integer IDs. |
| `scope` | Required tenant UUID selector, sent as `X-Tenant-Id`; optional `parameter` also binds the tenant into each operation path. Navigation and query keys carry the scope; changing it resets drafts, selection and cursor state. |
| `show_widgets`, `edit_widgets` | IDs of compiled specialized components; no scripts, imports or HTML from the manifest are executed. `show_widgets` fills detail-page slots. `edit_widgets` renders the same compiled slots alongside the edit form. |

Supported inputs: string/email/UUID/date/datetime, boolean, integer/number, scalar
enum and nullable variants. Required fields, length/range, simple patterns and
multiples derive from DTO constraints; default `false` and `0` are preserved.
Write-only/read-only input properties, nested writes, ambiguous unions and
unsupported constraints fail export. Complex validation still belongs to the
server. Server 422 field errors are mapped back to the generated form.

An explicit record command looks like this:

```python
"actions": [{
    "name": "publish",
    "label": "Publish",
    "path": "/api/v1/notes/{note_id}/publish",
    "method": "POST",
    "fields": [],
    "confirm": "Publish this note?",
}]
```

The module must actually provide and authorize that route. Commands and destructive
operations wait for server confirmation; no optimistic success or automatic
mutation retry is used. After a transport failure the effect may already have
committed: the UI says so, and offers only a retry with the SAME reference
(the `Idempotency-Key`), which an endpoint guarded by `idempotency_guard`
replays instead of re-applying. It does not invent DB-atomic deduplication for
endpoints that have none. An action may declare
`idempotency_field` naming a required UUID input. The renderer supplies a stable
random UUID per command, retains it after an uncertain failure, and rotates it
only after success. The API must still bind that key to its parameters and
commit its dedupe record with its effect (the bundled credit adjustment does).

This release does not implement arbitrary JSON Schema, relationship autocomplete,
nested editors, uploads or export/import. Saved views (named filter sets) are
per-operator browser preferences. Identity
grants and customer billing navigation are compiled extensions over public APIs.
A custom scoped resource may use the UUID selector; owner directories need no
selected tenant.

## Sessions and verification

Tokens are kept only in memory. Reloading the tab requires login. Refresh is
single-flight; a revoked session requires new authentication. Session changes
cancel requests and clear cached data, while organization queries include the
user and organization in their keys. Security mutations wait for the server
and are not automatically replayed after uncertain network failures.

Run `npm test`, `npm run test:browser`, `npm run build`, `npm run audit` and
`npm run contracts:check` from `admin/`. The contract check needs the backend
virtualenv installed and checks generated types without exposing production
OpenAPI. Browser tests use synthetic API fixtures; backend authorization is
verified separately by backend tests. A browser acceptance test adds an extra module solely through its compiled
manifest, starting with zero records, then creates a record and executes a command;
another switches tenant contexts for a custom scoped resource. Owner-console tests cover tenant-account rejection, global directories, service health, and a refused credit adjustment with a stable operation reference. The fixture is compiled from a Python test API
and checked for drift. The Docker smoke tests two configured upstreams and does
not claim a full live production deployment.

See [authorization](../concepts/authorization.md),
[multi-tenancy](../concepts/multi-tenancy.md),
[tenancy API](../modules/tenancy.md), and the
[operator guide](admin-operations.md). Internal design research is not part of
the public distribution.


## Command recovery and scoped resources

Each command freezes its payload, target/scope and operation reference before
sending. Unknown outcomes disable the original fields and retain that exact
attempt in the current session, including across dialog closure and page
navigation. The recovery banner identifies the pending operation and can retry
it. Other writes wait until it is resolved; reads remain available. Session
changes discard recovery state. Reloading warns, but cannot persist authority
or operation bodies: inspect server state before creating another operation.

Bulk commands remember confirmed per-record successes and do not resend them.
Definite refusals remain selected and show their individual errors; an unknown
outcome retries the original selection with the original per-record keys.
Every confirmed write invalidates audit queries as well as its declared data.

Tenant-scoped list/detail/create/edit/cancel/save/delete links retain `?tenant=`.
Changing scope remounts local form/selection state; another tenant's rows are
never used as query placeholder data. Server authorization remains independent.

Record Activity uses the declared audit type and ID, with links to the complete
filtered explorer. Outbox/DLQ inspection follows `X-Next-Cursor`; repairing a
selection returns to the first page. The outbox badge is a visible-window lower
bound (`100+` when more exist), not an invented total backlog count.

## Updating and troubleshooting a deployment

Keep `admin.py` declarations and their public APIs in the same change. Regenerate
the manifest after changing a declaration or DTO. For the bundled platform,
`npm run contracts` updates the manifest, public OpenAPI snapshot, and generated
TypeScript types; `npm run contracts:check` fails if any drift. Downstream apps
use `sandloom admin export --output <manifest> --check` in their own CI.

An ordinary resource update requires a new manifest/config, not a new React
screen. New compiled widgets or a manifest-language version require a compatible
UI build. Mount replacement JSON files read-only and reload the browser. Both
JSON responses use `Cache-Control: no-store` in the supplied nginx image.

| Failure | Check |
| --- | --- |
| Export reports an operation is not owned by a module | Bind its own `/api/v1/` endpoint; preserve endpoint module metadata in decorators. Do not point a declaration at another module's API. |
| Export reports an unsupported field/schema | Use a supported scalar DTO input or a specialized compiled component; do not silently drop required constraints. |
| UI reports invalid configuration | Use manifest version 1, unique resource/module keys, and only module names present in the exported manifest. Identity must remain enabled. |
| UI needs an unknown component | Deploy a UI build containing that widget or remove its declaration. The manifest cannot download executable components. |
| API requests fail locally | Check `SANDLOOM_ADMIN_API_TARGET` and that the backend, migrations, account, and required services are ready. |
| Requests reach the wrong split service | Check `SANDLOOM_ADMIN_API_UPSTREAM` and public gateway routing; no browser internal RPC token should be configured. |
| Many operators share a rate-limit identity | Configure the full trusted proxy chain on the backend; see `admin/README.md`. |

Local test/demo accounts are provisioned separately from the UI. Their credentials
must not be written into manifests, source files, or container images.
