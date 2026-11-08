from fastapi.testclient import TestClient

from catalog.settings import CatalogSettings
from harness import app, boot
from sandloom.sdk.testing import override_settings


def test_default_boots() -> None:
    code, err = boot({})
    assert code == 0, err


def test_page_size_zero_refused() -> None:
    code, err = boot({"CATALOG_PAGE_SIZE": "0"})
    assert code != 0
    assert "CATALOG_PAGE_SIZE" in err


def test_prod_check_and_accepted_risk() -> None:
    durable = {"EVENT_BUS_BACKEND": "streams"}
    code, err = boot({**durable, "CATALOG_SHOW_UNPUBLISHED": "true"}, prod=True)
    assert code != 0
    assert "CATALOG_SHOW_UNPUBLISHED" in err
    code, err = boot(
        {
            **durable,
            "CATALOG_SHOW_UNPUBLISHED": "true",
            "SANDLOOM_ACCEPTED_RISKS": '["catalog.show_unpublished_in_prod"]',
        },
        prod=True,
    )
    assert code == 0, err


def test_removed_setting_refused() -> None:
    code, err = boot({"CATALOG_LEGACY_INDEX": "true"})
    assert code != 0
    assert "CATALOG_LEGACY_INDEX" in err


def test_route_page_size_override() -> None:
    with TestClient(app()) as client:
        with override_settings(CatalogSettings, page_size=7):
            assert client.get("/catalog/info").json() == {"page_size": 7}
