"""Coordinated fixture backup and recovery into a NEW PostgreSQL/Redis stack.

Run backup on the source host while other qualification scripts are stopped.
Transfer .state/disaster-backup securely, then restore on another Docker host.
Backup material includes synthetic secrets and must NEVER be committed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import time

import check
from run import ROOT, STATE, compose, token

BACKUP = STATE / "disaster-backup"


def private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    path.chmod(0o600)


def run(*command, **kwargs):
    return subprocess.run(command, check=True, capture_output=True, **kwargs)


def backup():
    BACKUP.mkdir(mode=0o700, exist_ok=True)
    healthy, blocked, revoked = token(), token(), token()

    def claims(bearer):
        return json.loads(base64.urlsafe_b64decode(bearer.split(".")[1] + "=="))

    block_key = "sandloom:auth:blocked:" + claims(blocked)["sub"]
    revoke_key = "sandloom:auth:tokens_valid_after:" + claims(revoked)["sub"]
    compose(
        "exec",
        "-T",
        "security",
        "redis-cli",
        "SET",
        block_key,
        "recovery-fixture",
        capture_output=True,
    )
    compose(
        "exec",
        "-T",
        "security",
        "redis-cli",
        "SET",
        revoke_key,
        str(time.time() + 2),
        "EX",
        "86400",
        capture_output=True,
    )
    check.expect("source valid token", "/s5/1", healthy, 200)
    check.expect("source blocked token", "/s5/1", blocked, 401)
    check.expect("source revoked token", "/s5/1", revoked, 401)
    container = compose(
        "ps", "-q", "app", capture_output=True, text=True
    ).stdout.strip()
    source_image = run(
        "docker", "inspect", "--format", "{{.Image}}", container, text=True
    ).stdout.strip()
    compose(
        "exec",
        "-T",
        "app",
        "python",
        "-c",
        "from pathlib import Path;p=Path('/app/data');p.mkdir(exist_ok=True);"
        "(p/'recovery.txt').write_bytes(b'Sandloom coordinated recovery fixture\\n')",
        capture_output=True,
    )
    compose("stop", "-t", "15", "app", capture_output=True)
    try:
        # With the only writers stopped these snapshots describe one recovery
        # point. This exercise does not claim online PITR or an arbitrary RPO.
        config = json.loads(
            compose("config", "--format", "json", capture_output=True, text=True).stdout
        )
        app = {k: config["services"]["app"][k] for k in ("command", "environment")}
        app["image"] = source_image
        private(BACKUP / "app.json", json.dumps(app).encode())
        private(
            BACKUP / "tokens.json",
            json.dumps(
                {"healthy": healthy, "blocked": blocked, "revoked": revoked}
            ).encode(),
        )
        if (STATE / "real-subjects.json").exists():
            private(
                BACKUP / "subjects.json", (STATE / "real-subjects.json").read_bytes()
            )
        for name, command in (
            (
                "database.dump",
                ("pg_dump", "-U", "qualification_owner", "-d", "qualification", "-Fc"),
            ),
            (
                "roles.sql",
                ("pg_dumpall", "-U", "qualification_owner", "--globals-only"),
            ),
        ):
            private(
                BACKUP / name,
                compose("exec", "-T", "db", *command, capture_output=True).stdout,
            )
        for service in ("security", "broker"):
            compose("exec", "-T", service, "redis-cli", "SAVE", capture_output=True)
            private(
                BACKUP / (service + ".rdb"),
                compose(
                    "exec", "-T", service, "cat", "/data/dump.rdb", capture_output=True
                ).stdout,
            )
        # Local object storage is fixture-only, but include and verify a file
        # so recovery tests more than SQL rows and authentication state.
        content = compose(
            "run",
            "--rm",
            "--no-deps",
            "app",
            "python",
            "-c",
            "from pathlib import Path;import sys;"
            "sys.stdout.buffer.write(Path('/app/data/recovery.txt').read_bytes())",
            capture_output=True,
        ).stdout
        private(BACKUP / "object.txt", content)
        tables = check.sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).splitlines()
        counts = {
            table: int(check.sql(f'SELECT count(*) FROM "{table}"')) for table in tables
        }
        source_id = check.sql("SELECT system_identifier FROM pg_control_system()")
        manifest = {
            "source_image": source_image,
            "created_epoch": time.time(),
            "source_postgres_system_identifier": source_id,
            "table_counts": counts,
            "object_sha256": hashlib.sha256(content).hexdigest(),
            "files": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in BACKUP.iterdir()
                if p.is_file() and p.name != "manifest.json"
            },
        }
        private(BACKUP / "manifest.json", json.dumps(manifest, indent=2).encode())
        print(
            json.dumps(
                {"backup": str(BACKUP), "tables": len(tables), "coordinated": True}
            ),
            flush=True,
        )
    finally:
        compose("up", "-d", "--no-deps", "app", capture_output=True)
        check.wait_ready(healthy)
        compose(
            "exec",
            "-T",
            "security",
            "redis-cli",
            "DEL",
            block_key,
            revoke_key,
            capture_output=True,
        )


def restore():
    started = time.monotonic()
    manifest = json.loads((BACKUP / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((BACKUP / name).read_bytes()).hexdigest() == digest, name
    spec = json.loads((BACKUP / "app.json").read_text())
    tokens = json.loads((BACKUP / "tokens.json").read_text())
    target = STATE / "disaster-target"
    target.mkdir(mode=0o700, exist_ok=True)
    project = "sandloom-release-disaster-recovery"
    config_path = target / "compose.json"
    volume_prefix = project + "_"
    bootstrap_password = (
        spec["environment"]["DATABASE_URL"].split(":", 2)[2].split("@", 1)[0]
    )
    redis_services = {
        name: {
            "image": "redis:7-alpine",
            "command": ["redis-server", "--appendonly", "no", "--save", ""],
            "volumes": [f"{name}:/data"],
            "cpus": 0.5,
            "mem_limit": "384m",
        }
        for name in ("security", "broker")
    }
    config = {
        "services": {
            "db": {
                "image": "postgres:16-alpine",
                "environment": {
                    "POSTGRES_USER": "recovery_bootstrap",
                    "POSTGRES_PASSWORD": bootstrap_password,
                    "POSTGRES_DB": "postgres",
                },
                "volumes": ["pg:/var/lib/postgresql/data"],
                "cpus": 2,
                "mem_limit": "1g",
            },
            "cache": {
                "image": "redis:7-alpine",
                "command": ["redis-server", "--save", ""],
                "mem_limit": "192m",
            },
            **redis_services,
            "app": {
                **spec,
                "ports": ["127.0.0.1:18224:2222"],
                "volumes": [f"{ROOT.parent}:/benchmarks:ro", "objects:/app/data"],
                "cpus": 2,
                "mem_limit": "1g",
            },
        },
        "volumes": {name: {} for name in ("pg", "security", "broker", "objects")},
    }
    private(config_path, json.dumps(config).encode())

    def clone(*args, **kwargs):
        return run(
            "docker", "compose", "-p", project, "-f", str(config_path), *args, **kwargs
        )

    def sql(query):
        return clone(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "recovery_bootstrap",
            "-d",
            "qualification",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            query,
            text=True,
        ).stdout.strip()

    # A previous clone must never accidentally make a recovery look successful.
    existing = run(
        "docker", "volume", "ls", "--format", "{{.Name}}", text=True
    ).stdout.splitlines()
    assert not any(v.startswith(volume_prefix) for v in existing), (
        "recovery volumes already exist; use a deliberate fresh target"
    )
    report = {
        "passed": False,
        "source_tables": len(manifest["table_counts"]),
        "scope": "new cluster, roles, keys, independent security/broker Redis and local object file",
    }
    try:
        for name in ("security", "broker", "objects"):
            run("docker", "volume", "create", volume_prefix + name)
            source = (
                BACKUP / (name + ".rdb") if name != "objects" else BACKUP / "object.txt"
            )
            destination = "dump.rdb" if name != "objects" else "recovery.txt"
            clone_command = (
                f"cat > /target/{destination}; chown -R "
                + ("999:999" if name != "objects" else "10001:10001")
                + " /target"
            )
            run(
                "docker",
                "run",
                "--rm",
                "-i",
                "-v",
                volume_prefix + name + ":/target",
                "redis:7-alpine",
                "sh",
                "-ec",
                clone_command,
                input=source.read_bytes(),
            )
        clone("up", "-d", "db", "cache", "security", "broker")
        for _ in range(60):
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    project,
                    "-f",
                    str(config_path),
                    "exec",
                    "-T",
                    "db",
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "recovery_bootstrap",
                ],
                capture_output=True,
            )
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("fresh database did not start")
        clone(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "recovery_bootstrap",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            input=(BACKUP / "roles.sql").read_bytes(),
        )
        clone(
            "exec",
            "-T",
            "db",
            "createdb",
            "-U",
            "recovery_bootstrap",
            "-O",
            "qualification_owner",
            "qualification",
        )
        clone(
            "exec",
            "-T",
            "db",
            "pg_restore",
            "-U",
            "recovery_bootstrap",
            "-d",
            "qualification",
            "--exit-on-error",
            input=(BACKUP / "database.dump").read_bytes(),
        )
        for table, count in manifest["table_counts"].items():
            assert int(sql(f'SELECT count(*) FROM "{table}"')) == count, table
        report["all_initial_table_counts_match"] = True
        for name in ("security", "broker"):
            clone(
                "exec",
                "-T",
                name,
                "redis-cli",
                "CONFIG",
                "SET",
                "appendfsync",
                "always",
            )
            clone("exec", "-T", name, "redis-cli", "CONFIG", "SET", "appendonly", "yes")
            for _ in range(60):
                info = clone(
                    "exec", "-T", name, "redis-cli", "INFO", "persistence", text=True
                ).stdout
                if (
                    "aof_rewrite_in_progress:0" in info
                    and "aof_last_bgrewrite_status:ok" in info
                ):
                    break
                time.sleep(1)
            else:
                raise AssertionError("restored AOF did not become durable")
            config["services"][name]["command"] = [
                "redis-server",
                "--appendonly",
                "yes",
                "--appendfsync",
                "always",
                "--maxmemory",
                "256mb",
                "--maxmemory-policy",
                "noeviction",
            ]
        private(config_path, json.dumps(config).encode())
        clone("up", "-d", "--force-recreate", "security", "broker")
        clone("up", "-d", "--no-deps", "app")
        check.BASE = "http://127.0.0.1:18224"
        check.wait_ready(tokens["healthy"])
        for label, path, bearer, status in (
            ("restored valid key", "/s5/1", tokens["healthy"], 200),
            ("restored tenant isolation", "/s5/5001", tokens["healthy"], 404),
            ("restored blocked user", "/s5/1", tokens["blocked"], 401),
            ("restored revocation", "/s5/1", tokens["revoked"], 401),
        ):
            check.expect(label, path, bearer, status)
        assert (
            sql("SELECT system_identifier FROM pg_control_system()")
            != manifest["source_postgres_system_identifier"]
        )
        assert (
            sql(
                "SELECT rolsuper OR rolbypassrls OR pg_has_role(rolname,'qualification_owner','member') FROM pg_roles WHERE rolname='qualification_app'"
            )
            == "f"
        )
        assert (
            sql(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner='qualification_app'"
            )
            == "0"
        )
        assert (
            sql(
                "SELECT count(*) FROM pg_class WHERE relname LIKE 'bench_tenant_%' AND relkind='r' AND relrowsecurity AND relforcerowsecurity"
            )
            == "5"
        )
        raw = clone("exec", "-T", "app", "cat", "/app/data/recovery.txt").stdout
        assert hashlib.sha256(raw).hexdigest() == manifest["object_sha256"]
        # Validate password login and remote claims against a RESTORED account.
        import real_workload

        subject = real_workload.Subject(
            **json.loads((BACKUP / "subjects.json").read_text())[0]
        )
        assert subject.login() == 200, "restored identity login failed"
        assert (
            real_workload.request(
                "GET", f"/api/v1/billing/credits?org_id={subject.org_id}", subject.jwt
            )[0]
            == 200
        )
        report.update(
            passed=True,
            checks=check.RESULTS,
            distinct_cluster=True,
            role_restored=True,
            secrets_restored=True,
            blocked_and_revoked_rejected=True,
            object_restored=True,
            real_login_and_billing=True,
        )
    finally:
        report["seconds"] = round(time.monotonic() - started, 3)
        STATE.joinpath("disaster-recovery.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        clone("stop")  # Keep the recovery volumes and private backup for inspection.
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    args = parser.parse_args()
    backup() if args.action == "backup" else restore()
