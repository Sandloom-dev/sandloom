"""Reads its settings at import: the import audit must refuse it."""

from pydantic import BaseModel, Field

from sandloom.sdk import define_module
from sandloom.sdk.module import settings_of  # sandloom: advanced settings


class BadSettings(BaseModel):
    limit: int = Field(5, alias="KB_INVENTORY_BAD_IMPORT_LIMIT")


LIMIT = settings_of(BadSettings).limit  # the defect under test

register = define_module(__name__, version="0.1.0", settings=BadSettings, router=None)
