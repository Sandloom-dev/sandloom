"""The kernel's tables identity's tests read.

Their models are the kernel's, which a module test does not import. Written
with SQLAlchemy Core, the SQL the tests run is every database's
(SANDLOOM-C031), and the column types bind and read the values (a UUID, JSON)
the same on each.
"""

from sqlalchemy import JSON, String, Uuid, column, table

AUDIT_LOG = table(
    "audit_log",
    column("action", String),
    column("actor_id", Uuid),
    column("actor_type", String),
    column("resource_id", String),
    column("data", JSON),
    column("occurred_at"),
)
