#!/bin/bash
# What `require-tm-claim.sh` does, pinned.
#
# A hook has no compiler and no analyzer, and the way it fails is silence: a
# guard pointed at a path that does not exist stands dead, and everything goes
# on looking fine because a guard that never fires and a guard that always
# passes are the same thing from the outside. This script is the difference
# between those two.
#
# Self-contained: its own home and the store `tm` keeps in it, its own
# project, its own checkout-shaped directory. Of the person's `~/.tm` it reads
# only where the machine's embedding server listens, and this repository's
# marker it does not read at all.
#
#   .claude/hooks/require-tm-claim.test.sh
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

hook="$(cd "$(dirname "$0")" && pwd)/require-tm-claim.sh"

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
# Spelled the way the machine's own programs spell it: on Windows `tm` is a
# native binary, and the `/tmp/…` of the shell running this test names
# nothing to it — every path in a payload would be a file in no checkout, and
# the guard would pass it unchecked. Git's bash reads `C:/…` just as well.
work=$(cygpath -m "$work" 2>/dev/null || printf '%s' "$work")
trap 'rm -rf "$work"' EXIT

# A home of its own, which is a machine of its own: what `tm` keeps — the
# registry, the stores it files and the checkouts in it, the marks of the
# servers it asks — it finds under `~/.tm`, so none of the person's is read or
# written. The embedding server stays the machine's, read at its rendezvous:
# `TM_EMBED_SERVER=0` uses one found there and starts none, since one started
# here would publish itself where the machine's clients look.
export HOME="$work/home"
mkdir -p "$HOME"
export TM_EMBED_RV="$rendezvous" TM_EMBED_SERVER=0
# And from a directory of its own: from a checkout — this repository's,
# where the gate starts the suite — a verb naming no project reads its marker.
cd "$work" || exit 1

repo="$work/repo"
mkdir -p "$repo/packages"

pass=0
fail=0

# One verdict, counted: `report <label> <wanted exit> <got exit>`.
report() {
  if [ "$3" = "$2" ]; then
    pass=$((pass + 1))
    echo "ok   $1"
  else
    fail=$((fail + 1))
    echo "FAIL $1 — wanted exit $2, got $3"
  fi
}

# Run the hook over one edit and compare the exit code. `payload` is the tool
# call as the harness would send it; `root` is what CLAUDE_PROJECT_DIR would be.
#
# The identity travels in the payload, not in the environment. A name read from
# the environment with a default would have the guard ask about one name on
# behalf of every session in every checkout — two agents in one store would be
# one actor to it. `attach_session` below is what turns the session handle into
# an actor — a session takes an actor from the project's registry and holds it,
# and `tm --session` answers with the one it holds. The fourth argument stays
# the actor because that is what each case is about; the token is derived from
# it here so that each case reads as a statement about an actor.
check() {
  local label=$1 want=$2 root=$3 actor=$4 path=$5
  local got
  printf \
    '{"tool_name":"Edit","tool_input":{"file_path":"%s"},"session_id":"sess-%s"}' \
    "$path" "$actor" |
    CLAUDE_PROJECT_DIR="$root" TM_BIN="$tm" \
      bash "$hook" >/dev/null 2>&1
  report "$label" "$want" $?
}

# ── an unmarked checkout ─────────────────────────────────────────────────
# Degrades open, and must: blocking every edit where tm is not set up yet
# would make the repository unfixable, which is worse than the gap.
check "no marker → allowed, unchecked" 0 "$repo" alice "$repo/packages/a.dart"

# ── a marked checkout ────────────────────────────────────────────────────
project=$("$tm" project create "Hook Fixture" 2>/dev/null)
if [ -z "$project" ]; then
  echo "FAIL could not create the fixture project" >&2
  exit 1
fi
(cd "$repo" && "$tm" project use "$project" >/dev/null 2>&1)
# The store `tm project create` made for the project, named by its id under
# that home and registered in its `~/.tm/tm.yaml`: read behind its back by the
# one case that counts the log, and shut by the live cases.
store="$HOME/.tm/projects/$project.db"

# Each fixture identity is a registered actor held by a session, because that is
# the only way the guard can tell one from another. Note that all three report
# the same workspace — they are three sessions in one checkout, which is the
# arrangement a guard keyed on a name cannot distinguish at all.
attach_session() {
  "$tm" actor register "$1" --project "$project" >/dev/null 2>&1
  "$tm" actor attach "$1" --project "$project" --token "sess-$1" \
    --workspace "$repo" >/dev/null 2>&1
}
attach_session alice
attach_session bob
attach_session carol

check "marker, nothing claimed → refused" 2 \
  "$repo" alice "$repo/packages/a.dart"

# The claim is what the whole hook is about, so both sides of it are checked
# against the same actor — a pass that came from a different identity would
# prove nothing.
task=$("$tm" task create "Fixture work" --project "$project" 2>/dev/null)
"$tm" task start "$task" --actor alice >/dev/null 2>&1

check "marker, claim held → allowed" 0 "$repo" alice "$repo/packages/a.dart"

# Two sessions in one checkout, which is what a name-keyed guard cannot do:
# alice's claim must not open the gate for bob, and the only thing separating
# them is the session handle in the payload.
check "another session's claim does not count" 2 \
  "$repo" bob "$repo/packages/a.dart"

