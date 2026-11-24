"""The names and checks of the revisions ``sandloom migrate`` / ``add`` write.

Standard library only: ``sandloom add`` imports it and stays free of the database
stack.

* :func:`next_revision` numbers the next revision of a chain (``0002_<module>``,
  the file ``0002_<module>_<slug>.py``), the way the baseline (``0001_<module>``)
  and the framework's own chains are named, instead of a random hash.
* :func:`duplicate_revision_problem` refuses a chain in which two files declare one
  revision id (two branches that each added ``0002_<module>``).
* :func:`not_null_without_default` finds ``op.add_column`` of a ``NOT NULL`` column
  with no ``server_default``: autogenerate writes it for a model column that has
  only a Python ``default=``, and it fails on a table that already has rows while
  the drift gate (models against the chain) passes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

#: ``0002_orders_add_status.py``: a revision file of the numbered convention.
_NUMBERED = re.compile(r"^(\d{1,4})_")

#: The import line of a revision that calls ``enable_tenant_rls`` (an advanced
#: ``sandloom.sdk`` name, so the marker T009 asks for).
RLS_IMPORT = (
    "from sandloom.sdk.data import disable_tenant_rls, enable_tenant_rls  "
    "# sandloom: advanced RLS"
)


def next_revision(versions: Path, key: str) -> str:
    """The id of the next revision of the chain ``key`` kept in ``versions``:
    ``0002_<key>`` after ``0001_<key>``. A chain with no numbered file counts its
    files (a chain written with hash ids)."""
    files = sorted(versions.glob("*.py")) if versions.is_dir() else []
    numbers = [int(m.group(1)) for f in files if (m := _NUMBERED.match(f.name))]
    number = (max(numbers) if numbers else len(files)) + 1
    return f"{number:04d}_{key}"


def _constant(node: ast.expr | None) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _declared_revision(path: Path) -> str | None:
    """The string literal a revision file assigns to ``revision`` at module level,
    read from its syntax tree (the file is not imported); ``None`` for a file that
    declares none (``__init__.py``)."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        value = _constant(node.value)
        if "revision" in names and isinstance(value, str):
            return value
    return None


def duplicate_revision_problem(versions: Path, key: str) -> str | None:
    """The refusal for a chain in which two files declare one revision id, else
    ``None``. Numbered ids collide when two branches each add the next revision of
    one chain (both ``0002_orders``): Alembic keeps one file of the pair, so
    ``upgrade head`` raises ``MultipleHeads`` (``alembic merge`` cannot fix it) or,
    worse, a database already at the other branch's revision counts as at head and
    this branch's change never runs. The message says which file to renumber to
    which id: the later files by name, numbered after the highest of the chain."""
    if not versions.is_dir():
        return None
    declared: dict[str, list[Path]] = {}
    for path in sorted(versions.glob("*.py")):
        revision = _declared_revision(path)
        if revision is not None:
            declared.setdefault(revision, []).append(path)
    number = int(next_revision(versions, key).split("_")[0])
    problems: list[str] = []
    for revision, files in declared.items():
        if len(files) < 2:
            continue
        names = ", ".join(f.name for f in files)
        follows = revision
        steps = []
        for path in files[1:]:
            new = f"{number:04d}_{key}"
            steps.append(
                f"renumber {path.name} to {new} (rename the file, set "
                f'revision = "{new}" and down_revision = "{follows}")'
            )
            follows = new
            number += 1
        problems.append(
            f"the migration chain {key!r} declares revision id {revision!r} in "
            f"{len(files)} files ({names}): two branches each added the next "
            "revision, and Alembic keeps only one of them. " + "; ".join(steps)
        )
    return "\n".join(problems) if problems else None


def _not_null_columns(call: ast.Call) -> list[str]:
    """``table.column`` of a ``NOT NULL`` column with no ``server_default``
    that ``call`` (``op.add_column(table, sa.Column(...))``) adds."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "add_column"):
        return []
    table = _constant(call.args[0]) if len(call.args) > 1 else None
    out = []
    for arg in call.args:
        if not (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Attribute)
            and arg.func.attr == "Column"
        ):
            continue
        keywords = {k.arg: k.value for k in arg.keywords}
        name = _constant(arg.args[0]) if arg.args else None
        if _constant(keywords.get("nullable")) is False and (
            "server_default" not in keywords
        ):
            out.append(f"{table}.{name}" if isinstance(table, str) else f"{name}")
    return out


def not_null_without_default(path: Path) -> list[str]:
    """``table.column`` of every ``NOT NULL`` column the revision ``path`` adds
    with no ``server_default`` (a SQL default, not a Python ``default=``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    out += _not_null_columns(call)
    return out


def not_null_warning(column: str) -> str:
    """The one-line warning for ``column`` (a ``table.column`` of
    :func:`not_null_without_default`)."""
    return (
        f"{column} is added NOT NULL with no server_default: it fails when the "
        "table already has rows, and a Python default= is not a database default. "
        "Give the column a server_default= (in the model too), or add it nullable, "
        "backfill, then make it NOT NULL "
        "(docs: Zero-downtime migrations, expand and contract)"
    )


__all__ = [
    "RLS_IMPORT",
    "duplicate_revision_problem",
    "next_revision",
    "not_null_warning",
    "not_null_without_default",
]
