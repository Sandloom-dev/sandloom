---
name: tm-memory
description: >-
  What a project remembers between sittings — reflections as raw working
  memory, knowledge entities as the durable form, and how to read either back.
  Use when something was learned that the next sitting would pay to relearn,
  when asked why a decision was made or what a term means here, or when a
  finding keeps recurring and should stop being a surprise.
---

# What the project remembers

Nothing in an agent loop calls these on its own, and no operation's description
can say "now is the moment". That is what this skill is for: the moments.

## Two layers, and they are not the same thing

**A reflection** is raw working memory — a note left on a task or a project as
it happens. Reflections accumulate, decay in weight over time, and are ranked
rather than filtered.

**A knowledge entity** is the durable form — a term, a component, a decision
that outlives the task that produced it. Entities do not decay; they go stale,
which is a different claim and one you can act on.

The bridge is `reflection_consolidate`, which promotes corroborated reflections
into entities; the consolidation reminder says when there is enough to promote
(`tm consolidate ready` asks the same at a terminal). Writing
straight to the durable layer is `kg_entity_add`, and it is the right call when
you already know the thing is durable — a term the project uses, a decision
with a reason. Its answer names the entities that already share the new one's
phrasing (`similar`); the entity is written either way, so `kg_search` for the
lesson before adding is how a twin is found before it is one.

An entity that turned out wrong is corrected in place with `kg_entity_update`,
and its answer lists the tasks downstream (`downstream`) — the work that rested
on what you just changed, which is worth a look once it says something new.

**The first line is the lesson, because the first line is what travels.** A
claim hands each entity over by its name and opening line (`lead`, about two
hundred characters), and most readers never open the rest. So the first line
says the rule, when it applies, and where it is anchored — a file, a constant,
a commit — and the body holds the measurements and the story. A first line
that is a heading or the name again delivers nothing, and neither does one
that says where the entity came from — a task and a date, the drafts it was
made of — or stops at a colon before its list. When `kg_entity_add` or
`kg_entity_update` answers with `leadHint`, the write went through and the
first line is one of these: rewrite it with `kg_entity_update` before the
next claim is handed it.

**Not everything kept is a lesson.** The history behind a comment, a
superseded decision: worth keeping on the record, noise in every answer. Give
such an entity the type `archive` (`kg_entity_update` with `type`): searches
and claims leave it out unless asked (`includeArchive`). A declared departure
from a design is not one of them: it is a decision still in force, the reason
the code differs from the drawing, and the work on that code needs it in its
claims — it stays `decision`.
And what a doc comment or the code already says does not go into the graph at
all — it is found where it is, and a copy is one more thing to keep true.

## Write a reflection when

| Moment | Type |
| --- | --- |
| something stopped the work | `blocker` |
| you chose between two ways and the choice is not obvious from the diff | `decision` |
| the plan turned out not to match reality | `replan_trigger` |
| you understood why something is the way it is | `insight` |
| you noticed something true that nobody had written down | `observation` |

`importance` is a weight, never a gate — a low score still stores and still
surfaces, just lower down. So the question is never "is this important enough
to record", it is "would the next sitting pay to rediscover this".

**Not worth recording:** what the code already says, what the git history
already says, what the task description already says. A reflection restating
the diff is noise that pushes down the one that is not.

The moment is *while it happens*. Reconstructed at the end of a task, the
reason you rejected the first approach is exactly what you no longer remember.

## When a note turns out to be wrong

A reflection is never edited and never deleted. When what it claims is refuted
— the code says otherwise, a measurement came out the other way — **retract
it**: `reflection_retract` with a reason, and `supersededBy` when a later note
says what is true now. The note keeps its place in the log with the retraction
attached, so the next reader sees a decision rather than a gap. What stops is
everything that offers it as current: `memory_retrieve` withholds it, and
consolidation no longer proposes it for any entity. `restore` takes a
retraction back, with a reason of its own.

Retraction is for a claim that is FALSE, and the neighbours are easy to reach
for instead:

| the note is | call |
| --- | --- |
| false | `reflection_retract` — a judgement about the note |
| true, but does not belong in the entity it was offered | `reflection_rule_out` — a judgement about that pair only; the pass then offers the note to the next entity over the floor, which is right for a true note and a treadmill for a false one |
| true, but only about one sitting | `reflection_no_home` — the note stands, is found and decays, and consolidation stops offering it to one entity after another; retracting would call a true note false |
| already consolidated into the wrong entity | `reflection_unconsolidate` — a retraction does not take a note out of an entity |

A wrong entity is corrected in place with `kg_entity_update`; the drafts under
it are retracted only where they are wrong too.

## Reading it back

- `task_start` — the claim answers with **both layers for the task**: the
  knowledge that reaches it — at most five entities by name and opening line,
  each with `via`, the road it came by: `consumes` (the task links it, or
  its text names it), `upstream` (a predecessor, the one before it or an
  ancestor produced it), `area` (it names a path of the task's area and is no
  catalog of most areas), `search` (it matched the task's text and its name
  shares a rare word with the task's alias or title) — the notes left on the
  task, up to three notes the work around it left — its upstream tasks, the
  ones beside it under its parent, or no task — with any note that names the
  task by alias or id first, wherever it was left (`related_reflections`),
  and how many reflections the project holds. Read
  the opening lines; `kg_get` only the entity you need whole. `matchRefused`
  says the text match could not run (no embedder), not that nothing matched.
  `memory_task_start` is the same answer without the claim — a **read** — for
  work sent back or a task you are not taking.
- `memory_retrieve` — what this project learned, ranked. The general question,
  and the one to ask when a task comes back empty beside a project that is
  anything but.
- `kg_get` — one entity whole, by its id or its name, with the tasks that
  produced it and consume it: the read for an entity you were handed — in a
  task's `entities`, a report, a link. Empty `entities` at a claim means no
  edges reach that task and says nothing about the reflections; reading the
  two as one system is what the claim's answer is for.
- `kg_search` — by name or meaning, when you have a term and want what the
  project has decided about it. It answers five rows unless `limit` says
  otherwise, with `total` for how many matched and `applied` for what was
  left out — matches too weak to be an answer, and the `archive`
  (`includeArchive` brings it back). An empty answer means the graph has
  nothing on it, not that the search failed. Each row is the passage that
  matched, with how long the whole entity is; `kg_get` the one you need whole.

## Linking knowledge to work

`kg_task_link` ties an entity to the task that produced or consumed it, and
`kg_get` reads that back from the entity's side. It is what makes "why does this exist"
answerable later: an entity with no task behind it is an assertion, an entity
linked to the task that established it is a record.

The other direction is what delivers. `kg_task_link` with `refType: consumes`
says a task needs the entity, and a claim of that task hands it over first and
never cuts it — the one road that does not depend on a cosine. Its default is
`produces`, so say `consumes` when you mean it.

## Before adding an entity

`kg_search` for its name and its subject first: the same thing under another
name is the duplicate a graph collects. At a terminal, `tm kg dedup-advisory`
asks the same of a would-be entity, and `tm kg gap-advisory` asks which
existing knowledge might fill what a task waits on. Both are advice, not gates
— they surface a suspicion for you to resolve, and resolving it is the point.

## Use the project's own vocabulary

When you name a domain concept — in a task title, a reflection, a test name —
use the term as the graph and the project's context doc define it. Drifting to
a synonym creates a second name for one thing, and the second name is invisible
to everyone searching for the first.

If the concept is not in the graph yet, that is the signal: either you are
inventing language the project does not use, or there is a real gap, and one of
those two is worth writing down before you go on.
