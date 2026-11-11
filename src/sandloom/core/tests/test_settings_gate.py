"""The static settings gate (ADR-0015 decision 7, KB-56; scenarios P6 and P9 of
design/kernel-boundary/_archive/alternatives-d-settings.md §11, owner answers #25, #26, #30).

Each test writes small module packages and compose profiles into ``tmp_path``
and runs the gate over them: nothing here boots an application.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, Field

from sandloom.core.testing import settings_gate as gate
from sandloom.core.tests._entry_points_support import installed_entry_points
from sandloom.sdk.module import SemanticSetting  # sandloom: advanced test model

_MODULE = """
from typing import ClassVar

from pydantic import BaseModel, Field

from sandloom.sdk import define_module
from sandloom.sdk.module import SemanticSetting  # sandloom: advanced settings metadata


class Cfg(BaseModel):
{fields}
{semantic}

register = define_module(__name__, version="0.1.0", optional=True, settings=Cfg)
"""


class Pkg:
    """A module package written under ``tmp_path``."""

    def __init__(self, name: str, directory: Path) -> None:
        self.name, self.directory = name, directory

    @property
    def snapshot(self) -> Path:
        return self.directory / "module.snapshot.json"


MakeModule = Callable[..., Pkg]


@pytest.fixture
def make_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MakeModule:
    monkeypatch.syspath_prepend(str(tmp_path))

    def make(
        name: str,
        fields: dict[str, tuple[str, str]],
        *,
        semantic: tuple[str, ...] = (),
        snapshot: dict[str, object] | None = None,
    ) -> Pkg:
        body = "\n".join(
            f"    {attr}: {ann} = Field(None, alias={alias!r})"
            for alias, (attr, ann) in fields.items()
        )
        sem = ""
        if semantic:
            specs = ", ".join(
                f'SemanticSetting({n!r}, "x", profile={{"prod": False}}, '
                "strength=(True, False))"
                for n in semantic
            )
            sem = f"    semantic: ClassVar[tuple[SemanticSetting, ...]] = ({specs},)"
        directory = tmp_path / name
        directory.mkdir()
        (directory / "__init__.py").write_text(
            _MODULE.format(fields=body or "    pass", semantic=sem), encoding="utf-8"
        )
        pkg = Pkg(name, directory)
        if snapshot is not None:
            pkg.snapshot.write_text(json.dumps(snapshot), encoding="utf-8")
        return pkg

    return make


def _record(*names: str, tier: str = "ops", legacy: bool = False) -> dict[str, object]:
    entry: dict[str, object] = {"tier": tier}
    if legacy:
        entry["legacy"] = True
    return {"format": 1, "settings": {n: dict(entry) for n in names}}


def _problems(*pkgs: Pkg, profiles: list[Path] | None = None) -> list[str]:
    return gate.gate_problems([p.name for p in pkgs], profiles or [], kernel_names=())


# ----------------------------------------------------------- collisions ----


def test_a_name_declared_by_two_modules_that_never_load_together_is_flagged(
    make_module: MakeModule,
) -> None:
    a = make_module("col_a", {"COL_SHARED": ("x", "int | None")})
    b = make_module("col_b", {"COL_SHARED": ("x", "int | None")})
    problems = _problems(a, b)
    assert any(
        "COL_SHARED is declared by module col_a and by module col_b" in p
        and "SANDLOOM-B036" in p
        for p in problems
    ), problems


def test_a_name_declared_by_the_kernel_and_a_module_is_flagged(
    make_module: MakeModule,
) -> None:
    pkg = make_module("col_k", {"LOG_LEVEL": ("x", "str | None")})
    problems = gate.gate_problems([pkg.name], kernel_names=["LOG_LEVEL"])
    assert any("declared by the kernel and by module" in p for p in problems)


# --------------------------------------------------------------- prefix ----


def test_a_new_unprefixed_name_fails_and_a_prefixed_one_passes(
    make_module: MakeModule,
) -> None:
    bad = make_module(
        "pfx_bad", {"WAREHOUSE": ("x", "str | None")}, snapshot=_record("WAREHOUSE")
    )
    (problem,) = [p for p in _problems(bad) if "does not start with" in p]
    assert "PFX_BAD_" in problem and "SANDLOOM-T019" in problem
    good = make_module(
        "pfx_good",
        {"PFX_GOOD_WAREHOUSE": ("x", "str | None")},
        snapshot=_record("PFX_GOOD_WAREHOUSE"),
    )
    assert _problems(good) == []


def test_a_name_the_snapshot_records_as_legacy_is_grandfathered(
    make_module: MakeModule,
) -> None:
    pkg = make_module(
        "pfx_old",
        {"WAREHOUSE": ("x", "str | None")},
        snapshot=_record("WAREHOUSE", legacy=True),
    )
    assert _problems(pkg) == []


def test_settings_section_grandfathers_only_names_a_module_already_had() -> None:
    class Cfg(BaseModel):
        old: str | None = Field(None, alias="STRIPE_KEY")
        new: str | None = Field(None, alias="BILLING_NEW")

    fresh = gate.settings_section(Cfg, "billing", None)
    assert fresh == {"BILLING_NEW": {"tier": "ops"}, "STRIPE_KEY": {"tier": "ops"}}
    adopted = gate.settings_section(Cfg, "billing", {"format": 1})
    assert adopted["STRIPE_KEY"] == {"tier": "ops", "legacy": True}
    assert adopted["BILLING_NEW"] == {"tier": "ops"}

    class Later(Cfg):
        loose: str | None = Field(None, alias="LOOSE")

    again = gate.settings_section(Later, "billing", {"settings": adopted})
    assert again["STRIPE_KEY"].get("legacy") is True
    assert (
        "legacy" not in again["LOOSE"]
    )  # a later unprefixed name is not grandfathered


# ---------------------------------------------------------------- tiers ----


def test_a_module_without_a_recorded_settings_section_is_flagged(
    make_module: MakeModule,
) -> None:
    pkg = make_module("tier_none", {"TIER_NONE_X": ("x", "str | None")})
    assert any("records none of its settings" in p for p in _problems(pkg))


def test_a_tier_changed_in_the_code_but_not_in_the_snapshot_is_flagged(
    make_module: MakeModule,
) -> None:
    pkg = make_module(
        "tier_chg",
        {"TIER_CHG_FLAG": ("x", "bool | None")},
        semantic=("TIER_CHG_FLAG",),
        snapshot=_record("TIER_CHG_FLAG"),
    )
    (problem,) = _problems(pkg)
    assert "semantic in the code but ops in module.snapshot.json" in problem
    pkg.snapshot.write_text(
        json.dumps(_record("TIER_CHG_FLAG", tier="semantic")), encoding="utf-8"
    )
    assert _problems(pkg) == []


def test_a_name_left_in_the_snapshot_but_not_declared_is_flagged(
    make_module: MakeModule,
) -> None:
    pkg = make_module(
        "tier_gone",
        {"TIER_GONE_A": ("x", "str | None")},
        snapshot=_record("TIER_GONE_A", "TIER_GONE_B"),
    )
    (problem,) = _problems(pkg)
    assert "records TIER_GONE_B" in problem


def test_a_semantic_name_that_is_no_field_is_flagged(make_module: MakeModule) -> None:
    pkg = make_module(
        "tier_ghost",
        {"TIER_GHOST_A": ("x", "bool | None")},
        semantic=("TIER_GHOST_B",),
        snapshot=_record("TIER_GHOST_A"),
    )
    assert any("TIER_GHOST_B is not a field" in p for p in _problems(pkg))


def test_the_semantic_ratchet_helper() -> None:
    class Cfg(BaseModel):
        a: bool = Field(False, alias="X_A")
        b: bool = Field(False, alias="X_B")
        semantic: ClassVar[tuple[SemanticSetting, ...]] = (
            SemanticSetting(
                "X_A", "a", profile={"prod": False}, strength=(True, False)
            ),
            SemanticSetting(
                "X_B", "b", profile={"prod": False}, strength=(True, False)
            ),
        )

    assert gate.semantic_ratchet_problems(Cfg, baseline=["X_A", "X_B"], ceiling=2) == []
    assert gate.semantic_ratchet_problems(Cfg, baseline=["X_A"], ceiling=2) == [
        "X_B is a new semantic setting: justify it with an adr= or make it an ops setting"
    ]
    assert any(
        "left the semantic tier" in p
        for p in gate.semantic_ratchet_problems(
            Cfg, baseline=["X_A", "X_B", "X_C"], ceiling=3
        )
    )
    assert any(
        "exceed the ceiling" in p
        for p in gate.semantic_ratchet_problems(Cfg, baseline=["X_A", "X_B"], ceiling=1)
    )


# ------------------------------------------------------------- topology ----


def _profile(tmp_path: Path, services: dict[str, dict[str, object]]) -> Path:
    import yaml

    directory = tmp_path / "deploy" / "compose"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "microservices.yml"
    path.write_text(yaml.safe_dump({"services": services}), encoding="utf-8")
    return path


@pytest.fixture
def standard_modules() -> Iterator[None]:
    """``identity``, ``tenancy`` and ``billing`` are installed module names
    (their entry points), whether or not their packages are."""
    with installed_entry_points():
        yield


def _inventory(make_module: MakeModule, name: str) -> Pkg:
    prefix = name.upper()
    return make_module(
        name,
        {
            f"{prefix}_TTL": ("ttl", "int | None"),
            f"{prefix}_API_TOKEN": ("token", "int | None"),
        },
        snapshot=_record(f"{prefix}_TTL", f"{prefix}_API_TOKEN"),
    )


@pytest.mark.usefixtures("standard_modules")
def test_a_wrong_module_name_in_a_service_is_reported_with_a_hint(
    make_module: MakeModule, tmp_path: Path
) -> None:
    pkg = _inventory(make_module, "topo_a")
    profile = _profile(
        tmp_path,
        {
            "api": {
                "environment": {"SANDLOOM_MODULES": json.dumps([pkg.name, "identty"])}
            }
        },
    )
    (problem,) = [
        p for p in _problems(pkg, profiles=[profile]) if "SANDLOOM_MODULES names" in p
    ]
    assert "'identty'" in problem and "did you mean 'identity'" in problem


@pytest.mark.usefixtures("standard_modules")
def test_a_valid_module_name_or_import_path_is_not_reported(
    make_module: MakeModule, tmp_path: Path
) -> None:
    pkg = _inventory(make_module, "topo_b")
    # An importable package no profile module declares, named by its path.
    other = make_module("topo_b_other", {})
    modules = [pkg.name, "identity", other.name]
    profile = _profile(
        tmp_path, {"api": {"environment": {"SANDLOOM_MODULES": json.dumps(modules)}}}
    )
    assert _problems(pkg, profiles=[profile]) == []


@pytest.mark.usefixtures("standard_modules")
def test_a_name_set_for_a_module_the_service_does_not_load_is_flagged(
    make_module: MakeModule, tmp_path: Path
) -> None:
    pkg = _inventory(make_module, "topo_c")
    ttl = "TOPO_C_TTL"
    profile = _profile(
        tmp_path,
        {
            "tenancy": {"environment": {"SANDLOOM_MODULES": '["tenancy"]', ttl: "60"}},
            "inv": {
                "environment": {"SANDLOOM_MODULES": json.dumps([pkg.name]), ttl: "60"}
            },
        },
    )
    (problem,) = _problems(pkg, profiles=[profile])
    assert "service 'tenancy' sets TOPO_C_TTL" in problem


def test_a_literal_value_its_declaration_refuses_is_flagged(
    make_module: MakeModule, tmp_path: Path
) -> None:
    pkg = _inventory(make_module, "topo_d")
    profile = _profile(
        tmp_path,
        {
            "inv": {
                "environment": {
                    "SANDLOOM_MODULES": json.dumps([pkg.name]),
                    "TOPO_D_TTL": "soon",
                    "TOPO_D_API_TOKEN": "${INV_TOKEN:?set it}",
                }
            }
        },
    )
    (problem,) = _problems(pkg, profiles=[profile])
    assert "TOPO_D_TTL='soon'" in problem and "integer" in problem


def test_a_secret_literal_is_never_echoed(
    make_module: MakeModule, tmp_path: Path
) -> None:
    pkg = _inventory(make_module, "topo_e")
    profile = _profile(
        tmp_path,
        {
            "inv": {
                "environment": {
                    "SANDLOOM_MODULES": json.dumps([pkg.name]),
                    "TOPO_E_API_TOKEN": "hunter2-not-an-int",
                }
            }
        },
    )
    (problem,) = _problems(pkg, profiles=[profile])
    assert "hunter2" not in problem and "<value hidden>" in problem


def test_a_placeholder_or_a_valid_literal_passes(
    make_module: MakeModule, tmp_path: Path
) -> None:
    pkg = _inventory(make_module, "topo_f")
    modules = json.dumps([pkg.name])
    profile = _profile(
        tmp_path,
        {
            "inv": {
                "environment": {
                    "SANDLOOM_MODULES": modules,
                    "TOPO_F_TTL": "${INV_TTL:-300}",
                    "TOPO_F_API_TOKEN": "${INV_TOKEN:?set it}",
                }
            },
            "inv-two": {"environment": {"SANDLOOM_MODULES": modules, "TOPO_F_TTL": 60}},
        },
    )
    assert _problems(pkg, profiles=[profile]) == []


# ------------------------------------------------- the snapshot `generate` writes ----


def _records(pkg: Pkg) -> list[object]:
    """What the composed application's registry holds for ``pkg``."""
    declared, _ = gate.declared_modules([pkg.name])
    return [types.SimpleNamespace(package=d.package, model=d.model) for d in declared]


