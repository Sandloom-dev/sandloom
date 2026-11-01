#!/bin/bash
# What `report-bash-writes.sh` does, pinned — end to end, through the
# installed `tm`.
#
# The same reason its sibling has a test: a hook fails by silence, and a hook
# that never fires is indistinguishable from a hook with nothing to complain
# about. This one is worse than most in that respect — it reports rather than
# refuses, so a broken version costs nothing visible at all.
#
# Self-contained: its own home and the store `tm` keeps in it, its own git
# checkout under a temp directory, and a recording guard standing in for a
# project's own. What is pinned is the mechanism — did the right files reach
# the guards, dressed the right way — and not what the guards then say, which
# their own tests cover.
#
#   .claude/hooks/report-bash-writes.test.sh
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

hook="$(cd "$(dirname "$0")" && pwd)/report-bash-writes.sh"

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
mkdir -p "$repo/packages"
git -C "$repo" init -q 2>/dev/null
git -C "$repo" config user.email t@t
git -C "$repo" config user.name t
echo base >"$repo/packages/tracked.txt"
git -C "$repo" add -A && git -C "$repo" commit -qm base

# Stands in for a project's own guard, at a path no hard-coded list would
# guess: complains about every file it is given and records which ones, so a
# case can assert on the set that reached it.
#
# It reads the path where the harness nests it — under `tool_input` — and
# nowhere else. A replay that pasted `file_path` at the top level would be a
# payload no guard written against the harness reads, and this guard would
# see nothing. It records the path with `/` for every separator: on Windows
# the checkout's root arrives in the machine's `\`, which JSON escapes, and the
# file is the same file whichever way it is spelled.
seen="$work/seen.txt"
mkdir -p "$repo/apps/somewhere/.claude/hooks"
cat >"$repo/apps/somewhere/.claude/hooks/guard-wired-files.sh" <<EOF
#!/bin/bash
cat | grep -o '"tool_input":{"file_path":"[^"]*"' |
  sed 's/.*"file_path":"\\(.*\\)"/\\1/; s#\\\\\\\\#/#g' >>"$seen"
echo "wired guard says no" >&2
exit 2
EOF
chmod +x "$repo/apps/somewhere/.claude/hooks/guard-wired-files.sh"

# A guard's own test matches `require-*.sh`, is executable, and must never be
# handed a replayed write as though it were the guard it tests.
mkdir -p "$repo/.claude/hooks"
cat >"$repo/.claude/hooks/require-tm-claim.test.sh" <<EOF
#!/bin/bash
echo "the test script was invoked as a guard" >>"$seen"
EOF
chmod +x "$repo/.claude/hooks/require-tm-claim.test.sh"

pass=0
fail=0

ok() {
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
    echo "ok   $1"
  else
    fail=$((fail + 1))
    echo "FAIL $1 — wanted [$3], got [$2]"
  fi
}

payload() {
  printf '{"tool_name":"%s","hook_event_name":"%s","session_id":"%s","cwd":"%s"}' \
    "$1" "$2" "$3" "$repo"
}

# One command's worth of the hook: snapshot, then whatever `$1` does, then the
# comparison — exactly the Pre/Post pair the harness fires. `$2` is the
# session, which is what the claim guard asks about.
run() {
  local session=${2:-s1}
  : >"$seen"
  payload Bash PreToolUse "$session" |
    TM_BIN="$tm" bash "$hook" >/dev/null 2>&1
  eval "$1"
  payload Bash PostToolUse "$session" |
    TM_BIN="$tm" bash "$hook" >"$work/out" 2>"$work/err"
  echo $?
}

# ── the cases that must stay quiet ───────────────────────────────────────
ok "a command that writes nothing says nothing" "$(run ':')" 0
ok "  and reaches no guard" "$(wc -l <"$seen" | tr -d ' ')" 0

payload Edit PostToolUse s1 | TM_BIN="$tm" bash "$hook" >/dev/null 2>&1
ok "a non-Bash tool is ignored" "$?" 0

payload Bash PostToolUse nope | TM_BIN="$tm" bash "$hook" >/dev/null 2>&1
ok "no snapshot → silence, not a report of everything dirty" "$?" 0

# ── the case it exists for ───────────────────────────────────────────────
ok "a new file is caught" "$(run 'echo x >"$repo/packages/new.txt"')" 2
ok "  and the project's guard was given it, nested as the harness nests it" \
  "$(cat "$seen")" "$repo/packages/new.txt"
ok "  and the report names the guard" \
  "$(grep -c 'guard-wired-files.sh' "$work/out")" 1
ok "  as a block the harness puts in front of the model" \
  "$(grep -c '"decision":"block"' "$work/out")" 1

