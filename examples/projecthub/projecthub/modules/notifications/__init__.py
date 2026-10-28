"""Notifications module — reacts to domain events and delivers notifications.

It owns an in-app notification table (discovered from ``models/``, its
route from ``api/``) and a durable EVENT CONSUMER on another module's
channel, read through a view (``consumers.py``) — no cross-module import.
The consumer persists a record and dispatches through the notification
capability. A short ``setup`` registers what has no declaration yet: the
consumer in its L2 form (it writes in the delivery's session, which
``@on`` reaches only through ``db()``) and the planner's read operations.
"""

from sandloom.sdk import define_module
from sandloom.sdk.module import ModuleAPI  # sandloom: advanced setup registers L2 parts

from .consumers import CONSUMER_NAME, TaskCreated, on_task_created
from .services.operations import OPERATIONS


def _setup(core: ModuleAPI) -> None:
    core.register_event_consumer(
        TaskCreated.channel, on_task_created, name=CONSUMER_NAME
    )
    # Declared planner reads (the in-app list): planned at boot.
    core.register_operations(*OPERATIONS)


register = define_module(
    __name__,
    version="0.1.0",
    depends_on=["projecthub.modules.tasks"],
    capabilities=["database", "eventbus", "notification"],
    setup=_setup,
)
