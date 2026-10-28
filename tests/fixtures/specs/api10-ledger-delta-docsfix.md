# Ledger delta: what app-mode agents read the framework source for (2026-10-26)

The api10d app-mode DX eval (`t3_quotas`, `t6_team_overview`,
`t7_credits_event_v2`, `t8_purge_invitations`) showed agents opening the
installed framework's source for an event field named `channel`, contract
semantics, event fields, testing code outside a request, projections, a
`.v2` roll and periodic tasks. Applied to `backend/guarantees.yml`; test
paths are relative to `backend/`.

## Changed entries (stronger)

| id | change | enforced by |
| --- | --- | --- |
| G-DECL-01 | adds: a field named like the Event API (`channel`, `meta`, `publish`, `wire_schema`) is refused at class definition, abstract bases included, with AJA-B015 naming the name and the fix (another name, or `Field(alias=...)`). Before, a `channel` field replaced the class attribute, so the class passed for an abstract base and only its consumer failed (AJA-B016). | `src/tests/sdk/test_sdk_events.py::test_a_reserved_field_name_fails_at_definition` |

## New entries

| id | what | enforced by |
| --- | --- | --- |
| G-DECL-06 | an aliased event field round-trips: `publish()` emits the alias, the schema names it, every reader (producer class, view, projection, `published()`, `deliver()`) decodes the wire key, and the tenant is bound by the wire name. Before, a consumer dropped wire keys that were not Python field names, so the field was silently lost. | `test_sdk_events.py::test_an_aliased_field_survives_publish_deliver_and_handler`, `::test_on_binds_the_tenant_by_its_wire_name`, `test_sdk_testing_kit.py::test_an_aliased_field_survives_the_real_consumer_runtime` |
| C-SDK-04 | `aja docs contracts` / `events` / `<contract>` / `<channel>` from the installed packages (bundled and the app's `<app>.shared`); the generated stubs carry the providers' docstrings, and every bundled contract states its semantics | `src/tests/sdk/test_sdk_docs_wire.py` |
| C-SDK-05 | the recipes `test-service-or-task`, `call-contract`, `projection-from-platform-events`, `event-v2`, `periodic-task` run as written in a generated app | `test_sdk_recipes.py`, `src/tests/e2e/test_app_recipes.py`, CI job `app-recipes` (`scripts/recipes_check.py`) |
| C-GEN-13 | `aja new` writes the app skills (`aja-new-module`, `aja-quality-gates`, `aja-event-version-roll`, `aja-split-deploy`) to `.claude/skills` and `.agents/skills`; `aja add skills` refreshes an existing app | `src/aja/core/tests/test_project.py::test_generated_project_ships_the_app_skills`, `::test_aja_add_skills_refreshes_an_existing_app` |

No guarantee weakened: the reserved-name refusal moved earlier (class
definition instead of a consumer's registration), and decoding by wire name
accepts every payload it accepted before (a field without an alias has its
Python name as its wire name).
