"""Test seam for a module's OWN migration chain (upgrade / downgrade tests).

A migration test needs a database nobody else uses (the chain is walked up
and down, rows are inserted between revisions), the shared core chain in it,
and the module's chain moved to a given revision:

* :func:`temporary_database` — a throw-away PostgreSQL database next to the
  test database (``sandloom_test_scratch_*``), dropped afterwards; the
  ``scratch_database`` fixture of the ``sandloom.core.testing`` plugin wraps it;
* :func:`upgrade_core_chain` — apply the core (infra-only) chain to it;
* :func:`migrate_module` — upgrade / downgrade one module's chain
  (``<package>/migrations``, version table ``alembic_version_<key>``);
* :func:`run_sql` — run one statement, return its rows;
* :func:`migration_chains` / :func:`chain_coverage_problems` — every chain of
  the loaded modules (derived from their descriptors, never a hand-kept list)
  and the tables / indexes a model declares that its OWNING chain does not
  create (SANDLOOM-T002);
* :func:`migration_drift` / :func:`assert_migrations_match_models` — a
  chain upgraded to head on a scratch PostgreSQL database, compared with its
  models by Alembic's autogenerate (the gate that keeps the PostgreSQL schema,
  built by the chains, equal to the SQLite one, built from the models).

URLs are SYNC (psycopg2) DSNs, as Alembic and ``run_sql`` use them.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import re
import unittest
import uuid
from collections.abc import Callable, Container, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlunsplit

from alembic import command
from alembic.config import Config

from sandloom.core.composition import load_composition_core
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.migrations.migrations import (
    _order_by_dependencies,
    core_chain_dir,
    run_core_upgrade,
    version_table_for,
)
from sandloom.core.migrations.pg_sync import psycopg2
from sandloom.core.migrations.schema_compare import schema_differences
from sandloom.core.sql_alchemy.base import Base
from sandloom.core.sql_alchemy.portable import dialect_of_url, schema_from_models
from sandloom.core.url_split import split_url


def runs_no_chains(url: str) -> bool:
    """The test database ``url`` takes its schema from the models (SQLite):
    no migration chain runs on it (SANDLOOM-B028)."""
    return bool(url) and schema_from_models(dialect_of_url(url))


#: Why the migration-chain checks (coverage SANDLOOM-T002, migrations match
#: models SANDLOOM-T006) skip on a database that takes its schema from the
#: models, and what is still owed before production.
CHAINS_CHECKED_ON_POSTGRESQL = (
    "the test database takes its schema from the models and runs no migration "
    "chain (SANDLOOM-B028); the chains are checked on PostgreSQL: write each "
    "model change's revision there (sandloom migrate new) before production, "
    "which refuses this database (SANDLOOM-B029)"
)


def _sync(url: str, db: str) -> str:
    parts = split_url(url)
    return urlunsplit(parts._replace(scheme="postgresql", path=f"/{db}", query=""))


@contextmanager
def temporary_database(base_url: str | None = None) -> Iterator[str]:
    """Create an empty database on the test server; yield its sync URL.

    ``base_url`` (default: the test ``DATABASE_URL``) names the server; the
    database is dropped (``WITH (FORCE)``) when the block exits.
    """
    base = base_url or os.environ["DATABASE_URL"]
    admin = psycopg2.connect(_sync(base, "postgres"))
    admin.autocommit = True
    name = f"sandloom_test_scratch_{uuid.uuid4().hex[:12]}"
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
        try:
            yield _sync(base, name)
        finally:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.close()


def run_sql(url: str, sql: str, params: Any = None) -> list[Any]:
    """Run ``sql`` in its own committed transaction; the rows it returned."""
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
        conn.commit()
        return list(rows)
    finally:
        conn.close()


def upgrade_core_chain(url: str) -> None:
    """Apply the core chain (outbox / audit / inbox / command log) to ``url``."""
    run_core_upgrade(dry_run=False, database_url=url)


def _migrations_dir(package: str) -> Path:
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise ValueError(f"{package!r} is not an importable package")
    path = Path(next(iter(spec.submodule_search_locations))) / "migrations"
    if not path.is_dir():
        raise ValueError(f"{package!r} has no migrations/ directory")
    return path


def migrate_module(
    url: str,
    package: str,
    revision: str = "head",
    *,
    downgrade: bool = False,
    chain: str | None = None,
) -> None:
    """Move ``package``'s own chain in ``url`` to ``revision``.

    ``package`` is the module's import name (``"sandloom_billing"``); its chain
    lives in ``<package>/migrations`` with the version table
    ``alembic_version_<chain>`` (``chain`` defaults to the package's last
    component, like ``ModuleDescriptor.migrations_chain``). ``downgrade=True``
    walks DOWN to ``revision`` instead of up.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_migrations_dir(package)))
    cfg.set_main_option("sqlalchemy.url", url)
    key = chain or _module_name(package)
    cfg.set_main_option("version_table", f"alembic_version_{key}")
    if downgrade:
        command.downgrade(cfg, revision)
    else:
        command.upgrade(cfg, revision)


