"""Which ``sandloom.sdk.data`` forms need a ``database`` feature.

Pure data, importable without settings or a database library: ``sandloom
check`` (SANDLOOM-C030), the module preflight (SANDLOOM-B033) and the forms
themselves read it. The ``database`` capability's spec defines the features
(``sandloom.core.infra.capabilities``).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = ["DATABASE_FORM_FEATURES"]

#: The ``sandloom.sdk.data`` forms that need a ``database`` feature, and the
#: feature each needs (design/infra-agnostic/spec.md, section 5). A module that
#: uses one declares ``CapabilityRequirement("database", features=...)``
#: (SANDLOOM-C030); a table built with one is refused at startup on an
#: instance without its feature (SANDLOOM-B033). Pure data: ``sandloom check``
#: reads it without importing a database library.
DATABASE_FORM_FEATURES: Mapping[str, str] = MappingProxyType(
    {
        "Array": "arrays",
        "fulltext_index": "fulltext_search",
        "matches": "fulltext_search",
        "json_path": "json_path",
        "json_contains": "json_contains",
        "transaction_lock": "transaction_locks",
        "aggregate_list": "list_aggregates",
    }
)
