# Architecture analyzer fixtures (deliberately broken)

These are **example modules that deliberately violate** the architecture rules.
They are NOT importable Python and never ship: they are stored as `.py.txt`
data files OUTSIDE the `sandloom` package, so ruff / mypy / pytest collection and the
framework wheel never touch them.

The architecture self-tests read them by path and run the relevant analyzer to
prove each analyzer actually *catches* its violation (positive detection) — a
clean tree passing is not enough; a rotted analyzer must be caught.

| Fixture | Violation it encodes | Asserted by |
| --- | --- | --- |
| `boards/__init__.py.txt` | cross-module imports of `sandloom_identity` (every style) | `test_module_boundaries` |
| `broken_module/__init__.py.txt` | cross-module imports (incl. getattr / sys.modules tricks) | `test_module_boundaries` |
| `broken_module/utils.py.txt` | HTTP endpoint in a non-API file | `test_no_endpoints_outside_api_layer` |
| `broken_module/services/violator_service.py.txt` | HTTP endpoint in a service file | `test_no_endpoints_outside_api_layer` |
| `broken_module/api/correct_router.py.txt` | none — control (endpoint correctly in `api/`) | `test_no_endpoints_outside_api_layer` |
| `broken_module/api/orm_leaks_router.py.txt` | ORM models / SQLAlchemy leaked into endpoints | `test_api_no_orm_leakage` |
| `broken_module/tests/conftest.py.txt` | module test importing kernel internals (`sandloom.core._internal`, `.sql_alchemy`, `.database`, `.drivers`) | `test_archcheck_library` (legacy `kernel_internal_test_imports`) |
| `broken_module/services/contract_dto_leak.py.txt` | own-package contract DTOs (relative import, module alias, an L1 `@contract` method) without a generated copy; a copied one and a shared one as controls | `test_archcheck_library` (`contract-dtos` rule) |
| `broken_module/broken_module_dto.py.txt` | the generated DTO copies that fixture is checked against | `test_archcheck_library` (`contract-dtos` rule) |
| `broken_module/kernel_handles.py.txt` | kernel handles by attribute (`core.event_bus`, `.infra`, `.service_registry`, `app.state.core`) | `test_archcheck_library` (`kernel-handles` rule) |
| `broken_module/services/best_effort_effects.py.txt` | `publish_event` / `record_audit` downgraded to best-effort (`required=False` / non-literal) without a justified `# sandloom: best-effort` marker; plus compliant controls | `test_best_effort_effects_rule` (`best-effort-effects` rule) |
| `broken_module/services/best_effort_aliases.py.txt` | best-effort effects behind `publish_event as emit`, a re-bound name, `**kwargs` | `test_best_effort_effects_rule` |
| `broken_module/reflective_handles.py.txt` | kernel handles / escape hatches by name: `getattr(core, "event_bus")`, `attrgetter("raw_client")`, … | `test_no_kernel_handle_access`, `test_no_raw_provider_usage` |
| `broken_module/schemas/orm_leak.py.txt` | ORM `models` in `schemas/` via `from .. import models`, `from sandloom.x import models`, dynamic import | `test_schemas_no_orm_imports` |
| `broken_module/models/untenanted.py.txt` | tables declaring `tenant_id` outside `TenantScoped` (own column, `Column()`, same-file mixin, Core `Table`); plus compliant controls | `test_tenant_scoped_models_rule` (`tenant-scoped-models` rule) |
| `api/shop/services/endpoint_in_service.py.txt` | endpoint in `services/` of a module living under an `api/` directory ABOVE the module root | `test_no_endpoints_outside_api_layer` |
| `acme/modules/orders/services/framework_leaks.py.txt` | `from sandloom import identity` / `from sandloom import core`, dynamic imports under every `importlib` alias (app tree) | `test_archcheck_library` (`framework-imports`, `infra-imports`, `module-boundaries`) |
| `acme/modules/orders/tests/test_leaks.py.txt` | module test importing outside its whitelist (kernel module, sibling modules — absolute, relative, dynamic) | `test_archcheck_library` (`test-imports` rule) |
| `acme/modules/orders/schemas.py.txt` | single-file `schemas.py` importing the ORM | `test_schemas_no_orm_imports` |
| `ledger_pre_api10.json` | data, not code: fingerprints of `guarantees.yml` before the Public API 1.0 wave (`92db0f99`; CI has no git history) | `test_ledger_migration` |
