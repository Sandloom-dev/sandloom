# Publishing packages to PyPI

Sandloom ships as four distributions: the framework and three module packages.
The `0.7.0a1` alpha was published manually on 2026-10-28. This guide covers
package validation and the configuration for PyPI Trusted Publishing.

!!! note "Build-only workflow"
    [`.github/workflows/release.yml`](https://github.com/Sandloom-dev/sandloom/blob/main/.github/workflows/release.yml)
    runs by manual dispatch and builds distributions without uploading them.
    Its tag trigger is commented out and its publish step is gated with
    `if: ${{ false }}`. Enabling uploads requires verified Trusted Publishers
    and an explicit workflow change. Repository visibility does not enable uploads.

Trusted Publishing uses PyPI's OIDC integration: GitHub Actions authenticates
with a short-lived token, without storing a long-lived PyPI API token in the
repository.

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
composition. Run it for each release revision and include the results in the
release PR.

## One-time setup (operator, on pypi.org)

Do this **once per package**, before enabling automatic releases. It is the only step
that cannot be automated from this repository — it requires signing in to PyPI
under the project owner's account.

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

## Release procedure

The tag-driven upload flow below requires the build-only controls to be changed
after publisher configuration and release validation. In the checked-in workflow,
pushing these tags does not start a release or upload files.

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

4. With tag-triggered uploads enabled, the `Release (PyPI)` workflow first runs
   the complete framework CI and package composition checks on that exact ref, verifies the tag matches the package
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
publication. Enable the publishing trigger and condition only after validation
and publisher configuration.

## Testing source changes in an application

Use `sandloom new --framework-source path --framework-path <checkout>` and
`uv sync` to test local source changes. The path must name the repository root
containing `packages/`. These editable paths work locally and are not portable
Docker or CI dependencies.

`--framework-source git` is disabled in the generator. Use
`--framework-source pypi` for published packages. A portable deployment can
install `sandloom[standard]==0.7.0a1` from PyPI, or the complete, version-matched
wheel set built from `packages/`. The canonical source is
[Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).
