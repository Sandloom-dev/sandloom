# Fleet brief (Sandloom)

You are {FORK}. tm hands you this brief at `task_start`; your prompt carries
only your task and what is particular to it. The working rules (isolation,
reading, checks, the "Before the PR" checklist) are in your agent file.

## Branch and PR
- You start in your own worktree (the agent file sets `isolation: worktree`):
  every command below runs there. Never `git worktree add`, never a branch
  switch or a commit in the main checkout.
- A new task: `git fetch origin`, `git reset --hard origin/main` (or the base
  your prompt names), `git checkout -b claude/<alias>`: each its own call.
- A review round or a resumed checkpoint (the branch exists): never reset.
  `git fetch origin`, `git checkout -B claude/<alias> origin/claude/<alias>`
  (or the branch your prompt names), then merge `origin/main` if it moved.
  If git answers that the branch is used by another worktree, work detached:
  `git checkout --detach origin/claude/<alias>`, and push with
  `git push origin HEAD:claude/<alias>`.
- A base that moves is merged in; never rebase or force-push.
- Your changelog entry is your own file `changelog.d/<alias>.md`
  (`changelog.d/<package>/<alias>.md` for a package's own changelog), in
  your task area; never edit `CHANGELOG.md` (the release folds the fragments).
  `tools/packaging/public-source.json` merges by itself once the clone has run
  `python3 tools/packaging/merge_public_source.py --install` (worktrees share
  the clone's config; `git config merge.public-source.driver` shows it). A
  conflict left in it is real: keep both sides.
- Shared registries are unguarded in tm:
  `tools/packaging/public-source.json`, `public_api_baseline.json`,
  `guarantees.yml`, `docs/reference/guarantees.md`,
  `src/sandloom/core/error_codes.py`, `docs/reference/error-codes.md`,
  `tests/architecture/kernel_edges.yml`, `design/kernel-boundary/edges.yml`,
  `tests/architecture/test_lint_suppression_ratchet.py`. Never in your area;
  add your own lines, do not reorder others'. Every new file goes into
  `public-source.json` (`python3 tools/agent_registry.py classify <path>`).
- Commits end with `Co-Authored-By: Claude <your model> <noreply@anthropic.com>`.
  `gh pr create --base main`; the body ends with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Never merge.

## Test resources
Your own only, ports 20000-60000:
`docker run -d --name <alias>-pg -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_password -e POSTGRES_DB=sandloom_test -p 127.0.0.1:<P>:5432 postgres:16 -c max_connections=600`
and `docker run -d --name <alias>-redis -p 127.0.0.1:<R>:6379 redis:7-alpine redis-server --databases 256`.
Pass `SANDLOOM_TEST_DATABASE_URL`, `SANDLOOM_TEST_CACHE_URL`,
`SANDLOOM_TEST_BROKER_URL`, `SANDLOOM_TEST_OWNER=<alias>` in the same command.
Remove your containers before `task_done`.

## What tm hands you, and what you hand back
- **A review round.** When `task_start` answers with `review` and `threads`,
  the task came back from review: those comments ARE your brief. Fix each one
  on the PR's branch (the prompt names it; push to it, no new PR), then answer
  every comment with `review_comment_reply` (task, comment id, what changed
  and the commit, or why nothing did). A comment left unanswered reads as
  missed. Then `task_done` again.
- **Notes.** Every tm call may carry `notes` from other agents on your task:
  read them before your next edit. When you change something a neighbour's
  task depends on, or find its bug, write on its task: `active_claims` with
  `covering: <path>` names it, `reflection_add` with that `task` leaves the
  note, its first line standing on its own.
- **Checkpoints.** `task_checkpoint` after your first green test, before a
  long check, and before you stop: what is done (commit), what is next, what
  is still red. A compacted context or the next agent resumes from it.
- **Checks at the handover.** Your first `task_done` may be refused once with
  a `{"kind": "checks", ...}` deliverable in the reason: call `task_done`
  again with the same arguments plus that deliverable, unchanged, editing
  nothing in between (a new commit is a new run). A red result does not block
  the handover; it is recorded for the reviewer. The record has two stages: the
  pre-commit hooks (`results`) and the CI steps your diff selects (`ci_steps`:
  docs code check under Python 3.11, generated files, no-Redis list, driver
  lanes). A step the hook could not run is `not_run` with a reason, which is
  a record and no refusal; a driver lane runs only when the hook's environment
  carries your `SANDLOOM_TEST_*` and only for a change under `packages/`, so
  run it yourself before the push (a core change to what a driver uses
  included). One deadline of 570 s covers the whole hook; what it leaves no
  time for is `not_run`.
- **Deliverables.** `task_done` carries the PR (`git_commit`) and the
  `checklist` (one line per item of the "Before the PR" list, with the grep
  you ran for item 2). A task that requires them refuses a handover without.

## Report
Under 250 words: PR URL, head SHA, done against the task, test counts,
anything left or red on arrival (named apart from your own breakage).
