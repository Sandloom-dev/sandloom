---
title: Repository Map & Agent Tooling
---

# Repository map and agent tooling

Where things live, what looks wrong but is intentional, and the agent tools
wired into the repository. This page is for framework contributors; a module
author needs only the public `sandloom.sdk` API
([SDK reference](../reference/sdk/index.md)).

## Repository map

```
src/sandloom/
  sdk/         # Public API 1.0: the ONLY framework code a business module and
               # its tests import (plus generated contract stubs and DTOs).
               # Public modules: module, app, migrations, tooling, web, data,
               # tenant, events, effects, contracts, authz, sagas, projections,
               # infra, security, errors, testing. Each declares __all__ and
               # __beginner__; the root sandloom.sdk re-exports the beginner names.
  sdk/_decl/, sdk/_prim/  #   the declarative layer (typed events, @contract,
               #   define_module discovery) and the primitives behind it;
               #   private (leading underscore), never imported by modules
  core/        # framework kernel — touch only for framework work. Never
               # imported by a business module (`sandloom check` SANDLOOM-C002).
               # Internals are grouped into thematic subpackages:
  core/observability/  #   logging, metrics, tracing, guardrails (redaction)
  core/eventing/       #   event_bus, transport, core_events (channel schemas)
  core/http/           #   middlewares, error_handlers, rate_limit
  core/migrations/     #   migration runner (`sandloom migrate`), alembic_support
  core/loader/         #   plugin_loader, service_registry (module wiring)
  core/app_factory.py  #   kernel create_app() (public as sandloom.sdk.app.create_app);
               #   sandloom/main.py only adds every installed sandloom.modules
               #   entry point (the kernel names no module); enforce_configuration:
               #   the boot-time config checks every entry point runs (app,
               #   worker, `sandloom migrate`, the sandloom CLI)
  core/composition.py  #   SANDLOOM_COMPOSITION_ROOT resolution for worker/migrate/CLIs
  core/config/         #   configuration layer (imports nothing above it):
                       #   settings (Settings, one section per domain), tiers,
                       #   accepted_risks
  core/auth/           #   hashing, tokens (JWT), bearer, the subject-state
                       #   record (marks), writers / verification, blocks,
                       #   recovery
  core/cli/            #   `sandloom new`, `sandloom add` (generators and templates),
                       #   `sandloom check`, `sandloom test`, the ops CLIs, gunicorn_conf
  core/archcheck/      #   architecture analyzers behind `sandloom check`
  core/testing/        #   the test harness behind sandloom.sdk.testing (pytest
                       #   plugin, fixtures, UoW test helpers)
  core/sagas/          #   saga runtime (ADR-0008): registry checks, step
                       #   protocol, outcome table, timer sweep, operator ops
  core/worker.py       #   Celery worker/beat entrypoint
  core/infra/  # infrastructure plugin layer: capability contracts, the
               # InfraRegistry, built-in drivers, config — see
               # Capabilities & Infrastructure
  shared/      # cross-module DTOs and generated contract stubs
  identity/    # users, JWT auth, RBAC, 2FA, SSO
  tenancy/     # organizations, membership (owner/admin/member), invitations
  billing/     # plans, subscriptions, credits ledger, Stripe
tests/architecture/_fixtures/  # deliberately-broken example modules
                            # (.py.txt data, NOT importable, never shipped) the
                            # analyzer self-tests scan for positive detection
migrations/core/versions/   # CORE chain: shared infra tables only
                            # (outbox/audit/consumer_inbox/contract_command_log),
                            # applied to every hosting database; shipped in the
                            # wheel as sandloom/core/migrations/core_chain. Each
                            # module owns its OWN chain in
                            # src/sandloom/<module>/migrations/
architecture_rules.yml  # import-boundary manifest (main.py is the
                            # platform composition root and is allowlisted
                            # there; the wiring itself lives in
                            # core/app_factory.py inside the kernel)
public_api_baseline.json  # every public sandloom.sdk name and signature
                            # (a change fails `sandloom test`, SANDLOOM-T008)
deploy/                     # monolith + microservices compose profiles
                            # (deploy/README.md is the deployment reference)
docs/                       # docs site sources; archive/ = legacy and Russian
                            # specs; tutorial-app/ = the generated application
                            # the tutorial and how-tos include code from
admin/                      # platform-owner console (React + Vite)
```

See [Capabilities & Infrastructure](../concepts/capabilities-and-infrastructure.md)
for the plugin layer and [Versioning & Stability](versioning.md) for what is
public.

## Things that look wrong but are intentional

- Architecture analyzer fixtures are deliberately-broken example modules stored
  as `.py.txt` data under `tests/architecture/_fixtures/` (NOT
  importable, never shipped in the wheel). The self-tests read them by path and
  run each analyzer to prove it still CATCHES its violation (positive detection).
  See that directory's `README.md`.
- `ORM_LEAKAGE_BASELINE` in `tests/architecture/test_api_no_orm_leakage.py`
  — legacy identity debt ratchet; entries may be removed, never added.
- `FakeStripeClient` — the default PSP (`BILLING_PROVIDER=fake`) outside prod;
  it always rejects unsigned webhooks (tests sign with `sign_stripe_payload`).
  Production uses `BILLING_PROVIDER=stripe` + keys.
- `/health` and `/ready` return 503 when degraded — that is the k8s probe
  contract, not a bug. Public callers see check statuses only; check details
  and `/ready`'s module/consumer topology require `X-Internal-Token`. Results
  are cached for `HEALTH_CACHE_TTL_SECONDS`.
- App-wide error handling lives in the kernel (`core/http/error_handlers.py`;
  the framework owns it, modules never register their own).
- Duplicate event deliveries can happen (post-commit dispatch + relay race) —
  by design, at-least-once; the stable envelope id makes them deduplicable.

## Agent tooling (MCP servers and skills)

The repository registers no MCP servers; agents use their standard file and
search tools plus the project's skills and the `sandloom` CLI.

- **Skills** — reusable workflows under `.agents/skills/` and `.claude/skills/`
  (project `sandloom-*` skills plus the mattpocock engineering/productivity/misc set,
  e.g. `tdd`, `grill-me`, `diagnosing-bugs`, `handoff`).
- **`sandloom docs <name>`** prints a public name's exact signature, tier and
  docstring; **`sandloom explain <code>`** explains an error code. Both answer
  without reading kernel files.

MCP servers are registered for Copilot (`.vscode/mcp.json`) and Claude Code
(`.mcp.json`); Codex is wired globally via `codex mcp add` / `~/.codex`.
