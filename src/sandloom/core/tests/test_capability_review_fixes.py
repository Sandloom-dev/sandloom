"""Regression tests for the capability-runtime review fixes (P0/P1).

Covers: strict driver registration; build-free requirement validation;
publish_event refusing a backend without an outbox; atomic module-registration
preflight; feature corrections.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from sandloom.core.facade import CoreFacade
from sandloom.core.infra.capabilities import DATABASE_CAPABILITY, EVENTBUS_CAPABILITY
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import DatabaseProvider
from sandloom.core.infra.registry import (
    FeatureUnsupportedError,
    InfraRegistry,
    UnknownCapabilityError,
)


class _FakeDb(DatabaseProvider):
    def uow_factory(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def features(self) -> frozenset[str]:
        return frozenset({"transactions"})


# --- P1: strict driver registration ----------------------------------------
def test_strict_rejects_driver_without_spec() -> None:
    reg = InfraRegistry(strict_capabilities=True)
    with pytest.raises(UnknownCapabilityError):
        reg.register_driver("graph_store", "neo4j", lambda cfg: None)  # type: ignore[arg-type,return-value]


def test_strict_rejects_driver_feature_not_in_spec() -> None:
    reg = InfraRegistry(strict_capabilities=True)
    reg.register_capability(DATABASE_CAPABILITY)
    with pytest.raises(FeatureUnsupportedError):
        reg.register_driver(
            "database",
            "fake",
            lambda cfg: _FakeDb(name=cfg.name, driver=cfg.driver, options={}),
            features=frozenset({"time_travel"}),
        )


# --- P1: build-free requirement validation (factory never called) ----------
def test_validate_requirement_does_not_build() -> None:
    built = {"n": 0}

    def _exploding_factory(cfg):  # type: ignore[no-untyped-def]
        built["n"] += 1
        raise RuntimeError("network!")

    reg = InfraRegistry(
        config={
            "database": {
                "default": InstanceConfig(
                    capability="database", name="default", driver="fake", options={}
                )
            }
        }
    )
    reg.register_capability(DATABASE_CAPABILITY)
    reg.register_driver(
        "database", "fake", _exploding_factory, features=frozenset({"transactions"})
    )
    reg.validate_requirement(
        "database", "default", features=frozenset({"transactions"})
    )
    assert built["n"] == 0
    with pytest.raises(FeatureUnsupportedError):
        reg.validate_requirement("database", "default", features=frozenset({"audit"}))
    assert built["n"] == 0


# --- P1: feature corrections ------------------------------------------------
def test_database_capability_has_audit_feature() -> None:
    assert "audit" in DATABASE_CAPABILITY.features


def test_eventbus_capability_has_delivery_features() -> None:
    assert {"durable", "at_least_once", "volatile"} <= EVENTBUS_CAPABILITY.features


# --- P1: publish_event refuses a backend with no outbox --------------------
def test_outbox_store_refuses_unsupported_backend() -> None:
    from sandloom.core.infra.registry import TransactionalOutboxUnsupportedError
    from sandloom.core.unit_of_work.scope import (
        _OUTBOX_STORE_KEY,
        _UNSUPPORTED,
        _outbox_store,
    )

    fake_session = SimpleNamespace(info={_OUTBOX_STORE_KEY: _UNSUPPORTED})
    with pytest.raises(TransactionalOutboxUnsupportedError):
        _outbox_store(fake_session)  # type: ignore[arg-type]


def test_audit_store_skips_unsupported_backend() -> None:
    from sandloom.core.unit_of_work.scope import (
        _AUDIT_STORE_KEY,
        _UNSUPPORTED,
        _audit_store,
    )

    fake_session = SimpleNamespace(info={_AUDIT_STORE_KEY: _UNSUPPORTED})
    assert _audit_store(fake_session) is None  # type: ignore[arg-type]


# --- P0-3: atomic module-registration preflight ----------------------------
def test_draft_preflight_rejects_duplicate_service_atomically() -> None:
    from sandloom.core.module_api import (
        ModuleRegistrationConflict,
        ModuleRegistrationDraft,
    )

    core = CoreFacade(app=FastAPI())
    core.register_service("billing.psp.v1", object())

    draft = ModuleRegistrationDraft()
    draft.services.append(("billing.psp.v1", object()))  # duplicate of live
    draft.services.append(("billing.other.v1", object()))

    with pytest.raises(ModuleRegistrationConflict):
        draft.commit(core)
    assert not core.service_registry.contains("billing.other.v1")


def test_draft_preflight_rejects_within_draft_duplicate() -> None:
    from sandloom.core.module_api import (
        ModuleRegistrationConflict,
        ModuleRegistrationDraft,
    )

    core = CoreFacade(app=FastAPI())
    draft = ModuleRegistrationDraft()
    draft.services.append(("dup.svc.v1", object()))
    draft.services.append(("dup.svc.v1", object()))
    with pytest.raises(ModuleRegistrationConflict):
        draft.preflight(core)


def test_draft_commit_applies_when_clean() -> None:
    from sandloom.core.module_api import ModuleRegistrationDraft

    core = CoreFacade(app=FastAPI())
    draft = ModuleRegistrationDraft()
    draft.services.append(("clean.svc.v1", object()))
    draft.commit(core)
    assert core.service_registry.contains("clean.svc.v1")
