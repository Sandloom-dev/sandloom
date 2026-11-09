"""The platform settings view lists a module's declared setting only where that
module is loaded (ADR-0015, edge row B-C9, P10)."""

from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel, Field

from sandloom.core.admin.api import platform
from sandloom.core.config.module_settings import (
    ModuleSettingsRecord,
    ModuleSettingsRegistry,
)


class _Billing(BaseModel):
    billing_provider: str = Field("fake", alias="BILLING_PROVIDER")


class _Identity(BaseModel):
    refresh_token_expire_days: int = Field(14, alias="REFRESH_TOKEN_EXPIRE_DAYS")


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


def test_the_view_names_billing_provider_only_where_billing_is_loaded() -> None:
    assert "billing_provider" in platform._DECLARED_SAFE_SETTINGS
    assert platform._declared_setting(_core(_Billing), "billing_provider") == (
        True,
        "fake",
    )
    assert platform._declared_setting(_core(_Identity), "billing_provider") == (
        False,
        None,
    )
    assert platform._declared_setting(_core(), "billing_provider") == (False, None)


def test_the_kernel_view_lists_no_module_setting_of_its_own() -> None:
    assert "billing_provider" not in platform._SAFE_SETTINGS
    assert "refresh_token_expire_days" not in platform._SAFE_SETTINGS
