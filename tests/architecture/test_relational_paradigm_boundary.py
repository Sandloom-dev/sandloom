"""Architecture rule: SQLAlchemy lives ONLY in the relational-paradigm surface.

Storage is split by paradigm. ``database`` is the relational paradigm contract
(:class:`sandloom.core.infra.contracts.DatabaseProvider`, alias ``RelationalProvider``);
``document_store`` / ``graph_store`` / … are sibling paradigms with their own
contracts. The promise that the *paradigm-agnostic kernel* (the capability
registry, eventing core, observability, http, lifecycle, the DI/facade plumbing)
depends only on abstract contracts is only real if it is enforced: no kernel
module may import SQLAlchemy at runtime. The concrete relational backend
(SQLAlchemy) is confined to the relational paradigm surface below.

This is a ratchet, like ``ORM_LEAKAGE_BASELINE``: the allowlist is the relational
paradigm's footprint. Entries may be REMOVED (as coupling is extracted behind the
contract) but never ADDED — a new kernel file that reaches for SQLAlchemy fails
here and must go through ``DbSession`` / the ``DatabaseProvider`` contract instead.

Typing-only imports (inside ``if TYPE_CHECKING:``) are NOT runtime coupling and
are ignored — naming ``AsyncSession`` in an annotation is fine; constructing or
calling SQLAlchemy is not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src" / "sandloom"
CORE_ROOT = SANDLOOM_ROOT / "core"

# The relational paradigm surface — the ONLY core code allowed to import
# SQLAlchemy at runtime. A trailing "/" marks a whole subtree. Every entry is
# the relational paradigm doing its job; everything else is paradigm-agnostic.
RELATIONAL_PARADIGM_ALLOWLIST = {
    "core/tenant.py",  # TenantScoped column + the do_orm_execute scoping hook
    "core/di.py",  # DbSession / Database(name): the relational session surface
    "core/_internal/",  # raw get_db session adapter + relational-session predicates
    "core/sql_alchemy/",  # the relational UoW / outbox / audit / inbox / model impl
    # savepoint tracking of on_commit callbacks: a lazy sqlalchemy.event listener
    # installed only for a relational session (``session.sync_session``)
    "core/unit_of_work/savepoints.py",
    "core/projections/upsert.py",  # monotonic SQL upsert for projections
    "core/migrations/",  # Alembic-based relational migration tooling
    "core/cli/",  # scaffolders that emit relational module skeletons
    # the relational half of the test harness (engine / session / schema reset)
    "core/testing/database.py",
    "core/drivers/",  # drivers ARE the backend construction site
}


def _is_allowlisted(rel_path: str) -> bool:
    for entry in RELATIONAL_PARADIGM_ALLOWLIST:
        if entry.endswith("/"):
            if rel_path.startswith(entry):
                return True
        elif rel_path == entry:
            return True
    return False


def _is_type_checking_test(test: ast.expr) -> bool:
    """Match ``if TYPE_CHECKING:`` (bare name or ``typing.TYPE_CHECKING``)."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _typing_only_import_ids(tree: ast.AST) -> set[int]:
    """ids() of import nodes guarded by ``if TYPE_CHECKING:`` (not runtime)."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            for child in ast.walk(node):
                if isinstance(child, ast.Import | ast.ImportFrom):
                    guarded.add(id(child))
    return guarded


def find_runtime_sqlalchemy_imports(file_path: Path) -> list[str]:
    """Return descriptions of runtime ``sqlalchemy`` imports in a file."""
    tree = ast.parse(file_path.read_text())
    guarded = _typing_only_import_ids(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if (
                node.level == 0
                and node.module
                and node.module.split(".")[0] == "sqlalchemy"
            ):
                names = ", ".join(a.name for a in node.names)
                violations.append(
                    f"Line {node.lineno}: from {node.module} import {names}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "sqlalchemy":
                    violations.append(f"Line {node.lineno}: import {alias.name}")
    return violations


def _iter_core_files() -> list[Path]:
    return [
        py
        for py in CORE_ROOT.glob("**/*.py")
        if "tests" not in py.parts and "__pycache__" not in py.parts
    ]


@pytest.mark.parametrize(
    "file_path", _iter_core_files(), ids=lambda p: str(p.relative_to(SANDLOOM_ROOT))
)
def test_kernel_has_no_runtime_sqlalchemy_import(file_path: Path) -> None:
    rel = str(file_path.relative_to(SANDLOOM_ROOT))
    if _is_allowlisted(rel):
        return
    hits = find_runtime_sqlalchemy_imports(file_path)
    if hits:
        pytest.fail(
            f"Paradigm-agnostic kernel file '{rel}' imports SQLAlchemy at "
            "runtime:\n" + "\n".join(hits) + "\n\n"
            "The kernel must depend on abstract contracts only. Reach the "
            "relational backend through DbSession / Database(name) / the "
            "DatabaseProvider contract. If a SQLAlchemy name is needed only for "
            "typing, move the import under `if TYPE_CHECKING:`. SQLAlchemy at "
            "runtime belongs solely to the relational paradigm surface "
            "(see RELATIONAL_PARADIGM_ALLOWLIST).",
            pytrace=False,
        )


def test_allowlist_entries_are_relational() -> None:
    """Keep the ratchet honest: every allowlisted file/subtree exists and an
    allowlisted *file* really does import SQLAlchemy at runtime (else remove it).
    """
    for entry in RELATIONAL_PARADIGM_ALLOWLIST:
        path = SANDLOOM_ROOT / entry
        if entry.endswith("/"):
            assert path.is_dir(), f"stale allowlist subtree: {entry}"
        else:
            assert path.is_file(), f"stale allowlist file: {entry}"
            assert find_runtime_sqlalchemy_imports(path), (
                f"allowlist entry no longer imports SQLAlchemy — remove it: {entry}"
            )


def test_analyzer_ignores_typing_only_and_flags_runtime(tmp_path: Path) -> None:
    """Self-test: TYPE_CHECKING imports pass; runtime imports are flagged."""
    typing_only = tmp_path / "typing_only.py"
    typing_only.write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from sqlalchemy.ext.asyncio import AsyncSession\n"
    )
    assert find_runtime_sqlalchemy_imports(typing_only) == []

    leaky = tmp_path / "leaky.py"
    leaky.write_text(
        "from sqlalchemy import select\n"
        "import sqlalchemy as sa\n"
        "def f():\n"
        "    from sqlalchemy.orm import Session\n"
        "    return Session, sa, select\n"
    )
    assert len(find_runtime_sqlalchemy_imports(leaky)) == 3


#: Paradigm-agnostic kernel subtrees that must not reach the relational
#: IMPLEMENTATION (``sandloom.core.sql_alchemy``) either — not even lazily. They
#: go through the paradigm seams (``sandloom.core.unit_of_work`` /
#: the ``DatabaseProvider`` contract) instead.
NO_RELATIONAL_IMPL_SUBTREES = ("core/eventing/",)


def _relational_impl_imports(file_path: Path) -> list[str]:
    tree = ast.parse(file_path.read_text())
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.startswith("sandloom.core.sql_alchemy"):
                hits.append(f"Line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("sandloom.core.sql_alchemy"):
                    hits.append(f"Line {node.lineno}: import {alias.name}")
    return hits


@pytest.mark.parametrize(
    "file_path",
    [
        f
        for f in _iter_core_files()
        if str(f.relative_to(SANDLOOM_ROOT)).startswith(NO_RELATIONAL_IMPL_SUBTREES)
    ],
    ids=lambda p: str(p.relative_to(SANDLOOM_ROOT)),
)
def test_eventing_never_imports_the_relational_implementation(
    file_path: Path,
) -> None:
    hits = _relational_impl_imports(file_path)
    assert not hits, (
        f"{file_path.relative_to(SANDLOOM_ROOT)} imports sandloom.core.sql_alchemy:\n"
        + "\n".join(hits)
        + "\nUse the paradigm seam (sandloom.core.unit_of_work.durable_inbox, the "
        "DatabaseProvider contract) instead."
    )
