"""A ``bound_tenant()`` policy consumes the tenant authority's witness
(ADR-0014 decision 13, KB-31).

``Account.bound_tenant()`` asks ``tenant_kind`` whether the bound tenant is
an organization, and ``tenant_kind`` calls the membership witness of the
assigned tenant authority. The module whose route policy states it therefore
calls that contract: once the witness is resolved (boot, after the claim
authorities are assigned), its contract joins the module's ``consumes``,
optional and narrowed to the witness method. ``sandloom generate`` runs the
same composition, so the module snapshot records the use, the consumer copy
(the expectation the witness is read from) is written and ``sandloom add
deploy`` binds the contract in a split deployment.

Boot only: a walk over the application's routes and the load pass's
manifest, no I/O and nothing on a request.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from sandloom.core.descriptors import ContractRequirement, ModuleDescriptor
from sandloom.core.loader.manifest import AppManifest, with_consumed

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.facade._tenant_witness import TenantWitness

#: The ``reason`` of the derived requirement (diagnostics, snapshots).
REASON = "tenant kind: Account.bound_tenant() asks the tenant authority's witness"


def _route_modules(
    routes: Iterable[tuple[Any, str]], manifest: AppManifest
) -> dict[str, list[str]]:
    """``{module: [policy, ...]}`` of ``routes`` (``(route, policy)``, the
    routes whose policy states ``Account.bound_tenant()``), the module read
    from the manifest's routes (method and full path, as mounted)."""
    owners = {(r.method, r.path): r.module for r in manifest.routes}
    found: dict[str, list[str]] = {}
    for route, policy in routes:
        for method in sorted(getattr(route, "methods", None) or ("*",)):
            module = owners.get((str(method), str(getattr(route, "path", ""))))
            if module is not None and policy not in found.setdefault(module, []):
                found[module].append(policy)
    return found


def _merged(consumes: list[Any], witness: TenantWitness) -> list[Any] | None:
    """``consumes`` with the witness method, else ``None`` when it already
    covers it (an entry with no method list covers every method)."""
    for index, entry in enumerate(consumes):
        name = entry if isinstance(entry, str) else entry.name
        if name != witness.contract:
            continue
        if isinstance(entry, str) or not entry.methods:
            return None
        if witness.method in entry.methods:
            return None
        out = list(consumes)
        out[index] = dataclasses.replace(
            entry, methods=(*entry.methods, witness.method)
        )
        return out
    requirement = ContractRequirement(
        witness.contract, required=False, reason=REASON, methods=(witness.method,)
    )
    return [*consumes, requirement]


def _diagnostics(core: Any, summary: Any, desc: ModuleDescriptor, name: str) -> None:
    """Record the derived entry's resolution on ``summary.consumes`` (as the
    load preflight does, ``/diagnostics/core``)."""
    entries = summary.consumes.setdefault(desc.name, [])
    if any(e.get("contract") == name for e in entries):
        return
    bindings = getattr(core.settings, "service_bindings", None) or {}
    if core.service_registry.contains(name):
        resolution = "local"
    elif name in bindings:
        resolution = "remote"
    else:
        resolution = "missing"
    entries.append({"contract": name, "required": False, "resolution": resolution})


def add_witness_consumes(
    core: Any,
    summary: Any,
    witness: TenantWitness | None,
    routes: Iterable[tuple[Any, str]],
    *,
    log: logging.Logger | None = None,
) -> list[str]:
    """Add ``witness``'s contract to the ``consumes`` of every loaded module
    owning one of ``routes`` (``bound_tenant_routes``: the routes whose policy
    states ``Account.bound_tenant()``) and refresh their manifest entries;
    the names of the modules changed. Nothing without a witness: the startup
    report (H-19) names the policy."""
    if witness is None:
        return []
    modules = _route_modules(routes, AppManifest.from_summary(summary))
    changed: list[str] = []
    for desc in summary.loaded:
        if desc.name not in modules:
            continue
        merged = _merged(list(desc.consumes or ()), witness)
        if merged is None:
            continue
        desc.consumes = merged
        entry = summary.manifests.get(desc.name)
        if entry is not None:
            summary.manifests[desc.name] = with_consumed(entry, desc)
        _diagnostics(core, summary, desc, witness.contract)
        changed.append(desc.name)
        if log is not None:
            log.info(
                "tenant kind: %s consumes %s (its route policy %s states "
                "Account.bound_tenant())",
                desc.name,
                witness.describe(),
                "; ".join(modules[desc.name]),
            )
    return changed


__all__ = ["REASON", "add_witness_consumes"]
