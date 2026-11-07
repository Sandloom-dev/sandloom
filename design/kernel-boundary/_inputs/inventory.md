# Inventory: where the kernel (sandloom.core / sdk / shared) knows a business module

Repo: /Users/admin/Documents/dev_projects/sandloom, main cc90200. Produced by a read-only agent; rows are file:line it saw. Verify before relying on a row.

Business modules (every `src/sandloom_*`): billing, identity, tenancy, admin (`sandloom_admin`, entry point in packages/sandloom-admin/pyproject.toml:27-28).

## A. Imports of sandloom_*
- core/cli/password_check.py:21,42 — `importlib.import_module("sandloom_identity.password_policy")` in try/ImportError, fallback 8/100. tools/packaging/distribution_imports.py:12 exempts guarded imports. Used by `sandloom admin create` prompt.
- No other static or dynamic import in non-test kernel code.

## B. Generated artifacts inside the kernel package
- shared/contracts/{billing_credits_v1, billing_entitlements_v1, identity_contact_v1, identity_security_v1, tenancy_membership_v1}.py — stubs. Used by sandloom_admin/startup.py:14 and recipes.
- shared/contracts/identity_dto.py, tenancy_dto.py — DTO copies (AccountStanding, PlatformAdmin, UserContact, MemberState). Used by identity/schemas/contact.py:7, operator.py:5; tenancy/schemas/membership.py:29; billing/services/tenant_resolver.py:56 (billing imports tenancy's DTO at runtime through the kernel wheel).
- shared/events/*.json — 34 event schemas (billing 7, identity 15, tenancy 12).
- architecture_rules.yml:14-17 `shared_package: sandloom.shared`; packages/sandloom/pyproject.toml:133-135 ships them in the `sandloom` wheel.
- core/cli/generated.py:480 `_shared_packages` always returns "sandloom.shared" (DTO home hardcoded).
- core/cli/generated.py:543 `if "sandloom_admin" not in names` — admin manifest only when admin composed.
- In-flight (branch claude/generate-stubs-sdk-c52485, in review): generator writes app/example stubs importing sandloom.sdk.contracts; framework-tree stubs (shared_package == sandloom.shared) keep importing sandloom.core.descriptors because .importlinter `kernel-never-imports-sdk` (ADR-0010) forbids sandloom.core and sandloom.shared importing sandloom.sdk.

## C. Hand-written kernel code depending on a module's contract, DTO or claims
- shared/membership.py:1-32 (`member_event_action`, `refreshed_state_wins`) — imports MemberState from tenancy_dto; tenancy's ordering rule. Only real importer: tests/integration/test_revocation_pipeline.py:41; docstrings in tenancy and sdk/_decl/projections.py:328 point to it.
- shared/authz/live_contracts.py:74-114 — DefaultEntitlementsAdapter (service "billing.entitlements.v1": check_subscription_active, get_credit_balance), DefaultMembershipAdapter ("tenancy.membership.v1": has_role_at_least), registered by default in `_defaults()`. Public: ENTITLEMENTS, MEMBERSHIP, MembershipAdapter, register_live_adapter in sdk.authz. examples/helpdesk overrides via register_live_adapter.
- shared/authz/account.py:38,153-196 — `tenant_kind` asks MEMBERSHIP adapter (tenancy) to classify a tenant as organization. Public: tenant_kind in sdk.tenant.
- shared/authz/facts.py:528-531 WELL_KNOWN_FACT_OWNERS — "subscription.active", "organization.subscription.active" → "billing".
- shared/guards/platform.py:10-19 require_platform_admin — trusts "admin" only when "identity" in claims_sources. Public in sdk.web. Used by identity api/user_safety.py:21, tenancy api/platform.py:7, billing.
- core/admin/api/platform.py:54-76 platform_operator, mounted always by core/app_factory/__init__.py:251 at /api/v1/platform/<module>/ — same "identity" gate. :608 "billing_provider" setting shown in console.
- core/cli/admin.py:62, core/cli/security.py:61,63 — SECURITY_CONTRACT="identity.security.v1", MEMBERSHIP_CONTRACT="tenancy.membership.v1" (sandloom admin create/invite/list; sandloom security revoke-sessions / force-password-reset --tenant). Design: design/first-admin/design.md:67 (CLI calls identity via contract so it works on a split deploy).
- core/testing/authz.py:21-50,120-128 — fake_access_fact knows the two subscription facts and stubs billing.entitlements.v1. Public beginner name in sdk.testing; no module or example uses it.
- core/http/tenant_scope.py:5-6,130-170 — TenantScope reads `tenant_ids` and `org_id` claims; `tenant_ids` is tenancy's claim (sandloom_tenancy/claims.py:34). TenantScope public.

## D. Domain concepts in the public authz surface (beginner tier)
- shared/authz/atoms_claims.py:32 ACTIVE_SUBSCRIPTION_STATUSES {"active","trialing"}, :77 SUBSCRIPTION_FACT, :80 Subscribed (claim key subscription_status, mentions BILLING_DENY_ON_PAST_DUE), :304/:327 Feature/WithinLimit (claim keys features, limits). Public beginner.
- shared/authz/atoms_live.py:121 SubscriptionActive, :143 Credits, :168 OrgRole (roles member/admin/owner hardcoded :187). Public beginner.
- shared/authz/account.py:41 TenantKind personal/organization/canonical/unknown; :60-90 Account.user/org/bound_tenant ("the billing account"). Public.
- shared/authz/constants.py:12-31 Permissions (perm.users.*, perm.roles.*), Roles (admin/support/ops) — "Identity owns the authoritative rows and seeds these codes". Public sdk.authz; used by sandloom_identity only.
- core/unit_of_work/intent.py:91 ResolvedAccount(kind user/org) "billing account"; release_check, external_call; core/route_policy.py:212-221 operation_intent "(billing checkout and portal)", "PSP session". Public (core.di, sdk.web). Used only by sandloom_billing.
- core/sql_alchemy/money_ledger.py MoneyLedger (example table billing_credit_ledger). Public core.orm. Used only by billing.
- core/auth/* — tokens.py JWT minting, hashing.py argon2 passwords, blocks.py block_user, revocation; public(issues_tokens=) route_policy.py:160; module_api/_define.py:128 issues_tokens. Public sdk.security, sdk.web. create_access_token/hash_password used only by sandloom_identity.
- core/claims.py, facade/_services.py:25-41 (<m>.claims.v1, <m>.facts.v1) — generic contributor mechanism; module names only as examples.
- shared/subject.py:53-106 UserContext — first-class fields module-agnostic (roles/permissions "From 'identity' module" :73); billing/tenancy claims in claims{}.
- core/archcheck/api10_rules.py:167-178 (require_org_role, OrgRole), :500 BILLING_ATOMS; module_rules.py:59-62 ORG_ATOMS; runner.py:134 C027 account-route-mismatch; error_codes.py:513,883 — static checks know billing and tenancy atoms.

## E. Settings and configuration
- core/config/modules.py:1-190 ModulesConfig ("Settings of the bundled business modules") mixed into Settings (settings.py:46,100): TWOFA_ENCRYPTION_KEY, BILLING_PROVIDER, STRIPE_SECRET_KEY/WEBHOOK_SECRET, BILLING_DENY_ON_PAST_DUE, BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS, BILLING_INTENT_RECONCILE_*, BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES, GOOGLE/GITHUB/MICROSOFT/LINKEDIN_CLIENT_*, SSO_ALLOWED_REDIRECT_URIS.
- core/config/auth.py:88-95 TENANT_CLAIMS_MAX_ORGANIZATIONS (tiers.py:620) — tenancy.
- core/config/settings.py:497-505 validates TWOFA key; :623-660 _billing_requirements (fake PSP refused in prod, Stripe keys required).
- core/config/accepted_risks.py:56-61 fake_psp_in_prod (cites sandloom_billing.psp.psp_configuration_error).
- core/config/tiers.py:396-405 removed BILLING_* settings table.
- core/migrations/backup.py:248-260 backup plan lists TWOFA, STRIPE_*, "operator reset-2fa".
- No module-settings extension point exists anywhere (searched module_settings, BaseSettings in core/sdk/modules).

## F. CLI, scaffold, templates
- core/cli/project.py:54 _BUNDLED=("identity","tenancy","billing","admin"); :1131-1142 admin implies identity; :1225 name map; :1410-1416 install check; :120-130 _IDENTITY_NOTE, _LOG_LINKS; :970-983 _IDENTITY_ENV, _BILLING_ENV.
- core/cli/wizard.py:57-61 MODULES with descriptions; :406-446; default selection none.
- core/cli/scaffold_deploy.py:88-103 MODULE_ENV {sandloom_identity: TWOFA, sandloom_billing: BILLING/STRIPE}; :864 help text.
- core/cli/templates/deploy/postgres/init-databases.sh:7,20; compose/microservices.yml.tmpl:28; templates/skills/sandloom-split-deploy/SKILL.md:12-19; keys.py:237 "issuer ONLY (identity)".
- core/app_factory/pages.py:134-139,287-302 start page links /admin/ and `sandloom admin create`.
- sdk/_prim/recipes.py — 9 recipes, 29 module mentions (org-gate, call-contract with BILLING_CREDITS_V1, projection-from-platform-events with tenancy member_*.v2 classes and MemberState). CI-verified in a generated app.

## G. Naming examples only (~50 places in docstrings/help/comments)
core/contract_testing.py, testing/*, descriptors/*, facade/_claims.py, contract_compat.py, contract_cache_shadow.py, config/contracts.py, config/common.py:82, migrations, cli/*, archcheck/*, unit_of_work/*, sql_alchemy/durable_inbox.py, models/*, worker.py, module_api/*, sdk/_decl/*, sdk/_prim/*, shared/README.md, main.py. Also core/testing/static_checks.py:71 KNOWN_REDUNDANT={"external_identities.ix_external_identities_provider"} — identity's real table/index.

## H. Packaging
- `sandloom` wheel: src/sandloom + core migration chain; includes shared/contracts and shared/events. Extras `standard`, `admin` depend on sandloom-identity/-tenancy/-billing/-admin.
- Each sandloom-<m> wheel ships sandloom_<m> only and depends on sandloom; none ships its own stubs.
- Core migration chain 0001-0013 is clean (infrastructure tables only).

## Tests
- src/sandloom/core/tests: 92 files mention module names; two import a module (test_redis_cache_bounded.py:277 sandloom_identity.lockout; test_asymmetric_jwt.py:418).
- Root tests import sandloom.shared.membership and sandloom.shared.contracts.

## Why it is there
- docs/adr/0001-framework-distribution.md:236-262: sandloom.shared ships whole in the sandloom wheel (DTOs, standard modules' stubs and schemas, authz, guards) — "every caller process imports it" (SANDLOOM-C012). "The kernel names no module" (:255, G-KERNEL-13) covers only the default module list.
- shared/authz/constants.py:3-6: in shared "because any business module may reference them without importing identity". live_contracts.py:13-15 defaults "keep the current wiring working". ADR-0003:28 OrgRole via live adapter. ADR-0010:55-72, 189-199, 269: beginner tier with billing atoms and required of=Account.
- Platform-admin "identity" source: ADR-0009 item 6; no ADR justifies the hardcoded source.
- Module settings: no mechanism; settings.py:636-639 comment shows awareness.
- Admin console UI in a module (design/admin-delivery/research.md:214-224); compiler and platform API in kernel.
- git history starts at snapshot 8d719dd (2026-10-28); most items predate it.

## Guard gaps (verified by the main session)
- tests/architecture/test_shared_purity.py:18-38 discovers modules as subdirectories of src/sandloom/ and forbids `sandloom.<m>`; modules are top-level `sandloom_<m>`, so the test cannot fail. G-KERNEL-10 relies on it.
- G-KERNEL-13 (core/tests/test_module_names.py:165-192) scans only core/config, core/app_factory, loader/plugin_loader.py, main.py for exact literals.
- .importlinter has no contract forbidding core/sdk/shared from importing sandloom_*.
