#!/usr/bin/env bash
# Replays the file guards against what a shell command actually wrote.
#
# An adapter and nothing else: it finds `tm` and hands the harness's payload
# to `tm hook`, which reads which event this is from the payload — the same
# script runs before the command and after it. Before, the rule notes what is
# changed in the checkout; after, each file newly changed goes back through
# the claim guard and through any guard the project keeps beside it, dressed
# as the `Write` it would have been. The write has already happened, so what
# comes back is a report in front of the model rather than a refusal.
#
# Registered on `Bash` under `PreToolUse` and `PostToolUse` by
# `tm skills install`.
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
  echo "report-bash-writes: $here/tm-lib.sh is missing — shell writes go UNREPORTED;" \
    "\`tm skills install --harness $harness\` lays it down again." >&2
  exit 0
fi
. "$here/tm-lib.sh"

# Degrade open, but loudly. A reporter that cannot run stops nothing either
# way; what is not acceptable is one that stops running in silence.
if [ -z "$tm" ] || [ ! -x "$tm" ]; then
  echo "report-bash-writes: no usable tm binary — shell writes go UNREPORTED." >&2
  exit 0
fi

# stdout goes straight through, and stderr too as tm writes it, with a copy
# in `said` for what an exit 1 means below (`relay`, beside `tm`).
ask() {
  "$tm" hook "$harness" ${event:+"$event"}
}
relay ask

# 0 passes and 2 reports — both are the verb answering. So is 1 when tm says
# why: under agy, which reads no answer after a tool, the report is a warning
# with exit 1, and every one of tm's codecs opens such a line with the words
# matched here. It goes on as it came. Anything else is a binary that could
# not answer at all: one that predates the verb prints usage and exits 1,
# which is the ordinary case rather than an exotic one, because a rebuilt `tm`
# does not reach a session already running.
#
# Except a 2 that is a usage mistake (`unknown_to_tm`, beside `tm`).
if unknown_to_tm; then
  echo "report-bash-writes: this tm does not know \`$harness${event:+ $event}\`" \
    "— shell writes go UNREPORTED; a newer tm answers it." >&2
  exit 0
fi
case "$code:$said" in
0:* | 2:* | 1:*"tm: this harness does not let a hook "*) exit "$code" ;;
*)
  echo "report-bash-writes: \`tm hook\` could not answer (exit $code) —" \
    "shell writes go UNREPORTED." >&2
  exit 0
  ;;
esac
