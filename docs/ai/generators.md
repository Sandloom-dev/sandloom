---
title: Generators
description: What sandloom new and each sandloom add generator write, why every piece starts in the same shape, and how generators treat files you already have.
type: concept
job: When I or my agent start a new module, event, contract or other piece, I want the code to start in the shape Sandloom expects, so I can spend the work on the feature and not on the layout.
purpose: The reader starts every piece with a generator instead of an empty file, knows what each one writes, and trusts it with the files they already have.
audience: [developer, agent]
level: first-app
---

# Generators

Two features written from empty files rarely share a layout, and whoever changes the second one has to learn it first.

A generator writes the starting code for one piece of a Sandloom application, in the layout the [checks](checks.md) expect. `sandloom new` writes a whole application. Each `sandloom add` generator writes one piece into a module and then runs `sandloom generate`, which refreshes the [generated files](generate.md). Only `add deploy` and `add skills` skip that step. A generator adds to your files but never replaces the code in them.

## What each generator writes

<!-- lookup -->
| Command | Writes |
| --- | --- |
| `sandloom new <app>` | The application: `pyproject.toml` with pinned dev tools, `AGENTS.md`, the [skills](agent-tooling.md) for the agents you choose, a sample module, deploy and CI files |
| `sandloom add module <m>` | A module package (`__init__.py`, `models/`, `schemas.py`, `events.py`, `services.py`, `api.py`, `settings.py`), its [migration](../modules/database/migrations.md) chain and a test |
| `sandloom add event <m> <Name>` | An [event](../modules/between/events.md) class in `events.py`: `NoteArchived` in `notes` gets the channel `notes.note.archived.v1` |
| `sandloom add contract <m> <name>` | A [contract](../modules/between/contracts.md), the call one module answers for others, with one read and one command, and the stub callers import |
| `sandloom add consumer <m> <channel>` | A handler in `consumers.py` that reacts to another module's event |
| `sandloom add saga <m> <name>` | A [saga](../modules/between/sagas.md), a process across modules with undo, and the contract it calls |
| `sandloom add projection <m> <name>` | A [projection](../modules/between/projections.md), a table built from other modules' events, with its migration |
| `sandloom add task <m> <name>` | A [background task](../modules/background/tasks.md) in `tasks.py`; `--periodic "<cron>"` schedules it |
| `sandloom add deploy <m> --service <name>` | The microservices profile and gateway, with the module in its own service ([Split into services](../operate/split-into-services.md)) |
| `sandloom add skills` | The agent files for the application's coding agents, updated to this version ([Agent tooling](agent-tooling.md#keep-the-skills-current)) |

`uv run sandloom add <generator> --help` lists each one's options.

## A generator run

Adding a module writes the package and refreshes the generated files:

```bash
uv run sandloom add module notes
```

The run ends with the generated files it wrote and the next step:

```text
wrote demo/shared/events/notes.item.created.v1.json
wrote demo/modules/notes/module.snapshot.json
wrote demo/modules/notes/routes.snapshot.json
wrote .sandloom/inspection.json
generated files current (7 file(s))

Next: `sandloom test` (the module's tests and every gate).
```

The count covers every generated file it checked; the `wrote` lines are the ones that changed. Sandloom finds the module when the application starts, so there is no list of modules to edit.

## When the file already exists

A generator either adds to a file or creates one, and treats an existing file differently in each case:

1. `sandloom add event notes NoteArchived` adds a class to the end of `notes/events.py`, after your code. A file that does not parse stops it: "events.py does not parse (…); fix it first".
2. `sandloom add saga notes refund` would create `notes/sagas.py`. When you already have one, it writes `sagas.py.new` beside it and prints "An existing file was kept; merge the *.new file by hand."
3. Merge the `.new` file into yours, delete it, and run `uv run sandloom generate`.

Two generators replace your edits only when you pass `--force`: `sandloom add skills` and `sandloom add deploy`.

## What breaks a generator run

| Message | Fix |
| --- | --- |
| `… already exists` | `sandloom add module` refuses a module that is there; pick another name |
| `… does not parse (…); fix it first` | Fix the syntax error in the file it names |
| `… lacks <import>: add the import, then re-run` | Add the import it names at the top of the file |
| `no module '<m>' at …` | Create the module first, or run from the project root |

A generator writes a starting point; the feature's logic, fields and tests are yours or your agent's to write. A schema change to an existing table is a migration: `uv run sandloom migrate new <module> "<message>"`.

## Go deeper

- [The toolchain](index.md): where generators sit in the loop of a change.
- [Generate contracts and snapshots](generate.md): what `sandloom generate` writes and when it refuses.
- [Add a module](../modules/create.md): the module generator step by step.
- [CLI reference](../reference/cli.md): every flag.
