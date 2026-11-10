"""The issuer declares its login entry; the console's manifest carries it
(ADR-0012 decision 4, owner's confirmation X4, KB-41).

The kernel reads the issuer from the claim-authorities map (never from a
token), takes the ``login_entry`` the issuer declared on its registration and
refuses one the composed app does not serve. ``sandloom_admin`` renders it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

import sandloom.core.cli.generation_input as generation_input
from sandloom.core.claims import ClaimAuthorities
from sandloom.core.descriptors import ModuleDescriptor, login_entry_problem

ISSUER = "acme.test_identity"


def _issuer(**kwargs: Any) -> ModuleDescriptor:
    return ModuleDescriptor(name=ISSUER, version="1", issues_tokens=True, **kwargs)


def _app(
    descriptors: list[ModuleDescriptor],
    *,
    issuer: str | None,
    login_module: str = ISSUER + ".api",
    path: str = "/auth/login",
) -> FastAPI:
    app = FastAPI()

    async def login() -> dict[str, str]:
        return {}

    login.__module__ = login_module
    app.add_api_route(path, login, methods=["POST"])
    app.state.module_summary = SimpleNamespace(failed=[], loaded=descriptors)
    app.state.core = SimpleNamespace(
        claim_authorities=ClaimAuthorities(issuer=issuer, assigned=True)
    )
    return app


@pytest.fixture(autouse=True)
def _no_admin_submodules(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> Any:
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(
        "sandloom.core.cli.generation_input.importlib",
        SimpleNamespace(import_module=missing),
    )


def test_the_assigned_issuer_and_its_declaration_reach_the_renderer() -> None:
    app = _app([_issuer(login_entry="/auth/login")], issuer=ISSUER)
    issuer = generation_input.collect(app).issuer
    assert issuer is not None
    assert (issuer.package, issuer.name) == (ISSUER, "test_identity")
    assert issuer.login_entry == "/auth/login"


def test_an_issuer_without_a_declaration_is_kept_so_the_console_names_it() -> None:
    issuer = generation_input.collect(_app([_issuer()], issuer=ISSUER)).issuer
    assert issuer is not None and issuer.login_entry is None


def test_no_assigned_issuer_is_none() -> None:
    app = _app([_issuer(login_entry="/auth/login")], issuer=None)
    assert generation_input.collect(app).issuer is None


def test_an_issuer_the_map_assigns_but_this_composition_lacks_has_no_entry() -> None:
    other = ModuleDescriptor(name="acme.other", version="1")
    issuer = generation_input.collect(_app([other], issuer=ISSUER)).issuer
    assert issuer is not None and issuer.login_entry is None


def test_only_the_map_assigns_the_issuer_not_a_module_claiming_it() -> None:
    """A loaded module that declares an entry is not the issuer unless the map says so."""
    intruder = ModuleDescriptor(
        name="acme.intruder", version="1", issues_tokens=True, login_entry="/auth/login"
    )
    app = _app([intruder, _issuer()], issuer=ISSUER)
    issuer = generation_input.collect(app).issuer
    assert issuer is not None
    assert issuer.name == "test_identity" and issuer.login_entry is None


@pytest.mark.parametrize(
    ("login_module", "path"),
    [
        ("acme.elsewhere.api", "/auth/login"),  # served by another module
        (ISSUER + ".api", "/auth/other"),  # nothing serves the declared path
        (ISSUER + "_clone.api", "/auth/login"),  # a name sharing the prefix
    ],
)
def test_a_declaration_the_app_does_not_serve_is_refused(
    login_module: str, path: str
) -> None:
    app = _app(
        [_issuer(login_entry="/auth/login")],
        issuer=ISSUER,
        login_module=login_module,
        path=path,
    )
    with pytest.raises(ValueError, match="login_entry='/auth/login'"):
        generation_input.collect(app)


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "auth/login",
        "//evil.example/login",
        "https://evil.example/login",
        "/auth/../login",
        "/auth/./login",
        "/auth/login?next=/x",
        "/auth/login#x",
        "/auth\\login",
        "/auth/%2e%2e/login",
        "/auth/log in",
        "/auth/login\n",
        "/user@evil/login",
        7,
    ],
)
def test_a_login_entry_that_could_leave_the_app_is_refused(entry: Any) -> None:
    assert login_entry_problem(entry)
    with pytest.raises(ValueError, match="login_entry"):
        ModuleDescriptor(
            name=ISSUER, version="1", issues_tokens=True, login_entry=entry
        )


@pytest.mark.parametrize("entry", ["/auth/login", "/api/v1/sign-in", "/login.json"])
def test_a_route_path_is_a_login_entry(entry: str) -> None:
    assert login_entry_problem(entry) is None


def test_only_the_issuer_declares_a_login_entry() -> None:
    with pytest.raises(ValueError, match="issues_tokens=True"):
        ModuleDescriptor(name="acme.notes", version="1", login_entry="/auth/login")


def test_console_auth_does_not_require_roles_in_an_issuer_only_app() -> None:
    app = _app(
        [
            _issuer(
                login_entry="/auth/login",
                console_auth={"kind": "id_token"},
                refresh_sessions=False,
            )
        ],
        issuer=ISSUER,
    )
    generated = generation_input.collect(app).issuer
    assert generated is not None
    assert generated.console_auth == {"kind": "id_token"}


def test_console_auth_carries_the_compositions_roles_owner() -> None:
    app = _app(
        [_issuer(login_entry="/auth/login", console_auth={"kind": "id_token"})],
        issuer=ISSUER,
    )
    app.state.core.claim_authorities = ClaimAuthorities(
        issuer=ISSUER, assigned=True, owners={"roles": "acme_roles"}
    )
    generated = generation_input.collect(app).issuer
    assert generated is not None
    assert generated.console_auth == {"kind": "id_token", "roles_owner": "acme_roles"}


@pytest.mark.parametrize(
    "auth",
    [
        {"kind": "redirect"},
        {"kind": "password", "me": "//evil.example/me"},
        {"kind": "password", "arbitrary": "/anything"},
    ],
)
def test_console_auth_refuses_unsupported_or_unsafe_declarations(
    auth: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="console_auth"):
        _issuer(login_entry="/auth/login", console_auth=auth)


def test_console_auth_session_endpoints_are_served_by_the_issuer() -> None:
    app = _app(
        [
            _issuer(
                login_entry="/auth/login",
                console_auth={"kind": "password", "refresh": "/foreign/refresh"},
            )
        ],
        issuer=ISSUER,
    )
    with pytest.raises(ValueError, match="console_auth.refresh"):
        generation_input.collect(app)
