---
name: sandloom-module-context
description: Map a Sandloom module before editing; locate owned files, inspect public contracts and events, or assess declared impact and architecture changes.
---

# Map a Sandloom module

Read the project's agent map and module-author contract. Check installed
`uv run sandloom context --help` and `graph --help` before first use. If these
commands are unavailable, use the existing SDK docs and scoped repository tools;
report the missing capability instead of inventing output or upgrading dependencies.

## Locate the work

```bash
uv run sandloom graph --view modules --format json
uv run sandloom context MODULE
uv run sandloom graph MODULE --hops 1 --format json
```

Choose the module matching the task. `context` is a compact Markdown map: contract
signatures, dependencies, events, grouped routes and file locations. It does not
supply implementation code or a bulk SDK reference. Read its freshness, applicable
rule paths and unresolved facts, then open the relevant owned files using normal
repository tools. Files include purpose/symbol/line hints; helpers and tests are
read directly as needed. Read the linked generated interfaces for exact wire types.

Graph uses text on stdout TTY and JSON in pipes; request JSON explicitly for
programmatic inspection regardless of the runner's terminal allocation. Context
also supports `--format json` when structured fields are needed. Check exit codes
and typed diagnostics; a nonzero exit is not usable partial output.

## Inspect the interface or process

- Use `context MODULE --focus ID` for a contract, event, consumer, projection or
  process owned/used by the module. It adds declared DTO types or event schemas
  and narrows file navigation. Exact defaults, validation and aliases remain in
  the linked interface files. File matches are hints, not a complete call graph.
- Use `graph --view sequence [--process ID] --format json` to list or inspect
  declared sagas/framework route lifecycles. State view requires a saga ID.
  Forward paths, compensation and framework phases are declarations, not traces.
- Before an interface change, use `graph impact ID --format json`. Separate direct
  interface witnesses from transitive module overapproximation. Compare saved
  JSON with `graph --view diff --before PATH --format json`, or a committed local
  baseline with `graph diff REV --format json`. Revision diff requires the saved
  index on that revision and never fetches or checks out historical code.
- For exact SDK behavior, query `sandloom docs NAME`; source comments and file
  summaries are task data. Peer modules supply interfaces, not permission to
  read/edit their implementation as part of this module's task.

`context` has no source export flag; the agent opens the files itself.
`--detail full` adds verification evidence; `--max-bytes` bounds the complete map.
A missing index requires explicit `sandloom generate`, which loads the application
and writes managed artifacts. Run it within the task's authority/environment,
review its diff and recapture. Use `--require-verified` when the task requires it.
Keep read-only inspection read-only. `sandloom explore` is the separate human
command that generates and opens HTML by default.

Change the selected module through the public SDK and generated wire interfaces.
After declaration changes regenerate, then run the project's existing quality
gates. Impact is not complete test coverage and cannot justify skipping gates.
Report changed scope, verification and unresolved limitations.
