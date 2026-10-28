"""Links ORM models.

Table names must be prefixed with `links_`. Reference entities owned by
other modules by UUID only — cross-module foreign keys are forbidden (they
block extraction into a standalone service).
"""

from .link import Shortlink

__all__ = ["Shortlink"]
