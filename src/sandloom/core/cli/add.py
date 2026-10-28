"""``sandloom add`` — generators that write 1.0 code (Public API 1.0, spec §5.9).

    sandloom add module <m>                       a module (model, route, event, test)
    sandloom add event <m> <Name> [--field n:type ...]   append an event to
    <m>/events.py
    sandloom add contract <m> <name>              a provider in <m>/contracts.py and
    its DTO in <m>/schemas.py
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
- the code uses only the beginner tier of ``sandloom.sdk`` (SANDLOOM-T009); an
  advanced name carries ``# sandloom: advanced <reason>``.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sandloom.core.cli.scaffold_deploy as _deploy
import sandloom.core.cli.skills as _skills
import sandloom.core.cli.tool_process as _tool_process
from sandloom.core.archcheck import api10_rules
from sandloom.core.archcheck.manifest import ManifestError, load_rules
from sandloom.core.cli.scaffold_wiring import (
    FRAMEWORK_TREE_CONFTEST,
    PERIODIC_HOOK,
    contract_inherited,
    module_contract,
    project_root,
    wire_module,
)

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
#: ruff format's line length (generated files pass the project's format gate).
_LINE = 88
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
    shared_dir: Path
    shared_package: str
    source_root: Path

    def module_dir(self, module: str) -> Path:
        return self.modules_dir / module

    def dotted(self, module: str) -> str:
        return f"{self.modules_package}.{module}" if self.modules_package else module

    @property
    def framework_tree(self) -> bool:
        """The framework checkout (top-level modules next to ``sandloom``)."""
        return not self.modules_package and (self.source_root / "sandloom").is_dir()


def layout(root: Path) -> Layout:
    """The layout of the project at ``root`` (its ``architecture_rules.yml``)."""
    try:
        rules = load_rules(root / "architecture_rules.yml")
    except ManifestError as exc:
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


def _call(head: str, args: str) -> str:
    """``head(args)`` laid out the way ruff format does (one line when it
    fits, else the arguments on their own line), whatever the module name."""
    one = f"{head}({args})"
    if len(one) <= _LINE:
        return one
    indent = head[: len(head) - len(head.lstrip())]
    if len(f"{indent}    {args}") <= _LINE:
        return f"{head}(\n{indent}    {args}\n{indent})"
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
    return f"{head}(\n{lines}{indent})"


def _module_files(name: str) -> dict[str, str]:
    cls = camel(name)
    item = f"{cls}Item"
    files: dict[str, str] = {}
    files["__init__.py"] = f'''"""The {name} module.

Everything it declares is discovered: routes in ``api.py``, events in
``events.py``, tables in ``models/``. It talks to other modules only through
events and contracts.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
'''
    files["models/__init__.py"] = f'''"""The {name} tables.

Every module of this package is imported, so a new ``models/<x>.py`` joins
the module's tables and its migration chain. Table names start with
``{name}_``; rows of other modules are referenced by UUID, never by a
foreign key.
"""

import importlib
import pkgutil

for _info in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{{__name__}}.{{_info.name}}")
'''
    files["models/item.py"] = f'''"""An example table: replace it with your domain."""

import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id


class {item}(TenantScoped, Base):
    """``TenantScoped``: rows belong to the bound tenant and are filtered by it."""

    __tablename__ = "{name}_items"

    # new_id: a time-ordered UUIDv7; CreatedAt: clock_timestamp(), not now()
    # (the transaction's start), so rows of one transaction never tie.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    owner_id: Mapped[uuid.UUID] = mapped_column(index=True)
    created_at: Mapped[CreatedAt]
