# The fleet under Claude Code

What `tm-fleet` leaves to the harness, for Claude Code. Read it before you
dispatch an agent with `isolation: worktree`, and when you write the brief of
agents that run in auto mode.

## Where an isolated agent's work is

It is in `.claude/worktrees/<name>/` on branch `worktree-<name>`, so verifying
and committing read it there (`git -C .claude/worktrees/<name> status`).
Claude Code blocks an isolated agent from editing the main checkout,
`cd`-ing into it, or redirecting git at it, so the emptiness of your own
`git diff` is enforced rather than hoped for.

Two settings hold this up, and `tm setup --harness claude-code` offers to
write both (`--no-worktree` declines, `tm doctor` notes them missing):
`worktree.baseRef: "head"` (the default `"fresh"` branches from the default
branch, so on a feature branch the agent silently gets a tree without the work
it was sent to extend) and `.claude/worktrees/` in `.gitignore`. Where setup
was declined or run before this, check that the repository has them.

A worktree that outlives the run: `git worktree remove .claude/worktrees/<name>`
once its work is in. The periodic sweep skips anything still holding changes,
and neither it nor `prune` deletes the `worktree-*` branch.

## An isolated agent's shell, said in its brief, with the reason

Claude Code refuses a command in a worktree when it cannot prove the command
stays there, and what it cannot prove is mostly shape: a `cd`, `-C`, a loop, a
variable or a `$(…)` next to git. The agent's own system prompt pushes it into
that shape — it says the working directory is reset between Bash calls and
asks for absolute paths, which a model reads as "`cd` to the worktree first,
every time" (59% of the shell commands of one fleet's agents began that way,
and about a fifth of those compound commands were refused). The directory it
is reset to IS the worktree, so the brief says so: the working directory is
your worktree and comes back to it, so that line does not ask you to `cd`;
no `cd`, no `-C`; one plain git command per call. Without the reason the agent
follows its system prompt over your brief.

## Tools and auto mode

- **Load the tm tools before the first call, by name**, and again after a
  compaction: one `ToolSearch` with `select:` and the tools the agent will
  call (`mcp__tm__task_start,mcp__tm__get_task,mcp__tm__task_done,…`). A
  subagent that calls a deferred tool without its schema guesses the
  arguments — `id` for `task`, `summary` for `deliverables` — and is refused:
  197 of 2 222 such calls in one fleet run were, and none of 8 325 calls made
  after a load. Write the line out; "load the tools" alone was followed by
  fewer than half.
- **"No verdict" is not a refusal.** In auto mode a call can come back with
  "the server-side auto mode classifier gave no verdict": a hook changed it or
  a call beside it after the review. That is not a judgement on the call; the
  agent issues that one call again, alone in its response, exactly as the
  message quotes it. Only a call "denied" is a stop. A brief that says "the
  classifier blocked it — stop and report" turns every such retry into a
  stopped agent. tm's own hook changes no call under Claude Code: it leaves
  the call's session and subagent beside it, which the server takes by the
  call's id, so a tm write may share a response with other calls.
