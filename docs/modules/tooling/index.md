---
title: The toolchain
description: How Sandloom's generators, project rules and checks hold a backend's structure while you or a coding agent change it, and which page covers each step.
type: concept
job: When I start building with Sandloom, with or without a coding agent, I want to know which tools shape the code and which stop a bad change, so I can work with them instead of around them.
purpose: The reader knows the loop of every change (start from a generator, work by the project rules, finish on sandloom test) and opens the page for the step they are on.
audience: [developer, agent]
level: first-app
---

# The toolchain

An agent that starts each feature from an empty file invents a new layout every time. A rule it was only told about is a rule it can forget.

Sandloom puts three kinds of tools around every change. [Generators](generators.md) write the starting code in the shape the framework expects. The project rules in `AGENTS.md` and the [skills](agent-tooling.md) tell your agent how to change it. [Checks](checks.md) fail when a change breaks a rule, and a broken framework rule carries a `SANDLOOM-…` code that `sandloom explain` turns into a fix. A change is done when `uv run sandloom test` prints `sandloom test: OK`.

## The loop of a change

| Step | Tools | Read |
| --- | --- | --- |
| Start | `sandloom new`, `sandloom add`, `sandloom generate` | [Generators](generators.md), [Generate contracts and snapshots](generate.md) |
| Work | `AGENTS.md`, skills, `sandloom docs`, `sandloom context`, `sandloom graph` | [Agent tooling](agent-tooling.md), [Module maps for agents](module-context.md), [Explore the architecture graph](architecture-graph.md) |
| Check | `sandloom check`, `sandloom test`, `sandloom explain` | [Checks](checks.md), [Testing modules](testing.md) |

## One change through the loop

Start: in an application named `demo`, a generator adds an [event](../between/events.md), a message other modules can react to, to the `notes` module.

```bash
uv run sandloom add event notes NoteArchived --field note_id:UUID
```

It prints the file it edited (shortened here) and the generated files it rewrote:

```text
  …/demo/modules/notes/events.py
wrote demo/shared/events/notes.note.archived.v1.json
wrote demo/modules/notes/module.snapshot.json
wrote .sandloom/inspection.json
generated files current (8 file(s))

Next: `sandloom test` (the module's tests and every gate).
```

Work: read `AGENTS.md`, map the module with `uv run sandloom context notes`, and write the code that publishes the event.

Check: run the module's tests, the rest of the suite and every check:

```bash
uv run sandloom test notes
```

When the change breaks a rule, the `sandloom check` step fails and names the file, the line and the code, such as `SANDLOOM-C001`. [Checks](checks.md) walks one failure from the message to the fix.

The checks prove the structure; whether a feature does what you meant is for your tests.

## Go deeper

- [Error codes](../../reference/error-codes.md): every code, with its cause and fix.
- [CLI reference](../../reference/cli.md): every command and flag.
- [Tutorial: your first feature](../../getting-started/tutorial.md): the loop on a real feature.
