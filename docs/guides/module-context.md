---
title: Module maps for agents
---

# Module maps for agents

`sandloom context MODULE` returns a compact navigation map: what the module
provides, what it uses, who depends on it, and where its implementation lives.
The agent opens the relevant files itself. Implementation bodies, whole source
files, instruction documents and the SDK reference are not embedded in the map.

!!! note "Package availability"
    Available in `sandloom>=0.7.0a4,<0.8`. <!-- sandloom-release -->

```bash
uv run sandloom graph
uv run sandloom context notes
```

## What the map contains

- Provided and consumed contracts: declared method signatures, read/command kind,
  optionality, money risk (`[money]`), and callers/providers in this composition.
  Idempotency key parameters remain visible in method signatures.
- Published events and their declared listeners; incoming events and handlers.
- HTTP routes with handlers, tables grouped by tenant scope, sagas and
  infrastructure requirements. Markdown marks public routes with `[public]`;
  unmarked routes are authenticated. These are declarations, not runtime traffic
  or active bindings.
- Owned file paths, first-line purpose from the module docstring, and a
  few public symbols with line numbers. More symbols are counted explicitly.
  Schemas, models, migrations and tests have directory summaries. Inert package
  initializers are counted as excluded scaffolding; meaningful initializer logic
  remains navigable. JSON additionally includes file sizes.
- Paths to applicable instruction documents and generated public interfaces.
  Read the selected rules before editing, then open the implementation files
  needed for the task. Peer implementations are outside the map.

Maps contain module-specific facts, without a next-step checklist. The
`sandloom-module-context` skill covers follow-up commands and inspection limits.
Unresolved declarations belong to the selected module. Structured output retains
stable IDs.

Descriptions come from source metadata; the tool does not invent explanations
or infer a complete call graph. Syntax errors, non-UTF-8 text and oversized
implementation files leave their paths available with an explicit metadata
warning. Test bodies are not parsed or decoded for directory counts; raw source
hashes still track freshness. Source changes invalidate the freshness receipt. Migration heads come from literal revision links,
not filename sorting. Test summaries count files, not executed test cases.

## Read the relevant files

The complete module map points to generated public interfaces. Open those files
for exact DTO fields, validation, aliases and defaults; then use normal repository
search to inspect owned implementations and tests. The map does not embed source
bodies or infer dependencies inside opaque handlers. `context` has no source export
or interface-slicing flag.

## Formats

```bash
uv run sandloom context notes --format json
```

Markdown is the default in terminals and pipes. JSON returns the same map in a
versioned response envelope; `data.representation` is `module_map`. The command
accepts `--format`, `--require-verified` and `--root`. Graph-specific
output budgets and full provenance are outside the module map interface.

`.sandloomignore` contains project-relative glob patterns, one per line. Blank
lines and `#` comments are ignored; negation is unsupported. It excludes owned
file metadata, not mandatory rule/interface references. JSON retains counts for
`context_ignore` and `package_scaffolding`. Routine scaffolding/filter accounting
stays out of the text map.

## Freshness and cold reads

The map reads `.sandloom/inspection.json`, produced by explicit `sandloom generate`,
and current owned file metadata. It never loads `.env`, imports the application,
connects to backends or regenerates implicitly. The semantic index belongs in Git;
the local verification receipt lives under ignored `.sandloom/cache/`.

| JSON state | Markdown label |
| --- | --- |
| `verified` | verified |
| `missing_receipt` | not verified on this checkout |
| `source_changed` | source changed since last generate |
| `dependency_changed` | declarations or dependencies changed since last generate |
| `partial` | verification incomplete for this scope |

A fresh clone normally has no local verification receipt; its recorded declarations
remain readable. JSON keeps the stable status codes for automation.

This is source evidence, not production health. `--require-verified` exits 3 when
verification is unavailable. Missing mandatory interfaces fail independently;
malformed or unsupported receipts produce typed errors. A missing index or changed
layout requires explicit generation, which loads the app and writes managed files:

```bash
uv run sandloom generate
uv run sandloom context notes --require-verified
```

## Save a map

```bash
uv run sandloom context notes > notes-map.md
```

Both inspection commands write their result to stdout; use shell redirection to
save it. JSON mode emits one success/error envelope. Markdown errors leave stdout
empty and explain on stderr. `sandloom explore` manages its own HTML cache file.

The tool never sends the map to an LLM. Installed-only dependencies contribute
public interfaces, while a directly selected context target requires owned source.
See the [architecture graph](architecture-graph.md) for impact and process views.
