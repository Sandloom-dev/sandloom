"""L2 saga primitives of ``sandloom.sdk.sagas`` (ADR-0008, spec §3).

The L2 ``step`` names its command as ``"<contract>.<method>"``; the L1
``step`` of ``sandloom.sdk.sagas`` also takes a typed caller method and calls this
one with the name. ``Saga`` is today's definition; the L1 subclass records
its owning module for discovery.
"""

from __future__ import annotations

from sandloom.core.descriptors import CommandStep, SagaStep
from sandloom.core.module_api import Saga, StepKind, step

__all__ = ["CommandStep", "Saga", "SagaStep", "StepKind", "step"]
