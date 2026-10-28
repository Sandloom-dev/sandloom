"""In-memory registry of query projections, their consumers and rebuilders.

Mirrors the schema registry's role for events: a single source of truth that
business modules populate at load through ``ModuleAPI`` and that core reads to
(a) wire each consumer to the event bus at startup and (b) drive rebuilds from
the CLI. It supports :meth:`snapshot`/:meth:`restore` so a module registration
that fails partway is rolled back atomically, exactly like services and event
schemas (see ``CoreFacade.snapshot_registrations``).

The registry never imports a business module and never touches infrastructure —
it only stores descriptors and validates their internal consistency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.projections.descriptors import (
        ProjectionConsumerDescriptor,
        QueryProjectionDescriptor,
    )
    from sandloom.core.projections.rebuild import ProjectionRebuilder


class ProjectionRegistryError(RuntimeError):
    """A projection/consumer registration conflicts with existing state."""


class QueryProjectionRegistry:
    """Holds query-projection descriptors, consumers and rebuilders by name."""

    def __init__(self) -> None:
        self._projections: dict[str, QueryProjectionDescriptor] = {}
        self._consumers: dict[str, ProjectionConsumerDescriptor] = {}
        self._rebuilders: dict[str, ProjectionRebuilder] = {}

    # -- registration -----------------------------------------------------
    def register_projection(self, descriptor: QueryProjectionDescriptor) -> None:
        if descriptor.name in self._projections:
            raise ProjectionRegistryError(
                f"query projection {descriptor.name!r} already registered"
            )
        self._projections[descriptor.name] = descriptor

    def register_consumer(self, descriptor: ProjectionConsumerDescriptor) -> None:
        # Keyed by the effective consumer GROUP (``<module>:<name>``): two
        # modules may reuse a consumer name, one group may exist only once.
        key = getattr(descriptor, "group_name", descriptor.name)
        if key in self._consumers:
            raise ProjectionRegistryError(
                f"projection consumer {key!r} already registered"
            )
        self._consumers[key] = descriptor

    def register_rebuilder(
        self, projection: str, rebuilder: ProjectionRebuilder
    ) -> None:
        if projection in self._rebuilders:
            raise ProjectionRegistryError(
                f"rebuilder for projection {projection!r} already registered"
            )
        self._rebuilders[projection] = rebuilder

    # -- reads ------------------------------------------------------------
    def get_projection(self, name: str) -> QueryProjectionDescriptor | None:
        return self._projections.get(name)

    def get_consumer(self, name: str) -> ProjectionConsumerDescriptor | None:
        """Look a consumer up by its group id, or by its bare name."""
        found = self._consumers.get(name)
        if found is not None:
            return found
        return next((c for c in self._consumers.values() if c.name == name), None)

    def get_rebuilder(self, projection: str) -> ProjectionRebuilder | None:
        return self._rebuilders.get(projection)

    def list_projections(self) -> list[QueryProjectionDescriptor]:
        return list(self._projections.values())

    def list_consumers(self) -> list[ProjectionConsumerDescriptor]:
        return list(self._consumers.values())

    def consumers_for(self, projection: str) -> list[ProjectionConsumerDescriptor]:
        return [c for c in self._consumers.values() if c.projection == projection]

    def token_sources(
        self,
    ) -> dict[str, tuple[tuple[str, Any], ...]]:
        """``channel -> ((projection, token_entries), ...)`` for every
        projection that issues consistency tokens (empty: none does)."""
        sources: dict[str, tuple[tuple[str, Any], ...]] = {}
        for descriptor in self._projections.values():
            entries = getattr(descriptor, "token_entries", None)
            if entries is None:
                continue
            for channel in descriptor.source_events:
                sources[channel] = (
                    *sources.get(channel, ()),
                    (descriptor.name, entries),
                )
        return sources

    def __contains__(self, projection_name: object) -> bool:
        return projection_name in self._projections

    # -- transactional module registration --------------------------------
    def snapshot(self) -> dict[str, dict[str, object]]:
        """Shallow copy of the maps for a later :meth:`restore`.

        Descriptors are frozen/immutable and rebuilders are opaque objects, so a
        shallow per-map copy is enough to rewind a failed module registration
        without leaking later mutations into the snapshot.
        """
        return {
            "projections": dict(self._projections),
            "consumers": dict(self._consumers),
            "rebuilders": dict(self._rebuilders),
        }

    def restore(self, snapshot: dict[str, dict[str, object]]) -> None:
        """Reset the maps to a prior :meth:`snapshot`."""
        self._projections = dict(snapshot["projections"])  # type: ignore[arg-type]
        self._consumers = dict(snapshot["consumers"])  # type: ignore[arg-type]
        self._rebuilders = dict(snapshot["rebuilders"])  # type: ignore[arg-type]


__all__ = ["QueryProjectionRegistry", "ProjectionRegistryError"]