# ── the area ─────────────────────────────────────────────────────────────
# A task that declares its paths is the whole point: the guard stops asking who
# you are and starts asking whether this file is yours. Alice's fixture task
# above declares nothing, so it still speaks for everything — that is the
# migration rule, and the check above is what pins it.
scoped=$("$tm" task create "Scoped work" --project "$project" \
  --area lib/parser --area guide/format.md 2>/dev/null)
"$tm" task start "$scoped" --actor carol >/dev/null 2>&1

mkdir -p "$repo/lib/parser/src" "$repo/lib/parser_extra" "$repo/guide"

check "inside the declared area → allowed" 0 \
  "$repo" carol "$repo/lib/parser/src/x.dart"

check "a file named in the area itself → allowed" 0 \
  "$repo" carol "$repo/guide/format.md"

# The refusal the area exists for: carol holds a live claim, and it is still not
# a licence to edit anything.
check "outside the declared area → refused despite a live claim" 2 \
  "$repo" carol "$repo/apps/shell/app.dart"

# The classic prefix bug: a plain `startsWith` would let this through on the
# strength of `lib/parser` alone.
check "a sibling directory with the same prefix → refused" 2 \
  "$repo" carol "$repo/lib/parser_extra/y.dart"

# ── things outside the guard's remit ─────────────────────────────────────
check "a file outside the root is not repository work" 0 \
  "$repo" bob "$work/elsewhere.dart"

# `.claude/` is exempt on purpose: a guard that can lock you out of its own
# off-switch is a trap rather than a guardrail.
check "the guard's own directory stays editable" 0 \
  "$repo" bob "$repo/.claude/settings.json"

# A nested `.claude/` is configuration as well — an application directory of a
# monorepo carrying its own hooks — and the exemption is deliberately a
# deny-list of one rather than an allow-list of today's config directories. bob
# holds nothing, so the only thing that can answer 0 here is the exemption.
mkdir -p "$repo/apps/shell/.claude/hooks"
check "a nested config directory is exempt too" 0 \
  "$repo" bob "$repo/apps/shell/.claude/hooks/guard.sh"

# The project's own list of what is not work, which the guard reads from the
# core rather than carrying as text: bob holds nothing, so an edit here is
# refused while the project guards the path. What the list then lets through
# is tested where the CLI's hook verb is, beside the core it reads: adding to
# the list is a person's, and a suite runs as a script, which the core
# refuses.
mkdir -p "$repo/out/scratch"
check "a path the project guards, unclaimed → refused" 2 \
  "$repo" bob "$repo/out/scratch/notes.md"

# A tm that cannot answer — it crashed, or could not open what it reads —
# leaves the hook with no verdict. The one exemption the hook can state by
# itself has to hold then, or a broken tm would refuse every edit to the
# hook's own configuration, which is the lock-out the exemption exists
# against.
brokentm="$work/brokentm"
cat >"$brokentm" <<EOF
#!/bin/sh
case "\$1" in
  hook) echo "Segmentation fault" >&2; exit 139 ;;
esac
exec "$tm" "\$@"
EOF
chmod +x "$brokentm"
check_with() {
  local bin=$1 label=$2 want=$3 root=$4 actor=$5 path=$6
  local got
  printf \
    '{"tool_name":"Edit","tool_input":{"file_path":"%s"},"session_id":"sess-%s"}' \
    "$path" "$actor" |
    CLAUDE_PROJECT_DIR="$root" TM_BIN="$bin" bash "$hook" >/dev/null 2>&1
  report "$label" "$want" $?
}
check_with "$brokentm" "a tm that cannot answer cannot lock its own config" 0 \
  "$repo" bob "$repo/.claude/settings.json"
# And it cannot check anything else either: the verb is where every question
# is asked, so a binary that cannot answer it has nothing to ask with. That is
# the price of the entry point, and it is paid out loud — the next case is the
# sentence, because an exit code of 0 alone is exactly what a satisfied guard
# returns.
check_with "$brokentm" "and it checks nothing rather than refusing everything" 0 \
  "$repo" bob "$repo/packages/a.dart"
said=$(printf \
  '{"tool_name":"Edit","tool_input":{"file_path":"%s"},"session_id":"sess-bob"}' \
  "$repo/packages/a.dart" |
  CLAUDE_PROJECT_DIR="$repo" TM_BIN="$brokentm" bash "$hook" 2>&1 >/dev/null)
case "$said" in
*"could not answer"*UNCHECKED*)
  pass=$((pass + 1))
  echo "ok   and says so, rather than reading as a guard that passed"
  ;;
*)
  fail=$((fail + 1))
  echo "FAIL a tm that could not answer degraded quietly: $said"
  ;;
esac

# A tm older than the harness answers `tm hook <harness>` with a usage
# mistake — and older ones exit 2 for it, the code of a refusal. Passed on,
# that refuses every edit under a harness the binary predates, the edit that
# would fix it included; so it is a tm that could not answer, said out loud.
oldtm="$work/oldtm"
cat >"$oldtm" <<EOF
#!/bin/sh
case "\$1" in
  hook) echo "tm hook: unknown harness \"\$2\" — one of claude-code" >&2; exit 2 ;;
esac
exec "$tm" "\$@"
EOF
chmod +x "$oldtm"
check_with "$oldtm" "a tm that does not know the harness refuses nothing" 0 \
  "$repo" bob "$repo/packages/a.dart"
said=$(printf \
  '{"tool_name":"Edit","tool_input":{"file_path":"%s"},"session_id":"sess-bob"}' \
  "$repo/packages/a.dart" |
  CLAUDE_PROJECT_DIR="$repo" TM_BIN="$oldtm" bash "$hook" 2>&1 >/dev/null)
