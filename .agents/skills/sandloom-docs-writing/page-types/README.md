# Page types

A page holds one type, set in front matter (`type:`). Each type has its own file: who reads it, its blocks in order and what each block must do, what stays off it, and model pages. Bold block names are slots, not headings: write a heading that means something alone, such as "What breaks an event".

| `type:` | Reader | File |
| --- | --- | --- |
| `entry` | Has never heard of Sandloom | [entry.md](entry.md) |
| `concept` | Learning an idea, away from the keyboard | [concept.md](concept.md) |
| `tutorial` | A beginner learning by doing | [tutorial.md](tutorial.md) |
| `how-to` | Doing one framework operation | [how-to.md](how-to.md) |
| `recipe` | Solving a business problem with several mechanisms | [recipe.md](recipe.md) |
| `reference` | Looking up a fact | [reference.md](reference.md) |
| `troubleshooting` | Looking at a symptom | [troubleshooting.md](troubleshooting.md) |

Pick by the job: does the reader act or learn, acquire a skill or apply one?

## Add or change a type

The anatomies are the owner's decisions, kept as files so they can grow.

- **Change a type** when the owner corrects the same thing on two pages of that type, or a blind eval shows a block failing. Edit the file, say why in [owner-preferences.md](../../../../design/docs-writing/owner-preferences.md), and re-run an eval for that type ([sandloom-docs-eval](../../sandloom-docs-eval/SKILL.md)).
- **Add a type** when no row above fits a page's job, for example a migration guide or a comparison page. Copy [template.md](template.md) to `<type>.md`, fill it from two or three model pages (ours or outside), add a row to the table, and show the owner before the first page of that type is written. A new type gets its first eval scenario with its first page.

Each anatomy says what a block must achieve, never how many sentences it has or which words it uses. A number is allowed only where the reader needs it (a word limit on an entry page, a step count in a how-to). An example in these files shows a move, not words to reuse: adapt it to the page, and drop an example a page has copied verbatim.
