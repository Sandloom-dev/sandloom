"""First-class capability contracts beyond the built-in five.

These are *standard* capabilities the kernel knows about — document stores,
graph stores, durable workflows, search, distributed locks, secrets — but ships
NO driver for. Each is a small, opinionated, AI-safe surface (much smaller than
the underlying backend's SDK): a module calls ``graph.query(...)`` or
``workflow.start(...)``, never a raw Neo4j/Temporal client. A plugin makes one
usable by registering a driver under the matching capability::

    def register(registry):
        registry.register_driver("graph_store", "neo4j", create_neo4j_graph_store)

**All six are EXPERIMENTAL** (``CapabilitySpec.stability == "experimental"``):
the contracts may still change and core ships no driver. Resolving one without a
plugin-registered driver raises
:class:`~sandloom.core.infra.registry.ExperimentalCapabilityError`.

The specs are registered at bootstrap so the capabilities are *known* (they show
up in ``/diagnostics/infra`` and give precise "no driver / no instance"
diagnostics), while remaining unusable until a plugin supplies a driver. This is
how graph DBs, Mongo, Temporal, etc. are added without bloating core — see
``docs/archive/CAPABILITY_RUNTIME_TZ.md`` Phase 3.

Transactions note: each transactional backend exposes its OWN local unit of
work (``graph.transaction()``, ``document.transaction()``) — there is no fake
distributed transaction spanning SQL + Mongo + Neo4j.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from sandloom.core.infra.capabilities import CapabilitySpec
from sandloom.core.infra.contracts import InfraProvider


class DocumentStoreProvider(InfraProvider):
    """Document database capability (MongoDB, CouchDB, …).

    Key-and-collection addressed; values are JSON-like documents. The surface is
    deliberately small — CRUD over named collections — not the backend's full
    query DSL (which stays behind ``raw_client`` for driver authors).
    """

    capability: ClassVar[str] = "document_store"

    @abc.abstractmethod
    async def insert(self, collection: str, document: Mapping[str, Any]) -> str:
        """Insert ``document`` into ``collection``; return the new document id."""

    @abc.abstractmethod
    async def find_one(
        self, collection: str, query: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Return the first document matching ``query``, or ``None``."""

    @abc.abstractmethod
    async def update(
        self,
        collection: str,
        query: Mapping[str, Any],
        changes: Mapping[str, Any],
    ) -> int:
        """Apply ``changes`` to documents matching ``query``; return count."""

    @abc.abstractmethod
    async def delete(self, collection: str, query: Mapping[str, Any]) -> int:
        """Delete documents matching ``query``; return count removed."""


