---
title: Module maps for agents
---

# Module maps for agents

`sandloom context MODULE` returns a compact navigation map: what the module
provides, what it uses, who depends on it, and where its implementation lives.
The agent opens the relevant files itself. Implementation bodies, whole source
files, instruction documents and the SDK reference are not embedded in the map.

!!! note "Package availability"
    Added after 0.7.0a3. Use the current source checkout until the next package release.

```bash
uv run sandloom graph --view modules --format json
uv run sandloom context notes
uv run sandloom context notes --focus contract:stats.quota.v1
```

## What the map contains

- Provided and consumed contracts: declared method signatures, read/command kind,
  optionality, and callers/providers in this authoring composition.
- Published events and their declared listeners; incoming events and handlers.
- HTTP routes grouped by namespace/resource, tables, sagas and infrastructure
  requirements. These are declarations, not runtime traffic or active bindings.
- Owned file paths, sizes, first-line purpose from the module docstring, and a
  few public symbols with line numbers. More symbols are counted explicitly.
  Schemas, models, migrations and tests have directory summaries.
- Paths to applicable instruction documents and generated public interfaces.
  Read the selected rules before editing, then open the implementation files
  needed for the task. Peer implementations are outside the map.

Descriptions come from source metadata; the tool does not invent explanations
or infer a complete call graph. Syntax errors, non-UTF-8 text and oversized implementation files leave
their paths available with an explicit metadata warning. Test bodies are not
parsed or decoded for directory counts; raw source hashes still track freshness. They do not make stale
recorded declarations current. Migration heads come from literal revision links,
not filename sorting. Test summaries count files, not executed test cases.

## Narrow an interface

```bash
uv run sandloom context notes --focus contract:stats.quota.v1
uv run sandloom context notes --focus event:stats.changed.v1
```

Focus restricts the interface map and file navigation hints. Contract detail adds
DTO field types and requiredness; event detail adds its declared wire schema.
Exact validation rules, aliases and defaults remain in the linked generated
interface files. The agent reads those definitions when needed.

Route focus shows exactly the selected route and its recorded handler location.
Its opaque implementation does not establish which contracts or tables it uses.
Saga focus includes declared contract steps and local callback locations. Other
source matches use exact syntax landmarks, not substring search. These are
navigation hints, not a complete call graph. Open related
helpers and tests using normal repository tools. `context` has no source export
flag; `--file` is not supported. A focus must belong to or be used by the selected
module. A consumer/projection focus retains its incoming event interface.

## Formats and limits

```bash
uv run sandloom context notes --format json
uv run sandloom context notes --format json --detail full
uv run sandloom context notes --max-bytes 131072
```

Markdown is the default in terminals and pipes. JSON returns the same map in a
versioned response envelope; `data.representation` is `module_map`. Full detail
adds verification evidence, without embedding implementations or expanding the
business scope. The default limit is 131072 UTF-8 bytes for the complete response,
not a token estimate. A map that exceeds the limit fails with a remedy; no
successful partial map is returned.

`.sandloomignore` contains project-relative glob patterns, one per line. Blank
lines and `#` comments are ignored; negation is unsupported. It excludes owned
file metadata, not mandatory rule/interface references. The map reports excluded
counts by reason, including `context_ignore` and `outside_focus`.

## Freshness and cold reads

The map reads `.sandloom/inspection.json`, produced by explicit `sandloom generate`,
and current owned file metadata. It never loads `.env`, imports the application,
connects to backends or regenerates implicitly. The semantic index belongs in Git;
the local verification receipt lives under ignored `.sandloom/cache/`.

| State | Meaning |
| --- | --- |
| `verified` | Selected inputs match a successful authoring generation. |
| `missing_receipt` | Recorded declarations are readable, but have no local receipt. |
| `source_changed` | Source membership/content or scoped rules changed. |
| `dependency_changed` | The wire/interface verification basis changed. |
| `partial` | Selected modules have different verification states. |

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
uv run sandloom context notes --output notes-map.md
```

The destination receives the map; stdout receives a small JSON acknowledgement.
Without `--output`, JSON mode emits exactly one success/error envelope. Markdown
errors leave stdout empty and explain on stderr. Existing artifacts require
`--force` and must be recognized Sandloom inspection exports. Arbitrary files,
source/managed inputs and reserved tool/VCS directories are protected; detectable
concurrent destination changes are preserved. Output paths are project-relative.

The tool never sends the map to an LLM. Installed-only dependencies contribute
public interfaces, while a directly selected context target requires owned source.
See the [architecture graph](architecture-graph.md) for impact and process views.