@dataclass(frozen=True)
class MigrationChain:
    """One Alembic chain: the kernel's core chain or a module's own."""

    key: str
    module: str | None
    script_location: Path
    version_table: str

    @property
    def versions(self) -> Path:
        return self.script_location / "versions"


def _composition_core(core: Any) -> Any:
    if core is not None:
        return core

    return load_composition_core()


def migration_chains(core: Any = None) -> list[MigrationChain]:
    """The core chain plus the chain of every loaded module that has one.

    Derived from the loaded descriptors (``migrations_path``,
    ``migration_chain_key``), so a new module's chain is covered without
    editing any list. ``core`` defaults to the composition root's.
    """
    core_chain = MigrationChain("core", None, core_chain_dir(), "alembic_version")
    return [
        core_chain,
        *(
            MigrationChain(
                d.migration_chain_key(),
                d.name,
                Path(d.migrations_path),
                version_table_for(d),
            )
            for d in _composition_core(core).module_descriptors
            if d.migrations_path
        ),
    ]


def tables_created_by(chain: MigrationChain) -> set[str]:
    """Tables a chain has after its revisions' ``upgrade()`` ran in order.

    Replayed statically (no database) from the revisions' syntax trees, in
    ``down_revision`` order: ``op.create_table`` adds a table,
    ``op.drop_table`` removes it, ``op.rename_table`` renames it; a
    ``downgrade()`` is not read. The name is a string literal, the
    ``table_name=`` keyword, or a constant assigned a string literal in the
    revision (``_TABLE = "x"``; ``op.create_table(_TABLE, ...)``). A table
    created and dropped again is not the chain's: the advice of SANDLOOM-T002
    ("add a revision that drops it") clears the problem.
    """
    trees = _revision_trees(chain)
    found: set[str] = set()
    for revision in _revision_order(trees):
        for action, name, new_name in _table_ops(trees[revision]):
            if action == "create":
                found.add(name)
            elif action == "drop":
                found.discard(name)
            elif name in found:  # rename
                found.discard(name)
                found.add(str(new_name))
    return found


def tables_ever_created_by(chain: MigrationChain) -> set[str]:
    """Every table any revision's ``upgrade()`` of ``chain`` creates, or renames
    to, whether a later revision drops or renames it again: the tables a fresh
    database gets from this chain at some point. Two chains may share none
    (SANDLOOM-T002), moved away or not: a chain that creates and drops a table
    still creates it, and the order the chains run in decides whether the
    other chain's create fails."""
    ever: set[str] = set()
    for tree in _revision_trees(chain).values():
        for action, name, new_name in _table_ops(tree, every_op=True):
            if action == "create":
                ever.add(name)
            elif action == "rename":
                ever.add(str(new_name))
    return ever


def _revision_trees(chain: MigrationChain) -> dict[Path, ast.Module]:
    return {
        revision: ast.parse(revision.read_text(encoding="utf-8"))
        for revision in sorted(chain.versions.glob("*.py"))
    }


def _string_constants(tree: ast.AST) -> dict[str, str]:
    """``NAME = "literal"`` (and annotated) assignments anywhere in ``tree``."""
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        value = getattr(node, "value", None)
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _revision_values(tree: ast.Module) -> dict[str, object]:
    """The literal ``revision`` and ``down_revision`` of a revision file."""
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        for name in names:
            if name in ("revision", "down_revision") and node.value is not None:
                with suppress(ValueError):
                    values[name] = ast.literal_eval(node.value)
    return values


