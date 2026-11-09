"""The kernel's tools read the loaded modules' declarations (ADR-0015, KB-55).

In-process tests of the mechanism with models defined here: ``/diagnostics/core``
and doctor show the modules' risks, overrides and tiers beside the kernel's,
collect mode turns a module's refusal into a diagnosed entry in the loader,
and the declaration accessors read the metadata classes carry. The process
matrix (``config dump``, T014, the dev env, the scaffold) is
``tests/integration/test_kernel_tools_declarations_fixture.py``.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field

import sandloom.core.config.module_settings as ms
from sandloom.core.admin.api import platform
from sandloom.core.app_factory import ops
from sandloom.core.cli import doctor
from sandloom.core.cli.config import module_config
from sandloom.core.config import tiers
from sandloom.core.config.common import ConfigurationError, Environment
from sandloom.core.config.settings import Settings
from sandloom.core.descriptors import ModuleLoadSummary
from sandloom.core.facade import CoreFacade
from sandloom.core.loader import plugin_loader
from sandloom.core.module_api import Risk, SemanticSetting


class ShopSettings(BaseModel):
    allow_debt: bool = Field(False, alias="SHOP_ALLOW_DEBT")
    region: str = Field("eu", alias="SHOP_REGION")
    key: str | None = Field(None, alias="SHOP_SIGNING_KEY")

    semantic: ClassVar[tuple[SemanticSetting, ...]] = (
        SemanticSetting(
            "SHOP_ALLOW_DEBT",
            "Orders may exceed the account's balance.",
            profile={"dev": False, "test": False, "prod": False, "prod_strict": False},
            strength=(True, False),
            risk="debt_in_prod",
        ),
    )
    risks: ClassVar[tuple[Risk, ...]] = (
        Risk("debt_in_prod", "orders may exceed the balance", "ShopSettings"),
    )
    console: ClassVar[tuple[str, ...]] = ("region",)
    shared: ClassVar[tuple[str, ...]] = ("shop_region",)
    backup: ClassVar[tuple[tuple[str, str], ...]] = (("SHOP_SIGNING_KEY", "re-sign"),)
    deploy: ClassVar[tuple[str, ...]] = ("SHOP_REGION: ${SHOP_REGION:-eu}",)
    dev_env: ClassVar[dict[str, Any]] = {
        "SHOP_REGION": "local",
        "SHOP_SIGNING_KEY": lambda: "k",
    }
    dev_risks: ClassVar[tuple[str, ...]] = ("debt_in_prod",)


class PlainSettings(BaseModel):
    level: int = Field(1, alias="PLAIN_LEVEL")


def _kernel(environment: str = "test", *accepted: str) -> Settings:
    return Settings.model_construct(
        environment=Environment(environment), sandloom_accepted_risks=list(accepted)
    )


def _snapshot(**env: str) -> ms.SourceSnapshot:
    return ms.SourceSnapshot(environ=env, env_file=None)


def _records(kernel: Settings, **env: str) -> list[ms.ModuleSettingsRecord]:
    return [
        ms.validate_module_settings(
            model, package=pkg, module=pkg, kernel=kernel, snapshot=_snapshot(**env)
        )
        for pkg, model in (("shop", ShopSettings), ("plain", PlainSettings))
    ]


def _core(kernel: Settings, **env: str) -> Any:
    """A core carrying what the loader's post-load check stored on it."""
    records = _records(kernel, **env)
    outcome = ms.post_load_check(
        records, kernel, _snapshot(), known_modules=("billing",), loaded_modules=()
    )
    core = SimpleNamespace(module_settings=ms.ModuleSettingsRegistry())
    for record in records:
        core.module_settings.add(record)
    core.module_settings.risk_report = outcome.risks
    core.module_settings.overrides = list(outcome.overrides)
    return core


# --------------------------------------------------------- the accessors ----


