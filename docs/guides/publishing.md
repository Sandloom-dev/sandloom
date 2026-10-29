# Publishing packages to PyPI

Sandloom publishes four distributions through
[Release (PyPI)](https://github.com/Sandloom-dev/sandloom/actions/workflows/release.yml):
`sandloom`, `sandloom-identity`, `sandloom-tenancy` and `sandloom-billing`.
Versions are independent. A manual run can select one package or `all` for a
coordinated release. A per-package tag selects only that package.

## Authentication and approval

The publish job uses PyPI Trusted Publishing with GitHub OIDC. No permanent
PyPI API token, username or password is stored in GitHub. Configure each existing
PyPI project's Trusted Publisher with these exact public values:

| Field | Value |
| --- | --- |
| Owner | `Sandloom-dev` |
| Repository | `sandloom` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

The GitHub `pypi` environment requires approval by `Maryarty`, including runs
started by the owner. Administrator bypass is disabled. The owner may approve
their own run because there is only one maintainer. Allowed deployment refs are
`main` and the four tag patterns `sandloom-v*`, `sandloom-identity-v*`,
`sandloom-tenancy-v*` and `sandloom-billing-v*`.

The plan step also refuses publication from a feature branch or a tag whose
commit is not reachable from `origin/main`. A tag must exactly match the selected
package's version in its `pyproject.toml`. Merging a PR never publishes packages.

## Build and validation

1. The plan validates the event, ref, package selection and versions.
2. Full framework CI runs, including the compatibility matrix and heavy scenarios.
3. The package workflow builds every wheel and standalone source archive, checks
   licenses, compares direct and rebuilt wheels, enforces import layering and
   tests clean framework-only and standard installations.
4. It preserves those exact validated archives and their SHA-256 digests, then
   runs strict Twine metadata/description checks. The preparation job selects
   the requested packages without rebuilding them.
5. After owner approval, a separate publish job downloads the reviewed files,
   verifies local hashes and checks for matching files already on PyPI. Only
   missing files are uploaded. This job performs no source checkout, build or
   dependency installation and is the only job with `id-token: write`.
6. PyPI publication uses signed attestations. The final verification waits for
   the release to become visible and compares every filename and SHA-256 digest
   against the reviewed manifest.

Artifacts are retained for seven days. A differing existing file fails closed;
the workflow never replaces an immutable PyPI file or silently accepts a hash
mismatch. After an interrupted upload, a rerun reuses identical published files
and uploads only the missing ones. A publication/verification failure must be
investigated before choosing a new version.

## Manual release

Update the selected package versions and changelogs in a reviewed PR. When the
framework's generated applications need the new baseline, update `FRAMEWORK_RANGE`,
CLI help, generator expectations and compatibility documentation too. Re-lock
workspace packages without upgrading unrelated dependencies.

After the owner merges the PR, open **Actions → Release (PyPI) → Run workflow**:

- Select `main` and one package, or `all` for the coordinated `0.7.0a2` release.
- Leave **dry_run** checked for validation and artifacts without an upload.
- For publication, uncheck **dry_run**. The workflow still validates first and
  stops at the `pypi` environment for owner approval before obtaining credentials.
- Review the commit, validation results and artifact manifest before approving.

A dry run can validate a feature branch and never enters the publishing
environment. A real publication from a feature branch is refused.

## Tag release

A per-package tag on a reviewed `main` commit requests that package's release:

```bash
git tag sandloom-v0.7.0a2
git push origin sandloom-v0.7.0a2
```

The other package prefixes are `sandloom-identity-v`, `sandloom-tenancy-v` and
`sandloom-billing-v`. Release a framework version before module versions that
require it, or use one coordinated manual run. Each tag-triggered upload still
requires the owner's environment approval.

## Package metadata and licenses

Short descriptions come from `description` and long descriptions from `readme`
in each package's `pyproject.toml`. PyPI release metadata is immutable: changing
an already-published description requires a new release. Validate the rendered
metadata with `twine check --strict` before uploading.

Each package declares `license = "Apache-2.0"` and
`license-files = ["LICENSE", "NOTICE"]`. The build hook rejects package copies
that differ from the canonical root files. Standalone sdists and wheels retain
those files and third-party notices. See `packages/README.md` for build details.

## Testing source changes in an application

Use `sandloom new --framework-source path --framework-path <checkout>` and
`uv sync` to test local source changes. The path must name the repository root
containing `packages/`. These editable paths are local dependencies, not portable
Docker or CI inputs. `--framework-source git` is disabled; use published packages
or a complete matching wheel set for portable deployments.
