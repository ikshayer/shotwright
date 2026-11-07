"""Request helpers for the public ``create_asset`` multipart operation."""

from __future__ import annotations

from typing import Any

ASSET_REGISTRATION_FIELDS = (
    "license",
    "source_url",
    "provider",
    "generated",
    "type",
    "attribution",
    "attribution_surface_required",
    "reuse_scope",
    "asset_id",
    "expected_version",
)


def asset_registration_data(
    asset: dict[str, Any], *, expected_version: int = 0,
) -> dict[str, Any]:
    """Return the flat JSON object accepted by the API and CLI registration surfaces."""
    data = {
        name: asset[name]
        for name in ASSET_REGISTRATION_FIELDS
        if name != "expected_version" and name in asset and asset[name] is not None
    }
    data["expected_version"] = expected_version
    return data


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _resolve_schema(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        node: Any = spec
        for part in schema["$ref"].lstrip("#/ ").split("/"):
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
    expected_version: int = 0,
) -> dict[str, Any]:
    """Build the pinned flat multipart shape from an Asset or registration mapping."""
    spec = app.openapi()
    request = spec["paths"][path][method.lower()].get("requestBody", {}).get("content", {})
    multipart = request.get("multipart/form-data", {}).get("schema")
    assert isinstance(multipart, dict), (
        f"{method} {path} did not publish a multipart request schema; observed {request!r}"
    )
    schema = _resolve_schema(multipart, spec)
    properties = schema.get("properties", {})
    assert "files" in properties and (
        properties["files"].get("format") == "binary"
        or properties["files"].get("contentMediaType")
        or (
            isinstance(properties["files"].get("items"), dict)
            and (
                properties["files"]["items"].get("format") == "binary"
                or properties["files"]["items"].get("contentMediaType")
            )
        )
    ), (
        f"{method} {path} did not publish the pinned binary field 'files'; "
        f"observed multipart properties {sorted(properties)!r}"
    )
    missing_fields = sorted(set(ASSET_REGISTRATION_FIELDS) - set(properties))
    assert not missing_fields, (
        f"{method} {path} did not publish AssetRegistration fields separately; "
        f"missing {missing_fields!r}, observed properties {sorted(properties)!r}"
    )

    flat_data = asset_registration_data(asset, expected_version=expected_version)
    return {
        "data": {name: _form_value(value) for name, value in flat_data.items()},
        "files": {"files": (filename, content, "application/octet-stream")},
    }
