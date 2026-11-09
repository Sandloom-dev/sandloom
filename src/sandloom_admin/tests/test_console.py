"""What the console's routes serve, and what they refuse to."""

from __future__ import annotations

import base64
import json
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from sandloom.sdk.app import create_app  # sandloom: advanced test app
from sandloom.sdk.infra import settings
from sandloom_admin import api


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    values = SimpleNamespace(
        admin_console_enabled=None,
        admin_console_dist=None,
        contract_dto_packages=[],
        environment=SimpleNamespace(value="dev"),
    )
    monkeypatch.setattr(api, "settings", values)
    return values


@pytest.fixture
def application_package(
    tmp_path: Path, config: SimpleNamespace
) -> Iterator[tuple[Path, str]]:
    name = f"generated_{tmp_path.name.replace('-', '_')}"
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    config.contract_dto_packages = [name]
    yield package, name
    sys.path.remove(str(tmp_path))
    sys.modules.pop(name, None)


def _manifest(package: Path, **overrides: object) -> None:
    (package / "admin-manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "modules": ["identity", "core"],
                "resources": [],
                "issuer": {"module": "test_identity", "login_entry": "/auth/login"},
                **overrides,
            }
        )
    )


@pytest.fixture
async def client(
    config: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings, "sandloom_modules", ["sandloom_admin"])
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


@pytest.mark.parametrize("path", ["/admin/", "/admin/index.html"])
async def test_the_entry_page_forbids_foreign_code(
    client: AsyncClient, path: str
) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    assert "/admin/assets/" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_an_unchanged_font_costs_a_bodiless_304(client: AsyncClient) -> None:
    first = await client.get("/admin/fonts/hack-nerd-mono-regular.woff2")
    assert first.headers["cache-control"] == "no-cache"
    again = await client.get(
        "/admin/fonts/hack-nerd-mono-regular.woff2",
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert again.status_code == 304
    assert again.content == b""


async def test_the_bare_path_redirects(client: AsyncClient) -> None:
    response = await client.get("/admin")
    assert response.status_code == 308
    assert response.headers["location"] == "/admin/"


async def test_a_deep_link_serves_the_entry_page(client: AsyncClient) -> None:
    response = await client.get("/admin/resources/users/123")
    assert response.status_code == 200
    assert "<title>" in response.text


async def test_hashed_assets_are_cached_forever(client: AsyncClient) -> None:
    html = (await client.get("/admin/")).text
    asset = html.split('src="/admin/')[1].split('"')[0]
    response = await client.get(f"/admin/{asset}")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert "javascript" in response.headers["content-type"]


async def test_hashed_assets_come_precompressed(client: AsyncClient) -> None:
    html = (await client.get("/admin/")).text
    asset = html.split('src="/admin/')[1].split('"')[0]
    plain = await client.get(f"/admin/{asset}", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    for coding in ("br", "gzip"):
        response = await client.get(
            f"/admin/{asset}", headers={"Accept-Encoding": f"{coding}, deflate"}
        )
        assert response.headers["content-encoding"] == coding
        assert response.headers["vary"] == "Accept-Encoding"
        assert "javascript" in response.headers["content-type"]
        assert "immutable" in response.headers["cache-control"]
        assert int(response.headers["content-length"]) < len(plain.content) / 2
    for header in ("br;q=0, gzip", "br; Q=0.0, gzip", "br;q=0., gzip;q=0.5"):
        refused = await client.get(
            f"/admin/{asset}", headers={"Accept-Encoding": header}
        )
        assert refused.headers["content-encoding"] == "gzip", header


async def test_a_missing_file_is_not_answered_with_html(client: AsyncClient) -> None:
    assert (await client.get("/admin/assets/gone-abc.js")).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/admin/..%2f..%2f__init__.py",
        "/admin/%2e%2e/api.py",
        "/admin/..%2fapi.py",
    ],
)
async def test_nothing_outside_the_console_is_served(
    client: AsyncClient, path: str
) -> None:
    response = await client.get(path)
    assert "import" not in response.text.split("<")[0]
    assert "define_module" not in response.text
    assert "module_router" not in response.text


async def test_production_keeps_it_off_unless_enabled(
    client: AsyncClient, config: SimpleNamespace
) -> None:
    config.environment = SimpleNamespace(value="prod")
    assert (await client.get("/admin/")).status_code == 404
    assert (await client.get("/admin/config.json")).status_code == 404
    config.admin_console_enabled = True
    assert (await client.get("/admin/")).status_code == 200


async def test_it_can_be_switched_off_anywhere(
    client: AsyncClient, config: SimpleNamespace
) -> None:
    config.admin_console_enabled = False
    assert (await client.get("/admin/")).status_code == 404


async def test_a_missing_manifest_names_the_fix(client: AsyncClient) -> None:
    response = await client.get("/admin/admin-manifest.json")
    assert response.status_code == 503
    assert "sandloom generate" in response.json()["detail"]


async def test_the_manifest_comes_from_the_application(
    client: AsyncClient, application_package: tuple[Path, str]
) -> None:
    package, _ = application_package
    _manifest(package)
    response = await client.get("/admin/admin-manifest.json")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/admin/config.json")).json() == {
        "modules": ["identity"],
        "issuer": "test_identity",
        "login_entry": "/auth/login",
        "development": True,
    }


