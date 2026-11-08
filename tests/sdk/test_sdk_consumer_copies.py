"""Consumer copies owned by the consuming module (ADR-0013, KB-20).

A tree of two module distributions in a temporary source root: ``kbprov``
provides ``kbprov.quota.v1`` (a DTO, a closed DTO), ``kbshop`` consumes it.
The planner reads ``kbshop``'s declarations (AST, nothing composed), the
copies land in ``kbshop/_generated/contracts``; the fitness tests of the ADR:
byte-identical generation (hash seeds, orders, ruff absent, a foreign
``line-length``), the closed-DTO rule, provider absent (keep / edited /
missing), stem collisions, the planned set against the composed one.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli import consumer_copies as cc
from sandloom.core.cli import dto_copies, generated
from sandloom.core.contracts.compatibility import compare_contract

SCHEMAS = '''
    from pydantic import BaseModel, ConfigDict, Field


    class Quota(BaseModel):
        """A quota."""

        limit: int = Field(ge=0, description="the limit of this quota, never negative, in whole units of the plan")
        a_rather_long_field_name_for_the_layout: dict[str, list[int]] = Field(default_factory=dict)


    class Closed(BaseModel):
        model_config = ConfigDict(extra="forbid")

        n: int
'''

CONTRACTS = '''
    from sandloom.sdk import contract

    from .schemas import Closed, Quota


    @contract("{p}.quota.v1")
    class QuotaRead:
        async def get(self, org: str) -> Quota:
            """The quota."""
            return Quota(limit=1)

        async def closed(self) -> Closed:
            return Closed(n=1)

        async def unused(self) -> int:
            return 1
'''

SHOP = """
    from sandloom.sdk import Contract, define_module
    from sandloom.sdk.module import ContractRequirement  # sandloom: advanced test

    from ._generated.contracts.{p}_quota_v1 import {P}_QUOTA_V1

    quota = Contract({P}_QUOTA_V1)
    register = define_module(__name__, consumes=[ContractRequirement("{p}.quota.v1")])
"""


@pytest.fixture
def tree(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Provider and consumer packages under ``tmp_path/src``, importable."""
    tag = uuid.uuid4().hex[:6]
    prov, shop = f"kbprov{tag}", f"kbshop{tag}"
    src = tmp_path / "src"
    (src / prov).mkdir(parents=True)
    (src / shop).mkdir()
    (src / prov / "__init__.py").write_text("")
    (src / prov / "schemas.py").write_text(textwrap.dedent(SCHEMAS))
    (src / prov / "contracts.py").write_text(textwrap.dedent(CONTRACTS).format(p=prov))
    shop_init = textwrap.dedent(SHOP).format(p=prov, P=prov.upper(), s="")
    (src / shop / "__init__.py").write_text(
        shop_init.replace("from ._generated", "from ._generated")
    )
    sys.path.insert(0, str(src))
    try:
        yield {
            "src": src,
            "prov": prov,
            "shop": shop,
            "contract": f"{prov}.quota.v1",
            "modules": {shop: src / shop},
            "out": src / shop / "_generated" / "contracts",
        }
    finally:
        sys.path.remove(str(src))
        for name in [m for m in sys.modules if m.startswith((prov, shop))]:
            del sys.modules[name]


def _write(files: list[cc.Planned]) -> None:
    for item in files:
        assert not item.problems, item.problems
        item.path.parent.mkdir(parents=True, exist_ok=True)
        item.path.write_text(item.content)


def _composed(tree: dict[str, Any], methods: tuple[str, ...]) -> list[cc.Planned]:
    return cc.plan_composed(
        tree["modules"],
        {tree["shop"]: {tree["contract"]: methods}},
        {tree["shop"]: {tree["contract"]}},
        tree["src"],
        generated.STUBS,
    )


def test_the_planner_reads_the_declarations_before_composition(
    tree: dict[str, Any],
) -> None:
    """The consumer imports a stub that does not exist yet: the planned set
    comes from its AST, and the copies are written from the provider's
    ``contracts`` module, never composed."""
    files, planned = cc.plan_static(tree["modules"], tree["src"], generated.STUBS)
    assert planned == {tree["shop"]: {tree["contract"]}}
    names = sorted(f.path.name for f in files)
    assert names == [
        "__init__.py",
        "__init__.py",
        f"{tree['prov']}_dto.py",
        f"{tree['prov']}_quota_v1.py",
    ]
    _write(files)
    stub = (tree["out"] / f"{tree['prov']}_quota_v1.py").read_text()
    assert stub.startswith(cc.FIRST_LINE + "\n# contract: " + tree["contract"])
    assert f"from {tree['shop']}._generated.contracts.{tree['prov']}_dto import" in stub
    assert cc.verify(stub) is None
    module = importlib.import_module(
        f"{tree['shop']}._generated.contracts.{tree['prov']}_quota_v1"
    )
    assert getattr(module, f"{tree['prov'].upper()}_QUOTA_V1").name == tree["contract"]


