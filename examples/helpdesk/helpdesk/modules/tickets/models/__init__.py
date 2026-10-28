"""Tickets ORM models.

Table names must be prefixed with `tickets_`. Reference entities owned by
other modules by UUID only — cross-module foreign keys are forbidden (they
block extraction into a standalone service).
"""

from .ticket import TICKET_PRIORITIES, TICKET_STATUSES, Ticket

__all__ = ["Ticket", "TICKET_STATUSES", "TICKET_PRIORITIES"]
