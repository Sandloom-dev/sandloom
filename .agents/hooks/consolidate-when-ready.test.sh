#!/bin/bash
# What `consolidate-when-ready.sh` does, pinned, end to end.
#
# The siblings say why a hook needs a test at all: a hook has no compiler and
# no analyzer, and the way it fails is silence — one that never fires and one
# that always passes look identical from outside. This one has a second way to
# fail that they do not: it writes. A bug here does not let something through,
# it files tasks in somebody's tracker, and it would do it on every handover.
#
# A real `tm` over a store of its own, in a home of its own, because the
# adapter is one line and the rule is in the binary: what is pinned is the
# pair — the script finds the binary, the binary reads the payload, the rule
# asks the store and files. The rule's cases one at a time are tm's own test
# suite's; this is the wiring, and "did it create anything" is asked of the
# store rather than inferred from an exit code that is 0 either way.
#
#   .claude/hooks/consolidate-when-ready.test.sh
set -u
# The fixtures below are a terminal acting as fixed actors, not the harness
# session this suite may be run from: inside a session — any harness's —
# `--actor` naming anyone but what the session holds is refused. And every
# trace line its binaries write would name that session, so the writes to
# this suite's scratch store would read, in the live trace, as the agent's own.
unset TM_TRACE_SESSION CLAUDE_CODE_SESSION_ID OPENCODE_SESSION_ID ANTIGRAVITY_CONVERSATION_ID PI_SESSION_ID
# Nor as a harness's process: a `tm` with these is an agent's door, which
# does not make the fixture project.
unset CLAUDECODE OPENCODE ANTIGRAVITY_AGENT PI_CODING_AGENT
# Nor into the live trace: an agent's shell has it on, and what this suite's
# binaries do to its scratch store is no session's work.
unset TM_TRACE

hook="$(cd "$(dirname "$0")" && pwd)/consolidate-when-ready.sh"

tm=${TM_BIN:-$HOME/.local/bin/tm}
if [ ! -x "$tm" ]; then
  echo "SKIP: no tm binary at $tm (set TM_BIN)" >&2
  exit 0
fi
# Both found by the person's home, before the suite swaps it for its own
# below: the binary, made absolute so the move out of the checkout leaves it
# standing, and the machine's embedding server, which nothing here starts or
# stops.
tm="$(cd "$(dirname "$tm")" && pwd)/$(basename "$tm")"
rendezvous=${TM_EMBED_RV:-$HOME/.tm/embed.rv}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# A home of its own, which is a machine of its own: what `tm` keeps — the store
# it opens when none is named, the checkouts beside it, the marks of the servers
# it asks — it finds under `~/.tm`, so none of the person's is read or written.
# The embedding server stays the machine's, read at its rendezvous:
# `TM_EMBED_SERVER=0` uses one found there and starts none, since one started
# here would publish itself where the machine's clients look.
export HOME="$work/home"
mkdir -p "$HOME"
export TM_EMBED_RV="$rendezvous" TM_EMBED_SERVER=0
# And from a directory of its own: from a checkout — this repository's,
# where the gate starts the suite — a verb naming no project reads its marker.
cd "$work" || exit 1

repo="$work/repo"
mkdir -p "$repo"
git -C "$repo" init --quiet 2>/dev/null

# A path as the harness on this machine spells it: on Windows the payload
# carries `D:/…`, never the shell's `/tmp/…`, which the binary could not find.
native() {
  cygpath -m "$1" 2>/dev/null || printf '%s' "$1"
}

project=$("$tm" project create "Consolidation Fixture" 2>/dev/null)
if [ -z "$project" ]; then
  echo "FAIL could not create the fixture project" >&2
  exit 1
fi

pass=0
fail=0

# How many consolidation tasks the store holds, whatever their state.
filed() {
  "$tm" list --project "$project" --tag consolidation 2>/dev/null |
    grep -c 'Fold the reflections'
}