async def test_a_large_manifest_is_sent_gzipped(
    client: AsyncClient, application_package: tuple[Path, str]
) -> None:
    package, _ = application_package
    _manifest(package, padding="x" * 4096)
    response = await client.get(
        "/admin/admin-manifest.json", headers={"Accept-Encoding": "gzip"}
    )
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["padding"] == "x" * 4096
    plain = await client.get(
        "/admin/admin-manifest.json", headers={"Accept-Encoding": "identity"}
    )
    assert "content-encoding" not in plain.headers


async def test_production_config_does_not_say_development(
    client: AsyncClient,
    application_package: tuple[Path, str],
    config: SimpleNamespace,
) -> None:
    """``development`` lets the console keep a session across a reload and
    show local hints; production keeps sessions in memory only."""
    package, _ = application_package
    _manifest(package)
    config.environment = SimpleNamespace(value="prod")
    config.admin_console_enabled = True
    assert (await client.get("/admin/config.json")).json() == {
        "modules": ["identity"],
        "issuer": "test_identity",
        "login_entry": "/auth/login",
    }


async def test_the_issuer_need_not_be_a_module_named_identity(
    client: AsyncClient, application_package: tuple[Path, str]
) -> None:
    """The console serves in front of whichever module the map assigns."""
    package, _ = application_package
    _manifest(package, modules=["billing"])
    response = await client.get("/admin/config.json")
    assert response.status_code == 200
    assert response.json()["issuer"] == "test_identity"


async def test_no_assigned_issuer_is_a_configuration_error(
    client: AsyncClient, application_package: tuple[Path, str]
) -> None:
    package, _ = application_package
    _manifest(package, issuer=None)
    response = await client.get("/admin/config.json")
    assert response.status_code == 503
    assert "claim_authorities" in response.json()["detail"]


async def test_a_manifest_without_the_issuer_field_fails_closed(
    client: AsyncClient, application_package: tuple[Path, str]
) -> None:
    """A manifest written before the field existed: nothing is assumed."""
    package, _ = application_package
    manifest = {"version": 1, "modules": ["identity", "core"], "resources": []}
    (package / "admin-manifest.json").write_text(json.dumps(manifest))
    assert (await client.get("/admin/config.json")).status_code == 503


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "",
        "auth/login",
        "//evil.example/login",
        "https://evil.example/login",
        "/auth/../login",
        "/auth/login?next=//evil",
        "/auth\\login",
        7,
    ],
)
async def test_an_issuer_without_a_usable_login_entry_is_named(
    client: AsyncClient, application_package: tuple[Path, str], entry: object
) -> None:
    package, _ = application_package
    _manifest(package, issuer={"module": "test_identity", "login_entry": entry})
    response = await client.get("/admin/config.json")
    assert response.status_code == 503
    assert "test_identity" in response.json()["detail"]
    assert "login_entry" in response.json()["detail"]


async def test_a_forged_iss_changes_nothing(
    client: AsyncClient, application_package: tuple[Path, str]
) -> None:
    """The issuer is the map's, never a token's ``iss`` (an unverified input)."""
    package, _ = application_package
    forged = "x." + base64.urlsafe_b64encode(b'{"iss":"identity"}').decode() + ".y"
    headers = {"Authorization": f"Bearer {forged}"}
    _manifest(package, issuer=None)
    assert (await client.get("/admin/config.json", headers=headers)).status_code == 503
    _manifest(package)
    body = (await client.get("/admin/config.json", headers=headers)).json()
    assert body["issuer"] == "test_identity"
    assert body["login_entry"] == "/auth/login"


async def test_a_newer_manifest_version_is_refused(
    client: AsyncClient, application_package: tuple[Path, str]
) -> None:
    package, _ = application_package
    _manifest(package, version=99)
    response = await client.get("/admin/admin-manifest.json")
    assert response.status_code == 503
    assert "sandloom-admin" in response.json()["detail"]


async def test_a_widget_this_console_lacks_is_refused(
    client: AsyncClient, application_package: tuple[Path, str]
) -> None:
    package, _ = application_package
    _manifest(
        package,
        resources=[{"name": "x", "show_widgets": ["future.thing"], "edit_widgets": []}],
    )
    response = await client.get("/admin/admin-manifest.json")
    assert response.status_code == 503
    assert "future.thing" in response.json()["detail"]


async def test_a_custom_build_replaces_the_packaged_one(
    client: AsyncClient, config: SimpleNamespace, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<title>mine</title>")
    config.admin_console_dist = str(tmp_path)
    assert "mine" in (await client.get("/admin/")).text


async def test_a_custom_build_without_files_says_so(
    client: AsyncClient, config: SimpleNamespace, tmp_path: Path
) -> None:
    config.admin_console_dist = str(tmp_path)
    assert (await client.get("/admin/")).status_code == 503


async def test_it_stays_out_of_the_api_schema(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert not [path for path in schema.get("paths", {}) if path.startswith("/admin")]
