"""``sandloom outbox`` / ``sandloom streams`` — operator repair for dead-lettered
events.

    sandloom outbox list-dead [--database NAME] [--channel CH] [--limit N]
    sandloom outbox requeue   [--database NAME] (--id UUID ... | --all) [--channel CH]
    sandloom outbox purge     [--database NAME] [--hours H] [--batch N]
                         [--inbox-days D [--force]]
                                            delete delivered rows past retention
    sandloom streams dlq-list   <channel> [--limit N]
    sandloom streams dlq-replay <channel> [--id DLQ_ID ...] [--group G] [--limit N]
    sandloom streams groups     <channel>          consumer groups: lag / pending
    sandloom streams consumers  [--all]            subscription inventory (who reads
    what)
    sandloom streams release-group <channel> <group>  forget a group's owner fingerprint

Both dead-letter states are terminal for automatic delivery, so they need an
explicit way back once the cause (a bug, a long broker outage, a missing schema)
is fixed:

* the transactional outbox dead-letters a row after ``OUTBOX_RELAY_MAX_ATTEMPTS``
  failed publishes — ``requeue`` clears ``dead_at`` and resets the attempt budget
  so the relay redelivers it;
* the Streams transport moves a message a consumer group kept failing into
  ``<channel>.dlq`` — ``dlq-replay`` re-appends it to the source stream,
  TARGETED at the group that dead-lettered it (other groups ack-skip it, so
  their handlers do not repeat a side effect); ``--group`` limits the replay to
  one group's entries.

``purge`` deletes DELIVERED outbox rows older than ``--hours`` (default
``OUTBOX_RETENTION_HOURS``; the relay runs the same purge periodically) in
bounded batches; dead-lettered and pending rows are never touched.
``--inbox-days`` additionally prunes the durable consumer inbox
(``consumer_inbox``) claims older than that many days — never automatic: a
claim must outlive every possible redelivery of its event (Streams retention,
an operator ``requeue`` of an old dead row), or a late duplicate would apply
its effect twice; a ``D`` inside ``EVENT_STREAM_RETENTION_SECONDS`` + 1 day is
refused unless ``--force`` (:mod:`sandloom.core.guarantee_windows`). The audit log
is never purged by the framework (retention is a compliance policy; see
docs/operations/operator-reference.md).

``groups`` / ``consumers`` are the observability side: per-group lag / pending,
and the cross-service subscription inventory every consumer heartbeats into
Redis (which module / service reads which channel version — check it before
retiring a ``.v1`` channel). ``release-group`` clears the ownership fingerprint
of a group after a deliberate move of its module to another database.

Both keep the stable envelope id, so consumers that already applied an event
dedupe the redelivery. The commands build the app (to resolve the configured
databases / event bus by logical name) but do NOT start its lifespan.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sandloom.core.sql_alchemy.durable_inbox as _durable_inbox
from sandloom.core.composition import load_composition_core
from sandloom.core.guarantee_windows import inbox_purge_problems
from sandloom.core.tasks.event_ops import (
    outbox_list_dead,
    outbox_purge,
    outbox_requeue,
    streams_consumers,
    streams_dlq_list,
    streams_dlq_replay,
    streams_groups,
    streams_release_group,
)


def _build_core() -> Any:
    # The application the deployment runs (SANDLOOM_COMPOSITION_ROOT, default
    # sandloom.main) — the same resolution as sandloom migrate and the worker, so a
    # downstream app's outbox/streams are operated with ITS module set.
    return load_composition_core()


async def inbox_purge(
    core: Any,
    *,
    database: str = "default",
    days: float,
    batch: int = 1000,
    force: bool = False,
) -> int:
    """Prune durable consumer-inbox claims older than ``days`` (explicit only).

    Deletes in batches of ``batch`` rows, each in its own short Unit of Work,
    so a large backlog never becomes one long transaction. Refused when
    ``days`` is inside the redelivery window (``EVENT_STREAM_RETENTION_SECONDS``
    + a margin, :mod:`sandloom.core.guarantee_windows`) unless ``force``: a claim
    purged before its event's last possible redelivery lets it apply twice.
    """
    if days <= 0:
        raise SystemExit("error: --inbox-days must be > 0")
    if batch <= 0:
        raise SystemExit("error: --batch must be > 0")
    problems = inbox_purge_problems(core.settings, days)
    if problems and not force:
        raise SystemExit(f"error: {problems[0].message} (pass --force to purge anyway)")
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    total = 0
    while True:
        async with core.get_uow_factory(database)() as uow:
            deleted = int(
                await _durable_inbox.DurableConsumerInbox.purge_processed_before(
                    uow.session, cutoff, limit=batch
                )
            )
        total += deleted
        if deleted < batch:
            return total


async def _close(core: Any) -> None:
    infra = getattr(core, "infra", None)
    if infra is not None:
        await infra.aclose_all()


async def _run_outbox(args: argparse.Namespace) -> int:
    core = _build_core()
    try:
        if args.action == "list-dead":
            rows = await outbox_list_dead(
                core, database=args.database, channel=args.channel, limit=args.limit
            )
            for row in rows:
                print(
                    f"{row['id']}  {row['channel']}  attempts={row['attempts']}  "
                    f"dead_at={row['dead_at']}  error={row['last_error']!r}"
                )
            print(f"{len(rows)} dead event(s)")
            return 0
        if args.action == "purge":
            count = await outbox_purge(
                core, database=args.database, hours=args.hours, batch=args.batch
            )
            print(f"purged {count} delivered event(s) on database={args.database}")
            if args.inbox_days is not None:
                claims = await inbox_purge(
                    core,
                    database=args.database,
                    days=args.inbox_days,
                    batch=args.batch,
                    force=args.force,
                )
                print(
                    f"purged {claims} consumer-inbox claim(s) on "
                    f"database={args.database}"
                )
            return 0
        if not args.all and not args.ids and args.channel is None:
            print("error: pass --id ..., --channel, or --all", file=sys.stderr)
            return 2
        ids = [uuid.UUID(raw) for raw in args.ids] if args.ids else None
        count = await outbox_requeue(
            core,
            database=args.database,
            ids=ids,
            channel=args.channel,
            limit=args.limit,
        )
        print(f"requeued {count} event(s) on database={args.database}")
        return 0
    finally:
        await _close(core)


async def _run_streams(args: argparse.Namespace) -> int:
    core = _build_core()
    try:
        if args.action == "dlq-list":
            entries = await streams_dlq_list(core, args.channel, limit=args.limit)
            for entry in entries:
                print(
                    f"{entry['id']}  event={entry['event_id']}  "
                    f"group={entry.get('group')}  attempts={entry['attempts']}  "
                    f"reason={entry['reason']}  replayable={entry.get('replayable')}"
                )
            print(f"{len(entries)} DLQ entr(y/ies) on {args.channel}")
            return 0
        if args.action == "groups":
            groups = await streams_groups(core, args.channel)
            for g in groups:
                print(
                    f"{g['group']}  lag={g.get('lag')}  pending={g.get('pending')}  "
                    f"consumers={g.get('consumers')}  "
                    f"last_delivered={g.get('last_delivered_id')}"
                )
            print(f"{len(groups)} group(s) on {args.channel}")
            return 0
        if args.action == "consumers":
            entries = await streams_consumers(core, include_stale=args.all)
            for e in entries:
                stale = "  STALE" if e.get("stale") else ""
                print(
                    f"{e.get('stream')}  group={e.get('group')}  "
                    f"module={e.get('module') or '-'}  "
                    f"service={e.get('service') or '-'}  "
                    f"consumer={e.get('consumer') or '-'}{stale}"
                )
            print(f"{len(entries)} subscription(s)")
            return 0
        if args.action == "release-group":
            released = await streams_release_group(core, args.channel, args.group)
            print(
                f"released owner of {args.group} on {args.channel}"
                if released
                else f"no owner recorded for {args.group} on {args.channel}"
            )
            return 0
        count = await streams_dlq_replay(
            core,
            args.channel,
            ids=args.ids or None,
            limit=args.limit,
            group=args.group,
        )
        print(f"replayed {count} DLQ entr(y/ies) to {args.channel}")
        return 0
    finally:
        await _close(core)


def outbox_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom outbox",
        description="Inspect / requeue dead-lettered outbox events",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list-dead", help="list dead-lettered outbox events")
    requeue = sub.add_parser("requeue", help="return dead events to pending")
    for p in (listing, requeue):
        p.add_argument("--database", default="default", help="logical database name")
        p.add_argument("--channel", default=None, help="only this channel")
    listing.add_argument("--limit", type=int, default=100)
    requeue.add_argument("--limit", type=int, default=1000)
    requeue.add_argument("--id", dest="ids", action="append", default=[])
    requeue.add_argument(
        "--all", action="store_true", help="requeue every dead event (up to --limit)"
    )
    purge = sub.add_parser(
        "purge", help="delete delivered events older than the retention window"
    )
    purge.add_argument("--database", default="default", help="logical database name")
    purge.add_argument(
        "--hours",
        type=float,
        default=None,
        help="retention in hours (default: OUTBOX_RETENTION_HOURS)",
    )
    purge.add_argument("--batch", type=int, default=1000, help="rows per DELETE")
    purge.add_argument(
        "--inbox-days",
        type=float,
        default=None,
        help="also prune consumer_inbox claims older than this many days "
        "(refused inside EVENT_STREAM_RETENTION_SECONDS + 1 day unless --force)",
    )
    purge.add_argument(
        "--force",
        action="store_true",
        help="purge inbox claims even inside the redelivery window",
    )
    args = parser.parse_args(argv)
    if args.action == "purge" and args.batch <= 0:
        parser.error("--batch must be positive")
    return asyncio.run(_run_outbox(args))


def streams_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom streams",
        description="Inspect Streams groups / consumers; replay the dead-letter queue",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("dlq-list", help="list a channel's DLQ entries")
    replay = sub.add_parser("dlq-replay", help="re-publish DLQ entries")
    for p in (listing, replay):
        p.add_argument("channel", help="source channel/stream, e.g. billing.paid.v1")
        p.add_argument("--limit", type=int, default=100)
    replay.add_argument("--id", dest="ids", action="append", default=[])
    replay.add_argument(
        "--all", action="store_true", help="explicitly replay every matching entry"
    )
    replay.add_argument(
        "--group",
        default=None,
        help="only replay entries dead-lettered by this consumer group",
    )
    groups = sub.add_parser("groups", help="consumer groups of a channel")
    groups.add_argument("channel")
    consumers = sub.add_parser("consumers", help="subscription inventory")
    consumers.add_argument(
        "--all", action="store_true", help="include stale (not heartbeating) entries"
    )
    release = sub.add_parser(
        "release-group", help="forget a consumer group's owner fingerprint"
    )
    release.add_argument("channel")
    release.add_argument("group")
    args = parser.parse_args(argv)
    if args.action == "dlq-replay" and not args.ids and not args.all:
        parser.error("dlq-replay requires --id or --all")
    if args.action in {"dlq-list", "dlq-replay"} and args.limit <= 0:
        parser.error("--limit must be positive")
    return asyncio.run(_run_streams(args))


__all__ = [
    "inbox_purge",
    "outbox_main",
    "streams_main",
]
