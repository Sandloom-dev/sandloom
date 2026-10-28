"""Admission tests for generated admin: API contracts, not ORM inference."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Literal

import pytest
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from sandloom.core.admin import compile_app, compile_resources


class ItemOut(BaseModel):
    id: str
    name: str
    state: Literal["draft", "active"]
    quantity: int
    enabled: bool


class ItemIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    state: Literal["draft", "active"] = "draft"
    quantity: int = Field(default=0, ge=0, le=500)
    enabled: bool = False


class ActionIn(BaseModel):
    state: Literal["draft", "active"]


def catalog() -> tuple[FastAPI, dict]:
    app = FastAPI()

    async def listing(
        skip: int = 0,
        limit: int = Query(25, ge=1, le=101),
        state: Literal["draft", "active"] | None = None,
        sort: Literal["name", "-name"] = "name",
    ) -> list[ItemOut]:
        return []

    async def detail(item_id: str) -> ItemOut:
        return ItemOut(
            id=item_id, name="Example", state="draft", quantity=0, enabled=False
        )

    async def create(data: ItemIn) -> ItemOut:
        return ItemOut(id="new", **data.model_dump())

    async def edit(item_id: str, data: ItemIn) -> ItemOut:
        return ItemOut(id=item_id, **data.model_dump())

    async def action(item_id: str, data: ActionIn) -> None:
        return None

    for path, fn, method in [
        ("/api/v1/catalog", listing, "GET"),
        ("/api/v1/catalog/{item_id}", detail, "GET"),
        ("/api/v1/catalog", create, "POST"),
        ("/api/v1/catalog/{item_id}", edit, "PUT"),
        ("/api/v1/catalog/{item_id}/state", action, "POST"),
    ]:
        fn.__module__ = "demo.catalog.api.items"
        app.add_api_route(path, fn, methods=[method])
    declaration = {
        "name": "catalog",
        "label": "Catalog",
        "access": {"roles": ["admin"]},
        "group": "Inventory",
        "icon": "package",
        "list": {"path": "/api/v1/catalog", "method": "GET"},
        "pagination": {"mode": "offset"},
        "columns": ["name", "state", "quantity", "enabled"],
        "detail": {"path": "/api/v1/catalog/{item_id}", "method": "GET"},
        "create": {
            "path": "/api/v1/catalog",
            "method": "POST",
            "fields": ["name", "state", "quantity", "enabled"],
        },
        "edit": {
            "path": "/api/v1/catalog/{item_id}",
            "method": "PUT",
            "fields": ["name", "state", "quantity", "enabled"],
        },
        "filters": ["state"],
        "sort_parameter": "sort",
        "sort_fields": ["name"],
        "labels": {
            "name": "Name",
            "state": "State",
            "quantity": "Quantity",
            "enabled": "Enabled",
        },
        "actions": [
            {
                "name": "set_state",
                "label": "Change state",
                "confirm": "Change this record's state?",
                "path": "/api/v1/catalog/{item_id}/state",
                "method": "POST",
                "fields": ["state"],
                "tone": "danger",
                "typed_confirmation": "name",
            }
        ],
    }
    return app, declaration


def test_compiles_new_module_without_frontend_switches() -> None:
    app, declaration = catalog()
    result = compile_resources(app, [("demo.catalog", [declaration])])
    resource = result["resources"][0]
    assert resource["module"] == "catalog"
    assert resource["columns"][0]["name"] == "name"
    fields = {f["name"]: f for f in resource["create"]["inputs"]}
    assert fields["name"]["required"] and fields["name"]["minLength"] == 2
    assert fields["state"]["enum"] == ["draft", "active"]
    assert fields["quantity"]["maximum"] == 500
    assert fields["enabled"]["default"] is False
    assert resource["actions"][0]["inputs"][0]["required"]
    # Same artifact is exercised by frontend browser tests. A schema drift must
    # update the fixture deliberately, never hide behind duplicated TS fixtures.
    from pathlib import Path

    target = (
        Path(__file__).resolve().parents[4]
        / "admin/tests/fixtures/catalog-manifest.json"
    )
    assert json.loads(target.read_text()) == result


@pytest.mark.parametrize(
    "change, message",
    [
        ({"access": {}}, "choose authenticated"),
        ({"extra_surprise": True}, "Extra inputs"),
        ({"columns": ["password_hash"]}, "unknown API field"),
        (
            {
                "create": {
                    "path": "/api/v1/catalog",
                    "method": "POST",
                    "fields": ["state"],
                }
            },
            "omits required",
        ),
        ({"filters": ["is_superuser"]}, "undeclared API query"),
        ({"filters": ["limit"]}, "overwrite pagination"),
        ({"list": {"path": "/_sandloom/rpc", "method": "GET"}}, "unsupported public"),
        (
            {"list": {"path": "https://bad.test/api/v1/catalog", "method": "GET"}},
            "unsupported public",
        ),
        ({"list": {"path": "/api/v1/catalog", "method": "POST"}}, "unsupported public"),
        (
            {"pagination": {"mode": "offset", "max_page_size": 101}},
            "less than or equal",
        ),
        ({"scope": {"parameter": "tenant_id"}}, "expected scope"),
        ({"sort_fields": ["id"]}, "sorting needs"),
        ({"icon": "Bad Icon"}, "String should match"),
        (
            {
                "actions": [
                    {
                        "name": "set_state",
                        "label": "Change state",
                        "confirm": "Change?",
                        "path": "/api/v1/catalog/{item_id}/state",
                        "method": "POST",
                        "fields": ["state"],
                        "typed_confirmation": "quantity_secret",
                    }
                ]
            },
            "typed_confirmation must name a displayed field",
        ),
    ],
)
def test_rejects_unsafe_or_incomplete_declarations(change: dict, message: str) -> None:
    app, declaration = catalog()
    with pytest.raises(ValueError, match=message):
        compile_resources(app, [("demo.catalog", [{**declaration, **change}])])


def test_foreign_module_cannot_admit_owners_routes() -> None:
    app, declaration = catalog()
    with pytest.raises(ValueError, match="not owned"):
        compile_resources(app, [("demo.foreign", [declaration])])


def test_duplicate_resources_fail_not_shadow() -> None:
    app, declaration = catalog()
    with pytest.raises(ValueError, match="duplicate/unsafe"):
        compile_resources(
            app, [("demo.catalog", [declaration, copy.deepcopy(declaration)])]
        )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {}},
        {"type": "array", "items": {"type": "string"}},
        {"type": "string", "writeOnly": True},
        {"type": "string", "readOnly": True},
        {"oneOf": [{"type": "string"}, {"type": "number"}]},
        {"type": "string", "format": "binary"},
        {"type": "string", "pattern": "(?<=bad)x"},
    ],
)
def test_unsupported_form_schemas_fail_before_rendering(schema: dict) -> None:
    app, declaration = catalog()
    app.openapi()["components"]["schemas"]["ItemIn"]["properties"]["name"] = schema
    with pytest.raises(ValueError):
        compile_resources(app, [("demo.catalog", [declaration])])


def test_discovery_only_loaded_modules_and_never_swallows_broken_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.admin import compiler

    app, declaration = catalog()
    app.state.module_summary = SimpleNamespace(
        loaded=[SimpleNamespace(name="demo.catalog")], failed=[]
    )
    monkeypatch.setattr(
        compiler.importlib,
        "import_module",
        lambda name: SimpleNamespace(RESOURCES=[declaration]),
    )
    assert compile_app(app)["modules"] == ["catalog"]

    def absent(name: str) -> None:
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(compiler.importlib, "import_module", absent)
    assert compile_app(app)["resources"] == []

    def broken(name: str) -> None:
        raise ModuleNotFoundError(name="missing_dependency_inside_declaration")

    monkeypatch.setattr(compiler.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        compile_app(app)


def test_cli_honors_composition_and_checks_drift(tmp_path, monkeypatch) -> None:
    from sandloom.core import composition
    from sandloom.core.admin import compiler
    from sandloom.core.cli.admin import main

    app, declaration = catalog()
    app.state.module_summary = SimpleNamespace(
        loaded=[SimpleNamespace(name="demo.catalog")], failed=[]
    )
    monkeypatch.setattr(composition, "load_composition_app", lambda: app)
    monkeypatch.setattr(
        compiler.importlib,
        "import_module",
        lambda name: SimpleNamespace(RESOURCES=[declaration]),
    )
    output = tmp_path / "admin.json"
    assert main(["export", "--output", str(output)]) == 0
    assert main(["export", "--output", str(output), "--check"]) == 0
    output.write_text("{}")
    assert main(["export", "--output", str(output), "--check"]) == 1


def test_header_only_tenant_scope_and_no_fake_unpaged_api() -> None:
    app, declaration = catalog()
    declaration["scope"] = {"parameter": None}
    assert (
        compile_resources(app, [("demo.catalog", [declaration])])["resources"][0][
            "scope"
        ]["parameter"]
        is None
    )
    declaration["pagination"] = {"mode": "all"}
    with pytest.raises(ValueError, match="genuinely unpaged"):
        compile_resources(app, [("demo.catalog", [declaration])])


def test_audit_binding_is_explicit_and_uses_a_real_detail_field() -> None:
    app, declaration = catalog()
    declaration["audit"] = {"resource_type": "catalog_item", "id_field": "id"}
    resource = compile_resources(app, [("demo.catalog", [declaration])])["resources"][0]
    assert resource["audit"] == declaration["audit"]
    declaration["audit"]["id_field"] = "missing_psp_id"
    with pytest.raises(ValueError, match="audit"):
        compile_resources(app, [("demo.catalog", [declaration])])