'''
    files["schemas.py"] = '''"""Request and response shapes (pydantic only)."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ItemIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    owner_id: UUID
'''
    files["events.py"] = f'''"""Events of the {name} module (its own channels)."""

from uuid import UUID

from sandloom.sdk import Event


class ItemCreated(Event, channel="{name}.item.created.v1"):
    item_id: UUID
    tenant_id: UUID
'''
    files[
        "services.py"
    ] = f'''"""The {name} use cases. ``db()`` is the session of the running unit."""

from uuid import UUID

import sqlalchemy as sa

from sandloom.sdk import db

from .events import ItemCreated
from .models.item import {item}
from .schemas import ItemIn, ItemOut


async def create_item(body: ItemIn, *, owner_id: UUID) -> ItemOut:
    item = {item}(title=body.title, owner_id=owner_id)
    db().add(item)
    await db().flush()
    await ItemCreated(item_id=item.id, tenant_id=item.tenant_id).publish()
    return ItemOut.model_validate(item)


async def get_item(item_id: UUID) -> ItemOut | None:
    item = await db().get({item}, item_id)
    return None if item is None else ItemOut.model_validate(item)


async def list_items(*, limit: int) -> list[ItemOut]:
    """The newest items first: ``created_at`` (the database clock), then the
    UUIDv7 ``id`` (creation order) for rows stamped in the same microsecond.
    ``TenantScoped`` keeps them to the bound tenant."""
    newest = {item}.created_at.desc()
    tie = {item}.id.desc()
    query = sa.select({item}).order_by(newest, tie).limit(limit)
    rows = await db().scalars(query)
    return [ItemOut.model_validate(row) for row in rows]
'''
    files["api.py"] = f'''"""Routes of the {name} module: ``/api/v1/{name}/...``.

``module_router()`` authenticates every route and binds the tenant.
"""

from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, Query

from sandloom.sdk import CurrentUser, module_router

from . import services
from .schemas import ItemIn, ItemOut

router = module_router()


@router.post("/items", status_code=201)
async def create_item(body: ItemIn, user: CurrentUser) -> ItemOut:
    return await services.create_item(body, owner_id=user.user_id)


@router.get("/items")
async def list_items(
    user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[ItemOut]:
    """A page of the tenant's items: ``le=`` caps what one request may load."""
    return await services.list_items(limit=limit)


@router.get("/items/{{item_id}}")
async def get_item(item_id: UUID, user: CurrentUser) -> ItemOut:
    item = await services.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return item
'''
    files[
        "migrations/env.py"
    ] = '''"""The module's own migration chain (tables of ``models/``)."""

from sandloom.sdk.migrations import run_module_migrations  # sandloom: advanced chain

run_module_migrations(__file__)
'''
    files["migrations/script.py.mako"] = _MAKO
    files[f"migrations/versions/0001_{name}_baseline.py"] = _baseline(name)
    files["tests/__init__.py"] = ""
    items = f'"/api/v1/{name}/items"'
    create = _call(
        "    created = await client.post",
        f'{items}, json={{"title": "first"}}, headers=headers',
    )
    fetch = _call(
        "    fetched = await client.get",
        f"f\"/api/v1/{name}/items/{{item['id']}}\", headers=headers",
    )
    anonymous = _call(
        "    response = await client.post", f'{items}, json={{"title": "x"}}'
    )
    add_one = _call(
        "        created = await client.post",
        f'{items}, json={{"title": title}}, headers=headers',
    )
    listed = _call("    listed = await client.get", f"{items}, headers=headers")
    page = _call(
        "    page = await client.get",
        f'{items}, params={{"limit": 2}}, headers=headers',
    )
    foreign = _call("    foreign = await client.get", f"{items}, headers=other")
    over_cap = _call(
        "    over_cap = await client.get",
        f'{items}, params={{"limit": 101}}, headers=headers',
    )
    files[
        f"tests/test_{name}.py"
    ] = f'''"""Tests of the {name} module (sandloom.sdk.testing fixtures)."""

from typing import Any
from uuid import uuid4

from sandloom.sdk.testing import as_user, published

from ..events import ItemCreated


async def test_create_and_read_an_item(client: Any) -> None:
    _, headers = as_user(tenant_id=uuid4())
{create}
    assert created.status_code == 201, created.text
    assert len(published(ItemCreated)) == 1
    item = created.json()
{fetch}
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "first"


