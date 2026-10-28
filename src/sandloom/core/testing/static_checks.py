"""The static schema and deploy checks of ``sandloom test`` (no database, no Redis).

Three checks that used to run only inside the parallel fast suite, where a
Redis or connection shortage could hide them, run as one in-process step of
``sandloom test`` (``schema & deploy``, about a second) and stay pinned by the
framework's own tests:

* :func:`chain_problems` (SANDLOOM-T002): every table and index a model declares
  is created by its OWNING migration chain, no table is created by two
  chains, and no chain creates a table no model declares. Revisions are read
  from their syntax tree, so ``op.create_table(_TABLE, ...)`` counts.
* :func:`redundant_index_problems` (SANDLOOM-T012): a plain index on the leading
  column(s) of a unique key of the same table is pure write cost.
* :func:`periodic_beat_problems` (SANDLOOM-T010): every module with periodic tasks
  that a deploy profile serves has exactly one beat, a worker on that beat's
  broker, and no two beats share a broker.
* the split profile (``deploy/compose/microservices.yml``, SANDLOOM-T014):
  the invariants of :func:`sandloom.core.testing.deploy_profile.profile_problems`
  (shared settings, issuer-only keys, a database and migrate one-shot per
  service, bindings and allowlist derived from what the modules consume, the
  gateway routes), on an application's profile as on the framework's own.

Each problem is one line that names its code and the fix.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy import MetaData

import sandloom.core.testing.deploy_profile as _deploy_profile
from sandloom.core.composition import load_composition_core
from sandloom.core.sql_alchemy.base import Base
from sandloom.core.tasks.periodic import PERIODIC_HOOK, has_periodic_tasks
from sandloom.core.tenant import TenantScoped
from sandloom.core.testing.database import (
    GLOBAL_UNIQUE_INFO,
    redundant_indexes,
    tenant_keys_without_tenant,
)
from sandloom.core.testing.migrations import (
    chain_coverage_problems,
    migration_chains,
    model_tables,
    tables_created_by,
)

#: Known redundant indexes not yet dropped (remove-only ratchet): found by the
#: SQL profile's guard, outside the drop set approved for identity 0007.
KNOWN_REDUNDANT = frozenset({"external_identities.ix_external_identities_provider"})


# --------------------------------------------------------------------------- #
# SANDLOOM-T002: migration chains
# --------------------------------------------------------------------------- #


def chain_problems(core: Any = None) -> list[str]:
    """Coverage, disjointness and ownership of the migration chains."""
    problems = list(chain_coverage_problems(core))
    owners = model_tables(core)
    seen: dict[str, str] = {}
    for chain in migration_chains(core):
        for table in sorted(tables_created_by(chain)):
            if table in seen and seen[table] != chain.key:
                problems.append(
                    f"table {table} is created by two chains, {seen[table]!r} and "
                    f"{chain.key!r} — a table belongs to the module that "
                    "declares its model; drop it from the other chain (SANDLOOM-T002)"
                )
            seen.setdefault(table, chain.key)
    for table, key in sorted(seen.items()):
        owner = owners.get(table)
        if owner is None:
            problems.append(
                f"table {table} is created by chain {key!r} but no loaded model "
                "declares it — add a revision that drops it, or load the module "
                "that owns it (SANDLOOM-T002)"
            )
        elif owner.key != key:
            problems.append(
                f"table {table} is created by chain {key!r}, but its model "
                f"belongs to {owner.module or 'sandloom.core'} (chain {owner.key!r}) "
                "— move the create_table to the owner's chain (SANDLOOM-T002)"
            )
    return problems


# --------------------------------------------------------------------------- #
# SANDLOOM-T012: an index that duplicates a unique prefix
# --------------------------------------------------------------------------- #


def redundant_index_problems(
    metadata: MetaData | None = None, known: Iterable[str] = KNOWN_REDUNDANT
) -> list[str]:
    if metadata is None:
        metadata = Base.metadata  # the composition root imported the models
    allowed = set(known)
    return [
        f"index {name} duplicates the leading column(s) of a unique key of the "
        "same table: the unique index already serves those lookups. Fix: drop "
        "`index=True` / the Index (a TenantScoped tenant_id index is dropped "
        "for you when the unique key leads with tenant_id) and add a revision "
        "that drops it (SANDLOOM-T012)"
        for name in redundant_indexes(metadata)
        if name not in allowed
    ]


# --------------------------------------------------------------------------- #
# SANDLOOM-T013: a tenant table's natural key leads with tenant_id
# --------------------------------------------------------------------------- #


def tenant_key_problems() -> list[str]:
    """A ``TenantScoped`` table's natural keys should contain ``tenant_id``
    (reported as warnings by ``sandloom test``).

    A key of a per-tenant table that omits it is unique across tenants: the
    same user in two organizations collides (a tenant-filtered read misses
    the other tenant's row, and the insert fails on the key)."""
    tenant_tables = {
        str(getattr(mapper.local_table, "name", ""))
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantScoped)
    } - {""}
    return [
        f"{key} of a TenantScoped table does not contain tenant_id, so it is "
        "unique across tenants. Fix: lead the key with tenant_id "
        '(UniqueConstraint("tenant_id", ...) / a surrogate id primary key), '
        f'or mark a deliberately global key info={{"{GLOBAL_UNIQUE_INFO}": '
        '"<reason>"} (SANDLOOM-T013)'
        for key in tenant_keys_without_tenant(Base.metadata, tenant_tables)
    ]


