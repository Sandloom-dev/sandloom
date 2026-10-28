---
paths:
  - "**/migrations/**"
  - "migrations/core/**"
---
# Migrations

- **Each module owns its chain:** `<module>/migrations/`, version table
  `alembic_version_<key>`.
  - New revision: `uv run sandloom migrate new <module> "<message>"`. It
    autogenerates from the models and adds the RLS policy for each new
    `TenantScoped` table.
  - Autogenerate needs the module's database at the chain's head.
  - Never hand-edit another module's chain.
- **`migrations/core/versions/` is the CORE chain:** shared infra tables only
  (outbox, audit, consumer_inbox, contract_command_log). Module tables never go
  there.
- **Tenant tables.** Every `TenantScoped` table has
  `enable_tenant_rls(op, "<table>")` in `upgrade()` and
  `disable_tenant_rls(op, "<table>")` in `downgrade()`, both from
  `sandloom.sdk.data` (`sandloom migrate new` writes them; keep them in a hand-written
  revision).
  - `system_access=True` only for tables the audited system channel must
    reach (a model with `__system_access__ = True`).
  - The strict production profile refuses to start without the policy.
- **Indexes.** Index every column you filter or join on (`tenant_id` first).
  Model indexes must exist in a migration (`test_model_indexes_migrated.py`).
- **Apply:** `uv run sandloom migrate upgrade-all --apply`.
  - It runs the core chain on every hosting database, then each module's chain
    on its own database.
  - Migrations run as a one-shot, never inside the app.
- Concept: `docs/concepts/units.md` (tables and RLS).