def test_the_declaration_accessors_read_the_metadata() -> None:
    assert ms.declared_console(ShopSettings) == ("region",)
    assert ms.declared_shared(ShopSettings) == ("SHOP_REGION",)  # env names, upper
    assert ms.declared_backup(ShopSettings) == (("SHOP_SIGNING_KEY", "re-sign"),)
    assert ms.declared_deploy(ShopSettings) == ("SHOP_REGION: ${SHOP_REGION:-eu}",)
    assert ms.declared_dev_env(ShopSettings) == {
        "SHOP_REGION": "local",
        "SHOP_SIGNING_KEY": "k",
    }
    assert ms.declared_dev_risks(ShopSettings) == ("debt_in_prod",)
    for accessor in (
        ms.declared_console,
        ms.declared_shared,
        ms.declared_backup,
        ms.declared_deploy,
        ms.declared_dev_env,
        ms.declared_dev_risks,
    ):
        assert not accessor(PlainSettings)  # nothing declared: nothing read


def test_module_tier_counts_follow_the_declared_semantic_settings() -> None:
    counts = ms.module_tier_counts(_records(_kernel()))
    assert counts == {
        "shop": {"semantic": 1, "ops": 2},
        "plain": {"semantic": 0, "ops": 1},
    }


# ------------------------------------------------- /diagnostics/core, doctor ----


def test_core_diagnostics_show_the_modules_risks_overrides_and_tiers() -> None:
    """Backlog item folded into KB-55: ``/diagnostics/core`` showed the kernel's
    accepted risks only."""
    kernel = _kernel("test", "shop.debt_in_prod", "billing.fake_psp_in_prod")
    core = _core(kernel)
    block = tiers.diagnostics(
        kernel,
        core.module_settings.risk_report,
        core.module_settings.overrides,
        ms.module_tier_counts(core.module_settings.records()),
    )
    rows = {row["risk"]: row for row in block["accepted_risks"]}  # type: ignore[attr-defined]
    assert rows["shop.debt_in_prod"]["in_effect"] is False  # weakens nothing in test
    assert rows["billing.fake_psp_in_prod"]["applicable"] is False
    assert block["module_tiers"]["shop"] == {"semantic": 1, "ops": 2}  # type: ignore[index]

    weaker = _core(_kernel("test"), SHOP_ALLOW_DEBT="true")
    items = tiers.diagnostics(_kernel("test"), None, weaker.module_settings.overrides)[
        "overrides"
    ]
    assert [i["setting"] for i in items] == ["SHOP_ALLOW_DEBT"]  # type: ignore[attr-defined]


def test_the_ops_block_reads_what_the_loader_stored_on_the_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel("test", "shop.debt_in_prod")
    monkeypatch.setattr(ops, "settings", kernel)
    block = ops._configuration_diagnostics(_core(kernel, SHOP_ALLOW_DEBT="true"))
    assert block["tiers"] == tiers.tier_counts()
    assert [r["risk"] for r in block["accepted_risks"]] == ["shop.debt_in_prod"]  # type: ignore[attr-defined]
    assert "module_tiers" in block


def test_doctor_reports_module_overrides_and_inert_risks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel("test", "billing.fake_psp_in_prod")
    monkeypatch.setattr(doctor, "settings", kernel)
    report = doctor.Report()
    doctor.check_module_settings(report, _core(kernel, SHOP_ALLOW_DEBT="true"))
    by_name = {c.name: c for c in report.checks}
    assert by_name["override:SHOP_ALLOW_DEBT"].status == doctor.OK  # weaker, not prod
    assert "SHOP_ALLOW_DEBT=true weakens" in by_name["override:SHOP_ALLOW_DEBT"].message
    inert = by_name["risk:billing.fake_psp_in_prod"]
    assert inert.status == doctor.OK
    assert "not applicable here" in inert.message
    # prod: the same override is a warning
    monkeypatch.setattr(doctor, "settings", _kernel("prod"))
    loud = doctor.Report()
    doctor.check_module_settings(loud, _core(_kernel("prod"), SHOP_ALLOW_DEBT="true"))
    assert {c.status for c in loud.checks if c.name.startswith("override:")} == {
        doctor.WARN
    }


