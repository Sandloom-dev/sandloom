# Refusals that are not about your call

Read this when tm refuses a claim, an edit or a handover with one of the
codes below. Each one is an answer about something else than the arguments
you sent, and none of them is fixed by sending them again.

## `CAPABILITY_MISMATCH` on `task_start`

You do not offer capabilities; the front already knows what you can do. Your
role's record in the project's registry carries them — skills such as "can
review a design" — and both the front and `task_start` read them from there,
so a task gated on one is invisible to a role that does not have it. The
refusal is an answer about the REGISTRY: nobody has told it what the role can
do, or this work is not the role's to take. It names both sides, and the
repair is `actor_update` by whoever decides, not a re-claim with a wider list.
There is no wider list to send.

## An edit refused because no claim covers it

The claim may have run out (six hours without a tm call of the session), been
taken over by a person (`tm task take-over`), or by another session.
`tm claims-lost` says which. If the session ended — `/clear`, a restart — and
the work is still yours, `task_start` on the same task takes it back under a
new epoch; the old session is told once that its claim was superseded.

## A handover refused with `NOT_APPROVED`

Nothing you can call: a person reads the command the marker brought and runs
`tm trust` at their terminal. Say so and stop. An agent's `tm trust` is
refused, and getting past that, by a detached process or by writing
`~/.tm/trust.json`, takes the person's decision from them.
