---
name: tm-fleet
description: >-
  Run several agents at once over the active front and bring the results back
  in — select what may be handed out, divide them by claim area, issue the
  one-of-a-kind resources, then verify what each agent committed. Use
  when asked to parallelise work, run a fleet or a batch of agents, or take
  several tasks in one sitting.
---

# Running a fleet over the front

Launching agents is the easy part. What this skill is actually about is **what
must not be handed out**, and the order that keeps three agents in one checkout
from becoming one unpickable diff.

You are the orchestrator. That is a role with work in it: selection, division,
verification, history. None of it transfers to the agents.

## 1. Select — only what somebody has already decided

Take from `get_active_front`, and hand out only tasks tagged `ready-for-agent`.

**A `needs-triage` task is a decision, and a decision is the person's.** One of
them here framed its own choice backwards — the option the ticket called cheaper
could not be built at all — and finding that out was worth a conversation, not
an agent's best guess.

A task nobody has written down yet does not become an exception. **Create the
task before launching the agent**, including for research: an agent with nothing
to claim cannot edit a file, and the correct ones stop rather than route around
the guard. That has already happened here — a background agent could not write
its own findings.

## 2. Divide by area, not by prompt

The task's `area` is the set of paths it speaks for. It is what the claim guard
checks on every edit and what `task_start` refuses an overlap against, so the
division is data, verifiable before anybody starts.

- Read the areas of the candidate tasks. **Overlap means they do not run in
  parallel** — not "carefully", not "with a warning in the prompt".
- A task with no area declared is not ready to be handed out. Give it one
  (`task_update --area`) or leave it for a sitting of your own; an agent under
  an area-less claim is an agent the guard passes for the whole repository.
- Do not restate the boundary as prose in the prompt and leave it at that. The
  hook catches a stray edit as it happens; a paragraph catches it in a diff
  review afterwards, if at all.

## 3. Each agent is named by the hook

Subagents you launch share this session's MCP connection and sign as the role
the session signs as. They need no name and no argument: the hook puts the
session and the subagent (`<type>#<id>`, shown as the type and the last four
characters of the id) into every tm call they make, so a claim is held by the
pair, and a subagent that runs a second `task_start` is refused `ACTOR_BUSY`
only for its own second claim, never for its sibling's. Nothing is registered,
passed or cleaned up: do not tell an agent to carry a name.

- A claim an agent took stays until somebody puts it down or its term runs out
  (six hours, renewed by every tm call of the session), so the agents hand over
  or `task_hold` before they return. An agent that comes back to a task after a
  `/clear` or a restart takes it again with `task_start`.
- `AREA_CONFLICT` keeps holders apart, subagents of one session included: the
  second subagent whose area overlaps a sibling's claim is refused at its
  claim. Edits are checked differently: the guard asks whether any claim of the
  session covers the path, not which subagent is typing, so inside the session
  a subagent could still edit a sibling's files. Dividing by area at the claim
  is what stops it.

## 4. Issue the resources that exist once

Some things do not divide: a running window, a driver port, a device, a live
server. Two agents on one of those produce timeouts that read as *my change
broke it* — an hour, the first time.

Those tasks carry the resource as an **`@<resource>` entry in their area**, and
`task_start` is the lock: a second claim naming it is refused with
`AREA_CONFLICT`, naming who holds it. Nothing is granted to a record and
there is nothing to take back when the run ends — the resource is free again
when the holder's task is handed over or held.

The lock is between holders, and your subagents are holders of their own, you
being one too: a second claim naming the resource is refused whichever session
or subagent holds the first. **You are the issuer inside the run**: dispatch one
agent with a task naming the resource at a time, and the next after its task is
handed over or put down, so that the refusal never has to be what tells an
agent to wait.

Which entries this project uses, you read rather than remember: `list_tasks`
and `get_task` carry the `area`, and the project's `CLAUDE.md` or `AGENTS.md`
says what the resource behind a name actually is. A name nobody spells the same
way twice is two locks that never meet — check an entry against the ones the
existing tasks use before dispatching.

An agent without the resource does not skip the check — it returns **what you
should look at**, in a list. That is not a formality: such a list can name a
real risk nobody has asked about.

## 5. Isolate the checkout where it is cheap, and only there

`isolation: worktree` is a **per-agent** decision, not a property of an agent
definition, because what it depends on is the task's area.

**Decide it by what a fresh checkout of that area COSTS, not by where the area
sits.** A worktree is a full second copy: the agent has to rebuild whatever the
area's tests need before it can run one. So the question is how much of that
there is — dependencies to fetch, native artefacts to compile, an application
bundle to relink — and the answer is a property of the code, not of a directory
name. A rule written as "everything under this folder, but not that one" is a
rule that silently does nothing in the next repository, and does the wrong thing
in one that reorganised.

