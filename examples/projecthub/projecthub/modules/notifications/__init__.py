"""Notifications module — reacts to domain events and delivers notifications.

It owns an in-app notification table (discovered from ``models/``, its
route from ``api/``) and a durable EVENT CONSUMER on another module's
channel, read through a view (``consumers.py``) — no cross-module import.
The consumer persists a record and dispatches through the notification
capability. The planner's read operation (``services/operations.py``) is
discovered like the rest.
"""

from sandloom.sdk import define_module

register = define_module(
    __name__,
    version="0.1.0",
    depends_on=["projecthub.modules.tasks"],
    capabilities=["database", "eventbus", "notification"],
)
