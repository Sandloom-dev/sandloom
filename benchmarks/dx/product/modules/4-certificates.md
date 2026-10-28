### `certificates` — completion certificates

When a learner completes a course, they receive exactly one certificate and
an email telling them so (use the platform's notification capability).

| method | path | body / query | answer |
|---|---|---|---|
| GET | `/api/v1/certificates` | `?learner_id=` (optional) | `200` list of `{id, course_id, learner_id, issued_at}` |
