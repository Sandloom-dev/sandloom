---
name: tm-consolidate
description: >-
  Turn a project's accumulated reflections into durable knowledge — preview the
  pass before it writes, then do the part the core cannot: read what it held and
  say what the drafts jointly mean. Use when a consolidation task is on the
  front, when the consolidation reminder says a project is due, or when the
  same lesson keeps being written down again.
---

# Sweeping working memory into knowledge

`reflection_consolidate` is not the whole of consolidation. It is the
mechanical half — group by meaning, promote what independent sources agree on,
reinforce what an entity already says — and it is deliberately the only half
the core has: the core does not call an LLM, so it cannot **write** anything.
What consolidation is FOR is synthesis, and synthesis is why this skill exists
and why the work arrives as a task an agent claims rather than as a button.

You are usually here because a `task_done` found the project over the threshold
and filed a task — where the checkout's `.tm/project.yaml` says
`consolidate_hook: on`. That only notices. Nothing about it decided what should
be written.

## 1. Preview first. Always.

```
reflection_consolidate --preview        (tm consolidate run --project <id|name> --preview)
```

It decides everything and writes nothing. This is not caution for its own sake:
the log is accumulate-only, so a pass that surprises you cannot be taken back,
and a pass run without a preview writes the whole backlog with nobody having
read a record of it.

Read the three outcomes, because they are three different jobs:

| outcome | what the core did | what is left for you |
| --- | --- | --- |
| `reinforced` | this draft means what an existing entity already says, so it was added as another source | check the entity still reads true — a draft can reinforce a claim it actually **narrows** |
| `promoted` | independent sources converged, so a new entity was created from the **strongest draft, verbatim** | the entity now says one draft's sentence and cites the others; if the group carried more than that sentence, the synthesis is yours to write |
| `held` | one source, not corroborated — it stays in working memory and keeps decaying | **this is the pile that matters** |

## 1a. Then ask where the pass chose FOR you

```
consolidation_forks --project <id|name> [--limit <n>]
    (tm consolidate forks --project <id|name>)
```

The preview says what the pass will do. This says where it had **two** answers
and took the first one — and the first one is not a judgement, it is
arithmetic. The pass asks "is this near an existing entity?" before "does this
group with other drafts?", and that is a maximum over entities against a
minimum over pairs, so reinforcing wins at **any** threshold. Measured on this
repository: 69 of 88 reinforcements landed on one entity.

**There are two kinds, and the arrow says which.** Forward — the draft
reinforces an entity **and** its counterfactual group would have been PROMOTED,
corroborated by independent tasks. A draft whose group would have been `held`
is not a fork; there was nothing to choose. That is why the list is short: 76
forks of 168 pending drafts here, not 168.

Reverse — the draft cleared **no** entity, so the pass promotes it, and the
report names the nearest entity that exists anyway, below the floor and all.
This one is not reachable by raising the threshold, and it is the more
expensive to miss: a draft can be near an existing entity by MECHANISM and far
from it by subject, and the pass then writes a duplicate of something already
in the graph without a word. Seen here — a draft about the ORDER of two
failures was headed for a new entity while the catalogue of that exact family
sat in the graph, cited by id inside the draft itself.

Every fork carries the opening line of each draft on both sides, so the choice
is readable without opening them again. The arrow marks what the pass does by
itself; the other line is the alternative you are being offered:

```
  Installed the new binary and the claim guard went from 16 passes to 9
  → reinforce  019fe29e…  cos 0.98
    promote    with 2 more, 3 sources
      `tm skills install` lays down the archive EMBEDDED IN THE BINARY…
      The fleet skill is written entirely from experience already paid for…

  A probe on the ORDER of two refusals did not fail the test — a finding about the test
    reinforce  019fe295…  cos 0.79
  → promote    with 1 more, 2 sources
      Mutate the two halves SEPARATELY…
```

**Rows come in order of how NEARLY the pass went the other way** — nearest the
floor first, from either side. So the calls the arithmetic barely made are what
you read first, and a reverse fork cannot be buried under every forward one.

The opening line is not the draft. To read one whole — a row here, a preview
row, a draft the last pass's report named — ask for it by id:

```
reflection_get --project <id|name> <reflection-id>
    (tm memory get <reflection-id> --project <id|name>)
```

The first eight characters of an id are enough, as rows print them. It answers
the text, the author and task, and where the draft stands: `pending`,
`consolidated` with its entity, `no_home` or `retracted` with the reason, and
every entity it was ruled out of. **Not `memory_retrieve`** — that ranks by
meaning, and an id has none: a draft a report named was not found that way,
and the only other road was listing the project's whole memory.

