"""What a module may import: the caller's Protocol, the reference and the spec."""

import abc
from typing import ClassVar, Protocol

from sandloom.driverkit import CapabilityRef, CapabilitySpec, InfraProvider


class VectorIndex(Protocol):
    async def nearest(self, vector: list[float], *, limit: int) -> list[str]: ...


class VectorIndexProvider(InfraProvider, VectorIndex):
    capability: ClassVar[str] = "vector_index"

    @abc.abstractmethod
    async def nearest(self, vector: list[float], *, limit: int) -> list[str]: ...


VECTOR_INDEX: CapabilityRef[VectorIndex] = CapabilityRef("vector_index.v1")

VECTOR_INDEX_SPEC = CapabilitySpec(
    name="vector_index",
    version="v1",
    provider_contract=VectorIndexProvider,
    features=frozenset({"filters"}),
    stability="experimental",
)
