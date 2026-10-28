"""``sandloom-migrate extract-module``: moving one module's data to its own database.

Real PostgreSQL (two throw-away databases per test, ``sandloom_test_extract_*``):
the source is migrated with the core + tenancy + billing chains (a monolith
database), seeded with rows of every kind the extraction has rules for, and
billing is extracted to an empty target. Covers dry-run, the write fence (and
its refusal while a writer holds locks), crash + resume, verification,
sequences, the outbox hand-off, inbox/audit ownership, idempotent re-runs,
drop-source, unfence, a non-empty-target refusal and the Streams group-owner
transfer. Skips without PostgreSQL unless ``SANDLOOM_E2E_REQUIRED=1``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from sandloom.core.cli.extract import (
    COMPLETE_MARKER,
    HANDOFF_MARKER,
    MANIFEST_TABLE,
    ExtractionError,
    ModuleExtractor,
    database_identity_of_url,
    module_data_spec,
    transfer_group_owners,
)
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.testing import worker_env_values

# Slow (copies a module between real databases): skipped by the fast dev loop `pytest -m "not slow"`.
pytestmark = pytest.mark.slow

BACKEND = Path(__file__).resolve().parents[4]
SRC = BACKEND / "src"

psycopg2 = pytest.importorskip("psycopg2")


def _base_url() -> str:
    values = worker_env_values(BACKEND / ".env.test")
    return (
        os.environ.get("SANDLOOM_TEST_DATABASE_URL")
        or values.get("DATABASE_URL")
        or os.environ.get("DATABASE_URL", "")
    )


def _sync(url: str, db: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="postgresql", path=f"/{db}", query=""))


def _admin(url: str) -> Any:
    conn = psycopg2.connect(_sync(url, "postgres"))
    conn.autocommit = True
    return conn


@pytest.fixture
def databases() -> Iterator[tuple[str, str]]:
    base = _base_url()
    try:
        admin = _admin(base)
    except Exception as exc:  # noqa: BLE001 - no Postgres
        if os.environ.get("SANDLOOM_E2E_REQUIRED", "").strip() == "1":
            pytest.fail(f"PostgreSQL required: {exc}")
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    run = uuid.uuid4().hex[:8]
    names = (f"sandloom_test_extract_{run}_src", f"sandloom_test_extract_{run}_dst")
    with admin.cursor() as cur:
        for name in names:
            cur.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _sync(base, names[0]), _sync(base, names[1])
    finally:
        with admin.cursor() as cur:
            for name in names:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _descriptor(short: str) -> ModuleDescriptor:
    return ModuleDescriptor(
        name=f"sandloom_{short}",
        version="test",
        migrations_path=str(SRC / f"sandloom_{short}" / "migrations"),
    )


def _migrate(url: str, *modules: str) -> None:
    from alembic import command
    from alembic.config import Config

    from sandloom.core.migrations.migrations import run_core_upgrade

    _exec(url, 'CREATE EXTENSION IF NOT EXISTS "citext"')  # identity's, in a monolith
    run_core_upgrade(dry_run=False, database_url=url)
    for short in modules:
        cfg = Config()
        cfg.set_main_option(
            "script_location", str(SRC / f"sandloom_{short}" / "migrations")
        )
        cfg.set_main_option("sqlalchemy.url", url)
        cfg.set_main_option("version_table", f"alembic_version_{short}")
        command.upgrade(cfg, "head")


def _exec(url: str, sql: str, params: Any = None) -> list[Any]:
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
        conn.commit()
        return list(rows)
    finally:
        conn.close()


ORG = str(uuid.uuid4())
OUTBOX = {
    "billing_pending": str(uuid.uuid4()),
    "billing_dead": str(uuid.uuid4()),
    "billing_delivered": str(uuid.uuid4()),
    "tenancy_pending": str(uuid.uuid4()),
}


def _seed(url: str) -> None:
    stmts = [
        "INSERT INTO billing_products (name, type, is_active) "
        "VALUES ('Pro', 'subscription', true), ('Pack', 'one_time', true)",
        "INSERT INTO billing_prices (product_id, amount, currency, "
        "recurring_interval, stripe_price_id) "
        "VALUES (1, 1000, 'usd', 'month', 'price_pro'), "
        "(2, 500, 'usd', NULL, 'price_pack')",
    ]
    for stmt in stmts:
        _exec(url, stmt)
    for i in range(5):
        cid = str(uuid.uuid4())
        tenant = ORG if i == 0 else str(uuid.uuid4())
        col = "organization_id" if i == 0 else "user_id"
        _exec(
            url,
            f"INSERT INTO billing_customers (id, {col}) VALUES (%s, %s)",
            (cid, tenant),
        )
        _exec(
            url,
            "INSERT INTO billing_credit_balances (customer_id, amount) VALUES (%s, %s)",
            (cid, 100 - i),
        )
        _exec(
            url,
            "INSERT INTO billing_credit_ledger (customer_id, delta, reason, "
            "operation_key, balance_after) VALUES (%s, 100, 'grant', %s, 100), "
            "(%s, %s, 'spend', %s, %s)",
            (cid, f"grant-{i}", cid, -i, f"spend-{i}", 100 - i),
        )
        if i < 2:
            _exec(
                url,
                "INSERT INTO billing_subscriptions (customer_id, price_id, status, "
                "current_period_end, cancel_at_period_end) "
                "VALUES (%s, 1, 'active', now() + interval '30 days', false)",
                (cid,),
            )
    _exec(
        url,
        "INSERT INTO tenancy_organizations (id, name, slug, owner_id, is_active) "
        "VALUES (%s, 'Org', 'org-x', %s, true)",
        (ORG, str(uuid.uuid4())),
    )
    _exec(
        url,
        "INSERT INTO tenancy_members (id, organization_id, user_id, role) "
        "VALUES (%s, %s, %s, 'owner')",
        (str(uuid.uuid4()), ORG, str(uuid.uuid4())),
    )
    payload = json.dumps({"event_id": "e", "data": {"k": "v"}})
    outbox = [
        (OUTBOX["billing_pending"], "billing.credits.spent.v1", None, None),
        (OUTBOX["billing_dead"], "billing.credits.added.v1", None, "now()"),
        (OUTBOX["billing_delivered"], "billing.credits.spent.v1", "now()", None),
        (OUTBOX["tenancy_pending"], "tenancy.member.added.v1", None, None),
    ]
    for oid, channel, delivered, dead in outbox:
        _exec(
            url,
            "INSERT INTO outbox_messages (id, channel, schema_version, payload, "
            "emitted_at, delivery_attempts, delivered_at, dead_at) VALUES "
            f"(%s, %s, 'v1', %s::json, now(), 0, {delivered or 'NULL'}, "
            f"{dead or 'NULL'})",
            (oid, channel, payload),
        )
    for consumer in (
        "billing@orders.paid.v1",
        "sandloom_billing:x@y",
        "sandloom_identity:z",
    ):
        _exec(
            url,
            "INSERT INTO consumer_inbox (id, consumer, event_id, status, "
            "processed_at) VALUES (%s, %s, 'evt-1', 'processed', now())",
            (str(uuid.uuid4()), consumer),
        )
    for contract in ("billing.credits.v1.spend", "tenancy.membership.v1.add"):
        _exec(
            url,
            "INSERT INTO contract_command_log (dedupe_key, contract, tenant_id, "
            "args_digest, status, result, created_at, expires_at) VALUES "
            "(%s, %s, NULL, 'd', 'succeeded', '70', now(), now() + interval '1 day')",
            (uuid.uuid4().hex, contract),
        )
    for scope in ("billing.checkout_intent", "tenancy.org.create"):
        _exec(
            url,
            "INSERT INTO http_idempotency_log (dedupe_key, scope, tenant_id, "
            "args_digest, status, result, created_at, expires_at) VALUES "
            "(%s, %s, NULL, 'd', 'succeeded', '{}', now(), now() + interval '1 day')",
            (uuid.uuid4().hex, scope),
        )
    for saga_type in ("billing.refund", "tenancy.onboard"):
        instance = str(uuid.uuid4())
        _exec(
            url,
            "INSERT INTO saga_instances (id, saga_type, definition_version, saga_id, "
            "status, step_index, direction, resolving, attempt, next_attempt_at, "
            "deadline_at, input, results, applied, version, created_at, updated_at) "
            "VALUES (%s, %s, 1, 'biz-1', 'running', 0, 'forward', false, 0, now(), "
            "now(), '{}', '{}', '[]', 3, now(), now())",
            (instance, saga_type),
        )
        _exec(
            url,
            "INSERT INTO saga_steps (instance_id, step, direction, saga_type, status, "
            "attempts) VALUES (%s, 'charge', 'do', %s, 'failed', 1)",
            (instance, saga_type),
        )
    for action in ("billing.credits.granted", "tenancy.member.role_changed"):
        _exec(
            url,
            "INSERT INTO audit_log (id, occurred_at, actor_type, action, data) "
            "VALUES (%s, now(), 'user', %s, %s::json)",
            (str(uuid.uuid4()), action, json.dumps({"n": 1})),
        )


@pytest.fixture
def monolith(databases: tuple[str, str]) -> tuple[str, str]:
    src, dst = databases
    _migrate(src, "tenancy", "billing")
    _seed(src)
    return src, dst


def _billing(src: str, dst: str, **kw: Any) -> ModuleExtractor:
    spec = module_data_spec(_descriptor("billing"), ["sandloom_billing:x@y"])
    return ModuleExtractor(spec, src, dst, **kw)


def _count(url: str, table: str, where: str = "true") -> int:
    return int(_exec(url, f"SELECT count(*) FROM {table} WHERE {where}")[0][0])


def test_spec_owns_exactly_the_module_tables() -> None:
    spec = module_data_spec(_descriptor("tenancy"))
    assert set(spec.tables) == {
        "tenancy_organizations",
        "tenancy_members",
        "tenancy_invitations",
        "tenancy_member_versions",
    }
    assert spec.channel_prefixes == ("tenancy.",)
    assert spec.inbox_prefixes == ("sandloom_tenancy:", "tenancy@")


def test_same_database_is_refused() -> None:
    spec = module_data_spec(_descriptor("billing"))
    url = "postgresql://u:p@db:5432/app"
    with pytest.raises(ExtractionError, match="same database"):
        ModuleExtractor(spec, url, "postgresql+asyncpg://x@DB/app")


def test_dry_run_writes_nothing(monolith: tuple[str, str]) -> None:
    src, dst = monolith
    report = _billing(src, dst).plan()
    assert not report.applied and not report.problems and not report.fenced
    assert [t.status for t in report.tables] == ["pending"] * len(report.tables)
    names = [t.name for t in report.tables]
    assert names.index("billing_customers") < names.index("billing_credit_ledger")
    assert names.index("billing_products") < names.index("billing_prices")
    assert (report.outbox_pending, report.outbox_dead) == (1, 1)
    assert (report.inbox_rows, report.audit_rows) == (2, 1)
    assert _count(dst, "pg_tables", "tablename LIKE 'billing_%'") == 0
    assert _count(src, "pg_trigger", "tgname LIKE 'sandloom_extract_fence%'") == 0


def test_apply_requires_stopped_writes(monolith: tuple[str, str]) -> None:
    src, dst = monolith
    with pytest.raises(ExtractionError, match="i-have-stopped-writes"):
        _billing(src, dst).apply(writes_stopped=False)


def test_live_writer_blocks_the_fence(monolith: tuple[str, str]) -> None:
    src, dst = monolith
    writer = psycopg2.connect(src)
    try:
        with writer.cursor() as cur:  # an open write transaction: locks held
            cur.execute("UPDATE billing_credit_balances SET amount = amount")
        with pytest.raises(ExtractionError, match="still has writers"):
            _billing(src, dst, lock_timeout_ms=300).apply(writes_stopped=True)
    finally:
        writer.rollback()
        writer.close()
    assert _count(src, "pg_trigger", "tgname LIKE 'sandloom_extract_fence%'") == 0


def test_extract_crash_resume_verify_and_cleanup(monolith: tuple[str, str]) -> None:
    src, dst = monolith
    copied: list[str] = []

    def crash_after_two(table: str) -> None:
        copied.append(table)
        if len(copied) == 2:
            raise RuntimeError("simulated crash mid-extraction")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _billing(src, dst, on_table_copied=crash_after_two).apply(writes_stopped=True)

    # The fence is in place: the stale source copy refuses writes.
    with pytest.raises(psycopg2.Error) as err:
        _exec(src, "UPDATE billing_credit_balances SET amount = 0")
    assert err.value.pgcode == "25006"
    with pytest.raises(psycopg2.Error) as err:
        _exec(
            src,
            "INSERT INTO outbox_messages (id, channel, schema_version, payload, "
            "emitted_at, delivery_attempts) "
            "VALUES (%s, 'billing.x.v1', 'v1', '{}', now(), 0)",
            (str(uuid.uuid4()),),
        )
    assert err.value.pgcode == "25006"
    # Other modules' events are not fenced.
    _exec(
        src,
        "INSERT INTO outbox_messages (id, channel, schema_version, payload, "
        "emitted_at, delivery_attempts, delivered_at) "
        "VALUES (%s, 'tenancy.y.v1', 'v1', '{}', now(), 0, now())",
        (str(uuid.uuid4()),),
    )

    mid = _billing(src, dst).plan()
    assert [t.status for t in mid.tables].count("done") == 2
    assert not mid.complete

    # Resume: the re-run copies only the remaining tables.
    report = _billing(src, dst).apply(writes_stopped=True)
    assert report.applied and report.complete and not report.problems
    assert all(t.status == "done" for t in report.tables)
    for t in report.tables:
        assert t.target_checksum == t.source_checksum, t.name

    # Sequences continue past the copied ids (no PK collision).
    max_id = _exec(dst, "SELECT max(id) FROM billing_credit_ledger")[0][0]
    new_id = _exec(
        dst,
        "INSERT INTO billing_credit_ledger (customer_id, delta, reason, "
        "operation_key, balance_after) SELECT customer_id, 1, 'x', 'after', 1 "
        "FROM billing_credit_balances LIMIT 1 RETURNING id",
    )[0][0]
    assert new_id == max_id + 1

    # Outbox: undelivered billing rows moved (same ids, dead stays dead);
    # the source marks them handed off; other modules' rows untouched.
    tgt_outbox = {
        str(r[0]): r[1]
        for r in _exec(dst, "SELECT id, dead_at IS NOT NULL FROM outbox_messages")
    }
    assert tgt_outbox == {
        OUTBOX["billing_pending"]: False,
        OUTBOX["billing_dead"]: True,
    }
    handed = _exec(
        src,
        "SELECT id, delivered_at IS NOT NULL, dead_at IS NULL, last_error "
        "FROM outbox_messages WHERE id IN (%s, %s)",
        (OUTBOX["billing_pending"], OUTBOX["billing_dead"]),
    )
    assert all(r[1] and r[2] and r[3].startswith(HANDOFF_MARKER) for r in handed)
    assert (
        _count(
            src,
            "outbox_messages",
            f"id = '{OUTBOX['tenancy_pending']}' AND delivered_at IS NULL",
        )
        == 1
    )

    # Inbox claims and audit rows: only the module's.
    assert sorted(r[0] for r in _exec(dst, "SELECT consumer FROM consumer_inbox")) == [
        "billing@orders.paid.v1",
        "sandloom_billing:x@y",
    ]
    assert [r[0] for r in _exec(dst, "SELECT action FROM audit_log")] == [
        "billing.credits.granted"
    ]
    # Command idempotency records of the module's contracts travel with it.
    assert [r[0] for r in _exec(dst, "SELECT contract FROM contract_command_log")] == [
        "billing.credits.v1.spend"
    ]
    # ... and so do the durable HTTP idempotency records of its routes.
    assert [r[0] for r in _exec(dst, "SELECT scope FROM http_idempotency_log")] == [
        "billing.checkout_intent"
    ]
    # ... and its in-flight saga instances, with their step rows.
    assert _exec(dst, "SELECT saga_type, version FROM saga_instances") == [
        ("billing.refund", 3)
    ]
    assert [r[0] for r in _exec(dst, "SELECT saga_type FROM saga_steps")] == [
        "billing.refund"
    ]
    assert _count(dst, MANIFEST_TABLE, f"table_name = '{COMPLETE_MARKER}'") == 1

    # Idempotent: a re-run after the target took writes changes nothing.
    again = _billing(src, dst).apply(writes_stopped=True)
    assert again.applied and not again.problems
    assert _count(dst, "billing_credit_ledger", "operation_key = 'after'") == 1
    _billing(src, dst).verify()

    # Unfence after completion needs --force.
    assert _billing(src, dst).unfence(apply=True).problems

    # drop-source: dry-run first, then apply.
    dry = _billing(src, dst).drop_source(writes_stopped=True, apply=False)
    assert not dry.applied and not dry.problems
    assert _count(src, "pg_tables", "tablename LIKE 'billing_%'") == 10
    dropped = _billing(src, dst).drop_source(writes_stopped=True, apply=True)
    assert dropped.applied
    assert _count(src, "pg_tables", "tablename LIKE 'billing_%'") == 0
    assert _count(src, "pg_tables", "tablename = 'alembic_version_billing'") == 0
    assert _count(src, "pg_tables", "tablename LIKE 'tenancy_%'") == 4
    assert _count(src, "consumer_inbox") == 1  # only identity's claim remains
    assert _count(src, "audit_log") == 2  # audit is never deleted
    assert [r[0] for r in _exec(src, "SELECT contract FROM contract_command_log")] == [
        "tenancy.membership.v1.add"
    ]
    assert [r[0] for r in _exec(src, "SELECT scope FROM http_idempotency_log")] == [
        "tenancy.org.create"
    ]
    assert [r[0] for r in _exec(src, "SELECT saga_type FROM saga_instances")] == [
        "tenancy.onboard"
    ]
    assert [r[0] for r in _exec(src, "SELECT saga_type FROM saga_steps")] == [
        "tenancy.onboard"
    ]


def test_unfence_rolls_back_before_completion(monolith: tuple[str, str]) -> None:
    src, dst = monolith

    def crash(table: str) -> None:
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        _billing(src, dst, on_table_copied=crash).apply(writes_stopped=True)
    report = _billing(src, dst).unfence(apply=True)
    assert report.applied and not report.problems
    _exec(src, "UPDATE billing_credit_balances SET amount = amount")  # writable


def test_non_empty_target_is_refused(monolith: tuple[str, str]) -> None:
    src, dst = monolith
    _migrate(dst, "billing")
    _exec(
        dst,
        "INSERT INTO billing_products (name, type, is_active) "
        "VALUES ('Foreign', 'subscription', true)",
    )
    plan = _billing(src, dst).plan()
    assert any("not written by this tool" in p for p in plan.problems)
    with pytest.raises(ExtractionError, match="refusing to overwrite"):
        _billing(src, dst).apply(writes_stopped=True)


def test_source_behind_head_is_refused(databases: tuple[str, str]) -> None:
    src, dst = databases
    _migrate(src)
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(SRC / "sandloom_billing" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", src)
    cfg.set_main_option("version_table", "alembic_version_billing")
    command.upgrade(cfg, "0001_billing")
    plan = _billing(src, dst).plan()
    assert any("code head" in p for p in plan.problems), plan.problems


async def test_group_owner_transfer_memory_transport() -> None:
    from sandloom.core.eventing.memory_transport import MemoryTransport
    from sandloom.core.eventing.transport_base import GroupOwnershipError

    src_id = database_identity_of_url("postgresql://db:5432/monolith")
    dst_id = database_identity_of_url("postgresql+asyncpg://u:p@billing-db/billing")
    t = MemoryTransport()
    mine = {"module": "sandloom_billing", "database": src_id, "channel": "a.b.v1"}
    other = {"module": "sandloom_identity", "database": src_id, "channel": "a.b.v1"}
    await t.claim_group_owner("a.b.v1", "sandloom_billing:x", mine)
    await t.claim_group_owner("a.b.v1", "sandloom_identity:y", other)

    dry = await transfer_group_owners(
        t,
        "sandloom_billing",
        source_identity=src_id,
        target_identity=dst_id,
        apply=False,
    )
    assert dry == ["a.b.v1 sandloom_billing:x"]
    moved = await transfer_group_owners(
        t,
        "sandloom_billing",
        source_identity=src_id,
        target_identity=dst_id,
        apply=True,
    )
    assert moved == dry
    await t.claim_group_owner(
        "a.b.v1", "sandloom_billing:x", {**mine, "database": dst_id}
    )  # the new service claims it
    with pytest.raises(GroupOwnershipError):
        await t.claim_group_owner(
            "a.b.v1", "sandloom_billing:x", mine
        )  # stale monolith
    await t.claim_group_owner("a.b.v1", "sandloom_identity:y", other)  # untouched


async def test_group_owner_transfer_redis_transport() -> None:
    redis_asyncio = pytest.importorskip("redis.asyncio")
    from sandloom.core.eventing.streams_transport import RedisStreamsTransport
    from sandloom.core.eventing.transport_base import GroupOwnershipError

    values = worker_env_values(BACKEND / ".env.test")
    url = os.environ.get("SANDLOOM_TEST_CACHE_URL") or values.get("CACHE_URL")
    if not url:
        pytest.skip("no CACHE_URL")
    client = redis_asyncio.from_url(url)
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip
        await client.aclose()
        pytest.skip("Redis unavailable")
    prefix = f"tx{uuid.uuid4().hex[:8]}:"

    async def _factory() -> Any:
        return client

    t = RedisStreamsTransport(client_factory=_factory, prefix=prefix)
    try:
        src_id, dst_id = "url:aaaa", "url:bbbb"
        mine = {
            "module": "sandloom_billing",
            "database": src_id,
            "channel": "billing.x.v1",
        }
        await t.claim_group_owner("billing.x.v1", "sandloom_billing:n@c", mine)
        listed = await t.list_group_owners()
        assert listed == [
            {"stream": "billing.x.v1", "group": "sandloom_billing:n@c", "owner": mine}
        ]
        moved = await transfer_group_owners(
            t,
            "sandloom_billing",
            source_identity=src_id,
            target_identity=dst_id,
            apply=True,
        )
        assert moved == ["billing.x.v1 sandloom_billing:n@c"]
        await t.claim_group_owner(
            "billing.x.v1", "sandloom_billing:n@c", {**mine, "database": dst_id}
        )
        with pytest.raises(GroupOwnershipError):
            await t.claim_group_owner("billing.x.v1", "sandloom_billing:n@c", mine)
        assert not await t.transfer_group_owner(
            "billing.x.v1", "sandloom_billing:n@c", expected=mine, owner=mine
        )  # compare-and-set: the owner is no longer ``mine``
    finally:
        keys = [k async for k in client.scan_iter(match=f"{prefix}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def test_shared_rows_stream_idempotently_and_detect_missing_keys(
    databases: tuple[str, str],
) -> None:
    """``_copy_rows`` (audit / inbox / command log) streams through COPY and a
    temp table: JSON survives byte-for-byte, a re-run copies nothing new, and
    a key present in the target but outside the filter is reported missing
    (the same check the old in-memory key-set diff made)."""
    from sandloom.core.cli.extract import _connect

    src, dst = databases
    for url in (src, dst):
        _migrate(url)
    rows = [
        (
            str(uuid.uuid4()),
            f"billing.credits.spent.{i}",
            json.dumps({"n": i, "s": "é"}),
        )
        for i in range(5)
    ]
    for rid, action, data in rows:
        _exec(
            src,
            "INSERT INTO audit_log (id, occurred_at, actor_type, action, data) "
            "VALUES (%s, now(), 'system', %s, %s::json)",
            (rid, action, data),
        )
    _exec(
        src,
        "INSERT INTO audit_log (id, occurred_at, actor_type, action) "
        "VALUES (%s, now(), 'system', 'tenancy.org.created')",
        (str(uuid.uuid4()),),
    )
    copy = _billing(src, dst)._copy_rows
    cols = ("id", "occurred_at", "actor_type", "action", "data")
    args = ("audit_log", cols, "(action LIKE %s)", ["billing.%"], ("id",))
    with _connect(src) as s, _connect(dst) as t:
        copy(s, t, *args)
        copy(s, t, *args)  # idempotent re-run
    got = _exec(
        dst, "SELECT id::text, action, data::text FROM audit_log ORDER BY action"
    )
    assert [(r[0], r[1], json.loads(r[2])) for r in got] == [
        (rid, action, json.loads(data)) for rid, action, data in rows
    ]

    # The target already holds one of the ids under another action: the
    # INSERT skips it (ON CONFLICT) and the check reports it missing.
    _exec(dst, "DELETE FROM audit_log")
    _exec(
        dst,
        "INSERT INTO audit_log (id, occurred_at, actor_type, action) "
        "VALUES (%s, now(), 'system', 'other.action')",
        (rows[0][0],),
    )
    with _connect(src) as s, _connect(dst) as t:
        with pytest.raises(ExtractionError, match="1 row"):
            copy(s, t, *args)
    assert _count(dst, "audit_log") == 1  # rolled back: nothing half-copied


def test_rls_target_role_is_refused_before_fencing(monolith: tuple[str, str]) -> None:
    """A target that cannot copy FORCE-RLS data must not stop source writes."""
    from sqlalchemy.engine import make_url

    src, dst = monolith
    role = f"sandloom_extract_unpriv_{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex
    _exec(
        src,
        f"CREATE ROLE {role} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS",
        (password,),
    )
    try:
        _exec(src, "ALTER TABLE billing_customers ENABLE ROW LEVEL SECURITY")
        _exec(src, "ALTER TABLE billing_customers FORCE ROW LEVEL SECURITY")
        target = (
            make_url(dst)
            .set(username=role, password=password)
            .render_as_string(hide_password=False)
        )
        extractor = _billing(src, target)
        with pytest.raises(ExtractionError, match="target role"):
            extractor.apply(writes_stopped=True)
        assert _billing(src, dst).plan().fenced is False
    finally:
        _exec(src, f"DROP ROLE IF EXISTS {role}")
