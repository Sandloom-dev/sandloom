# What tm's hook scripts do alike, sourced by each of them and run by none.
#
# `tm skills install` lays the hooks directory down whole, so this file is
# beside every script that reads it, under every harness whose hooks are
# scripts. A script that does not find it runs no `tm`, and the guards say
# so by this file's name and the install that lays it down again, since what
# is missing then is a file of the install's rather than the binary.
#
# Sets `tm`: the binary to hand the payload to, or empty where there is none
# to run. Defines `relay`, for a script that reads what tm said.

# The tm the machine's registry names, `tm:` at the top of ~/.tm/tm.yaml,
# which the installers write: read as the one line of that key, without a
# YAML parser — a value plain or quoted, a comment after it, `~/` read as the
# home.
registered=$(sed -n 's/^tm:[[:space:]]*//p' "$HOME/.tm/tm.yaml" 2>/dev/null |
  head -n 1)
case "$registered" in
\"*) registered=${registered#\"}; registered=${registered%%\"*} ;;
\'*) registered=${registered#\'}; registered=${registered%%\'*} ;;
*) registered=${registered%%[[:space:]]#*} ;;
esac
registered=${registered%"${registered##*[![:space:]]}"}
case "$registered" in
"~/"*) registered="$HOME/${registered#\~/}" ;;
esac

# The freshly built binary first: a `dart install` shim may sit earlier on
# PATH, and one built from an older tree refuses a log with an event type it
# predates — which a guard would report as breakage on every call.
# Then the registry's, the installed one, and PATH, in that order.
if [ -n "${TM_BIN:-}" ] && [ -x "${TM_BIN}" ]; then
  tm="$TM_BIN"
elif [ -x "$registered" ]; then
  tm="$registered"
elif [ -x "$HOME/.local/bin/tm" ]; then
  tm="$HOME/.local/bin/tm"
else
  tm=$(command -v tm 2>/dev/null)
fi

# Runs its arguments, and sets `code` to their status, `said` to what they
# wrote on stderr, and `wrote` to whether they wrote on stdout at all: `yes`
# or `no`.
#
# The status on its own line, because `if ! cmd; then code=$?` reads the
# status of the negation rather than of the command, and that is a silent 0.
# stdout goes straight through, by way of `pass_on`, which says whether
# there was any. So does stderr, line by line as tm writes it, with a copy
# kept in a file of its own, because what an exit 1 means is read off it by
# the script: a hook the harness kills on its timeout — a `tm hook` hung on
# its store — has passed on whatever tm said by then, where one that held it
# until tm was done passed on nothing.
#
# The copy needs `mktemp`, `tee`, and a bash that can wait for `tee` (4.4):
# read before `tee` has written it, it would miss tm's words. Without them
# stderr is held until tm is done and passed on whole, as before, on a
# descriptor kept aside for stdout.
relay() {
  copy=
  case "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}" in
  [0-3].* | 4.[0-3]) ;;
  *) command -v tee >/dev/null 2>&1 && copy=$(mktemp 2>/dev/null) ;;
  esac
  if [ -n "$copy" ] && [ -f "$copy" ]; then
    # Gone however the script ends: bash runs an EXIT trap when a signal ends
    # it too, the TERM of a timeout among them. A trap on the signal itself
    # would not do, since bash runs one only once tm is done — and keeps the
    # timeout that sent it waiting with it.
    trap 'rm -f "$copy"' EXIT
    # In a subshell, which waits for `tee` before it is done — `tee` may
    # still be writing when tm is gone — so the copy is whole once the
    # pipeline is.
    (
      "$@" 2> >(tee "$copy" >&2)
      code=$?
      wait $!
      exit "$code"
    ) | pass_on
    # tm's status, then `pass_on`'s.
    set -- "${PIPESTATUS[@]}"
    code=$1
    case $2 in
    1) wrote=no ;;
    *) wrote=yes ;;
    esac
    said=$(cat "$copy")
  else
    # The substitution that holds stderr is the one way out of the pipeline,
    # so a `pass_on` that found nothing says so through it, in words of this
    # call's own that no tm writes. They fall where tm's stdout closed, which
    # a child tm left behind may outlive on stderr, so they are looked for
    # anywhere and taken out wherever they are.
    quiet="(tm-lib: nothing on stdout, $$.$RANDOM)"
    exec 3>&1
    said=$(
      {
        "$@" 2>&4 3>&- 4>&- | pass_on >&3
        set -- "${PIPESTATUS[@]}"
        [ "$2" = 1 ] && printf '%s' "$quiet"
        exit "$1"
      } 4>&1
    )
    code=$?
    exec 3>&-
    case $said in
    *"$quiet"*) wrote=no said=${said%%"$quiet"*}${said#*"$quiet"} ;;
    *) wrote=yes ;;
    esac
    # And without the newlines stderr ended with, as a substitution leaves it.
    said=$(printf '%s' "$said")
    [ -n "$said" ] && printf '%s\n' "$said" >&2
  fi
}

# Copies stdin to stdout, and fails when there was nothing to copy.
#
# The first character by `read`, which takes no more from a pipe than it
# returns, and a NUL as the empty string it reads one as; the rest by `cat`.
pass_on() {
  IFS= read -r -n 1 -d '' first || return 1
  if [ -n "$first" ]; then
    printf '%s' "$first"
  else
    printf '\0'
  fi
  cat
  return 0
}

# Whether tm's answer — `code`, `said` and `wrote`, as `relay` leaves them —
# was a usage mistake rather than a verdict: a `tm` older than the harness or
# the event says it does not know them, and older ones exit 2 for that, the
# code of a refusal. Passed on, it would refuse every call of a harness the
# binary predates, the fix included.
#
# Told from a refusal by both of two things: nothing on stdout, where every
# codec writes a refusal's JSON, and stderr opening with the verb's words —
# its wording since its first version, with or without its `tm hook:`
# prefix. Anywhere else in stderr the words prove nothing, nor at
# its start beside an answer on stdout: a refusal quotes the path or the
# command it refuses, and the claim guard's opens with the path.
unknown_to_tm() {
  [ "${code:-}" = 2 ] && [ "${wrote:-}" = no ] || return 1
  case "${said#tm hook: }" in
  'unknown harness "'* | 'unknown hook event "'*) return 0 ;;
  esac
  return 1
}