async def test_list_items_is_capped_and_tenant_scoped(client: Any) -> None:
    _, headers = as_user(tenant_id=uuid4())
    _, other = as_user(tenant_id=uuid4())
    for title in ("a", "b", "c"):
{add_one}
        assert created.status_code == 201, created.text
{listed}
    assert listed.status_code == 200
    # Newest first, deterministically: all three rows share one transaction.
    assert [item["title"] for item in listed.json()] == ["c", "b", "a"]
{page}
    assert len(page.json()) == 2
{foreign}
    assert foreign.json() == []
{over_cap}
    assert over_cap.status_code == 422


async def test_items_need_an_authenticated_user(client: Any) -> None:
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

_RLS_IMPORT = (
    "from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  "
    "# sandloom: advanced RLS"
)


def _rls_block(table: str, *, system_access: bool = False) -> tuple[str, str]:
    """The upgrade / downgrade lines of a ``TenantScoped`` table (spec §5.6)."""
    extra = ", system_access=True" if system_access else ""
    guard = 'if op.get_context().config.attributes.get("sandloom_tenant_rls", False):'
    quoted = json.dumps(table)
    return (
        f"    {guard}\n        enable_tenant_rls(op, {quoted}{extra})\n",
        f"    {guard}\n        disable_tenant_rls(op, {quoted})\n",
    )


def _baseline(name: str) -> str:
    table = f"{name}_items"
    up_rls, down_rls = _rls_block(table)
    return f'''"""{name} baseline: the example table (rendered by `sandloom add module`)

Revision ID: 0001_{name}
Revises:
Create Date: generated

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

{_RLS_IMPORT}

revision: str = "0001_{name}"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "{table}",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
    op.create_index(op.f("ix_{table}_tenant_id"), "{table}", ["tenant_id"])
    op.create_index(op.f("ix_{table}_owner_id"), "{table}", ["owner_id"])
    # The runner sets sandloom_tenant_rls from the target database's INFRA config.
{up_rls}

def downgrade() -> None:
{down_rls}    op.drop_index(op.f("ix_{table}_owner_id"), table_name="{table}")
    op.drop_index(op.f("ix_{table}_tenant_id"), table_name="{table}")
    op.drop_table("{table}")
'''


def add_module(name: str, where: Layout) -> list[Path]:
    """``sandloom add module <name>``: a new package; refuses an existing one.

    Also wires it where its project loads modules (``wire_module``: the
    framework tree's ``LOCAL_MODULES``, an ``SANDLOOM_MODULES`` list in the env
    files and the monolith profile; an ``sandloom new`` app discovers its modules
    and lists nothing). In the framework tree, whose suite does not activate
    the ``sandloom.sdk.testing`` plugin, the module gets a one-line
    ``tests/conftest.py`` with the kit's fixtures. Returns created and edited
    files.
    """
    _check_name(name, "the module name")
    target = where.module_dir(name)
    if target.exists():
        raise GeneratorError(f"{target} already exists")
    files = _module_files(name)
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


