"""``sandloom add`` — generators that write code against the public API.

    sandloom add module <m> [--entity NAME | --empty] [--settings]
    a module (one example entity: model, route, event, test; or none)
    sandloom add event <m> <Name> [--field n:type ...]   append an event to
    <m>/events.py
    sandloom add contract <m> <name>              a provider in <m>/contracts.py and
    its DTO in <m>/schemas.py
    sandloom add contract-use <m> <contract>      declare that <m> consumes a contract
    and write its stub before the code imports it
    sandloom add consumer <m> <channel> [--field n:type ...]   a view and an @on handler
    sandloom add saga <m> <name>                  a saga with a typed command step
    sandloom add projection <m> <name>            a projection, its row model and
    migration
    sandloom add skills [--force]                 the app's agent skills (also from
    sandloom new)

Rules every generator keeps:

- it writes new files inside the module package only (``deploy`` writes the
  deploy profile); every generator but ``skills`` and ``deploy`` then runs
  ``sandloom generate``, which writes the generated files (snapshots, contract
  stubs and DTO copies in the shared DTO package, exported event schemas),
  so they are current after scaffolding;
- it never rewrites a hand-written file: ``sandloom add event`` (and a second
  ``contract`` / ``consumer``) only APPEND to their file, refusing a file
  that does not parse; any other existing target is written as ``*.new``;
- the code uses only the beginner tier of ``sandloom.sdk`` (SANDLOOM-T009), and
  tier 2 in the file named for it (``sagas.py``, ``projections.py``,
  ``tasks.py``); any other advanced name carries ``# sandloom: advanced <reason>``;
- ``module`` and ``event`` scaffold tenant-scoped (a ``TenantScoped`` table,
  ``tenant_id`` on the event); ``--no-tenant`` drops both, for an app without
  tenants. The choice is the developer's, never read from the environment or
  ``.env`` (a gitignored per-machine file would scaffold a table without
  row-level security into an app that is multi-tenant in production);
  ``add projection`` always keys its rows by tenant.

Every generator is run into a fresh app by the framework's tests and the
result must pass ``sandloom check`` and the app's own tests.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import keyword
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sandloom.core.cli.scaffold_deploy as _deploy
import sandloom.core.cli.skills as _skills
import sandloom.core.cli.stack_files as _stack_files
import sandloom.core.cli.stack_service as _stack
import sandloom.core.cli.tool_process as _tool_process
from sandloom.core.archcheck import api10_rules
from sandloom.core.archcheck.manifest import (
    MANIFEST_REFUSAL_CODES,
    ManifestError,
    load_rules,
)
from sandloom.core.cli.migrate_checks import RLS_IMPORT, next_revision
from sandloom.core.cli.scaffold_wiring import (
    FRAMEWORK_TREE_CONFTEST,
    contract_inherited,
    module_contract,
    project_root,
    wire_module,
)
from sandloom.core.descriptors.derived_capabilities import defines_periodic
from sandloom.core.tasks.cron import parse_cron

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
#: ruff format's line length (generated files pass the project's format gate).
_LINE = 88
_DEFS = ("def ", "async def ")
_CLASS = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_CHANNEL = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+\.v[1-9][0-9]*$")
#: ``--field name:type`` types and the import each needs.
FIELD_TYPES: dict[str, tuple[str, str | None]] = {
    "str": ("str", None),
    "int": ("int", None),
    "float": ("float", None),
    "bool": ("bool", None),
    "UUID": ("UUID", "from uuid import UUID"),
    "datetime": ("datetime", "from datetime import datetime"),
    "date": ("date", "from datetime import date"),
    "Decimal": ("Decimal", "from decimal import Decimal"),
}


class GeneratorError(ValueError):
    """A generator refused (bad name, unparsable target, missing module)."""


@dataclass(frozen=True)
class Layout:
    """Where an application keeps its modules and its shared DTO package."""

    modules_dir: Path
    modules_package: str
    #: The application's generated DTO package; ``None`` in a tree of top-level
    #: module packages, where each module keeps its own ``_generated/`` home.
    shared_dir: Path | None
    shared_package: str | None
    source_root: Path
    #: Whether a generated table is ``TenantScoped`` and a generated event
    #: carries ``tenant_id``. The default; ``--no-tenant`` (an app without
    #: tenants) turns it off. Never read from the app's settings.
    tenant_scoped: bool = True

    def module_dir(self, module: str) -> Path:
        return self.modules_dir / module

    def dotted(self, module: str) -> str:
        return f"{self.modules_package}.{module}" if self.modules_package else module

    def generated_home(self, module: str) -> tuple[Path, str]:
        """``(directory, import package)`` of the generated contract stubs and
        DTO copies a module's code imports: the application's
        ``<app>.shared/contracts``, else ``<module>/_generated/contracts``
        (ADR-0013 decision 1)."""
        if self.shared_dir is not None and self.shared_package is not None:
            return self.shared_dir / "contracts", f"{self.shared_package}.contracts"
        return (
            self.module_dir(module) / "_generated" / "contracts",
            f"{self.dotted(module)}._generated.contracts",
        )

    @property
    def framework_tree(self) -> bool:
        """The framework checkout (top-level modules next to ``sandloom``)."""
        return not self.modules_package and (self.source_root / "sandloom").is_dir()


def layout(root: Path, *, tenant_scoped: bool = True) -> Layout:
    """The layout of the project at ``root`` (its ``architecture_rules.yml``);
    ``tenant_scoped=False`` is ``--no-tenant``."""
    try:
        rules = load_rules(root / "architecture_rules.yml")
    except ManifestError as exc:
        if any(code in str(exc) for code in MANIFEST_REFUSAL_CODES):
            # The manifest was found and read: the project root is right, the
            # message already says which entries to delete.
            raise GeneratorError(str(exc)) from exc
        raise GeneratorError(
            f"{exc}; run `sandloom add` from the project root (where "
            "architecture_rules.yml is)"
        ) from exc
    return Layout(
        modules_dir=rules.modules_path,
        modules_package=rules.modules_package,
        shared_dir=rules.shared_dir,
        shared_package=rules.shared_dto_package,
        source_root=rules.source_root,
        tenant_scoped=tenant_scoped,
    )


def _check_name(name: str, what: str) -> None:
    if not _NAME.fullmatch(name) or keyword.iskeyword(name):
        raise GeneratorError(
            f"{what} must be snake_case starting with a letter: {name!r}"
        )


def camel(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))


def _write(path: Path, content: str) -> Path:
    """Write a new file; an existing target is never touched (``*.new``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path if not path.exists() else path.with_name(path.name + ".new")
    target.write_text(content, encoding="utf-8")
    return target


def _append(path: Path, block: str, *, header: str) -> Path:
    """Append ``block`` to ``path`` (created with ``header`` when absent)."""
    if not path.exists():
        return _write(path, header + block)
    source = path.read_text(encoding="utf-8")
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise GeneratorError(f"{path} does not parse ({exc}); fix it first") from exc
    path.write_text(source.rstrip("\n") + "\n\n\n" + block, encoding="utf-8")
    return path


def _imports_present(path: Path, needed: list[str]) -> list[str]:
    """The import lines of ``needed`` that ``path`` does not have (refuses a
    file that does not parse first)."""
    if not path.exists():
        return []
    source = path.read_text(encoding="utf-8")
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise GeneratorError(f"{path} does not parse ({exc}); fix it first") from exc
    return [line for line in needed if line not in source]


# --------------------------------------------------------------------------- #
# sandloom add module
# --------------------------------------------------------------------------- #


def _call(head: str, args: str, suffix: str = "") -> str:
    """``head(args)suffix`` laid out the way ruff format does (one line when it
    fits, else the arguments on their own line), whatever the module name.
    ``suffix`` is what follows the closing parenthesis (``:``, `` -> None:``,
    ``.publish()``)."""
    one = f"{head}({args}){suffix}"
    if len(one) <= _LINE:
        return one
    indent = head[: len(head) - len(head.lstrip())]
    if len(f"{indent}    {args}") <= _LINE:
        # ruff format adds the magic trailing comma to a def with no comma in it.
        comma = "," if "," not in args and head.lstrip().startswith(_DEFS) else ""
        return f"{head}(\n{indent}    {args}{comma}\n{indent}){suffix}"
    # Still too long: one argument per line, with the magic trailing comma.
    parts: list[str] = []
    depth, quote, start = 0, "", 0
    for i, ch in enumerate(args):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(args[start:i].strip())
            start = i + 1
    parts.append(args[start:].strip())
    lines = "".join(f"{indent}    {part},\n" for part in parts)
    return f"{head}(\n{lines}{indent}){suffix}"


def _plural(word: str) -> str:
    """``word`` in the plural: ``item`` -> ``items``, ``category`` ->
    ``categories``, ``box`` -> ``boxes``."""
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return f"{word}es"
    if word.endswith("y") and word[-2:-1] not in ("a", "e", "i", "o", "u"):
        return f"{word[:-1]}ies"
    return f"{word}s"


def _import_line(source: str, names: str) -> str:
    """``from source import names`` laid out the way ruff format does."""
    one = f"from {source} import {names}"
    if len(one) <= _LINE:
        return one
    lines = "".join(f"    {n.strip()},\n" for n in names.split(","))
    return f"from {source} import (\n{lines})"


def _assign(head: str, expression: str) -> str:
    """``head = expression``, in parentheses on its own line when it is too long."""
    one = f"{head} = {expression}"
    if len(one) <= _LINE:
        return one
    indent = head[: len(head) - len(head.lstrip())]
    return f"{head} = (\n{indent}    {expression}\n{indent})"


def _select_query(model: str) -> str:
    """The list query of the example service, laid out the way ruff format does:
    one line, then in parentheses, then one call of the chain per line."""
    chain = f"sa.select({model}).order_by(newest, tie).limit(limit)"
    if len(f"    query = {chain}") <= _LINE:
        return f"    query = {chain}"
    if len(f"        {chain}") <= _LINE:
        return f"    query = (\n        {chain}\n    )"
    return (
        f"    query = (\n        sa.select({model})\n"
        "        .order_by(newest, tie)\n        .limit(limit)\n    )"
    )


def _settings_py(name: str) -> str:
    """The module's ``settings.py`` (``add module --settings``)."""
    page_size = _call(
        "    default_page_size: int = Field",
        f'default=20, ge=1, le=100, alias="{name.upper()}_DEFAULT_PAGE_SIZE"',
    )
    return f'''"""Settings for the {name} module.

Keep environment aliases prefixed with ``{name.upper()}_``.
Read validated settings at runtime with ``settings_of({camel(name)}Settings)``
from ``sandloom.sdk.module``.
"""

from pydantic import BaseModel, Field


class {camel(name)}Settings(BaseModel):
{page_size}
'''


def _chain_files(name: str, *, with_settings: bool) -> dict[str, str]:
    """What every module has, with or without an example entity: the package,
    its tables package, its own migration chain and the test that keeps the
    chain and the models equal."""
    cls = camel(name)
    files: dict[str, str] = {}
    settings_import = ""
    settings_argument = ""
    if with_settings:
        files["settings.py"] = _settings_py(name)
        settings_import = f"from .settings import {cls}Settings\n\n"
        settings_argument = f", settings={cls}Settings"
    register = _call(
        "register = define_module", f'__name__, version="0.1.0"{settings_argument}'
    )
    files["__init__.py"] = f'''"""The {name} module.

Everything it declares is discovered: routes in ``api.py``, events in
``events.py``, tables in ``models/``. It talks to other modules only through
events and contracts.
"""

from sandloom.sdk import define_module

{settings_import}{register}
'''
    files["models/__init__.py"] = f'''"""The {name} tables.

Every module of this package is imported (the app and the migration chain walk
it), so a new ``models/<x>.py`` joins the module's tables and its migration
chain.

Table names start with ``{name}_``.
Rows of other modules are referenced by UUID, never by a foreign key.
"""
'''
    files[
        "migrations/env.py"
    ] = '''"""The module's own migration chain (tables of ``models/``)."""

from sandloom.sdk.migrations import run_module_migrations  # sandloom: advanced chain

run_module_migrations(__file__)
'''
    files["migrations/script.py.mako"] = _MAKO
    files["tests/__init__.py"] = ""
    files["tests/test_migrations.py"] = f'''\
"""The chain at head builds the schema the models declare.

A test database on SQLite takes its schema from the models and runs no chain;
on PostgreSQL this compares the two (it skips on SQLite). A model change
without its revision fails here: ``sandloom migrate new {name} "<message>"``.
"""

from sandloom.sdk.testing import (  # sandloom: advanced migrations gate
    assert_migrations_match_models,
)


def test_migrations_match_models() -> None:
    assert_migrations_match_models("{name}")
'''
    return files


def _empty_files(name: str, *, with_settings: bool) -> dict[str, str]:
    """``--empty``: the package and its chain, no example table, route or event.
    ``migrations/versions/`` is empty until ``sandloom migrate new``."""
    files = _chain_files(name, with_settings=with_settings)
    files["migrations/versions/.gitkeep"] = ""
    return files


def _module_files(
    name: str,
    *,
    with_settings: bool,
    tenant_scoped: bool = True,
    entity: str = "item",
) -> dict[str, str]:
    """The files of a new module. ``tenant_scoped`` (the default): the example
    table is ``TenantScoped``, its event carries the tenant and its tests prove
    the isolation; ``--no-tenant`` leaves all of that out. ``entity`` names the
    example (``item``: ``<Module>Item``, ``<module>_items``, ``/items``,
    ``<module>.item.created.v1``)."""
    cls = camel(name)
    ent = entity
    ents = _plural(ent)
    ent_cls = camel(ent)
    model = f"{cls}{ent_cls}"
    created_event = f"{ent_cls}Created"
    schema_in, schema_out = f"{ent_cls}In", f"{ent_cls}Out"
    labels = ents.replace("_", " ")
    label = ent.replace("_", " ")
    article = "an" if ent[0] in "aeiou" else "a"
    files = _chain_files(name, with_settings=with_settings)
    scoped_import = "TenantScoped, " if tenant_scoped else ""
    item_bases = "TenantScoped, Base" if tenant_scoped else "Base"
    item_doc = (
        "``TenantScoped``: rows belong to the bound tenant and are filtered by it."
        if tenant_scoped
        else "A plain table (``--no-tenant``): for an app without tenants."
    )
    event_tenant = "    tenant_id: UUID\n" if tenant_scoped else ""
    publish_tenant = ", tenant_id=row.tenant_id" if tenant_scoped else ""
    list_doc = (
        f"The newest {labels} first: ``created_at`` (the database clock), then the\n"
        "    UUIDv7 ``id`` (creation order) for rows stamped in the same microsecond."
        + (
            "\n    ``TenantScoped`` keeps them to the bound tenant."
            if tenant_scoped
            else ""
        )
    )
    router_doc = " and binds the tenant" if tenant_scoped else ""
    owner_of = f"the tenant's {labels}" if tenant_scoped else f"the {labels}"
    # The tests take the tenant the app's mode expects at run time (the
    # scaffold never reads TENANCY_MODE): a fresh one under ``multi``, the
    # canonical SINGLE_TENANT_ID under ``single``, where any other tenant is a
    # 403 ``tenant_mismatch``.
    test_user = (
        "as_user(tenant_id=_app_tenant(sandloom_core))"
        if tenant_scoped
        else "as_user(tenant=False)"
    )
    fixtures = "client: Any, sandloom_core: Any" if tenant_scoped else "client: Any"
    uuid_import = "from uuid import UUID, uuid4\n" if tenant_scoped else ""
    tenant_helper = (
        '''

def _app_tenant(core: Any) -> UUID:
    """The tenant this app's mode expects: SINGLE_TENANT_ID when
    TENANCY_MODE=single, else a fresh tenant."""
    settings = core.settings
    if settings.tenancy_mode == "single":
        return UUID(str(settings.single_tenant_id))
    return uuid4()
'''
        if tenant_scoped
        else ""
    )
    settings_import = ""
    settings_reader = ""
    limit_parameter = "limit: Annotated[int, Query(ge=1, le=100)] = 20"
    resolve_limit = ""
    if with_settings:
        settings_import = f"from .settings import {cls}Settings\n"
        settings_reader = (
            "from sandloom.sdk.module import settings_of"
            "  # sandloom: advanced module settings\n"
        )
        limit_parameter = "limit: Annotated[int | None, Query(ge=1, le=100)] = None"
        resolve_limit = (
            "    if limit is None:\n"
            f"        limit = settings_of({cls}Settings).default_page_size\n"
        )
    model_class = _call(f"class {model}", item_bases, ":")
    files[f"models/{ent}.py"] = f'''"""An example table: replace it with your domain."""

import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, {scoped_import}new_id


{model_class}
    """{item_doc}"""

    __tablename__ = "{name}_{ents}"

    # new_id: a time-ordered UUIDv7; CreatedAt: clock_timestamp(), not now()
    # (the transaction's start), so rows of one transaction never tie.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    owner_id: Mapped[uuid.UUID] = mapped_column(index=True)
    created_at: Mapped[CreatedAt]
'''
    files["schemas.py"] = f'''"""Request and response shapes (pydantic only)."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class {schema_in}(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class {schema_out}(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    owner_id: UUID
'''
    channel = f"{name}.{ent}.created.v1"
    event_class = _call(f"class {created_event}", f'Event, channel="{channel}"', ":")
    files["events.py"] = f'''"""Events of the {name} module (its own channels)."""

from uuid import UUID

from sandloom.sdk import Event


{event_class}
    {ent}_id: UUID
{event_tenant}'''
    create_def = _call(
        f"async def create_{ent}",
        f"body: {schema_in}, *, owner_id: UUID",
        f" -> {schema_out}:",
    )
    get_def = _call(
        f"async def get_{ent}", f"{ent}_id: UUID", f" -> {schema_out} | None:"
    )
    list_def = _call(
        f"async def list_{ents}", "*, limit: int", f" -> list[{schema_out}]:"
    )
    new_row = _call(f"    row = {model}", "title=body.title, owner_id=owner_id")
    publish = _call(
        f"    await {created_event}", f"{ent}_id=row.id{publish_tenant}", ".publish()"
    )
    fetch_row = _call("    row = await db().get", f"{model}, {ent}_id")
    query = _select_query(model)
    import_events = _import_line(".events", created_event)
    import_model = _import_line(f".models.{ent}", model)
    import_schemas = _import_line(".schemas", f"{schema_in}, {schema_out}")
    files[
        "services.py"
    ] = f'''"""The {name} use cases. ``db()`` is the session of the running unit."""

from uuid import UUID

import sqlalchemy as sa

from sandloom.sdk import db

{import_events}
{import_model}
{import_schemas}


{create_def}
{new_row}
    db().add(row)
    await db().flush()
{publish}
    return {schema_out}.model_validate(row)


{get_def}
{fetch_row}
    return None if row is None else {schema_out}.model_validate(row)


{list_def}
    """{list_doc}"""
    newest = {model}.created_at.desc()
    tie = {model}.id.desc()
{query}
    rows = await db().scalars(query)
    return [{schema_out}.model_validate(row) for row in rows]
'''
    api_create = _call(
        f"async def create_{ent}",
        f"body: {schema_in}, user: CurrentUser",
        f" -> {schema_out}:",
    )
    api_get = _call(
        f"async def get_{ent}",
        f"{ent}_id: UUID, user: CurrentUser",
        f" -> {schema_out}:",
    )
    api_created = _call(
        "    return await services.create_" + ent, "body, owner_id=user.user_id"
    )
    api_found = _call(f"    found = await services.get_{ent}", f"{ent}_id")
    files["api.py"] = f'''"""Routes of the {name} module: ``/api/v1/{name}/...``.

``module_router()`` authenticates every route{router_doc}.
"""

from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, Query

from sandloom.sdk import CurrentUser, module_router
{settings_reader}
from . import services
{import_schemas}
{settings_import}
router = module_router()


@router.post("/{ents}", status_code=201)
{api_create}
{api_created}


@router.get("/{ents}")
async def list_{ents}(
    user: CurrentUser, {limit_parameter}
) -> list[{schema_out}]:
    """A page of {owner_of}.

    ``le=`` caps what one request may load.
    """
{resolve_limit}    return await services.list_{ents}(limit=limit)


@router.get("/{ents}/{{{ent}_id}}")
{api_get}
{api_found}
    if found is None:
        raise HTTPException(status_code=404, detail="{label} not found")
    return found
'''
    files[f"migrations/versions/0001_{name}_baseline.py"] = _baseline(
        name, ents, tenant_scoped=tenant_scoped
    )
    path = f'"/api/v1/{name}/{ents}"'
    create = _call(
        "    created = await client.post",
        f'{path}, json={{"title": "first"}}, headers=headers',
    )
    fetch = _call(
        "    fetched = await client.get",
        f"f\"/api/v1/{name}/{ents}/{{data['id']}}\", headers=headers",
    )
    anonymous = _call(
        "    response = await client.post", f'{path}, json={{"title": "x"}}'
    )
    add_one = _call(
        "        created = await client.post",
        f'{path}, json={{"title": title}}, headers=headers',
    )
    listed = _call("    listed = await client.get", f"{path}, headers=headers")
    page = _call(
        "    page = await client.get",
        f'{path}, params={{"limit": 2}}, headers=headers',
    )
    foreign = _call("    foreign = await client.get", f"{path}, headers=other")
    list_test = f"test_list_{ents}_is_capped"
    other_user = ""
    foreign_check = ""
    if tenant_scoped:
        list_test += "_and_tenant_scoped"
        other_user = "    _, other = as_user(tenant_id=uuid4())\n"
        foreign_check = (
            f"{foreign}\n"
            '    if sandloom_core.settings.tenancy_mode == "single":\n'
            "        # One canonical tenant: a token naming another one is refused.\n"
            "        assert foreign.status_code == 403\n"
            '        assert foreign.json()["code"] == "tenant_mismatch"\n'
            "    else:\n"
            "        assert foreign.json() == []\n"
        )
    over_cap = _call(
        "    over_cap = await client.get",
        f'{path}, params={{"limit": 101}}, headers=headers',
    )
    create_test = _call(
        f"async def test_create_and_read_{article}_{ent}", fixtures, " -> None:"
    )
    list_def_test = _call(f"async def {list_test}", fixtures, " -> None:")
    auth_test = _call(
        f"async def test_{ents}_need_an_authenticated_user",
        "client: Any",
        " -> None:",
    )
    files[
        f"tests/test_{name}.py"
    ] = f'''"""Tests of the {name} module (sandloom.sdk.testing fixtures)."""

from typing import Any
{uuid_import}
from sandloom.sdk.testing import as_user, published

from ..events import {created_event}
{tenant_helper}

{create_test}
    _, headers = {test_user}
{create}
    assert created.status_code == 201, created.text
    assert len(published({created_event})) == 1
    data = created.json()
{fetch}
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "first"


{list_def_test}
    _, headers = {test_user}
{other_user}    for title in ("a", "b", "c"):
{add_one}
        assert created.status_code == 201, created.text
{listed}
    assert listed.status_code == 200
    # Newest first, deterministically: all three rows share one transaction.
    assert [item["title"] for item in listed.json()] == ["c", "b", "a"]
{page}
    assert len(page.json()) == 2
{foreign_check}{over_cap}
    assert over_cap.status_code == 422


{auth_test}
{anonymous}
    assert response.status_code == 401
'''
    return files


_MAKO = '''"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
'''


def _rls_block(table: str, *, system_access: bool = False) -> tuple[str, str]:
    """The upgrade / downgrade lines of a ``TenantScoped`` table: one call each,
    acting only where the database's INFRA instance sets ``tenant_rls``."""
    extra = ", system_access=True" if system_access else ""
    quoted = json.dumps(table)
    enable = _call("    enable_tenant_rls", f"op, {quoted}{extra}, if_configured=True")
    disable = _call("    disable_tenant_rls", f"op, {quoted}, if_configured=True")
    return f"{enable}\n", f"{disable}\n"


def _baseline(name: str, plural: str = "items", *, tenant_scoped: bool = True) -> str:
    table = f"{name}_{plural}"
    rls_import = f"{RLS_IMPORT}\n\n" if tenant_scoped else ""
    tenant_column = (
        '        sa.Column("tenant_id", sa.Uuid(), nullable=False),\n'
        if tenant_scoped
        else ""
    )
    tenant_index = (
        f'    op.create_index(op.f("ix_{table}_tenant_id"), "{table}", ["tenant_id"])\n'
        if tenant_scoped
        else ""
    )
    drop_tenant_index = (
        f'    op.drop_index(op.f("ix_{table}_tenant_id"), table_name="{table}")\n'
        if tenant_scoped
        else ""
    )
    up_tail = down_head = ""
    if tenant_scoped:
        up_tail, down_head = _rls_block(table)
    return f'''"""{name} baseline: the example table (rendered by `sandloom add module`)

Revision ID: 0001_{name}
Revises:
Create Date: generated

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

{rls_import}revision: str = "0001_{name}"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "{table}",
{tenant_column}        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
{tenant_index}    op.create_index(op.f("ix_{table}_owner_id"), "{table}", ["owner_id"])
{up_tail}

def downgrade() -> None:
{down_head}    op.drop_index(op.f("ix_{table}_owner_id"), table_name="{table}")
{drop_tenant_index}    op.drop_table("{table}")
'''


def add_module(
    name: str,
    where: Layout,
    *,
    with_settings: bool = False,
    entity: str | None = None,
    empty: bool = False,
) -> list[Path]:
    """``sandloom add module <name>``: a new package; refuses an existing one.

    The package carries one example entity, ``item`` unless ``entity`` names
    another, or none with ``empty`` (the package and its migration chain only).
    ``settings.py`` is written on ``with_settings`` only.

    Also wires it where its project loads modules (``wire_module``: the
    framework tree's ``LOCAL_MODULES``, an ``SANDLOOM_MODULES`` list in the env
    files and the monolith profile; an ``sandloom new`` app discovers its modules
    and lists nothing). In the framework tree, whose suite does not activate
    the ``sandloom.sdk.testing`` plugin, the module gets a one-line
    ``tests/conftest.py`` with the kit's fixtures. Returns created and edited
    files.
    """
    _check_name(name, "the module name")
    if entity is not None:
        _check_name(entity, "the entity name")
        # The example names the entity's id ``<entity>_id`` (route parameter,
        # service argument, event field) next to the columns every module's
        # example carries: a name that makes it one of them would write the
        # field twice (a SyntaxError in services.py).
        framework = {"owner_id"} | ({"tenant_id"} if where.tenant_scoped else set())
        if f"{entity}_id" in framework:
            raise GeneratorError(
                f"the entity name {entity!r} makes the example's {entity}_id the "
                f"{entity}_id the scaffold already writes for the row's "
                f"{entity}: give the entity another name"
            )
        if empty:
            raise GeneratorError(
                "--entity names the example entity and --empty leaves it out: "
                "give one of them"
            )
    if empty and not where.tenant_scoped:
        raise GeneratorError(
            "--empty has no table, so --no-tenant has nothing to change: "
            "give one of them"
        )
    target = where.module_dir(name)
    if target.exists():
        raise GeneratorError(f"{target} already exists")
    if empty:
        files = _empty_files(name, with_settings=with_settings)
    else:
        files = _module_files(
            name,
            with_settings=with_settings,
            tenant_scoped=where.tenant_scoped,
            entity=entity or "item",
        )
    if where.framework_tree:
        files["tests/conftest.py"] = FRAMEWORK_TREE_CONFTEST
    if not contract_inherited(target.parent):
        # Agents working in the module get its rules (Claude Code reads the
        # nested CLAUDE.md, which imports AGENTS.md; Codex reads AGENTS.md).
        files["AGENTS.md"] = module_contract()
        files["CLAUDE.md"] = "@AGENTS.md\n"
    created = [_write(target / rel, body) for rel, body in files.items()]
    return created + wire_module(where.dotted(name), target)


# --------------------------------------------------------------------------- #
# sandloom add event
# --------------------------------------------------------------------------- #


def parse_fields(specs: list[str]) -> list[tuple[str, str, str | None]]:
    """``["note_id:UUID", ...]`` -> ``[(name, annotation, import line)]``."""
    out = []
    for spec in specs:
        name, _, kind = spec.partition(":")
        if not _NAME.fullmatch(name) or kind not in FIELD_TYPES:
            raise GeneratorError(
                f"--field {spec!r}: expected name:type with type in "
                f"{', '.join(FIELD_TYPES)}"
            )
        annotation, imp = FIELD_TYPES[kind]
        out.append((name, annotation, imp))
    return out


def _require_module(where: Layout, module: str) -> Path:
    _check_name(module, "the module name")
    directory = where.module_dir(module)
    if not (directory / "__init__.py").is_file():
        raise GeneratorError(f"no module {module!r} at {directory}")
    return directory


def _class_block(
    cls: str, channel: str, fields: list[tuple[str, str, str | None]]
) -> str:
    lines = [f'class {cls}(Event, channel="{channel}"):']
    lines += [f"    {name}: {annotation}" for name, annotation, _ in fields]
    return "\n".join(lines) + "\n"


def event_channel(module: str, name: str) -> str:
    """The channel ``sandloom add event <module> <Name>`` declares."""
    channel = f"{module}.{_snake(name).replace('_', '.', 1)}.v1"
    if channel.count(".") < 2:
        channel = f"{module}.{_snake(name)}.v1"
    return channel


def add_event(module: str, name: str, where: Layout, fields: list[str]) -> list[Path]:
    """``sandloom add event <m> <Name>``: append a producer class to ``events.py``.

    The event carries ``tenant_id: UUID`` (bound as the consumer's tenant) unless
    ``where`` is not tenant-scoped (``--no-tenant``, an app without tenants):
    there a tenant-less event is legal, and a ``--field tenant_id:UUID`` is
    still written as given."""
    directory = _require_module(where, module)
    if not _CLASS.fullmatch(name):
        raise GeneratorError(f"the event name must be CamelCase: {name!r}")
    parsed = parse_fields(fields) or [
        (f"{_snake(name).split('_')[0]}_id", "UUID", "from uuid import UUID"),
    ]
    if where.tenant_scoped and not any(f[0] == "tenant_id" for f in parsed):
        parsed.append(("tenant_id", "UUID", "from uuid import UUID"))
    channel = event_channel(module, name)
    path = directory / "events.py"
    needed = sorted(
        {imp for _, _, imp in parsed if imp} | {"from sandloom.sdk import Event"}
    )
    missing = _imports_present(path, needed)
    if missing:
        raise GeneratorError(
            f"{path} lacks {missing}: add the import(s) at the top, then re-run"
        )
    stdlib = [line for line in needed if not line.startswith("from sandloom")]
    header = (
        f'"""Events of the {module} module (its own channels)."""\n\n'
        + ("\n".join(stdlib) + "\n\n" if stdlib else "")
        + "from sandloom.sdk import Event\n\n\n"
    )
    return [_append(path, _class_block(name, channel, parsed), header=header)]


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# --------------------------------------------------------------------------- #
# sandloom add contract
# --------------------------------------------------------------------------- #


def add_contract(module: str, name: str, where: Layout) -> list[Path]:
    """``sandloom add contract <m> <name>``: the provider in ``contracts.py``
    and its DTO in the module's ``schemas.py`` (the provider authors it).
    The stub and the DTO's copy in the shared DTO package are generated
    files: written at once so the scaffold boots, then refreshed by
    ``sandloom generate``, which ``sandloom add`` runs last."""
    directory = _require_module(where, module)
    _check_name(name, "the contract name")
    contract = f"{module}.{name}.v1"
    cls = camel(name)
    dto = f"{cls}Status"
    created = [_contract_dto(directory, contract, dto)]
    block = f'''@contract("{contract}")
class {cls}:
    """Contract ``{contract}``: public ``async def`` methods are reads,
    ``@command`` marks a command (deduplicated by its key, a saga step)."""

    async def status(self) -> {dto}:
        return {dto}(ready=True)

    @command(idempotency_key="operation_key")
    async def record(self, amount: int, operation_key: str) -> int:
        return amount
'''
    path = directory / "contracts.py"
    needed = ["from sandloom.sdk import command, contract"]
    missing = _imports_present(path, needed)
    if missing:
        raise GeneratorError(f"{path} lacks {missing}: add the import, then re-run")
    own = f"from .schemas import {dto}"
    header = (
        f'"""Contracts the {module} module provides."""\n\n{needed[0]}\n\n{own}\n\n\n'
    )
    if path.exists():
        path.write_text(
            _import_dto(path.read_text(encoding="utf-8"), dto), encoding="utf-8"
        )
    created.append(_append(path, block, header=header))
    _contract_bootstrap(where, module, name, contract)
    return created


def _contract_bootstrap(where: Layout, module: str, name: str, contract: str) -> None:
    """Write the new contract's stub and its DTO copies at once, rendered
    like ``sandloom generate`` renders them: code in the same scaffold (a
    saga, a caller) imports the stub, so the app must boot before
    ``sandloom generate`` (run last by ``sandloom add``) rewrites every
    generated file."""
    stubs = _stubs()
    copies = importlib.import_module("sandloom.core.cli.dto_copies")
    stem, _protocol, _ref = stubs.stub_names(contract)
    contracts_dir, contracts_package = where.generated_home(module)
    init_text = '"""Contract DTO package (generated stubs and DTO copies)."""\n'
    inits = [contracts_dir]
    if where.shared_dir is not None:
        inits.insert(0, where.shared_dir)
    else:  # the module's `_generated` package, with its own docstring
        inits.insert(0, contracts_dir.parent)
        init_text = _consumer_init()
    for directory in inits:
        init = directory / "__init__.py"
        if not init.exists():
            _write(init, init_text)
    if str(where.source_root) in sys.path:
        sys.path.remove(str(where.source_root))
    sys.path.insert(0, str(where.source_root))
    importlib.invalidate_caches()
    package = where.dotted(module)
    top = where.modules_package.split(".", 1)[0] or package.split(".", 1)[0]
    for loaded in [m for m in sys.modules if m == top or m.startswith(top + ".")]:
        del sys.modules[loaded]  # import the project's current files
    module_contracts = importlib.import_module(f"{package}.contracts")
    provider = getattr(module_contracts, camel(name))
    dto_stem = copies.dto_module_stem(module)
    resolver = copies.Resolver(
        provider_package=package,
        dto_package=f"{contracts_package}.{dto_stem}",
        shared_packages=(
            copies.KERNEL_SHARED,
            *([where.shared_package] if where.shared_package else []),
        ),
    )
    # Exactly as `sandloom generate` writes them: provenance header, final
    # layout, never the project's formatter (ADR-0013 decisions 6, 7).
    consumer = importlib.import_module("sandloom.core.cli.consumer_copies")
    label = consumer.distribution_label(package)
    group = [consumer.Provider(contract, provider, package, label)]
    for other in vars(module_contracts).values():
        if not issubclass(type(other), type):  # never touch a lazy proxy
            continue
        spec = other.__dict__.get("__sandloom_contract__")
        if spec is not None and other is not provider:
            stubs.render_stub(spec.name, other, resolver)  # its DTOs too
            group.append(consumer.Provider(spec.name, other, package, label))
    (contracts_dir / f"{stem}.py").write_text(
        consumer.stub_file(contract, group[0], resolver, stubs.STUBS),
        encoding="utf-8",
    )
    target = contracts_dir / f"{dto_stem}.py"
    if resolver.copied:
        target.write_text(consumer.dto_file(module, resolver, group), encoding="utf-8")


def _contract_dto(directory: Path, contract: str, dto: str) -> Path:
    """Append the contract's DTO to the module's ``schemas.py`` (the provider
    authors its contract DTOs; ``sandloom generate`` copies them)."""
    path = directory / "schemas.py"
    if (directory / "schemas").is_dir():
        path = directory / "schemas" / "__init__.py"
    needed = ["from pydantic import BaseModel"]
    source = path.read_text(encoding="utf-8") if path.exists() else ""
    if path.exists() and not re.search(
        r"^from pydantic import [^\n]*\bBaseModel\b", source, re.M
    ):
        raise GeneratorError(f"{path} lacks {needed}: add the import, then re-run")
    header = (
        '"""Request and response shapes (pydantic only)."""\n\n' + needed[0] + "\n\n\n"
    )
    block = f'''class {dto}(BaseModel):
    """What ``{contract}.status`` returns. A contract DTO lives in its
    provider; `sandloom generate` copies it into the shared DTO package."""

    ready: bool
'''
    return _append(path, block, header=header)


def _import_dto(source: str, dto: str) -> str:
    """``source`` importing ``dto`` from the module's schemas: added to an
    existing one-line ``from .schemas import ...``, else a new line after the
    last top-level import."""
    tree = ast.parse(source)
    lines = source.split("\n")
    after = 0
    for node in tree.body:
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        after = node.end_lineno or node.lineno
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module == "schemas"
            and node.lineno == node.end_lineno
            and all(a.asname is None for a in node.names)
        ):
            names = sorted({a.name for a in node.names} | {dto})
            line = f"from .schemas import {', '.join(names)}"
            if len(line) > _LINE:
                inner = "".join(f"    {n},\n" for n in names)
                line = f"from .schemas import (\n{inner})"
            lines[node.lineno - 1] = line
            return "\n".join(lines)
    if any(f"import {dto}" in line for line in lines):
        return source
    lines[after:after] = [f"from .schemas import {dto}"]
    return "\n".join(lines)


