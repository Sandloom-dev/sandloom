"""Projects ORM models.

Table names must be prefixed with `projects_`. Reference entities owned by
other modules by UUID only — cross-module foreign keys are forbidden (they
block extraction into a standalone service).
"""

from .project import Project

__all__ = ["Project"]
