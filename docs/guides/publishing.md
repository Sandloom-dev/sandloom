# Publishing packages to PyPI

!!! warning "Automatic publishing is currently disabled"
    The initial `0.7.0a1` alpha was published manually on 2026-10-28.
    `release.yml` is switched off: the tag trigger is commented out and the
    publish step is gated with `if: ${{ false }}`. A manual dispatch only
    builds the distributions. Apache-2.0 has been selected and license files
    are configured for all four packages. Automatic releases remain deferred
    until Trusted Publishing is configured. Creating a token or a
    pending publisher alone does not claim a package name.

Sandloom ships as four distributions: the framework and three module packages
(ADR-0001). This
guide covers the **one-time PyPI setup** an operator must do and the **routine
release flow** the CI automates. The workflow is
[`.github/workflows/release.yml`](https://github.com/Sandloom-dev/sandloom/blob/main/.github/workflows/release.yml).

The planned automatic flow uses **Trusted Publishing** (PyPI's OIDC integration): GitHub Actions
authenticates to PyPI with a short-lived OIDC token, so there are **no API
tokens or secrets** stored in the repository.

## Packages

| Distribution | Owns | Tag |
| --- | --- | --- |
| `sandloom` | the kernel, the public `sandloom.sdk` API, `sandloom.shared` (framework DTOs, guards, authz), the CLI and the composition root `sandloom.main`; extras `redis`, `celery`, `standard`, `fast`, `check`, `testing`, `sentry` | `sandloom-vX.Y.Z` |
| `sandloom-identity` | `sandloom_identity`: users, JWT, RBAC, 2FA, SSO | `sandloom-identity-vX.Y.Z` |
| `sandloom-tenancy` | `sandloom_tenancy`: organizations, membership, invitations | `sandloom-tenancy-vX.Y.Z` |
| `sandloom-billing` | `sandloom_billing`: plans, subscriptions, credits ledger, Stripe | `sandloom-billing-vX.Y.Z` |

Each module package registers one `sandloom.modules` entry point and depends
on `sandloom>=0.7.0a1,<0.8`; versions are independent
([Versioning](../contributing/versioning.md)). `sandloom[standard]` installs
the three module packages with Redis and Celery. The kernel ships the standard
modules' generated DTOs, contract stubs and event schemas; applications generate
their own modules' copies and stubs. The layout, extras and entry
points are listed in `packages/README.md`.

Each package keeps its own `packages/<name>/CHANGELOG.md`; the aggregate
lives in [docs/changelog.md](../changelog.md).

## License metadata and files

Each package declares `license = "Apache-2.0"` and
`license-files = ["LICENSE", "NOTICE"]`, with local copies of the root files.
Hatchling includes these files in the standalone source archive and under
`.dist-info/licenses/` in the wheel. The package build hook rejects local copies
that differ from the root copies. Third-party licenses remain in effect.

Before release, inspect both archive formats and the wheel rebuilt from the
source archive: `License-Expression` must be `Apache-2.0`, both `License-File`
entries must be present, and the files must match the reviewed source.
`tools/packaging/check_package_distributions.py` checks these conditions for all
four packages, together with import layering across them, a clean install of
`sandloom` alone (every CLI subcommand) and the `sandloom[standard]`
composition. Its result for the four-distribution layout will be recorded in
`RELEASE_READINESS.md`; repeat it for each release revision. Automatic publishing remains disabled.

## One-time setup (operator, on pypi.org)

Do this **once per package**, before enabling automatic releases. It is the only step
that cannot be automated from this repository — it requires signing in to PyPI
under the project owner's account.

!!! note "Prepare a real first release"
    A pending publisher does not reserve a distribution name. Check availability
    before the first actual release; do not publish empty placeholder packages
    to occupy names. See [PyPI's pending-publisher guidance](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
    and [name-retention policy](https://docs.pypi.org/project-management/name-retention/).

### 1. Add a Trusted Publisher to each existing project

All four projects exist after the initial alpha upload. Follow
[PyPI's existing-project setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/):

1. Sign in to <https://pypi.org> → **Your projects** → **Manage** on the
   distribution → **Publishing** in that project's sidebar.
2. In the GitHub publisher form, fill in:
    - **Owner**: `Sandloom-dev`
    - **Repository name**: `sandloom`
    - **Workflow name**: `release.yml`
    - **Environment name**: `pypi`
3. Repeat for every package in the table above.

### 2. Create the `pypi` GitHub environment (optional but recommended)

In the GitHub repo: **Settings → Environments → New environment → `pypi`**. Add
required reviewers / a deployment branch rule (e.g. tags only) so a human
approves each publish. The workflow already targets `environment: pypi`.

Until a matching Trusted Publisher exists on PyPI, an OIDC upload fails closed
(no credentials). The workflow also remains disabled until deliberately enabled.

## Routine release flow

1. Bump the package `version` in `packages/<name>/pyproject.toml` and add
   a `## [X.Y.Z]` entry to its `CHANGELOG.md` (and the aggregate changelog).
2. Merge to `main`.
3. Tag the release with the **per-package tag**: `sandloom-vX.Y.Z` for the
   framework, `sandloom-<module>-vX.Y.Z` for a module package:

    ```bash
    git tag sandloom-v0.7.0
    git tag sandloom-identity-v0.7.0
    git push origin sandloom-v0.7.0 sandloom-identity-v0.7.0
    ```

    Release a module package after the `sandloom` release its range needs.

4. The `Release (PyPI)` workflow first runs the complete framework CI and package
   composition checks on that exact ref, verifies the tag matches the package
   version, then builds a standalone sdist and its wheel and publishes them via
   Trusted Publishing. Dependency audit failures block validation.

Package builds use `tools/packaging/package_build.py` only while exporting the monorepo:
each source archive includes its own Python sources and core migrations where
applicable. The archived pyproject uses ordinary local Hatch package selection;
building a wheel from it needs neither the repository nor the export hook.
`tools/packaging/check_package_distributions.py` also compares direct-wheel
contents with the wheel rebuilt from the sdist before testing clean installs.

### Manual / dry run

Use **Actions → Release (PyPI) → Run workflow**, choose the package, and leave
**dry_run** checked to build-and-inspect without publishing. While the publish
step is gated with `if: ${{ false }}`, unchecking this input does not enable
publication. A future release must deliberately restore the publishing trigger
and condition after validation and publisher setup.

## Dogfooding while the repo is private

To test local source changes while the canonical repository remains private,
use `sandloom new --framework-source path --framework-path <checkout>` and
`uv sync`. The path must name the repository root containing `packages/`.
`--framework-source git` is deliberately disabled in the current generator;
`--framework-source pypi` uses the published alpha without repository access.

The intended source is [Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).
After transfer, Git dependencies would need a source entry for **every** required
Sandloom distribution (`sandloom` and each module package the app uses) and
access to the private repository.
A portable deployment can install `sandloom[standard]==0.7.0a1` from PyPI,
or the complete, version-matched wheel set built from `packages/`.
No GitHub Release assets have been published.