case "$said" in
*"does not know"*UNCHECKED*)
  pass=$((pass + 1))
  echo "ok   and says so"
  ;;
*)
  fail=$((fail + 1))
  echo "FAIL a tm that does not know the harness was not said: $said"
  ;;
esac

# And a refusal is never one, whatever it quotes. A path carrying the words
# of a usage mistake is quoted in the reason — mid-sentence for bob, who
# holds nothing, and as its opening words for carol, whose refusal starts
# with the path it refuses — and both stay refused, as the old tm's mistake
# stays no answer, with stderr copied as tm writes it and with no `mktemp`
# to copy it into. The paths are spelled as the payload's JSON spells them.
held="$work/no-mktemp"
mkdir -p "$held"
printf '#!/bin/sh\nexit 1\n' >"$held/mktemp"
chmod +x "$held/mktemp"
for mode in copied held; do
  path=$PATH
  [ "$mode" = held ] && path="$held:$PATH"
  PATH=$path check_with "$oldtm" \
    "($mode) a tm that does not know the harness refuses nothing" 0 \
    "$repo" bob "$repo/packages/a.dart"
  PATH=$path check "($mode) a refusal quoting a usage mistake's words → refused" \
    2 "$repo" bob "$repo"'/packages/unknown harness \"agy\".dart'
  PATH=$path check "($mode) and one opening with them → refused" 2 \
    "$repo" carol "$repo"'/unknown harness \"agy\".dart'
done

# But a worktree is a checkout filed under `.claude/`, not configuration, and
# `.claude/worktrees/` is where Claude Code puts one. A blanket exemption would
# mean switching worktree isolation on switches this guard off, silently, for
# every agent using it.
check "a worktree under .claude is repository work, not configuration" 2 \
  "$repo" bob "$repo/.claude/worktrees/feature/packages/a.dart"

# Firing is not the same as answering correctly, and the case above cannot tell
# those apart: bob holds nothing, so it is refused whatever the guard makes of
# the path. Carol holds `lib/parser`, so the next two say which side of
# her area a file lands on when it is reached through a worktree — an area
# speaks about a position in the checkout, and `.claude/worktrees/<name>/` is
# not part of that position.
wt="$repo/.claude/worktrees/agent-1"
# A worktree is a checkout, so the repository's marker is in it as well.
# Without it the hook degrades open and the last check below would pass for
# that reason instead of the right one.
mkdir -p "$wt/.tm" && cp "$repo/.tm/project.yaml" "$wt/.tm/project.yaml"

check "inside the area, reached through a worktree → allowed" 0 \
  "$repo" carol "$wt/lib/parser/src/x.dart"

check "outside the area, reached through a worktree → still refused" 2 \
  "$repo" carol "$wt/apps/shell/app.dart"

# The other half of it. Which of these two the harness actually produces —
# CLAUDE_PROJECT_DIR at the main checkout or at the worktree — is undocumented,
# so the guard is pinned under both rather than under the one somebody guessed.
check "the worktree itself as the project root → allowed" 0 \
  "$wt" carol "$wt/lib/parser/src/x.dart"

# And the case that says the one above went through the claim rather than past
# it. With the worktree as the root, the absolute path still reads
# `…/repo/.claude/worktrees/agent-1/…`, and an exemption matched on that path
# would catch every file in it: the hook would exit 0 before reading a claim, so
# the check above would pass with the guard switched off. Only a file outside
# carol's area can tell those apart — a pass proves nothing here, a refusal
# proves the area was consulted.
check "the worktree as the root, outside the area → refused" 2 \
  "$wt" carol "$wt/apps/shell/app.dart"

