"""Ratchet: the kernel imports no backend SDK outside ``core/drivers`` (ADR-0016).

``src/sandloom/core`` reaches Redis, Celery, S3 or PostgreSQL only through a
capability provider, so a driver package can own the SDK and the kernel starts
without it. This test counts every import of ``redis``, ``aioredis``,
``celery``, ``kombu``, ``boto3``, ``botocore``, ``aiobotocore``, ``asyncpg``,
``psycopg`` and ``psycopg2`` (a module-level, a function-local, an
``if TYPE_CHECKING:`` import and an ``importlib.import_module("<sdk>...")`` with
a literal name) in the kernel and its two public siblings, ``sandloom.sdk`` and
``sandloom.shared``, outside ``core/drivers`` and outside the kernel's own unit
tests (``core/tests``, ``core/**/tests``).

The allowlist ``kernel_backend_import_allowlist.json`` is seeded with today's
sites and shrinks only:

* a (file, SDK) pair that is not in it fails, and so does a pair with more
  sites than it allows;
* a stale entry (fewer sites than listed, or none) does NOT fail, so the slice
  that removes a site never edits this file; ``idk-ratchets-close`` prunes the
  entries and makes the test strict;
* every entry names ``removed_by``, the slice that removes it (or the
  justification when the site stays).

An entry is keyed by file and SDK, never by line, so a shifted line does not
fail the test.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SANDLOOM_ROOT = REPO / "src" / "sandloom"
CORE = SANDLOOM_ROOT / "core"
DRIVERS = CORE / "drivers"
ALLOWLIST = Path(__file__).with_name("kernel_backend_import_allowlist.json")

SDK = SANDLOOM_ROOT / "sdk"
SHARED = SANDLOOM_ROOT / "shared"

BACKEND_SDKS = frozenset(
    {
        "redis",
        "aioredis",
        "celery",
        "kombu",
        "boto3",
        "botocore",
        "aiobotocore",
        "asyncpg",
        "psycopg",
        "psycopg2",
    }
)


def _sdk_of(module: str | None) -> str | None:
    root = (module or "").split(".")[0]
    return root if root in BACKEND_SDKS else None


def find_backend_imports(path: Path) -> Counter[str]:
    """SDK root -> number of import sites in ``path``, at any nesting depth."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites: Counter[str] = Counter()
    for node in ast.walk(tree):
        found: str | None = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                found = _sdk_of(alias.name)
                if found:
                    sites[found] += 1
            continue
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            found = _sdk_of(node.module)
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                )
                or (isinstance(node.func, ast.Name) and node.func.id == "__import__")
            )
        ):
            found = _sdk_of(node.args[0].value)
        if found:
            sites[found] += 1
    return sites


def kernel_files() -> list[Path]:
    return sorted(
        path
        for path in CORE.rglob("*.py")
        if DRIVERS not in path.parents and "tests" not in path.relative_to(CORE).parts
    )


def scanned_files() -> list[Path]:
    """The kernel plus ``sandloom.sdk`` and ``sandloom.shared`` (no unit tests)."""
    return sorted(
        [
            *kernel_files(),
            *(
                path
                for root in (SDK, SHARED)
                for path in root.rglob("*.py")
                if "tests" not in path.relative_to(root).parts
            ),
        ]
    )


def scan_kernel() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in scanned_files():
        for sdk, count in find_backend_imports(path).items():
            found[(str(path.relative_to(SANDLOOM_ROOT)), sdk)] = count
    return found


def load_allowlist() -> Mapping[tuple[str, str], Mapping[str, Any]]:
    entries = json.loads(ALLOWLIST.read_text(encoding="utf-8"))["entries"]
    return {(entry["file"], entry["sdk"]): entry for entry in entries}


