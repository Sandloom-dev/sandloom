# Sandloom distributions (ADR-0001)

This directory defines the four publishable distributions Sandloom ships, per
[ADR-0001](../docs/adr/0001-framework-distribution.md): the framework,
`sandloom`, and three ecosystem module packages built the way a third party
ships a module. The source of truth stays in the monorepo tree (`src/sandloom`,
`src/sandloom_identity`, `src/sandloom_tenancy`, `src/sandloom_billing`); each
package here builds a wheel and a self-contained source archive through the
[shared Hatchling build hook](../tools/packaging/package_build.py), so **no
source file moves** for packaging and the path-coupled tooling (architecture
checks, Alembic `env.py`, docker mounts, documentation imports) is undisturbed.

## Distributions

| distribution | import package | depends on | extras | entry points |
| --- | --- | --- | --- | --- |
| `sandloom` | `sandloom`: the kernel (`sandloom.core`), the public SDK (`sandloom.sdk`), framework DTOs, guards and authz (`sandloom.shared`), the CLI and the composition root `sandloom.main` | third-party ranges only | `redis`, `celery`, `standard`, `fast`, `check`, `testing`, `sentry` | console script `sandloom` |
| `sandloom-identity` | `sandloom_identity` (users, JWT, RBAC, 2FA, SSO) | `sandloom>=0.7.0a1,<0.8`, pyotp, authlib, qrcode, pillow, itsdangerous, email-validator | none | `sandloom.modules`: `identity = "sandloom_identity"` |
| `sandloom-tenancy` | `sandloom_tenancy` (organizations, membership, invitations) | `sandloom>=0.7.0a1,<0.8`, email-validator | none | `sandloom.modules`: `tenancy = "sandloom_tenancy"` |
| `sandloom-billing` | `sandloom_billing` (plans, subscriptions, credits, Stripe) | `sandloom>=0.7.0a1,<0.8` | none | `sandloom.modules`: `billing = "sandloom_billing"` |

Extras of `sandloom`:

| extra | adds | for |
| --- | --- | --- |
| `redis` | `redis` | the Redis cache and the Redis Streams event bus |
| `celery` | `celery[redis]` | the Celery task queue |
| `standard` | `sandloom[redis,celery]`, `sandloom-identity`, `sandloom-tenancy`, `sandloom-billing` (each `>=0.7.0a1,<0.8`) | the batteries-included set |
| `fast` | `redis[hiredis]`, `jsonschema-rs` | optional accelerators |
| `check` | `PyYAML` | `sandloom check` (reads YAML manifests) |
| `testing` | `pytest`, `pytest-asyncio`, `pytest-xdist` | the test kit (`sandloom.sdk.testing`, `sandloom test`) |
| `sentry` | `sentry-sdk` | error reporting; imported only when `SENTRY_DSN` is set |

What the layout means:

- **Modules register by entry point.** A module distribution declares one
  entry point in the `sandloom.modules` group. Its name is the module name
  (tables `identity_*`, migration chain `alembic_version_identity`, channels
  and contracts `identity.*`); its value is the import path.
  `sandloom.main:app` composes every installed entry point when
  `SANDLOOM_MODULES` is unset, so `pip install "sandloom[standard]"` serves
  identity, tenancy and billing. The kernel names no module. See
  [Modules](../docs/concepts/modules.md#module-packages).
- **Modules do not depend on each other.** Contract calls are bound at
  runtime. `sandloom.shared` ships whole inside `sandloom`: `UserContext` in
  `subject.py`, the generated contract DTOs (including `UserContact` and
  `MemberState`), stubs and event schemas of the standard modules (`contracts/`,
  `events/`), `authz/` and `guards/` — every process installs `sandloom`, so
  every caller imports them (SANDLOOM-C012). An app's own stubs and DTOs stay
  in `<app>.shared` (`CONTRACT_DTO_PACKAGES`).
- **Dependencies are ranges.** Every distribution states
  `>=tested,<next breaking release` (for a `0.x` project, the next minor);
  a narrower range (pydantic, FastAPI, Starlette, SQLAlchemy, aiohttp,
  asyncpg) carries its reason in a comment. Exact versions live only in
  `uv.lock`.
- **Versions are independent.** Each distribution versions on its own; all
  start at 0.7.0a1 and the module packages depend on `sandloom>=0.7.0a1,<0.8`
  ([versioning](../docs/contributing/versioning.md)).

## Editable development installs

The root contributor environment installs every distribution in editable
mode. Each distribution's `dev-mode-dirs` points at
`packages/editable/<distribution>/`, which holds one symlink to its import
package in `src/` (`packages/editable/sandloom-identity/sandloom_identity ->
src/sandloom_identity`), so an editable install exposes exactly that
distribution's package.

## License files

All four projects declare `license = "Apache-2.0"` and
`license-files = ["LICENSE", "NOTICE"]` using PEP 639 metadata. Hatchling 1.27
or newer includes the local files in source archives and in each wheel's
`.dist-info/licenses/` directory.

The root `LICENSE` and `NOTICE` are canonical. Every package keeps regular
local copies so its source archive remains self-contained. The shared build
hook rejects copies that differ from the root; when changing notices, update
all four copies together. A wheel rebuilt from a standalone source archive
uses its included files and does not need the original repository. Public
source selection is documented in the
[source snapshot policy](../docs/contributing/source-export.md).

## Build & validate locally

Run from the repository root. Nothing here publishes packages:

```bash
# One distribution: sdist plus a wheel rebuilt from that sdist.
uv build packages/sandloom

# All four, the same script CI runs (.github/workflows/packages.yml).
uv run --locked python tools/packaging/check_package_distributions.py

# Build and layering only (no network, no install).
uv run --locked python tools/packaging/check_package_distributions.py --skip-install
```

The script:

1. builds each sdist and wheel (direct and rebuilt from the sdist: same
   runtime files) and checks the license metadata;
2. checks import layering across distributions
   ([`distribution_imports.py`](../tools/packaging/distribution_imports.py)):
   every import of a shipped module, function-local and lazy ones included,
   resolves to a module shipped by the same wheel or a dependency; no shipped
   module imports an unshipped first-party module; every third-party import is
   declared (any extra) or standard library;
3. installs `sandloom` alone in a clean environment and runs every CLI
   subcommand's `--help`, `sandloom docs <name>`, `sandloom new` and the RPC
   transport smoke from the installed wheel;
4. installs `sandloom[standard]` and composes `sandloom.main:app` from the
   module entry points.

`tests/integration/test_package_distributions.py` wraps it as a slow test.

The canonical source is [Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).
Install published packages from PyPI as shown in the
[quickstart](../docs/getting-started/index.md), or use a source checkout for
editable development installs.

## History

Until 2026-10-28 the framework shipped as eight distributions
(`sandloom-framework`, `sandloom-shared`, three module packages,
`sandloom-infra-redis`, `sandloom-infra-celery` and the `sandloom-platform`
composition root). They became the four distributions listed above; see
[ADR-0001](../docs/adr/0001-framework-distribution.md) for the original design.
