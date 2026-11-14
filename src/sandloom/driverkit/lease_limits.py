"""The argument limits of the ``lease`` contract, for a driver to enforce.

One set, defined by the port (``sandloom.core.infra.ports.lease``) and re-exported
here, so a driver package states no number of its own: a lease name is
1..``MAX_LEASE_NAME_LENGTH`` characters (an owner label at most as long) and a
TTL is a whole number of seconds ``>= MIN_LEASE_TTL_SECONDS``.
"""

from sandloom.core.infra.ports.lease import (
    MAX_LEASE_NAME_LENGTH,
    MIN_LEASE_TTL_SECONDS,
)

__all__ = ["MAX_LEASE_NAME_LENGTH", "MIN_LEASE_TTL_SECONDS"]
