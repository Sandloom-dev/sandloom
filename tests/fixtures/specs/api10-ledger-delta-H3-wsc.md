# Public API 1.0: ledger delta for hand-offs H3 and H4 (WS-C)

For the integrator to apply to `backend/guarantees.yml` (spec §11.1). Source
branch: `worktree-agent-af6507bf1fa98f943` (on `integration/api10` at
`88e22366`). Test paths are relative to `backend/` unless they start with
`examples/`.

## 1. Removed or renamed test node ids

None. No test file was moved or renamed and no test function was renamed or
deleted. Tests whose assertions changed keep their node ids:

| node id | change (reason) |
| --- | --- |
| `src/aja/tenancy/tests/test_registration_surface.py::test_registration_surface_is_pinned` | pins the L1 calls: producer class names and schemas, the discovered `@contract` provider (events and contract became L1 declarations) |
| `src/aja/tenancy/tests/test_member_events.py::test_member_event_schemas_declare_the_optional_version` | reads the producer schema (`MemberAdded.model_json_schema()`); `version` still optional with minimum 1 |
| `src/aja/billing/tests/test_registration_surface.py::test_registration_surface_is_pinned`, `::test_fake_psp_in_prod_adds_a_critical_startup_task` | as tenancy |
| `src/aja/identity/tests/test_module_registration.py::test_register_declares_the_identity_surface` | event schemas are the 15 producer classes; the security provider is discovered |
| `src/aja/identity/tests/services/test_twofa_service.py::TestEnableDisable::test_enable_publishes_and_audits`, `src/aja/identity/tests/services/test_rbac_audit.py::*` | record typed publishes (`BaseEvent.publish`), since the services no longer call module-level `publish_event` |
| tenancy tests using the `captured` fixture (`test_member_events.py`, `test_security_hardening.py`, `test_actor_row_lock.py`, `test_role_change_revocation.py`, `test_membership_api.py`) | the fixture records typed publishes |
| `src/aja/identity/tests/api/test_emergency_block.py::*` | imports the consumer from `aja.identity.consumers` |
| `src/aja/core/tests/test_deploy_profiles.py::*`, `src/tests/architecture/test_ledger_static_pins.py::*` | the compose migrate command is `aja migrate upgrade-all --apply` |
| every bundled-module test | `make_user_headers` -> `as_user`, `deliver_event` -> `deliver`, imports from `aja.sdk.testing` (spec §9.1: identical parameters and results) |

## 2. New tests

- `examples/projecthub/projecthub/modules/notifications/tests/test_notifications.py::test_a_redelivered_event_notifies_once`
  (`deliver(..., times=2)` through the real consumer runtime): attach to
  C-TEST-10.
- New `published()` assertions inside existing example tests (linkly create
  and conflict, helpdesk create and status change, projecthub create and
  update): attach to C-TEST-08 if wanted.

## 3. Rewordings

- **G-DECL-05**: the bundled modules (identity 25, tenancy 7, billing 16) and
  the examples (11) have no function-local import; `qrcode` keeps a deferred
  import marked `# aja: optional-import`. AJA-C021 can be un-planned in
  `aja.core.error_codes` (WS-A one-line change); `aja check` is then clean.
- **G-MODULE-02, 13**: the bundled modules import `aja.sdk` public modules
  wherever the identical object has a home there. Remaining kernel imports,
  each because the `aja.sdk` name is still a stub on this base: `public`,
  `operation_intent`, `at_commit`, `external_call`, `release_check`,
  `ResolvedAccount`, `sensitive_effect`, `mark_reauthenticated` (aja.core.di),
  `db_session` (-> `open_uow`), `contract_session` (-> `db()`),
  `MoneyLedger`, `Account`. They flip once WS-B's wiring lands.
- **G-MODULE-14, C-MODULE-04**: the bundled modules declare themselves with
  `aja.sdk.define_module`; their events are typed producers and their
  contracts `@contract` classes in `<module>/contracts.py`, registered by
  discovery. Plain routers keep today's rule.
- **G-SENS-02**: `billing.credits.v1.spend` keeps `risk="money"` in the
  contract schema (L2 form inside the `@contract` class).
- **G-BILLING-21** (spec §9.4): the billing API account is explicit (code
  from H1); `Docs/archive/BILLING_SPEC.md` §6 now states it.
- **G-MODULE-18, C-MODULE-01, G-MIGRATE-\*, G-OPS-16/18, G-CONFIG-07 and
  every statement that names `aja-migrate` / `aja-scaffold`**: name-only
  rewording to `aja migrate <command>` / `aja add module`. Deploy files,
  Dockerfiles and runbooks no longer use the console scripts.

## 4. Sources to repoint (362 entries)

`Docs/reference/rules/` was dissolved into the concepts and the error
catalogue. The files remain as "moved" stubs (excluded from the site) only
so the ledger stays green. Repoint every `sources:` path, then delete
`Docs/reference/rules/` and re-render `Docs/reference/guarantees.md`:

| old source | new source |
| --- | --- |
| `Docs/reference/rules/modules.md` (100) | `Docs/concepts/modules.md` (route policy: `Docs/concepts/route-policy.md`, authz: `Docs/concepts/authorization.md`) |
| `Docs/reference/rules/commands.md` (75) | `Docs/reference/cli.md` |
| `Docs/reference/rules/data-and-effects.md` (61) | `Docs/concepts/units.md` |
| `Docs/reference/rules/contracts.md` (45) | `Docs/concepts/contracts.md` |
| `Docs/reference/rules/infrastructure.md` (28) | `Docs/concepts/capabilities-and-infrastructure.md` |
| `Docs/reference/rules/deployment.md` (21) | `Docs/concepts/deployment.md` |
| `Docs/reference/rules/background-tasks.md` (12) | `Docs/concepts/background-tasks.md` |
| `Docs/reference/rules/testing.md` (11) | `Docs/reference/testing.md` |
| `Docs/reference/rules/repository.md` (9) | `Docs/contributing/repository.md` |

A plain path substitution (`sed`) is enough: sources are checked for file
existence only (headings are checked for ADRs).

## 5. ADR sections

ADR-0010 "9. Business modules carry no function-local imports" -> G-DECL-05
no longer planned once AJA-C021 is un-planned.

## 6. Removals

None.
