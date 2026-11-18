"""The standard stack's backing server for a driver package: what ``sandloom dev``
and the scaffold of ``sandloom new`` / ``sandloom add deploy`` ask of it.

A package whose driver the standard stack selects registers an object with the
members of :class:`StackService` in the entry-point group ``sandloom.stack``
(``"<driver>" = "package.stack:STACK_SERVICE"``); the kernel CLI loads it by the
driver's name and holds no client, image name or url layout of its own.
``sandloom-redis`` is the model. One implementation, in the kernel; a driver
imports it from here (ADR-0016: a driver package imports only ``sandloom.driverkit``
and ``sandloom.sdk``).
"""

from __future__ import annotations

from sandloom.core.cli.stack_service import (
    GROUP,
    STANDARD_DRIVER,
    KeyedServer,
    StackService,
    StackServiceError,
)

__all__ = [
    "GROUP",
    "STANDARD_DRIVER",
    "KeyedServer",
    "StackService",
    "StackServiceError",
]