def test_doctor_lists_every_modules_problem_and_leaves_collect_mode_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor loads the composition in collect mode: each module's refusal is a
    failed check (not the first one ending the run), and the switch is off
    again afterwards."""
    monkeypatch.setattr(doctor, "settings", _kernel("test"))
    seen: list[bool] = []

    def loader() -> Any:
        found = ms.collecting()
        assert found is not None
        seen.append(True)
        found.append(ms.Diagnosed("shop_pkg", "module shop_pkg: SHOP_LIMIT: bad"))
        found.append(ms.Diagnosed("other_pkg", "module other_pkg: OTHER_X: bad"))
        return _core(_kernel("test"))

    monkeypatch.setattr(doctor, "check_config", lambda report, cfg: None)
    report = asyncio.run(doctor.run_doctor(core_loader=loader))
    failed = {c.name: c.message for c in report.checks if c.status == doctor.FAIL}
    assert failed == {
        "module:shop_pkg": "module shop_pkg: SHOP_LIMIT: bad",
        "module:other_pkg": "module other_pkg: OTHER_X: bad",
    }
    assert report.exit_code == doctor.EXIT_FAIL
    assert seen == [True]
    assert ms.collecting() is None


# --------------------------------------------------------- the loader ----


def test_a_refusal_propagates_outside_collect_mode_and_is_diagnosed_inside() -> None:
    summary = ModuleLoadSummary()
    refusal = ConfigurationError(
        "[core.config] Invalid configuration: module m: X: bad"
    )
    with pytest.raises(ConfigurationError):
        plugin_loader._diagnose("m", refusal, summary)
    assert ms.collecting() is None and summary.failed == []
    with ms.collect_mode() as found:
        plugin_loader._diagnose("m", refusal, summary)
    assert [(d.module, d.message) for d in found] == [("m", "module m: X: bad")]
    (failed,) = summary.failed
    assert failed.name == "m" and failed.required is False
    assert ms.collecting() is None


@pytest.fixture
def facade() -> CoreFacade:
    return CoreFacade(FastAPI(), settings_obj=_kernel("dev"))


class _Twin(BaseModel):
    region: str = Field("x", alias="SHOP_REGION")


def test_post_load_problems_are_diagnosed_in_collect_mode_not_raised(
    facade: CoreFacade,
) -> None:
    for record in _records(_kernel("dev")):
        facade.module_settings.add(record)
    for record in [
        ms.validate_module_settings(
            _Twin,
            package="twin",
            module="twin",
            kernel=_kernel("dev"),
            snapshot=_snapshot(),
        )
    ]:
        facade.module_settings.add(record)
    with pytest.raises(ConfigurationError, match=ms.COLLISION_CODE):
        plugin_loader._check_module_settings(facade)
    with ms.collect_mode() as found:
        plugin_loader._check_module_settings(facade)  # no raise
    assert [d.module for d in found] == ["(composition)"]
    assert ms.COLLISION_CODE in found[0].message


def test_the_check_stores_the_modules_overrides_for_diagnostics(
    facade: CoreFacade, caplog: pytest.LogCaptureFixture
) -> None:
    for record in _records(_kernel("dev"), SHOP_ALLOW_DEBT="true"):
        facade.module_settings.add(record)
    with caplog.at_level(logging.INFO, logger="sandloom.settings"):
        plugin_loader._check_module_settings(facade)
    assert [o.setting for o in facade.module_settings.overrides] == ["SHOP_ALLOW_DEBT"]


# ------------------------------------------------------------- the dump ----


def test_module_config_lists_values_with_origin_and_redaction() -> None:
    kernel = _kernel("test")
    records = [
        ms.validate_module_settings(
            ShopSettings,
            package="shop",
            module="shop",
            kernel=kernel,
            snapshot=_snapshot(SHOP_SIGNING_KEY="s3cret", SHOP_REGION="us"),
        )
    ]
    core = SimpleNamespace(module_settings=ms.ModuleSettingsRegistry())
    core.module_settings.add(records[0])
    rows = {e.name: e for e in module_config(core)}
    assert rows["SHOP_SIGNING_KEY"].value == "<redacted>"
    assert rows["SHOP_SIGNING_KEY"].secret is True
    assert rows["SHOP_SIGNING_KEY"].source == "env"
    assert (rows["SHOP_REGION"].value, rows["SHOP_REGION"].source) == ("us", "env")
    assert rows["SHOP_ALLOW_DEBT"].source == "default"
    assert {e.module for e in rows.values()} == {"shop"}
    clear = {e.name: e for e in module_config(core, redact=False)}
    assert clear["SHOP_SIGNING_KEY"].value == "s3cret"


# ----------------------------------------- SEC-7: known modules by their names ----


class _EntryPoints:
    def __init__(self, *pairs: tuple[str, str]) -> None:
        self._eps = [SimpleNamespace(name=n, value=v) for n, v in pairs]

    def select(self, *, group: str) -> list[Any]:
        return self._eps if group == ms.MODULES_ENTRY_POINT_GROUP else []


def test_known_modules_name_an_import_path_by_its_module_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-7: ``SANDLOOM_MODULES`` entries are import paths (``sandloom_billing``)
    but a risk namespace is the module's name (``billing``, the entry point's):
    an image that names the path must know ``billing.fake_psp_in_prod`` as the
    module's, and never ``sandloom_billing`` as a module of its own."""
    monkeypatch.setattr(
        ms.metadata,
        "entry_points",
        lambda: _EntryPoints(("billing", "sandloom_billing"), ("identity", "sandloom_identity")),
    )
    kernel = Settings.model_construct(
        sandloom_modules=["sandloom_billing", "app.modules.shop_eu", "identity"],
        sandloom_accepted_risks=[],
    )
    assert ms.known_module_names(kernel) == {
        "billing",
        "identity",
        "shop_eu",
    }


