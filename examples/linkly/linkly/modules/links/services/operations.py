"""Declared read operations of the links module (execution planner)."""

from sandloom.sdk.data import ReadOperation  # sandloom: advanced declared planner read

from ..models import Shortlink
from ..schemas import LinkTargetOut

#: ``GET /api/v1/links/{code}`` on a cache miss: the code's target URL
#: (``code`` is unique, so the read proves at most one row).
link_target_by_code = ReadOperation(
    entity=Shortlink,
    result=LinkTargetOut,
    by=("code",),
    name="links.target.by_code",
)

#: Everything the module declares (registered at startup, see ``__init__``).
OPERATIONS = (link_target_by_code,)

__all__ = ["OPERATIONS", "link_target_by_code"]
