"""Request helpers for the public ``create_asset`` multipart operation."""

from __future__ import annotations

import json
from typing import Any


def _resolve_schema(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        node: Any = spec
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def create_asset_request_kwargs(
    app: Any,
    *,
    method: str,
    path: str,
    asset: dict[str, Any],
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    """Build a multipart request from the published schema and an Asset record.

    Amendment 1 requires create_asset to carry file provenance but does not name the
    multipart metadata field. This harness currently treats it as an ``asset`` JSON
    field; the API schema must publish that field so callers can discover it.
    """
    spec = app.openapi()
    request = spec["paths"][path][method.lower()].get("requestBody", {}).get("content", {})
    multipart = request.get("multipart/form-data", {}).get("schema")
    assert isinstance(multipart, dict), (
        f"{method} {path} did not publish a multipart request schema; observed {request!r}"
    )
    schema = _resolve_schema(multipart, spec)
    properties = schema.get("properties", {})
    binary_fields = [
        name for name, prop in properties.items()
        if prop.get("format") == "binary"
        or prop.get("contentMediaType")
        or (isinstance(prop.get("items"), dict) and (
            prop["items"].get("format") == "binary" or prop["items"].get("contentMediaType")
        ))
    ]
    assert len(binary_fields) == 1, (
        f"{method} {path} did not publish exactly one upload field; observed {binary_fields!r}"
    )
    assert "asset" in properties, (
        f"{method} {path} did not publish the assumed asset metadata field; "
        f"observed multipart properties {sorted(properties)!r}"
    )
    return {
        "data": {"asset": json.dumps(asset, sort_keys=True, separators=(",", ":"))},
        "files": {binary_fields[0]: (filename, content, "application/octet-stream")},
    }
