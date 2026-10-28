"""Architecture rule: infrastructure backends are constructed ONLY in drivers.

The whole point of the infra plugin layer is that infrastructure is reached
through capability contracts and constructed by a single owner — the driver.
No other code (a business module, a core helper, a "legacy global") may call a
concrete backend constructor directly; if it does, that backend is no longer
swappable by configuration and the "infra-agnostic" promise is a fiction.

This test fails if a backend constructor is called outside ``drivers/``.
Constructors checked: ``create_async_engine`` (SQLAlchemy), ``redis.from_url``
(Redis), ``Storage(...)`` (filesystem object store), ``Celery(...)``.

The legacy ``sandloom.core.{db,cache,queue,storage}`` modules are deliberately NOT
exempt: after the inversion they are thin shims that forward to the driver and
must contain no construction. There are NO exceptions: the Celery WORKER
entrypoint (``sandloom.core.worker``) resolves its app through the taskqueue
capability (the driver builds it), and modules declare worker tasks with the
app-agnostic ``@shared_task`` in ``<module>/tasks.py`` — nobody else calls
``Celery(...)``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src" / "sandloom"
DRIVERS_DIR = SANDLOOM_ROOT / "core" / "drivers"

# Bare-name constructor calls, e.g. ``create_async_engine(...)`` / ``Celery(...)``.
FORBIDDEN_NAME_CALLS = {"create_async_engine", "Celery", "Storage"}
# Attribute constructor calls, e.g. ``redis.from_url(...)``.
FORBIDDEN_ATTR_CALLS = {("redis", "from_url")}

# Files allowed to construct a backend despite living outside drivers/.
# Keep this list tiny and justified — every entry is a hole in the invariant.
# Empty since the worker moved to sandloom.core.worker (registry-resolved app).
ALLOWLIST: set[str] = set()


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
            and isinstance(func.value, ast.Name)
            and (func.value.id, func.attr) in FORBIDDEN_ATTR_CALLS
        ):
            hits.append(f"Line {node.lineno}: {func.value.id}.{func.attr}(...)")
    return hits


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
    if rel in ALLOWLIST:
        return
    hits = _find_backend_constructions(file_path)
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
    )
    assert len(_find_backend_constructions(sample)) == 4