ok "an edit to a tracked file is caught" \
  "$(run 'echo more >>"$repo/packages/tracked.txt"')" 2
ok "  and the guard was given it" "$(cat "$seen")" "$repo/packages/tracked.txt"

ok "  the guard's own test was never invoked as a guard" \
  "$(grep -c 'invoked as a guard' "$seen")" 0

git -C "$repo" add -A && git -C "$repo" commit -qm caught

# The status column changes when a file goes from untracked to added; only the
# path may be compared, or the same file reads as a second write.
echo y >"$repo/packages/staged.txt"
git -C "$repo" add packages/staged.txt
ok "staging alone is not a new write" \
  "$(run 'git -C "$repo" status >/dev/null')" 0

# An untracked directory is one line in the default porcelain and a line per
# file once staged, so the path set would change with nothing written.
mkdir -p "$repo/packages/fresh/lib"
echo a >"$repo/packages/fresh/lib/one.dart"
echo b >"$repo/packages/fresh/lib/two.dart"
ok "staging a whole new directory is not two new writes" \
  "$(run 'git -C "$repo" add packages/fresh')" 0
ok "  and no guard was troubled" "$(wc -l <"$seen" | tr -d ' ')" 0

git -C "$repo" commit -qm fresh
ok "a command creating several files names each one" \
  "$(run 'mkdir -p "$repo/packages/more" && echo a >"$repo/packages/more/x.dart" && echo b >"$repo/packages/more/y.dart"')" 2
ok "  both of them" "$(sort "$seen" | tr '\n' ' ')" \
  "$repo/packages/more/x.dart $repo/packages/more/y.dart "

# ── what it must not mangle ──────────────────────────────────────────────
ok "a path with a space survives the round trip" \
  "$(run 'echo x >"$repo/packages/two words.txt"')" 2
ok "  intact" "$(cat "$seen")" "$repo/packages/two words.txt"

# `core.quotePath` is on by default, so a non-ASCII name comes back from the
# plain porcelain as `"\303\234..."` — and a guard handed a path that does not
# exist reports nothing wrong, which is indistinguishable from a clean write.
ok "a non-ASCII path is not mangled into octal" \
  "$(run 'echo x >"$repo/packages/Übersicht.md"')" 2
ok "  intact" "$(cat "$seen")" "$repo/packages/Übersicht.md"

git -C "$repo" add -A && git -C "$repo" commit -qm paths

