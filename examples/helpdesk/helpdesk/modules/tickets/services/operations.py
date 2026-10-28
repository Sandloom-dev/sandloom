"""Declared read operations of the tickets module (execution planner).

The module states WHAT it reads; the kernel's planner decides HOW (a generated
statement on the optimized executor in a read-only transaction, or the ORM
general form). Only the tickets module's own entity is referenced.
"""

from sandloom.sdk.data import ListOperation  # sandloom: advanced declared planner read

from ..models import Ticket
from ..schemas import TicketOut

#: Upper bound of one ticket list page (the routes' ``limit`` caps).
MAX_TICKETS_PAGE = 100

#: ``GET /orgs/{id}/tickets`` (and the export): an organization's newest
#: tickets — ``ORDER BY created_at DESC`` with the primary key as the
#: tie-breaker, served by ``ix_tickets_tickets_org_created_id``.
tickets_of_org = ListOperation(
    entity=Ticket,
    result=TicketOut,
    by=("organization_id",),
    order_by=("created_at",),
    descending=True,
    max_limit=MAX_TICKETS_PAGE,
    name="tickets.of_org.newest",
)

#: Everything the module declares (registered at startup, see ``__init__``).
OPERATIONS = (tickets_of_org,)

__all__ = ["MAX_TICKETS_PAGE", "OPERATIONS", "tickets_of_org"]