# --------------------------------------------------------------------------- #
# SANDLOOM-T010: periodic tasks need exactly one beat
# --------------------------------------------------------------------------- #


def _interpolate(value: object) -> str:
    """Compose ``${VAR:-default}`` -> default (the variable's name when none)."""
    return re.sub(
        r"\$\{(\w+)(?::?[-?]([^}]*))?\}",
        lambda m: m.group(2) if m.group(2) is not None else m.group(1),
        str(value),
    )


_Procs = dict[str, tuple[str, set[str]]]


def _processes(profile: Path) -> tuple[set[str], _Procs, _Procs]:
    """``(modules the app processes serve, beats, workers)`` of a profile;
    beats and workers map a service to ``(broker, modules)``."""
    yaml = importlib.import_module("yaml")  # optional: the check extra ships it
    data = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
    served: set[str] = set()
    beats: _Procs = {}
    workers: _Procs = {}
    for name, svc in (data.get("services") or {}).items():
        env = svc.get("environment") or {}
        if not isinstance(env, dict) or "SANDLOOM_MODULES" not in env:
            continue
        if svc.get("restart") == "no":
            continue  # migrate one-shots
        try:
            modules = set(json.loads(_interpolate(env["SANDLOOM_MODULES"])))
        except ValueError:
            continue
        broker = _interpolate(env.get("BROKER_URL", ""))
        command = [str(part) for part in (svc.get("command") or [])]
        if command[:4] == ["celery", "-A", "sandloom.core.worker", "worker"]:
            workers[name] = (broker, modules)
        elif command[:4] == ["celery", "-A", "sandloom.core.worker", "beat"]:
            beats[name] = (broker, modules)
        else:
            served |= modules
    return served, beats, workers


