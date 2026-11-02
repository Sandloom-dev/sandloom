---
title: Agent tooling
description: What a coding agent gets in a Sandloom application (AGENTS.md, the skills, the sandloom commands it can ask, the docs as Markdown) and how to keep the skills current.
type: concept
job: When a coding agent works in my Sandloom application, I want to know what it reads and which commands it can ask, so I can point it at the right place and keep its instructions current.
purpose: The reader knows what the agent finds in the project and in the docs, keeps the skills current, and points the agent at them instead of pasting rules into a prompt.
audience: [developer, agent]
level: first-app
---

# Agent tooling

A coding agent knows Python but not this project's layout or rules, and an instruction typed into a prompt is gone by the next task.

A Sandloom application keeps its instructions for agents in its own files. `sandloom new` writes `AGENTS.md` and five skills, procedures an agent loads when its task matches one. Four `sandloom` commands answer questions about the installed framework from the command line. The [checks](checks.md) hold the rules these files describe.

## What the agent finds in the project

| File | What it holds |
| --- | --- |
| `AGENTS.md` | The map of the application: where modules live, the command for each kind of change, the import rule, the names a module needs, and pointers to the skills and commands |
| `CLAUDE.md` | One line, `@AGENTS.md`, for agents that read that file name |
| `.claude/skills/`, `.agents/skills/` | The same five skills, in the folder each kind of agent reads |

| Skill | The agent loads it to |
| --- | --- |
| `sandloom-module-context` | Map a module before editing it: its files, what it exposes and what depends on it |
| `sandloom-new-module` | Create a module with its migration and tests, and pass the checks |
| `sandloom-quality-gates` | Run every check and fix what fails |
| `sandloom-event-version-roll` | Change an event's fields without breaking the modules that react to it |
| `sandloom-split-deploy` | Run modules as separate services with `sandloom add deploy` |

An application's `AGENTS.md` is a short map. The framework's [module-author contract](https://github.com/Sandloom-dev/sandloom/blob/main/src/sandloom/AGENTS.md) lists every rule with the check that holds it.

## Ask the installed framework

An agent that meets an unfamiliar name asks the framework it runs:

```bash
uv run sandloom docs on
```

The answer comes from the installed version, not from what the agent learned in training (shortened here):

```text
sandloom.sdk.on(event: 'type[E]', *, name: 'str | None' = None, durable: 'bool' = True, …)
  tier: beginner; docs: docs/reference/sdk/index.md
  Register the decorated coroutine as a consumer of ``event`` (§5.4).
```

| Command | Answers |
| --- | --- |
| `sandloom docs <name>` | What a name of `sandloom.sdk`, a contract or an event channel is; `sandloom docs recipes` lists the how-tos with tested code |
| `sandloom explain <code>` | Why a check failed and how to fix it ([Checks](checks.md)) |
| `sandloom context <module>` | What a module provides, uses and owns ([Module maps for agents](module-context.md)) |
| `sandloom graph <module>` | What connects to a module, and what a change would reach ([Explore the architecture graph](architecture-graph.md)) |

## Keep the skills current

`sandloom add skills` writes the skills that are missing and keeps every skill file that differs from this framework version's:

```bash
uv run sandloom add skills
```

When every file matches, it prints "the app's skills are up to date". When a file differs, it names the file and exits 1:

```text
sandloom add: kept .claude/skills/sandloom-new-module/SKILL.md: it differs from the framework's version; `sandloom add skills --force` overwrites it
```

A file differs when you edited it, and also when an upgrade of Sandloom changed the skill: the command cannot tell the two apart. After an upgrade, note your own edits, run `uv run sandloom add skills --force`, and apply your edits again.

## The docs, for agents

These docs are served in a form an agent reads directly. <a href="../../../llms.txt"><code>llms.txt</code></a> lists the documentation pages, and `llms-full.txt` holds them all in one file. Append `index.md` to a page's URL to get it as Markdown.

## Go deeper

- [The toolchain](index.md): where agent tooling sits in the loop of a change.
- [Module maps for agents](module-context.md) and [Explore the architecture graph](architecture-graph.md).
- [CLI reference](../../reference/cli.md): every command and flag.