def _consumer_init() -> str:
    """The ``__init__.py`` text of a module's ``_generated`` package."""
    return str(importlib.import_module("sandloom.core.cli.consumer_copies").INIT)


def _stubs() -> Any:
    """:mod:`sandloom.core.cli.generated`, imported when a stub is named: the
    other generators stay static (no settings module)."""
    return importlib.import_module("sandloom.core.cli.generated")


#: ``sandloom generate`` in a child process (the project's ``.env.test``;
#: nothing connects to a database), run after every generator.
_GENERATE = """\
import sys
from pathlib import Path
from sandloom.core.cli.run_tests import load_project_test_env
from sandloom.core.observability import log_level
log_level.request_default("WARNING")
root = Path(sys.argv[1])
load_project_test_env(root)
from sandloom.core.cli.generated import main
sys.exit(main(["--root", str(root), *sys.argv[2:]]))
"""


def own_modules(root: Path) -> list[str]:
    """The names of the modules under the project's modules directory."""
    try:
        return load_rules(root / "architecture_rules.yml").business_modules()
    except ManifestError as exc:
        raise GeneratorError(str(exc)) from exc


def draft_options(names: Iterable[str]) -> list[str]:
    """``--draft NAME`` for each name: what the creating commands pass to
    ``sandloom generate`` so what they just created is written as a draft
    (a snapshot written fresh would record it as released, ADR-0017)."""
    return [option for name in names for option in ("--draft", name)]


