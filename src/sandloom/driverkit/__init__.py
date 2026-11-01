"""The API a driver package implements. Most code should use ``sandloom.sdk.infra``.

A business module may not import this package (SANDLOOM-C004), and the kernel
never does. A contract joins it only with a kit in :mod:`sandloom.driverkit.testing`.
A driver states its ``contract_version``; another major version is refused at load
(SANDLOOM-B022). Guide: ``docs/infrastructure/write-a-driver.md``.
"""

from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.capabilities import CapabilityRef, CapabilitySpec
from sandloom.core.infra.contracts import InfraProvider, Notification, NotifierProvider

__all__ = [
    "CapabilityRef",
    "CapabilitySpec",
    "HealthResult",
    "InfraProvider",
    "Notification",
    "NotifierProvider",
]
