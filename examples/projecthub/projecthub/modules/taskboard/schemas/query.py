"""Taskboard read-model DTOs (ORM-free)."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TaskboardRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: UUID
    task_title: str
    task_status: str
    project_id: UUID
    project_name: str
    project_status: str
