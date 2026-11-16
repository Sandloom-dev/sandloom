# Changelog fragments

One file per branch, `changelog.d/<task-alias>.md`; never edit `CHANGELOG.md`
for a new entry. Bullets sit under `### <kind>` headings, the kind one of
`Breaking changes`, `Security`, `Deprecated`, `Added`, `Changed`, `Fixed`:

```markdown
### Fixed

- **What changed** (task-alias). Why, and the upgrade steps if there are any.
  A bullet may continue on lines indented by two spaces.
```

`python3 tools/packaging/changelog_fragments.py --check` validates the files.
The release preparation runs `--fold <version>`, which moves them into
`CHANGELOG.md` and deletes them.
