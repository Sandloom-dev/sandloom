---
name: tm-plan
description: >-
  Create tasks in tm from an instruction — "create tasks", "add tasks", "plan
  this", "break it down", "make a roadmap", or any instruction bigger than one
  sitting: the feature as a root task, slices as its children, each with an
  area and an alias, blocking links between them. Read this before the first
  task_create, even when tm's tools are already in view; also the way to
  correct a plan that turned out wrong, with replan or rollback.
---

# Planning into the tree

## The two relations are different, and mixing them is the usual mistake

- **Parent/child is the WBS tree** — containment. "This slice is part of that
  feature." Set with `parent` on `task_create`, moved with `task_move`.
- **A link is a blocking dependency** — order. "This cannot start until that
  finishes." Added with `link_add`, and it is a separate graph (`get_graph`,
  not `get_tree`).

Siblings under one feature are *not* ordered by being siblings. If the third
slice needs the first one's output, that is a `link_add`, or the front will
offer all three at once and be right to.

`link_add` direction is easy to get backwards: `from` is the prerequisite,
`to` is the work that waits.

What "finishes" means is the waiting task's `triggerRule`. The default,
`allCompleted`, holds it until the prerequisite is accepted — where a person
accepts, that can be hours after the code was committed. When the next slice
only needs the first one's code, give it `allSubmitted`: it starts once the
prerequisite is in review with a `git_commit` in its handover. An agent may
set it. The person's acceptance still closes the chain in order: the waiting
task is not accepted before its prerequisite (`PREDECESSOR_NOT_ACCEPTED`), and
if the prerequisite is sent back, the waiting task carries
`predecessorsSentBack` in `get_task` and `task_start` until it is handed over
again.

## Shape

- **A feature is a root task** — `task_create` with no `parent`. What would
  have been a PRD is that task's `description`; write it there rather than in a
  file beside the tree.
- **A slice is a child** — one sitting of work with an observable result. If
  you cannot say what `task_done` would hand over, it is not a slice yet.
- **A slice names its `area`** — the paths it speaks for. See below; it is the
  one field where leaving the default costs somebody else something.
- **Give everything an `alias`.** Ids are UUIDv7. The alias is what you can
  type, what a later call resolves, and what a commit message can name.
- **One project per repository.** Work smeared across three projects of one
  repository is not visible whole on any screen — the active front, the human
  queue and the header counts are all about one open project.

## Before writing the plan, read what the project already knows

`get_tree` for what exists — a slice that duplicates a pending task is worse
than no slice. `kg_search` and `memory_retrieve` for decisions already taken;
a plan that re-opens a settled question wastes the sitting that settles it
again.

What you found is worth more than the plan's first sitting: link each entity a
slice will rest on as one it consumes. Naming it in the slice's description is
enough — `kg «<its name>»` or `kg <its id>` becomes that link when the task is
written; `kg_task_link` with `refType: consumes` does the same for one you
would rather not name. A claim hands what its task consumes over first and
never cuts it, so the agent who takes the slice reads the decision you just
read instead of finding it again — or not finding it.

## The area is the planner's job, and nobody else can do it

`area` is the set of paths a task speaks for — repo-relative prefixes, where an
entry covers itself and everything under it:

```
area: ["lib/parser/src/tokens", "guide/format.md"]
```

Three things read it. The claim guard refuses an edit outside the area of what
you hold. `task_start` refuses a claim whose area overlaps a live claim of
somebody else. And a rollback finally has a definite answer to "what do I throw
away".

**Write it while you are writing the slice.** By the time an agent is claiming
the task, the boundary is a guess by the one person with a reason to draw it
generously; while you are planning, it is the same thought as "what is this
slice" and costs nothing extra.

**A shared file is not a dependency.** Two slices that need the same file ask
one question: does the second need what the first produces? Then it is order,
and a `link_add`. If they only touch the same file, the area already keeps
them apart — `task_start` refuses the second with `AREA_CONFLICT` while the
first is claimed, and `task_done` releases the claim. A link added for that is
stricter by a whole review: the waiting slice stays off the front until a
person has accepted the first (until it is handed over, under
`allSubmitted`), and the graph tells its next reader about a
dependency that does not exist. Seen: seven slices linked through nothing but
one shared specification stood still from the moment the first went to
review. Either way the two will not run side by side, which is worth knowing
before counting them as parallel work.

**Include what the work will actually touch**, not just its centre: the test
beside the code, the decision record it will write, the specification if it
changes the contract. An area that refuses half the ordinary moves gets widened in
irritation, and a guard people route around is worse than no guard.

Omitting it is allowed and means "not declared" — the task then constrains
nothing, and the guard passes any edit under its claim. That is the migration
path for tasks planned before areas existed, not a default to lean on.

## A slice that needs a one-of-a-kind resource says so

