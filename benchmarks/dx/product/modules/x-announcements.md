### `announcements` — organization announcements

Organization members post announcements; when a course is created, an
announcement "New course: <title>" is posted automatically (`catalog`
announces new courses on `catalog.course.created.v1`).

| method | path | body / query | answer |
|---|---|---|---|
| POST | `/api/v1/announcements` | `{text}` (1–500 chars) | `201` `{id, text, created_at}` |
| GET | `/api/v1/announcements` | — | `200` list, newest first |