def add_event(module: str, name: str, where: Layout, fields: list[str]) -> list[Path]:
    """``sandloom add event <m> <Name>``: append a producer class to ``events.py``."""
    directory = _require_module(where, module)
    if not _CLASS.fullmatch(name):
        raise GeneratorError(f"the event name must be CamelCase: {name!r}")
    parsed = parse_fields(fields) or [
        (f"{_snake(name).split('_')[0]}_id", "UUID", "from uuid import UUID"),
    ]
    if not any(f[0] == "tenant_id" for f in parsed):
        parsed.append(("tenant_id", "UUID", "from uuid import UUID"))
    channel = f"{module}.{_snake(name).replace('_', '.', 1)}.v1"
    if channel.count(".") < 2:
        channel = f"{module}.{_snake(name)}.v1"
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
    for directory in (where.shared_dir, where.shared_dir / "contracts"):
        init = directory / "__init__.py"
        if not init.exists():
            _write(
                init, '"""Contract DTO package (generated stubs and DTO copies)."""\n'
            )
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
        dto_package=f"{where.shared_package}.contracts.{dto_stem}",
        shared_packages=("sandloom.shared", where.shared_package),
    )
    contracts_dir = where.shared_dir / "contracts"
    (contracts_dir / f"{stem}.py").write_text(
        stubs.render_stub(contract, provider, resolver), encoding="utf-8"
    )
    for other in vars(module_contracts).values():
        spec = getattr(other, "__dict__", {}).get("__sandloom_contract__")
        if spec is not None and other is not provider:
            stubs.render_stub(spec.name, other, resolver)  # its DTOs too
    target = contracts_dir / f"{dto_stem}.py"
    if resolver.copied:
        text = copies.render_module(module, resolver)
        target.write_text(
            copies.ruff_formatted(text, target, cwd=where.source_root),
            encoding="utf-8",
        )


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
root = Path(sys.argv[1])
load_project_test_env(root)
from sandloom.core.cli.generated import main
sys.exit(main(["--root", str(root)]))
"""


def run_generate(root: Path) -> tuple[int, str]:
    """``sandloom generate`` for the project at ``root``, in a child process
    of this interpreter (a fresh import of the project's files); returns
    ``(exit code, output)``."""
    proc = _tool_process.run_tool(
        [sys.executable, "-c", _GENERATE, str(root.resolve())],
        cwd=root,
        capture=True,
    )
    return proc.returncode, proc.stdout or ""


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
    exported = where.shared_dir / "events" / f"{channel}.json"
    if exported.is_file():
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
            f"no producer schema for {channel!r} in this tree or "
            f"{where.shared_dir / 'events'}: pass the fields (--field name:type ...); "
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
    block = (
        "\n".join(lines)
        + f"\n\n\n@on({cls})\nasync def {handler}(event: {cls}) -> None:\n"
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
    return [_append(path, block, header=header)]


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
                "from sandloom.sdk.sagas import Saga, local_step, step  "
                "# sandloom: advanced sagas",
                f"from {where.shared_package}.contracts.{stem} import {ref}",
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


async def _confirm(session: Any, ctx: Any) -> dict[str, str]:
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

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, TenantScoped


class {cls}Row(TenantScoped, Base):
    __tablename__ = "{table}"

    {name}_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
'''
    projection = f'''"""The {module}.{name} projection (spec §6.4).

Full-state events of one aggregate, versioned tombstones, one owning group
per column.
"""

from uuid import UUID

from sandloom.sdk import Event
from sandloom.sdk.projections import (  # sandloom: advanced projections
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
    revision = f"{module}_{name}_projection"[:32]
    up_rls, down_rls = _rls_block(table)
    migration = f'''"""{module}.{name} projection table (by `sandloom add projection`)

Revision ID: {revision}
Revises: {head or ""}
Create Date: generated

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

{_RLS_IMPORT}

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
        _write(versions / f"{revision}.py", migration),
    ]
    return created


# --------------------------------------------------------------------------- #
# sandloom add task
# --------------------------------------------------------------------------- #

_TASK_IMPORTS = [
    "from sandloom.sdk.data import open_uow  # sandloom: advanced task unit",
    "from sandloom.sdk.effects import run_async  # sandloom: advanced task body",
    "from sandloom.sdk.tasks import shared_task  # sandloom: advanced tasks",
]


def _missing_imports(path: Path, lines: list[str]) -> list[str]:
    """The ``from x import name`` lines whose name ``path`` does not import."""
    if not path.exists():
        return []
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise GeneratorError(f"{path} does not parse ({exc}); fix it first") from exc
    imported = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    return [line for line in lines if line.split()[3] not in imported]


def _task_block(module: str, dotted: str, name: str) -> str:
    return f'''async def _{name}() -> int:
    """The work of one run; a redelivered or overlapping run must be harmless
    (delete by a condition, upsert, or claim a durable_inbox key)."""
    async with open_uow():
        ...  # db(), await Event(...).publish() and record_audit work here
    return 0


@shared_task(
    name="{dotted}.tasks.{name}", acks_late=True, reject_on_worker_lost=True
)
def {name}() -> int:
    """At-least-once (acks_late): Celery redelivers it if a worker dies."""
    return run_async(_{name}())
'''