A row may also carry `ruled out of: …` — entities this draft has already been
judged out of, with the reason. It is context for the choice in front of you,
never the choice itself: the entity on the row can no longer BE one of them,
because the planner drops a ruled-out entity from play. The question is not
asked with an answer attached; it is not asked.

The cosine says "reinforce". Read the three drafts and they are about
pack-and-install breaking the checks, while the entity is about entry-point
topology. **That is the whole reason this exists:** a cosine does not separate
"about the same thing" from "in the same genre", at any threshold — measured,
0.90 groups the register, above 0.92 nothing groups, and drafts inside a group
sit 0.088 above random pairs with the ranges overlapping. You do separate them.

Record what you decide, because nothing here writes:

| you decided | you call |
| --- | --- |
| it does belong to that entity | `reflection_absorb --entity <id> <draft>…` |
| the group is its own lesson | `kg_entity_add`, then `reflection_absorb` into it |
| **not that entity, and no home yet** | `reflection_rule_out --entity <id> --reason <t> <draft>…` |
| what the draft claims is false | `reflection_retract --reason <t> <draft>` — see "What to take out" |
| **true, but about one sitting — no entity is its home** | `reflection_no_home --reason <t> <draft>` (`tm consolidate no-home`) |
| nothing fits and nothing was judged | nothing — it stays pending and keeps decaying |

**The third row is the one that keeps a reading.** Answering "not that entity,
and no home yet" with nothing is a leak: the reading vanishes and the next
`consolidate run` proposes the same entity for the same draft, at the same
cosine, because nothing about the geometry has changed and nothing can. Of the
drafts a pass wants to put in one entity, some belong there and some do not,
and without this row only the ones that do leave a trace.

`reflection_rule_out` records the rest. It is read by the pass, not only by
this report: a ruled-out entity stops being a candidate for that draft, so it is
not reinforced into and not offered as the near miss on a reverse fork. It is a
judgement about a pair — never about the draft — so an entity somebody writes
next week is a candidate the moment it exists. Reversed by absorbing the draft
into the very entity it was ruled out of, which needs no verb of its own.

The reason is required here where an absorb's is optional, and the asymmetry is
the point: this takes a candidate away from every later pass, and nothing else
will say why.

**Say why, and say what you turned down.** `--reason` and `--instead-of` on
`reflection_absorb` are what separate your judgement from a coin toss in the
log: a pass never writes a reason, so a reason present means somebody read the
drafts. `--instead-of <entity>` names the branch you rejected, and it does
exactly what `rule_out` does — the entity leaves that draft's candidates, and
the ruling survives an undo, because taking a draft out of an entity does not
unmake the judgement that a *different* one was the wrong home. Naming a
rejected branch
without a reason is refused.

Every resolution also records WHICH judge made it — `caller` when you read the
fork, `api:<model>` when a model did. Nothing else can say: both append under
the same actor, because relaying a verdict is still you making the call. You do
not normally pass `--decided-by`; the CLI fills in `caller` whenever you give a
reason.

**Whether a model may resolve these at all is the project's rule**, not yours
to decide per run: `forkResolver` — `caller | api | both` — which `get_project`
reports and a person sets with `tm project policy <id|name> --fork-resolver`.
Unset, `caller` — the API path refuses. Under `api` or `both`,
`tm consolidate resolve --project <id|name>` puts each fork to the model the
machine's registry names — `judge`, its `endpoint` and `model`, in
`~/.tm/tm.yaml` — and prints the verdicts; `--apply` records them. Under
`api`, an absorb of yours that names `--instead-of` refuses instead — that is
the rule talking, and the message says how to widen it.

`judge.api` names the request shape — `lmstudio`, `openai-chat`,
`openai-responses`, `anthropic` — and you only need it when the endpoint's path
does not. Do not leave it to chance on a path you are unsure of: measured
against LM Studio, the native body sent to `/v1/responses` is ACCEPTED with the
system prompt silently dropped, so the judge would run with neither the genre
warning nor the three verdicts and the output would look ordinary. That one
combination is refused by name; an unfamiliar path is refused rather than
guessed at.

Two things about that path are worth knowing before you reach for it. It writes
only `REINFORCE`, because that is the verdict naming an entity that exists;
`PROMOTE` asks for a synthesis nobody has written and `NEITHER` asks for
nothing, so both come back as advice. And an unreachable endpoint is a
**refusal**, never a fall back to the threshold — the threshold answers every
fork by construction, so that fallback would hand you a run full of decisions
nobody made.