def created_names(args: argparse.Namespace) -> list[str]:
    """The module, contracts and events ``sandloom add <what>`` created."""
    if args.what == "module":
        return [args.module]
    if args.what == "event":
        return [event_channel(args.module, args.name)]
    if args.what == "contract":
        return [f"{args.module}.{args.name}.v1"]
    if args.what == "saga":
        return [f"{args.module}.{args.name}_ledger.v1"]
    if args.what == "projection":
        return [
            f"{args.module}.{args.name}.saved.v1",
            f"{args.module}.{args.name}.deleted.v1",
        ]
    return []


def run_generate(root: Path, *options: str) -> tuple[int, str]:
    """``sandloom generate [options]`` for the project at ``root``, in a child
    process of this interpreter (a fresh import of the project's files);
    returns ``(exit code, output)``."""
    proc = _tool_process.run_tool(
        [sys.executable, "-c", _GENERATE, str(root.resolve()), *options],
        cwd=root,
        capture=True,
    )
    return proc.returncode, proc.stdout or ""


# --------------------------------------------------------------------------- #
# sandloom add contract-use
# --------------------------------------------------------------------------- #

_REQUIREMENT_IMPORT = (
    "from sandloom.sdk.module import ContractRequirement  # sandloom: advanced contract"
)


def _requirement_name(item: ast.expr) -> str | None:
    """The contract a ``consumes=`` entry names (a string, or the first
    argument of ``ContractRequirement(...)``)."""
    if isinstance(item, ast.Call) and item.args:
        item = item.args[0]
    if isinstance(item, ast.Constant) and isinstance(item.value, str):
        return item.value
    return None