def _periodic_line(module: str, name: str, schedule: str) -> str:
    args = f'cron("{schedule}"), {name}.s(), name="{module}.{name}"'
    one = f"    sender.add_periodic_task({args})"
    if len(one) <= 88:  # ruff format's line length
        return one + "\n"
    return f"    sender.add_periodic_task(\n        {args}\n    )\n"


def _insert_stdlib_import(source: str, line: str) -> str:
    """``line`` after the module docstring / ``__future__`` imports."""
    tree = ast.parse(source)
    after = 0
    for node in tree.body:
        is_doc = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        future = isinstance(node, ast.ImportFrom) and node.module == "__future__"
        if not (is_doc or future):
            break
        after = node.end_lineno or node.lineno
    lines = source.split("\n")
    lines[after:after] = ["", line] if after else [line, ""]
    return "\n".join(lines)


def _add_periodic(path: Path, module: str, name: str, schedule: str) -> None:
    """Register ``name`` in the module's beat hook (created when absent)."""
    source = path.read_text(encoding="utf-8")
    first = source.split("from sandloom.sdk.tasks import", 1)
    if len(first) == 2 and "cron" not in first[1].split("\n", 1)[0]:
        source = source.replace(
            "from sandloom.sdk.tasks import shared_task",
            "from sandloom.sdk.tasks import cron, shared_task",
            1,
        )
    if "from typing import Any" not in source:
        source = _insert_stdlib_import(source, "from typing import Any")
    hook = next(
        (
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == PERIODIC_HOOK
        ),
        None,
    )
    line = _periodic_line(module, name, schedule)
    if hook is None:
        source = (
            source.rstrip("\n")
            + "\n\n\n"
            + f"def {PERIODIC_HOOK}(sender: Any, **kwargs: Any) -> None:\n"
            + '    """Beat hook: exactly one beat per deploy profile runs it '
            + '(SANDLOOM-T010)."""\n'
            + line
        )
    else:
        lines = source.split("\n")
        end = hook.end_lineno or hook.lineno
        lines[end:end] = line.rstrip("\n").split("\n")
        source = "\n".join(lines)
    path.write_text(source, encoding="utf-8")


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
    """``sandloom add task <m> <name> [--periodic "<cron>"]``: an idempotent,
    at-least-once task in ``tasks.py``; with ``--periodic`` its beat schedule
    and the deploy wiring. Returns the edited files and a note to print."""
    directory = _require_module(where, module)
    _check_name(name, "the task name")
    if periodic is not None and len(periodic.split()) != 5:
        raise GeneratorError(
            f"--periodic {periodic!r}: expected 5 cron fields "
            "'minute hour day-of-month month day-of-week', e.g. '0 3 * * *'"
        )
    path = directory / "tasks.py"
    if path.exists() and re.search(rf"^def {name}\(", path.read_text(), re.M):
        raise GeneratorError(f"{path} already defines {name}()")
    missing = _missing_imports(path, _TASK_IMPORTS)
    if missing:  # an existing tasks.py: add them after its last import
        source = path.read_text(encoding="utf-8")
        imports = [
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.Import | ast.ImportFrom)
        ]
        lines = source.split("\n")
        at = (imports[-1].end_lineno or 0) if imports else 0
        lines[at:at] = missing
        path.write_text("\n".join(lines), encoding="utf-8")
    header = (
        f'"""Background tasks of the {module} module (the framework worker '
        "imports this file).\n\nTask bodies are sync: async work runs through "
        '``run_async`` in ``open_uow()`` blocks."""\n\n'
        "from sandloom.sdk.data import open_uow  # sandloom: advanced task unit\n"
        "from sandloom.sdk.effects import run_async  # sandloom: advanced task body\n"
        "from sandloom.sdk.tasks import shared_task  # sandloom: advanced tasks\n\n\n"
    )
    block = _task_block(module, where.dotted(module), name)
    edited = [_append(path, block, header=header)]
    if periodic is None:
        return edited, None
    _add_periodic(path, module, name, periodic)
    wired, note = _wire_beat(where, module)
    return edited + wired, note


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandloom add", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="project root")
    sub = parser.add_subparsers(dest="what", required=True)
    sub.add_parser("module").add_argument("module")
    event = sub.add_parser("event")
    event.add_argument("module")
    event.add_argument("name")
    event.add_argument("--field", action="append", default=[])
    contract = sub.add_parser("contract")
    contract.add_argument("module")
    contract.add_argument("name")
    consumer = sub.add_parser("consumer")
    consumer.add_argument("module")
    consumer.add_argument("channel")
    consumer.add_argument("--field", action="append", default=[])
    for kind in ("saga", "projection"):
        p = sub.add_parser(kind)
        p.add_argument("module")
        p.add_argument("name")
    task = sub.add_parser("task")
    task.add_argument("module")
    task.add_argument("name")
    task.add_argument(
        "--periodic",
        default=None,
        metavar="CRON",
        help='a beat schedule, e.g. "0 3 * * *" (minute hour dom month dow)',
    )
    skills = sub.add_parser(
        "skills",
        help="write the app's agent skills (.claude/skills, .agents/skills)",
    )
    skills.add_argument(
        "--force", action="store_true", help="overwrite a skill file you edited"
    )
    return parser


