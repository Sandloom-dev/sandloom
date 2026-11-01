#!/usr/bin/env bash
# Refuses an edit to a file no live claim of this session speaks for.
#
# An adapter and nothing else: it finds `tm` and hands the harness's payload
# to `tm hook`, which decodes it, asks the core what is guarded and who holds
# it, and writes the harness's own answer back. Which paths are exempt, which
# tool counts as an edit, where the checkout root is and who this session is
# all belong to layers a hook for another harness shares, so the one decision
# left here is the one no other layer can make: what to do when `tm` cannot be
# found at all.
#
# Registered on `Write`/`Edit`/`MultiEdit` by `tm skills install`. Not on
# `Bash`: a shell command carries no path to check, and `report-bash-writes.sh`
# covers shell writes by watching the effect instead.
#
# The harness is the first argument, and Claude Code when there is none: the
# registration names it, because one script serves every harness whose hooks
# are a program with JSON on stdin, and which one is calling is the one fact
# the script cannot read off that JSON.
set -u

harness=${1:-claude-code}

# Finding `tm` is what every adapter does alike, and it is in the file laid
# down beside this one.
case ${BASH_SOURCE[0]} in
*/*) here=${BASH_SOURCE[0]%/*} ;;
*) here=. ;;
esac
# Without it this script is an install missing a file, not a machine
# missing `tm`, and the fix is the install's.
if [ ! -r "$here/tm-lib.sh" ]; then
  echo "require-tm-claim: $here/tm-lib.sh is missing — allowing the edit UNCHECKED;" \
    "\`tm skills install --harness $harness\` lays it down again." >&2
  exit 0
fi
. "$here/tm-lib.sh"

# Degrade open, but loudly. Blocking every edit when `tm` is missing would
# leave nobody able to repair what is broken — and in the repository where
# `tm` itself is built, that is a deadlock worse than the gap. What is not
# acceptable is degrading in silence: a guard pointed at a path that does not
# exist stands dead that way, with nothing to say so.
if [ -z "$tm" ] || [ ! -x "$tm" ]; then
  echo "require-tm-claim: no usable tm binary — allowing the edit UNCHECKED." >&2
  exit 0
fi

# stdout goes straight through, and stderr too as tm writes it, with a copy
# in `said` and the status in `code` (`relay`, beside `tm`).
ask() {
  "$tm" hook "$harness" pre-tool-use
}
relay ask

# 0 allows and 2 refuses — both are the verb answering. Anything else is a
# binary that could not answer at all: one that crashed, or could not open
# what it reads.
#
# Except a 2 that is a usage mistake (`unknown_to_tm`, beside `tm`): passed
# on, it would refuse every edit under a harness the binary predates.
if unknown_to_tm; then
  echo "require-tm-claim: this tm does not know \`$harness\` — allowing" \
    "the edit UNCHECKED; a newer tm answers it." >&2
  exit 0
fi
case "$code:$said" in
0:* | 2:*) exit "$code" ;;
*)
  echo "require-tm-claim: \`tm hook\` could not answer (exit $code) —" \
    "allowing the edit UNCHECKED." >&2
  exit 0
  ;;
esac
