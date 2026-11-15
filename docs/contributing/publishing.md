---
type: how-to
---

# Publishing packages to PyPI

Sandloom publishes eight distributions through
[Release (PyPI)](https://github.com/Sandloom-dev/sandloom/actions/workflows/release.yml):
`sandloom`, `sandloom-standard`, `sandloom-identity`, `sandloom-tenancy`,
`sandloom-billing`, `sandloom-admin`, `sandloom-s3` and `sandloom-redis`. `sandloom-standard` holds the standard
composition root (`sandloom_standard.main`) that `sandloom[standard]` installs:
release it before or together with a `sandloom` version whose `standard` extra
requires it.
Versions are independent. A manual run can select one package or `all` for a
coordinated release. A per-package tag selects only that package.
The current coordinated alpha is `0.7.0a13` for all eight packages. <!-- sandloom-release -->

## Authentication and approval

The publish job uses PyPI Trusted Publishing with GitHub OIDC. No permanent
PyPI API token, username or password is stored in GitHub. Configure each PyPI
project's Trusted Publisher with these exact public values. A project that doesn't
exist yet needs a *pending* publisher, added on PyPI under your account before the
first release:

| Field | Value |
| --- | --- |
| Owner | `Sandloom-dev` |
| Repository | `sandloom` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

The GitHub `pypi` environment requires approval by `Maryarty`, including runs
started by the owner. Administrator bypass is disabled. The owner may approve
their own run because there is only one maintainer. Allowed deployment refs are
`main` and the tag patterns of the packages that release by tag:
`sandloom-v*`, `sandloom-identity-v*`, `sandloom-tenancy-v*`,
`sandloom-billing-v*` and `sandloom-redis-v*`.

The workflow starts on `sandloom*-v*`. Its plan step checks the tag against
`tools/packaging/packages.json`: the package must exist and have `tag_release`
true, which means the environment allows its tag pattern. Any other tag is
refused there and the run ends. Validation and packaging never start for a tag
the environment would reject, and the run holds the `pypi-release` concurrency
group for seconds.

Configure a pending Trusted Publisher for the first `sandloom-s3` release.
Use a manual release from `main` and include the compatible framework wheel in
the same run or publish the framework first.

The plan step also refuses publication from a feature branch or a tag whose
commit is not reachable from `origin/main`. A tag must exactly match the selected
package's version in its `pyproject.toml`. Merging a PR never publishes packages.

## Build and validation

1. The plan validates the event, ref, package selection and versions.
2. Full framework CI covers the compatibility matrix and heavy scenarios. It
   reuses qualifying green PR jobs for the exact source tree and runs the rest.
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

## GitHub Releases

After PyPI publication and hash verification succeed, a separate job creates
an initially private draft GitHub Release, uploads the same reviewed archives,
`SHA256SUMS` and the source manifest, verifies their hashes, then publishes it.
Release notes come from the selected version's changelog section, captured
before owner approval. Only this job receives `contents: write`; it has no
PyPI token or OIDC permission and does no checkout, build or installation.

A coordinated `all` release requires matching package versions and creates
`v<version>` (for example `v0.7.0a13`). A single-package release creates <!-- sandloom-release -->
`<package>-v<version>`. Alpha, beta and release candidates are marked as
GitHub prereleases. The tag must point to the manifest's exact source commit.
Reruns can fill missing assets in a draft but never overwrite a tag or a
conflicting asset. A matching published release is verified without changes.
A dry run never creates a tag, draft, GitHub Release or PyPI upload.

GitHub can briefly delay a newly created draft's visibility in REST reads.
The publisher retries that read up to six times, with two seconds between
attempts; it never repeats the create or upload command to handle this delay.
If the job still fails, inspect the draft and rerun only the `github-release`
job. Matching drafts and assets are resumed, while conflicting hashes or tags
still stop publication. A successful PyPI upload does not need to be repeated.

## Manual release

For a coordinated release, run this from the repository root:

```bash
python tools/packaging/sync_release.py --version 0.7.0a13 --write
uv lock
uv lock --project benchmarks
uv run python tools/checks/check_benchmark_dependencies.py --write
```

<!-- sandloom-release --> The command above prepares the `0.7.0a13` release.
The canonical current framework version is `packages/sandloom/pyproject.toml`.
The helper updates all eight package versions, compatible dependency floors,
example manifests, scaffold requirements, CLI help and current documentation.
It discovers package pins in source documentation; prose that describes the
current release carries an inline `<!-- sandloom-release -->` marker.
Changelogs, ADRs and the dependency security audit retain historical versions.
Move unreleased changes to the new changelog section and review the lock diffs;
these commands must not upgrade unrelated dependencies.

`python tools/packaging/sync_release.py` checks without writing. This guard runs
in required CI, before package validation and before a website build. It uses
checked-in metadata, so builds need no PyPI request to determine a version.
Module packages may still release independently: without `--version`, their
recorded versions are retained and references use their own package metadata.
A release PR describes its candidate version. Successful publication from
`main` triggers GitHub Pages after the GitHub Release is published. You can
[deploy the site manually](website.md) after verifying the PyPI packages.

After the owner merges the PR, open **Actions → Release (PyPI) → Run workflow**:

- Select `main` and one package, or `all` for the coordinated `0.7.0a13` release. <!-- sandloom-release -->
- Leave **dry_run** checked for validation and artifacts without an upload.
- For publication, uncheck **dry_run**. The workflow still validates first and
  stops at the `pypi` environment for owner approval before obtaining credentials.
- Review the commit, validation results and artifact manifest before approving.

A dry run can validate a feature branch and never enters the publishing
environment. A real publication from a feature branch is refused.

## Tag release

A per-package tag on a reviewed `main` commit requests that package's release:

```bash
git tag sandloom-v0.7.0a13
git push origin sandloom-v0.7.0a13
```

The other tag prefixes are `sandloom-identity-v`, `sandloom-tenancy-v`,
`sandloom-billing-v` and `sandloom-redis-v`. A driver package's tag releases
together with, or after, the kernel version it requires (ADR-0016, lockstep).
The kernel's `redis` extra requires `sandloom-redis`, and `sandloom-redis`
requires the kernel. The plan step refuses either tag while PyPI has neither at
the new version. So a version bump of the two goes out through a manual run with
`all`. A tag then releases a package whose sibling is already on PyPI.
Every other package, such as `sandloom-standard`, releases through a manual
run. No tag pattern of the environment allows its tag. Its `tag_release` is
false in `packages.json`, so the plan step refuses that tag.

Pass `all` or the package name in the `package` input. A manual run can select
`sandloom-admin` alone, and the owner's approval still gates its upload.
Release a framework version before packages that require it, or use one
coordinated manual run. Each tag-triggered upload also requires the owner's
environment approval.

## Adding a package

`tools/packaging/packages.json` is the one list of workspace distributions.
The release, sync and distribution tools read it for names, order and
behavior, and never compare a package name. The loader refuses unknown
keys and values of the wrong shape. A test adds a package to a copy of the tooling and runs each tool on it. A new
package still has these steps:

1. The entry in `packages.json`, after the packages it depends on. Set
   `standard: true` for a package that `sandloom[standard]` installs, a
   console's `extra`, `route` and `with` where it ships one, and `tag_release`
   only once step 4 is done.
2. Its own `packages/<name>/` files.
3. The kernel's `[project.optional-dependencies]` in
   `packages/sandloom/pyproject.toml`: an extra for the package, and an entry
   in `standard` when it is in the standard set. The standard-install check
   imports what that extra requires.
4. The `pypi` environment's deployment-tag pattern `<name>-v*`, then
   `tag_release: true`. Without both, the package releases through a manual run,
   and the plan step refuses its tag before validation starts.
5. A Trusted Publisher for the new project, and, for a driver that needs a real
   service, its own CI lane like `objectstore-s3.yml`.

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

## Related reading

- [Package versioning policy](versioning.md)
- [Website publishing](website.md)
