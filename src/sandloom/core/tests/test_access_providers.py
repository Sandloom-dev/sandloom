"""Ownership of access-bearing contract names (ADR-0014 decision 6; KB-13).

A contract the authorization layer asks to decide access is registered
in-process only by the module its first segment names, or by the module
``create_app(access_providers=...)`` assigns, checked by its real identity
(owner confirmation X3): the loaded import path, not only the short name.
Which names bear access is read from the declarations of this process (the
assignment's keys, the contracts of the imported generated access checks, an
implementation carrying ``@access_check`` methods), never from a list in the
kernel. Every other contract name keeps the first-registrant rule.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI

from sandloom.core.app_factory import access_providers as _access
from sandloom.core.cli.generated import render_stub
from sandloom.core.config.common import ConfigurationError
from sandloom.core.error_codes import code_for_exception
from sandloom.core.facade import CoreFacade
from sandloom.core.facade._services import ForeignOwnerRegistration
from sandloom.core.lifecycle import CriticalStartupError, run_startup
from sandloom.core.module_api import contract_method
from sandloom.core.module_api._registration import ModuleRegistrationConflict
from sandloom.core.tests._entry_points_support import installed_entry_points
from sandloom.sdk import contract
from sandloom.shared.authz import Category, From, access_check
from sandloom.shared.authz import contract_check as _contract_check

LOG = logging.getLogger("test.access_providers")
MEMBERSHIP = "tenancy.membership.v1"
SEATS = "acme.seats.v1"


class _Membership:
    """A third-party implementation of the membership contract, carrying
    its access check as tenancy's provider does: the implementation alone
    makes the name access-bearing (ADR-0014 decision 6), with no consumer's
    generated atom imported (a provider-only process)."""

    @access_check(
        "OrgRole",
        bind={"org_id": From.org(), "user_id": From.subject_id()},
        values={"minimum": ["member", "admin", "owner"]},
    )
    @classmethod
    @contract_method
    async def has_role_at_least(cls, org_id: UUID, user_id: UUID, minimum: str) -> bool:
        """Answer anything."""
        return True


class _Seats:
    """A provider of a contract only a generated access check names."""

    @classmethod
    @contract_method
    async def has_seat(cls, account_type: str, account_id: UUID) -> bool:
        """Answer anything."""
        return True


@contract(SEATS)
class _SeatsContract:
    """The declared contract a consumer's ``sandloom generate`` renders from."""

    @access_check(
        "HasSeat",
        bind={"account_type": From.account_type(), "account_id": From.account_id()},
        category=Category.PAYMENT_REQUIRED,
    )
    async def has_seat(self, account_type: str, account_id: UUID) -> bool:
        """True while the account holds a seat."""
        return True


@pytest.fixture(autouse=True)
def _standard_entry_points() -> Iterator[None]:
    """``tenancy`` is the module ``sandloom_tenancy`` of the distribution
    ``sandloom-tenancy`` by its entry point, installed or not."""
    with installed_entry_points():
        yield


