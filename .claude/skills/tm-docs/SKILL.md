---
name: tm-docs
description: >-
  Write and use a project's documents in tm (spec, design, guide, reference)
  section by section, with requirements a task can cite. Use when a task asks
  for a spec or a document, when a task cites `doc://` or `REQ-…`, when a
  handover changes behaviour a document describes, and before planning a
  feature that has no spec.
---

# Documents in tm

A document says what the project **intends**: what the system shall do, how
it is built, how to do a thing, what a thing is. The knowledge graph holds
what was **learned**. The two are searched apart (`doc_search`, `kg_search`)
and they never copy each other: a document cites an entity as `kg:<id>`.

Documents live in tm, not in files of the repository. A file made from one
(`tm doc export`) is generated and not edited; `tm doc import` reads Markdown
in once.

## Which document

| The reader needs | Type | A task cites |
| --- | --- | --- |
| what the system shall do | `spec` | one requirement, `REQ-<doc>-<NNN>` |
| how it is built and why that way | `design` | the section that is the contract |
| how to do a thing | `guide` | the document |
| what exactly a thing is | `reference` | one entry |

Not a document: a decision, a rule, an observation. Each is an entity in the
graph (`decision`, `rule`, `finding`), and a document links it rather than
retelling it. There is no decision-record document type.

## When a spec is needed

Write a spec, or extend one, when any of these holds:

- the work is bigger than one sitting, so it will be sliced;
- the behaviour is seen from outside: an operation, a command, a file format,
  a screen;
- more than one agent will build against it.

Otherwise the task's description is the spec. A spec for a one-sitting change
is overhead nobody reads twice.

## Writing

1. **Search first.** `doc_search` and `kg_search` for the subject. A section
   that covers it is extended; a second document on the same thing repeats
   paragraphs the lint flags ([lint.md](lint.md)) and drifts from the first.
2. **Start from the type's template** ([templates.md](templates.md)). A
   document goes to review only with the sections its type needs.
3. **Write section by section.** `doc_set` writes one section per call: the
   first call names `type` and makes the document; each later one makes or
   replaces one section. A section is a `##` (depth 2) or a `###` under one
   (`parent`). Its anchor is minted once and stays when the title changes, so
   the anchor is the address, not the title. Send `expectRev` with the `rev`
   you read: another writer's revision in between is refused `DOC_CONFLICT`
   instead of lost.
4. **Read before you write a section that exists.** `doc_get` without
   `section` is the outline (anchors, titles, sizes, revisions, requirement
   ids). Read the one section you will change, not the whole document.
5. **Requirements** take the EARS form with one SHALL each; the layout is in
   [templates.md](templates.md). A section's body names each of its
   requirements by a line `::req REQ-<doc>-<NNN>`, exactly once; `doc_set`
   refuses a body that drops one or names one that was never minted. A
   requirement is minted only by `tm doc import`, from the requirement block
   of an exported file, and import makes a new document only: a spec with
   requirements is drafted as one Markdown file and imported, then kept by
   `doc_set`. A spec that exists gains no requirement and changes none, so
   write them all before the import. A number is never reused; removing the
   section (`tm doc rm`) withdraws its requirements.
6. **Do not guess.** What you do not know is written
   `[NEEDS CLARIFICATION: <the question>]`. It is a warning in a draft and an
   error at submit, so the question reaches a person instead of a guess
   reaching the code.
7. **Read the lint.** `doc_set` answers `lint`, the warnings of the section and
   of the document. It refuses `DOC_LINT_ERRORS` for a reference to nothing
   and for what the project forbids. `doc_lint` lists every finding; the rules
   and what each wants are in [lint.md](lint.md).
8. **Submit.** `doc_submit` sends the draft to review once the template is
   whole and no question is open. A person accepts it, and never one who wrote
   any part of it (`DOC_SELF_REVIEW`). A document sent back reads its reason in
   `doc_get` (`statusNote`) beside the threads left open: answer a thread by
   changing what it is about and submit again; settling it is the person's.

Writing to an accepted document sends it back to review. A superseded or
withdrawn one takes no writes.

Documents are written in English, the language of the repository, so EARS and
RFC 2119 keywords read as keywords.

## Citing from a task

- A task's description that names `doc://<slug>#<anchor>` or `REQ-<doc>-<NNN>`
  links the task to it as `implements` when the task is created or updated.
  Only a plain mention counts: inside backticks, quotes, a quoted line or a
  code block it is an example, not a citation.
- `specifies`, for the task that writes or changes the section, is never
  inferred: `doc_link` with `rel: specifies`.
- Cite an `accepted` document. A draft is cited by the task that writes it;
  the claim warns any other task that builds on one.
- At `task_start` the claim lists the cited sections with their revision,
  says which moved since the task was written, and records the revision the
  work starts from. Read those sections by `doc_get` with `section`, not the
  whole document; `doc_status` with `task` says whether any is stale.

## Updating at handover

- **The behaviour a section describes changed:** write a new revision of the
  section in the same task. A task that `specifies` a section and hands over
  with its revision unchanged is warned in the remarks of `task_done`. The
  warning is counted; read it and either revise the section or say in the
  handover why it still holds.
- **A requirement cannot be met as written:** do not edit it from a task that
  only implements it. Write a `finding` (`kg_entity_add`) that names the
  `REQ-…` and what makes it impossible, leave a `blocker` reflection on the
  task, and `task_hold` it so a person decides.
- **A section is wrong or gone:** `doc_status` says which sections are
  `stale` (they cite an operation or a path that is not there) and which
  `suspect` (a task that specifies them was done and left them as they
  were). A stale section is lifted only by a new
  revision. That a suspect section still holds is a person's word
  (`tm doc confirm`), not an agent's. A section removed with `tm doc rm`
  leaves a tombstone, so links to it say when it went.

## Checklist before `task_done`

- Every requirement the task implements has a scenario or a named check that
  proves it, and the handover says which.
- No `[NEEDS CLARIFICATION]` is left that this task was to close.
- `doc_lint` shows no error in the documents the task wrote.
- Each section the task specifies has a new revision, or the handover says why
  it needed none.

Where the tm MCP server only reads, each write above is the same verb of the
shell: `tm doc set`, `tm doc link`, `tm doc submit`.
