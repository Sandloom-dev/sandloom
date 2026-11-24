### Added

- **An agent skill `sandloom-module-rules`: the rules no check enforces, one file per rule** (dx-skills-module-rules). Nine rules (a tenant row reached through a parent, an effect that can run twice, event order and `.v2`, a contract command as a saga step, a capped list `limit`, token revocation on a security change, `on_commit`, the body of a task) each have a file with an Incorrect and a Correct example. The skill is an index that links to the files, and `sandloom new` writes both into each agent's skill folder. In the framework repository `python3 tools/docs/module_rules.py` compiles the index from the files, and a test fails when it is stale. Upgrade: run `uv run sandloom add skills` in an existing app; a file you edited is kept (`--force` replaces it).

### Changed

- **The module-author contract lists the rules no check enforces by name and points at the skill** (dx-skills-module-rules). The section keeps its title; each rule is one line with the name of its file. The line on tasks says plainly that a task can run more than once for one enqueue, so a repeated run must do no harm. Upgrade: `uv run sandloom add skills`.
