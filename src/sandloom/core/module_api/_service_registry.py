from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from sandloom.core.module_api._provider import contract_schema

try:  # Phase J: strict naming (optional fallback if import fails)
    from sandloom.core.contracts import (
        validate_service_name as _validate_service_contract,
    )
except Exception:  # pragma: no cover  # noqa: BLE001 - strict naming is optional
    _validate_service_contract = None  # type: ignore


class ServiceRegistryError(Exception):
    """Base error for service registry."""


class ServiceAlreadyRegistered(ServiceRegistryError):
    pass


class ServiceNotFound(ServiceRegistryError):
    pass


class ServiceUnavailableInDeployment(HTTPException, ServiceNotFound):
    """A contract is neither provided locally nor bound to a remote provider.

    Raised by ``CoreFacade.get_service`` so the configurable monolith degrades
    *explicitly* (501 Not Implemented) instead of a bare 500 when, in this
    deployment topology, nobody implements the contract. Subclasses
    ``ServiceNotFound`` so existing ``except ServiceNotFound`` handlers and the
    RPC server's not-found mapping keep working unchanged.

    Lives here (next to ``ServiceNotFound``) rather than in ``sandloom.core.rpc`` so
    the facade can raise it WITHOUT importing the concrete RPC mechanism; the
    RPC package re-exports it as part of its public API.
    """

    #: Machine-readable code of the 501 body; ``sandloom explain SANDLOOM-R005``.
    code = "service_unavailable_in_deployment"
    sandloom_code = "SANDLOOM-R005"

    def __init__(self, service_name: str, *, reason: str | None = None) -> None:
        HTTPException.__init__(
            self,
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                f"Service {service_name!r} is not available in this deployment "
                f"({reason or 'no local provider and no remote binding'})"
            ),
        )


def _validate_service_name(name: str) -> None:
    """Validate service naming convention.

    Delegates to the contracts validator, which always enforces the strict
    `domain.component(.sub)*.vN` pattern — unversioned names are rejected.
    """
    # Basic sanity first (cheap and gives precise errors)
    if len(name) > 80:
        raise ServiceRegistryError("Service name too long")
    if " " in name:
        raise ServiceRegistryError("Service name must not contain spaces")
    if name.count(".."):
        raise ServiceRegistryError("Service name has empty segment (double dot)")
    parts = name.split(".")
    if len(parts) < 2:
        raise ServiceRegistryError("Service name must have at least 2 segments")
    for seg in parts:
        if not seg or not seg.replace("_", "").isalnum():
            raise ServiceRegistryError(
                f"Invalid characters in service name segment '{seg}'"
            )
    if _validate_service_contract is not None:
        try:
            _validate_service_contract(name)
        except Exception as exc:  # normalize to registry error type
            raise ServiceRegistryError(str(exc)) from exc


def is_valid_service_name(name: str) -> bool:
    """True when ``name`` passes the registry's naming convention."""
    try:
        _validate_service_name(name)
    except ServiceRegistryError:
        return False
    return True


class ServiceRegistry:
    """Simple registry of synchronous services keyed by string."""

    def __init__(self) -> None:
        self._map: dict[str, Any] = {}

    def register(self, name: str, impl: Any) -> None:
        """Register contract ``name``; the provider must be wire-portable.

        Raises :class:`~sandloom.core.contract_wire.ContractTypeError` when a public
        member is not a contract method or its signature cannot cross the
        internal-RPC wire (it would behave differently local vs remote).
        """
        _validate_service_name(name)
        if name in self._map:
            raise ServiceAlreadyRegistered(f"Service '{name}' already registered")
        contract_schema(impl)
        self._map[name] = impl

    def get(self, name: str) -> Any:
        if name not in self._map:
            raise ServiceNotFound(f"Service '{name}' not found")
        return self._map[name]

    def try_get(self, name: str) -> Any | None:
        return self._map.get(name)

    def list(self) -> list[str]:
        return list(self._map.keys())

    def contains(self, name: str) -> bool:
        return name in self._map

    def remove(self, name: str) -> None:
        """Drop ``name`` if present (no error if absent).

        Used to roll back a module registration that failed partway through
        :meth:`ModuleRegistrationDraft.apply` — never on the happy path.
        """
        self._map.pop(name, None)