def test_the_expectation_is_narrowed_to_the_consumed_methods(
    tree: dict[str, Any],
) -> None:
    files = _composed(tree, ("closed", "get"))
    _write(files)
    doc = json.loads(
        (tree["out"] / f"{tree['prov']}_quota_v1{cc.EXPECTATION_SUFFIX}").read_text()
    )
    assert doc["methods"] == ["closed", "get"]
    assert sorted(doc["describe"]["methods"]) == ["closed", "get"]  # not "unused"
    assert doc["provider"]["class"] == f"{tree['prov']}.contracts.QuotaRead"
    assert doc["generator_format"] == cc.GENERATOR_FORMAT
    assert doc["describe"]["methods"]["closed"]["returns"]["extra"] == "forbid"


def test_an_entry_without_methods_is_refused_naming_consumes(
    tree: dict[str, Any],
) -> None:
    problems = [p for f in _composed(tree, ()) for p in f.problems]
    assert problems and "define_module(consumes=[" in problems[0]
    assert all(f.code == cc.CODE for f in _composed(tree, ()) if f.problems)


def test_the_planned_set_must_equal_the_composed_consumes(tree: dict[str, Any]) -> None:
    other = f"{tree['prov']}.other.v1"
    files = cc.plan_composed(
        tree["modules"],
        {tree["shop"]: {tree["contract"]: ("get",), other: ("x",)}},
        {tree["shop"]: {tree["contract"]}},
        tree["src"],
        generated.STUBS,
    )
    problems = [p for f in files for p in f.problems]
    assert any(other in p and "consumes=" in p for p in problems)
    files = cc.plan_composed(
        tree["modules"],
        {tree["shop"]: {}},
        {tree["shop"]: {tree["contract"]}},
        tree["src"],
        generated.STUBS,
    )
    assert any("drop the declaration" in p for f in files for p in f.problems)


def test_an_import_with_no_consumes_entry_is_refused(tree: dict[str, Any]) -> None:
    (tree["src"] / tree["shop"] / "api.py").write_text(
        "from ._generated.contracts.nothing_here_v1 import X\n"
    )
    files, _ = cc.plan_static(tree["modules"], tree["src"], generated.STUBS)
    problems = [p for f in files for p in f.problems]
    assert any("nothing_here_v1" in p and "no consumes entry" in p for p in problems)


def test_kernel_families_get_no_copy(tree: dict[str, Any]) -> None:
    assert cc.is_kernel_family("billing.facts.v1") and cc.is_kernel_family(
        "x.claims.v1"
    )
    spec = cc.ModuleCopies(
        tree["shop"],
        tree["src"] / tree["shop"],
        {"billing.facts.v1": ("x",)},
        {},
        generated.STUBS,
    )
    assert cc.module_files(spec) == []


def test_stem_collisions_are_refused_naming_both(tree: dict[str, Any]) -> None:
    assert cc.stem_collisions(["a.b_c.v1", "a_b.c.v1", "x.y.v1"]) == {
        "a_b_c_v1": ["a.b_c.v1", "a_b.c.v1"]
    }
    spec = cc.ModuleCopies(
        tree["shop"],
        tree["src"] / tree["shop"],
        {"a.b_c.v1": ("m",), "a_b.c.v1": ("m",)},
        {},
        generated.STUBS,
    )
    files = cc.module_files(spec)
    problems = [p for f in files for p in f.problems]
    assert len(problems) == 1 and "a.b_c.v1 and a_b.c.v1" in problems[0]
    assert all(not f.content for f in files if f.problems)


def test_provider_absent_keeps_a_verified_copy_and_refuses_otherwise(
    tree: dict[str, Any],
) -> None:
    _write(_composed(tree, ("get",)))
    shop = tree["shop"]
    # keep: no provider importable, the committed copy verifies
    kept = cc.keep_committed(shop, tree["contract"], tree["out"])
    assert kept and not [p for f in kept for p in f.problems]
    assert all(f.content == f.path.read_text() for f in kept)  # nothing rewritten
    # edited: a hand edit breaks the digest
    dto = tree["out"] / f"{tree['prov']}_dto.py"
    dto.write_text(dto.read_text().replace("ge=0", "ge=1"))
    edited = [
        p
        for f in cc.keep_committed(shop, tree["contract"], tree["out"])
        for p in f.problems
    ]
    assert len(edited) == 1 and "hand-edited" in edited[0]
    # missing: nothing committed
    missing = cc.keep_committed(shop, "gone.thing.v1", tree["out"])
    assert missing[0].code == "SANDLOOM-T018"
    assert (
        "(module 'gone') is not importable here and no copy is committed"
        in missing[0].problems[0]
    )


