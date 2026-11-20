# Lint rules

The rules are data the project sets (`tm doc lint-rules`); these are the
defaults. A finding names its `rule`, the section's `anchor` and the `line`.
`doc_lint` lists them all; `doc_lint` with `rules` (`A5`, or a rule id such as
`A4.keyword-case`) narrows the list.

What refuses a write is only what the next write could not repair:

| Refuses | Rules |
| --- | --- |
| `doc_set` | A1, and any rule the project marks `block: set` |
| `doc_submit` (and acceptance) | A2, A7.open-question |
| nothing: told in `lint` | the rest |

## A1 · references hold

Every `doc://slug#anchor`, `task://alias`, `kg:<id>` and `REQ-…` names
something that exists.

- Bad: `See doc://export#naming.` where the document has no section `naming`.
- Good: read the outline (`doc_get` without `section`) and cite an anchor it
  lists. A removed section answers `removedIn`; cite what replaced it.

## A2 · the template of the type

The sections in [templates.md](templates.md), by title.

- Bad: a spec with Summary, Requirements and Notes.
- Good: Goals, Non-goals and Open questions added, each with something in it.
  An empty Open questions section is fine; a missing one is not.

## A3 · abbreviations spelled out

A run of three to six capitals is spelled out once, in brackets either side.

- Bad: `The WAL is checkpointed on close.`
- Good: `The write-ahead log (WAL) is checkpointed on close.`

Common ones (API, JSON, CLI, MCP, HTTP, SQL, UUID, …) need no spelling out.

## A4 · keywords of RFC 2119 in a requirement

In a requirement a keyword is in capitals (RFC 8174), and MUST and MAY do not
share one requirement.

- Bad: `When a section is renamed, the exporter must keep its anchor.`
- Good: `When a section is renamed, the exporter SHALL keep its anchor.`

## A5 · the form of a requirement

One SHALL per requirement, and nothing that cannot be checked.

- Bad: `The search SHALL be fast and SHALL rank exact titles first.`
- Good: two requirements, `The search SHALL answer within 200 ms for 10,000
  sections.` and `The search SHALL rank a section whose title is the query
  first.`

## A6 · forbidden marks

Set by the project, not by default: a project may forbid the marks it keeps
out of its texts, such as a section sign citing another document, a
decision-record number, or a sentence telling history (`used to`, `until
2025`). Say what holds now, and cite a section or `kg:<id>` instead.

## A7 · hygiene

Left-over markers, a word written twice in a row, a sentence over 40 words, a
paragraph over 150, and open questions.

- Bad: `TODO: decide the format.`
- Good: `[NEEDS CLARIFICATION: is the index sorted by type or by slug?]`, which
  reaches a person; answered, the marker goes and the answer stays in the
  section it is about.

## A8 · paragraphs repeated from another document

A paragraph that repeats one elsewhere (80% of its five-word runs, twelve
words or more) is copied text that will drift from its original.

- Bad: the definition of a section anchor pasted into a guide.
- Good: `The anchor rules are in doc://docs-spec#anchors.`