# ── a checkout git knows about and the harness does not ──────────────────
# The two cases above hold because `.claude/worktrees/<name>/` is stripped by
# name, which is one harness's directory layout written into this guard. A
# worktree filed anywhere else is the case that layout cannot answer and git
# can: the file below is `lib/parser/src/x.dart` of its checkout, and carol's
# area speaks for it — but read against the main checkout it is
# `wt-other/lib/parser/src/x.dart`, which no area matches.
#
# Physical paths throughout, because git answers with them and a macOS
# temporary directory is a symlink; the hook falls back to the harness's root
# when the two disagree, so a fixture built on the symlinked path would exercise
# the fallback and call it a pass.
if command -v git >/dev/null 2>&1; then
  hard=$(cd "$work" && pwd -P)
  # And spelled natively again, for the reason `work` is: `pwd -P` answers in
  # the shell's `/c/…` spelling, which a native `tm` cannot open.
  hard=$(cygpath -m "$hard" 2>/dev/null || printf '%s' "$hard")
  gitrepo="$hard/gitrepo"
  mkdir -p "$gitrepo/.tm"
  cp "$repo/.tm/project.yaml" "$gitrepo/.tm/project.yaml"
  (
    cd "$gitrepo" &&
      git init -q &&
      git add -A &&
      git -c user.email=fixture@example.invalid -c user.name=fixture \
        commit -qm "fixture"
  ) >/dev/null 2>&1
  git -C "$gitrepo" worktree add -q --detach "$gitrepo/wt-other" HEAD >/dev/null 2>&1

  if [ -d "$gitrepo/wt-other/.tm" ]; then
    mkdir -p "$gitrepo/wt-other/lib/parser/src" "$gitrepo/wt-other/apps/shell"

    check "a worktree outside .claude, inside the area → allowed" 0 \
      "$gitrepo" carol "$gitrepo/wt-other/lib/parser/src/x.dart"

    # The other side of the same answer: git moving the root must not turn the
    # guard into a pass. Outside carol's area is outside it in either checkout.
    check "a worktree outside .claude, outside the area → refused" 2 \
      "$gitrepo" carol "$gitrepo/wt-other/apps/shell/app.dart"

    # `Write` creates directories, and git cannot be asked about one that is not
    # there yet. Every case above `mkdir -p`s its fixture first, which is how a
    # suite ends up testing only the state it prepared: the two cases here
    # differ from the two above in nothing but whether the directory exists.
    check "a new directory inside the area, in a worktree → allowed" 0 \
      "$gitrepo" carol "$gitrepo/wt-other/lib/parser/src/fresh/x.dart"

    check "a new directory outside the area, in a worktree → refused" 2 \
      "$gitrepo" carol "$gitrepo/wt-other/apps/shell/fresh/x.dart"

    # A nested checkout is somebody's vendored dependency, not this repository.
    # Taking its root would move the marker lookup into it, where there is
    # none — and the hook degrades open when it cannot find one, so the git
    # lookup would have switched the guard off for every file inside. bob holds
    # nothing, so a 0 here can only come from that degradation.
    mkdir -p "$gitrepo/vendor/lib"
    (cd "$gitrepo/vendor/lib" && git init -q) >/dev/null 2>&1
    check "a nested checkout does not move the marker" 2 \
      "$gitrepo" bob "$gitrepo/vendor/lib/x.dart"

    # And the same nested checkout with a marker of its own, which is the case
    # "does it carry a marker" cannot tell from a worktree. It would pass every
    # other condition: under the harness root, path under it, marker present.
    # Then claims would be checked against whatever project its marker names
    # and `rel` shortened to a path inside it — so carol, who holds
    # `lib/parser` in the outer project, would be authorised for
    # `vendor/app/lib/parser/x.dart`, which her area does not speak for.
    #
    # `--git-common-dir` is what separates them: a worktree shares the
    # checkout's, an independent repository has its own.
    mkdir -p "$gitrepo/vendor/app/.tm" "$gitrepo/vendor/app/lib/parser"
    cp "$repo/.tm/project.yaml" "$gitrepo/vendor/app/.tm/project.yaml"
    (cd "$gitrepo/vendor/app" && git init -q) >/dev/null 2>&1
    check "a nested checkout with its own marker is still not this repo" 2 \
      "$gitrepo" carol "$gitrepo/vendor/app/lib/parser/x.dart"
  else
    echo "skip a worktree git knows about (git worktree add failed)"
  fi

  # ── the same script under Codex, through the line Codex would run ───────
  # One adapter for every harness whose hooks are a program with JSON on
  # stdin, and the registration names the harness as the first argument. What
  # is run here is not the script but the REGISTERED LINE, the way Codex runs
  # it: through a login shell, in the session's directory — a subdirectory,
  # so a relative path would miss — with no variable naming the checkout and
  # the session id in the payload alone. A quoting slip in the spelling shows
  # up here and nowhere else; a test that called the script by path would
  # pass with the registration broken.
  #
  # The payload is Codex's: Claude Code's with three fields more. The
  # verdicts have to come out as above, because the decision was never the
  # harness's.
  mkdir -p "$gitrepo/lib/parser/src" "$gitrepo/packages"
  (cd "$gitrepo" && "$tm" skills install --harness codex) >/dev/null 2>&1
  registered=$(sed -n 's/.*"command": "\(.*require-tm-claim[^"]*"\) codex".*/\1 codex/p' \
    "$gitrepo/.codex/hooks.json" | head -1 | sed 's/\\"/"/g')
  if [ -z "$registered" ]; then
    fail=$((fail + 1))
    echo "FAIL codex: no registration for the claim hook in .codex/hooks.json"
  fi
  codex_payload() {
    printf '{"session_id":"sess-%s","transcript_path":null,"cwd":"%s","hook_event_name":"PreToolUse","model":"gpt","permission_mode":"default","turn_id":"t1","tool_name":"%s","tool_input":%s,"tool_use_id":"call_1"}' \
      "$1" "$gitrepo/lib" "$2" "$3"
  }
  # Run as Codex would: `sh -lc '<line>'` from a subdirectory.
  check_codex() {
    local label=$1 want=$2 actor=$3 tool=$4 input=$5
    codex_payload "$actor" "$tool" "$input" |
      (cd "$gitrepo/lib" &&
        env -u CLAUDE_PROJECT_DIR TM_BIN="$tm" sh -lc "$registered") \
      >"$work/codex.out" 2>"$work/codex.err"
    report "$label" "$want" $?
  }

  # Every native edit under Codex arrives as `apply_patch`, with the patch
  # text where a shell command would be and the file names inside it, one
  # hunk each, relative to the session's directory. The guard reads every
  # one, so a patch is refused for the first file no claim speaks for, and
  # passes when all of them are spoken for.
  check_codex "codex: apply_patch naming an unclaimed file → refused" 2 \
    bob apply_patch \
    '{"command":"*** Begin Patch\n*** Update File: ../packages/a.dart\n@@\n-x\n+y\n*** End Patch"}'
  if grep -q 'packages/a.dart' "$work/codex.err"; then
    pass=$((pass + 1))
    echo "ok     and the refusal names the file the patch touches"
  else
    fail=$((fail + 1))
    echo "FAIL   the refusal has to name the file: $(cat "$work/codex.err")"
  fi
  check_codex "codex: apply_patch inside the claimed area → allowed" 0 \
    carol apply_patch \
    '{"command":"*** Begin Patch\n*** Add File: parser/src/y.dart\n+z\n*** End Patch"}'
  check_codex "codex: apply_patch naming no file → nothing to refuse" 0 \
    bob apply_patch '{"command":"*** Begin Patch\n*** End Patch"}'

  # A payload naming a file, which is how the same wire arrives from Claude
  # Code and how a Codex tool that carries a path would arrive: the codec
  # reads it the same under either name.
  check_codex "codex: a write nobody claimed → refused, from a subdirectory" 2 \
    bob Write "{\"file_path\":\"$gitrepo/packages/a.dart\"}"
  if grep -q '"permissionDecision":"deny"' "$work/codex.out" &&
    [ -s "$work/codex.err" ]; then
    pass=$((pass + 1))
    echo "ok     with the reason in the JSON and on stderr, which Codex needs"
  else
    fail=$((fail + 1))
    echo "FAIL   the refusal has to reach both channels under Codex"
  fi
  check_codex "codex: inside the claimed area → allowed" 0 \
    carol Write "{\"file_path\":\"$gitrepo/lib/parser/src/x.dart\"}"
