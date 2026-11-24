"""The tickets module.

Everything it declares is discovered: routes in ``api/``, the ticket events
in ``events.py``, the table in ``models/`` and the planner's read operations
in ``services/operations.py``. Organisation membership comes from the tenancy
module's ``tenancy.membership.v1`` contract (the route gate ``OrgRole`` of its
generated stub) — never an import.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
