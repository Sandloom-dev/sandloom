"""Restore pg_dump into a new local database, boot the strict app on it, verify it."""

from __future__ import annotations

import json
import os
import time
import uuid

import check
from run import STATE, compose, token

RESTORE_DATABASE = "qualification_restore_" + uuid.uuid4().hex[:12]
os.environ["RESTORE_DATABASE"] = RESTORE_DATABASE


def restored_sql(query):
    return compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "qualification_owner",
        "-d",
        RESTORE_DATABASE,
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        query,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main():
    STATE.joinpath("restore.json").unlink(missing_ok=True)
    started = time.monotonic()
    # Quiesce this synthetic writer before the consistency snapshot.
    # pg_dump is transactionally consistent; quiescing lets us compare exact counts.
    compose("stop", "-t", "10", "app")
    try:
        before = check.counts()
        writes_before = check.sql("SELECT count(*) FROM bench_tenant_writes")
        dump = compose(
            "exec",
            "-T",
            "db",
            "pg_dump",
            "-U",
            "qualification_owner",
            "-d",
            "qualification",
            "-Fc",
            capture_output=True,
        ).stdout
        STATE.joinpath("backup.dump").write_bytes(dump)
        check.sql(f"CREATE DATABASE {RESTORE_DATABASE} OWNER qualification_owner")
        compose(
            "exec",
            "-T",
            "db",
            "pg_restore",
            "-U",
            "qualification_owner",
            "-d",
            RESTORE_DATABASE,
            "--exit-on-error",
            input=dump,
            capture_output=True,
        )
        compose("--profile", "restore", "stop", "restored-app", capture_output=True)
        compose(
            "--profile",
            "restore",
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "restored-cache",
            capture_output=True,
        )
        compose("--profile", "restore", "up", "-d", "--no-deps", "restored-app")
    finally:
        compose("up", "-d", "--no-deps", "app")
    jwt = token()
    check.BASE = "http://127.0.0.1:18223"
    check.wait_ready(jwt)
    check.expect("restored own tenant", "/s5/1", jwt, 200)
    check.expect("restored foreign tenant", "/s5/5001", jwt, 404)
    check.expect("restored missing token", "/s5/1", None, 401)
    after = restored_sql(
        "SELECT (SELECT count(*) FROM bench_tenant_orders),"
        "(SELECT count(*) FROM bench_tenant_order_lines),"
        "(SELECT count(*) FROM outbox_messages),"
        "(SELECT count(*) FROM audit_log),"
        "(SELECT count(*) FROM contract_command_log)"
    )
    assert before == after, (before, after)
    writes_after = restored_sql("SELECT count(*) FROM bench_tenant_writes")
    assert writes_before == writes_after, (writes_before, writes_after)
    role = restored_sql(
        "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
        "AND tableowner='qualification_app'"
    )
    assert role == "0", role
    assert (
        restored_sql(
            "SELECT count(*) FROM pg_class WHERE relname LIKE 'bench_tenant_%' "
            "AND relkind='r' AND relrowsecurity AND relforcerowsecurity"
        )
        == "5"
    )
    result = {
        "checks": check.RESULTS,
        "snapshot_counts": before,
        "write_rows_before": writes_before,
        "write_rows_restored": writes_after,
        "restored_counts": after,
        "dump_bytes": len(dump),
        "restore_seconds": round(time.monotonic() - started, 3),
        "scope": "new database, same isolated PostgreSQL cluster; existing roles and security Redis",
    }
    STATE.joinpath("restore.json").write_text(json.dumps(result, indent=2) + "\n")
    compose("--profile", "restore", "stop", "restored-app")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
