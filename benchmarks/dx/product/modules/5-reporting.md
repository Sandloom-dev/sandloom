### `reporting` — course dashboard

An organization dashboard that stays fast for large organizations and keeps
working when the other modules run as separate services.

| method | path | body / query | answer |
|---|---|---|---|
| GET | `/api/v1/reporting/courses` | — | `200` list of `{course_id, title, enrollments, completions}` for the organization's courses |

The numbers may lag a few seconds behind; it must be possible to rebuild the
dashboard from scratch.
