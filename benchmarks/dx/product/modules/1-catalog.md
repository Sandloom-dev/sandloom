### `catalog` — courses

A course belongs to an organization: `title` (1–200 chars), `lessons`
(integer ≥ 1), `price_credits` (integer ≥ 0), `capacity` (integer ≥ 1, or
`null` for unlimited).

| method | path | body / query | answer |
|---|---|---|---|
| POST | `/api/v1/catalog/courses` | `{title, lessons, price_credits, capacity}` | `201` course: `{id, title, lessons, price_credits, capacity}` |
| GET | `/api/v1/catalog/courses` | — | `200` list of the organization's courses |
| GET | `/api/v1/catalog/courses/{course_id}` | — | `200` course, `404` if not in the organization |

Other modules need a course's `lessons`, `price_credits` and `capacity`.

When a course is created, `catalog` announces it on the event channel
`catalog.course.created.v1` (the course's `id`, `title`, `lessons`,
`price_credits`, `capacity` and its organization), for modules that react to
new courses.
