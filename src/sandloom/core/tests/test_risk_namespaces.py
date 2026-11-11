"""Namespaced accepted risks (ADR-0015 decision 4, KB-51, owner answers #27, #28).

The rule in-process: kernel ids stay flat, a module's id is
``<module>.<name>``; the namespace is checked once the modules are
registered. The process matrix (P4: a split with one shared anchor, every
entry point) is ``tests/integration/test_settings_risk_namespaces.py``.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, Field

import sandloom.core.config.module_settings as ms
from sandloom.core.config import accepted_risks as risks
from sandloom.core.config import tiers
from sandloom.core.config.common import Environment
from sandloom.core.config.settings import Settings
from sandloom.core.module_api import Risk, SemanticSetting


class ShopSettings(BaseModel):
    allow_debt: bool = Field(False, alias="SHOP_ALLOW_DEBT")

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


def _kernel(environment: str = "prod", *accepted: str) -> Settings:
    return Settings.model_construct(
        environment=Environment(environment), sandloom_accepted_risks=list(accepted)
    )


def _shop(kernel: Settings) -> ms.ModuleSettingsRecord:
    return ms.validate_module_settings(
        ShopSettings,
        package="shop_pkg",
        module="shop",
        kernel=kernel,
        snapshot=ms.SourceSnapshot(environ={}, env_file=None),
    )


def _check(
    kernel: Settings,
    *,
    loaded: tuple[str, ...] = (),
    known: tuple[str, ...] = (),
    with_shop: bool = True,
) -> ms.PostLoadOutcome:
    records = [_shop(kernel)] if with_shop else []
    return ms.post_load_check(
        records,
        kernel,
        ms.SourceSnapshot(environ={}, env_file=None),
        known_modules=known,
        loaded_modules=loaded,
    )


# ------------------------------------------------------------- the registry ----


def test_kernel_ids_stay_flat_and_module_ids_are_not_kernel_ids() -> None:
    assert risks.unknown_ids(["relay_disabled", "shop.debt_in_prod"]) == []
    assert risks.unknown_ids(["fake_psp"]) == ["fake_psp"]
    assert risks.is_module_id("shop.debt_in_prod")
    assert not risks.is_module_id("relay_disabled")
    # not <module>.<name>: refused by the Settings validator, not deferred
    assert risks.unknown_ids(["a.b.c", "Shop.x", ".x", "shop."]) == sorted(
        ["a.b.c", "Shop.x", ".x", "shop."]
    )


def test_settings_defers_a_module_id_and_refuses_a_flat_unknown_one() -> None:
    cfg = Settings.model_validate({"SANDLOOM_ACCEPTED_RISKS": '["shop.debt_in_prod"]'})
    assert cfg.sandloom_accepted_risks == ["shop.debt_in_prod"]
    # the kernel registry does not know it: the module declares it
    assert cfg.accepted_risks == frozenset()
    with pytest.raises(ValueError, match="unknown risk id"):
        Settings.model_validate({"SANDLOOM_ACCEPTED_RISKS": '["debt_in_prod"]'})


def test_a_flat_id_names_the_replacement_of_an_installed_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ms,
        "installed_risk_declarations",
        lambda: {"shop": ShopSettings.risks},
    )
    with pytest.raises(ValueError, match="'debt_in_prod' is now 'shop.debt_in_prod'"):
        Settings.model_validate({"SANDLOOM_ACCEPTED_RISKS": '["debt_in_prod"]'})


def test_a_flat_id_without_an_installed_module_names_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "installed_risk_declarations", dict)
    message = ms.flat_risk_refusal(["debt_in_prod"])
    assert "<module>.<name>" in message
    assert " is now " not in message


def test_two_installed_modules_declaring_one_flat_id_name_no_replacement() -> None:
    declared = {"a": ShopSettings.risks, "b": ShopSettings.risks}
    assert risks.replacement_ids(["debt_in_prod"], declared) == {}


def test_the_installed_declarations_are_read_from_the_entry_points() -> None:
    declared = ms.installed_risk_declarations()
    # no bundled module declares settings yet (KB-52..54): nothing to name
    assert all(isinstance(v, tuple) for v in declared.values())


# ---------------------------------------------------- the post-load check ----


def test_the_id_of_a_loaded_module_is_checked_against_its_declaration() -> None:
    kernel = _kernel("prod", "shop.debt_in_prod")
    outcome = _check(kernel)
    assert outcome.problems == []
    ((risk_id, item),) = outcome.risks.active
    assert risk_id == "shop.debt_in_prod" and item.id == "shop.debt_in_prod"
    (override,) = [o for o in outcome.overrides if o.kind == "accepted_risk"]
    assert override.message().startswith("ACCEPTED RISK: shop.debt_in_prod")


def test_an_undeclared_id_of_a_loaded_module_refuses() -> None:
    outcome = _check(_kernel("prod", "shop.nope"))
    (problem,) = outcome.problems
    assert "module shop declares no risk 'nope'" in problem
    assert "shop.debt_in_prod" in problem


def test_a_loaded_module_without_settings_declares_no_risk() -> None:
    outcome = _check(_kernel("prod", "notes.x"), loaded=("notes",), with_shop=False)
    (problem,) = outcome.problems
    assert "module notes declares no risk 'x'" in problem


def test_the_id_of_a_known_module_that_is_not_loaded_is_inert() -> None:
    outcome = _check(_kernel("prod", "billing.fake_psp_in_prod"), known=("billing",))
    assert outcome.problems == []
    assert outcome.risks.inert == ("billing.fake_psp_in_prod",)
    assert outcome.risks.active == ()
    assert not [o for o in outcome.overrides if o.kind == "accepted_risk"]


def test_an_unknown_namespace_refuses_even_beside_a_known_one() -> None:
    kernel = _kernel("prod", "billing.fake_psp_in_prod", "biling.fake_psp_in_prod")
    outcome = _check(kernel, known=("billing",), with_shop=False)
    (problem,) = outcome.problems
    assert "'biling.fake_psp_in_prod'" in problem
    assert "typo" in problem
    assert outcome.risks.inert == ("billing.fake_psp_in_prod",)


def test_a_typo_in_the_risk_name_of_a_loaded_module_is_not_inert() -> None:
    outcome = _check(_kernel("prod", "shop.debt_in_prd"), known=("shop",))
    assert len(outcome.problems) == 1


def test_known_modules_are_asked_only_when_a_namespace_needs_them() -> None:
    def explode() -> Any:  # pragma: no cover - must not run
        raise AssertionError("the entry points were scanned")

    kernel = _kernel("prod", "relay_disabled", "shop.debt_in_prod")
    records = [_shop(kernel)]
    report = ms._risk_report(records, kernel, ["shop"], explode)
    assert report.problems == ()
    assert ms._risk_report([], _kernel("prod", "relay_disabled"), [], explode) == (
        risks.ModuleRiskReport()
    )


def test_no_accepted_module_id_is_a_no_op() -> None:
    outcome = _check(_kernel("prod", "relay_disabled"))
    assert outcome.risks == risks.ModuleRiskReport()
    assert outcome.problems == []


def test_a_module_risk_outside_prod_is_reported_only_when_it_weakens_there() -> None:
    outcome = _check(_kernel("dev", "shop.debt_in_prod"))
    assert outcome.problems == []
    assert [r for r, _ in outcome.risks.active] == ["shop.debt_in_prod"]
    assert not [o for o in outcome.overrides if o.kind == "accepted_risk"]


def test_a_semantic_setting_with_an_accepted_risk_is_reported_as_the_risk() -> None:
    kernel = _kernel("prod", "shop.debt_in_prod")
    record = ms.validate_module_settings(
        ShopSettings,
        package="shop_pkg",
        module="shop",
        kernel=kernel,
        snapshot=ms.SourceSnapshot(environ={"SHOP_ALLOW_DEBT": "true"}, env_file=None),
    )
    outcome = ms.post_load_check(
        [record], kernel, ms.SourceSnapshot(environ={}, env_file=None)
    )
    kinds = sorted(o.kind for o in outcome.overrides)
    assert kinds == ["accepted_risk"]
    assert tiers.profile_of(kernel) == "prod"


def test_diagnostics_list_an_inert_id_as_not_applicable_here() -> None:
    kernel = _kernel("prod", "relay_disabled", "shop.debt_in_prod", "billing.x_y")
    outcome = _check(kernel, known=("billing",))
    rows = tiers.diagnostics(kernel, outcome.risks)["accepted_risks"]
    by_id = {row["risk"]: row for row in rows}  # type: ignore[attr-defined]
    assert by_id["relay_disabled"]["in_effect"] is True
    assert by_id["shop.debt_in_prod"]["in_effect"] is True
    assert by_id["billing.x_y"]["applicable"] is False
    assert "not applicable here" in by_id["billing.x_y"]["note"]
    # without the loader's report only the kernel's ids are listed
    kernel_only = tiers.diagnostics(kernel)["accepted_risks"]
    assert [row["risk"] for row in kernel_only] == ["relay_disabled"]  # type: ignore[attr-defined]
