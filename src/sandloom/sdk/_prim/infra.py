"""New L2 infrastructure accessors of ``sandloom.sdk.infra`` (spec §2.2).

Module-level accessors for the object store, the task queue and a capability
reference, alongside today's ``cache()`` and ``notifier()``: they resolve through
the active infrastructure registry, like the facade's ``CoreRuntime`` accessors
(H1, WS-A), and return the Protocol of what a module may call.
"""

from __future__ import annotations

from sandloom.core.di import CapabilityRef, capability, objectstore, taskqueue

__all__ = ["CapabilityRef", "capability", "objectstore", "taskqueue"]