An area divides agents by **file**. Some things do not divide at all: a running
desktop window, a driver suite, a port, a device. Two slices with no file in
common still collide over one of those, and the collision does not read as a
collision — it reads as "my change broke it".

Such a slice carries the resource as a `requiredCapabilities` entry, and the
orchestrator hands that capability to exactly one agent in a run. Everyone else
stops seeing the slice on their front, and `task_start` refuses it even by
alias (`CAPABILITY_MISMATCH`).

**What the entry is CALLED is a fact about the project, and this skill does not
know it.** Look it up before writing one: the project's `CLAUDE.md` or
`AGENTS.md` names the resource it has, and `list_tasks` shows what its existing
tasks already require.
If neither says anything, the project has no such resource — ask rather than
invent, because an invented name is not a second lock, it is a second name for
one, and the two never meet.

**A name is not portable, and reading one here as if it were costs a
project.** These skills are installed into every repository that tracks work in
tm, so an example written here reads as an instruction there: a neighbouring
project ends up carrying tasks labelled with another repository's resource — a
resource that does not exist in it, under a name it has already chosen
differently. Nothing is protected; work is serialised for no reason.

Two rules decide who gets the label, and both are about cost — a label
serialises work, so a careless one is a stop-work:

- **Label the check, not the directory.** The question is whether finishing the
  slice needs the live run or the driver, not whether the slice happens to be
  under the app. Work that widget tests and an audit close out gets no label.
- **Label the leaf, not the container.** Leaves are what get claimed. A label on
  a container stops nobody and only hides it from the front of an agent who
  lacks it.

**The core reads capabilities off the actor's record**, not off the call, so a
label on a task is a real gate: an agent whose record lacks the entry cannot
claim it. What the core still cannot tell is whether the RESOURCE is free —
that the window is actually open, that nobody else is driving it. So the label
routes co-operating agents; exclusivity holds because the capability was
**granted to one record**, and granting it to two is what breaks it.

That has a consequence worth knowing before you rely on it: there is an issuer
only when somebody is orchestrating. Two independent sessions have nobody
handing anything out, so between them the entry is a convention rather than a
lock. A project that has decided such a resource exists will have written the
decision down; if you cannot find where, that is a question for the project and
not a gap to fill from here.

## Defaults worth setting deliberately

`task_create` gives everything a working default, so a minimal call is enough
to start. Three defaults are worth a thought rather than a shrug:

- **`requiresReview` is true, and where a person accepts it stays true.** A
  twelve-slice plan is therefore twelve acceptances. Under the project's
  default `review: human`, only a person takes a review off: an agent's
  `requiresReview: false` is refused with `HUMAN_DOOR`, on `task_create`, on
  `task_update` and inside an `onSuccess` template. If some slices are
  mechanical steps of one whole and should not each wait for an acceptance,
  say so and leave the decision to the person at their terminal. Do not plan
  "the review on the parent" instead: a parent with children is a container,
  its status comes from them, and it never goes to review itself.
- **`requiredCapabilities`** keeps a slice off the front of anyone who cannot
  do it, which is how "ready for a human" is expressed without a tag.
- **`requiresDeliverable`** on anything whose result is a file or a commit, so
  a completion cannot be claimed empty.
- **`metadata.design`** on a slice tagged `design`: `{file, version, readAt}`,
  the version of the design the slice was written against. A design moves
  after the plan does, and nothing else on the task can say so. When the slice
  is claimed, a hook reads this back to the agent and asks it to compare with
  the live design before drawing; without it the agent is told only that no
  version was recorded. How to read a design's version is the project's to
  say — its `CLAUDE.md` or `AGENTS.md`.

## When the plan turns out wrong

That is expected; the tree is not a contract with the past.

- `task_replan` restructures a subtree — moves and re-parenting in one
  operation, recorded as a replan rather than as a series of unexplained edits.
- `rollback_plan` restores a subtree to how it was, when the replan itself was
  the mistake. The two are neighbours and are easy to confuse: one changes the
  shape forward, the other undoes a change to it.
- `reflection_add --type replan_trigger` on the task that revealed it, before
  either — otherwise the next person reads a tree that changed shape for no
  recorded reason.

Cancel with `task_cancel`; deleting is a person's `tm task delete`. Cancelling
is terminal *and* visible, so the decision survives in the tree. Deleting
removes the evidence that anybody ever thought it was a good idea. Neither is
an agent's to use on a
task waiting for acceptance, and neither is a replan or a rollback that would
remove one: under `review: human` they are refused (`HUMAN_DOOR`), because
withdrawing a review is the person's decision.

## Handing the plan over

Say what the root task is, what the slices are, and which one the front will
offer first. Then stop — picking the first slice up is `tm-implement`, and it
is a separate decision the person may want to make differently.
