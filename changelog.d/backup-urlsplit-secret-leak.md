### Security

- **`sandloom backup` no longer echoes the password of a database url `urllib` cannot parse** (backup-urlsplit-secret-leak). A netloc character that Unicode-normalizes to `:` `/` `?` `#` or `@` (U+FF1A, say) makes `urlsplit` raise a `ValueError` whose message carries the whole netloc, `user:password` included. `sandloom backup plan` and `sandloom backup verify` parse the configured urls (`_libpq_url`, `_with_database`, the restorer and the scratch database name) and let that error through to the terminal and the logs. They now raise a fixed message that names no part of the url, and the original is not chained. A url with such a character was refused before and is refused now; only the message changes.

### Fixed

- **The `task_done` hook of this repository no longer sets `SANDLOOM_COMPOSITION_ROOT` for `sandloom generate --check`** (backup-urlsplit-secret-leak). Since PR #314 the project names its composition root (`[tool.sandloom] composition-root` in `pyproject.toml`), so the variable only repeated that key. CI dropped it in idk-low9; the hook of the repository now agrees.
