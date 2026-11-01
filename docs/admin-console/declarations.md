---
title: Declaration reference
description: Every key of an admin.py resource declaration, manifest version 1, with its constraints.
---

# Declaration reference

The keys of an `admin.py` resource, in manifest version 1. See
[Add your module](add-your-module.md) for a complete example.

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