Against the threshold a local model (`google/gemma-4-e4b`) wins every fork,
since the threshold says reinforce to all of them by construction. Against a
reader with the repository open it can lose a hard case — it has a page of
text and no way to open the decision record the draft is about. It is a second
judge, not a better one.

**And a wrong call is not permanent.** `tm consolidate undo --project
<id|name> --reason <text> <draft>…` takes drafts back out: they return to
working memory, resume decaying and come back to the next pass. Use it rather
than living with a pass you accepted too fast.

`total` is printed apart from the list on purpose: a capped answer must not be
read as the whole one. If `total` exceeds what you were shown, you saw a page.

**And the price, so it is not discovered later.** A pass whose forks a model
resolved is not reproducible: the same journal, run twice, can consolidate
differently. That is what is bought — judging what a number cannot. It is true
of you as well, which is why the price is stated once and not as an argument
against the endpoint.

## 2. A reinforcement can be a coin toss — and that is a fact about the graph

The preview prints the cosine to the entity a draft landed on. That number
alone cannot tell a decision from an `argmax` over a plateau — for that you
need the **runner-up**, and neither the preview nor the fork report prints it.
The forks answer a neighbouring question and not this one: they say the other
branch was open (this draft would have promoted a group), not that a second
entity was almost as close. A draft can be a fork with a clear winner among
entities, and a plateau with no fork at all.

It matters because a reinforcement is not cheap to be wrong about: it writes
`ReflectionConsolidated`, and a consolidated draft **halts decay at full stored
importance** and drops out of the next pass's input, attached to whatever it
landed on.

Not permanently — `tm consolidate undo` exists — but not free either: the
undo is a second event a reader has to follow, and a draft that spent a week
pinned drops straight to the weight its age always implied. Cheaper to look
before accepting than to look after.

So before accepting a pass with reinforcements in it, score one draft against
every entity yourself and look at the top ten rather than the top one. The
vectors are not in the log: they are in the store's `embedding_cache`, one per
record and model, beside the digest of the text it was made from — keep only
rows whose model is the active one and whose digest matches the record's
current text (anything else is a cache miss the handler re-embeds on the fly,
so reading it compares two different functions, or an old text).

**A plateau is a verdict on the graph, not on the floor.** Same drafts, same
model, same floor, two kinds of target:

| targets | candidates over the floor | gap to the runner-up |
| --- | --- | --- |
| entities from a bulk one-draft-one-entity pass | a dozen to dozens | thousandths |
| entities somebody wrote by hand | a few | hundredths to tenths |

Against real entities the same mechanism lands most drafts on their own
subject. Against near-duplicate blobs it cannot land anywhere, because dozens
of them are equally close to everything. When the top ten are separated by
thousandths, read the **names** of those ten: if they are forty-character slugs
of somebody's draft, you have found a graph that needs rebuilding, and lowering
or raising a threshold will not touch it. **Decline the pass and say why** —
that is what the preview is for.

**Do not re-litigate the floor from this.** It has been swept against a hand
grouping of the same drafts: at 0.78 unrelated lessons chain together, at 0.86 a
single investigation splits into three, at 0.90 almost nothing groups at all,
and at the shipped 0.82 every multi-draft group was thematically clean. The
number is doing its job.

Blaming the floor on nothing but pairs read by eye is the trap: the same
reader then distils an entity out of the very group it called meaningless.
**Reading pairs by eye gives you a hypothesis; only a labelling made separately
from the thing it judges turns it into a finding.**

## 3. The held pile is the work

A held draft with one source stays held forever unless a second source happens
to arrive. The core cannot do better: it groups by embedding cosine, and two
drafts about the same lesson in different words fall under the threshold.

So read them. For each held draft, or each pair of them, ask:

- **Is this the same lesson as another held draft, said differently?** Then it
  is corroborated in fact and not in cosine. Write the entity yourself with
  `kg_entity_add`, then hand the drafts to it with `reflection_absorb` — see
  below, and do NOT reach for `kg_task_link` instead.
- **Is it a fact about this project that will outlive its task?** A term, a
  measured number, a decision and its reason. Those belong in the durable layer
  whether or not anything corroborates them — see `tm-memory`, which is about
  which layer a thing belongs in.
- **Is it a note about one sitting?** Leave it held. Decay is the mechanism for
  that, and a held draft costs nothing.

