"""Talking to the dev services from the host: reachability, databases,
Redis indexes, who else is connected."""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psycopg2  # type: ignore[import-untyped]
from psycopg2 import errors, sql

try:  # the `redis` extra: without it the Redis checks stop at TCP
    import redis
except ImportError:  # pragma: no cover - lean install
    redis = None  # type: ignore[assignment]

#: (user, password) of the generated compose file's local services.
DEV_CREDENTIALS = ("sandloom", "sandloom")
TEST_CREDENTIALS = ("test_user", "test_password")


def service_errors() -> tuple[type[BaseException], ...]:
    """What a client raises when a dev service is down or refuses us."""
    found: tuple[type[BaseException], ...] = (psycopg2.Error, OSError)
    if redis is not None:
        found += (redis.RedisError,)
    return found


@dataclass(frozen=True)
class Postgres:
    host: str
    port: int
    user: str
    password: str

    def url(self, database: str, *, driver: str = "postgresql+asyncpg") -> str:
        return (
            f"{driver}://{self.user}:{self.password}@{self.host}:{self.port}/{database}"
        )

    def connect(self, database: str = "postgres", timeout: int = 5) -> Any:
        conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=database,
            connect_timeout=timeout,
        )
        conn.autocommit = True
        return conn


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pg_ready(pg: Postgres) -> str | None:
    """``None`` when ``SELECT 1`` works, else why not (one line)."""
    if not tcp_open(pg.host, pg.port):
        return "connection refused"
    try:
        conn = pg.connect()
    except psycopg2.Error as exc:
        return str(exc).strip().splitlines()[0] if str(exc).strip() else "error"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    finally:
        conn.close()
    return None


def ensure_database(pg: Postgres, name: str) -> bool:
    """Create ``name`` if missing (``True``: created)."""
    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone():
                return False
            try:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            except errors.DuplicateDatabase:
                return False
            return True
    finally:
        conn.close()


def drop_database(pg: Postgres, name: str) -> None:
    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
    finally:
        conn.close()


def database_exists(pg: Postgres, name: str) -> bool:
    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def has_tables(pg: Postgres, name: str) -> bool:
    """Whether database ``name`` holds a table outside the system schemas
    (an empty one has never been migrated)."""
    conn = pg.connect(name)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname NOT IN "
                "('pg_catalog', 'information_schema'))"
            )
            row = cur.fetchone()
            return bool(row and row[0])
    finally:
        conn.close()


def sessions(pg: Postgres, databases: Iterable[str] | None = None) -> dict[str, int]:
    """``{database: other client connections}`` (this connection excluded)."""
    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT datname, count(*) FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() AND datname IS NOT NULL "
                "AND backend_type = 'client backend' GROUP BY datname"
            )
            rows = {str(name): int(n) for name, n in cur.fetchall()}
    finally:
        conn.close()
    if databases is None:
        return rows
    wanted = set(databases)
    return {k: v for k, v in rows.items() if k in wanted}


def advisory_holders(pg: Postgres) -> int:
    """Sessions holding an advisory lock (test-run slots) on this server."""
    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(DISTINCT pid) FROM pg_locks "
                "WHERE locktype = 'advisory' AND granted AND pid <> pg_backend_pid()"
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    finally:
        conn.close()


def _redis(host: str, port: int, db: int = 0) -> Any:
    if redis is None:
        raise ImportError("the redis client is not installed (sandloom[redis])")
    return redis.Redis(
        host=host, port=port, db=db, socket_connect_timeout=3, socket_timeout=5
    )


def redis_ready(host: str, port: int) -> str | None:
    if not tcp_open(host, port):
        return "connection refused"
    try:
        client = _redis(host, port)
    except ImportError:
        return None  # no redis client installed: the TCP check stands
    try:
        client.ping()
    except service_errors() as exc:
        return str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    finally:
        with contextlib.suppress(*service_errors()):
            client.close()
    return None


def redis_clients(host: str, port: int, indexes: Iterable[int] | None = None) -> int:
    """Other clients connected to the server (to ``indexes`` when given)."""
    client = _redis(host, port)
    try:
        own = int(client.execute_command("CLIENT", "ID"))
        wanted = None if indexes is None else set(indexes)
        count = 0
        for entry in client.client_list():
            if int(entry.get("id", -1)) == own:
                continue
            if wanted is None or int(entry.get("db", 0)) in wanted:
                count += 1
        return count
    finally:
        client.close()


def flush_indexes(host: str, port: int, indexes: Iterable[int]) -> None:
    for index in indexes:
        client = _redis(host, port, index)
        try:
            client.flushdb()
        finally:
            client.close()


__all__ = [
    "DEV_CREDENTIALS",
    "TEST_CREDENTIALS",
    "Postgres",
    "advisory_holders",
    "database_exists",
    "drop_database",
    "ensure_database",
    "flush_indexes",
    "has_tables",
    "pg_ready",
    "redis_clients",
    "redis_ready",
    "service_errors",
    "sessions",
    "tcp_open",
]
