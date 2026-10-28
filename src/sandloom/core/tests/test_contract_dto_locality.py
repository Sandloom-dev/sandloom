"""Contract DTOs: authored in the provider, copied for every caller (C012).

A remote caller rebuilds a pydantic model / Enum named in a contract signature
only from a class its own process holds: the class itself when imported, else
a loaded generated copy (``sandloom generate`` writes one per provider DTO into
the shared DTO package); otherwise the RPC result degrades to a ``dict`` (an
Enum to a ``Literal`` of its values). A provider's own DTO is therefore
allowed (the generator copies it); registration refuses what no copy can come
from: another module's class, or a function-local one.

Deliberately no ``from __future__ import annotations``: the contract methods
below are annotated with dynamically created classes.
"""

from enum import Enum
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, create_model

from sandloom.core import contract_wire
from sandloom.core.config.settings import settings
from sandloom.core.contract_wire import ContractTypeError
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.descriptors.contract_refs import dto_copies, dto_copy
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import ModuleRegistrationConflict, contract_method
from sandloom.shared.contracts.tenancy_dto import MemberState

# A DTO "defined" inside the provider module's own package.
LedgerEntry = create_model(
    "LedgerEntry", __module__="sandloom.ledger.schemas", amount=(int, ...)
)
# A DTO in a package the deployment declares shared (CONTRACT_DTO_PACKAGES).
PublicEntry = create_model(
    "PublicEntry", __module__="sandloom.ledger_public.dto", amount=(int, ...)
)


class LedgerKind(str, Enum):
    DEBIT = "debit"


LedgerKind.__module__ = "sandloom.ledger.enums"


class Wrapper(BaseModel):
    """A DTO outside every module package that NESTS a module-owned enum."""

    kind: LedgerKind


class _LeakyLedger:
    @classmethod
    @contract_method
    async def entry(cls, amount: int) -> LedgerEntry:
        return LedgerEntry(amount=amount)


class _NestedLeak:
    @classmethod
    @contract_method
    async def wrap(cls) -> Wrapper:
        return Wrapper(kind=LedgerKind.DEBIT)


class _PublicLedger:
    @classmethod
    @contract_method
    async def entry(cls, amount: int) -> PublicEntry:
        return PublicEntry(amount=amount)


class _SharedLedger:
    @classmethod
    @contract_method
    async def state(cls) -> MemberState | None:
        return None


def test_remote_decode_degrades_when_the_dto_is_not_importable() -> None:
    """The defect the check prevents: local returns the model, RPC a dict."""
    descriptor = contract_wire.type_descriptor(LedgerEntry)
    assert descriptor["ref"] == "sandloom.ledger.schemas:LedgerEntry"
    # sandloom.ledger.schemas is not an importable module in the caller process.
    remote_annotation = contract_wire.annotation_from_descriptor(descriptor)
    assert remote_annotation == dict[str, Any]
    local = contract_wire.wire_plan(LedgerEntry).local(LedgerEntry(amount=3))
    remote = contract_wire.wire_plan(remote_annotation).decode(
        contract_wire.encode(LedgerEntry(amount=3)), contract_wire.CODEC_TAGGED
    )
    assert isinstance(local, LedgerEntry)
    assert remote == {"amount": 3} and not isinstance(remote, LedgerEntry)
    # A DTO from a shared package resolves to the same class remotely.
    shared = contract_wire.annotation_from_descriptor(
        contract_wire.type_descriptor(MemberState)
    )
    assert shared is MemberState


def test_remote_decode_resolves_a_loaded_generated_copy() -> None:
    """A provider ref the caller cannot import resolves to its registered
    generated copy (so the typed codec and the class survive RPC)."""
    origin = "sandloom.copytest.schemas:Receipt"
    Receipt = create_model("Receipt", __module__="sandloom.shared.x", amount=(int, ...))
    dto_copy(origin)(Receipt)
    assert dto_copies(origin)[-1] is Receipt
    descriptor = {"k": "model", "ref": origin}
    assert contract_wire.annotation_from_descriptor(descriptor) is dto_copies(origin)[0]


def test_module_contract_with_own_package_dto_is_accepted() -> None:
    """The norm: the provider authors its DTO, ``sandloom generate`` copies it."""
    core = CoreFacade(app=FastAPI())
    with core.module_registration("sandloom.ledger") as api:
        api.register_service("ledger.entries.v1", _LeakyLedger)
    assert core.service_registry.contains("ledger.entries.v1")


def test_a_function_local_dto_is_refused() -> None:
    def build() -> type:
        class Local(BaseModel):
            amount: int

        class _Provider:
            @classmethod
            @contract_method
            async def entry(cls, amount: int) -> Local:
                return Local(amount=amount)

        return _Provider

    core = CoreFacade(app=FastAPI())
    with pytest.raises(ModuleRegistrationConflict) as exc_info:
        with core.module_registration("sandloom.ledger") as api:
            api.register_service("ledger.local.v1", build())
    message = str(exc_info.value)
    assert "[SANDLOOM-C012] contract 'ledger.local.v1' entry(return)" in message
    assert "is defined in a function / __main__" in message
    assert "sandloom generate" in message
    assert not core.service_registry.contains("ledger.local.v1")


def test_nested_enum_of_another_loaded_module_is_refused() -> None:
    core = CoreFacade(app=FastAPI())
    core.module_descriptors = [ModuleDescriptor(name="sandloom.ledger", version="1")]
    core._registering_module = "sandloom.reports"  # a module's direct registration
    with pytest.raises(ContractTypeError) as exc_info:
        core.register_service("reports.wrap.v1", _NestedLeak)
    message = str(exc_info.value)
    assert (
        "sandloom.ledger.enums:LedgerKind (Enum) lives in another module's package "
        "'sandloom.ledger'" in message
    )
    assert "generated copy in the shared DTO package" in message


def test_shared_and_declared_dto_packages_are_accepted() -> None:
    core = CoreFacade(
        app=FastAPI(),
        settings_obj=settings.model_copy(
            update={"contract_dto_packages": ["sandloom.ledger_public"]}
        ),
    )
    with core.module_registration("sandloom.ledger") as api:
        api.register_service("ledger.public.v1", _PublicLedger)
        api.register_service("ledger.shared.v1", _SharedLedger)
    assert core.service_registry.contains("ledger.public.v1")
    assert core.service_registry.contains("ledger.shared.v1")


def test_kernel_level_registration_is_exempt() -> None:
    """No registering module (framework tests, auto-exposed contracts)."""
    core = CoreFacade(app=FastAPI())
    core.register_service("ledger.entries.v1", _LeakyLedger)
    assert core.service_registry.contains("ledger.entries.v1")
