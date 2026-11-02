---
title: Agent tooling
description: What a coding agent gets in a Sandloom application (AGENTS.md, the module contract, the skills in each agent's folder, the sandloom commands it can ask, the docs as Markdown), how to choose the agents, and how to keep the files current.
type: concept
job: When a coding agent works in my Sandloom application, I want to know what it reads and which commands it can ask, so I can point it at the right place and keep its instructions current.
purpose: The reader knows what the agent finds in the project and in the docs, keeps the skills current, and points the agent at them instead of pasting rules into a prompt.
audience: [developer, agent]
level: first-app
---

# Agent tooling

A coding agent knows Python but not this project's layout or rules, and an instruction typed into a prompt is gone by the next task.

A Sandloom application keeps its instructions for agents in its own files. `sandloom new` writes `AGENTS.md`, the module-author contract and five skills, procedures an agent loads when its task matches one. Four `sandloom` commands answer questions about the installed framework from the command line. The [checks](checks.md) hold the rules these files describe.

## Choose your agents

Each coding agent reads skills from its own folder, so the application writes them where your agents look. At a terminal, `sandloom new` lists the agents it knows, with the ones installed on your machine marked, and asks which to install for (shortened here):

```text
Coding agents for this project (each reads skills from its own folder):
  [x] 1. Claude Code                .claude/skills  (found: claude)
  [x] 2. Codex                      .agents/skills  (found: ~/.codex)
  [ ] 3. Gemini CLI / Antigravity   .agents/skills
  ...
Numbers or names, comma-separated (Enter keeps the marked):
```

| Folder | Read by |
| --- | --- |
| `.claude/skills/` | Claude Code |
| `.agents/skills/` | Codex, Gemini CLI and Antigravity, Cursor, GitHub Copilot, OpenCode, Amp, Devin Desktop |
| `.kiro/skills/` | Kiro |

In a script or CI, pass the choice: `uv run sandloom new shop --agents claude,codex`. Without a terminal and without `--agents`, the installed agents are used, or Claude Code and Codex when none is found. The choice is kept in `.sandloom/agent-files.json`; commit it, so the team gets the same files.

## What the agent finds in the project

| File | What it holds |
| --- | --- |
| `AGENTS.md` | The map of the application: where modules live, the command for each kind of change, the import rule, the names a module needs, and the skill folders |
| `<app>/modules/AGENTS.md` | The module-author contract: every rule a module follows and the check that holds it |
| `CLAUDE.md`, `GEMINI.md` | One line, `@AGENTS.md`, for Claude Code and Gemini CLI, which read their own file name |
| The skill folders | The same five skills, in each chosen agent's folder |

| Skill | The agent loads it to |
| --- | --- |
| `sandloom-module-context` | Map a module before editing it: its files, what it exposes and what depends on it |
| `sandloom-new-module` | Create a module with its migration and tests, and pass the checks |
| `sandloom-quality-gates` | Run every check and fix what fails |
| `sandloom-event-version-roll` | Change an event's fields without breaking the modules that react to it |
| `sandloom-split-deploy` | Run modules as separate services with `sandloom add deploy` |

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

After you upgrade Sandloom, refresh the agent files:

```bash
uv run sandloom add skills
```

It writes the files your agents are missing and replaces each file it wrote earlier with this version's. A file you edited is kept: the command names it and exits 1, and `uv run sandloom add skills --force` replaces it too.

```text
sandloom add: kept .claude/skills/sandloom-quality-gates/SKILL.md: you edited it; `sandloom add skills --force` replaces it with the framework's version
```

To install for another agent, or stop installing for one, pass the new list, `uv run sandloom add skills --agents claude,codex,kiro`, or run `uv run sandloom add skills --choose` at a terminal to tick them again. Files of an agent you drop stay where they are.

## The docs, for agents

These docs are served in a form an agent reads directly. <a href="/docs/llms.txt"><code>llms.txt</code></a> lists the documentation pages, and `llms-full.txt` holds them all in one file. Append `index.md` to a page's URL to get it as Markdown.

## Go deeper

- [The toolchain](index.md): where agent tooling sits in the loop of a change.
- [Module maps for agents](module-context.md) and [Explore the architecture graph](architecture-graph.md).
- [CLI reference](../../reference/cli.md): every command and flag.
