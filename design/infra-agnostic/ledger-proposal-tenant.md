# Ledger proposal: tenant isolation without row security (slice 6)

Status: **proposal, for the owner's approval.** `guarantees.yml` is unchanged.
- Spec: [spec.md](spec.md), decision 3 and section 7.
- After approval, apply it with `uv run python tools/checks/guarantees.py render` and `uv run pytest -m guarantees src`.

## Why the ledger changes

Slice 6 moves row security behind the SQL dialect renderer (`row_security()`). PostgreSQL keeps every statement byte-identical. SQLite has no row security.
- On a database without row security, `tenant_rls`, `system_url` and `require_system_channel` stop startup (SANDLOOM-B026).
- On such a database, `TenantScoped` isolation is application-enforced: the ORM guard alone. Startup warns, and the guarantee lattice computes that label.

Each G-RLS statement says "on a tenant_rls database". That now implies a database with row security, but the ledger does not say so. The G-TENANT entries hold on every database, but the ledger does not say that either. No guarantee becomes weaker on PostgreSQL. The proposal therefore qualifies wording and adds two entries. It removes nothing.

## Entries to qualify (changed statements)

### G-RLS-07

- **Now:** enable_tenant_rls(op, table) emits (through the Alembic op) ENABLE + FORCE row level security and the <table>_tenant_isolation policy, is idempotent, and disable_tenant_rls reverts it; the tenant setting SQL is parameterised.
- **Proposed:** enable_tenant_rls(op, table) emits, through the Alembic op and in the SQL of the migration database's renderer (row_security()), ENABLE + FORCE row level security and the <table>_tenant_isolation policy on PostgreSQL, byte-identical to the SQL before slice 6. It is idempotent, and disable_tenant_rls reverts it. The tenant setting SQL is parameterised. On a database without row security the helper refuses (SANDLOOM-R015).
- **Tests, kept:** the four in the entry today.
- **Tests, added:**
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_policy_ddl_is_byte_identical`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_the_migration_helper_sends_the_same_ddl_through_op`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_binding_statement_is_byte_identical`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_sqlite_has_no_row_security_and_the_helpers_refuse`

### G-RLS-09

- **Now:** system_session refuses (SystemSessionRefused) an empty actor or a too-short reason and raises SystemChannelUnavailable for a database with no system channel; require_system_channel without a system DSN, or a system_url without tenant_rls, fails at provider construction.
- **Proposed:** system_session refuses (SystemSessionRefused) an empty actor or a too-short reason and raises SystemChannelUnavailable for a database with no system channel. require_system_channel without a system DSN, or a system_url without tenant_rls, fails at provider construction. On a database without row security, system_url or require_system_channel stops startup (SANDLOOM-B026).
- **Tests, kept:** the two in the entry today.
- **Tests, added:**
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_row_security_options_without_row_security_are_b026`

### G-RLS-15

- **Now:** A database instance without tenant_rls is unchanged: no hook, no system channel, no I/O at aopen, and the provider does not advertise the tenant_rls feature (it is advertised only when the option is on).
- **Proposed:** A database instance without tenant_rls has no hook, no system channel and no I/O at aopen. The provider does not advertise the tenant_rls feature, which is advertised only when the option is on. Its TenantScoped isolation is application-enforced, a PostgreSQL instance without tenant_rls included: the guarantee lattice labels it "application-enforced tenant isolation" from the instance's features (tenant_isolation_label). Startup warns once for each instance whose database has no row security, when the application maps a TenantScoped table. A PostgreSQL instance without tenant_rls gets no warning, by design: the operator chose it.
- **Tests, kept:** the three in the entry today.
- **Tests, added:**
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_the_lattice_labels_isolation_from_the_instance_features`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_startup_warns_once_per_application_enforced_instance`

### G-PROFILE-01

- **Now:** "... boot refuses (enforce_configuration) TENANCY_MODE other than multi, any database instance without tenant_rls, ..."
- **Proposed:** in that sentence, "any database instance without tenant_rls" becomes "any database instance without tenant_rls, a database without row security included (it cannot have tenant_rls: SANDLOOM-B026)". The rest is unchanged.
- **Tests, kept:** the entry's tests today.
- **Tests, added:**
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_the_strict_profile_refuses_an_instance_without_row_security`

### G-TENANT-01 to G-TENANT-18

