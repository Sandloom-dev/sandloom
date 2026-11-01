---
name: tm-review
description: >-
  Resolve tasks sitting in `in_review` — read what was handed over, check it
  against what was asked, then accept or send it back with a reason. Use when
  asked what is waiting for review, to accept or reject somebody's work, or
  when a task cannot proceed because its predecessor is still unverified.
---

# Resolving a review

## What is waiting

`awaiting_reviews` — tasks in `in_review`, where `task_done` put them. A task
sits there until somebody who is **not** its submitter resolves it.

This is also the answer when a slice you wanted to start comes back blocked:
its predecessor is done but not verified, and the DAG waits for completion, not
for submission.

## An agent sends work back; a person accepts it

Who may accept is the project's `review` mode (`get_project`). Under `human`,
the default, a person accepts — at the console, or at a terminal outside any
agent's session — and not one who claimed or submitted the task. An agent
never accepts there, under whatever name: the refusal is `HUMAN_ACCEPTS`,
because what it reads is the door the call came through, not the name on it.
Under `solo` anybody accepts, and an acceptance of one's own work is marked.

So the rule answers for you before the call: the project's mode is in
`get_project`, the task's authors are in `get_task`. Under `human` your
acceptance would not pass, so your part is the reading and, where it falls
short, the rejection with findings — sending work back is open to anybody, the
author included. Say what you found, and leave the acceptance to a person.

The failure to avoid here is quiet: changing the mode to get past the refusal
would remove the only thing that makes the queue mean anything. So an agent's
switch to `solo` is refused too (`HUMAN_DOOR`), and so is cancelling or
deleting a task in review. If the project genuinely needs `solo`, that is the
person's call, made at their terminal, and worth saying out loud — not a step
on the way to something else.

## Reading the submission

`get_task` for the task and its deliverables. Read three things, in this order:

1. **What was asked** — the task's own `description`, not your memory of the
   conversation that produced it.
2. **What was handed over** — the deliverables. A `reference` is a locator, so
   go and look at what it points at; a completion whose deliverables do not
   name anything checkable is itself a finding.
3. **What the submitter said about the checks.** "All green" with no output
   quoted is not evidence, and neither is a suite that was already red.

On a task tagged `design`, the handover says which version of the design the
work was checked against. If it does not, that is a finding: without it "did
not look" and "the design moved after" read the same.

Reflections on the task (`memory_task_start`'s `reflections`) carry what went sideways during
the work — a `blocker` or a `decision` there often explains a choice that looks
arbitrary in the diff.

Work that comes back from an earlier rejection has `reviewRounds` on
`get_task`: what each round said, comment by comment, and the submitter's
answers under each (`tm show` prints the same). Read the new submission
against them — a comment with no answer, or an answer the diff does not bear
out, is the first thing to check.

A comment whose `resolved` is set was settled by a person; it is not a
finding to raise again. Resolving a thread — or reopening one — is a
person's call even when you are the reviewer: `tm task resolve` refuses an
agent's door. When an answer settles your comment, say so in the verdict's
`reason`, and the person resolves it.

## The verdict

`task_verify` with `accept: true` completes the task and fires its `on_success`
template — whatever that spawns appears in the reply, so read it rather than
assuming nothing happened.

A task that follows another under `allSubmitted` can reach review before the
work it rests on is accepted. Accepting it then is refused
(`PREDECESSOR_NOT_ACCEPTED`, naming the predecessor; `acceptAfter` on
`awaiting_reviews`, and `tm task admission`, say so before the call): review
the predecessor first. Sending it back is never held up.

`accept: false` sends it back to `pending` for rework. The `reason` is the
whole value of a rejection: it is what the next attempt reads instead of
guessing. Name the specific thing that is wrong and what would make it right —
a rejection that says "does not look finished" costs a full sitting to
interpret and usually gets the same submission back.

Say it where it is. `comments` on `task_verify` (`--comment
'<path>:<line>=<text>'` at the CLI) puts each finding on the line it is about,
under an id the submitter answers; a comment with no `path` is on the review
as a whole. The `reason` stays the one-line summary.

Reject on substance, not on style you would have written differently. The
question is whether it does what the task asked, not whether it reads the way
you would have written it.

## After

Say which tasks you resolved and how, and what the acceptance unblocked — an
accepted predecessor usually puts something new on the front, and the person
deciding what happens next needs to know that before they ask.
