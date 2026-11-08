"""The token issuer ends its sessions on a block, or says it holds none
(ADR-0012 decision 5, owner's confirmation A8, SANDLOOM-B035).

An ``issues_tokens=True`` module registers a durable consumer of
``core.auth.user_blocked.v1``, or declares ``refresh_sessions=False`` — never
inferred from an absent consumer — and startup verifies that declaration
against its refresh routes, session tables and consumers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from sandloom.core.app_factory import revocation_applier as applier
from sandloom.core.cli.generated import module_surface
from sandloom.core.config.common import ConfigurationError
from sandloom.core.consumers.callbacks import EventConsumerDescriptor
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.loader.manifest import (
    ModelEntry,
    ModuleManifest,
    RouteEntry,
)

ISSUER = "acme.modules.accounts"
BLOCKED = "core.auth.user_blocked.v1"


async def _noop(_session: Any, _envelope: Any) -> None:
    return None


def _consumer(module: str = "accounts", *, durable: bool = True) -> Any:
    return EventConsumerDescriptor(
        name="sessions@user_blocked",
        channel=BLOCKED,
        handler=_noop,
        module=module,
        durable=durable,
    )


def _issuer(**kwargs: Any) -> ModuleDescriptor:
    return ModuleDescriptor(name=ISSUER, version="1", issues_tokens=True, **kwargs)


@dataclass
class _Manifest:
    modules: list[ModuleManifest] = field(default_factory=list)


def _manifest(routes: tuple[str, ...] = (), tables: tuple[str, ...] = ()) -> Any:
    return _Manifest(
        [
            ModuleManifest(
                name=ISSUER,
                version="1",
                routes=tuple(
                    RouteEntry(module=ISSUER, method="POST", path=p, name=p)
                    for p in routes
                ),
                models=tuple(
                    ModelEntry(module=ISSUER, table=t, tenant_scoped=False)
                    for t in tables
                ),
                issues_tokens=True,
            )
        ]
    )


def test_an_issuer_without_a_block_consumer_is_refused() -> None:
    problems = applier.issuer_obligation_problems([_issuer()], [], _manifest())
    assert len(problems) == 1
    assert ISSUER in problems[0] and BLOCKED in problems[0]
    assert "refresh_sessions=False" in problems[0]  # says the other way out


def test_a_volatile_block_consumer_does_not_count() -> None:
    problems = applier.issuer_obligation_problems(
        [_issuer()], [_consumer(durable=False)], _manifest()
    )
    assert problems


@pytest.mark.parametrize("owner", ["accounts", ISSUER])
def test_an_issuer_with_a_durable_block_consumer_passes(owner: str) -> None:
    assert not applier.issuer_obligation_problems(
        [_issuer()], [_consumer(owner)], _manifest(routes=("/auth/refresh",))
    )


def test_another_modules_block_consumer_does_not_count() -> None:
    assert applier.issuer_obligation_problems(
        [_issuer()], [_consumer("billing")], _manifest()
    )


def test_a_stateless_issuer_passes_when_nothing_contradicts_it() -> None:
    """``refresh_sessions=False`` with only a login route and a user table."""
    assert not applier.issuer_obligation_problems(
        [_issuer(refresh_sessions=False)],
        [],
        _manifest(routes=("/auth/login", "/auth/me"), tables=("accounts_users",)),
    )


@pytest.mark.parametrize(
    "routes,tables,consumers,named",
    [
        (("/auth/refresh",), (), [], "/auth/refresh"),
        (("/auth/token/refresh-session",), (), [], "refresh-session"),
        ((), ("refresh_tokens",), [], "refresh_tokens"),
        ((), ("accounts_sessions",), [], "accounts_sessions"),
        ((), (), [_consumer()], "sessions@user_blocked"),
    ],
)
def test_a_contradicted_stateless_declaration_is_refused(
    routes: tuple[str, ...],
    tables: tuple[str, ...],
    consumers: list[Any],
    named: str,
) -> None:
    problems = applier.issuer_obligation_problems(
        [_issuer(refresh_sessions=False)], consumers, _manifest(routes, tables)
    )
    assert len(problems) == 1
    assert "refresh_sessions=False" in problems[0]
    assert named in problems[0]


def test_a_module_that_issues_no_tokens_owes_nothing() -> None:
    plain = ModuleDescriptor(name="acme.modules.notes", version="1")
    assert not applier.issuer_obligation_problems([plain], [], _manifest())


def test_the_refusal_is_an_invalid_configuration_naming_b035(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(ConfigurationError, match="SANDLOOM-B035") as refused:
        applier.enforce_issuer_obligation(
            [_issuer()], [], _manifest(), log=logging.getLogger("t")
        )
    assert "acme.modules.accounts" in str(refused.value)
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_the_declaration_is_recorded_in_the_module_snapshot() -> None:
    """``module.snapshot.json`` records an issuer's declaration (only an
    issuer's: other modules' snapshots carry no ``issuer`` key)."""
    stateless = ModuleManifest(
        name=ISSUER, version="1", issues_tokens=True, refresh_sessions=False
    )
    assert module_surface(stateless)["issuer"] == {"refresh_sessions": False}
    stateful = ModuleManifest(name=ISSUER, version="1", issues_tokens=True)
    assert module_surface(stateful)["issuer"] == {"refresh_sessions": True}
    assert "issuer" not in module_surface(ModuleManifest(name="n", version="1"))


@pytest.mark.parametrize("level", ["sdk", "core"])
def test_define_module_carries_the_declaration(level: str) -> None:
    from fastapi import FastAPI

    from sandloom.core.facade import CoreFacade
    from sandloom.core.module_api import ModuleAPI, ModuleRegistrationDraft
    from sandloom.core.module_api import define_module as core_define
    from sandloom.sdk import define_module

    define = define_module if level == "sdk" else core_define
    register = define(
        "sandloom.core.tests",
        issues_tokens=True,
        refresh_sessions=False,
        router=None,
        events=None,
    )
    api = ModuleAPI(CoreFacade(app=FastAPI()), draft=ModuleRegistrationDraft())
    descriptor = register(api)
    assert descriptor.issues_tokens is True
    assert descriptor.refresh_sessions is False
    default = define("sandloom.core.tests", router=None, events=None)(
        ModuleAPI(CoreFacade(app=FastAPI()), draft=ModuleRegistrationDraft())
    )
    assert default.refresh_sessions is True  # never inferred: the default holds


def test_doctor_shows_the_issuer_declaration() -> None:
    from types import SimpleNamespace

    from sandloom.core.cli.doctor import Report, check_modules

    core = SimpleNamespace(
        loaded_modules=[ISSUER],
        service_registry=None,
        module_descriptors=[_issuer(refresh_sessions=False)],
    )
    report = Report()
    check_modules(report, core)
    (shown,) = [c for c in report.checks if c.name == f"{ISSUER} issues tokens"]
    assert "refresh_sessions=False" in shown.message
    assert shown.details["refresh_sessions"] is False


def test_diagnostics_show_the_issuer_declaration() -> None:
    """``/diagnostics/core`` (the console's source) lists each issuer's
    declaration from the load summary."""
    from sandloom.core.descriptors import ModuleLoadSummary

    summary = ModuleLoadSummary(
        loaded=[
            _issuer(refresh_sessions=False),
            ModuleDescriptor(name="acme.modules.notes", version="1"),
        ]
    )
    assert summary.to_dict()["issuers"] == {ISSUER: {"refresh_sessions": False}}
