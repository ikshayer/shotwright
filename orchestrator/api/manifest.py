"""Agent tool manifest generated from the FastAPI route schema."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute


def build_manifest(app: FastAPI) -> dict[str, Any]:
    """Derive agent tools from the same registered routes that generate OpenAPI.

    The schema is regenerated from ``app.routes`` rather than read through
    ``app.openapi()``, which memoises into ``app.openapi_schema``. Reading that cache
    would hand back a schema predating any route registered since the first call, and
    populating it here would do the same to every later reader. Deriving from the
    route table is the contract's requirement, so the derivation must not be stale.
    """
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    paths = schema.get("paths", {})
    tools: list[dict[str, Any]] = []
    # Walked as routes rather than as schema entries so a tool is named after its route
    # function. An operationId is only reliable for a route that was given one
    # explicitly; FastAPI derives the rest from name, path and method together, which
    # would publish a tool called "brand_new_operation_brand_new_operation_get".
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        path_item = paths.get(route.path, {})
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            operation = path_item.get(method.lower())
            if operation is None:
                continue
            parameters = {
                "type": "object",
                "properties": {
                    item["name"]: item.get("schema", {"type": "string"})
                    for item in operation.get("parameters", [])
                    if item.get("in") == "path"
                },
                "required": [
                    item["name"]
                    for item in operation.get("parameters", [])
                    if item.get("in") == "path" and item.get("required")
                ],
                "additionalProperties": True,
            }
            request_body = operation.get("requestBody", {})
            content = request_body.get("content", {})
            # Multipart matters as much as JSON: without it an upload operation would be
            # published with no input at all, so an agent could not call it.
            body_schema = (
                content.get("application/json")
                or content.get("multipart/form-data")
                or {}
            ).get("schema")
            required = list(parameters["required"])
            if body_schema is not None and request_body.get("required"):
                required.append("payload")
            tools.append({
                "name": route.name,
                "description": operation.get("summary", ""),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        **parameters["properties"],
                        **({"payload": body_schema} if body_schema else {}),
                    },
                    "required": required,
                    "additionalProperties": False,
                },
                "http": {"method": method.upper(), "path": route.path},
            })
    # A body schema is a "$ref" into the generated components, so the components travel
    # with the manifest. Without them the references dangle and a tool definition cannot
    # be resolved into a callable shape by whoever reads it.
    return {"tools": tools, "components": schema.get("components", {})}