def _revision_order(trees: dict[Path, ast.Module]) -> list[Path]:
    """The revision files parents first (``down_revision``); a file without a
    ``revision`` id has no parents and keeps its name order."""
    ids: dict[str, Path] = {}
    parents: dict[Path, list[str]] = {}
    for path, tree in trees.items():
        values = _revision_values(tree)
        revision = values.get("revision")
        if isinstance(revision, str):
            ids[revision] = path
        down = values.get("down_revision")
        if isinstance(down, str):
            parents[path] = [down]
        elif isinstance(down, tuple | list):  # a merge revision
            parents[path] = [d for d in down if isinstance(d, str)]
        else:
            parents[path] = []
    ordered: list[Path] = []
    seen: set[Path] = set()
    # Iterative post-order: a linear chain of thousands of revisions must not
    # exhaust the interpreter's recursion limit.
    for root in trees:
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(parents[root]))]
        while stack:
            path, pending = stack[-1]
            for parent in pending:
                if parent in ids and ids[parent] not in seen:
                    seen.add(ids[parent])
                    stack.append((ids[parent], iter(parents[ids[parent]])))
                    break
            else:
                stack.pop()
                ordered.append(path)
    return ordered


def _table_ops(
    tree: ast.Module, *, every_op: bool = False
) -> list[tuple[str, str, str | None]]:
    """``(action, table, new name)`` of the ``op.create_table`` /
    ``drop_table`` / ``rename_table`` calls of a revision, in source order,
    outside ``downgrade()``. A drop or rename counts only as an unconditional
    top-level statement of ``upgrade()`` (a ``drop_table`` under an ``if`` may
    not run), unless ``every_op``; a create counts wherever it is."""
    constants = _string_constants(tree)
    skipped = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
        for inner in ast.walk(node)
    }
    unconditional = {
        id(statement.value)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        for statement in node.body
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
    }

    def text(node: ast.expr | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        return None

    calls = sorted(
        (n for n in ast.walk(tree) if isinstance(n, ast.Call) and id(n) not in skipped),
        key=lambda n: (n.lineno, n.col_offset),
    )
    ops = (_table_op(call, text) for call in calls)
    return [
        op
        for call, op in zip(calls, ops, strict=True)
        if op is not None
        and (every_op or op[0] == "create" or id(call) in unconditional)
    ]


def _table_op(
    call: ast.Call, text: Callable[[ast.expr | None], str | None]
) -> tuple[str, str, str | None] | None:
    """``(action, table, new name)`` of one ``op.*_table`` call, else ``None``."""
    func = call.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else func.id
        if isinstance(func, ast.Name)
        else ""
    )
    keywords = {k.arg: k.value for k in call.keywords}
    first = call.args[0] if call.args else None
    second = call.args[1] if len(call.args) > 1 else None
    if name == "create_table":
        table = text(keywords.get("table_name") or keywords.get("name") or first)
        return ("create", table, None) if table else None
    if name == "drop_table":
        table = text(keywords.get("table_name") or first)
        return ("drop", table, None) if table else None
    if name == "rename_table":
        old = text(keywords.get("old_table_name") or first)
        new = text(keywords.get("new_table_name") or second)
        return ("rename", old, new) if old and new else None
    return None


def model_tables(core: Any = None) -> dict[str, MigrationChain]:
    """``{table: owning chain}`` for every ORM table of the core and the
    loaded modules — the owner is the package that DECLARES the model."""
    chains = migration_chains(core)
    by_module = {c.module: c for c in chains if c.module}
    # The kernel's own tables (outbox, audit, inbox, command log, sagas) are
    # always part of the core chain, whatever imported them so far.
    for kernel_models in (
        "sandloom.core.sql_alchemy.models",
        "sandloom.core.sql_alchemy.models.audit",
    ):
        importlib.import_module(kernel_models)
    for module in by_module:
        with suppress(ModuleNotFoundError):
            importlib.import_module(f"{module}.models")
    owners: dict[str, MigrationChain] = {}
    for mapper in Base.registry.mappers:
        owner_module = mapper.class_.__module__
        local = getattr(mapper, "local_table", None)
        table = getattr(local, "name", None)
        if not table:
            continue
        if owner_module.startswith("sandloom.core."):
            owners[table] = chains[0]
            continue
        for module, chain in by_module.items():
            if owner_module == module or owner_module.startswith(module + "."):
                owners[table] = chain
    return owners