else
  echo "skip a worktree git knows about (no git)"
fi

# ── a marker naming a project this store never held ──────────────────────
# The second machine, which is ordinary: `.tm/project.yaml` is committed and
# travels with the repository, `~/.tm/tm.db` is local and does not. So the
# marker names a project that exists somewhere else, and the guard has nothing
# to check against.
#
# It degrades open, like every other "cannot check" — and what this case is
# about is the sentence. Folded into `tm claims failed` it would read as
# breakage on every single write, which is the worst of both: noise that looks
# like noise and means "there is no guard right now". The exit code alone cannot
# tell the two apart, so this is the one case here that reads stderr.
elsewhere="$work/elsewhere"
mkdir -p "$elsewhere/.tm/x" "$elsewhere/packages"
printf 'version: 1\nproject: 019f0000-0000-7000-8000-000000000000\n' \
  >"$elsewhere/.tm/project.yaml"
said=$(printf \
  '{"tool_name":"Edit","tool_input":{"file_path":"%s"},"session_id":"sess-alice"}' \
  "$elsewhere/packages/a.dart" |
  CLAUDE_PROJECT_DIR="$elsewhere" TM_BIN="$tm" bash "$hook" 2>&1 >/dev/null)
code=$?

case "$code:$said" in
0:*"this tm store has no project 019f0000-"*)
  pass=$((pass + 1))
  echo "ok   a project this store never held is named, not called a failure"
  ;;
0:*)
  fail=$((fail + 1))
  echo "FAIL degraded open but said nothing about the missing project: $said"
  ;;
*)
  fail=$((fail + 1))
  echo "FAIL wanted exit 0 with a named cause, got $code"
  ;;
esac

# A non-editing tool is none of its business. Bob's, because bob's Edit of the
# same file is refused above: a hook that gated a Read would refuse this one.
printf \
  '{"tool_name":"Read","tool_input":{"file_path":"%s"},"session_id":"sess-bob"}' \
  "$repo/packages/a.dart" |
  CLAUDE_PROJECT_DIR="$repo" TM_BIN="$tm" \
    bash "$hook" >/dev/null 2>&1
if [ $? = 0 ]; then
  pass=$((pass + 1))
  echo "ok   a Read is not gated"
else
  fail=$((fail + 1))
  echo "FAIL a Read is not gated"
fi

# ── the edit costs nothing ───────────────────────────────────────────────
# A passing edit is a read. A hook that ended by writing — a beat on every
# passing edit — would make the event log mostly beats, and this is the case
# that says it does not, rather than the comment above it.
#
# Differentiating: a hook that writes on the passing path fails this case. What
# it guards is the property — a passing edit is a read — and not the absence of
# one particular line: a hook calling a verb this binary does not have writes
# nothing and looks clean, so a test for the line would pass on it.
if command -v sqlite3 >/dev/null 2>&1; then
  count_events() { sqlite3 "$store" 'SELECT COUNT(*) FROM event_log;' 2>/dev/null; }
  before=$(count_events)
  # Alice's, whose claim speaks for the file: an edit that passes, which is
  # what the case is about — a refused one would write nothing either.
  printf \
    '{"tool_name":"Edit","tool_input":{"file_path":"%s"},"session_id":"sess-alice"}' \
    "$repo/packages/a.dart" |
    CLAUDE_PROJECT_DIR="$repo" TM_BIN="$tm" \
      bash "$hook" >/dev/null 2>&1
  passed=$?
  after=$(count_events)
  if [ "$passed" != 0 ]; then
    fail=$((fail + 1))
    echo "FAIL the edit meant to pass was refused (exit $passed) — the case below measures nothing"
  elif [ -z "$before" ] || [ -z "$after" ]; then
    # Both reads are `2>/dev/null`, so a store this shell cannot open answers
    # empty twice — and empty equals empty, which is the assertion below
    # passing on no evidence at all. The one case pinning "a passing edit is a
    # read" is not allowed to be the one case that cannot fail.
    fail=$((fail + 1))
    echo "FAIL could not count events in $store — the case below would pass vacuously"
  elif [ "$before" = "$after" ]; then
    pass=$((pass + 1))
    echo "ok   a passing edit writes nothing to the log"
  else
    fail=$((fail + 1))
    echo "FAIL a passing edit wrote $((after - before)) event(s)"
  fi
