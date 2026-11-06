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
from collections.abc import Container, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg2  # type: ignore[import-untyped]
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
from sandloom.core.migrations.schema_compare import schema_differences
from sandloom.core.sql_alchemy.base import Base
from sandloom.core.sql_alchemy.portable import dialect_of_url, schema_from_models


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
    parts = urlsplit(url)
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
    """Tables a chain's revisions create (``op.create_table(<name>, ...)``).

    Read from the revisions' syntax tree: the name is a string literal, the
    ``table_name=`` keyword, or a constant assigned a string literal in the
    revision (``_TABLE = "x"``; ``op.create_table(_TABLE, ...)``).
    """
    found: set[str] = set()
    for revision in chain.versions.glob("*.py"):
        found |= _created_tables(revision.read_text(encoding="utf-8"))
    return found


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


def _created_tables(source: str, *, upgrade_only: bool = False) -> set[str]:
    tree = ast.parse(source)
    constants = _string_constants(tree)
    found: set[str] = set()
    roots: list[ast.AST] = [tree]
    if upgrade_only:  # not the tables a downgrade() re-creates
        roots = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        ]
    for node in (n for root in roots for n in ast.walk(root)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "create_table":
            continue
        arg: ast.expr | None = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg in ("table_name", "name"):
                arg = keyword.value
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.add(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in constants:
            found.add(constants[arg.id])
    return found


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
    its revisions' ``upgrade()`` creates (a table dropped from the models but
    not from the chain is reported, not ignored; a table a ``downgrade()``
    re-creates, after it moved to another module, is not this chain's)."""
    declared = {t for t, c in model_tables(core).items() if c.key == chain.key}
    upgraded: set[str] = set()
    for revision in chain.versions.glob("*.py"):
        source = revision.read_text(encoding="utf-8")
        upgraded |= _created_tables(source, upgrade_only=True)
    return frozenset(declared | upgraded)


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