def chain_coverage_problems(core: Any = None) -> list[str]:
    """Tables / indexes a model declares that its owning chain does not create.

    The harness builds tables from the models, production from the chains: a
    table or index on the model only passes the suite and is missing in
    production (SANDLOOM-T002). Each problem names the table, the model's owning
    module and that module's chain directory.
    """
    return [problem for _chain, problem in uncovered_by_chain(core)]


def uncovered_by_chain(
    core: Any = None, *, chains: Container[str] | None = None
) -> list[tuple[MigrationChain, str]]:
    """:func:`chain_coverage_problems` with the owning chain of each problem;
    ``chains``: only the chains of these keys (``"core"``: the kernel's). Read
    from the models and the revisions' source only: no database is reached,
    so ``sandloom migrate`` runs it before it applies anything."""
    problems: list[tuple[MigrationChain, str]] = []
    owners = {
        table: chain
        for table, chain in model_tables(core).items()
        if chains is None or chain.key in chains
    }
    sources: dict[str, str] = {}
    created: dict[str, set[str]] = {}
    for chain in {c.key: c for c in owners.values()}.values():
        sources[chain.key] = "\n".join(
            p.read_text(encoding="utf-8") for p in chain.versions.glob("*.py")
        )
        created[chain.key] = tables_created_by(chain)
    fix = 'add a revision: sandloom migrate new <module> "<message>"'
    for table, chain in sorted(owners.items(), key=lambda item: item[0]):
        where = (
            f"{chain.versions} (chain {chain.key!r}, "
            f"module {chain.module or 'sandloom.core'})"
        )
        if table not in created[chain.key]:
            problems.append(
                (
                    chain,
                    f"table {table} is not created by its owning chain {where} — "
                    f"{fix} (SANDLOOM-T002)",
                )
            )
            continue
        problems += [
            (
                chain,
                f"index {table}.{index.name} is not created by its owning chain "
                f"{where} — {fix}; a revision names each index literally, as "
                "autogenerate writes it (a name built at runtime, such as an "
                "f-string in a loop, is not recognised) (SANDLOOM-T002)",
            )
            for index in Base.metadata.tables[table].indexes
            if not re.search(rf"\b{re.escape(str(index.name))}\b", sources[chain.key])
        ]
    return problems


def _find_chain(chains: list[MigrationChain], module: str) -> MigrationChain:
    for chain in chains:
        if module in (chain.key, chain.module) or (
            chain.module is None and module in ("sandloom.core", "sandloom")
        ):
            return chain
    name = _module_name(module)
    for chain in chains:
        if name in (chain.key, chain.module):
            return chain
    known = ", ".join(sorted(c.module or c.key for c in chains))
    raise ValueError(
        f"no migration chain for {module!r} among the loaded modules ({known}); "
        "name a loaded module that has a migrations/ directory, or 'core'"
    )


def _chains_to_upgrade(chain: MigrationChain, core: Any) -> list[MigrationChain]:
    """``chain`` after the module chains it depends on, in ``upgrade-all``
    order (the core chain is applied separately, always first)."""
    if chain.module is None:
        return []
    descriptors = {
        d.name: d
        for d in _composition_core(core).module_descriptors
        if d.migrations_path
    }
    needed: set[str] = set()
    pending = [chain.module]
    while pending:
        name = pending.pop()
        if name in needed or name not in descriptors:
            continue
        needed.add(name)
        pending += list(getattr(descriptors[name], "depends_on", []) or [])
    by_module = {c.module: c for c in migration_chains(core) if c.module}
    return [by_module[n] for n in _order_by_dependencies(descriptors, needed)]


def _upgrade(url: str, chain: MigrationChain) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(chain.script_location))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.set_main_option("version_table", chain.version_table)
    command.upgrade(cfg, "head")


def _owned_tables(chain: MigrationChain, core: Any) -> frozenset[str]:
    """The tables ``chain`` answers for: those its models declare and those
    its revisions' ``upgrade()`` leaves (:func:`tables_created_by`; a table
    dropped from the models but not from the chain is reported, not ignored; a
    table a ``downgrade()`` re-creates, after it moved to another module, is
    not this chain's)."""
    declared = {t for t, c in model_tables(core).items() if c.key == chain.key}
    return frozenset(declared | tables_created_by(chain))


