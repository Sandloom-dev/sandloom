"""Taskboard read module — owns the ``project_task_query_index`` projection.

It is a pure READ model: it owns no domain entity, only a denormalised index
built asynchronously from the projects/tasks modules' events (and rebuildable
through their read contracts). It never imports those modules: it declares
VIEWS of their channels and calls their contracts through the generated
stubs (``consumers.py``, ``rebuilder.py``), keeping module isolation intact;
``define_module`` derives the consumed contracts from those callers.

Its route and tables are discovered. A short ``setup`` registers what has no
declaration: the projection, its consumers and its rebuilder in their L2
form (a ``Projection(...)`` cannot express the project fan-out over task
rows, see ``consumers.py``) and the board's planner read.
"""

from sandloom.sdk import define_module
from sandloom.sdk.module import ModuleAPI  # sandloom: advanced setup registers L2 parts
from sandloom.sdk.projections import (  # sandloom: advanced L2 projection descriptors
    ProjectionConsumerDescriptor,
    QueryProjectionDescriptor,
)

from .consumers import (
    PROJECTION_NAME,
    ProjectCreated,
    ProjectUpdated,
    TaskCreated,
    TaskUpdated,
    on_project_updated,
    on_task_upserted,
)
from .rebuilder import TaskboardRebuilder
from .services.operations import OPERATIONS

#: (durable consumer name — its inbox identity, never rename —, view, handler)
CONSUMERS = (
    ("taskboard.ptqi@task.created", TaskCreated, on_task_upserted),
    ("taskboard.ptqi@task.updated", TaskUpdated, on_task_upserted),
    ("taskboard.ptqi@project.created", ProjectCreated, on_project_updated),
    ("taskboard.ptqi@project.updated", ProjectUpdated, on_project_updated),
)


def _setup(core: ModuleAPI) -> None:
    """Register the projection, its consumers and its rebuilder."""
    core.register_query_projection(
        QueryProjectionDescriptor(
            name=PROJECTION_NAME,
            owner_module=__name__,
            version="v1",
            source_events=tuple(view.channel for _, view, _ in CONSUMERS),
            storage="default",
        )
    )
    for name, view, handler in CONSUMERS:
        core.register_projection_consumer(
            ProjectionConsumerDescriptor(
                name=name,
                source_channel=view.channel,
                projection=PROJECTION_NAME,
                handler=handler,
                bind_tenant_from="tenant_id",
            )
        )
    core.register_projection_rebuilder(PROJECTION_NAME, TaskboardRebuilder())
    # The board's declared read (execution planner; planned at startup).
    core.register_operations(*OPERATIONS)


register = define_module(
    __name__,
    version="0.1.0",
    depends_on=["projecthub.modules.projects", "projecthub.modules.tasks"],
    capabilities=["database", "eventbus"],
    setup=_setup,
)
