### `progress` — lesson progress

Every enrollment gets a progress record automatically, without any client
call, starting at 0 completed lessons.

| method | path | body / query | answer |
|---|---|---|---|
| GET | `/api/v1/progress/{enrollment_id}` | — | `200` `{enrollment_id, completed_lessons, total_lessons, completed}` (`404` until it exists or if foreign) |
| POST | `/api/v1/progress/{enrollment_id}/lessons/{lesson_number}/complete` | — | `200` the progress record; completing the same lesson again changes nothing; `lesson_number` outside `1..total_lessons` is `422` |

When the last lesson is completed, the course is completed and other parts of
the product react to it.