def _declare_consumes(init: Path, contract: str) -> tuple[list[Path], str | None]:
    """Declare ``contract`` in the module's ``define_module(consumes=[...])``.
    A call it cannot rewrite safely (comments inside it, a computed list) is
    left alone and the note says what to write; an entry already there is kept."""
    manual = (
        f"declare it in {init.parent.name}/__init__.py: define_module(..., "
        f'consumes=[ContractRequirement("{contract}")])'
    )
    site = _define_module_site(init)
    if site is None:
        return [], manual
    source, tree, statement, call = site
    entries: list[str] = []
    consumes = next((k for k in call.keywords if k.arg == "consumes"), None)
    if consumes is not None:
        if not isinstance(consumes.value, ast.List | ast.Tuple):
            return [], manual
        for item in consumes.value.elts:
            segment = ast.get_source_segment(source, item)
            if segment is None:
                return [], manual
            if _requirement_name(item) == contract:
                return [], None  # declared already
            entries.append(segment)
    entries.append(f'ContractRequirement("{contract}")')
    source = _with_keyword(source, statement, call, "consumes", entries)
    source = _import_requirement(
        source, tree, "ContractRequirement", _REQUIREMENT_IMPORT
    )
    init.write_text(source, encoding="utf-8")
    return [init], None


