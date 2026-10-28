"""``sandloom projection`` — operate the Cross-Module Query / Projection layer.

    sandloom projection list                         list registered projections +
    consumers
    sandloom projection rebuild <name>               rebuild a whole projection
    sandloom projection rebuild <name> --tenant <id> rebuild one tenant's rows
    sandloom projection purge <name> --older-than-days N
                                                purge tombstones (spec §6.4 item 5)

A projection is a cache reconstructable from its source modules, so rebuild is
the primary repair path (drop/rebuild after corruption or a schema migration).
The registry is populated by ``create_app`` when the read module loads; ``list``
only needs that, while ``rebuild`` runs the app's startup tasks (infra,
databases, schema registry) so the rebuilder has a fully wired core.

``rebuild`` deliberately does NOT enter the ASGI lifespan: that would start the
consumer runtime (joining the projection's live consumer groups and stealing
events from the running service, which then never see them) and, in prod, the
outbox relay. It reads the source modules through their public read contracts —
over ``SERVICE_BINDINGS`` when they live in other services — so run it as a
one-off JOB once those services are up, not in the ``migrate`` one-shot.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import timedelta

import sandloom.core.composition as _composition
import sandloom.core.lifecycle as _lifecycle
import sandloom.core.projections.operations as _ops
import sandloom.core.projections.purge as _purge


def _build_app() -> object:
    # The application the deployment runs (SANDLOOM_COMPOSITION_ROOT, default
    # sandloom.main) — the same resolution as sandloom migrate and the worker.
    return _composition.load_composition_app()


def _cmd_list() -> int:
    app = _build_app()
    core = app.state.core  # type: ignore[attr-defined]
    registry = core.projection_registry
    projections = registry.list_projections()
    if not projections:
        print("No query projections registered.")
        return 0
    for projection in projections:
        consumers = registry.consumers_for(projection.name)
        has_rebuilder = registry.get_rebuilder(projection.name) is not None
        print(
            f"{projection.name} (owner={projection.owner_module} "
            f"version={projection.version} storage={projection.storage} "
            f"rebuildable={projection.rebuildable} rebuilder={has_rebuilder})"
        )
        for consumer in consumers:
            print(
                f"    <- {consumer.source_channel}  "
                f"[consumer={consumer.name} durable={consumer.durable}]"
            )
    return 0


async def _run_rebuild(name: str, tenant: str | None) -> int:
    app = _build_app()
    core = app.state.core  # type: ignore[attr-defined]
    # Startup tasks only (infra + databases): no consumer runtime, no relay.
    await _lifecycle.run_startup(core)
    try:
        registry = core.projection_registry
        if registry.get_projection(name) is None:
            print(f"error: unknown projection {name!r}", file=sys.stderr)
            return 1
        if registry.get_rebuilder(name) is None:
            print(f"error: no rebuilder registered for {name!r}", file=sys.stderr)
            return 1
        # the rebuild is marked in the projection's watermark row: a
        # tombstone purge refuses meanwhile, the purge watermark is kept
        result = await _ops.rebuild_projection(
            name,
            tenant_id=uuid.UUID(tenant) if tenant is not None else None,
            core=core,
        )
    finally:
        await _lifecycle.run_shutdown(core)
    print(f"rebuilt {result.projection}: rows={result.rows} cleared={result.cleared}")
    return 0


async def _run_purge(name: str, older_than_days: float) -> int:
    app = _build_app()
    core = app.state.core  # type: ignore[attr-defined]
    await _lifecycle.run_startup(core)
    try:
        purged = await _ops.purge_projection_tombstones(
            name, older_than=timedelta(days=older_than_days), core=core
        )
    except (_purge.PurgeRefused, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await _lifecycle.run_shutdown(core)
    print(f"purged {purged} tombstone(s) of {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom projection", description="Operate query projections"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="list registered projections and consumers")
    rebuild = sub.add_parser("rebuild", help="rebuild a projection from source")
    rebuild.add_argument("name", help="projection name (e.g. project_task_query_index)")
    rebuild.add_argument(
        "--tenant", default=None, help="rebuild only this tenant id (UUID)"
    )
    purge = sub.add_parser(
        "purge",
        help="purge tombstones older than the replay horizon (raises the "
        "purge watermark first)",
    )
    purge.add_argument("name", help="projection name")
    purge.add_argument(
        "--older-than-days", type=float, required=True, help="tombstone age (days)"
    )
    args = parser.parse_args(argv)

    if args.action == "list":
        return _cmd_list()
    if args.action == "rebuild":
        return asyncio.run(_run_rebuild(args.name, args.tenant))
    if args.action == "purge":
        return asyncio.run(_run_purge(args.name, args.older_than_days))
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
