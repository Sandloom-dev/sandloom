"""Architecture rule: infrastructure backends are constructed ONLY in drivers.

The whole point of the infra plugin layer is that infrastructure is reached
through capability contracts and constructed by a single owner — the driver.
No other code (a business module, a core helper, a "legacy global") may call a
concrete backend constructor directly; if it does, that backend is no longer
swappable by configuration and the "infra-agnostic" promise is a fiction.

This test fails if a backend constructor is called outside ``drivers/``.
Constructors checked: ``create_async_engine`` (SQLAlchemy), ``redis.from_url``
and ``redis.Redis(...)`` / ``StrictRedis(...)`` (Redis, any alias or import
spelling), ``psycopg2.connect`` (PostgreSQL), ``Storage(...)`` (filesystem
object store), ``Celery(...)``.

The legacy ``sandloom.core.{db,cache,queue,storage}`` modules are deliberately NOT
exempt: after the inversion they are thin shims that forward to the driver and
must contain no construction. There are NO exceptions: the Celery WORKER
entrypoint (``sandloom.core.worker``) resolves its app through the taskqueue
capability (the driver builds it), and modules declare worker tasks with the
app-agnostic ``@shared_task`` in ``<module>/tasks.py`` — nobody else calls
``Celery(...)``.

Ratchet (ADR-0016, slice ``idk-ratchets``): the PostgreSQL ops tools and the
test harness connect with ``psycopg2.connect`` and the dev services with
``redis.Redis``; ``SEEDED`` lists today's sites by file and constructor (never
by line), each with the slice that removes it or why it stays. It is
shrink-only: a new site or a higher count fails, a stale entry does not
(``idk-ratchets-close`` prunes it and makes the test strict).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src" / "sandloom"
DRIVERS_DIR = SANDLOOM_ROOT / "core" / "drivers"

# Bare-name constructor calls, e.g. ``create_async_engine(...)`` / ``Celery(...)``.
FORBIDDEN_NAME_CALLS = {
    "create_async_engine",
    "Celery",
    "Storage",
    "Redis",
    "StrictRedis",
}
# Attribute constructor calls, e.g. ``redis.from_url(...)``.
FORBIDDEN_ATTR_CALLS = {("redis", "from_url"), ("psycopg2", "connect")}
# ``<anything>.Redis(...)``: ``redis.Redis``, ``redis.asyncio.Redis``,
# ``aioredis.Redis`` under whatever alias the file imported it as.
FORBIDDEN_ANY_RECEIVER_ATTRS = {"Redis", "StrictRedis"}

# Seeded, shrink-only: (file relative to src/sandloom, constructor) ->
# (sites, removed_by). Every entry is a hole in the invariant; ``removed_by``
# names the slice that removes it or why it stays. A stale entry does not fail.
_OPS = "stays: PostgreSQL ops tool (design.md section 4: psycopg2 stays where the ops tools need it)"
_HARNESS = "stays: PostgreSQL test harness (design.md section 4)"
SEEDED: dict[tuple[str, str], tuple[int, str]] = {
    ("core/cli/dev/services.py", "Redis"): (1, "idk-test-harness"),
    ("core/cli/dev/services.py", "psycopg2.connect"): (1, _OPS),
    ("core/migrations/backup.py", "psycopg2.connect"): (2, _OPS),
    ("core/testing/connections.py", "psycopg2.connect"): (1, _HARNESS),
    ("core/testing/migrations.py", "psycopg2.connect"): (2, _HARNESS),
    ("core/testing/ownership.py", "Redis"): (1, "idk-test-harness"),
    ("core/testing/ownership.py", "psycopg2.connect"): (1, _HARNESS),
    ("core/testing/resources.py", "Redis"): (1, "idk-test-harness"),
    ("core/testing/resources.py", "psycopg2.connect"): (2, _HARNESS),
    ("core/testing/session_checks.py", "psycopg2.connect"): (1, _HARNESS),
    ("core/testing/slots.py", "psycopg2.connect"): (1, _HARNESS),
    ("core/testing/workers.py", "psycopg2.connect"): (2, _HARNESS),
}


def _find_backend_constructions(file_path: Path) -> list[str]:
    tree = ast.parse(file_path.read_text())
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAME_CALLS:
            hits.append(f"Line {node.lineno}: {func.id}(...)")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in FORBIDDEN_ANY_RECEIVER_ATTRS
        ):
            hits.append(f"Line {node.lineno}: {func.attr}(...)")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "from_url"
            and isinstance(func.value, ast.Attribute | ast.Name)
            and (
                func.value.attr
                if isinstance(func.value, ast.Attribute)
                else func.value.id
            )
            in FORBIDDEN_ANY_RECEIVER_ATTRS
        ):
            hits.append(f"Line {node.lineno}: Redis(...)")  # ``Redis.from_url``
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and (func.value.id, func.attr) in FORBIDDEN_ATTR_CALLS
        ):
            hits.append(f"Line {node.lineno}: {func.value.id}.{func.attr}(...)")
    return hits


def _constructor_sites(file_path: Path) -> dict[str, int]:
    """Constructor -> number of call sites (line-free, for the seeded keys)."""
    sites: dict[str, int] = {}
    for hit in _find_backend_constructions(file_path):
        ctor = hit.split(": ", 1)[1].removesuffix("(...)")
        sites[ctor] = sites.get(ctor, 0) + 1
    return sites


def _new_sites(rel: str, sites: dict[str, int]) -> list[str]:
    return [
        f"{ctor}(...) x{count} (allowed {SEEDED.get((rel, ctor), (0, ''))[0]})"
        for ctor, count in sorted(sites.items())
        if count > SEEDED.get((rel, ctor), (0, ""))[0]
    ]


def _iter_production_files() -> list[Path]:
    files: list[Path] = []
    for py in SANDLOOM_ROOT.glob("**/*.py"):
        if "tests" in py.parts:
            continue
        if DRIVERS_DIR in py.parents:  # drivers ARE the construction site
            continue
        files.append(py)
    return files


@pytest.mark.parametrize(
    "file_path",
    _iter_production_files(),
    ids=lambda p: str(p.relative_to(SANDLOOM_ROOT)),
)
def test_no_backend_construction_outside_drivers(file_path: Path) -> None:
    rel = str(file_path.relative_to(SANDLOOM_ROOT))
    hits = _new_sites(rel, _constructor_sites(file_path))
    if hits:
        pytest.fail(
            f"Backend constructed outside drivers/ in '{rel}':\n"
            + "\n".join(hits)
            + "\n\nInfrastructure must be constructed by its DRIVER (the single "
            "owner), then resolved by logical name through the capability "
            "registry. Move the construction into sandloom/core/drivers/ and "
            "resolve it via the facade / di / a driver-owned factory.",
            pytrace=False,
        )


def test_analyzer_detects_construction(tmp_path: Path) -> None:
    """Self-test: the analyzer flags each forbidden constructor shape."""
    sample = tmp_path / "leaky.py"
    sample.write_text(
        "def f():\n"
        "    e = create_async_engine('x')\n"
        "    c = redis.from_url('y')\n"
        "    s = Storage()\n"
        "    a = Celery('z')\n"
        "    r = redis.Redis(host='h')\n"
        "    r2 = aioredis.Redis(host='h')\n"
        "    r3 = redis.asyncio.Redis.from_url('u')\n"
        "    r4 = Redis()\n"
        "    q = psycopg2.connect('dsn')\n"
    )
    assert len(_find_backend_constructions(sample)) == 9
    assert _constructor_sites(sample)["Redis"] == 4


def test_a_seeded_site_passes_a_new_one_fails_a_stale_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(SEEDED, ("core/x.py", "psycopg2.connect"), (2, "idk-x"))
    assert _new_sites("core/x.py", {"psycopg2.connect": 2}) == []
    assert _new_sites("core/x.py", {"psycopg2.connect": 1}) == []
    assert _new_sites("core/x.py", {}) == []
    assert len(_new_sites("core/x.py", {"psycopg2.connect": 3})) == 1
    assert len(_new_sites("core/y.py", {"psycopg2.connect": 1})) == 1
    assert len(_new_sites("core/x.py", {"Redis": 1})) == 1