def add_skills(root: Path, where: Layout, *, force: bool = False) -> list[Path]:
    """``sandloom add skills``: the app's agent skills (``sandloom new`` writes them
    too); a skill file that differs is kept unless ``force``."""
    app = where.modules_package.split(".", 1)[0]
    if not app or where.framework_tree:
        raise GeneratorError(
            "the framework tree has its own skills (.claude/skills); "
            "`sandloom add skills` is for an `sandloom new` application"
        )
    written, kept = _skills.write_skills(root, app, force=force)
    if kept:
        names = ", ".join(str(p.relative_to(root)) for p in kept)
        raise GeneratorError(
            f"kept {names}: it differs from the framework's version; "
            "`sandloom add skills --force` overwrites it"
        )
    return written


def _run(args: argparse.Namespace) -> tuple[list[Path], str | None]:
    where = layout(args.root)
    if args.what == "skills":
        return add_skills(args.root, where, force=args.force), None
    if args.what == "task":
        return add_task(args.module, args.name, where, periodic=args.periodic)
    if args.what == "module":
        return add_module(args.module, where), None
    if args.what == "event":
        return add_event(args.module, args.name, where, args.field), None
    if args.what == "contract":
        return add_contract(args.module, args.name, where), None
    if args.what == "consumer":
        return add_consumer(args.module, args.channel, where, args.field), None
    if args.what == "saga":
        return add_saga(args.module, args.name, where), None
    return add_projection(args.module, args.name, where), None


def main(argv: list[str] | None = None) -> int:
    rest = list(sys.argv[1:] if argv is None else argv)
    if rest[:1] == ["deploy"]:  # its own argument parser (sandloom add deploy --help)
        return _deploy.main(rest[1:])
    args = _parser().parse_args(rest)
    try:
        created, note = _run(args)
    except GeneratorError as exc:
        print(f"sandloom add: {exc}", file=sys.stderr)
        return 1
    for path in created:
        print(f"  {path}")
    if args.what == "skills":
        print("" if created else "the app's skills are up to date")
        return 0
    if any(p.name.endswith(".new") for p in created):
        print("\nAn existing file was kept; merge the *.new file by hand.")
    if note:
        print(f"\n{note}")
    code, output = run_generate(args.root)
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
    "add_event",
    "add_module",
    "add_projection",
    "add_saga",
    "add_skills",
    "add_task",
    "layout",
    "main",
    "parse_fields",
    "run_generate",
]