# ── what an exit 1 is ────────────────────────────────────────────────────
# Two different things exit 1: tm answering with a warning — what a codec
# says where its harness cannot do what the rules asked, a refusal after a
# tool under agy — and a tm that could not answer at all, one that predates
# the verb or one that threw. The first goes on as it came; the second is
# said, and degrades open. Pinned for both adapters that hand a call to
# `tm hook`, against stand-ins for each kind of binary; the codecs' side —
# that every warning opens with the words matched — is pinned with tm's own
# tests (`hook_warning_opening_test.dart`).
#
# Each case twice: stderr passed on as tm writes it, with the copy the exit 1
# is read off in a file of its own, and — with no `mktemp` to make that file —
# stderr held until tm is done, as before the copy. The file is made under a
# TMPDIR of the suite's, which must be empty again however the script ended.
stub() {
  cat >"$work/stub-$1" <<EOF
#!/bin/sh
cat >"$work/stub-stdin"
$2
EOF
  chmod +x "$work/stub-$1"
}
stub warns 'echo "tm: this harness does not let a hook stop PostToolUse. The reason was: elsewhere" >&2; exit 1'
stub predates 'echo "Could not find a command named \"hook\"." >&2; echo >&2; echo "Usage: tm <command>" >&2; exit 1'
stub threw 'echo "error: Bad state: no element" >&2; exit 1'
stub refuses 'echo "{\"decision\":\"block\"}"; echo "the reason" >&2; exit 2'
# A tm older than the harness: its usage mistake, exit 2 with nothing on
# stdout — a code that is a refusal on every harness, and no refusal.
stub unknown 'echo "tm hook: unknown harness \"agy\" - one of claude-code" >&2; exit 2'
# Its wording before the verb had its prefix, the event's this time.
stub unprefixed 'echo "unknown hook event \"post-tool-use\" - one of pre-tool-use" >&2; exit 2'
# Refusals with those words in their reason, as a quoted path brings them:
# one opening with them, with its JSON on stdout as every refusal has, and
# one with nothing on stdout, where they are not its first words.
stub quotes 'echo "{\"decision\":\"block\"}"; echo "unknown harness \"agy\".dart is not yours" >&2; exit 2'
stub mute 'echo "the reason names tm hook: unknown harness \"agy\"" >&2; exit 2'
stub notes 'echo "{}"; echo "a notice" >&2; exit 0'
# stdout is read for whether there is any, and must come through as it was,
# down to a first byte the shell cannot hold.
stub nul 'printf "\0{}"; exit 0'
# tm gone, its words not yet: a child it left behind still holds its stderr,
# and says them last. The copy is read once all of stderr has gone through.
stub late '(sleep 0.5; echo "tm: this harness does not let a hook stop it." >&2) & exit 1'
# And one that holds stderr alone, so tm's stdout is done before it is.
stub orphan '(sleep 0.5; echo "tm: this harness does not let a hook stop it." >&2) >/dev/null & exit 1'
stub hangs 'echo $$ >"'"$work"'/hang.pid"; echo "tm: still waiting for the store" >&2; exec sleep 30'
held="$work/no-mktemp"
mkdir -p "$held"
printf '#!/bin/sh\nexit 1\n' >"$held/mktemp"
chmod +x "$held/mktemp"
for adapter in report-bash-writes.sh tm-hook.sh; do
  a="$(dirname "$hook")/$adapter"
  for mode in copied held; do
    path=$PATH
    [ "$mode" = held ] && path="$held:$PATH"
    via() {
      rm -rf "$work/tmp" && mkdir "$work/tmp"
      payload Bash PostToolUse s1 |
        PATH="$path" TMPDIR="$work/tmp" TM_BIN="$work/stub-$1" \
          bash "$a" agy post-tool-use >"$work/out" 2>"$work/err"
      echo $?
    }
    left() { ls -A "$work/tmp"; }
    ok "$adapter ($mode): tm's warning goes on with its exit 1" "$(via warns)" 1
    ok "  its line intact" "$(cat "$work/err")" \
      "tm: this harness does not let a hook stop PostToolUse. The reason was: elsewhere"
    ok "  and alone" "$(wc -l <"$work/err" | tr -d ' ')" 1
    ok "  and stdin reached tm" "$(grep -c '"session_id":"s1"' "$work/stub-stdin")" 1
    ok "  and no copy is left behind" "$(left)" ""
    ok "$adapter ($mode): words tm's stderr carries after tm are read too" \
      "$(via late)" 1
    ok "  and passed on" "$(cat "$work/err")" \
      "tm: this harness does not let a hook stop it."
    ok "$adapter ($mode): and so are those of one holding stderr alone" \
      "$(via orphan)" 1
    ok "  passed on" "$(cat "$work/err")" \
      "tm: this harness does not let a hook stop it."
    ok "$adapter ($mode): a tm that predates the verb is no answer" \
      "$(via predates)" 0
    ok "  and says so" "$(grep -c 'could not answer (exit 1)' "$work/err")" 1
    ok "  after all tm said, in its order" "$(head -n 3 "$work/err")" \
      "$(printf 'Could not find a command named "hook".\n\nUsage: tm <command>')"
    ok "  and no copy is left behind" "$(left)" ""
    ok "$adapter ($mode): a tm that threw is no answer either" "$(via threw)" 0
    ok "  and says so" "$(grep -c 'could not answer (exit 1)' "$work/err")" 1
    ok "$adapter ($mode): a tm that does not know the harness is no answer" \
      "$(via unknown)" 0
    ok "  and says so" "$(grep -c 'does not know' "$work/err")" 1
    ok "$adapter ($mode): nor one that said so before its prefix" \
      "$(via unprefixed)" 0
    ok "  and says so" "$(grep -c 'does not know' "$work/err")" 1
    ok "$adapter ($mode): a refusal opening with those words is a refusal" \
      "$(via quotes)" 2
    ok "  stdout to stdout" "$(cat "$work/out")" '{"decision":"block"}'
    ok "  stderr to stderr" "$(cat "$work/err")" \
      'unknown harness "agy".dart is not yours'
    ok "$adapter ($mode): and so is one quoting them, with nothing on stdout" \
      "$(via mute)" 2
    ok "  stderr to stderr" "$(cat "$work/err")" \
      'the reason names tm hook: unknown harness "agy"'
    ok "$adapter ($mode): a refusal goes on with its exit 2" "$(via refuses)" 2
    ok "  stdout to stdout" "$(cat "$work/out")" '{"decision":"block"}'
    ok "  stderr to stderr" "$(cat "$work/err")" "the reason"
    ok "$adapter ($mode): a pass goes on with its exit 0" "$(via notes)" 0
    ok "  stdout to stdout" "$(cat "$work/out")" '{}'
    ok "  stderr to stderr, saying nothing more" "$(cat "$work/err")" "a notice"
    ok "  and no copy is left behind" "$(left)" ""
    ok "$adapter ($mode): stdout goes on byte for byte" "$(via nul)" 0
    ok "  a NUL first included" "$(od -An -c "$work/out" | tr -d ' \n')" '\0{}'
  done

  # What the copy is for: a harness kills a hook that outlives its timeout,
  # and a `tm hook` hung on its store is one. What tm had said by then has
  # gone on already, the script dies of the signal at once rather than when
  # tm is done, and takes its copy with it.
  rm -rf "$work/tmp" && mkdir "$work/tmp"
  rm -f "$work/hang.pid"
  payload Bash PostToolUse s1 >"$work/payload"
  TMPDIR="$work/tmp" TM_BIN="$work/stub-hangs" \
    bash "$a" agy post-tool-use <"$work/payload" >"$work/out" 2>"$work/err" &
  hooked=$!
  for _ in $(seq 100); do
    grep -q 'still waiting' "$work/err" 2>/dev/null && break
    sleep 0.1
  done
  since=$SECONDS
  kill -s TERM "$hooked"
  wait "$hooked"
  ok "$adapter: killed while tm hangs, it dies of the signal" "$?" 143
  ok "  at once, not when tm is done" "$((SECONDS - since < 10))" 1
  ok "  having passed on what tm said by then" "$(cat "$work/err")" \
    "tm: still waiting for the store"
  ok "  and no copy is left behind" "$(ls -A "$work/tmp")" ""
  [ -s "$work/hang.pid" ] && kill "$(cat "$work/hang.pid")" 2>/dev/null
