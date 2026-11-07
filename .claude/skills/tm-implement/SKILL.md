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

## 1. Find what is ready

`get_active_front` — unblocked, in scope, matching the capabilities offered, in
priority order. It answers a different question from `list_tasks`, which
filters without regard to readiness, and from `active_claims`, which is work
somebody already has.

An empty front is information, not an error: either everything ready is
claimed, or the plan needs `tm-plan`. Say which.

If the person named the task, skip this and `get_task` it by alias or id.

**You do not offer capabilities; the front already knows what you can do.**
Your role's record in the project's registry carries them — skills such as
"can review a design" — and both the front and `task_start` read them from
there, so a task gated on one is invisible to a role that does not have it. A
`CAPABILITY_MISMATCH` is an answer about the REGISTRY, not about your call:
nobody has told it what the role can do, or this work is not the role's to
take. The refusal names both sides, and the repair is `actor_update` by whoever
decides, not a re-claim with a wider list. There is no wider list to send.

A thing that exists **once** — a running window, a driver port, a device — is
not a capability. A task that needs it names it as an `@<resource>` entry in
its area, and the claim holds it: `task_start` refuses a second claim naming
it with `AREA_CONFLICT`, naming the holder. What the entry stands for here, and
how to use it, is the project's own business: its `CLAUDE.md` or `AGENTS.md`
says.

## 2. What the last agent left comes back with the claim

`task_start` (step 3) answers with it: the `checkpoint` the last holder left
the task at, the knowledge that reaches this task through provenance, the
notes left on the task itself, how many reflections the project holds, and the
last round of review with the threads waiting on you. Read that answer before
the first file is opened — there is nothing to ask first. A task with many
notes comes with the newest of them and `reflections_total`;
`memory_task_start` with that `limit` returns them all. A separate read before every claim came back empty 140 times in
149, a turn spent each time; carried on the claim it costs nothing when empty.

To read a task WITHOUT claiming it — somebody else's, or one you are still
deciding about — `memory_task_start` answers with the same fields and changes
nothing.

`entities` are at most five, each by name and opening line and each with
`via` — the road it came by: `consumes` (the task links it, or its text names
it), `upstream` (a predecessor, the one before it or an ancestor produced it),
`area` (it names a path you are about to touch), `search` (it matched the
task's text and shares a rare word of its name with the task's alias or
title). The first two are edges somebody drew; the last two are a guess, so
weigh them as one. `related_reflections` are notes the work around the task
left — on its upstream tasks, on the ones beside it under its parent — and,
first, any note that names the task by alias or id: a hand-over left for you.
Read the opening lines and `kg_get` only what you need whole.

Read the answer as separate facts, because one layer says nothing about the
other. Empty `entities` means no knowledge reaches THIS task. Empty
`reflections` means nothing was written on THIS task. `project_reflections`
above zero with both lists empty means the answers are in the project's
working memory and `memory_retrieve` is how you ask it: an agent that read
empty `entities` as "there is no memory" went off to re-derive from the
decision records while the reflections it wrote itself sat in the layer it
never asked.

Skipping this is how the same wall gets walked into twice, and the second time
nobody writes it down either.

### Work that was sent back

A rejected review hands the task back to the submitter's role, reserved: no
session holds it until one of the role calls `task_start` on it, which takes
it under a new epoch. Read before that call, with `memory_task_start`, or from
the reply to it. The reply's `review` is the last round of review:
the verdict, its reason, and the comments written with it, each under an id,
anchored to a `path` and `line` of a `commit` or to the review as a whole. `get_task` has every round under
`reviewRounds`; `tm show` prints them.

Answer every comment with `review_comment_reply` (`tm task reply <task>
<comment-id> --body …`): what you changed for it and where, or why you did
not. Answer it when the change is made, not all at once at the end — the
answer is what the reviewer reads beside the new diff, and a comment left
without one reads as missed. Disagreeing is an answer; silence is not.

The reviewer can answer your answer. They read every round's comments on the
lines of the diff and reply on the comment the conversation is about, which is
seldom the latest round's — so those replies are not in `review`. They are in
the reply's `threads`: each earlier comment whose last word is not yours, with
its round and every reply in order. Treat a reply to your answer as the
comment it continues. Change what it asks and answer again on the same
comment, or answer why not. A thread stays in `threads` until the last word
in it is yours, or until a person resolves it.

A resolved thread is settled: its comment carries `resolved` (who, when) in
`review` and in `get_task`, and it is no longer waiting on you. Resolving is a
person's call and not yours — `tm task resolve` refuses an agent's door with
`HUMAN_DOOR`. Settling remarks on your own work is the self-acceptance a
review exists against. Answer the comment; the reviewer resolves it.

## 3. Claim it — before the first file is opened

`task_start`. Not after the last edit, not "once I know it works".

A hook refuses `Write`/`Edit`/`MultiEdit` for any file **outside the area of
what you hold**, and its refusal says why the claim comes first.

The claim is held by your session — and by your subagent within it — under an
epoch, for the project's claim term (six hours unless the project set another).
Nothing has to be sent to keep it alive: every tm call of the session renews
all of its claims. A claim that runs out puts the task back in the queue with
its checkpoint. Where there is something a successor would want to read,
`task_checkpoint` records it — a note, not a pulse, and not on a timer.

