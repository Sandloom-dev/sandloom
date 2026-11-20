# A task tagged `design`, or one that cites documents

## Design

A task tagged `design` was written against one version of a design, and the
design may have moved since. The answer to `task_start` says which version the
task records in `metadata.design`, or that it records none. Read the live
version before drawing and compare. If it moved, add to the task what changed
and update `metadata.design` to the version you work from; the handover says
which version the work was checked against. How to read a design's version is
the project's to say — its `CLAUDE.md` or `AGENTS.md`.

## Documents

A task that cites the project's documents is told so in the claim's remarks:
each section and requirement it `implements` or `specifies`, at which
revision. Read those sections, each by `doc_get` with its `section`, and not
the whole document. Where the spec and the code you find disagree, the spec is
not quietly bent to fit: a `finding` naming the `REQ-…`, a `blocker`
reflection, and the task held for a person. `tm-docs` has the steps, and the
checklist of what a handover owes the documents.
