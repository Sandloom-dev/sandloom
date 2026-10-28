"""Compile only explicitly admitted module HTTP operations into static UI data."""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable
from typing import cast

from fastapi import FastAPI
from fastapi.routing import APIRoute

from sandloom.core.descriptors.module_names import module_name as _module_name

from .models import FormOperation, Json, Operation, Resource

_PUBLIC = re.compile(r"^/api/v1/[A-Za-z0-9_/{\}.-]+$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORBIDDEN = {"__proto__", "prototype", "constructor"}


def _safe_name(name: str) -> None:
    if not _NAME.fullmatch(name) or name in _FORBIDDEN:
        raise ValueError(f"unsupported field/parameter name: {name!r}")


def resolve(schema: Json, document: Json) -> Json:
    """Resolve local schema refs; reject cycles and ambiguous polymorphism."""
    seen: set[str] = set()
    while "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
            raise ValueError("only local component schema references are supported")
        if ref in seen:
            raise ValueError(f"cyclic schema reference: {ref}")
        seen.add(ref)
        schema = document["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    if "anyOf" in schema:
        variants = schema["anyOf"]
        nonnull = [v for v in variants if v.get("type") != "null"]
        if len(nonnull) != 1:
            raise ValueError("only nullable single-type anyOf schemas are supported")
        schema = {
            **resolve(nonnull[0], document),
            **{k: v for k, v in schema.items() if k != "anyOf"},
            "nullable": True,
        }
    if any(k in schema for k in ("oneOf", "allOf", "not", "if")):
        raise ValueError(
            "unsupported polymorphic/conditional schema; use a custom view"
        )
    return schema


def _field(  # noqa: C901
    name: str,
    schema: Json,
    document: Json,
    required: bool,
    label: str,
    *,
    writable: bool,
) -> Json:
    _safe_name(name)
    schema = resolve(schema, document)
    if schema.get("writeOnly") or (writable and schema.get("readOnly")):
        raise ValueError(f"{name}: writeOnly/readOnly fields cannot be exposed here")
    kind = schema.get("type")
    if kind not in ("string", "boolean", "integer", "number"):
        if writable:
            raise ValueError(
                f"{name}: unsupported input type {kind!r}; use a custom view"
            )
        kind = "json"
    result = {
        "name": name,
        "label": label,
        "type": kind,
        "required": required,
        "nullable": schema.get("nullable", False),
    }
    for key in (
        "format",
        "enum",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "pattern",
        "default",
        "description",
        "multipleOf",
    ):
        if key in schema:
            result[key] = schema[key]
    # Do not silently ignore validation features in writable DTOs.
    if writable:
        supported = {
            "type",
            "title",
            "description",
            "nullable",
            "format",
            "enum",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "pattern",
            "default",
            "readOnly",
            "writeOnly",
            "examples",
            "multipleOf",
        }
        if set(schema) - supported:
            raise ValueError(
                f"{name}: unsupported input constraints {set(schema) - supported}"
            )
        if "pattern" in schema:
            # Keep a portable, bounded regex subset shared by Python/JS.
            if (
                re.search(r"\(\?(?!:)", schema["pattern"])
                or len(schema["pattern"]) > 512
            ):
                raise ValueError(f"{name}: pattern needs a custom validator")
            re.compile(schema["pattern"])
        if schema.get("format") not in (None, "email", "uuid", "date", "date-time"):
            raise ValueError(f"{name}: unsupported input format")
    return result


def _response(op: Json, document: Json) -> Json:
    variants = [
        v.get("content", {}).get("application/json", {}).get("schema")
        for code, v in op.get("responses", {}).items()
        if str(code).startswith("2")
    ]
    variants = [v for v in variants if v is not None]
    if len(variants) != 1:
        raise ValueError("resource operation needs one JSON success response")
    return resolve(variants[0], document)


def compile_resources(
    app: FastAPI, declarations: Iterable[tuple[str, list[Json]]]
) -> Json:
    """Validate ownership, API bindings, pagination and form safety; fail closed."""
    document = app.openapi()
    routes = {
        (r.path, method): r
        for r in app.routes
        if isinstance(r, APIRoute)
        for method in r.methods
    }
    resources: list[Json] = []
    names: set[str] = set()
    modules: list[str] = []
    for owner, raw_resources in declarations:
        if not isinstance(raw_resources, list):
            raise ValueError("admin.RESOURCES must be a list of declarations")
        module = _module_name(owner)
        if module in modules:
            raise ValueError(f"duplicate admin module key: {module}")
        modules.append(module)
        for raw in raw_resources:
            resource = Resource.model_validate(raw)
            if resource.name in names or resource.name in _FORBIDDEN:
                raise ValueError(f"duplicate/unsafe admin resource: {resource.name}")
            names.add(resource.name)
            compiler = _ResourceCompiler(resource, owner, document, routes)
            resources.append(compiler.compile())
    return {"version": 1, "modules": modules, "resources": resources}


class _ResourceCompiler:
    def __init__(
        self,
        resource: Resource,
        owner: str,
        document: Json,
        routes: dict[tuple[str, str], APIRoute],
    ) -> None:
        self.r = resource
        self.owner = owner
        self.document = document
        self.routes = routes

    def operation(self, binding: Operation, *, record: bool, methods: set[str]) -> Json:
        if binding.method not in methods or not _PUBLIC.fullmatch(binding.path):
            raise ValueError(
                f"unsupported public operation: {binding.method} {binding.path}"
            )
        if "//" in binding.path or any(
            x in binding.path.split("/") for x in (".", "..")
        ):
            raise ValueError("unsafe operation path")
        route = self.routes.get((binding.path, binding.method))
        endpoint_owner = getattr(getattr(route, "endpoint", None), "__module__", "")
        expected_owner = self.owner + (
            ".admin.api." if self.owner == "sandloom.core" else ".api."
        )
        if route is None or not endpoint_owner.startswith(expected_owner):
            raise ValueError(
                f"{binding.path}: operation is not owned by {self.owner}.api"
            )
        params = re.findall(r"\{([^}]+)\}", binding.path)
        for parameter in params:
            _safe_name(parameter)
        scope = self.r.scope.parameter if self.r.scope else None
        ids = [p for p in params if p != scope]
        if (scope and scope not in params) or len(ids) != int(record):
            raise ValueError(
                f"{binding.path}: expected scope + one record id, "
                "or scope only for collections"
            )
        op = self.document["paths"][binding.path][binding.method.lower()]
        declared_path = {
            p["name"] for p in op.get("parameters", []) if p["in"] == "path"
        }
        if set(params) != declared_path:
            raise ValueError("every path placeholder must be declared by the API")
        for p in op.get("parameters", []):
            tenant_header = (
                self.r.scope is not None
                and p["in"] == "header"
                and p["name"].lower() == "x-tenant-id"
            )
            if p.get("required") and p["in"] != "path" and not tenant_header:
                raise ValueError(
                    f"{binding.path}: required {p['in']} parameter "
                    f"{p['name']} needs a custom view"
                )
        return cast(Json, op)

    def fields(self, schema: Json, names: list[str], *, writable: bool) -> list[Json]:
        schema = resolve(schema, self.document)
        properties = schema.get("properties", {})
        if len(names) != len(set(names)):
            raise ValueError("duplicate field in declaration")
        if writable and set(schema.get("required", [])) - set(names):
            raise ValueError("form omits required API fields")
        result = []
        for name in names:
            if name not in properties:
                raise ValueError(f"{self.r.name}: unknown API field {name}")
            result.append(
                _field(
                    name,
                    properties[name],
                    self.document,
                    name in schema.get("required", []),
                    self.r.labels.get(name, name),
                    writable=writable,
                )
            )
        return result

    def form(self, binding: FormOperation, *, record: bool, methods: set[str]) -> Json:
        op = self.operation(binding, record=record, methods=methods)
        body = (
            op.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        if body is None and binding.fields:
            raise ValueError("declared form fields but operation accepts no JSON body")
        if body is not None and not binding.fields:
            body_schema = resolve(body, self.document)
            if body_schema.get("required"):
                raise ValueError("command omits required body fields")
        return {
            **binding.model_dump(),
            "inputs": self.fields(body, binding.fields, writable=True) if body else [],
            "has_body": body is not None,
        }

    def compile(self) -> Json:
        r = self.r
        listing = self.operation(r.listing, record=False, methods={"GET"})
        row = self._list_row(listing)
        result = {
            **r.model_dump(by_alias=True),
            "module": _module_name(self.owner),
            "columns": self.fields(row, r.columns, writable=False),
        }
        query = self._list_query(listing)
        result["filters"] = [
            _field(
                n,
                query[n]["schema"],
                self.document,
                False,
                r.labels.get(n, n),
                writable=True,
            )
            for n in r.filters
        ]
        self._check_sorting(query)
        if r.detail:
            detail = self.operation(r.detail, record=True, methods={"GET"})
            result["show"] = self.fields(
                _response(detail, self.document), r.show or r.columns, writable=False
            )
            if r.audit:
                detail_schema = resolve(_response(detail, self.document), self.document)
                field_schema = detail_schema.get("properties", {}).get(r.audit.id_field)
                if not field_schema or resolve(field_schema, self.document).get(
                    "type"
                ) not in {"string", "integer"}:
                    raise ValueError("audit binding needs a scalar detail field")
        elif (
            r.audit or r.show or r.edit or r.actions or r.show_widgets or r.edit_widgets
        ):
            raise ValueError("show/edit/actions/widgets require a detail API")
        self._compile_forms(result)
        if r.delete:
            if not r.detail:
                raise ValueError("delete requires detail API")
            self.operation(r.delete, record=True, methods={"DELETE"})
        self._compile_actions(result)
        return result

    def _list_row(self, listing: Json) -> Json:
        """The schema of one listed record (an array item with an id)."""
        r = self.r
        array = _response(listing, self.document)
        if r.items_field:
            _safe_name(r.items_field)
            array = resolve(array["properties"][r.items_field], self.document)
        if array.get("type") != "array":
            raise ValueError(
                f"{r.name}: list must return an array (or declare items_field)"
            )
        row = resolve(array["items"], self.document)
        if resolve(row.get("properties", {}).get("id", {}), self.document).get(
            "type"
        ) not in ("string", "integer"):
            raise ValueError(f"{r.name}: records need a string/integer id")
        return row

    def _list_query(self, listing: Json) -> dict[str, Json]:
        """The listing's query parameters, checked against paging / filters."""
        r = self.r
        query = {
            p["name"]: p for p in listing.get("parameters", []) if p["in"] == "query"
        }
        paging = r.pagination
        needed = (
            []
            if paging.mode == "all"
            else [
                paging.limit,
                paging.offset if paging.mode == "offset" else paging.cursor,
            ]
        )
        for name in [
            *needed,
            *r.filters,
            *([r.sort_parameter] if r.sort_parameter else []),
        ]:
            _safe_name(name)
            if name not in query:
                raise ValueError(f"{r.name}: undeclared API query parameter {name}")
        if paging.mode == "all" and any(
            n in query for n in (paging.limit, paging.offset, paging.cursor)
        ):
            raise ValueError("all pagination requires a genuinely unpaged API")
        if r.sort_parameter in needed or r.sort_parameter in r.filters:
            raise ValueError("sort parameter must not overwrite pagination/filters")
        if set(r.filters) & set(needed):
            raise ValueError("filters must not overwrite pagination parameters")
        if paging.mode != "all":
            limit = resolve(query[paging.limit]["schema"], self.document)
            if limit.get("type") != "integer" or limit.get(
                "maximum", float("inf")
            ) < paging.max_page_size + int(paging.mode == "offset"):
                raise ValueError(
                    "API limit cannot support declared page size/lookahead"
                )
        return query

    def _check_sorting(self, query: dict[str, Json]) -> None:
        r = self.r
        if bool(r.sort_fields) != bool(r.sort_parameter) or not set(
            r.sort_fields
        ) <= set(r.columns):
            raise ValueError(
                "sorting needs explicit sort_parameter and displayed sort_fields"
            )
        if r.sort_parameter:
            # One well-defined convention; never invent an API sorting protocol.
            sort_schema = resolve(query[r.sort_parameter]["schema"], self.document)
            allowed = sort_schema.get("enum", [])
            if not all(n in allowed and "-" + n in allowed for n in r.sort_fields):
                raise ValueError(
                    "sort parameter must explicitly enumerate field and -field"
                )

    def _compile_forms(self, result: Json) -> None:
        r = self.r
        for name, binding, record, methods in [
            ("create", r.create, False, {"POST"}),
            ("edit", r.edit, True, {"PUT", "PATCH"}),
        ]:
            if binding:
                result[name] = self.form(binding, record=record, methods=methods)
                self.fields(
                    _response(
                        self.operation(binding, record=record, methods=methods),
                        self.document,
                    ),
                    ["id"],
                    writable=False,
                )

    def _compile_actions(self, result: Json) -> None:
        action_names = set()
        result["actions"] = []
        for action in self.r.actions:
            if action.name in action_names:
                raise ValueError("duplicate action name")
            action_names.add(action.name)
            compiled = self.form(
                action, record=True, methods={"POST", "PUT", "PATCH", "DELETE"}
            )
            if action.idempotency_field is not None:
                matched = [
                    f
                    for f in compiled["inputs"]
                    if f["name"] == action.idempotency_field
                ]
                if (
                    len(matched) != 1
                    or matched[0].get("format") != "uuid"
                    or not matched[0]["required"]
                ):
                    raise ValueError(
                        "idempotency_field must name a required UUID input"
                    )
            if (
                action.typed_confirmation is not None
                and action.typed_confirmation
                not in {f["name"] for f in result.get("show", [])}
            ):
                raise ValueError("typed_confirmation must name a displayed field")
            result["actions"].append(compiled)


def compile_app(app: FastAPI) -> Json:
    """Discover admin.RESOURCES only on successfully loaded composition modules.

    No lifespan is entered. Export the full desired module set at build time;
    the static artifact is identical when those APIs are split behind a gateway.
    """
    declarations = []
    for descriptor in app.state.module_summary.loaded:
        name = descriptor.name + ".admin"
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:
                continue
            raise  # An error INSIDE a declaration must never be silently ignored.
        declarations.append((descriptor.name, module.RESOURCES))
    if app.state.module_summary.failed:
        raise ValueError("cannot export admin for a partially failed composition")
    operations: list[Json] = []
    for descriptor in app.state.module_summary.loaded:
        source = _module_name(descriptor.name)
        if not any(
            getattr(route, "path", "") == f"/api/v1/platform/{source}/audit"
            for route in app.routes
        ):
            continue
        for suffix, label, columns in (
            (
                "audit",
                "Audit log",
                [
                    "occurred_at",
                    "actor_id",
                    "action",
                    "resource_type",
                    "resource_id",
                    "request_id",
                ],
            ),
            ("services", "Service health", ["id", "status", "category"]),
        ):
            operations.append(
                {
                    "name": f"{suffix}_{source}",
                    "label": f"{label} · {source}",
                    "access": {"roles": ["admin"]},
                    "group": "Platform",
                    "icon": "audit" if suffix == "audit" else "health",
                    "list": {
                        "path": f"/api/v1/platform/{source}/{suffix}",
                        "method": "GET",
                    },
                    # Audit is keyset-paged (X-Next-Cursor, no totals).
                    "pagination": {"mode": "cursor" if suffix == "audit" else "all"},
                    "columns": columns,
                    "filters": [
                        "action_prefix",
                        "actor_id",
                        "resource_type",
                        "resource_id",
                        "since",
                        "until",
                    ]
                    if suffix == "audit"
                    else [],
                    "labels": {
                        c: c.replace("_", " ").capitalize()
                        for c in [*columns, "action_prefix", "since", "until"]
                    }
                    | {"action_prefix": "Action starts with"},
                }
            )
    if operations:
        declarations.append(("sandloom.core", operations))
    return compile_resources(app, declarations)
