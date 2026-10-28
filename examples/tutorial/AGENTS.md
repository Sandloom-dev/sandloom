<!-- tasklist: a Sandloom application (Public API 1.0). -->
# tasklist — agent map

`tasklist` is a Sandloom application: business modules live in `tasklist/modules/`
(sample: `tasks`), `tasklist/asgi.py` is the composition root and loads every
module of `tasklist/modules` (no list to edit). From the project root:

- a new module: `uv run sandloom add module <name>` (also `sandloom add event|contract|
  consumer|saga|projection <module> ...`);
- a schema change: `uv run sandloom migrate new <module> "<message>"`;
- the one gate: `uv run sandloom test` (tests, lint, types, `sandloom check`, generated
  files; `sandloom explain <SANDLOOM-code>` says how to fix a failure).

A module imports only `sandloom.sdk` (plus generated contract stubs and DTOs in
`tasklist.shared`) and its own package. The beginner names, all you need for
CRUD, events and cross-module calls:

| need | names (`from sandloom.sdk import ...`) |
| --- | --- |
| a module | `define_module` |
| tables | `Base`, `TenantScoped`, `db`, `new_id`, `CreatedAt` |
| routes | `module_router`, `public`, `CurrentUser` |
| events | `Event`, `on` |
| contracts | `contract`, `command`, `Contract` |
| authorization | `require`, `Subscribed`, `SubscriptionActive`, `Credits`, |
| | `OrgRole`, `Owner`, `authorize_resource` |
| effects | `record_audit`, `on_commit` |
| tests (`sandloom.sdk.testing`) | `client` (fixture), `as_user`, `published`, |
| | `deliver`, `fake_contract`, `fake_access_fact` |

Any other `sandloom.sdk` name is advanced: mark its line `# sandloom: advanced <reason>`.
Ids and times: `default=new_id` (UUIDv7) and `created_at: Mapped[CreatedAt]`
(`clock_timestamp()`), so a list ordered by `created_at, id` never ties.