Writing by hand is not a workaround for a pass that would not run — it is the
repair. A handful of entities written by hand are targets that the same drafts
then reinforce correctly, where a hundred machine-made blobs absorb everything
into a plateau. **A graph gets good targets by somebody writing them.**

**Writing the entity by hand is half the act. The other half is saying which
drafts went into it:**

```
reflection_absorb --project <id|name> --entity <id> <reflection-id>…
    (tm consolidate absorb --project <id|name> --entity <id> <id>…)
```

It writes exactly what a pass writes for those drafts and nothing else: each
draft's task `produces` the entity, and each draft is marked consolidated. Skip
it and both halves go missing:

- the drafts stay pending, so readiness never falls and the next `task_done`
  over the threshold files this task again — a loop, not a one-off cost;
- the entity has no `produces` edges at all, so the tasks that learned the
  lesson are a sentence inside its text and an edge nowhere — a dozen tasks
  can go missing that way before anybody looks.

`kg_task_link` is not the substitute, and the difference is worth stating
because the wrong one looks right: it draws the edges and leaves the drafts
pending, which closes the visible half and keeps the loop.

Each draft comes back `absorbed`, or `alreadyConsolidated` naming the entity it
is **already** in — which may not be the one you asked for. Nothing is written
for those, and running the whole command again changes nothing. An unknown or
retracted draft refuses the WHOLE call before anything is written, so a typo
cannot leave half a consolidation behind.

What is still the worst available move is running the mechanical pass to clear
the counter: it pins every draft it touches at full weight, forever, onto
whatever it landed on.

## 4. Where the text goes, and the one thing to watch

A promoted entity takes its strongest draft **verbatim**. That is the seam the
core cannot cross: five drafts about one thing become an entity that reads like
one of them, with four ids underneath.

Where the group said more than its strongest member, fix it in place after the
pass:

```
kg_entity_update <entity> --content <the synthesis>
```

Write what the group jointly establishes, and keep the numbers. An entity that
says "the threshold was re-measured" is worth less than one that says "2.4 gave
7.9 passes a day on a 4.8-day journal; 8.0 gives 2.9, which is the rate the
mechanism was taken from". The second can be argued with.

`kg_entity_rename` is the repair for a bad name; `kg_entity_update` for a wrong
body. A name that stopped being true is not a new entity.

## 5. What to take out

A draft that later proved false is not deleted — nothing is. It is retracted,
with a reason and an author, so the next reader sees that somebody decided
rather than that a record went missing:

```
reflection_retract --reason <t> [--superseded-by <draft>] <draft>
    (tm memory retract <draft> --project <id|name> --reason <t>)
```

`--superseded-by` names the draft that says what is true now, when one does,
and `--restore` takes the retraction back. Until then the draft is in neither
the preview nor the fork report, and `reflection_absorb` refuses it.

**Nor a true draft that has no home anywhere.** A note about one session —
right, and worth finding while it fades — ruled out of one entity is offered
to the next one over the floor, and here one went through four entities in
four previews. `reflection_no_home` is the judgement about the draft: the pass
and the fork report stop offering it to any entity, while `memory_retrieve`
still finds it and it keeps decaying. Absorbing it into any entity lifts it.
Before reaching for it, grep the code: a lesson already in a doc comment or in
`CLAUDE.md` or `AGENTS.md` is homeless in the graph for exactly this reason.

**Do not answer a false draft with `reflection_rule_out`.** A ruling judges one
pair, so the next preview offers the same draft to the next entity over the
floor — measured on this repository, one refuted draft went through three
entities in three previews, and every ruling was spent on a question nobody
needed answered. Retraction is the judgement about the draft itself, and it is
what makes the durable layer safe to trust; `tm-memory` says when a note is
false rather than merely homeless.

Retracting a draft that is already in an entity does not take it out:
`reflection_unconsolidate` does that, and what the entity says is
`kg_entity_update`'s.

## 6. A long tail is bounded, not swallowed

The first pass after a quiet stretch faces everything that accumulated. Preview
says how much. If it is more than you can actually read, consolidate the part
you read and leave the rest — readiness is recomputed from what is still
pending, so the next pass picks up where you stopped. A pass you did not read
is the failure this whole skill exists against, and doing half of it well beats
approving all of it blind.

## 7. Hand it over

The task closes like any other, and its deliverable is the interesting part:
what was promoted, what was reinforced, what you wrote by hand and why, and
what you left held. That last one is a result, not an omission — it is the
record of what the project decided it did not yet know.
