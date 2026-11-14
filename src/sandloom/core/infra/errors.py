"""The errors a capability operation raises on purpose (ADR-0016, spec 2.1).

One hierarchy for every capability, exported by name from ``sandloom.sdk.infra``
(module door) and ``sandloom.driverkit`` (driver door). A driver maps its
backend's errors onto it; any other exception from an operation is a driver bug.

- **A miss is a result.** An absent key answers ``None`` and a conditional write
  that loses answers ``False``. :class:`NotFound` is for an addressed resource
  the caller expected (an object), never for a key-value lookup. ``None`` only
  means "known absent": a present value that cannot be read raises.
- **Codes, not messages.** Callers branch on the class and :attr:`InfraError.retryable`.
- **No built-in base classes.** None of these is also a ``ValueError``,
  ``LookupError`` or ``TimeoutError``.

These are call-time errors. The registry's load-time refusals (a module that
requires a feature its instance does not declare: SANDLOOM-B033, B037) stay
:class:`sandloom.core.infra.registry.FeatureUnsupportedError`.
"""

from __future__ import annotations

from typing import ClassVar


class InfraError(Exception):
    """Base of every error a capability operation raises on purpose."""

    #: Whether the same call may succeed when repeated (a transient fault).
    retryable: ClassVar[bool] = False

    def __init__(
        self, message: str, *, capability: str, instance: str | None = None
    ) -> None:
        self.capability = capability
        self.instance = instance
        super().__init__(message)


class NotFound(InfraError):
    """An addressed resource is absent (``objectstore.get`` of a missing key)."""


class AlreadyExists(InfraError):
    """A create that must not overwrite found an existing resource."""


class InvalidArgument(InfraError):
    """A key, value, TTL or option outside the contract."""


class TooLarge(InfraError):
    """A value, object or payload over a limit."""


class PermissionDenied(InfraError):
    """The backend refused the credentials."""


class Unavailable(InfraError):
    """Transient: connection lost, failover, overload. The caller may retry."""

    retryable: ClassVar[bool] = True


class Timeout(InfraError):
    """The operation did not finish within its bound. The caller may retry."""

    retryable: ClassVar[bool] = True


class Unsupported(InfraError):
    """A call needs a behaviour feature the instance does not declare."""

    def __init__(
        self, feature: str, *, capability: str, instance: str | None = None
    ) -> None:
        self.feature = feature
        where = f"{capability}/{instance}" if instance else capability
        super().__init__(
            f"{where} does not declare the feature {feature!r}",
            capability=capability,
            instance=instance,
        )


__all__ = [
    "AlreadyExists",
    "InfraError",
    "InvalidArgument",
    "NotFound",
    "PermissionDenied",
    "TooLarge",
    "Timeout",
    "Unavailable",
    "Unsupported",
]
