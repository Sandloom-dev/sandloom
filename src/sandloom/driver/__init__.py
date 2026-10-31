"""The experimental API a driver package implements and registers.

Most code should use :mod:`sandloom.sdk.infra`: a business module may not import
this package (SANDLOOM-C004), and the kernel never imports it. It may change in
any release, and a contract joins it only with a kit in
:mod:`sandloom.driver.testing`. Guide: ``docs/guides/write-a-driver.md``.
"""

from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.capabilities import CapabilitySpec
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import InfraProvider, Notification, NotifierProvider
from sandloom.core.infra.registry import InfraRegistry

__all__ = [
    "CapabilitySpec",
    "HealthResult",
    "InfraProvider",
    "InfraRegistry",
    "InstanceConfig",
    "Notification",
    "NotifierProvider",
]
