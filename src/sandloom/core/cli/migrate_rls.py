"""``sandloom migrate new``: a module revision with row-level security (spec §5.6).

``sandloom migrate new <module> "<message>"`` is the per-chain autogenerate of
``sandloom migrate revision <module> -m ... --autogenerate`` (the module's database
must be at its chain head), followed by this step: every ``op.create_table``
of a ``TenantScoped`` table in the new revision gets
``enable_tenant_rls(op, <table>)`` at the end of ``upgrade()`` and
``disable_tenant_rls(op, <table>)`` at the start of ``downgrade()``, under the
same ``sandloom_tenant_rls`` switch the runner sets from the database's INFRA
configuration (``system_access=True`` for a model with
``__system_access__ = True``).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from sandloom.core.orm import Base, TenantScoped

GUARD = 'if op.get_context().config.attributes.get("sandloom_tenant_rls", False):'
RLS_IMPORT = (
    "from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  "
    "# sandloom: advanced RLS"
)


def tenant_tables(package: str) -> dict[str, bool]:
    """``{table: system_access}`` of the ``TenantScoped`` models of ``package``."""
    out: dict[str, bool] = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        owner = getattr(cls, "__module__", "")
        if owner != package and not owner.startswith(package + "."):
            continue
        table = getattr(cls, "__tablename__", None)
        if isinstance(table, str) and issubclass(cls, TenantScoped):
            out[table] = bool(getattr(cls, "__system_access__", False))
    return out


def _created_tables(fn: ast.FunctionDef) -> list[str]:
    return [
        node.args[0].value
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_table"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]


def add_tenant_rls(path: Path, tables: dict[str, bool]) -> list[str]:
    """Add the RLS lines for the ``TenantScoped`` tables a revision creates.

    Returns the tables it covered (``[]``: the file is unchanged).
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    upgrade, downgrade = functions.get("upgrade"), functions.get("downgrade")
    if upgrade is None or downgrade is None:
        return []
    covered = [t for t in _created_tables(upgrade) if t in tables]
    if not covered:
        return []
    lines = source.splitlines()
    enable = []
    for table in covered:
        extra = ", system_access=True" if tables[table] else ""
        call = f"enable_tenant_rls(op, {json.dumps(table)}{extra})"
        enable += [f"    {GUARD}", f"        {call}"]
    disable = []
    for table in reversed(covered):
        call = f"disable_tenant_rls(op, {json.dumps(table)})"
        disable += [f"    {GUARD}", f"        {call}"]
    up_end = upgrade.end_lineno or upgrade.lineno
    down_start = downgrade.body[0].lineno
    if isinstance(downgrade.body[0], ast.Expr) and isinstance(
        downgrade.body[0].value, ast.Constant
    ):  # keep a docstring first
        down_start = (downgrade.body[0].end_lineno or down_start) + 1
    # Edit bottom-up so earlier line numbers stay valid.
    lines[down_start - 1 : down_start - 1] = disable
    lines[up_end:up_end] = enable
    text = "\n".join(lines) + "\n"
    if RLS_IMPORT not in text:
        anchor = "from alembic import op\n"
        if anchor in text:
            text = text.replace(anchor, f"{anchor}\n{RLS_IMPORT}\n", 1)
        else:
            text = f"{RLS_IMPORT}\n{text}"
    path.write_text(text, encoding="utf-8")
    return covered


__all__ = ["GUARD", "RLS_IMPORT", "add_tenant_rls", "tenant_tables"]