def new_offences(
    found: dict[tuple[str, str], int],
    allowed: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[str]:
    offences: list[str] = []
    for (rel, sdk), count in sorted(found.items()):
        entry = allowed.get((rel, sdk))
        limit = int(entry["sites"]) if entry else 0
        if count > limit:
            offences.append(f"{rel}: {count} import(s) of {sdk} (allowed {limit})")
    return offences


def test_the_kernel_imports_no_new_backend_sdk() -> None:
    offences = new_offences(scan_kernel(), load_allowlist())
    assert offences == [], (
        "The kernel reaches a backend SDK outside core/drivers (ADR-0016). Reach "
        "it through a capability provider; the driver owns the SDK. The "
        "allowlist only shrinks:\n" + "\n".join(offences)
    )


def test_every_allowlist_entry_names_its_removal() -> None:
    raw = json.loads(ALLOWLIST.read_text(encoding="utf-8"))["entries"]
    problems = [
        f"{entry.get('file')}/{entry.get('sdk')}"
        for entry in raw
        if not (
            entry.get("sdk") in BACKEND_SDKS
            and isinstance(entry.get("sites"), int)
            and entry["sites"] > 0
            and str(entry.get("removed_by", "")).strip()
            and (SANDLOOM_ROOT / entry.get("file", "")).is_file()
        )
    ]
    assert problems == [], (
        f"allowlist entries without sdk/sites/removed_by/file: {problems}"
    )
    keys = [(entry["file"], entry["sdk"]) for entry in raw]
    assert len(keys) == len(set(keys)), "duplicate allowlist entry"
    assert keys == sorted(keys), "keep the allowlist sorted by file, then SDK"
    assert not [k for k in keys if k[0].startswith("core/drivers/")], (
        "core/drivers is exempt, never allowlisted"
    )


# -- self-tests: each shape the scanner must count -----------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import redis\n", {"redis": 1}),
        ("import redis.asyncio as aioredis\n", {"redis": 1}),
        ("from redis.exceptions import RedisError\n", {"redis": 1}),
        ("from celery import signals\n", {"celery": 1}),
        ("def f():\n    from kombu.exceptions import EncodeError\n", {"kombu": 1}),
        ("import boto3, asyncpg\n", {"boto3": 1, "asyncpg": 1}),
        ("import psycopg\nfrom psycopg import sql\n", {"psycopg": 2}),
        ("import aioredis\n", {"aioredis": 1}),
        (
            "import botocore.session\nfrom aiobotocore import session\n",
            {"botocore": 1, "aiobotocore": 1},
        ),
        (
            "def f():\n    import psycopg2\n    from psycopg2 import sql\n",
            {"psycopg2": 2},
        ),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import redis\n",
            {"redis": 1},
        ),
        (
            "import importlib\nm = importlib.import_module('celery.signals')\n",
            {"celery": 1},
        ),
        ("m = __import__('redis')\n", {"redis": 1}),
        ("import redisx\nimport sandloom.core.redis_thing\nfrom . import redis\n", {}),
    ],
)
def test_the_scanner_counts_every_spelling(
    tmp_path: Path, source: str, expected: dict[str, int]
) -> None:
    sample = tmp_path / "leaky.py"
    sample.write_text(source)
    assert dict(find_backend_imports(sample)) == expected


def test_a_new_site_fails_and_a_stale_entry_does_not() -> None:
    allowed = {("core/x.py", "redis"): {"sites": 2}}
    assert new_offences({("core/x.py", "redis"): 2}, allowed) == []
    assert new_offences({("core/x.py", "redis"): 1}, allowed) == []  # shrunk: stale
    assert new_offences({}, allowed) == []  # gone: stale
    assert len(new_offences({("core/x.py", "redis"): 3}, allowed)) == 1  # grew
    assert len(new_offences({("core/y.py", "redis"): 1}, allowed)) == 1  # new file
    assert len(new_offences({("core/x.py", "celery"): 1}, allowed)) == 1  # new SDK


def test_the_scan_sees_the_kernel_and_skips_drivers_and_unit_tests() -> None:
    rels = {str(p.relative_to(CORE)) for p in kernel_files()}
    assert "worker.py" in rels and "testing/resources.py" in rels
    assert not [r for r in rels if r.startswith(("drivers/", "tests/"))]


def test_the_scan_also_sees_sdk_and_shared() -> None:
    rels = {str(p.relative_to(SANDLOOM_ROOT)) for p in scanned_files()}
    assert "core/worker.py" in rels
    assert any(r.startswith("sdk/") for r in rels)
    assert any(r.startswith("shared/") for r in rels)
    assert not [r for r in rels if r.startswith("core/drivers/")]


# -- the import-linter half: eventing speaks no transport ----------------------

IMPORTLINTER = REPO / ".importlinter"
EVENTING_CONTRACT = "importlinter:contract:eventing-no-transport-impl"
TRANSPORT_IMPL = {
    "sandloom.core.eventing.streams_transport",
    "sandloom.core.eventing.streams_transport_base",
    "sandloom.core.eventing.streams_consume",
    "sandloom.core.eventing.streams_groups",
}


def _eventing_contract() -> dict[str, str]:
    import configparser

    parser = configparser.ConfigParser()
    parser.read(IMPORTLINTER, encoding="utf-8")
    return dict(parser[EVENTING_CONTRACT])


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def test_the_eventing_contract_forbids_every_transport_and_backend() -> None:
    contract = _eventing_contract()
    assert contract["type"] == "forbidden"
    forbidden = set(_lines(contract["forbidden_modules"]))
    assert TRANSPORT_IMPL <= forbidden
    assert {"sandloom.core.drivers", *BACKEND_SDKS} <= forbidden
    # G-KERNEL-04: no kernel contract carries an ignore_imports escape.
    assert "ignore_imports" not in contract


def test_every_eventing_module_is_a_delivery_source_or_a_transport_file() -> None:
    """import-linter does not check a package against a forbidden module inside
    it, so the contract lists the delivery modules one by one. A new eventing
    module must be placed on purpose: a source here (and in the contract), or a
    transport file (TRANSPORT_IMPL, until it moves to a driver)."""
    sources = set(_lines(_eventing_contract()["source_modules"]))
    assert not sources & TRANSPORT_IMPL, "a transport file is not a source"
    on_disk = {
        f"sandloom.core.eventing.{path.stem}"
        for path in (CORE / "eventing").glob("*.py")
        if path.stem != "__init__"
    }
    unplaced = on_disk - sources - TRANSPORT_IMPL
    assert not unplaced, (
        f"place {sorted(unplaced)} in .importlinter {EVENTING_CONTRACT} "
        "source_modules (delivery logic) or, if it speaks a backend, in "
        "TRANSPORT_IMPL and forbidden_modules"
    )
