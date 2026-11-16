# Changelog fragments

One file per branch, `changelog.d/<task-alias>.md`; never edit `CHANGELOG.md`
for a new entry. The alias is the tm task alias of the branch (lowercase letters,
digits, `.`, `_`, `-`). Bullets sit under `### <kind>` headings, the kind one of
`Breaking changes`, `Security`, `Deprecated`, `Added`, `Changed`, `Fixed`. A
bullet is the **title** in bold, the alias in parentheses, then why and the
upgrade steps if there are any:

```markdown
### Fixed

- **The Celery driver registers any task name** (celery-task-name-identifier). Why it failed, what changed, the upgrade steps if any.
  A bullet may continue on lines indented by two spaces.
```

A change that belongs to one package's own `packages/<package>/CHANGELOG.md`
(the notes of a release of that package alone) goes into
`changelog.d/<package>/<task-alias>.md`, in the same format. The folder name is
the package's directory name under `packages/`; one that is not a package with a
`CHANGELOG.md` fails the check. A change to the root `CHANGELOG.md` and to a
package's changelog is two files, one in each scope.

`python3 tools/packaging/changelog_fragments.py --check` validates the files. The
release preparation, next to `sync_release.py --version <version> --write`, runs

```bash
python3 tools/packaging/changelog_fragments.py --fold <version>
python3 tools/packaging/changelog_fragments.py --fold <version> --package <package>
```

(the second one for each package that has fragments). Each moves its scope's
fragments into the changelog under `## [<version>] — <date>`, grouped by kind
with breaking changes first, and deletes them. `--fold` refuses a version that is
not the one in the package's `pyproject.toml`, and `pypi_release.py plan` refuses
a release whose scope still has fragments.

The bullets that were under `## [Unreleased]` before fragments existed are folded
once, under `### Changed`; after that fold `## [Unreleased]` stays empty.
