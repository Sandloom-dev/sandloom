"""Taskboard projection models (table prefix ``taskboard_``)."""

from .project_snapshot import ProjectSnapshot
from .query_index import ProjectTaskQueryIndex

__all__ = ["ProjectSnapshot", "ProjectTaskQueryIndex"]