done

# ── the claim guard, in the same process ─────────────────────────────────
# The project's own guard above is a script; tm's own guard is a rule that
# runs inside `tm hook`. Both have to see the write, and the second is the
# one a repository actually leans on.
rm "$repo/apps/somewhere/.claude/hooks/guard-wired-files.sh"

project=$("$tm" project create "Reporter Fixture" 2>/dev/null)
if [ -z "$project" ]; then
  echo "FAIL could not create the fixture project" >&2
  exit 1
fi
(cd "$repo" && "$tm" project use "$project" >/dev/null 2>&1)
git -C "$repo" add -A && git -C "$repo" commit -qm marker

"$tm" actor register alice --project "$project" >/dev/null 2>&1
"$tm" actor attach alice --project "$project" --token s1 \
  --workspace "$repo" >/dev/null 2>&1
task=$("$tm" task create "Scoped work" --project "$project" \
  --area packages/mine 2>/dev/null)
"$tm" task start "$task" --actor alice >/dev/null 2>&1

ok "a write inside the claimed area passes" \
  "$(run 'mkdir -p "$repo/packages/mine" && echo x >"$repo/packages/mine/a.dart"')" 0

ok "a write outside it is reported through the claim guard" \
  "$(run 'echo x >"$repo/packages/elsewhere.dart"')" 2
ok "  naming the guard" "$(grep -c 'tm claim guard' "$work/out")" 1
ok "  and the area" "$(grep -c 'outside the area' "$work/out")" 1

ok "the same write under a session holding no actor is reported too" \
  "$(run 'echo y >"$repo/packages/mine/b.dart"' s2)" 2
ok "  as a session that holds no actor" \
  "$(grep -c 'holds no actor' "$work/out")" 1

"$tm" actor register bob --project "$project" >/dev/null 2>&1
"$tm" actor attach bob --project "$project" --token s3 \
  --workspace "$repo" >/dev/null 2>&1
ok "and under a session holding an actor but no claim" \
  "$(run 'echo z >"$repo/packages/mine/c.dart"' s3)" 2
ok "  as work the tracker never saw" \
  "$(grep -c 'Nothing this session holds' "$work/out")" 1

# ── agy, whose payload names no event ────────────────────────────────────
# agy's registration names the event after the harness, and the script hands
# it on: without it the pair is no pair, and the write goes unreported. After
# a tool agy reads no answer, so the report is a warning on stderr — what
# agy keeps in its log.
agy_payload() {
  printf '{"conversationId":"%s","workspacePaths":["%s"],"toolCall":{"name":"run_command","args":{"CommandLine":"%s"}}%s}' \
    "$1" "$repo" "$2" "$3"
}
: >"$seen"
agy_payload s1 write '' | TM_BIN="$tm" bash "$hook" agy pre-tool-use \
  >/dev/null 2>&1
echo x >"$repo/packages/agy-elsewhere.dart"
agy_payload s1 write ',"error":""' |
  TM_BIN="$tm" bash "$hook" agy post-tool-use >"$work/out" 2>"$work/err"
code=$?
ok "under agy a write outside the area reaches the claim guard" \
  "$(grep -c 'outside the area' "$work/err")" 1
ok "  and nothing on stdout" "$(cat "$work/out")" ""
ok "  with the exit 1 agy's codec answered it with" "$code" 1
ok "  not reported as a tm that could not answer" \
  "$(grep -c 'could not answer' "$work/err")" 0

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
