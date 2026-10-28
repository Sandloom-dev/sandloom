### `enrollment` — paid enrollments

A learner (`learner_id`, a user id) enrolls in a course of the current
organization. Enrolling charges the course's `price_credits` to the
organization's credit balance in billing — exactly once per enrollment.

| method | path | body / query | answer |
|---|---|---|---|
| POST | `/api/v1/enrollment/enrollments` | `{course_id, learner_id}` | `201` `{id, course_id, learner_id, status}` with `status = "active"` |
| GET | `/api/v1/enrollment/enrollments` | `?course_id=` (optional) | `200` list |

- `404` when the course is not in the organization.
- `402` when the organization lacks the credits; nothing is enrolled.
- `409` when the learner is already enrolled in that course, or the course
  is at capacity.
- Other parts of the product react to a new enrollment.
