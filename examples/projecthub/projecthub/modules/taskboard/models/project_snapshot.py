"""Local snapshot of each project's denormalised fields (taskboard-owned).

The project fields on ``project_task_query_index`` rows are copied from HERE,
never straight from an RPC read: the snapshot row is the single,
version-guarded (``version``) source of truth inside the read model and its row
lock serialises the two writers that touch a project's fields —

* ``on_project_updated`` upserts the snapshot (row lock) and then fans the new
  values out over the project's task rows;
* ``on_task_upserted`` locks the snapshot (``SELECT ... FOR UPDATE``) and copies
  its CURRENT values into the task row it inserts/updates.

Whichever commits first, the other observes it: a task inserted while a project
event is in flight either waits for that event's commit (and copies the new
values) or commits first (and the event's fan-out UPDATE, issued after it
acquired the lock, sees the committed row). Without the snapshot a task handler
could read the project over RPC, lose the race to a project event whose UPDATE
matched no row yet, and insert stale project data that nothing would ever fix.
"""

import uuid

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, TenantScoped


class ProjectSnapshot(TenantScoped, Base):
    __tablename__ = "taskboard_project_snapshot"

    project_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