else
  echo "skip a passing edit writes nothing (no sqlite3)"
fi

# ── a shell command ──────────────────────────────────────────────────────
# `Bash` is not in this hook's matcher. It must still be inert if something
# calls it that way: the command text does not say what it writes — that is the
# whole reason `report-bash-writes.sh` exists — so a hook guessing from it would
# block `ls` and pass a heredoc, which is worse than not guessing.
printf '{"tool_name":"Bash","command":"rm -rf /","session_id":"sess-bob"}' |
  CLAUDE_PROJECT_DIR="$repo" TM_BIN="$tm" \
    bash "$hook" >/dev/null 2>&1
if [ $? = 0 ]; then
  pass=$((pass + 1))
  echo "ok   a Bash call is not this hook's business"
else
  fail=$((fail + 1))
  echo "FAIL a Bash call was refused; only files are gated here"
fi

# ── a machine the hook cannot read ───────────────────────────────────────
# A registry that cannot be used — not YAML, the console's earlier list —
# or a store it names that is gone leaves no rule a store to read, and the
# claim guard is off. The edit goes through, as a hook that cannot check
# lets it: a broken registry that locked every edit would lock the person
# out of the checkout they repair it from. What it may not do is pass in
# silence, and Claude Code shows nobody the stderr of a hook that exits 0 —
# so the session is told in the harness's own answer, `systemMessage` for
# the person and `additionalContext` for the model, and told once, not on
# every edit.
#
# Bob's edits, because bob holds nothing: an edit of his that passes is the
# guard being off, not a claim. What was said is kept in the temp
# directory — the suite's own here, under each name a platform reads it by
# — so no earlier run has said anything.
registry="$HOME/.tm/tm.yaml"
cp "$registry" "$work/tm.yaml.good"
mkdir -p "$work/said"
off_out="$work/off.out"
# One call through script $1 with payload $2: its exit, its stdout kept.
off_call() {
  printf '%s' "$2" |
    CLAUDE_PROJECT_DIR="$repo" TM_BIN="$tm" TMPDIR="$work/said" \
      TEMP="$work/said" TMP="$work/said" bash "$1" >"$off_out" 2>/dev/null
}
off_edit() {
  printf \
    '{"tool_name":"Edit","tool_input":{"file_path":"%s"},"session_id":"%s"}' \
    "$repo/packages/a.dart" "$1"
}
# Whether the last call's answer says the guard is off, to the person and
# to the model, with $1 in it.
off_said() {
  grep -q '"systemMessage":"tm claim guard OFF: [^"]*'"$1" "$off_out" &&
    grep -q '"additionalContext":"tm claim guard OFF: [^"]*'"$1" "$off_out"
}
# `off_case <label> <ok>`: one verdict on a condition already tested.
off_case() {
  if [ "$2" = 0 ]; then
    pass=$((pass + 1))
    echo "ok   $1"
  else
    fail=$((fail + 1))
    echo "FAIL $1: $(cat "$off_out")"
  fi
}

printf 'a: [\n' >"$registry"
off_call "$hook" "$(off_edit sess-off-1)"
report "a registry that is not YAML → allowed" 0 $?
off_said "cannot be read"
off_case "  and the session is told, the person and the model" $?
off_call "$hook" "$(off_edit sess-off-1)"
code=$?
! grep -q 'claim guard OFF' "$off_out"
off_case "  once: the next edit of the session passes saying nothing" \
  $((code + $?))

# The repair is the one `tm doctor` names, word for word: a person sent
# from the session to `tm doctor` reads there what the session was told.
off_call "$hook" "$(off_edit sess-off-2)"
fix=$(sed -n 's/.*"systemMessage":"[^"]* — fix: \([^"]*\)".*/\1/p' "$off_out")
doctor=$(cd "$repo" && "$tm" doctor 2>&1)
[ -n "$fix" ] && case "$doctor" in *"fix: $fix"*) true ;; *) false ;; esac
off_case "  and the fix is the one tm doctor names" $?

# A session start says it too, through the adapter every event shares, and
# is the once for the edits after it.
off_call "$(dirname "$hook")/tm-hook.sh" \
  "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"sess-off-3\",\"cwd\":\"$repo\",\"source\":\"startup\"}"
report "a session start on it → answered" 0 $?
off_said "cannot be read"
off_case "  and it says the guard is off" $?
off_call "$hook" "$(off_edit sess-off-3)"
code=$?
! grep -q 'claim guard OFF' "$off_out"
off_case "  and the edits after it pass saying nothing more" $((code + $?))

printf 'version: 3\n' >"$registry"
off_call "$hook" "$(off_edit sess-off-4)"
report "a registry a later tm wrote → allowed" 0 $?
off_said 'fix: run the tm that wrote it'
off_case "  and told to run that tm" $?

# A store the registry names that is not on the disk: the project's alone,
# moved aside with whatever sqlite keeps beside it and put back after.
cp "$work/tm.yaml.good" "$registry"
for f in "$store" "$store-wal" "$store-shm"; do
  if [ -e "$f" ]; then mv "$f" "$f.aside"; fi