# `want` is how many tasks this case must file: 1 or 0. `bin` is the binary
# the hook is given and `path` the PATH it runs under, both the suite's own
# unless a case takes them away.
check() {
  local label=$1 want=$2 payload=$3 bin=${4:-$tm} path=${5:-$PATH}
  local before after
  before=$(filed)
  said=$(printf '%s' "$payload" |
    TM_BIN="$bin" PATH="$path" "$BASH" "$hook" 2>/dev/null)
  after=$(filed)
  if [ "$((after - before))" = "$want" ]; then
    pass=$((pass + 1))
    echo "ok   $label"
  else
    fail=$((fail + 1))
    echo "FAIL $label — wanted $want filed, got $((after - before))"
  fi
}

call() {
  printf '{"hook_event_name":"PostToolUse","session_id":"s","cwd":"%s","tool_name":"%s","tool_input":{"task":"t"},"tool_response":"ok"}' \
    "$(native "$repo")" "$1"
}
done_call=$(call mcp__tm__task_done)

marker() {
  printf 'version: 1\nproject: %s\n%s\n' "$project" "$1" \
    >"$repo/.tm/project.yaml"
}
mkdir -p "$repo/.tm"

# --- the gate before anything is asked of the store -------------------------

marker 'consolidate_hook: on'
check "under the threshold — nothing filed" 0 "$done_call"

# Over the threshold: the sum of decayed importance is 8, and a fresh
# reflection at full importance has not decayed yet.
anchor=$("$tm" task create "Anchor" --project "$project" 2>/dev/null)
for i in 1 2 3 4 5 6 7 8 9 10; do
  "$tm" reflect "$anchor" --project "$project" --importance 1 \
    --content "Fixture reflection number $i, about nothing in particular." \
    >/dev/null 2>&1
done

check "another tool is not a task boundary" 0 "$(call mcp__tm__task_start)"
check "another vendor's server is not this tracker" 0 \
  "$(call mcp__tmux__task_done)"

rm -f "$repo/.tm/project.yaml"
check "no project marker — nothing to consolidate against" 0 "$done_call"

marker '# no key'
check "marker without \`consolidate_hook: on\` — opt-in means opt-in" 0 \
  "$done_call"

# --- filing, and the one-open rate limit -------------------------------------

marker 'consolidate_hook: on   # a trailing comment is not part of the value'

# With the memory due and the repository opted in, so a binary found anywhere
# would file one: none at the path the hook is given, none in the suite's
# home, which has nothing installed in it, and none on a PATH that names no
# directory. Asked where the rule files nothing, the case would pass whether
# the hook found a binary or not.
check "no usable tm binary — quietly does nothing" 0 "$done_call" \
  "$work/absent" "$work/no-path"

check "over the threshold with nothing open — files one" 1 "$done_call"

# The model is told, in the harness's own channel for it: a task it did not
# file is on the front, and the procedure is named.
case "$said" in
*'additionalContext'*'tm-consolidate'*)
  pass=$((pass + 1))
  echo "ok   and says so to the session"
  ;;
*)
  fail=$((fail + 1))
  echo "FAIL filing said nothing to the session:" \
    "$(printf '%s' "$said" | head -c 120)"
  ;;
esac

check "one already open and pending — files nothing" 0 "$done_call"

consolidation=$("$tm" list --project "$project" --tag consolidation 2>/dev/null |
  grep 'Fold the reflections' | head -1 | awk '{print $1}')
"$tm" task start "$consolidation" --actor fixture >/dev/null 2>&1
check "one already being worked on — files nothing" 0 "$done_call"

# In review is not open: it has been done, and the verdict is somebody
# else's. Blocking on it would mean no consolidation until a person got round
# to the last one, which is the stall this hook exists to end.
"$tm" task done "$consolidation" --actor fixture >/dev/null 2>&1
check "the last one is awaiting a verdict — files the next" 1 "$done_call"

# A worktree running its own agent registers the same binary under its own
# name, so the tool name changes while the boundary it marks does not.
next=$("$tm" list --project "$project" --tag consolidation --status pending \
  2>/dev/null | grep 'Fold the reflections' | head -1 | awk '{print $1}')
"$tm" task cancel "$next" >/dev/null 2>&1
check "the last one is closed, and a worktree's tm server marks the boundary" 1 \
  "$(call mcp__tm-wt__task_done)"


echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
