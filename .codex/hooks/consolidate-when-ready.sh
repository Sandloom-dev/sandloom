#!/usr/bin/env bash
# Hands a finished `task_done` to `tm hook`, which files a consolidation task
# when the handover leaves the project's working memory over the threshold.
#
# An adapter and nothing else: it finds `tm` and hands it the harness's
# payload. Whether this is a task boundary, whether the repository asked for
# it (`consolidate_hook: on`), whether the memory is due and whether a
# consolidation is already open are the rule's, and the rule is in the `tm`
# binary this finds.
#
# The harness is the first argument, and Claude Code when there is none.
set -u

harness=${1:-claude-code}

# Finding `tm` is what every adapter does alike, and it is in the file laid
# down beside this one.
case ${BASH_SOURCE[0]} in
*/*) here=${BASH_SOURCE[0]%/*} ;;
*) here=. ;;
esac
tm=
[ -r "$here/tm-lib.sh" ] && . "$here/tm-lib.sh"

# Quietly, where the guards beside it speak up. They shout when they cannot
# check, because a guard that silently stops guarding is worse than a missing
# one. Nothing here is guarded: a missed boundary costs one deferred
# consolidation, and the next handover catches it.
[ -n "$tm" ] && [ -x "$tm" ] || exit 0

# After the tool, nothing is refused, so whatever the verb exits with this
# exits 0 with the verb's stdout — the note that a task was filed. A binary
# that predates the verb answers with a usage error, and after a tool a 2 is
# read as a block whose text reaches the model.
"$tm" hook "$harness" post-tool-use 2>/dev/null
exit 0