class GraphStoreProvider(InfraProvider):
    """Graph database capability (Neo4j, Memgraph, Neptune, …).

    The portable surface is parameterized query + execute. Query *language*
    (Cypher vs Gremlin) is a feature a driver advertises and a module requires —
    so a module written for Cypher cannot silently run on a Gremlin backend.
    """

    capability: ClassVar[str] = "graph_store"

    @abc.abstractmethod
    async def query(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run a read query and return result rows as dicts."""

    @abc.abstractmethod
    async def execute(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> None:
        """Run a write/DDL query (create nodes/edges/constraints)."""


class WorkflowProvider(InfraProvider):
    """Durable workflow capability (Temporal, durable-execution engines).

    Distinct from ``taskqueue`` (fire-and-forget jobs): a workflow is a
    long-running, durable, resumable business process. The surface is start /
    cancel / result by a caller-supplied id — never the engine's worker SDK.
    """

    capability: ClassVar[str] = "workflow"

    @abc.abstractmethod
    async def start(
        self,
        name: str,
        *,
        id: str,  # workflow id, the domain term
        input: Mapping[str, Any] | None = None,  # domain term
    ) -> str:
        """Start workflow ``name`` with idempotent ``id``; return a run handle."""

    @abc.abstractmethod
    async def cancel(self, id: str) -> None:  # workflow id
        """Request cancellation of the workflow run identified by ``id``."""

    @abc.abstractmethod
    async def result(self, id: str) -> Any:  # workflow id
        """Await and return the workflow's result (raises on failure)."""


class SearchProvider(InfraProvider):
    """Full-text / vector search capability (Elasticsearch, OpenSearch, …)."""

    capability: ClassVar[str] = "search"

    @abc.abstractmethod
    async def index(
        self,
        index: str,
        id: str,
        document: Mapping[str, Any],
    ) -> None:
        """Index ``document`` under ``id`` in ``index`` (upsert)."""

    @abc.abstractmethod
    async def search(
        self, index: str, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` documents matching ``query``."""

    @abc.abstractmethod
    async def delete(self, index: str, id: str) -> None:
        """Remove the document ``id`` from ``index``."""


class LockProvider(InfraProvider):
    """Distributed lock capability (Redis, etcd, Zookeeper, …)."""

    capability: ClassVar[str] = "lock"

    @abc.abstractmethod
    async def acquire(self, key: str, *, ttl_seconds: int = 30) -> str | None:
        """Try to acquire ``key``; return an opaque token, or ``None`` if held."""

    @abc.abstractmethod
    async def release(self, key: str, token: str) -> None:
        """Release ``key`` iff ``token`` matches the holder (safe unlock)."""


class SecretsProvider(InfraProvider):
    """Secret store capability (Vault, AWS/GCP Secrets Manager, …)."""

    capability: ClassVar[str] = "secrets"

    @abc.abstractmethod
    async def get(self, name: str) -> str | None:
        """Return the secret value for ``name``, or ``None`` if absent."""

    @abc.abstractmethod
    async def names(self) -> Sequence[str]:
        """List available secret names (not values)."""


# --------------------------------------------------------------------------- #
# Specs
# --------------------------------------------------------------------------- #
DOCUMENT_STORE_CAPABILITY = CapabilitySpec(
    name="document_store",
    version="v1",
    stability="experimental",
    provider_contract=DocumentStoreProvider,
    # ``conditional_write`` + ``tenant_native``: the store may be a projection
    # target (version-guarded writes and tombstones, native tenancy; spec
    # §6.4, sandloom.core.projections.stores)
    features=frozenset(
        {"transactions", "aggregation", "indexes", "conditional_write", "tenant_native"}
    ),
    description="Document database (collections of JSON-like documents).",
)

GRAPH_STORE_CAPABILITY = CapabilitySpec(
    name="graph_store",
    version="v1",
    stability="experimental",
    provider_contract=GraphStoreProvider,
    features=frozenset({"cypher", "gremlin", "transactions", "vector_index"}),
    description="Graph database (nodes/edges, graph queries).",
)

WORKFLOW_CAPABILITY = CapabilitySpec(
    name="workflow",
    version="v1",
    stability="experimental",
    provider_contract=WorkflowProvider,
    features=frozenset({"start", "cancel", "result", "signal", "schedule"}),
    description="Durable, resumable long-running workflows.",
)

SEARCH_CAPABILITY = CapabilitySpec(
    name="search",
    version="v1",
    stability="experimental",
    provider_contract=SearchProvider,
    features=frozenset({"fulltext", "vector", "faceting"}),
    description="Full-text / vector search.",
)

LOCK_CAPABILITY = CapabilitySpec(
    name="lock",
    version="v1",
    stability="experimental",
    provider_contract=LockProvider,
    features=frozenset({"ttl", "reentrant"}),
    description="Distributed locks.",
)

SECRETS_CAPABILITY = CapabilitySpec(
    name="secrets",
    version="v1",
    stability="experimental",
    provider_contract=SecretsProvider,
    features=frozenset({"get", "rotate", "versioning"}),
    description="Secret store.",
)

#: Standard capabilities known to the kernel but driver-less until a plugin
#: registers one. Registered at bootstrap for discoverability + diagnostics.
EXTRA_CAPABILITIES: tuple[CapabilitySpec, ...] = (
    DOCUMENT_STORE_CAPABILITY,
    GRAPH_STORE_CAPABILITY,
    WORKFLOW_CAPABILITY,
    SEARCH_CAPABILITY,
    LOCK_CAPABILITY,
    SECRETS_CAPABILITY,
)


__all__ = [
    "DocumentStoreProvider",
    "GraphStoreProvider",
    "WorkflowProvider",
    "SearchProvider",
    "LockProvider",
    "SecretsProvider",
    "DOCUMENT_STORE_CAPABILITY",
    "GRAPH_STORE_CAPABILITY",
    "WORKFLOW_CAPABILITY",
    "SEARCH_CAPABILITY",
    "LOCK_CAPABILITY",
    "SECRETS_CAPABILITY",
    "EXTRA_CAPABILITIES",
]
