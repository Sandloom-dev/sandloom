### Added

- **Two agent skills: `sandloom-cross-module` and `sandloom-background-tasks`** (dx-skills-routers). `sandloom-cross-module` says when to use an event, a consumer, a contract call, a projection or a saga, with the command and recipe of each. `sandloom-background-tasks` covers `sandloom add task`, `@task`, `@periodic`, `at_least_once`, `open_uow` and `unscoped`. `sandloom new` writes both. Upgrade: run `uv run sandloom add skills` in an existing app; a skill you edited is kept (`--force` replaces it).

### Changed

- **The agent skills of a new app are short routers** (dx-skills-routers). Each description starts with "Use when ...", so an agent loads the skill by its task. `sandloom-new-module` opens with a table from a task to its command, recipe or skill, and names the 29 names of tier 1 and the 12 of tier 2. `sandloom-module-context` is rewritten in plain words. A test checks that every recipe, `sandloom` command, error code and sibling skill a skill names exists. Upgrade: `uv run sandloom add skills`.
