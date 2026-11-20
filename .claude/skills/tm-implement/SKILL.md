---
name: tm-implement
description: >-
  Pick work up from the active front and carry it to review — read what was
  left on the task, claim it before the first edit, do it, run this repo's
  checks, hand it over with deliverables. Use on "carry on", "take the next
  thing", "what should I work on", or whenever you are about to start editing
  files for work that is tracked in tm.
---

# Taking work from the front and handing it back

The rare branches are in `references/`, each named below where it applies:
work sent back from review, refusals that are not about your call, tasks
tagged `design` or citing documents.

## 1. Find what is ready

`get_active_front` — unblocked, in scope, matching your role's capabilities,
in priority order. It answers a different question from `list_tasks`, which
filters without regard to readiness, and from `active_claims`, which is work
somebody already has. If the person named the task, skip this and `get_task`
it by alias or id.

An empty front is information, not an error: either everything ready is
claimed, or the plan needs `tm-plan`. Say which.

A thing that exists **once** — a running window, a driver port, a device — is
not a capability. A task that needs it names it as an `@<resource>` entry in
its area, and the claim holds it: a second claim naming it is refused with
`AREA_CONFLICT`. What the entry stands for is the project's business: its
`CLAUDE.md` or `AGENTS.md` says.

## 2. What the last agent left comes back with the claim

`task_start` (step 3) answers with it: the `checkpoint` the last holder left,
the knowledge that reaches the task, the notes on the task, how many
reflections the project holds, and the last round of review with the threads
waiting on you. Read that answer before the first file is opened — there is
nothing to ask first. A task with many notes comes with the newest and
`reflections_total`; `memory_task_start` with that `limit` returns them all.
`memory_task_start` also reads a task WITHOUT claiming it.

`entities` are at most five, each with `via` — the road it came by:
`consumes` and `upstream` are edges somebody drew; `area` and `search` are a
guess, so weigh them as one. `related_reflections` are notes the work around
the task left, first any note that names this task: a hand-over left for you.
Read the opening lines and `kg_get` only what you need whole.

Read the answer as separate facts. Empty `entities` means no knowledge
reaches THIS task; empty `reflections`, nothing was written on THIS task.
`project_reflections` above zero with both empty means the answers are in the
project's working memory, and `memory_retrieve` is how you ask it — an agent
that read empty `entities` as "there is no memory" re-derived from the
decision records what its own reflections already said.

- A `review` that sent the work back, or `threads` waiting on you:
  `references/sent-back.md` — every comment is answered.
- Remarks citing documents, or a task tagged `design`:
  `references/design-and-docs.md`.

## 3. Claim it — before the first file is opened

`task_start`. Not after the last edit, not "once I know it works". A hook
refuses an edit of any file **outside the area of what you hold**.

The claim is held by your session — and by your subagent within it — for the
project's claim term (six hours unless the project set another), and every tm
call of the session renews it. One that runs out puts the task back in the
queue with its checkpoint. `task_checkpoint` records what a successor would
want to read — a note, not a pulse. After `/clear` or a restart, `task_start`
on the same task takes it back.

One at a time: a second `task_start` by the same session and subagent is
refused with `ACTOR_BUSY`. Turning to something else is `task_hold` on this
one first.

**Read the task's `area` before you start**: it is the boundary your edits are
checked against.

- `AREA_CONFLICT` means another holder — a session, or a sibling subagent — is
  live in those paths. That is information, not an obstacle: `active_claims`
  says who, and the work is theirs until they put it down.
- Needing a file outside the area mid-task is normal — a test, a spec section.
  Widen it with `task_update --area`, listing the existing paths too (it
  replaces the set). Not normal: widening it over a file another live claim
  covers.
- A task with no area constrains nothing. Do not take that as licence to edit
  the repository at large.

A refusal that is not about your arguments — `CAPABILITY_MISMATCH`, an edit
refused for a claim you thought you held, `NOT_APPROVED` —
`references/refusals.md`.

## 4. Do the work, and write down what changed your mind

Reflections are cheap and the moment does not come back:
`reflection_add --type blocker` when something stops you, `decision` when you
chose between two ways, `replan_trigger` when the plan turns out wrong. See
`tm-memory` for what is worth keeping. If the plan itself is wrong, stop and go
to `tm-plan` — `task_replan` corrects a plan rather than working around it.

## 5. Run this repository's checks, and report what they said

**Before `task_done`, run what covers what you touched.** Which commands is the
repository's: its `CLAUDE.md` or `AGENTS.md` names them. The rule here is
honesty:

- Report what the checks actually said, including failures.
- Say which tests were red on arrival, rather than folding them into "all
  green" or into your own breakage.
- A test that only passes from its own directory is red at the root, which is
  the run that counts.

**Then read the diff — a green suite is not a review.** A test says the
behaviour you thought of still holds; reading the change finds the one you did
not think of. You may review your own work; only accepting it is barred to you.
Where the project asks for that pass, the kinds in `requiredDeliverableKinds`
(`get_project`) say so, and `reviewBy` names what produces it. **What it found
is the deliverable, including when it found nothing** — "nothing found" written
without the pass is the failure this step exists against.

## 6. Hand it over

**Commit the work first.** The review opens the changes from the commits the
handover names. For a task with an area, uncommitted work in it refuses the
handover and names the files, and the commits that touched the area since the
claim are added to the deliverables for you. A task with no area gets
neither; name its commit yourself.

`task_done` with `deliverables` — each `{kind, content | reference, label?}`,
exactly one of `content` or `reference`: the commit, one result saying what the
checks said, and the review where the project asks for one. A task that
requires deliverables, or names kinds, refuses without them; the tag is free
text, so `commit` and `git_commit` are different answers. A task that cites
documents owes them more: `tm-docs` has the checklist. Notes and results are
written in the project's language (`project_status` → `language`).

This moves the task to `in_review`. **You cannot accept it yourself** — under
the default `review` mode a person accepts it. Then stop and say what is in
review; do not claim the next thing on the front without being asked.

## When it does not go that way

| What happened | Call |
| --- | --- |
| attempted, did not succeed | `task_fail` with a reason — bounded retry decides whether it returns to `pending` or ends `failed` |
| a pause, or so another agent takes it, or to replan it | `task_hold` — releases the claim, the task stays on the front (`task_replan` refuses a task with a live executor: hold, then replan) |
| blocked on somebody else's answer, or nobody should take it until a person decides | `task_hold` with `keepInScope: false` — off the front until `task_set_scope` brings it back — plus a `blocker` reflection saying what is awaited |
| deliberately abandoning it | `task_cancel` — terminal and visible, cascades to children |

`task_fail` records an attempt that did not work; `task_hold` records that
nobody is on it. Whoever decides what to pick up next reads them differently.