def test_generate_records_the_settings_and_grandfathers_a_module_that_adopts_them(
    make_module: MakeModule,
) -> None:
    from sandloom.core.cli import generated

    # a module that already had a snapshot (no settings record) adopts its names
    pkg = make_module(
        "gen_old", {"OLD_NAME": ("x", "str | None"), "GEN_OLD_NEW": ("y", "int | None")}
    )
    pkg.snapshot.write_text(
        json.dumps({"format": 1, "module": "gen_old"}), encoding="utf-8"
    )
    doc: dict[str, object] = {"format": 1}
    generated._record_settings(doc, pkg.name, pkg.snapshot, _records(pkg))
    assert doc["settings"] == {
        "GEN_OLD_NEW": {"tier": "ops"},
        "OLD_NAME": {"tier": "ops", "legacy": True},
    }
    # the record it wrote is the baseline the gate holds the module to
    pkg.snapshot.write_text(json.dumps(doc), encoding="utf-8")
    assert _problems(pkg) == []

    # a new module (no snapshot) gets no grandfathering: the gate flags it
    new = make_module("gen_new", {"LOOSE": ("x", "str | None")})
    fresh: dict[str, object] = {"format": 1}
    generated._record_settings(fresh, new.name, new.snapshot, _records(new))
    assert fresh["settings"] == {"LOOSE": {"tier": "ops"}}
    new.snapshot.write_text(json.dumps(fresh), encoding="utf-8")
    assert any("does not start with GEN_NEW_" in p for p in _problems(new))


def test_a_module_without_settings_gets_no_settings_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.cli import generated

    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "gen_plain").mkdir()
    (tmp_path / "gen_plain" / "__init__.py").write_text(
        "from sandloom.sdk import define_module\n"
        "register = define_module(__name__, version='0.1.0')\n",
        encoding="utf-8",
    )
    doc: dict[str, object] = {"format": 1}
    generated._record_settings(doc, "gen_plain", tmp_path / "gen_plain" / "m.json", [])
    assert doc == {"format": 1}