If the session ended — `/clear`, a restart — and the work is still yours,
`task_start` on the same task takes it back under a new epoch; the old session
is told once that its claim was superseded. If an edit is refused because no
claim of the session covers it, `tm claims-lost` says whether the claim ran out
or was taken over by a person (`tm task take-over`) or by another session.

One at a time — and this is the core's answer, not yours to remember: a
second `task_start` by the same session and subagent is refused with
`ACTOR_BUSY`, naming what it already holds. Turning to something else is
`task_hold` on this one first. Work a review sent back is reserved for the
role and held by no session, so it does not count until you take it.

**Read the task's `area` before you start**, because it is the boundary your
edits are checked against. Two things follow:

- A claim refused with `AREA_CONFLICT` means another holder — a session, or a
  sibling subagent of yours — is live in those paths. That is information, not
  an obstacle to get past: `active_claims` says who, and the work is theirs
  until they put it down.
- Finding mid-task that you need a file outside the area is normal — a test, an
  ADR, a spec section. Widen it deliberately with `task_update --area`, listing
  the existing paths too (it replaces the set, not adds to it). What is not
  normal is widening it to take over a file another live claim already covers.

A task with no area declared constrains nothing, which is how everything
planned before areas existed reads. Do not take that as licence to edit the
repository at large under it.

**A task tagged `design` was written against one version of a design**, and the
design may have moved since. The answer to `task_start` says which version the
task records in `metadata.design`, or that it records none. Read the live
version before drawing and compare. If it moved, add to the task what changed
and update `metadata.design` to the version you work from; the handover says
which version the work was checked against.

## 4. Do the work, and write down what changed your mind

Reflections are cheap and the moment does not come back:
`reflection_add --type blocker` when something stops you, `decision` when you
chose between two ways, `replan_trigger` when the plan turns out wrong. See
`tm-memory` for what is worth keeping and what is noise.

If the plan itself is wrong, stop and go to `tm-plan` — `task_replan` exists so
that a wrong plan gets corrected rather than worked around.

## 5. Run this repository's checks, and report what they said

**Before `task_done`, run what covers what you touched.** What that is belongs
to the repository, not to this skill — its `CLAUDE.md` or `AGENTS.md` names
the commands. The rule here is about honesty, not about which command:

- Report what the checks actually said, including failures.
- A suite that was already red before your change is not your result to claim
  either way — say which tests were red on arrival rather than folding them
  into "all green" or into your own breakage.
- A test that only passes from its own directory is red at the root, which is
  the run that counts.

**Then read the diff — a green suite is not a review.** A test says the
behaviour you thought of still holds; it says nothing about the one you did not
think of, and the two are told apart by somebody reading the change. That
somebody may be you: reviewing and accepting are different acts, and only
accepting is barred to the author.

Where a project asks for that pass, it says so in its own policy — the kinds
in `required_deliverable_kinds`, which `get_project` reports and every task
created there inherits — and its handover carries the result as a deliverable
of that kind. What PRODUCES the review is the project's too: `reviewBy`, which
`get_project` reports beside the kinds and `task_done` names on work it sends
to review — read it there, not from a file in the checkout. **What it found is
the deliverable, including when it found nothing** — a pass with no findings is
a result, while the sentence "nothing found" written without the pass is the
failure this whole step exists against.

## 6. Hand it over

`task_done` with `deliverables` — each `{kind, content | reference, label?}`,
carrying exactly one of `content` (inline text) or `reference` (a locator).
Files changed, the commit, one result entry saying what the checks said, and
the review from the checks step where the repository asks for one. A task with
`requiresDeliverable` refuses to complete without them, and one that names
kinds — its own, or the project default it inherited — refuses without one of
each. The refusal names what was wanted beside what was attached, because the
tag is free text and nobody normalises it: `commit` and `git_commit` are two
different answers to the same ask.

**Commit the work before handing it over.** The review opens the changes from
the commits the handover names, so work that is not in the history cannot be
reviewed at all. For a task with an area, both halves are enforced before
`task_done` runs — by `tm task done` itself from the shell, and over MCP by
tm's hook where the harness runs it: uncommitted work in the area refuses the
handover and names the files, and the commits that touched the area since the
claim are added to the deliverables for you — you do not have to copy hashes.
A task with no area gets neither; name its commit yourself.

This moves the task to `in_review`. **You cannot accept it yourself** — that is
the point, not an obstacle: under the project's default `review` mode a person
accepts it, at the console or at their terminal, and an agent is refused
whatever name it gives. The rejection path gets exercised instead of assumed.

Then stop and say what is in review. Do not claim the next thing on the front
without being asked; the queue is now the person's move.

## When it does not go that way

| What happened | Call |
| --- | --- |
| attempted, did not succeed | `task_fail` with a reason — bounded retry decides whether it returns to `pending` or reaches terminal `failed` |
| putting it down without failing it | `task_hold` — releases the claim, keeps the task |
| deliberately abandoning it | `task_cancel` — terminal and visible, cascades to children |
| blocked on somebody else's answer | `task_hold` plus a `blocker` reflection saying what is awaited |
| a handover refused with `NOT_APPROVED` | nothing you can call: a person reads the command the marker brought and runs `tm trust` at their terminal. Say so and stop; an agent's `tm trust` is refused, and getting past that, by a detached process or by writing `~/.tm/trust.json`, takes the person's decision from them |

`task_fail` and `task_hold` are not interchangeable: one records an attempt
that did not work, the other records that nobody is on it. The Agent Fleet view
reads them differently, and so does anybody deciding what to pick up next.
