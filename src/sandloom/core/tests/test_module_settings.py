"""Module-declared settings (ADR-0015 decisions 1-3, KB-50).

In-process tests of the mechanism with models defined here; the fixture
module outside ``src/`` and the process matrix are
``tests/integration/test_module_settings_fixture.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any, ClassVar

import pytest
from fastapi import FastAPI
from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode

import sandloom.core.config.module_settings as ms
import sandloom.core.module_api._settings as module_settings_api
from sandloom.core.config.common import INVALID_CONFIGURATION, Environment
from sandloom.core.config.settings import Settings
from sandloom.core.facade import CoreFacade
from sandloom.core.loader import plugin_loader
from sandloom.core.module_api import (
    ModuleSettingsError,
    RemovedSetting,
    Risk,
    SemanticSetting,
    SettingsNotBoundError,
    define_module,
    override_settings,
    settings_of,
    validate_settings,
)


class ShopSettings(BaseModel):
    token: SecretStr | None = Field(None, alias="SHOP_TOKEN")
    limit: int = Field(10, ge=1, le=100, alias="SHOP_LIMIT")
    allow_debt: bool = Field(False, alias="SHOP_ALLOW_DEBT")
    regions: list[str] = Field(default_factory=list, alias="SHOP_REGIONS")
    raw_regions: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="SHOP_RAW_REGIONS"
    )

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
    removed: ClassVar[tuple[RemovedSetting, ...]] = (
        RemovedSetting("SHOP_LEGACY", "0.2", "the legacy flow is gone"),
    )

    @field_validator("raw_regions", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        return value.split(",") if isinstance(value, str) else value

    @model_validator(mode="after")
    def _production(self, info: ValidationInfo) -> ShopSettings:
        context = info.context or {}
        if (
            context.get("environment") == "prod"
            and self.allow_debt
            and not context["accepts_risk"]("debt_in_prod")
        ):
            raise ValueError(
                "SHOP_ALLOW_DEBT=true is not allowed with ENVIRONMENT=prod"
            )
        return self


class OtherSettings(BaseModel):
    limit: int = Field(1, alias="SHOP_LIMIT")


def _kernel(environment: str = "dev", risks: tuple[str, ...] = ()) -> Settings:
    return Settings.model_construct(
        environment=Environment(environment), sandloom_accepted_risks=list(risks)
    )


def _snapshot(**env: str) -> ms.SourceSnapshot:
    return ms.SourceSnapshot(environ=env, env_file=None)


def _validate(
    model: type[BaseModel] = ShopSettings,
    *,
    kernel: Settings | None = None,
    **env: str,
) -> ms.ModuleSettingsRecord:
    return ms.validate_module_settings(
        model,
        package="shop_pkg",
        module="shop",
        kernel=kernel or _kernel(),
        snapshot=_snapshot(**env),
    )


# ---------------------------------------------------------------- sources ----


def test_snapshot_precedence_env_over_dotenv_over_secrets(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SHOP_LIMIT=8\nSHOP_TOKEN=from-dotenv\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "shop_limit").write_text("7\n")
    (secrets / "shop_token").write_text("from-secrets\n")
    (secrets / "SHOP_ALLOW_DEBT").write_text("true\n")
    snap = ms.SourceSnapshot(
        environ={"shop_limit": "9"},
        env_file=tmp_path / ".env",
        secrets_dir=secrets,
    )
    assert snap.get("SHOP_LIMIT") == ("9", "env")
    assert snap.get("SHOP_TOKEN") == ("from-dotenv", "dotenv")
    assert snap.get("SHOP_ALLOW_DEBT") == ("true", "secrets_dir")
    assert snap.get("SHOP_MISSING") == (None, None)
    assert snap.dotenv_parses == 1
    record = _validate_from(snap)
    assert record.instance.limit == 9  # type: ignore[attr-defined]
    assert record.origins["SHOP_TOKEN"] == "dotenv"
    assert record.origins["SHOP_REGIONS"] is None


def _validate_from(snap: ms.SourceSnapshot) -> ms.ModuleSettingsRecord:
    return ms.validate_module_settings(
        ShopSettings, package="shop_pkg", module="shop", kernel=_kernel(), snapshot=snap
    )


def test_secrets_dir_is_never_listed_and_read_once_per_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "shop_token").write_text("s3cret\n")
    listed: list[Path] = []
    real_iterdir = Path.iterdir

    def iterdir(self: Path) -> Any:
        listed.append(self)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    snap = ms.SourceSnapshot(environ={}, env_file=None, secrets_dir=tmp_path)
    for _ in range(3):
        _validate_from(snap)
    assert listed == []
    assert set(snap.secret_lookups.values()) == {1}
    assert snap.names() == set()  # secrets are never offered as names


# ------------------------------------------------------------- validation ----


def test_valid_model_with_json_and_nodecode_values() -> None:
    record = _validate(
        SHOP_LIMIT="5", SHOP_REGIONS='["eu", "us"]', SHOP_RAW_REGIONS='["x"],y'
    )
    instance = record.instance
    assert isinstance(instance, ShopSettings)
    assert instance.limit == 5
    assert instance.regions == ["eu", "us"]
    assert instance.raw_regions == ['["x"]', "y"]  # NoDecode: never JSON-decoded
    assert record.module == "shop"
    assert record.package == "shop_pkg"


def test_invalid_value_names_the_setting_with_the_kernel_prefix() -> None:
    with pytest.raises(ModuleSettingsError) as caught:
        _validate(SHOP_LIMIT="1000")
    message = str(caught.value)
    assert message.startswith(f"{INVALID_CONFIGURATION}module shop_pkg: SHOP_LIMIT:")
    assert "less than or equal to 100" in message
    # A SystemExit: the loader's per-module ``except Exception`` cannot swallow
    # it, so an optional module refuses the boot too (answer #29).
    assert not issubclass(ModuleSettingsError, Exception)
    assert caught.value.code == message


def test_production_check_reads_the_context() -> None:
    assert _validate(SHOP_ALLOW_DEBT="true").instance.allow_debt  # type: ignore[attr-defined]
    with pytest.raises(
        ModuleSettingsError, match="SHOP_ALLOW_DEBT=true is not allowed"
    ):
        _validate(kernel=_kernel("prod"), SHOP_ALLOW_DEBT="true")
    accepted = _validate(
        kernel=_kernel("prod", ("shop.debt_in_prod",)), SHOP_ALLOW_DEBT="true"
    )
    assert accepted.instance.allow_debt  # type: ignore[attr-defined]


def test_accepts_risk_refuses_an_undeclared_risk() -> None:
    class Asks(BaseModel):
        on: bool = Field(True, alias="ASKS_ON")

        @model_validator(mode="after")
        def _check(self, info: ValidationInfo) -> Asks:
            assert info.context is not None
            info.context["accepts_risk"]("nope")
            return self

    with pytest.raises(KeyError, match="declares no risk 'nope'"):
        _validate(Asks)


def test_kernel_view_is_read_only() -> None:
    seen: dict[str, Any] = {}

    class Peeks(BaseModel):
        @model_validator(mode="after")
        def _check(self, info: ValidationInfo) -> Peeks:
            assert info.context is not None
            kernel = info.context["kernel"]
            seen["environment"] = kernel.environment
            with pytest.raises(AttributeError, match="read-only"):
                kernel.environment = Environment.prod
            return self

    _validate(Peeks)
    assert seen["environment"] == Environment.dev


def test_only_plain_pydantic_models_are_declarable() -> None:
    class Reads(BaseSettings):
        x: int = 1

    with pytest.raises(TypeError, match="BaseSettings"):
        ms.check_declaration(Reads)
    with pytest.raises(TypeError, match="BaseModel class"):
        ms.check_declaration(object())


# --------------------------------------------- registration and settings_of ----


@pytest.fixture
def facade(monkeypatch: pytest.MonkeyPatch) -> CoreFacade:
    core = CoreFacade(FastAPI(), settings_obj=_kernel())
    monkeypatch.setattr(ms, "_PROCESS_SNAPSHOT", _snapshot(SHOP_LIMIT="3"))
    monkeypatch.setattr(module_settings_api, "get_active_core", lambda: core)
    return core


def test_declared_settings_are_readable_in_setup_and_after_commit(
    facade: CoreFacade,
) -> None:
    with facade.module_registration("shop_pkg") as api:
        instance = api.declare_settings(ShopSettings)
        assert settings_of(ShopSettings) is instance  # the setup reader
        assert facade.module_settings.get(ShopSettings) is None  # still a draft
    assert settings_of(ShopSettings).limit == 3
    (record,) = facade.module_settings.records()
    assert record.package == "shop_pkg"


def test_a_failed_registration_keeps_no_settings(facade: CoreFacade) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with facade.module_registration("shop_pkg") as api:
            api.declare_settings(ShopSettings)
            raise RuntimeError("boom")
    assert facade.module_settings.records() == []
    with pytest.raises(SettingsNotBoundError, match="no module loaded"):
        settings_of(ShopSettings)


def test_settings_of_with_no_application_bound_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module_settings_api, "get_active_core", lambda: None)
    with pytest.raises(SettingsNotBoundError, match="never at import"):
        settings_of(ShopSettings)


def test_override_settings_nests_and_restores(facade: CoreFacade) -> None:
    with facade.module_registration("shop_pkg") as api:
        api.declare_settings(ShopSettings)
    with override_settings(ShopSettings, limit=50) as outer:
        assert settings_of(ShopSettings) is outer
        with override_settings(ShopSettings, allow_debt=True):
            assert settings_of(ShopSettings).limit == 50
            assert settings_of(ShopSettings).allow_debt
        assert not settings_of(ShopSettings).allow_debt
    assert settings_of(ShopSettings).limit == 3
    with (
        pytest.raises(TypeError, match="no field"),
        override_settings(ShopSettings, limt=1),
    ):
        pass


def test_override_settings_without_an_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module_settings_api, "get_active_core", lambda: None)
    with override_settings(ShopSettings, limit=7):
        assert settings_of(ShopSettings).limit == 7


def test_validate_settings_is_the_loaders_validation() -> None:
    assert validate_settings(ShopSettings, {"SHOP_LIMIT": "4"}).limit == 4
    with pytest.raises(ModuleSettingsError, match="SHOP_ALLOW_DEBT=true is not"):
        validate_settings(ShopSettings, {"SHOP_ALLOW_DEBT": "true"}, environment="prod")
    accepted = validate_settings(
        ShopSettings,
        {"SHOP_ALLOW_DEBT": "true"},
        environment="prod",
        accepted_risks=["shop_pkg.debt_in_prod"],
        package="shop_pkg",
    )
    assert accepted.allow_debt


# --------------------------------------------------------- post-load check ----


def _records(*pairs: tuple[str, type[BaseModel]], **env: str) -> list[Any]:
    return [
        ms.validate_module_settings(
            model, package=pkg, module=pkg, kernel=_kernel(), snapshot=_snapshot(**env)
        )
        for pkg, model in pairs
    ]


def test_collisions_refuse_naming_both_owners() -> None:
    class KernelName(BaseModel):
        level: str = Field("INFO", alias="LOG_LEVEL")

    records = _records(
        ("shop", ShopSettings), ("other", OtherSettings), ("k", KernelName)
    )
    problems = ms.post_load_check(records, _kernel(), _snapshot()).problems
    assert any(
        p.startswith(f"{ms.COLLISION_CODE}: LOG_LEVEL is declared by the kernel")
        for p in problems
    )
    assert any(
        f"{ms.COLLISION_CODE}: SHOP_LIMIT is declared by module other and by module shop"
        in p
        for p in problems
    )


def test_removed_module_setting_refuses_a_live_value_and_warns_inert() -> None:
    records = _records(("shop", ShopSettings))
    live = ms.post_load_check(records, _kernel(), _snapshot(SHOP_LEGACY="true"))
    assert live.problems == [
        "module shop: SHOP_LEGACY was removed in 0.2 and SHOP_LEGACY=true is no "
        "longer supported: the legacy flow is gone; drop it from the environment"
    ]
    inert = ms.post_load_check(records, _kernel(), _snapshot(SHOP_LEGACY="false"))
    assert inert.problems == []
    assert inert.warnings and "SHOP_LEGACY was removed in 0.2" in inert.warnings[0]


def test_production_typo_check_runs_over_module_names() -> None:
    records = _records(("shop", ShopSettings))
    near = _snapshot(SHOP_ALLOW_DEBTS="true")
    assert ms.post_load_check(records, _kernel(), near).problems == []  # dev
    (problem,) = ms.post_load_check(records, _kernel("prod"), near).problems
    assert problem == (
        "Unknown setting SHOP_ALLOW_DEBTS; did you mean SHOP_ALLOW_DEBT "
        "(declared by module shop)?"
    )


class ShopEuNeighbour(BaseModel):
    """A loaded ``shop`` whose name sits one letter from a variable of the
    ``shop_eu`` module another service loads."""

    region: str = Field("eu", alias="SHOP_EU_REGION")


def test_production_typo_check_ignores_an_absent_modules_variables() -> None:
    """Answer #25: a variable of a module known in the deployment but not
    loaded here (``shop_eu``) never refuses the boot as a near-miss of a
    loaded module's name; a near-miss under the loaded module's own prefix
    still does, as does one of a module nobody knows."""
    records = _records(("shop", ShopEuNeighbour))
    prod = _kernel("prod")
    foreign = _snapshot(SHOP_EU_REGIONS="eu-west")  # shop_eu's own setting
    (problem,) = ms.post_load_check(records, prod, foreign, known_modules=()).problems
    assert problem.startswith("Unknown setting SHOP_EU_REGIONS; did you mean")
    assert (
        ms.post_load_check(
            records, prod, foreign, known_modules=("shop", "shop_eu")
        ).problems
        == []
    )
    # the longest known prefix decides: a loaded shop_eu owns SHOP_EU_*
    # even where a module named shop is absent
    records_eu = _records(("shop_eu", ShopEuNeighbour))
    (problem,) = ms.post_load_check(
        records_eu, prod, foreign, known_modules=("shop", "shop_eu")
    ).problems
    assert "did you mean SHOP_EU_REGION (declared by module shop_eu)" in problem
    # a typo of the loaded module's own name is still refused
    typo = _snapshot(SHOP_ALLOW_DEBTS="true")
    (problem,) = ms.post_load_check(
        _records(("shop", ShopSettings)), prod, typo, known_modules=("shop_eu",)
    ).problems
    assert "did you mean SHOP_ALLOW_DEBT" in problem


def test_production_typo_check_counts_a_loaded_module_without_settings() -> None:
    """A loaded module that declares no settings still owns its prefix: the
    near-miss under it refuses the boot instead of passing as the variable of
    an absent module (the loader passes every loaded module)."""
    records = _records(("shop", ShopEuNeighbour))
    prod = _kernel("prod")
    foreign = _snapshot(SHOP_EU_REGIONS="eu-west")
    known = ("shop", "shop_eu")
    assert (
        ms.post_load_check(records, prod, foreign, known_modules=known).problems == []
    )
    (problem,) = ms.post_load_check(
        records, prod, foreign, known_modules=known, loaded_modules=("shop", "shop_eu")
    ).problems
    assert problem.startswith("Unknown setting SHOP_EU_REGIONS; did you mean")


def test_known_modules_are_the_installed_entry_points_and_the_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.descriptors import module_names

    assert ms.MODULES_ENTRY_POINT_GROUP == module_names.ENTRY_POINT_GROUP
    installed = {m.name for m in module_names.installed_modules()}
    kernel = Settings.model_construct(
        environment=Environment.prod,
        sandloom_modules=["app.modules.shop_eu", "sandloom_identity"],
        sandloom_accepted_risks=[],
    )
    # an import path is the module's name where an entry point provides it
    # (sandloom_identity is identity), else its last segment (app.modules.shop_eu)
    assert ms.known_module_names(kernel) == installed | {"shop_eu"}
    assert "sandloom_identity" not in ms.known_module_names(kernel)
    # the default reads them: shop_eu, named by the composition, is absent here
    records = _records(("shop", ShopEuNeighbour))
    foreign = _snapshot(SHOP_EU_REGIONS="eu-west")
    assert ms.post_load_check(records, kernel, foreign).problems == []


def test_semantic_override_is_reported_unless_its_risk_is_accepted() -> None:
    records = _records(("shop", ShopSettings), SHOP_ALLOW_DEBT="true")
    (item,) = ms.post_load_check(records, _kernel(), _snapshot()).overrides
    assert (item.setting, item.value, item.profile, item.explicit) == (
        "SHOP_ALLOW_DEBT",
        True,
        "dev",
        True,
    )
    assert item.message() == (
        "CONFIG OVERRIDE: SHOP_ALLOW_DEBT=true weakens the dev profile value false"
    )
    accepted = _kernel("dev", ("shop.debt_in_prod",))
    assert ms.post_load_check(records, accepted, _snapshot()).overrides == []


def test_loader_check_refuses_and_logs(
    facade: CoreFacade, caplog: pytest.LogCaptureFixture
) -> None:
    plugin_loader._check_module_settings(facade)  # nothing declared: no-op
    for record in _records(("shop", ShopSettings), SHOP_ALLOW_DEBT="true"):
        facade.module_settings.add(record)
    with caplog.at_level(logging.INFO, logger="sandloom.settings"):
        plugin_loader._check_module_settings(facade)
    assert "CONFIG OVERRIDE: SHOP_ALLOW_DEBT=true" in caplog.text
    for record in _records(("other", OtherSettings)):
        facade.module_settings.add(record)
    with pytest.raises(ModuleSettingsError) as caught:
        plugin_loader._check_module_settings(facade)
    assert str(caught.value).startswith(f"{INVALID_CONFIGURATION}{ms.COLLISION_CODE}")


def test_define_module_validates_settings_before_setup(facade: CoreFacade) -> None:
    order: list[str] = []

    def setup(core: Any) -> None:
        order.append(f"setup limit={settings_of(ShopSettings).limit}")

    register = define_module(
        "shop_pkg", settings=ShopSettings, setup=setup, router=None, events=None
    )
    assert register.__dict__["sandloom_settings"] is ShopSettings
    with facade.module_registration("shop_pkg") as api:
        register(api)
    assert order == ["setup limit=3"]
