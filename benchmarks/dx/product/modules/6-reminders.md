### `reminders` — nudges for stalled learners

Every night, email each learner whose active, not completed enrollment had
no lesson completed for 7 days. At most one reminder per enrollment per day,
even if the job runs twice or a worker dies halfway.

| method | path | body / query | answer |
|---|---|---|---|
| GET | `/api/v1/reminders/sent` | `?enrollment_id=` (optional) | `200` list of `{enrollment_id, sent_on}` |