def add_contract_use(
    module: str, contract: str, where: Layout
) -> tuple[list[Path], str | None]:
    """``sandloom add contract-use <m> <contract>``: ``<m>`` consumes
    ``contract`` (a name such as ``tenancy.membership.v1``). Declares it in
    ``define_module(consumes=[ContractRequirement(...)])`` and writes the stub
    (and the DTO copies it names) from the importable provider at once, so the
    module's code can import it before any ``sandloom generate`` ran."""
    directory = _require_module(where, module)
    if not _CHANNEL.fullmatch(contract):
        raise GeneratorError(
            f"not a versioned contract name such as tenancy.membership.v1: {contract!r}"
        )
    package = where.dotted(module)
    consumer = importlib.import_module("sandloom.core.cli.consumer_copies")
    if consumer.is_kernel_family(contract):
        raise GeneratorError(
            f"{contract} is a kernel contract family: its shape is fixed by the "
            "kernel and it gets no stub; consume it with "
            "ContractRequirement alone"
        )
    modules = {
        where.dotted(d.name): d
        for d in sorted(where.modules_dir.iterdir())
        if (d / "__init__.py").is_file() and not d.name.startswith(("_", "."))
    }
    if str(where.source_root) in sys.path:
        sys.path.remove(str(where.source_root))
    sys.path.insert(0, str(where.source_root))
    importlib.invalidate_caches()
    provider = consumer.importable_providers(where.source_root, modules).get(contract)
    if provider is None:
        raise GeneratorError(
            f"no importable provider declares {contract}: install the module that "
            "provides it (it is listed by `sandloom docs contracts`), or check the "
            "name; the file was not changed"
        )
    if provider.package == package:
        raise GeneratorError(f"{module} provides {contract} itself: nothing to consume")
    created, note = _declare_consumes(directory / "__init__.py", contract)
    generated = _stubs()
    if where.shared_dir is not None and where.shared_package is not None:
        planned = consumer.plan_shared(
            modules,
            where.source_root,
            generated.STUBS,
            where.shared_dir,
            where.shared_package,
            (
                importlib.import_module("sandloom.core.cli.dto_copies").KERNEL_SHARED,
                where.shared_package,
            ),
            extra=[contract],
        )
    else:  # a tree of module packages: the module's own _generated/ home
        planned, _names = consumer.plan_static(
            {package: directory}, where.source_root, generated.STUBS
        )
    files = [
        generated.Generated(f.path, f.content, f.code, f.problems) for f in planned
    ]
    declared = bool(created)
    created += [f.path for f in files if f.stale and not f.problems]
    if generated.apply(files, write=True, shared_dir=where.shared_dir) != 0:
        raise GeneratorError(
            f"the stub of {contract} was not written (see above); "
            + (
                f"the declaration is in {directory.name}/__init__.py"
                if declared
                else "no file was changed"
            )
        )
    return created, note


# --------------------------------------------------------------------------- #
# sandloom add consumer
# --------------------------------------------------------------------------- #


def _producer_fields(where: Layout, channel: str) -> tuple[list[tuple[str, str]], str]:
    """The producer's fields for a view: from the tree, else the exported
    schema; ``([], "")`` when neither is found."""
    for py in api10_rules.iter_python([where.modules_dir]):
        if "tests" in py.parts or "migrations" in py.parts:
            continue
        for event in api10_rules.event_classes(py):
            module = py.relative_to(where.modules_dir).parts[0]
            if event.channel == channel and channel.split(".", 1)[0] == module:
                return list(
                    event.fields.items()
                ), f"{py.relative_to(where.modules_dir)}"
    exported = (
        where.shared_dir / "events" / f"{channel}.json" if where.shared_dir else None
    )
    if exported is not None and exported.is_file():
        schema = json.loads(exported.read_text(encoding="utf-8"))
        names = {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
        }
        fields = []
        for prop, spec in (schema.get("properties") or {}).items():
            kind = (
                "UUID"
                if spec.get("format") == "uuid"
                else names.get(spec.get("type"), "str")
            )
            fields.append((prop, kind))
        return fields, exported.name
    return [], ""


def add_consumer(
    module: str, channel: str, where: Layout, fields: list[str]
) -> list[Path]:
    """``sandloom add consumer <m> <channel>``: a view of ``channel`` and an
    ``@on`` handler in ``consumers.py``."""
    directory = _require_module(where, module)
    if not _CHANNEL.fullmatch(channel):
        raise GeneratorError(f"not a versioned channel: {channel!r}")
    found, source = _producer_fields(where, channel)
    if fields:
        view_fields = [(n, a) for n, a, _ in parse_fields(fields)]
        note = "fields from --field"
    elif found:
        view_fields = found
        note = f"fields of the producer ({source})"
    else:
        raise GeneratorError(
            f"no producer schema for {channel!r} in this tree"
            + (f" or {where.shared_dir / 'events'}" if where.shared_dir else "")
            + ": pass the fields (--field name:type ...); "
            "the view is then marked unverified"
        )
    unverified = "" if found else "  # unverified: no producer schema was found"
    parts = channel.split(".")[1:-1]
    cls = "".join(camel(p) for p in parts) or camel(parts[0])
    handler = f"on_{'_'.join(parts)}"
    lines = [
        f'class {cls}(Event, channel="{channel}"):{unverified}',
        f'    """A view: {note}."""',
        "",
    ]
    lines += [f"    {n}: {a}" for n, a in view_fields]
    # The group is named from the function: an explicit name= keeps the stream
    # position when the function is renamed (docs/modules/between/events.md).
    block = (
        "\n".join(lines)
        + f'\n\n\n@on({cls}, name="{handler}")\n'
        + f"async def {handler}(event: {cls}) -> None:\n"
        + '    """Runs once per event (durable), with the event\'s tenant bound."""\n'
    )
    imports = sorted(
        {imp for _, a in view_fields if (imp := FIELD_TYPES.get(a, ("", None))[1])}
    )
    path = directory / "consumers.py"
    needed = [*imports, "from sandloom.sdk import Event, on"]
    missing = _imports_present(path, needed)
    if missing:
        raise GeneratorError(f"{path} lacks {missing}: add the import(s), then re-run")
    stdlib = "\n".join(imports)
    header = (
        f'"""Consumers of the {module} module."""\n\n'
        + (stdlib + "\n\n" if stdlib else "")
        + "from sandloom.sdk import Event, on\n\n\n"
    )
    created = [_append(path, block, header=header)]
    tests_init = directory / "tests" / "__init__.py"
    if not tests_init.exists():  # `add module` writes it: never a `__init__.py.new`
        created.append(_write(tests_init, ""))
    created.append(
        _write(
            directory / "tests" / f"test_{handler}.py",
            _consumer_test(cls, handler, view_fields),
        )
    )
    return created


#: A sample value per view field type and the names it imports, by module.
_SAMPLES: dict[str, tuple[str, dict[str, tuple[str, ...]]]] = {
    "str": ('"sample"', {}),
    "int": ("1", {}),
    "float": ("1.0", {}),
    "bool": ("True", {}),
    "UUID": ("uuid4()", {"uuid": ("uuid4",)}),
    "datetime": (
        "datetime(2026, 1, 1, tzinfo=UTC)",
        {"datetime": ("UTC", "datetime")},
    ),
    "date": ("date(2026, 1, 1)", {"datetime": ("date",)}),
    "Decimal": ('Decimal("1")', {"decimal": ("Decimal",)}),
}


def _consumer_test(cls: str, handler: str, view_fields: list[tuple[str, str]]) -> str:
    """The test of a scaffolded consumer: the same event delivered twice, applied
    once (``deliver`` keeps the envelope id across ``times``)."""
    modules: dict[str, set[str]] = {"typing": {"Any"}}
    arguments: list[str] = []
    marked = False
    for field, annotation in view_fields:
        sample = _SAMPLES.get(annotation)
        if sample is None:  # a type with no sample: marked, never guessed
            arguments.append(f"{field}=None,  # a {annotation} value for {field}")
            marked = True
            continue
        value, needs = sample
        for module_name, names in needs.items():
            modules.setdefault(module_name, set()).update(names)
        arguments.append(f"{field}={value},")
    imports = "\n".join(
        f"from {m} import {', '.join(sorted(names))}"
        for m, names in sorted(modules.items())
    )
    one_line = f"{cls}({' '.join(arguments)[:-1]})" if arguments else f"{cls}()"
    if marked or len(f"    event = {one_line}") > _LINE:
        body = "\n".join(f"        {a}" for a in arguments)
        event = f"{cls}(\n{body}\n    )"
    else:
        event = one_line
    test = _call(
        f"async def test_{handler}_applies_a_redelivered_event_once",
        "client: Any, db: Any",
        " -> None:",
    )
    return f'''"""Tests of the {handler} consumer (sandloom.sdk.testing fixtures)."""

{imports}

from sandloom.sdk.testing import deliver

from ..consumers import {cls}


{test}
    event = {event}
    consumers = await deliver(event, times=2, session=db)
    assert consumers >= 1  # this consumer is registered for the channel
    # Then assert the handler's effect once: a row written, a counter at 1 and
    # not 2. Both deliveries carry one envelope id, so a durable consumer
    # applies the event once.
'''