done
off_call "$hook" "$(off_edit sess-off-5)"
report "a store the registry names and the disk lacks → allowed" 0 $?
off_said 'fix: put the store back where the registry says'
off_case "  and told where to put it back" $?
for f in "$store" "$store-wal" "$store-shm"; do
  if [ -e "$f.aside" ]; then mv "$f.aside" "$f"; fi
done

# Repaired, the guard is on again: bob's edit is refused.
off_call "$hook" "$(off_edit sess-off-1)"
report "the registry repaired → refused again" 2 $?

# ── the same verdicts from the session's live server ─────────────────────
# Every check above opened the store, because no session here had a server —
# the case the hook goes on handling. Where a session's `tm mcp` is up, the
# hook asks it instead and folds no log, which is what makes an edit cheap.
# The verdicts must be the same ones.
#
# What says the SERVER answered, rather than the hook's own store: the store
# is unreadable while each hook runs. The servers opened it before that and
# keep their connections, and a hook that fell back to opening it could not:
# it would degrade open, exit 0 with UNCHECKED on stderr. So a refusal here is
# the server's, and an allow is the server's only when it comes back clean.
#
# In the suite's home, as everything here is, so the marks they hold are not
# the machine's and the store each opens is the one the cases above wrote.
# `TM_EMBED_SERVER=0` from the top matters here most: a server warms its
# embedder on the way up, and with no embedding server at the rendezvous it
# would otherwise start one — a model load, for a guard that embeds nothing.
case "$(uname -s)" in
MINGW* | MSYS* | CYGWIN*)
  echo "skip the live server: a hook finds no socket on Windows"
  ;;
*)
  live="$HOME/.tm/live"
  live_pids=""
  # A server for sess-<actor>. Its stdin is a fifo the shell holds open,
  # because a server whose stdin ends has lost its session and exits. Started
  # in the fixture's checkout with no store named, as a harness starts it: it
  # serves what the registry under this home files, opening the store of the
  # project the checkout's marker names as it starts, and its mark names that
  # registry, which is the one the hook reads.
  live_serve() {
    mkfifo "$work/live-$1.in"
    (cd "$repo" && CLAUDE_CODE_SESSION_ID="sess-$1" \
      exec "$tm" mcp <"$work/live-$1.in" >/dev/null 2>>"$work/live.err") &
    live_pids="$live_pids $!"
  }
  live_serve alice
  exec 3>"$work/live-alice.in"
  live_serve bob
  exec 4>"$work/live-bob.in"
  live_serve carol
  exec 5>"$work/live-carol.in"

  live_up=0
  for _ in $(seq 1 150); do
    if [ -S "$live/tm.sess-alice.sock" ] && [ -S "$live/tm.sess-bob.sock" ] &&
      [ -S "$live/tm.sess-carol.sock" ]; then
      live_up=1
      break
    fi
    sleep 0.1
  done

  # `check`, with the store shut while the hook runs.
  check_live() {
    local label=$1 want=$2 actor=$3 path=$4
    local got said
    # Shutting a file that is not the store shuts nothing: the hook would read
    # the log itself, and its verdicts would pass for the server's.
    if [ ! -s "$store" ]; then
      fail=$((fail + 1))
      echo "FAIL live: $label — no store at $store to shut"
      return
    fi
    chmod 000 "$store"
    said=$(printf \
      '{"tool_name":"Edit","tool_input":{"file_path":"%s"},"session_id":"sess-%s"}' \
      "$path" "$actor" |
      CLAUDE_PROJECT_DIR="$repo" TM_BIN="$tm" bash "$hook" 2>&1 >/dev/null)
    got=$?
    chmod 644 "$store"
    if [ "$got" = 0 ] && [ -n "$said" ]; then
      fail=$((fail + 1))
      echo "FAIL live: $label — allowed, but not by the server: $said"
      return
    fi
    report "live: $label" "$want" "$got"
  }

  if [ "$live_up" = 1 ]; then
    check_live "nothing claimed → refused" 2 bob "$repo/packages/a.dart"
    check_live "a claim held → allowed" 0 alice "$repo/packages/a.dart"
    check_live "inside the declared area → allowed" 0 \
      carol "$repo/lib/parser/src/x.dart"
    check_live "outside the declared area → refused" 2 \
      carol "$repo/apps/shell/app.dart"
    check_live "a sibling with the same prefix → refused" 2 \
      carol "$repo/lib/parser_extra/y.dart"
    check_live "the guard's own directory stays editable" 0 \
      bob "$repo/.claude/settings.json"

    # A claim taken by another process after the servers came up: what a
    # server answers is the log as it stands, not as it stood at its start.
    late=$("$tm" task create "Late work" --project "$project" \
      --area out/late 2>/dev/null)
    "$tm" task start "$late" --actor bob >/dev/null 2>&1
    check_live "a claim taken since the server started → allowed" 0 \
      bob "$repo/out/late/notes.md"

    # A server that cannot read the registry either answers every call
    # with its code and not the problem, and the repair turns on which
    # problem it was: the hook reads the registry the server reads, and
    # says the guard is off as it does with no server at all.
    printf 'a: [\n' >"$registry"
    off_call "$hook" "$(off_edit sess-bob)"
    report "live: a registry the server cannot read → allowed" 0 $?
    off_said 'fix: repair'
    off_case "live:   and the session is told, with the repair" $?
    cp "$work/tm.yaml.good" "$registry"
  else
    fail=$((fail + 1))
    echo "FAIL live: the servers made no socket for their hooks:"
    cat "$work/live.err"
  fi

  exec 3>&- 4>&- 5>&-
  # shellcheck disable=SC2086 # one pid per word
  kill $live_pids 2>/dev/null
  # shellcheck disable=SC2086
  wait $live_pids 2>/dev/null
  ;;