_NEEDS_POSTGRESQL = (
    "migrations match models needs PostgreSQL: no DATABASE_URL is set, or "
    + CHAINS_CHECKED_ON_POSTGRESQL
)


def _skip(reason: str) -> None:
    """Skip the running test: ``pytest.skip`` when pytest is installed (the
    skip is reported at the caller), else unittest's, so this module stays
    importable without pytest (the CLI's lean install)."""
    try:
        pytest = importlib.import_module("pytest")
    except ImportError:
        raise unittest.SkipTest(reason) from None
    pytest.skip(reason)


def _has_no_chain(module: str, core: Any) -> bool:
    """``module`` is loaded and has no ``migrations/`` (no chain to compare)."""
    names = {module, _module_name(module)}
    for descriptor in _composition_core(core).module_descriptors:
        if descriptor.name in names:
            return not descriptor.migrations_path
    return False


def migration_drift(module: str, *, core: Any = None) -> list[str]:
    """How a module's migration chain at head differs from its models.

    On a scratch database next to the test database: the core chain, the
    chains ``module`` depends on, then its own chain, each to head; then
    Alembic's autogenerate comparison (``compare_metadata``) of the result
    with the models, over the tables the chain owns, with the options and the
    filter ``sandloom migrate new`` uses (:data:`COMPARE_OPTIONS`: types and
    server defaults; a model index the database never creates is skipped),
    plus each index as the database reflects it (access method, operator
    classes, sort order, included columns, ``NULLS NOT DISTINCT``, a partial
    index's condition) against the same index created from the models in a
    second scratch database, which Alembic does not compare. Not compared:
    what the models cannot declare (RLS policies, extensions, triggers), CHECK
    constraints (Alembic does not reflect them) and an index's collation
    (SQLAlchemy does not reflect it). Each difference is one line naming the
    table and the object; an empty list means they match.

    ``module`` is a loaded module's name, its package (``"sandloom_billing"``)
    or ``"core"`` for the kernel's chain. PostgreSQL only: a test database that
    takes its schema from the models (SQLite) has no chain to compare.
    """
    database_url = os.getenv("DATABASE_URL") or ""
    if not database_url or runs_no_chains(database_url):
        raise RuntimeError(_NEEDS_POSTGRESQL)
    chain = _find_chain(migration_chains(core), module)
    owned = _owned_tables(chain, core)
    with temporary_database() as url, temporary_database() as models_url:
        upgrade_core_chain(url)
        for each in _chains_to_upgrade(chain, core):
            _upgrade(url, each)
        return schema_differences(url, owned, models_url)


def assert_migrations_match_models(module: str, *, core: Any = None) -> None:
    """Fail with every difference between ``module``'s chain and its models.

    Skips (with the reason) on a test database that takes its schema from the
    models: SQLite never runs the chains (SANDLOOM-B028), and this gate is what
    keeps the PostgreSQL schema the chains build equal to the models. Skips
    too for a loaded module with no ``migrations/`` directory; a name no
    loaded module has raises ``ValueError``.
    """
    url = os.getenv("DATABASE_URL") or ""
    if not url or runs_no_chains(url):
        _skip(_NEEDS_POSTGRESQL)
    if _has_no_chain(module, core):
        _skip(f"{module} has no migrations/ directory: no chain to compare")
    drift = migration_drift(module, core=core)
    if drift:
        chain = _find_chain(migration_chains(core), module)
        lines = "\n  ".join(drift)
        fix = (
            f"the core chain is the kernel's: add a revision under {chain.versions}"
            if chain.module is None
            else f'sandloom migrate new {chain.module} "<message>"'
        )
        raise AssertionError(
            f"the {chain.module or 'core'} migration chain at head differs from "
            f"its models (SANDLOOM-T006):\n  {lines}\nfix: {fix} (or correct "
            "the model), then review the revision"
        )


__all__ = [
    "MigrationChain",
    "assert_migrations_match_models",
    "chain_coverage_problems",
    "migrate_module",
    "migration_chains",
    "migration_drift",
    "model_tables",
    "run_sql",
    "tables_created_by",
    "temporary_database",
    "uncovered_by_chain",
    "upgrade_core_chain",
]
