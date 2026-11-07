---
name: tm
description: >-
  Fallback for work tracked in `tm`, only when no other tm-* skill fits the
  request: tm-plan creates tasks from an instruction, tm-implement picks work
  up, tm-review accepts or rejects it, tm-fleet runs several agents, tm-memory
  keeps what was learned. Carries only what belongs to none of them — reaching
  tm over MCP or the CLI, who the actor is, one claim at a time, why a rebuilt
  server does not reach a running session.
---

# Work tracked in `tm`

`tm` holds this repository's plan and its state. A feature is a root task, a
slice is its child, and the WBS tree **is** the plan — there is no markdown
tracker and no harness todo list running alongside it to be kept in step.

Each phase has its own skill. They fire on the situation; reading them ahead of
time is not how they work.

| About to | Skill |
| --- | --- |
| turn an instruction into a plan | `tm-plan` |
| pick work up and do it | `tm-implement` |
| resolve somebody else's `in_review` | `tm-review` |
| run several agents at once over the front | `tm-fleet` |
| record something worth keeping, or find why a decision was made | `tm-memory` |
| set a repository up for tm at all | `tm-setup` |

## The operations document themselves — read them, do not guess

Every operation carries its own summary and one line per argument, and a test
rejects a description that says no more than the name already did. The accepted
values of a closed set are listed; an optional argument states what omitting it
does. So "what does this do, and what happens if I leave that out" is answered
by the tool schema, at the moment you reach for it.

**That is why there is no skill per operation.** Anything written here about a
single call would be a second copy of prose that is already tested against the
operation table, in a place where nothing would notice it going stale. What
follows is only what a description structurally cannot carry: which calls
follow which, when to start at all, and the rules that live outside the core.

## Reaching tm: MCP by default, the CLI as the fallback

If `mcp__tm__*` tools are listed, use them — a hook holds that default, and
a `tm <verb>` in a shell call is refused naming the tool that answers it.
Without a server behind the session, the `tm` CLI dispatches the *same*
operation table (`tm help` lists its verbs); `--json` makes an answer
machine-readable, and a non-zero exit is the failure category (2 validation,
3 not found, 4 conflict, 5 precondition).

### The project is resolved, not pasted

Project-scoped calls take a project, by id or by name. On the CLI you rarely
pass it: it comes from `--project`, else `$TM_PROJECT`, else `.tm/project.yaml`
found by walking up from the working directory. That file is this repository's
committed statement of which project it is, and it is what stops the CLI and
the claim hook from each guessing separately. Write it with
`tm project use <id|name>`; read what currently resolves with `tm project use`
and no argument.

Over MCP there is no such resolution — pass the project, its id or its name,
and take it from the marker if you do not have it.

### `--actor` is not cosmetic, and it works differently on each surface

Who took part in a task — and so may not accept it — is read off the actors
on its claims and submissions, so the actor is not cosmetic. An actor is a
**role**: the program of the harness (`claude`, `opencode`, `pi`, `codex`,
`agy`) or a name given on purpose, and any number of sessions sign as one. A
session is not leased a role; what a session holds is its claims. Over MCP, a
server signs as its `--actor`, else as the program of its harness. On the CLI,
a call in a harness session signs as that harness's role, and `--actor <name>`
names another, for a script, a fixture, a person at a terminal, or a second
role of one harness.

Every event also records its **door**, which the caller does not choose:
`agent` for anything a harness runs, `terminal` for the CLI with a terminal on
stdin and no harness around it, `console`, and `script` for the rest. A person
at a terminal signs as their login, and their first write in a project enters
them into its registry as a person. An agent shell has no terminal, so taking
the harness variables out of it makes a script, not a person. A name the
project registers as a person is refused from an agent's session
(`HUMAN_DOOR`).

The claims a session holds are its own: the session a claim belongs to is the
one the harness's hook states in the call. `tm claims-lost` names the claims a
session held and lost — granted to another session, taken over, run out — and
the claim guard says the same when an edit meets none of the session's claims.
`tm log` shows who signed each event and through which door.

Agents left on a shared role are one person as far as the core is concerned,
and cannot review each other's work.

## The rules that belong to no single call

- **One claim at a time per holder, and the core refuses the second**
  (`ACTOR_BUSY`). A holder is a session, or a subagent within it; each subagent
  holds a claim of its own, named by the hook — `tm-fleet` says how. Turning to
  something else means `task_hold` on what you hold, not a second `task_start`:
  two live claims mean one of them is lying to the Agent Fleet view about work
  nobody is doing. The boundary is the project, so working in two checkouts at
  once is unaffected. Review does not keep you busy: `task_done` releases the
  claim, so the next task is claimable while a person reads the last one.
- **The claim comes before the first edit** — a hook refuses an edit no claim
  of the session covers, and says why. A claim has an epoch and a term (the
  project's `claimTtl`, six hours unless a person set another) that every tm
  call of the session renews, so keeping it alive is nobody's chore; one that
  runs out puts the task back in the queue with its checkpoint. After `/clear`
  or a restart the new session takes its work back with `task_start` on the
  same task, and the old session is told once that it was superseded. A person
  can take a claim over (`tm task take-over`, with a reason), and work sent back
  by a review waits for its role.
- **An agent submits; a person accepts.** Under a project's default `review`
  mode, `human`, `task_verify` accepts only from a person at the console or a
  terminal outside an agent's session, and never from anybody who claimed or
  submitted the task — an author accepts their own only with `ownWork` and a
  reason. An agent is refused with `HUMAN_ACCEPTS` whatever name it gives; it
  may send work back with findings. At a terminal, `tm task admission <task>`
  answers before the call.
- **Nor does an agent take away what makes work wait for a person.** Under
  `human`, these are refused from an agent's session with `HUMAN_DOOR`, and
  the person makes them at their terminal:
  - `requiresReview: false`, on a task or inside an `onSuccess` template, and
    a task — or a template — naming fewer kinds than the project's default;
  - a switch to `solo`, a change of the claim term, the review term or the
    planning limit or the limit of attempts, a kind taken off the project's deliverable default, a path
    added to `unguardedPaths`;
  - entering, changing or retiring a person, and the capabilities of any
    actor;
  - cancelling, deleting, replanning or rolling back a task in review,
    removing a link out of one (or out of its parent), and switching a task
    it holds back to `oneCompleted`.
  Tightening passes any door. Making a project is a person's whatever the
  mode: ask them to run `tm project create` at their terminal. What a
  refusal cannot stop is counted instead:
  `tm guards` lists own work accepted, work closed with no review,
  the policy loosened and the doors verdicts came through, and the console's
  guards draw the same.
- **The server is a compiled binary, and a rebuild does not reach a running
  session.** This matters only where the checkout you are in is the one tm is
  built from: change a package the server is compiled out of, recompile *and ask
  the person to restart the session* — the agent inside the old one goes on
  talking to the old server. The symptom that catches this late is a tool
  answering the old way, or missing entirely, while the code plainly has it.
- **Where tm is what you are working on, an awkward surface is a bug report.**
  It arrives with its own reproduction, and a way around it does not get chosen —
  it gets fixed. Everywhere else this reads the other way: tm is somebody else's
  tool, and what you owe it is the report, not the fix. Which of the two you are
  in, the repository says; this skill cannot.

## When an answer does not match the documentation

Suspect the stale binary first — compare the operations you can actually see
against the operation table in the source. Five of them missing is what a
server compiled before the last change looks like from the inside.
