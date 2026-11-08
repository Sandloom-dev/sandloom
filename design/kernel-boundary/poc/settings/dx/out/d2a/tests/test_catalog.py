from __future__ import annotations

from fastapi.testclient import TestClient
from sandloom.sdk.testing import override_settings

from catalog.settings import CatalogSettings
from harness import app, boot


def test_default_boots() -> None:
    assert boot({})[0] == 0


def test_page_size_zero_refused() -> None:
    code, line = boot({"CATALOG_PAGE_SIZE": "0"})
    assert code != 0 and "CATALOG_PAGE_SIZE" in line


def test_prod_check_and_accepted_risk() -> None:
    code, line = boot(
        {"CATALOG_SHOW_UNPUBLISHED": "true", "EVENT_BUS_BACKEND": "streams"},
        prod=True,
    )
    assert code != 0 and "CATALOG_SHOW_UNPUBLISHED" in line
    code, line = boot(
        {
            "CATALOG_SHOW_UNPUBLISHED": "true",
            "EVENT_BUS_BACKEND": "streams",
            "SANDLOOM_ACCEPTED_RISKS": '["catalog.show_unpublished_in_prod"]',
        },
        prod=True,
    )
    assert code == 0, line


def test_removed_setting_refused() -> None:
    code, line = boot({"CATALOG_LEGACY_INDEX": "true"})
    assert code != 0 and "CATALOG_LEGACY_INDEX" in line


def test_route_page_size_override() -> None:
    with override_settings(CatalogSettings, page_size=7):
        with TestClient(app()) as client:
            assert client.get("/catalog/info").json() == {"page_size": 7}