# --------------------------------------------------------------------------- #
# sandloom add saga
# --------------------------------------------------------------------------- #


def add_saga(module: str, name: str, where: Layout) -> list[Path]:
    """``sandloom add saga <m> <name>``: a saga whose command step calls a ledger
    contract the module provides (replace it with the real provider)."""
    directory = _require_module(where, module)
    _check_name(name, "the saga name")
    ledger = f"{name}_ledger"
    created = add_contract(module, ledger, where)
    stem, _protocol, ref = _stubs().stub_names(f"{module}.{ledger}.v1")
    cls = camel(name)
    saga_imports = "\n".join(
        sorted(
            [
                "from sandloom.sdk import Contract",
                "from sandloom.sdk.sagas import Saga, local_step, step",
                f"from {where.generated_home(module)[1]}.{stem} import {ref}",
            ]
        )
    )
    sagas = f'''"""The {name} saga (ADR-0008): ``reserve``, then ``confirm``.

Replace the stand-in ledger contract (contracts.py) with the real provider's
command contract; the typed step names it through its generated reference.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel

{saga_imports}

ledger = Contract({ref})


class {cls}Input(BaseModel):
    owner_id: UUID
    amount: int


def _amount(ctx: Any) -> dict[str, Any]:
    return {{"amount": ctx.input.amount}}


async def _confirm(ctx: Any) -> dict[str, str]:
    return {{"owner_id": str(ctx.input.owner_id)}}


{name.upper()} = Saga(
    "{name}",
    version=1,
    input={cls}Input,
    deadline=timedelta(minutes=10),
    steps=[
        step("reserve", call=ledger.record, args=_amount, compensate=ledger.record),
        local_step("confirm", apply=_confirm, kind="pivot"),
    ],
)
'''
    created.append(_write(directory / "sagas.py", sagas))
    return created


# --------------------------------------------------------------------------- #
# sandloom add projection
# --------------------------------------------------------------------------- #


def _chain_head(versions: Path) -> str | None:
    revisions: dict[str, str | None] = {}
    for py in sorted(versions.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign | ast.Assign) and node.value is not None:
                target = (
                    node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
                )
                if isinstance(target, ast.Name) and isinstance(
                    node.value, ast.Constant
                ):
                    values[target.id] = node.value.value
        rev = values.get("revision")
        if isinstance(rev, str):
            down = values.get("down_revision")
            revisions[rev] = down if isinstance(down, str) else None
    referenced = set(revisions.values())
    heads = [r for r in revisions if r not in referenced]
    if len(heads) > 1:
        raise GeneratorError(f"{versions} has several heads {heads}: merge them first")
    return heads[0] if heads else None


def add_projection(module: str, name: str, where: Layout) -> list[Path]:
    """``sandloom add projection <m> <name>``: a projection of the module's own
    ``<name>.saved`` / ``<name>.deleted`` events, its row model and the
    migration that creates its table."""
    directory = _require_module(where, module)
    _check_name(name, "the projection name")
    cls = camel(name)
    table = f"{module}_{name}_rows"
    rows = f'''"""Rows of the {module}.{name} projection (a read model)."""

import uuid
from datetime import datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, TenantScoped, UtcDateTime


class {cls}Row(TenantScoped, Base):
    __tablename__ = "{table}"

    {name}_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
'''
    projection = f'''"""The {module}.{name} projection.

Full-state events of one aggregate, versioned tombstones, one owning group
per column.
"""

from uuid import UUID

from sandloom.sdk import Event
from sandloom.sdk.projections import (
    Projection,
    SourceGroup,
    full_state,
    tombstone,
)

from .models.{name}_row import {cls}Row


class {cls}Saved(Event, channel="{module}.{name}.saved.v1"):
    {name}_id: UUID
    tenant_id: UUID
    title: str
    version: int


class {cls}Deleted(Event, channel="{module}.{name}.deleted.v1"):
    {name}_id: UUID
    tenant_id: UUID
    version: int


{name.upper()} = Projection(
    "{module}.{name}",
    key=("tenant_id", "{name}_id"),
    model={cls}Row,
    groups=[
        SourceGroup(
            events={{
                {cls}Saved: full_state(lambda e: {{"title": e.title}}),
                {cls}Deleted: tombstone(lambda e: {{}}),
            }},
            aggregate_key="{name}_id",
            version_field="version",
            version_column="version",
            owns=("title",),
        )
    ],
)
'''
    versions = directory / "migrations" / "versions"
    head = _chain_head(versions) if versions.is_dir() else None
    revision = next_revision(versions, module)
    up_rls, down_rls = _rls_block(table)
    migration = f'''"""{module}.{name} projection table (by `sandloom add projection`)

Revision ID: {revision}
Revises: {head or ""}
Create Date: generated

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

{RLS_IMPORT}

revision: str = "{revision}"
down_revision: str | Sequence[str] | None = {json.dumps(head) if head else "None"}
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "{table}",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("{name}_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "{name}_id"),
    )
    op.create_index(op.f("ix_{table}_tenant_id"), "{table}", ["tenant_id"])
{up_rls}

def downgrade() -> None:
{down_rls}    op.drop_index(op.f("ix_{table}_tenant_id"), table_name="{table}")
    op.drop_table("{table}")
'''
    if not (directory / "models").is_dir():
        raise GeneratorError(
            f"{directory}/models is not a package: move the module's tables into "
            "models/ (as `sandloom add module` lays them out), then re-run"
        )
    created = [
        _write(directory / "models" / f"{name}_row.py", rows),
        _write(directory / "projections.py", projection),
        _write(versions / f"{revision}_{name}_projection.py", migration),
    ]
    return created


# --------------------------------------------------------------------------- #
# sandloom add task
# --------------------------------------------------------------------------- #

# Tier 2 (``__after_first__`` of ``sandloom.sdk.tasks`` and ``.data``): no
# marker in a module's ``tasks.py``, the file these imports are written in.
_OPEN_UOW_IMPORT = "from sandloom.sdk.data import open_uow"
_TASKS_MODULE = "sandloom.sdk.tasks"


def _tasks_import(*names: str) -> str:
    return f"from {_TASKS_MODULE} import {', '.join(sorted(names))}"


def _task_block(module: str, name: str, schedule: str | None) -> str:
    if schedule is None:
        decorator = "@task"
    else:
        args = f'cron="{schedule}", id="{module}.{name.replace("_", "-")}"'
        one = f"@periodic({args})"
        decorator = (
            one if len(one) <= 88 else f"@periodic(\n    {args}\n)"
        )  # ruff format's line length
    if schedule is None:
        doc = (
            "The work of one run. It runs at most once unless the task\n"
            "    declares at_least_once=True (which needs the taskqueue feature\n"
            "    redelivery); then make it harmless to repeat (delete by a\n"
            "    condition, upsert, or claim a durable_inbox key)."
        )
    else:
        doc = (
            "The work of one run. A slot can overlap a run that still works. It\n"
            "    runs at most once per slot (a worker that dies mid-run loses it)\n"
            "    unless the task declares at_least_once=True, for work that must\n"
            "    not be lost (add the taskqueue feature redelivery next to\n"
            "    periodic); then make it harmless to repeat (delete by a\n"
            "    condition, upsert, or claim a durable_inbox key)."
        )
    return f'''{decorator}
async def {name}() -> None:
    """{doc}"""
    async with open_uow():
        ...  # db(), await Event(...).publish() and record_audit work here
'''


