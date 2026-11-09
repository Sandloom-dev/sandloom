# The `sandloom/shared` Module

## Philosophy

This module is the shared, common library for the Sandloom project. It contains passive, declarative code that is needed by multiple business modules. Its purpose is to enforce the DRY (Don't Repeat Yourself) principle and provide stable contracts between modules.

## The Golden Rule

- **ALLOWED:** Business modules (`identity`, `billing`, `tenancy`, etc.) can import from `shared`.
- **ALLOWED:** The `shared` module can ONLY import from `core`'s public surface (`sandloom.core.di`, …).
- **FORBIDDEN:** The `shared` module MUST NEVER import from any business module (e.g., `from sandloom_identity import ...` is forbidden here; `tests/architecture/test_shared_purity.py` enforces it).

## Contents

- `shared/authz/`: the unified, claims-first authorization subsystem — the
  composable algebra (`Perm`, `Role`, `Owner`, `Entitled`, and the access
  checks a provider declares with `access_check` / `claim_atom`, combined
  with `& | ~`), the FastAPI entry points (`current_context`, `require`,
  `authorize`, `authorize_resource`), the `enforce` chokepoint for use-cases and
  workers, structured `AccessDecision` / `AccessDenied` results, configurable
  access facts (`AccessFact`, claim / projection / live / auto) and the
  generated-check bases (`ContractCheck`, `ClaimFactCheck`, `ClaimReadCheck`).
  The kernel names no billing or tenancy atom: `Subscribed`, `SubscriptionActive`,
  `Credits`, `Feature`, `WithinLimit` and `OrgRole` are generated into the
  consumer's contract stub from the provider's declarations.
  - Decisions are three-valued: an atom that cannot be evaluated (fact not
    registered, contract absent in this deployment, no tenant context) is
    *indeterminate* — a denial with its own status (500/501/400/403) that
    `~` / `|` can never turn into an allow.
  - Denials answer with the decision's status (402 payment/quota, 403
    forbidden, 501 not deployed, 500 misconfigured); `require(...,
    status_code=)` only overrides definite denials.
  - Live atoms resolve contracts through `core.get_service` (local provider,
    else the `SERVICE_BINDINGS` RPC proxy). Which contract and method an atom
    calls is the provider's `access_check` declaration, carried by the
    consumer's generated copy; the kernel binds no contract by default.
- `shared/guards/auth.py`: `get_current_user_id` (public as `sandloom.sdk.web.current_user_id`), a thin pure-JWT shim over <!-- stale-ok: kernel-internal name of the file described -->
  `authz.current_context` (no I/O). There are no other guard modules — billing
  and role gates are `require(...)` policies from `shared.authz`.
- No generated file lives here (ADR-0013): a module's contract stubs and DTO
  copies are in its own `_generated/` package and its event schemas in its
  `module.snapshot.json`, so a module change never changes this package.
- `shared/subject.py`: `UserContext`, the subject decoded from the
  access token. Only module-agnostic fields are first-class (`user_id`,
  `roles`, `permissions`, `claims_sources`); every other claim lives in the
  namespaced `claims` mapping (`subject.claim("subscription_status")`). The
  billing-specific attributes (`plan_code`, `subscription_status`, `features`,
  `limits`) remain as deprecated read-only properties.

## Forbidden Contents

- NO SQLAlchemy Models (a `DbSession` / `get_db` dependency from `sandloom.core.di`
  is the only allowed touchpoint, and only inside the authz HTTP entry points
  that need it for live / resource atoms).
- NO Service classes with business logic.
- NO direct infrastructure access (Redis clients, engines, HTTP clients) —
  everything goes through `sandloom.core.di` adapters.