Measured here, to show the spread rather than to be copied: a slice of pure
library code cost 66 MB and a dependency fetch, while a slice touching the
desktop application cost 1.1 GB and a full platform bundle rebuild — sixteen
times, and the second is paid before the first test runs. Your own numbers will
differ; that they differ by that much is the part that generalises.

**Measured once, and the shared tree was enough.** Four agents on four
documentation slices with disjoint areas, each a subagent of its own, two of them
isolated and two in the shared checkout: the shared pair finished in 4:46 and
8:10, the isolated pair in 5:37 and 10:17, and neither shared agent saw the
other's work in its `git status` or in its tests. The isolated pair paid what
the paragraph above predicts — one package with a native build hook spent
about seventy seconds compiling it before its first test — and paid in
handling: git beyond a single plain command was refused inside the worktree,
and the work had to be carried into the checkout by hand before it could be
committed. So with disjoint areas and a subagent per slice, the claim guard already
gives what a worktree was meant to. Isolate an agent when its checks would
collide with another's — the same package's tests, a shared build directory —
not by default.

**tm says when two agents share a package.** `task_start` answers
with `sharedPackages` and a `worktreeHint` when another holder of the same
session has a live claim in a package its area reaches — the nearest
`pubspec.yaml` above a path, a workspace root passed over. Areas apart do not
keep a package's compilation, generators or formatter apart: in one run an
agent's uncommitted edit broke the compilation of another's tests, a
`build_runner --build-filter` deleted a neighbour's `*.freezed.dart`, and a
`dart format` passed over a neighbour's files. It is a hint, not a move: the
decision stays yours, by the cost above. So divide the front with it in mind
— two slices of one package dispatched together are the case it names — and
when an agent reports the hint, decide then: put the next one of that package
in a worktree, or keep it in the tree with generators and formatters run on
its own files only.

**What a worktree is not.** Not a boundary: the area and the claim guard are
that, and they go on working inside it. Not a fix for a resource that exists
once — one window, one port, one device is not divided by a filesystem (see
"Issue the resources that exist once"). It buys two things and no others:
nobody else's test runs in your tree, and nobody else's edits in your
`git status`.

**The work does not land in your tree.** It is in `.claude/worktrees/<name>/`
on branch `worktree-<name>`, so verifying and committing read it there
(`git -C .claude/worktrees/<name> status`). A `git diff` in your own checkout
showing nothing is not the agent having done nothing — Claude Code blocks an
isolated agent from editing the main checkout, `cd`-ing into it, or redirecting
git at it, so that emptiness is enforced rather than hoped for.

Two settings hold this up, both already in the repository: `worktree.baseRef:
"head"` (the default `"fresh"` branches from the default branch, so on a
feature branch the agent silently gets a tree without the work it was sent to
extend) and `.claude/worktrees/` in `.gitignore`. And two things leak anyway,
worth knowing before rather than after: the stash is **shared** across
worktrees, and `git worktree prune` clears metadata but never branches, so
abandoned `worktree-*` branches pile up.

## 6. Brief each agent with what has already been paid for

**The rules every agent of the fleet follows are a file of the project:**
`.tm/fleet-brief.md` beside `.tm/project.yaml`, with `{FORK}` wherever the
agent's own name goes. `task_start` called by a subagent answers with it as
`brief`, the name (`<type>#<last four>`) put in, read from the file at each
claim; a call that is not a subagent's is handed none, and a project without
the file hands none. So the prompt you write carries only what is particular to the
agent — the task and the resource it was issued — and the shared rules are
changed in the file, in a commit, not in a copy you keep and substitute per
agent.

Without these, an agent repeats a mistake this repository has already bought:

- **Show the test biting.** A new test that has never failed proves nothing.
- **A probe that did not break the test is a finding about the TEST**, not a
  confirmation of the code. Print what the test measures before concluding
  anything.
- **Run the checks the way the project documents them**, and from where it says.
  A suite that only passes when invoked from somewhere else is red where it
  counts — and which "somewhere" that is can differ per package, so it is the
  project's `CLAUDE.md` or `AGENTS.md` that settles it rather than a habit
  carried in.
- **Name what was already red on arrival**, separately, rather than folding it
  into either "all green" or their own breakage.
- **Walk the call path first and name the line** where the ticket's claim
  happens. Tickets here have twice been right in the observation and wrong in
  the scenario.