@pytest.fixture
def seats_atoms(tmp_path: Path) -> Iterator[Callable[[], Any]]:
    """Imports the consumer's generated ``HasSeat`` atom on call, as a
    consumer's stub is imported: from then on ``acme.seats.v1`` bears access
    (KB-13 source (c)). Nothing is left behind for the next test."""
    imported: list[Any] = []

    def load() -> Any:
        path = tmp_path / "acme_seats_v1.py"
        path.write_text(render_stub(SEATS, _SeatsContract))
        spec = importlib.util.spec_from_file_location("_kb34a_acme_seats_v1", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        imported.append(module)
        return module.HasSeat

    yield load
    for module in imported:
        sys.modules.pop(module.__name__, None)
        _contract_check._GENERATED.discard(module.HasSeat)
    assert SEATS not in _contract_check.generated_access_contracts()


def _core(access_providers: dict[str, str] | None = None) -> CoreFacade:
    """A facade with the root's assignment bound, as ``create_app`` does
    before the modules load."""
    core = CoreFacade(app=FastAPI())
    _access.install_access_providers(core, access_providers=access_providers, log=LOG)
    return core


def _register(core: CoreFacade, module: str, contract: str, impl: Any) -> None:
    with core.module_registration(module) as api:
        api.register_service(contract, impl)


# ---------------------------------------------------------------- in-process


def test_an_unassigned_third_party_provider_is_refused() -> None:
    """``acme_orgs`` serving tenancy's membership contract with no assignment."""
    core = _core()
    with pytest.raises(ForeignOwnerRegistration, match="SANDLOOM-B034") as exc:
        _register(core, "acme_orgs", MEMBERSHIP, _Membership)
    assert code_for_exception(exc.value) == "SANDLOOM-B034"
    assert "access_providers" in str(exc.value)
    assert not core.service_registry.contains(MEMBERSHIP)


def test_an_assigned_third_party_provider_is_accepted() -> None:
    """The composition root keeps third-party providers possible (owner #2)."""
    core = _core({MEMBERSHIP: "acme_orgs"})
    _register(core, "acme_orgs", MEMBERSHIP, _Membership)
    assert core.contract_owner(MEMBERSHIP) == "acme_orgs"
    assert core.access_provider_problems() == []


def test_an_app_local_module_of_the_assigned_name_is_refused() -> None:
    """X3: the provider is the module from the assigned path, not its name."""
    core = _core({MEMBERSHIP: "acme_orgs"})
    with pytest.raises(ForeignOwnerRegistration, match="'acme_orgs'"):
        _register(core, "myapp.modules.acme_orgs", MEMBERSHIP, _Membership)
    assert not core.service_registry.contains(MEMBERSHIP)


def test_the_first_segment_module_from_its_distribution_is_accepted() -> None:
    """The ordinary case: the installed tenancy module provides its contract."""
    core = _core()
    _register(core, "sandloom_tenancy", MEMBERSHIP, _Membership)
    assert core.contract_owner(MEMBERSHIP) == "sandloom_tenancy"


def test_a_same_named_module_from_another_path_is_refused() -> None:
    """X3: an app module named ``tenancy`` is not the installed tenancy."""
    core = _core()
    with pytest.raises(ForeignOwnerRegistration, match="sandloom_tenancy"):
        _register(core, "myapp.modules.tenancy", MEMBERSHIP, _Membership)


def test_an_app_module_of_the_first_segment_with_no_distribution_is_accepted(
    seats_atoms: Callable[[], Any],
) -> None:
    """A first segment no installed distribution names is the app's module."""
    seats_atoms()
    core = _core()
    _register(core, "myapp.modules.acme", SEATS, _Seats)
    assert core.contract_owner(SEATS) == "myapp.modules.acme"


def test_an_assignment_is_exclusive() -> None:
    """Assigned to ``acme_orgs``, not even the first-segment module provides it."""
    core = _core({MEMBERSHIP: "acme_orgs"})
    with pytest.raises(ForeignOwnerRegistration, match="assigns to 'acme_orgs'"):
        _register(core, "sandloom_tenancy", MEMBERSHIP, _Membership)


def test_an_access_bearing_contract_outside_a_registration_is_refused() -> None:
    """No registering module: nothing to check the provider against."""
    core = _core()
    with pytest.raises(ForeignOwnerRegistration, match="outside a module"):
        core.register_service(MEMBERSHIP, _Membership)


def test_an_entry_point_name_is_resolved_like_a_module_list() -> None:
    """``"tenancy"`` assigns the installed distribution's import path."""
    assert _access.build_access_providers({"acme.orgs.v1": "tenancy"}) == {
        "acme.orgs.v1": "sandloom_tenancy"
    }


# ---------------------------------------------------- classification, generic


def test_a_generated_access_check_makes_its_contract_access_bearing(
    seats_atoms: Callable[[], Any],
) -> None:
    """No kernel edit: the imported atom's contract is read generically."""
    seats_atoms()
    core = _core()
    with pytest.raises(ForeignOwnerRegistration, match="'acme.seats.v1'"):
        _register(core, "evil", SEATS, _Seats)
    _register(core, "acme", SEATS, _Seats)


def test_an_access_check_imported_after_the_registration_refuses_the_boot(
    seats_atoms: Callable[[], Any],
) -> None:
    """The post-load sweep: a later module's generated atom classifies an
    earlier registration, and the boot is refused (B034)."""
    core = _core()
    _register(core, "evil", SEATS, _Seats)  # not access-bearing yet
    seats_atoms()
    with pytest.raises(ConfigurationError, match="SANDLOOM-B034") as exc:
        _access.enforce_access_providers(core, log=LOG)
    assert "'evil'" in str(exc.value)
    assert "'acme.seats.v1'" in str(exc.value)


async def test_an_access_check_imported_after_create_app_refuses_the_startup(
    seats_atoms: Callable[[], Any],
) -> None:
    """The startup recheck: a generated atom the root imports after the boot
    sweep still classifies a registration before the app serves."""
    core = _core()
    _register(core, "evil", SEATS, _Seats)
    _access.enforce_access_providers(core, log=LOG)  # nothing to refuse yet
    seats_atoms()
    with pytest.raises(CriticalStartupError) as exc:
        await run_startup(core)
    assert isinstance(exc.value.__cause__, ForeignOwnerRegistration)
    assert "'acme.seats.v1'" in str(exc.value.__cause__)


def test_the_assignment_keys_are_access_bearing() -> None:
    """A contract no adapter names is access-bearing once the root assigns it."""
    core = _core({"acme.orgs.v1": "acme_orgs"})
    with pytest.raises(ForeignOwnerRegistration):
        _register(core, "acme", "acme.orgs.v1", _Membership)
    _register(core, "acme_orgs", "acme.orgs.v1", _Membership)


def test_an_unclassified_contract_is_first_registrant() -> None:
    """H-17: a contract nobody classifies keeps the first-registrant rule."""
    core = _core()
    _register(core, "evil", "notes.stats.v1", _Seats)
    assert core.contract_owner("notes.stats.v1") == "evil"
    with pytest.raises(ModuleRegistrationConflict):
        _register(core, "notes", "notes.stats.v1", _Seats)
    assert core.access_provider_problems() == []


def test_an_access_check_on_the_implementation_classifies_its_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L1: a provider-only process (no consumer imported a generated atom,
    no assignment) still refuses a module that is not the first-segment
    provider of an implementation carrying ``@access_check`` methods."""
    monkeypatch.setattr(_contract_check, "generated_access_contracts", frozenset)
    core = _core()
    with pytest.raises(ForeignOwnerRegistration, match="'acme.seats.v1'"):
        _register(core, "evil", SEATS, _SeatsContract)
    assert not core.service_registry.contains(SEATS)
    _register(core, "acme", SEATS, _SeatsContract)
    assert core.contract_owner(SEATS) == "acme"
    assert core.access_provider_problems() == []


def test_a_refused_attempt_keeps_the_name_access_bearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first registrant without an access check cannot keep a name an
    access-checked implementation was offered for: the provider's refused
    attempt classifies it, and the post-load sweep refuses the boot."""
    monkeypatch.setattr(_contract_check, "generated_access_contracts", frozenset)
    core = _core()
    _register(core, "evil", SEATS, _Seats)  # nothing classifies it yet
    with pytest.raises(ModuleRegistrationConflict):
        _register(core, "acme", SEATS, _SeatsContract)
    with pytest.raises(ConfigurationError, match="SANDLOOM-B034") as exc:
        _access.enforce_access_providers(core, log=LOG)
    assert "'evil'" in str(exc.value)


def test_an_access_check_on_a_base_class_classifies_its_contract() -> None:
    """An implementation deriving from the declared ``@contract`` class
    inherits its access checks: the name is access-bearing."""

    class _Derived(_SeatsContract):
        """A subclass of the declared contract."""

    core = _core()
    with pytest.raises(ForeignOwnerRegistration, match="'acme.seats.v1'"):
        _register(core, "evil", SEATS, _Derived())


def test_a_bare_facade_keeps_the_first_registrant_rule() -> None:
    """No ``create_app`` assignment bound: nothing is classified."""
    core = CoreFacade(app=FastAPI())
    core.register_service(MEMBERSHIP, _Membership)
    assert core.access_provider_problems() == []


# ------------------------------------------------------------- the root's map


@pytest.mark.parametrize(
    ("mapping", "match"),
    [
        ({"tenancy.claims.v1": "acme_orgs"}, "reserved"),
        ({"billing.facts.v1": "acme_orgs"}, "reserved"),
        ({"not a contract": "acme_orgs"}, "contract name"),
        ({MEMBERSHIP: ""}, "contract name"),
        ({MEMBERSHIP: 3}, "contract name"),
    ],
)
def test_a_malformed_map_refuses_the_boot(mapping: dict[str, Any], match: str) -> None:
    core = CoreFacade(app=FastAPI())
    with pytest.raises(ConfigurationError, match="SANDLOOM-B034") as exc:
        _access.install_access_providers(core, access_providers=mapping, log=LOG)
    assert match in str(exc.value)


# ------------------------------------------------------------ through create_app

_PROVIDER = """
from uuid import UUID

from sandloom.core.module_api import contract_method, define_module
from sandloom.shared.authz import From, access_check


class Membership:
    @access_check("OrgRole", bind={"org_id": From.org(), "user_id": From.subject_id()})
    @classmethod
    @contract_method
    async def has_role_at_least(cls, org_id: UUID, user_id: UUID, minimum: str) -> bool:
        return False


def _setup(api):
    api.register_service("tenancy.membership.v1", Membership)


register = define_module(__name__, setup=_setup, events=None)
"""


@pytest.fixture
def provider_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Writes a module at an import path that serves the membership contract
    and makes it the composition's only module."""
    from sandloom.core.config.settings import settings

    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(settings, "module_load_entrypoints", False)

    def write(path: str) -> None:
        parts = path.split(".")
        for depth in range(1, len(parts) + 1):
            package = tmp_path.joinpath(*parts[:depth])
            package.mkdir(exist_ok=True)
            init = package / "__init__.py"
            if not init.exists():
                init.write_text(_PROVIDER if depth == len(parts) else "")
        monkeypatch.setattr(settings, "sandloom_modules", [path])
        monkeypatch.setattr(settings, "module_required", [path])

    return write


def _boot_refusal(**kwargs: Any) -> str:
    """The text of ``create_app``'s refusal, with its causes."""
    from sandloom.core.app_factory import create_app

    # Any refusal (a module load failure or ConfigurationError); its text is
    # what the tests check.
    with pytest.raises(BaseException) as exc:
        create_app(**kwargs)
    texts: list[str] = []
    err: BaseException | None = exc.value
    while err is not None:
        texts.append(str(err))
        err = err.__cause__ or err.__context__
    return " | ".join(texts)


def test_create_app_refuses_an_unassigned_third_party_provider(
    provider_module: Any,
) -> None:
    provider_module("acme_orgs")
    refusal = _boot_refusal()
    assert "SANDLOOM-B034" in refusal
    assert "'tenancy.membership.v1'" in refusal


def test_create_app_accepts_an_assigned_third_party_provider(
    provider_module: Any,
) -> None:
    from sandloom.core.app_factory import create_app

    provider_module("acme_orgs")
    app = create_app(access_providers={MEMBERSHIP: "acme_orgs"})
    assert app.state.core.contract_owner(MEMBERSHIP) == "acme_orgs"


def test_create_app_refuses_an_app_local_namesake_of_the_assigned(
    provider_module: Any,
) -> None:
    provider_module("kbapp.modules.acme_orgs")
    refusal = _boot_refusal(access_providers={MEMBERSHIP: "acme_orgs"})
    assert "SANDLOOM-B034" in refusal
    assert "'kbapp.modules.acme_orgs'" in refusal


def test_create_app_refuses_a_malformed_assignment(provider_module: Any) -> None:
    provider_module("acme_orgs")
    refusal = _boot_refusal(access_providers={"acme_orgs.facts.v1": "acme_orgs"})
    assert "SANDLOOM-B034" in refusal
    assert "reserved" in refusal
