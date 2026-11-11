"""The platform settings view lists a module's declared console settings only
where that module is loaded (ADR-0015, edge row B-C9, P10): the view names no
module's setting; each module's ``console`` declaration does."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from pydantic import BaseModel, Field

from sandloom.core.admin.api import platform
from sandloom.core.config.module_settings import (
    ModuleSettingsRecord,
    ModuleSettingsRegistry,
)


class _Billing(BaseModel):
    billing_provider: str = Field("fake", alias="BILLING_PROVIDER")
    stripe_key: str = Field("sk", alias="STRIPE_KEY")
    console: ClassVar[tuple[str, ...]] = ("billing_provider",)


class _Identity(BaseModel):
    refresh_token_expire_days: int = Field(14, alias="REFRESH_TOKEN_EXPIRE_DAYS")
    console: ClassVar[tuple[str, ...]] = ("refresh_token_expire_days",)


def _core(*models: type[BaseModel]) -> SimpleNamespace:
    registry = ModuleSettingsRegistry()
    for model in models:
        registry.add(
            ModuleSettingsRecord(
                package=model.__name__,
                module=model.__name__.lower(),
                model=model,
                instance=model(),
            )
        )
    return SimpleNamespace(module_settings=registry)


def test_the_view_lists_a_declared_console_setting_only_where_its_module_is_loaded() -> (
    None
):
    assert platform._declared_settings(_core(_Billing)) == [
        ("billing_provider", "fake")
    ]
    assert platform._declared_settings(_core(_Identity)) == [
        ("refresh_token_expire_days", 14)
    ]
    assert platform._declared_settings(_core()) == []


def test_only_declared_console_fields_are_listed() -> None:
    keys = [k for k, _ in platform._declared_settings(_core(_Billing, _Identity))]
    assert keys == ["billing_provider", "refresh_token_expire_days"]
    assert "stripe_key" not in keys


def test_the_kernel_view_lists_no_module_setting_of_its_own() -> None:
    assert "billing_provider" not in platform._SAFE_SETTINGS
    assert "refresh_token_expire_days" not in platform._SAFE_SETTINGS
