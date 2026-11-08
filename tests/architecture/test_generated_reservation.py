"""``generated-reserved`` (SANDLOOM-C032): ``<module>/_generated/`` is the
generator's (ADR-0013 decision 1).

``sandloom check`` refuses a file in it the generator did not write (or wrote
and somebody edited), and an import of another module's ``_generated``
package; the module's own generated copies and imports of them pass.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from sandloom.core import error_codes
from sandloom.core.archcheck.generated_rules import find_reserved_generated_problems
from sandloom.core.cli import consumer_copies as cc
from sandloom.core.descriptors import generated_provenance as gp


def _write(root: Path, files: dict[str, str]) -> list[Path]:
    out = []
    for rel, src in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(src))
        out.append(path)
    return out


def _generated_tree(module: Path) -> None:
    """What ``sandloom generate`` writes: inits, a stub, an expectation."""
    contracts = module / "_generated" / "contracts"
    contracts.mkdir(parents=True)
    (module / "_generated" / "__init__.py").write_text(gp.INIT)
    (contracts / "__init__.py").write_text(gp.INIT)
    (contracts / "a_b_v1.py").write_text(
        cc.with_provenance("X = 1\n", ["contract: a.b.v1  fingerprint: f"])
    )
    doc = gp.signed_expectation({"contract": "a.b.v1", "generator_format": 2})
    (contracts / f"a_b_v1{gp.EXPECTATION_SUFFIX}").write_text(json.dumps(doc))


def _details(module: Path, files: list[Path] | None = None) -> list[str]:
    hits = find_reserved_generated_problems(module, module.name, files or [])
    return [detail for _, detail in hits]


def test_the_generators_own_files_pass(tmp_path: Path) -> None:
    module = tmp_path / "shop"
    _generated_tree(module)
    (module / "_generated" / "__pycache__").mkdir()
    (module / "_generated" / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\0")
    files = _write(
        module,
        {
            "__init__.py": "from ._generated.contracts.a_b_v1 import X\n",
            "api.py": "from shop._generated.contracts import a_b_v1\n",
        },
    )
    assert _details(module, files) == []


def test_a_hand_written_or_edited_file_is_refused(tmp_path: Path) -> None:
    module = tmp_path / "shop"
    _generated_tree(module)
    contracts = module / "_generated" / "contracts"
    (contracts / "helpers.py").write_text("def f() -> None: ...\n")
    (contracts / "notes.txt").write_text("mine\n")
    stub = contracts / "a_b_v1.py"
    stub.write_text(stub.read_text().replace("X = 1", "X = 2"))
    expectation = contracts / f"a_b_v1{gp.EXPECTATION_SUFFIX}"
    doc = json.loads(expectation.read_text())
    expectation.write_text(json.dumps({**doc, "contract": "a.c.v1"}))
    (module / "_generated" / "__init__.py").write_text('"""Mine."""\n')
    details = _details(module)
    assert len(details) == 5
    by_file = {d.split(" is reserved")[0]: d for d in details}
    assert sorted(by_file) == [
        "shop/_generated/__init__.py",
        "shop/_generated/contracts/a_b_v1.describe.json",
        "shop/_generated/contracts/a_b_v1.py",
        "shop/_generated/contracts/helpers.py",
        "shop/_generated/contracts/notes.txt",
    ]
    assert "hand-edited" in by_file["shop/_generated/contracts/a_b_v1.py"]
    assert "hand-edited" in by_file["shop/_generated/contracts/a_b_v1.describe.json"]
    assert "no provenance header" in by_file["shop/_generated/contracts/helpers.py"]
    assert "not a file" in by_file["shop/_generated/contracts/notes.txt"]


def test_an_import_of_another_modules_generated_package_is_refused(
    tmp_path: Path,
) -> None:
    module = tmp_path / "app" / "modules" / "shop"
    files = _write(
        module,
        {
            "__init__.py": "",
            "api.py": """
            import importlib
            import billing._generated.contracts.x_v1
            from billing._generated.contracts import x_v1
            from billing import _generated
            from ..billing._generated.contracts.x_v1 import X
            from app.modules.shop._generated.contracts import own_v1
            from ._generated.contracts.own_v1 import Y

            m = importlib.import_module("billing._generated.contracts.x_v1")
            """,
            "tests/test_x.py": "from app.modules.billing._generated import y\n",
        },
    )
    hits = find_reserved_generated_problems(module, "app.modules.shop", files)
    found = sorted((path.name, detail.split(":")[0]) for path, detail in hits)
    assert found == [
        ("api.py", "Line 10"),
        ("api.py", "Line 3"),
        ("api.py", "Line 4"),
        ("api.py", "Line 5"),
        ("api.py", "Line 6"),
        ("test_x.py", "Line 1"),
    ]
    details = [detail for _, detail in hits]
    assert "imports billing._generated, another module's" in details[0]
    assert any("imports app.modules.billing._generated" in d for d in details)


def test_the_rule_has_its_catalogue_entry() -> None:
    entry = error_codes.lookup("generated-reserved")
    assert entry is not None and entry.code == "SANDLOOM-C032"
    assert entry.kind == "check"
