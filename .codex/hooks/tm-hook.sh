#!/usr/bin/env bash
# Hands a harness's hook call to `tm hook`, for any event.
#
# An adapter and nothing else: it finds `tm` and hands the harness's payload
# to `tm hook`, which reads which event this is from the payload, runs the
# rules that answer it, and writes the harness's own answer back. Which rule
# answers which event is the rules' own decision, so one script serves every
# event a harness has, and the installer registers it wherever a rule needs
# an event no other adapter covers.
#
# The harness is the first argument, and Claude Code when there is none: the
# registration names it, because one script serves every harness whose hooks
# are a program with JSON on stdin, and which one is calling is the one fact
# the script cannot read off that JSON. The event is the second, where the
# harness's payload does not name it — agy's does not, and its registration
# names it after the harness.
set -u

harness=${1:-claude-code}
event=${2:-}

# Finding `tm` is what every adapter does alike, and it is in the file laid
# down beside this one.
case ${BASH_SOURCE[0]} in
*/*) here=${BASH_SOURCE[0]%/*} ;;
*) here=. ;;
esac
# Without it this script is an install missing a file, not a machine
# missing `tm`, and the fix is the install's.
if [ ! -r "$here/tm-lib.sh" ]; then
  echo "tm-hook: $here/tm-lib.sh is missing — this hook is not running;" \
    "\`tm skills install --harness $harness\` lays it down again." >&2
  exit 0
fi
. "$here/tm-lib.sh"

# Degrade open, but loudly: a hook that cannot run says so once, rather than
# standing dead with nothing to show for it.
if [ -z "$tm" ] || [ ! -x "$tm" ]; then
  echo "tm-hook: no usable tm binary — this hook is not running." >&2
  exit 0
fi

# stdout goes straight through, and stderr too as tm writes it, with a copy
# in `said` for what an exit 1 means below (`relay`, beside `tm`).
ask() {
  "$tm" hook "$harness" ${event:+"$event"}
}
relay ask

# 0 and 2 are the verb answering, and so is 1 when tm says why: a codec
# answers 1 where its harness cannot do what the rules asked — a refusal
# after a tool under agy, one at the start of a session under pi — and
# every one of tm's codecs opens that line with the words matched here. The
# code goes on as it came, since under pi it is what makes the line a
# warning the person sees. Anything else is a binary that could not answer
# at all — one that predates the verb prints usage and exits 1, which is the
# ordinary case rather than an exotic one, because a rebuilt `tm` does not
# reach a session already running.
#
# Except a 2 that is a usage mistake (`unknown_to_tm`, beside `tm`).
if unknown_to_tm; then
  echo "tm-hook: this tm does not know \`$harness${event:+ $event}\` — this" \
    "call went unanswered; a newer tm answers it." >&2
  exit 0
fi
case "$code:$said" in
0:* | 2:* | 1:*"tm: this harness does not let a hook "*) exit "$code" ;;
*)
  echo "tm-hook: \`tm hook\` could not answer (exit $code) — this call went" \
    "unanswered." >&2
  exit 0
  ;;
esac