esac

# ── which tm the hooks run ───────────────────────────────────────────────
# Every adapter beside this one finds `tm` by the same steps, pinned here for
# all four: `TM_BIN`, then `tm:` in the machine's registry, then the installed
# one, then PATH. A step skipped is a hook that runs another build — one that
# predates an event type in the log refuses it, and the guard stands down.
# Each stand-in records its name and answers as the verb would; a home of
# their own holds the registry and the installed one.
picks="$work/picks"
mkdir -p "$picks/home/.tm" "$picks/home/.local/bin" "$picks/home/bin" \
  "$picks/path"
stand_in() {
  printf '#!/bin/sh\necho %s >"%s/ran"\nexit 0\n' "$1" "$picks" >"$2"
  chmod +x "$2"
}
stand_in dev "$picks/dev-tm"
stand_in registered "$picks/registered tm"
stand_in home "$picks/home/bin/tm"
stand_in installed "$picks/home/.local/bin/tm"
stand_in onpath "$picks/path/tm"
# Run [hook] under the stand-ins' home with the registry saying [line], and
# with `TM_BIN` set to [dev] where it is not empty: which ran, or `none`.
ran() {
  local hook=$1 line=$2 dev=$3
  rm -f "$picks/ran"
  printf 'version: 2\n%s\nprojects: {}\n' "$line" >"$picks/home/.tm/tm.yaml"
  printf '{"tool_name":"Edit","tool_input":{"file_path":"%s"}}' \
    "$repo/packages/a.dart" |
    env -u TM_BIN ${dev:+TM_BIN="$dev"} HOME="$picks/home" \
      PATH="$picks/path:$PATH" CLAUDE_PROJECT_DIR="$repo" \
      bash "$hook" >/dev/null 2>&1
  cat "$picks/ran" 2>/dev/null || echo none
}
picked() {
  local label=$1 want=$2 got=$3
  if [ "$got" = "$want" ]; then
    pass=$((pass + 1))
    echo "ok   $label"
  else
    fail=$((fail + 1))
    echo "FAIL $label — wanted $want, ran $got"
  fi
}
hooks_dir=$(dirname "$hook")
for adapter in require-tm-claim.sh tm-hook.sh report-bash-writes.sh \
  consolidate-when-ready.sh; do
  a="$hooks_dir/$adapter"
  picked "$adapter: TM_BIN over the registry" dev \
    "$(ran "$a" "tm: \"$picks/registered tm\"" "$picks/dev-tm")"
  picked "$adapter: the registry's over the installed one" registered \
    "$(ran "$a" "tm: \"$picks/registered tm\" # laid down by xtask" "")"
  picked "$adapter: a plain value, \`~/\` read as the home, a comment after" \
    home \
    "$(ran "$a" 'tm: ~/bin/tm # by hand' "$picks/nothing-here")"
  # Nothing runnable where the key points: the step after it, which `tm
  # doctor` names as an error.
  picked "$adapter: a key naming nothing falls through" installed \
    "$(ran "$a" "tm: $picks/gone/tm" "")"
  picked "$adapter: a key under another one is not the key" installed \
    "$(ran "$a" "judge:
  tm: '$picks/registered tm'" "")"
done
# The steps are in `tm-lib.sh`, which every adapter sources from beside
# itself. One laid down without it finds no `tm` and stands down as it does
# where there is none: nothing runs, and the call goes through.
alone="$work/alone"
mkdir -p "$alone"
for adapter in require-tm-claim.sh tm-hook.sh report-bash-writes.sh \
  consolidate-when-ready.sh; do
  cp "$hooks_dir/$adapter" "$alone/$adapter"
  picked "$adapter: without tm-lib.sh beside it, no tm runs" none \
    "$(ran "$alone/$adapter" "tm: \"$picks/registered tm\"" "$picks/dev-tm")"
  picked "$adapter: and the call goes through" 0 \
    "$(printf '{"tool_name":"Edit","tool_input":{"file_path":"%s"}}' \
      "$repo/packages/a.dart" |
      env TM_BIN="$picks/dev-tm" HOME="$picks/home" \
        CLAUDE_PROJECT_DIR="$repo" bash "$alone/$adapter" >/dev/null 2>&1
      echo $?)"
done
# The three that speak up say what is missing — this file, not the binary —
# and the install that lays it down, for the harness the registration names.
for adapter in require-tm-claim.sh tm-hook.sh report-bash-writes.sh; do
  said=$(printf '{"tool_name":"Edit","tool_input":{"file_path":"%s"}}' \
    "$repo/packages/a.dart" |
    env TM_BIN="$picks/dev-tm" HOME="$picks/home" \
      CLAUDE_PROJECT_DIR="$repo" bash "$alone/$adapter" codex 2>&1 >/dev/null)
  case $said in
  *"$alone/tm-lib.sh is missing"*"tm skills install --harness codex"*)
    got=named ;;
  *) got="said: $said" ;;
  esac
  picked "$adapter: without tm-lib.sh, says which file and which install" \
    named "$got"
done
mv "$picks/home/.local/bin/tm" "$picks/installed-aside"
picked "no registry key and none installed: PATH" onpath \
  "$(ran "$hook" '' "")"

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