def periodic_beat_problems(
    profile: Path, *, periodic: Sequence[str] | None = None
) -> list[str]:
    """One compose profile's beats against its modules' periodic tasks.

    ``periodic`` defaults to the served modules whose ``tasks.py`` defines
    ``setup_periodic_tasks``.
    """
    served, beats, workers = _processes(profile)
    wanted = (
        set(periodic)
        if periodic is not None
        else {m for m in served if has_periodic_tasks(m)}
    )
    where = f"{profile.parent.name}/{profile.name}"
    problems: list[str] = []
    by_broker: dict[str, list[str]] = {}
    for name, (broker, _) in beats.items():
        by_broker.setdefault(broker, []).append(name)
    for broker, names in sorted(by_broker.items()):
        if len(names) > 1:
            problems.append(
                f"{where}: beats {sorted(names)} share the broker {broker}: each "
                "beat needs its own broker, or the schedule runs twice (SANDLOOM-T010)"
            )
    for module in sorted(wanted & served):
        owners = sorted(n for n, (_, mods) in beats.items() if module in mods)
        if len(owners) != 1:
            problems.append(
                f"{where}: {module} has periodic tasks ({PERIODIC_HOOK}) and "
                f"needs exactly one beat, got {owners or 'none'}. Fix: in the "
                "microservices profile `sandloom add deploy --refresh` (an undeployed "
                "module: `sandloom add deploy <module> --service <name>`) writes its "
                "worker and beat; in a hand-written profile add a beat for "
                f"{module} on its own broker plus a worker consuming it "
                "(SANDLOOM-T010)"
            )
            continue
        broker = beats[owners[0]][0]
        if not any(b == broker and module in mods for b, mods in workers.values()):
            problems.append(
                f"{where}: no worker consumes {module}'s tasks on {broker}: add "
                f"a worker with {module} in SANDLOOM_MODULES "
                "on that broker (SANDLOOM-T010)"
            )
    return problems


def deploy_profiles(root: Path) -> list[Path]:
    """The compose profiles next to the project (``deploy/`` or ``../deploy``)."""
    for deploy in (root / "deploy", root.parent / "deploy"):
        if deploy.is_dir():
            return sorted(
                set(deploy.glob("docker-compose.*.yml"))
                | set((deploy / "compose").glob("*.yml"))
                - {deploy / "compose/test.yml"}
            )
    return []


# --------------------------------------------------------------------------- #
# the sandloom test step
# --------------------------------------------------------------------------- #


def all_problems(
    root: Path, core: Any = None, warnings: list[str] | None = None
) -> list[str]:
    """Every problem of the step; ``warnings`` (when given) collects the
    split profile's warnings (SANDLOOM-T003: a module no service deploys)."""
    core = core if core is not None else load_composition_core()
    problems = chain_problems(core) + redundant_index_problems()
    profiles = deploy_profiles(root)
    if profiles and importlib.util.find_spec("yaml") is None:
        profiles = []  # PyYAML (the check extra) is not installed
    for profile in profiles:
        problems += periodic_beat_problems(profile)
        if profile.name == Path(_deploy_profile.PROFILE).name:
            # The split invariants (SANDLOOM-T014): the checks the framework's own
            # profile passes, on this project's profile.
            report = _deploy_profile.profile_problems(
                profile.parent.parent, served=list(getattr(core, "loaded_modules", []))
            )
            problems += report.problems
            if warnings is not None:
                warnings += report.warnings
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom test (schema & deploy)",
        description="Static migration-chain, index, beat and split-profile checks",
    )
    parser.add_argument("--root", default=".", help="the project root")
    args = parser.parse_args(argv)
    warnings: list[str] = []
    problems = all_problems(Path(args.root).resolve(), warnings=warnings)
    # A warning, not a failure: a globally unique id (a UUID aggregate id
    # of another module) is a safe key, which no static check can tell.
    for warning in [*tenant_key_problems(), *warnings]:
        print(f"warning: {warning}")
    for problem in problems:
        print(f"error: {problem}")
    if problems:
        print(f"{len(problems)} schema/deploy problem(s); `sandloom explain <code>`")
        return 1
    print("schema & deploy: OK")
    return 0


__all__ = [
    "KNOWN_REDUNDANT",
    "all_problems",
    "chain_problems",
    "deploy_profiles",
    "has_periodic_tasks",
    "main",
    "periodic_beat_problems",
    "redundant_index_problems",
    "tenant_key_problems",
]


if __name__ == "__main__":
    raise SystemExit(main())
