"""Containment and upload validation as seen through the HTTP surface (FR-25).

The API must add no second path check: these tests assert outcomes (nothing escapes the
root; rejections are 400 and name the field), not where the check lives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.platform.containment import UploadLimits
from orchestrator.platform.settings import Settings

from .conftest import (
    TOKEN,
    TOKEN_HEADER,
    Op,
    find_key,
    make_app,
    operations,
    sample_value,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 120
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 120
CRAFTED = ["../../outside.txt", "..\\..\\outside.txt", "/etc/passwd", "C:\\Windows\\evil", "a/../../b", "NUL"]


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _multipart_ops(app: FastAPI) -> list[Op]:
    spec = app.openapi()
    found = []
    for op in operations(app):
        content = spec["paths"][op.path][op.method.lower()].get("requestBody", {}).get("content", {})
        if "multipart/form-data" in content:
            found.append(op)
    return found


def _file_field(app: FastAPI, op: Op) -> tuple[str, dict[str, str]]:
    spec = app.openapi()
    content = spec["paths"][op.path][op.method.lower()]["requestBody"]["content"]["multipart/form-data"]
    schema = content["schema"]
    while "$ref" in schema:
        node: Any = spec
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        schema = node
    file_field = ""
    data: dict[str, str] = {}
    for name, prop in schema.get("properties", {}).items():
        if prop.get("format") == "binary" or prop.get("items", {}).get("format") == "binary":
            file_field = name
        elif name in schema.get("required", []):
            data[name] = str(sample_value(prop, spec))
    assert file_field, f"{op.key}: multipart route declares no binary field"
    return file_field, data


def _upload(client: TestClient, app: FastAPI, op: Op, filename: str, payload: bytes):
    field, data = _file_field(app, op)
    return client.request(
        op.method,
        op.url(),
        headers={TOKEN_HEADER: TOKEN},
        data=data,
        files={field: (filename, payload, "application/octet-stream")},
    )


@pytest.fixture
def upload_ops(app: FastAPI) -> list[Op]:
    ops = _multipart_ops(app)
    if not ops:
        pytest.fail("contract requires validated uploads but the app exposes no multipart route")
    return ops


def test_magic_bytes_contradicting_extension_is_rejected(client, app, upload_ops, tmp_path) -> None:
    for op in upload_ops:
        resp = _upload(client, app, op, "frame.png", JPEG)
        assert resp.status_code == 400, f"{op.key}: {resp.status_code} {resp.text}"
        assert TOKEN not in resp.text


def test_extension_outside_allow_list_is_rejected(client, app, upload_ops) -> None:
    for op in upload_ops:
        assert _upload(client, app, op, "payload.exe", PNG).status_code == 400


def test_rejection_names_the_offending_field(client, app, upload_ops) -> None:
    for op in upload_ops:
        resp = _upload(client, app, op, "frame.png", JPEG)
        assert "frame.png" in resp.text or "png" in resp.text.lower(), resp.text


def test_oversize_upload_reports_limit_and_observed(project_root: Path, tmp_path: Path) -> None:
    limit = 64
    settings = Settings(project_root=project_root, upload=UploadLimits(max_image_bytes=limit))
    app = make_app(settings)
    ops = _multipart_ops(app)
    if not ops:
        pytest.fail("contract requires validated uploads but the app exposes no multipart route")
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as client:
        for op in ops:
            resp = _upload(client, app, op, "big.png", PNG)  # len(PNG) > limit
            assert resp.status_code == 400, f"{op.key}: {resp.status_code} {resp.text}"
            body = resp.json()
            assert limit in find_key(body, lambda k: "limit" in k.lower() and "name" not in k.lower()), body
            assert len(PNG) in find_key(body, lambda k: "observed" in k.lower()), body


def test_crafted_upload_filename_cannot_escape_the_root(client, app, upload_ops, tmp_path, project_root) -> None:
    before = _tree(tmp_path)
    for op in upload_ops:
        for name in CRAFTED:
            _upload(client, app, op, name.replace("outside.txt", "outside.png") if name.startswith("..") else name, PNG)
    escaped = {p for p in _tree(tmp_path) - before if Path(p).parts[0] != project_root.name}
    assert not escaped, f"files written outside the project root: {escaped}"


def _string_fields(app: FastAPI, op: Op) -> list[str]:
    spec = app.openapi()
    schema = spec["paths"][op.path][op.method.lower()].get("requestBody", {}).get("content", {}).get(
        "application/json", {}
    ).get("schema")
    if not schema:
        return []
    ref = schema.get("$ref")
    if ref:
        schema = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    return [k for k, v in schema.get("properties", {}).items() if v.get("type") == "string"]


@pytest.mark.parametrize("crafted", CRAFTED)
def test_crafted_path_in_any_string_field_cannot_write_outside_root(
    client, app, tmp_path: Path, project_root: Path, crafted: str
) -> None:
    before = _tree(tmp_path)
    attempted = 0
    for op in operations(app):
        if not op.mutating or op.path.startswith("/__test__"):
            continue
        fields = _string_fields(app, op)
        if not fields:
            continue
        attempted += 1
        resp = client.request(
            op.method, op.url(), headers={TOKEN_HEADER: TOKEN}, json={f: crafted for f in fields}
        )
        assert resp.status_code != 500, f"{op.key} crashed on {crafted!r}: {resp.text}"
    assert attempted, "no mutating route with string fields to attack"
    escaped = {p for p in _tree(tmp_path) - before if Path(p).parts[0] != project_root.name}
    assert not escaped, f"files written outside the project root: {escaped}"
