# Docs skill scenarios

Each JSON file is one re-runnable test of the docs skills, run with the `sandloom-docs-eval` skill and `tools/docs/docs_skill_eval.py`.

| Field | Meaning |
| --- | --- |
| `page` | The page the agents write |
| `start_from` | The commit whose version of the page sits in the copy, as production would |
| `reference` | The commit holding the version the owner approved; the judge compares with it |
| `exclude` | Path prefixes left out of the copy: other harnesses, tests, briefs, results, these scenarios |
| `scrub` | Regular expressions removed from a file in the copy, where it quotes the approved page |
| `leaks` | Phrases of the approved page; `prepare` refuses a copy that still contains one |
| `stages` | Skill, request and stop instruction for each agent in turn; `answers` holds the owner's recorded decisions by topic, given by the operator when the agent asks |
| `checks` | Mechanical checks `grade` runs: prose word limit, front matter keys, terms that must be absent from prose, phrases that must be present, the strict prose lint |
| `judge` | Yes-or-no questions for a judge subagent who sees the page and the reference |

The answers are the owner's decisions as recorded in `../positioning.md`, `../briefs/` and `../owner-preferences.md`; a question none of them covers goes to the owner, and the answer is added here.
