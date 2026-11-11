"""SANDLOOM-C034 sees every way to build a hand-written contract client.

The W5+W6 security review (M1) found spellings the ``handwritten-access-check``
analysis missed: a ContractRef reached through a module alias, a lowercase or
star re-export or a local alias; a client built under an annotation, inline,
in a function or a class; and any class carrying the ``__sandloom_atom__``
literal was exempt. Each case of ``_fixtures/c034/bypasses.py.txt`` is one
module that must be reported; only a class in a ``_generated/`` package is
exempt. The low finding (an import overriding a later local class, silencing
SANDLOOM-C022) is pinned here too.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandloom.core.archcheck import api10_rules as a10
from sandloom.core.archcheck import atom_metadata

_FIXTURES = Path(__file__).parent / "_fixtures"
_STUB = "c034app._generated.contracts.projects_roles_v1"
_PLAIN_STUB = """
from typing import Protocol

from sandloom.sdk.contracts import ContractRef


class PlainV1(Protocol):
    async def has_project_role(self, organization_id, user_id, level) -> bool: ...


PLAIN_V1: ContractRef[PlainV1] = ContractRef("plain.v1", PlainV1)
"""


def _sections(name: str, marker: str) -> dict[str, str]:
    text = (_FIXTURES / "c034" / name).read_text(encoding="utf-8")
    parts = re.split(rf"^# === {marker}: (\w+)\n", text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


_CASES = _sections("bypasses.py.txt", "case")


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """``c034app`` on ``sys.path``: the Z-8 generated stub in its
    ``_generated/contracts`` package and the helper modules."""
    root = tmp_path / "c034app"
    contracts = root / "_generated" / "contracts"
    contracts.mkdir(parents=True)
    for package in (root, root / "_generated", contracts):
        (package / "__init__.py").write_text("")
    stub = (_FIXTURES / "z8_stub.py.txt").read_text(encoding="utf-8")
    (contracts / "projects_roles_v1.py").write_text(stub, encoding="utf-8")
    (root / "plain_stub.py").write_text(_PLAIN_STUB, encoding="utf-8")
    for name, source in _sections("helpers.py.txt", "module").items():
        (root / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    atom_metadata.clear_caches()
    yield root
    atom_metadata.clear_caches()


def _hits(root: Path, name: str, source: str) -> list[str]:
    path = root / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return a10.find_handwritten_access_checks(path)


def test_every_case_of_the_review_is_a_fixture() -> None:
    """The probe's spellings (security review M1) each have a case."""
    assert {
        "baseline",
        "module_alias",
        "annotated_assignment",
        "inline_client",
        "client_in_function",
        "client_on_self",
        "lowercase_reexport",
        "star_reexport",
        "ref_variable_alias",
        "forged_atom_literal",
    } <= set(_CASES)


@pytest.mark.parametrize("case", sorted(_CASES))
def test_c034_reports_the_bypass(app: Path, case: str) -> None:
    hits = _hits(app, f"case_{case}", _CASES[case])
    assert hits, f"{case}: the hand-written check is not reported"
    assert all(
        h.startswith("Line ")
        and ": Hand calls the declared access check projects.roles.v1.has_project_role"
        in h
        for h in hits
    ), hits
    assert any("without stating tier (declared 3)" in h for h in hits), hits
    assert any("does not state its declared risk ['admin']" in h for h in hits), hits


def test_c034_controls_are_not_reported(app: Path) -> None:
    """Declared metadata stated, a delegating class, a contract with no
    declared access check and a service function: nothing to report."""
    source = (_FIXTURES / "c034" / "controls.py.txt").read_text(encoding="utf-8")
    assert _hits(app, "controls", source) == []


def test_c034_only_a_generated_package_exempts_a_literal(app: Path) -> None:
    """The literal exempts a class only in a ``_generated/`` package: the same
    module is reported elsewhere (``forged_atom_literal``) and not there."""
    source = _CASES["forged_atom_literal"]
    generated = app / "_generated" / "contracts"
    assert _hits(generated, "forged", source) == []
    assert _hits(app, "forged", source)


def test_c034_still_follows_an_uppercase_reexport(app: Path) -> None:
    """kb-34g: a renamed uppercase re-export of the ContractRef by a package."""
    (app / "_generated" / "contracts" / "__init__.py").write_text(
        "from .projects_roles_v1 import PROJECTS_ROLES_V1 as ROLES\n"
    )
    source = _CASES["baseline"].replace(
        f"from {_STUB} import PROJECTS_ROLES_V1",
        "from c034app._generated.contracts import ROLES as PROJECTS_ROLES_V1",
    )
    assert len(_hits(app, "upper", source)) == 3


def test_a_star_import_honours_all(app: Path) -> None:
    """A star re-export binds only what ``__all__`` lists: a ContractRef left
    out of it is no ref (no false report from a name that is not there)."""
    (app / "narrow.py").write_text(f"from {_STUB} import *\n", encoding="utf-8")
    stub = app / "_generated" / "contracts" / "projects_roles_v1.py"
    stub.write_text(
        stub.read_text(encoding="utf-8") + '\n__all__ = ["ProjectRole"]\n',
        encoding="utf-8",
    )
    source = _CASES["star_reexport"].replace("c034app.star", "c034app.narrow")
    assert _hits(app, "narrow_case", source) == []


# --------------------------------------------------------------------------- #
# the low finding: a later local definition shadows an import (C022)
# --------------------------------------------------------------------------- #
_SHADOW_ROUTE = """
from fastapi import APIRouter, Depends, HTTPException

from sandloom.sdk import require
from sandloom.sdk.authz import Requirement
{imports}

router = APIRouter()


@router.post("/orgs/{{org_id}}/x", dependencies=[Depends(require(ProjectRole("lead")))])
async def x(org_id):
    raise HTTPException(status_code=403)
"""


def test_a_later_local_class_shadows_an_imported_atom(app: Path) -> None:
    """``from stub import ProjectRole`` then a hand-written ``class
    ProjectRole``: the name is the local class, no atom, so its call is no
    admin gate and the route's hand-rolled authority is reported (C022)."""
    shadowing = f"from {_STUB} import ProjectRole\n\n\nclass ProjectRole(Requirement):\n    pass\n"
    tree = ast.parse(shadowing)
    literals = atom_metadata.atom_literals(tree, app / "shadow.py")
    assert "ProjectRole" not in literals
    call = ast.parse('ProjectRole("lead")').body[0]
    assert isinstance(call, ast.Expr)
    assert not atom_metadata.admin_gate([call.value], literals)
    assert not atom_metadata.member_gate([call.value], literals)

    route = app / "shadow_api.py"
    route.write_text(textwrap.dedent(_SHADOW_ROUTE.format(imports=shadowing)))
    hits = a10.find_untagged_sensitive(route)
    assert len(hits) == 1 and "POST /orgs/{org_id}/x" in hits[0], hits


def test_a_later_import_shadows_a_local_class(app: Path) -> None:
    """The other order: the import is the last binding, the atom is read."""
    imports = f"class ProjectRole(Requirement):\n    pass\n\n\nfrom {_STUB} import ProjectRole\n"
    literals = atom_metadata.atom_literals(ast.parse(imports), app / "x.py")
    assert literals["ProjectRole"]["method"] == "has_project_role"
    route = app / "ordered_api.py"
    route.write_text(textwrap.dedent(_SHADOW_ROUTE.format(imports=imports)))
    assert a10.find_untagged_sensitive(route) == []