def _ensure_task_imports(path: Path, decorator: str) -> None:
    """Make an existing ``tasks.py`` import ``open_uow`` and ``decorator``
    (merged into its ``sandloom.sdk.tasks`` import when it has one)."""
    if not path.exists():
        return
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise GeneratorError(f"{path} does not parse ({exc}); fix it first") from exc
    lines = source.split("\n")
    imports = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    imported = {
        alias.asname or alias.name
        for node in imports
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    existing = next(
        (
            n
            for n in imports
            if isinstance(n, ast.ImportFrom) and n.module == _TASKS_MODULE
        ),
        None,
    )
    shift = 0  # lines the merged import gives back
    if existing is not None and decorator not in imported:
        names = {a.name for a in existing.names if a.asname is None} | {decorator}
        first, last = existing.lineno - 1, existing.end_lineno or existing.lineno
        lines[first:last] = [_tasks_import(*names)]
        shift = (last - first) - 1
    added = [_OPEN_UOW_IMPORT] if "open_uow" not in imported else []
    if existing is None and decorator not in imported:
        added.append(_tasks_import(decorator))
    if added:
        at = max((n.end_lineno or n.lineno for n in imports), default=0) - shift
        lines[at:at] = added
    path.write_text("\n".join(lines), encoding="utf-8")


def _wire_beat(where: Layout, module: str) -> tuple[list[Path], str]:
    """A beat for the module in the microservices profile, or the next step."""
    dotted = where.dotted(module)
    root = project_root(where.module_dir(module))
    deploy = None
    for base in [p for p in (root, root.parent if root else None) if p]:
        candidate = base / "deploy" / _deploy.PROFILE
        if candidate.is_file():
            deploy = candidate
            break
    if deploy is None or root is None:
        return [], (
            f"no deploy/{_deploy.PROFILE}: when you split the app, `sandloom add "
            f"deploy {module} --service <name>` gives the module its beat"
        )
    service = _deploy.service_of(deploy, dotted)
    if service is None:
        return [], (
            f"{dotted} is not deployed in {deploy.name}: run "
            f"`sandloom add deploy {module} --service <name>`; it adds the "
            "module's worker and beat (SANDLOOM-T010)"
        )
    try:
        result = _deploy.refresh(_deploy.Project.find(root, deploy.parent.parent))
    except _deploy.DeployError as exc:
        return (
            [],
            f"{deploy.name} not updated ({exc}): run `sandloom add deploy --refresh`",
        )
    if result.refusals:
        return [], (
            f"{deploy.name} has hand edits inside its generated blocks: run "
            "`sandloom add deploy --refresh` (it shows the diff) to add the beat"
        )
    if result.written:
        return (
            result.written,
            f"{service}-worker / {service}-beat run {dotted}'s schedule",
        )
    return [], f"{service}-beat already runs {dotted}'s schedule"


def add_task(
    module: str, name: str, where: Layout, *, periodic: str | None = None
) -> tuple[list[Path], str | None]:
    """``sandloom add task <m> <name> [--periodic "<cron>"]``: a ``@task`` (or,
    with ``--periodic``, a ``@periodic``) in ``tasks.py`` and, for a periodic
    one, the deploy wiring. Returns the edited files and a note to print."""
    directory = _require_module(where, module)
    _check_name(name, "the task name")
    if periodic is not None:
        try:
            parse_cron(periodic)  # the grammar the scheduler reads, not a field count
        except ValueError as exc:
            raise GeneratorError(
                f"--periodic {periodic!r}: {exc}; expected 5 cron fields "
                "'minute hour day-of-month month day-of-week', e.g. '0 3 * * *'"
            ) from exc
        if '"' in periodic or "\\" in periodic:
            raise GeneratorError(
                f"--periodic {periodic!r}: no quote or backslash in a cron expression"
            )
    path = directory / "tasks.py"
    if path.exists() and re.search(rf"^(async )?def {name}\(", path.read_text(), re.M):
        raise GeneratorError(f"{path} already defines {name}()")
    decorator = "task" if periodic is None else "periodic"
    _ensure_task_imports(path, decorator)  # an existing tasks.py
    header = (
        f'''"""Background tasks of the {module} module (the framework worker '''
        "imports this file).\n\nTask bodies are ``async def``: the worker runs them "
        "on its persistent loop,\nand a unit of work opens with "
        '``open_uow()``."""\n\n'
        f"{_OPEN_UOW_IMPORT}\n{_tasks_import(decorator)}\n\n\n"
    )
    block = _task_block(module, name, periodic)
    edited = [_append(path, block, header=header)]
    declared, declare_note = _declare_taskqueue(
        directory / "__init__.py", periodic=defines_periodic(path)
    )
    edited += declared
    if periodic is None:
        return edited, declare_note
    wired, note = _wire_beat(where, module)
    notes = [n for n in (note, declare_note) if n]
    return edited + wired, "; ".join(notes) or None


#: The declaration ``sandloom add task`` writes (SANDLOOM-C033).
_TASKQUEUE = '"taskqueue"'
_TASKQUEUE_PERIODIC = (
    'CapabilityRequirement("taskqueue", features=frozenset({"periodic"}))'
)
_CAPABILITY_IMPORT = (
    "from sandloom.sdk.module import CapabilityRequirement  "
    "# sandloom: advanced periodic"
)


def _taskqueue_entries(source: str, call: ast.Call, periodic: bool) -> list[str] | None:
    """The ``capabilities=`` entries after declaring the task queue, or
    ``None`` when the existing list is not a plain literal (or already
    declares it as it should be)."""
    keyword = next((k for k in call.keywords if k.arg == "capabilities"), None)
    if keyword is None:
        return [_TASKQUEUE_PERIODIC if periodic else _TASKQUEUE]
    if not isinstance(keyword.value, ast.List | ast.Tuple):
        return None
    entries: list[str] = []
    found = False
    for item in keyword.value.elts:
        segment = ast.get_source_segment(source, item)
        if segment is None:
            return None
        is_string = isinstance(item, ast.Constant) and item.value == "taskqueue"
        is_requirement = (
            isinstance(item, ast.Call)
            and item.args
            and isinstance(item.args[0], ast.Constant)
            and item.args[0].value == "taskqueue"
        )
        if is_requirement:
            return None  # a hand-written requirement: the author's to extend
        if is_string:
            found = True
            if periodic:
                segment = _TASKQUEUE_PERIODIC
            else:
                return None  # already declared
        entries.append(segment)
    if not found:
        entries.append(_TASKQUEUE_PERIODIC if periodic else _TASKQUEUE)
    return entries


def _declare_taskqueue(init: Path, *, periodic: bool) -> tuple[list[Path], str | None]:
    """Declare ``taskqueue`` (with ``periodic`` for a beat hook) in the
    module's ``define_module(capabilities=[...])``, which ``sandloom check``
    asks for (SANDLOOM-C033). A call it cannot rewrite safely (comments
    inside it, a computed list, a hand-written requirement) is left alone and
    the note says what to declare."""
    wanted = _TASKQUEUE_PERIODIC if periodic else f"[{_TASKQUEUE}]"
    manual = (
        f"declare the task queue in {init.parent.name}/__init__.py: "
        f"define_module(..., capabilities=[{wanted.strip('[]')}]) (SANDLOOM-C033)"
    )
    site = _define_module_site(init)
    if site is None:
        return [], manual
    source, tree, statement, call = site
    entries = _taskqueue_entries(source, call, periodic)
    if entries is None:
        has_periodic = "periodic" in (ast.get_source_segment(source, statement) or "")
        return [], (None if has_periodic or not periodic else manual)
    source = _with_keyword(source, statement, call, "capabilities", entries)
    if periodic:
        source = _import_requirement(
            source, tree, "CapabilityRequirement", _CAPABILITY_IMPORT
        )
    init.write_text(source, encoding="utf-8")
    return [init], None


def _define_module_site(
    init: Path,
) -> tuple[str, ast.Module, ast.Assign, ast.Call] | None:
    """The source of ``init``, its tree and its one ``x = define_module(...)``
    statement; ``None`` where a rewrite is not safe (no file, not exactly one
    call, comments inside it)."""
    if not init.is_file():
        return None
    source = init.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise GeneratorError(f"{init} does not parse ({exc}); fix it first") from exc
    found = [
        (node, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "define_module"
    ]
    if len(found) != 1:
        return None
    statement, call = found[0]
    text = ast.get_source_segment(source, statement)
    if text is None or "#" in text or call.end_lineno is None:
        return None
    return source, tree, statement, call


def _with_keyword(
    source: str,
    statement: ast.Assign,
    call: ast.Call,
    keyword: str,
    entries: list[str],
) -> str:
    """``source`` with the ``define_module`` statement's ``keyword=[entries]``
    set (the other arguments as written), laid out as ruff format does."""
    args = [ast.get_source_segment(source, a) or "" for a in call.args]
    args += [
        f"{k.arg}={ast.get_source_segment(source, k.value)}"
        if k.arg
        else f"**{ast.get_source_segment(source, k.value)}"
        for k in call.keywords
        if k.arg != keyword
    ]
    args.append(f"{keyword}=[{', '.join(entries)}]")
    targets = " = ".join(
        ast.get_source_segment(source, t) or "" for t in statement.targets
    )
    rewritten = _call(f"{targets} = define_module", ", ".join(args))
    lines = source.split("\n")
    start, end = statement.lineno - 1, statement.end_lineno or statement.lineno
    lines[start:end] = rewritten.split("\n")
    return "\n".join(lines)


def _import_requirement(source: str, tree: ast.Module, name: str, line: str) -> str:
    """``source`` importing ``name`` (``CapabilityRequirement`` or
    ``ContractRequirement``; ``line`` is its whole import line) from
    ``sandloom.sdk.module`` (lines before the rewritten ``define_module`` are
    those of ``tree``), in the place isort keeps it: merged into an existing
    one-line import of that module, else among the ``sandloom`` imports in
    order."""
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.level == 0]
    if any(
        (alias.asname or alias.name) == name for node in imports for alias in node.names
    ):
        return source
    lines = source.split("\n")
    module = "sandloom.sdk.module"
    for node in imports:
        if node.module == module and node.lineno == node.end_lineno:
            if any(a.asname for a in node.names):
                break  # an aliased import: a separate line keeps it readable
            comment = lines[node.lineno - 1].partition("#")[2]
            names = sorted({*(a.name for a in node.names), name})
            new = f"from {module} import {', '.join(names)}"
            new += f"  #{comment}" if comment else ""
            lines[node.lineno - 1] = new
            return "\n".join(lines)
    ours = [n for n in imports if (n.module or "").split(".")[0] == "sandloom"]
    after = [n for n in ours if (n.module or "") > module]
    if after:
        at = after[0].lineno - 1
    elif ours:
        at = ours[-1].end_lineno or ours[-1].lineno
    else:
        everything = [
            n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)
        ]
        at = (everything[-1].end_lineno or 0) if everything else 0
    lines[at:at] = [line]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


_NO_TENANT_HELP = (
    "for an app without tenants: no TenantScoped table (a plain table, no "
    "row-level security) and no tenant_id on the event. Without it the "
    "scaffold is always tenant-scoped (no setting is read)"
)