def test_an_identity_only_image_treats_a_billing_risk_as_inert_not_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ms.metadata,
        "entry_points",
        lambda: _EntryPoints(("billing", "sandloom_billing"), ("identity", "sandloom_identity")),
    )
    kernel = _kernel("prod", "billing.fake_psp_in_prod", "sandloom_billing.fake_psp_in_prod")
    kernel.sandloom_modules = ["sandloom_identity"]
    outcome = ms.post_load_check([], kernel, _snapshot(), loaded_modules=["identity"])
    assert [r for r in outcome.risks.inert] == ["billing.fake_psp_in_prod"]
    # the path is no namespace: it is refused as unknown, never accepted
    assert any("sandloom_billing" in p for p in outcome.risks.problems)


# ------------------------------------------- the platform bindings view ----


def test_the_bindings_view_says_unverified_for_a_contract_with_no_expectation() -> None:
    """KB-21: a required contract with no expectation fails ``/ready`` with
    ``contract_unverified``; the platform view shows it as ``unverified``, not as
    an unreachable peer."""
    core = SimpleNamespace(
        settings=SimpleNamespace(
            service_bindings={"a.b.v1": {"required": True}, "c.d.v1": {}}
        ),
        service_registry=SimpleNamespace(contains=lambda name: False),
    )
    details = {
        "remote_services": {
            "a.b.v1": {"contract_unverified": ["no expectation: consume it"]},
            "c.d.v1": {"error": "connection refused"},
        }
    }
    by_name = {v.contract: v for v in platform._bindings(core, details)}
    assert by_name["a.b.v1"].status == "unverified"
    assert "no expectation" in (by_name["a.b.v1"].detail or "")
    assert by_name["c.d.v1"].status == "unreachable"
