# Work that was sent back

Read this when `task_start` (or `memory_task_start`) answers with a `review`
whose verdict sent the work back, or with `threads` waiting on you.

A rejected review hands the task back to the submitter's role, reserved: no
session holds it until one of the role calls `task_start` on it, which takes
it under a new epoch. It does not count against your one claim until you take
it. Read before that call, with `memory_task_start`, or from the reply to it.
The reply's `review` is the last round of review: the verdict, its reason, and
the comments written with it, each under an id, anchored to a `path` and
`line` of a `commit` or to the review as a whole. `get_task` has every round
under `reviewRounds`; `tm show` prints them.

Answer every comment with `review_comment_reply` (`tm task reply <task>
<comment-id> --body …`): what you changed for it and where, or why you did
not. Answer it when the change is made, not all at once at the end — the
answer is what the reviewer reads beside the new diff, and a comment left
without one reads as missed. Disagreeing is an answer; silence is not.

The reviewer can answer your answer. They read every round's comments on the
lines of the diff and reply on the comment the conversation is about, which is
seldom the latest round's — so those replies are not in `review`. They are in
the reply's `threads`: each earlier comment whose last word is not yours, with
its round and every reply in order. Treat a reply to your answer as the
comment it continues. Change what it asks and answer again on the same
comment, or answer why not. A thread stays in `threads` until the last word
in it is yours, or until a person resolves it.

A resolved thread is settled: its comment carries `resolved` (who, when) in
`review` and in `get_task`, and it is no longer waiting on you. Resolving is a
person's call and not yours — `tm task resolve` refuses an agent's door with
`HUMAN_DOOR`. Settling remarks on your own work is the self-acceptance a
review exists against. Answer the comment; the reviewer resolves it.