The statements stay as they are. These entries describe the ORM guard, which runs on every database. Its strength does not depend on row security.
- **Proposed:** add `docs/modules/database/multi-tenancy.md#Database-enforced and application-enforced isolation` to each entry's `sources`.
- No test changes.

## New entries

### G-RLS-16 (issued G-RLS: 15 → 16)

- **Statement:** tenant_rls, system_url or require_system_channel on a database instance whose SQL dialect renderer has no row security stops every entry point at boot (SANDLOOM-B026, enforce_configuration, build-free). The same holds when the system channel's URL (the system_url option, or the default instance's DATABASE_SYSTEM_URL) names such a database. The driver factory and install_tenant_rls refuse it too (RowSecurityUnavailable, a TenantRLSConfigError). The startup role and coverage checks on such a database raise RowSecurityUnavailable, so the startup tasks that treat TenantRLSConfigError as fatal still stop.
- **Sources:**
  - `docs/modules/database/multi-tenancy.md`
  - `docs/advanced/security-model.md#Tenant isolation`
- **Tests:**
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_configuration_problems_refuse_b026_build_free`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_the_driver_factory_refuses_b026`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_install_on_a_database_without_row_security_is_b026`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_row_security_options_without_row_security_are_b026`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_the_system_channel_url_is_checked_too`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_startup_checks_on_a_database_without_row_security_are_fatal`
- **startup_checks:** ConfigurationError (`sandloom.core.app_factory.row_security_config_problem` via `enforce_configuration`).

### G-RLS-17 (issued G-RLS: 16 → 17)

- **Statement:** Every SQL text the kernel sends to PostgreSQL for row security comes from the renderer's RowSecurity and is byte-identical to the text before slice 6. This covers the policy DDL, the parameterised binding for every paramstyle, the merged BEGIN; set_config message for every isolation, read-only and deferrable combination, the planner's read-only BEGIN and re-bind, and the role and coverage queries. The binding text is built once per engine.
- **Sources:**
  - `design/infra-agnostic/spec.md`
- **Tests:**
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_policy_ddl_is_byte_identical`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_binding_statement_is_byte_identical`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_merged_begin_is_byte_identical`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_role_and_coverage_queries_are_byte_identical`
  - `src/sandloom/core/tests/test_row_security_renderer.py::test_install_builds_the_binding_once_per_engine`
  - `src/sandloom/core/tests/test_planner_begin_sql.py::test_exact_uuid_statement_is_cached_and_canonical`
  - `src/sandloom/core/tests/test_planner_begin_sql.py::test_the_rebind_statement_is_the_old_literal`
  - `src/sandloom/core/tests/test_sql_dialect_renderers.py::TestPostgresqlRenderer::test_row_security_is_whole_or_absent`

## Kernel (L3) API changes the entries rely on

These names are internal (`sandloom.core`, not `sandloom.sdk` or `sandloom.driverkit`). They are listed so the reviewer can check every caller.
- **Changed signature:** `sandloom.core.sql_alchemy.rls.enable_tenant_rls_statements(table, *, dialect, column=..., schema=..., system_access=..., system_role=...)` and `disable_tenant_rls_statements(table, *, dialect, schema=None)` now take a required `dialect=`: a dialect name, a dialect, a connection, an engine or a session. Every caller in tests and benchmarks passes it. The public `enable_tenant_rls(op, ...)` and `disable_tenant_rls(op, ...)` keep their signatures and read the dialect from the Alembic op's migration context.
- **Removed from `rls.py`:** `tenant_rls_policy_name`, `system_rls_policy_name`, `tenant_context_expression` and `tenant_rls_predicate`. They moved into the renderer (`RowSecurity.policy_name` and the PostgreSQL DDL). `TENANT_SETTING` and `DEFAULT_SYSTEM_ROLE` stay exported, and `rls.row_security(bind)` is new.
- **Removed from `planner_adapter/forms.py`:** `begin_read_only_sql` and its module cache. `ReadOnlyBegin` replaces them, built once per adapter.
- **Changed in `tenant_rls.py` (private):** `_set_config_sql(binding, tenant)` and `_merged_begin_sql(adapter, value, binding)` take the engine's `_Binding`.

## Not proposed

- **No `removed:` entry.** No guarantee is withdrawn or weakened on PostgreSQL.
- **No separate G-TENANT entry for the warning.** It is part of G-RLS-15.