**Agents write to each other through the tracker, not through you.** An
agent that changed something a neighbour depends on, or found the
neighbour's bug, leaves a reflection on the neighbour's task:
`active_claims` with `covering: <path>` names the task and its executor,
and `reflection_add` with that `task` writes the note.
The holder is handed it on whatever tm call it makes next, beside the
result under `notes`: one `{id, task, author, line}` per note other actors
wrote on its task since it claimed it, each once, the line being the
note's first — so a note's first line has to stand on its own.
`memory_task_start` on the task reads one whole. A note written before the
claim is not among them: `task_start` answers with it in full. You write to
an agent the same way; the brief is where to say that `notes` are read
before the next edit.

## 7. Verify — reading the report is not verification

For each agent that comes back: re-read the diff, re-run the checks yourself.
Confirmation is part of the procedure, not a courtesy.

Where the repository runs its checks at the handover (a hook on `task_done`,
see below), the handover already carries a `checks` deliverable: which checks
covered the area, against which commit, and what each said. That record was
run, not retold, so it is your re-run for those checks — read it rather than
repeating it. What it names as failed, not run, or run against a tree busy with
someone else's work is what you still run yourself, along with whatever needs
a resource that exists once, which such a hook names and never runs.

`git status` before the run as well, or a dirty tree afterwards cannot be told
from what an agent did. After each one, `git diff --stat -- <its area>` plus a
look at whether anything landed outside it. For an isolated agent both of those
run against its worktree instead of your checkout, as the isolation section
says.

## 8. The agent commits its area, then hands over; you verify after

**Each agent commits its own area before `task_done`**, with a message that
names its task, and the handover carries that commit. The review opens the
changes from the commits of the handover, so the order is not a preference: a
hook refuses a handover while the task's area has uncommitted work, and adds
the commits that touched the area since the claim to the deliverables. A run
where agents handed over and the orchestrator committed after put four tasks
in review whose changes could not be opened.

Where the repository has a hook that runs the checks covering the area at
`task_done`, tell the agents what it does, because it looks like a refusal.
The first `task_done` is refused once, with a `{"kind": "checks", …}`
deliverable in the reason: call `task_done` again with the same arguments and
that deliverable appended, unchanged, and without editing anything in between
— a new commit is a new run. A red or unrun outcome does not hold the handover
back; it is recorded for whoever accepts. Handovers of one checkout run their
checks one at a time, so in a wave the later ones wait, and one that waited
past its limit is recorded as not run rather than run short.

Tell the agents to stage their area by path (`git add <area>`), never
`git add -A`: in a shared tree that would take a sibling's files. An isolated
agent commits on its worktree branch and **merges that branch into the shared
one itself, before `task_done`** — a merge, never a cherry-pick, because a
cherry-pick gives the work a new hash and the handover names the commit it
made. Its handover then names commits the shared branch has, as a shared-tree
agent's does. Where its harness refuses it git at the main checkout, it says
so and stops short of `task_done`; you merge, and it hands over after.

Your verification comes after, and it has teeth: work that falls short is
sent back with findings, and a commit that should not stand is reverted. Commit
early is still the point — the first agent of a run finishes minutes before
the last, and what it leaves uncommitted meanwhile shows up in everybody
else's `git status`.

Acceptance is not yours, though, and not theirs: the agent submits with
`task_done`, and a person accepts — at the console or at their terminal. Under
the default `review` mode no agent accepts another's work, whatever name each
holds, so a reviewing agent's part is the reading and, where it falls short,
sending the work back with findings.

A wave need not wait for that acceptance. A successor under `allSubmitted`
is on the front as soon as its predecessor is in review with a commit in its
handover, so the next agent can start on committed work; set the rule when you
plan the wave, not after it stalls. The acceptances still go in order — a
successor is refused (`PREDECESSOR_NOT_ACCEPTED`) until its predecessor is
accepted — and a predecessor sent back marks the successor with
`predecessorsSentBack`: tell its agent before it builds further on that
ground.

## When it does not go that way

| What happened | What to do |
| --- | --- |
| an agent edited outside its area | the guard should have refused it — check the task's area is actually set, then `git checkout -- <the stray paths>` |
| an agent failed the task | `task_fail` with the reason, and read whether the plan was wrong before relaunching |
| two candidate tasks overlap in area | run one; the other waits — or `tm-plan` merges them, because they were one slice |
| a task needs a one-of-a-kind resource and it is taken | its `task_start` is refused with `AREA_CONFLICT`, naming the holder; it waits until that task is handed over or held |
| rollback is needed | `git revert` the task's commit — it touches the task's area and nothing else, which is correct only because the boundary held, so it is the area that made rollback definable |
| an agent asks for a decision | answer it yourself or give it back to the person; do not let it choose and report the choice as done |
| an isolated agent's worktree outlives the run | `git worktree remove .claude/worktrees/<name>` once its work is in — the periodic sweep skips anything still holding changes, and neither it nor `prune` ever deletes the branch |