#: One example per generator, the last line of its ``--help``.
_EXAMPLES: dict[str, str] = {
    "module": "uv run sandloom add module notes",
    "event": "uv run sandloom add event notes NoteArchived "
    "--field note_id:UUID --field reason:str",
    "contract": "uv run sandloom add contract notes summary",
    "contract-use": "uv run sandloom add contract-use billing tenancy.membership.v1",
    "consumer": "uv run sandloom add consumer billing notes.note.created.v1 "
    "--field note_id:UUID",
    "saga": "uv run sandloom add saga orders checkout",
    "projection": "uv run sandloom add projection reports note_counts",
    "task": 'uv run sandloom add task notes purge_old --periodic "0 3 * * *"',
    "stack": "uv run sandloom add stack",
    "skills": "uv run sandloom add skills --agents claude,codex",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandloom add",
        description="Generators that write Sandloom code into the app. Each one "
        "creates or appends files inside a module package, then runs `sandloom "
        "generate` so the generated files are current. Run it from the project "
        "root, or pass --root.",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="project root")
    sub = parser.add_subparsers(dest="what", required=True)

    def generator(name: str, summary: str) -> argparse.ArgumentParser:
        return sub.add_parser(
            name,
            help=summary,
            description=summary,
            epilog=f"example: {_EXAMPLES[name]}",
        )

    types = ", ".join(FIELD_TYPES)
    module = generator(
        "module",
        "a module under the app's modules package: one example entity (a model, "
        "a route, an event, a migration, tests), or none with --empty",
    )
    module.add_argument("module", help="the new module's name (snake_case)")
    module.add_argument(
        "--entity",
        metavar="NAME",
        help="name the example entity (snake_case; default: item): its model, "
        "table, route, event and tests take the name",
    )
    module.add_argument(
        "--empty",
        action="store_true",
        help="no example entity: the package and its migration chain only (a "
        "module that starts from a model you write, then `sandloom migrate new`)",
    )
    module.add_argument(
        "--settings",
        action="store_true",
        help="also write settings.py (a module settings class, read with "
        "settings_of); a module that needs no setting has none",
    )
    module.add_argument("--no-tenant", action="store_true", help=_NO_TENANT_HELP)
    event = generator(
        "event",
        "append an event class to the module's events.py, with its channel "
        "<module>.<entity>.<verb>.v1 taken from the name",
    )
    event.add_argument("module", help="the module that owns the event")
    event.add_argument(
        "name", help="the event class name, CamelCase (e.g. NoteArchived)"
    )
    event.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME:TYPE",
        help=f"a payload field, as name:type with type one of {types}; repeat "
        "it for each field (default: one <entity>_id: UUID field; the event "
        "also gets tenant_id: UUID, unless --no-tenant)",
    )
    event.add_argument("--no-tenant", action="store_true", help=_NO_TENANT_HELP)
    contract = generator(
        "contract",
        "a @contract provider in the module's contracts.py and its DTO in schemas.py",
    )
    contract.add_argument("module", help="the module that provides the contract")
    contract.add_argument(
        "name",
        help="the contract's name (snake_case): the contract is <module>.<name>.v1",
    )
    use = generator(
        "contract-use",
        "declare that a module consumes a contract and write its stub "
        "before the module's code imports it",
    )
    use.add_argument("module", help="the module that consumes the contract")
    use.add_argument("contract", help="a versioned name, e.g. tenancy.membership.v1")
    consumer = generator(
        "consumer",
        "append a view of another module's event and an @on handler to the "
        "module's consumers.py",
    )
    consumer.add_argument("module", help="the module that consumes the event")
    consumer.add_argument(
        "channel",
        help="the versioned channel to consume, e.g. notes.note.created.v1",
    )
    consumer.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME:TYPE",
        help=f"a field of the view, as name:type with type one of {types}; "
        "repeat it for each field (default: the producer's own fields, read "
        "from its class or its exported schema)",
    )
    saga = generator(
        "saga", "a saga with a typed command step, a stand-in contract and tests"
    )
    saga.add_argument("module", help="the module that owns the saga")
    saga.add_argument("name", help="the saga's name (snake_case)")
    projection = generator(
        "projection",
        "a projection of the module's <name>.saved and <name>.deleted events, "
        "its row model and the migration that creates its table (the rows "
        "always carry tenant_id)",
    )
    projection.add_argument("module", help="the module that owns the projection")
    projection.add_argument("name", help="the projection's name (snake_case)")
    task = generator(
        "task",
        "an idempotent background task in the module's tasks.py, or with "
        "--periodic a task on a beat schedule",
    )
    task.add_argument("module", help="the module that owns the task")
    task.add_argument("name", help="the task's function name (snake_case)")
    task.add_argument(
        "--periodic",
        default=None,
        metavar="CRON",
        help='a beat schedule, e.g. "0 3 * * *" (minute hour dom month dow)',
    )
    # Listed for --help only: main() hands ``deploy`` to its own parser.
    sub.add_parser(
        "deploy",
        add_help=False,
        help="split the app into services (`sandloom add deploy --help`)",
    )
    sub.add_parser(
        "stack",
        help="write the stack's own lines (compose blocks, urls) that `sandloom "
        "new` left for the app's environment: needs the stack's package "
        "(`uv sync` installs it)",
        epilog=f"example: {_EXAMPLES['stack']}",
    )
    skills = sub.add_parser(
        "skills",
        help="write the app's agent skills (.claude/skills, .agents/skills)",
        epilog=f"example: {_EXAMPLES['skills']}",
    )
    skills.add_argument(
        "--force", action="store_true", help="overwrite a skill file you edited"
    )
    skills.add_argument(
        "--agents",
        default=None,
        help="comma-separated coding agents to install for: "
        + ", ".join(a.key for a in _skills.CODING_AGENTS)
        + " (default: the application's recorded choice)",
    )
    skills.add_argument(
        "--choose",
        action="store_true",
        help="ask again which agents to install for (at a terminal)",
    )
    return parser


def add_skills(
    root: Path,
    where: Layout,
    *,
    force: bool = False,
    agents: Sequence[str] | None = None,
) -> list[Path]:
    """``sandloom add skills``: the app's agent files (``sandloom new`` writes them
    too) for ``agents`` (default: the recorded choice): the skills in each
    agent's folder and the module-author contract. A file the framework wrote
    is updated; a file you edited is kept unless ``force``."""
    app = where.modules_package.split(".", 1)[0]
    if not app or where.framework_tree:
        raise GeneratorError(
            "the framework tree has its own skills (.claude/skills); "
            "`sandloom add skills` is for an `sandloom new` application"
        )
    if agents is None:
        agents = _skills.load_record(root)[0] or list(_skills.DEFAULT_AGENTS)
    modules = where.modules_dir.resolve().relative_to(root.resolve()).as_posix()
    written, kept = _skills.write_skills(
        root, app, agents, modules=modules, force=force
    )
    if kept:
        names = ", ".join(str(p.relative_to(root)) for p in kept)
        updated = f"wrote {len(written)} file(s), " if written else ""
        raise GeneratorError(
            f"{updated}kept {names}: you edited it; `sandloom add skills --force` "
            "replaces it with the framework's version"
        )
    return written


def add_stack(root: Path) -> int:
    """``sandloom add stack``: fill the tokens ``sandloom new`` left in the app's
    files (it ran without the stack's package) from the package installed in
    this environment: the stack the app records (``[tool.sandloom] stack``, the
    default one where none is). One line and a status; nothing changes when
    the app is complete."""
    try:
        driver = _stack.stack_choice(_stack_files.recorded(root)).driver
        written = _stack_files.complete(root, _stack.load(driver))
    except (_stack.StackServiceError, OSError) as exc:
        print(f"sandloom add stack: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"  {path}")
    print(
        f"stack files written ({len(written)})"
        if written
        else "the stack's files are already written"
    )
    return 0


def _run(args: argparse.Namespace) -> tuple[list[Path], str | None]:
    where = layout(
        args.root,
        tenant_scoped=not (args.what in ("module", "event") and args.no_tenant),
    )
    if args.what == "skills":
        agents = _skills.choose_agents(
            args.root,
            args.agents,
            interactive=_skills.stdin_is_interactive(),
            reask=args.choose,
        )
        return add_skills(args.root, where, force=args.force, agents=agents), None
    if args.what == "task":
        return add_task(args.module, args.name, where, periodic=args.periodic)
    if args.what == "module":
        return (
            add_module(
                args.module,
                where,
                with_settings=args.settings,
                entity=args.entity,
                empty=args.empty,
            ),
            None,
        )
    if args.what == "event":
        return add_event(args.module, args.name, where, args.field), None
    if args.what == "contract":
        return add_contract(args.module, args.name, where), None
    if args.what == "contract-use":
        return add_contract_use(args.module, args.contract, where)
    if args.what == "consumer":
        return add_consumer(args.module, args.channel, where, args.field), None
    if args.what == "saga":
        return add_saga(args.module, args.name, where), None
    return add_projection(args.module, args.name, where), None


def main(argv: list[str] | None = None) -> int:
    rest = list(sys.argv[1:] if argv is None else argv)
    if rest[:1] == ["deploy"]:  # its own argument parser (sandloom add deploy --help)
        return _deploy.main(rest[1:])
    parser = _parser()
    args, extra = parser.parse_known_args(rest)
    if args.what == "deploy":  # after --root: sandloom add --root . deploy …
        return _deploy.main(extra)
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if args.what == "stack":  # no module to generate, no `sandloom generate` after
        return add_stack(args.root)
    return _generate_and_report(args)


def _generate_and_report(args: argparse.Namespace) -> int:
    try:
        created, note = _run(args)
    except GeneratorError as exc:
        print(f"sandloom add: {exc}", file=sys.stderr)
        return 1
    for path in created:
        print(f"  {path}")
    if args.what == "skills":
        record = _skills.load_record(args.root)[0] or []
        print(f"agents: {', '.join(record)} ({_skills.RECORD})")
        print("" if created else "the app's skills are up to date")
        return 0
    if any(p.name.endswith(".new") for p in created):
        print("\nAn existing file was kept; merge the *.new file by hand.")
    if note:
        print(f"\n{note}")
    code, output = run_generate(args.root, *draft_options(created_names(args)))
    print(output.rstrip())
    if code != 0:
        print(
            "\nsandloom add: the files above were written, but `sandloom generate` "
            "failed: fix what it reports, then run `uv run sandloom generate`.",
            file=sys.stderr,
        )
        return code
    print("\nNext: `sandloom test` (the module's tests and every gate).")
    return 0


__all__ = [
    "FIELD_TYPES",
    "GeneratorError",
    "Layout",
    "add_consumer",
    "add_contract",
    "add_contract_use",
    "add_event",
    "add_module",
    "add_projection",
    "add_saga",
    "add_skills",
    "add_task",
    "draft_options",
    "event_channel",
    "layout",
    "main",
    "own_modules",
    "parse_fields",
    "run_generate",
]
