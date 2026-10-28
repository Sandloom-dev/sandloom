"""New L2 infrastructure accessors of ``sandloom.sdk.infra`` (spec §2.2).

Module-level accessors for the object store, the task queue and any
capability instance, alongside today's ``cache()`` and ``notifier()``: they
resolve through the active infrastructure registry, like the facade's
``CoreRuntime.objectstore`` / ``.taskqueue`` / ``.capability`` (H1, WS-A).
"""

from __future__ import annotations

from sandloom.core.di import capability, objectstore, taskqueue

__all__ = ["capability", "objectstore", "taskqueue"]
