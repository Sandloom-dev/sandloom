# Templates

The sections each type needs before `doc_submit` lets it go to review (rule
A2). The order is the one a reader expects. A section is a `##`; anything
finer is a `###` under it. Russian titles of the same sections count too
(`Требования`, `Открытые вопросы`, …).

## spec

What the system shall do, seen from outside.

| Section | Holds |
| --- | --- |
| Summary | two or three sentences: what this is and who it is for |
| Goals | what will be true when it is built, each checkable |
| Non-goals | what a reader might expect and will not get |
| Requirements | the requirements, one block each (below) |
| Open questions | each `[NEEDS CLARIFICATION: …]` still open; empty at review |

A spec large enough to need more sections gets them as `###` under
Requirements, one per area of behaviour, each holding its own requirements.

### A requirement

One EARS sentence with one SHALL, then the scenarios that prove it. The kind
is one of `ubiquitous`, `event`, `state`, `optional`, `unwanted`:

| Kind | Form |
| --- | --- |
| ubiquitous | The `<system>` SHALL `<response>`. |
| event | When `<trigger>`, the `<system>` SHALL `<response>`. |
| state | While `<state>`, the `<system>` SHALL `<response>`. |
| optional | Where `<feature>`, the `<system>` SHALL `<response>`. |
| unwanted | If `<condition>`, then the `<system>` SHALL `<response>`. |

In tm the section's body carries `::req REQ-export-001` on a line of its own
where the requirement stands. In the Markdown that `tm doc import` reads, and
that `tm doc export` writes, the same requirement is a quoted block:

```markdown
## Requirements

<a id="REQ-export-001"></a>
> **REQ-export-001** (event)
>
> When a section is renamed, the exporter SHALL keep its anchor unchanged.
>
> | Scenario | Given | When | Then |
> | --- | --- | --- | --- |
> | renamed heading | a task cites export#rules | the heading is renamed | the anchor still resolves |
```

The id is `REQ-<slug>-<NNN>`, the slug being the document's. A number is
never reused. Import keeps each id that carries the document's slug and
renumbers the rest after the highest, saying so in its `notes`.

## design

How a thing is built and why that way.

| Section | Holds |
| --- | --- |
| Summary | what is built and the one choice that shapes it |
| Context | the forces: what exists, what constrains, which spec it serves |
| Design | the contract other work builds against; tasks cite this section |
| Alternatives | each option weighed, and why it lost |
| Risks | what could go wrong and how it would show |
| Traces | the requirements it meets (`REQ-…`), the decisions it rests on (`kg:<id>`) |

The decision itself is an entity in the graph (`decision`); the design links
it under Traces and does not restate it.

## guide

How to do one thing.

| Section | Holds |
| --- | --- |
| Summary | the outcome and who needs it |
| Prerequisites | what must be in place first |
| Steps | numbered, one action each, with the command to run |
| Verification | how the reader knows it worked |

## reference

What exactly a thing is.

| Section | Holds |
| --- | --- |
| Summary | what the entries describe |
| Entries | one `###` per entry, every entry in the same form |
