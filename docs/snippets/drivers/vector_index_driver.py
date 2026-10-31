"""The driver: a module may not import this."""

from vector_index.contracts import VectorIndexProvider


class MemoryVectorIndex(VectorIndexProvider):
    contract_version = "v1"
    supported_features = frozenset({"filters"})

    async def nearest(self, vector: list[float], *, limit: int) -> list[str]:
        return ["a", "b"][:limit]