def test_the_closed_dto_rule() -> None:
    """A field added to a closed result DTO breaks an older copy; to an open
    one it is additive."""

    def describe(extra: str | None, fields: list[str]) -> dict[str, Any]:
        model: dict[str, Any] = {
            "k": "model",
            "ref": "p:M",
            "schema_hash": "-".join(fields),
            "fields": {
                f: {"type": {"k": "scalar", "t": "int"}, "required": True}
                for f in fields
            },
        }
        if extra:
            model["extra"] = extra
        return {
            "hash": "-".join(fields),
            "methods": {"get": {"kind": "read", "params": [], "returns": model}},
        }

    closed = compare_contract(
        "p.c.v1", describe("forbid", ["n"]), describe(None, ["n", "added"])
    )
    assert not closed.compatible
    assert "closed model" in closed.errors[0]
    assert compare_contract(
        "p.c.v1", describe(None, ["n"]), describe(None, ["n", "added"])
    ).compatible


def test_explain_diff_tells_a_generator_change_from_a_contract_change() -> None:
    old = cc.with_provenance("x = 1\n", ["contract: a.b.v1  fingerprint: f1"])
    bumped = old.replace(
        f"format {cc.GENERATOR_FORMAT}", f"format {cc.GENERATOR_FORMAT + 1}"
    )
    assert "generator change" in cc.explain_diff(old, bumped)
    other = cc.with_provenance("x = 2\n", ["contract: a.b.v1  fingerprint: f2"])
    assert "contract change" in cc.explain_diff(old, other)


_DIGEST = """
import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
from sandloom.core.cli import consumer_copies as cc, generated
modules = json.loads(sys.argv[2])
items = dict(reversed(list(modules.items()))) if sys.argv[3] == "rev" else modules
files, _ = cc.plan_static({k: __import__("pathlib").Path(v) for k, v in items.items()}, __import__("pathlib").Path(sys.argv[1]), generated.STUBS)
blob = "".join(f"{f.path.name}\\0{f.content}\\0" for f in sorted(files, key=lambda f: str(f.path)))
print(hashlib.sha256(blob.encode()).hexdigest()[:16])
"""


def test_generation_is_byte_identical_across_seeds_orders_and_formatters(
    tree: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hash seeds and module orders (subprocesses), ruff absent and a
    foreign ``line-length=60`` in the project (the generator never runs the
    project's formatter): one digest."""
    second = tree["src"] / f"{tree['shop']}b"
    second.mkdir()
    (second / "__init__.py").write_text(
        (tree["src"] / tree["shop"] / "__init__.py").read_text()
    )
    modules = {tree["shop"]: str(tree["src"] / tree["shop"]), second.name: str(second)}
    digests = set()
    for seed in ("0", "12345", "random"):
        for order in ("fwd", "rev"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _DIGEST,
                    str(tree["src"]),
                    json.dumps(modules),
                    order,
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr
            digests.add(proc.stdout.strip())
    assert len(digests) == 1
    (tree["src"].parent / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 60\n"
    )

    def no_ruff(*_a: Any, **_k: Any) -> str:
        raise AssertionError("the generator ran the project's formatter")

    monkeypatch.setattr(dto_copies, "ruff_formatted", no_ruff)
    files, _ = cc.plan_static(
        {tree["shop"]: tree["src"] / tree["shop"]}, tree["src"], generated.STUBS
    )
    again, _ = cc.plan_static(
        {tree["shop"]: tree["src"] / tree["shop"]}, tree["src"], generated.STUBS
    )
    assert [f.content for f in files] == [f.content for f in again]


def test_the_final_layout_is_what_ruff_format_writes_at_88(
    tree: dict[str, Any],
) -> None:
    """The DTO copies' own layout (long fields wrapped, isort sections)
    equals ``ruff format`` at 88 columns, so a project formatter at its
    default leaves them alone."""
    pytest.importorskip("ruff")
    files, _ = cc.plan_static(tree["modules"], tree["src"], generated.STUBS)
    dto = next(f for f in files if f.path.name.endswith("_dto.py"))
    assert any(len(line) > 60 for line in dto.content.splitlines())
    root = Path(__file__).resolve().parents[2]
    assert (
        dto_copies.ruff_formatted(dto.content, root / "src" / "x_dto.py", cwd=root)
        == dto.content
    )
