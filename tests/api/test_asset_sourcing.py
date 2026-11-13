"""HTTP/CLI/manifest conformance for provider sourcing; only HTTP is faked."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from orchestrator.domain.entities import Asset, Project
from orchestrator.platform.containment import ProjectRoot
from orchestrator.store.json_store import ProjectStore

from .conftest import TOKEN, TOKEN_HEADER, operations
from .test_parity import _cli


class FakeHTTP:
    """Small deterministic Poly Haven fixture; no sockets or external tools."""

    def __init__(self, settings):
        self.calls: list[tuple[str, bool]] = []
        type(self).instances.append(self)

    instances: ClassVar[list[FakeHTTP]] = []

    def get_json(self, url: str, *, authenticated: bool = False):
        self.calls.append((url, authenticated))
        path = urlparse(url).path
        if path == "/search":
            return {"results": [{"slug": "chair-safe", "score": 1.0}]}
        if path == "/taxonomy/textures":
            return {"categories": [{
                "id": "98e4a9f0-9b68-4d81-b0f9-4f60f36fce31",
                "name": "Wood",
                "children": [],
            }]}
        if path.startswith("/info/"):
            return {
                "name": "Chair <script>", "type": 1,
                "authors": {"Fixture Artist": "https://polyhaven.com"},
                "thumbnail_url": "https://cdn.polyhaven.com/thumb.png",
            }
        if path.startswith("/files/"):
            descriptor = {
                "url": "https://dl.polyhaven.org/files/fixture.png",
                "size": len(_PNG), "md5": hashlib.md5(_PNG).hexdigest(),
            }
            return {"Diffuse": {"1k": {"png": descriptor, "jpg": descriptor}}}
        raise AssertionError(f"unexpected fake metadata request: {url}")

    def get_bytes(self, url: str, *, max_bytes: int, authenticated: bool = False):
        self.calls.append((url, authenticated))
        return _PNG


_png_buffer = io.BytesIO()
Image.new("RGB", (1, 1), (20, 30, 40)).save(_png_buffer, format="PNG")
_PNG = _png_buffer.getvalue()


def _op(app: FastAPI, name: str):
    return next(item for item in operations(app) if item.name == name)


def _seed_project(root: Path, project_id: str = "p1") -> ProjectStore:
    parent = ProjectRoot(root)
    parent.make_dir(project_id)
    store = ProjectStore(ProjectRoot(parent.resolve(project_id)))
    store.save(Project(project_id=project_id, title="Provider tests"), expected_version=0)
    return store


def _fake_http(monkeypatch):
    module = importlib.import_module("orchestrator.adapters.assets.http")
    FakeHTTP.instances.clear()
    fake = FakeHTTP
    monkeypatch.setattr(module, "AssetHTTP", fake)
    service = importlib.import_module("orchestrator.service.sourcing")
    monkeypatch.setattr(service, "AssetHTTP", fake)
    return fake


def test_asset_search_uses_query_fields_and_returns_typed_results(app: FastAPI, client: TestClient, monkeypatch):
    _seed_project(app.state.settings.project_root)
    fake_type = _fake_http(monkeypatch)
    op = _op(app, "search_assets")
    response = client.get(
        op.path.replace("{project_id}", "p1"),
        params={"provider": "polyhaven", "q": "chair", "asset_type": "texture", "limit": 5, "page": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "polyhaven"
    assert body["results"][0]["source_asset_id"] == "chair-safe"
    assert body["credit"] == "Assets from Poly Haven"
    assert fake_type


def test_asset_browser_escapes_remote_html_and_shows_provider_credit(app: FastAPI, client: TestClient, monkeypatch):
    _seed_project(app.state.settings.project_root)
    _fake_http(monkeypatch)
    browser = _op(app, "asset_browser")
    response = client.get(
        browser.path.replace("{project_id}", "p1"),
        params={"provider": "polyhaven", "q": "chair", "asset_type": "texture", "limit": 5, "page": 1},
    )
    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    assert "Assets from Poly Haven" in response.text
    assert "Chair &lt;script&gt;" in response.text
    assert "Chair <script>" not in response.text
    assert "<script>" not in response.text


def test_browser_empty_sketchfab_view_still_shows_saved_credit_and_polyhaven_form_credit(app: FastAPI, client: TestClient, monkeypatch):
    store = _seed_project(app.state.settings.project_root)
    store.save(Asset(
        asset_id="saved-credit", type="texture", generated=False,
        source_url="https://polyhaven.com/a/saved-credit", provider="polyhaven",
        license="CC0-1.0", attribution="Saved Artist — Poly Haven",
        attribution_surface_required=True,
    ), expected_version=0)
    fake_type = _fake_http(monkeypatch)
    browser = _op(app, "asset_browser")
    response = client.get(
        browser.path.replace("{project_id}", "p1"),
        params={"provider": "sketchfab", "asset_type": "model", "limit": 5, "page": 1},
    )
    assert response.status_code == 200, response.text
    assert "Saved Artist — Poly Haven" in response.text
    assert "Assets from Poly Haven" in response.text
    assert fake_type.instances == []


def test_acquire_is_token_protected_before_body_parse_or_provider_io(app: FastAPI, client: TestClient, monkeypatch):
    _seed_project(app.state.settings.project_root)
    fake_type = _fake_http(monkeypatch)
    route = _op(app, "acquire_asset")
    response = client.post(route.path.replace("{project_id}", "p1"), content=b"not-json")
    assert response.status_code == 401, response.text
    assert fake_type.instances == []


def test_acquire_requires_expected_version_before_provider_io(app: FastAPI, client: TestClient, monkeypatch):
    _seed_project(app.state.settings.project_root)
    fake_type = _fake_http(monkeypatch)
    route = _op(app, "acquire_asset")
    response = client.post(
        route.path.replace("{project_id}", "p1"), headers={TOKEN_HEADER: TOKEN},
        json={"provider": "polyhaven", "source_asset_id": "chair-safe", "asset_type": "texture"},
    )
    assert response.status_code == 422, response.text
    assert fake_type.instances == []


def test_acquire_registers_provider_rights_and_credit_in_project_store(app: FastAPI, client: TestClient, monkeypatch):
    store = _seed_project(app.state.settings.project_root)
    _fake_http(monkeypatch)
    route = _op(app, "acquire_asset")
    response = client.post(
        route.path.replace("{project_id}", "p1"), headers={TOKEN_HEADER: TOKEN},
        json={"provider": "polyhaven", "source_asset_id": "chair-safe", "asset_id": "chair-texture",
              "asset_type": "texture", "resolution": "1k", "map": "Diffuse", "format": "png",
              "expected_version": 0},
    )
    assert response.status_code == 200, response.text
    registered = Asset.model_validate(response.json())
    reopened = store.load(Asset, "chair-texture")
    assert registered.model_dump(mode="json") == reopened.model_dump(mode="json")
    assert reopened.generated is False
    assert reopened.provider == "polyhaven"
    assert reopened.license == "CC0-1.0"
    assert "Fixture Artist" in reopened.attribution
    assert "polyhaven.com/a/chair-safe" in reopened.source_url


def test_asset_operations_appear_in_manifest_and_cli(app: FastAPI):
    names = {o.name for o in operations(app)}
    assert {"search_assets", "acquire_asset", "asset_browser"} <= names
    manifest_module = importlib.import_module("orchestrator.api.manifest")
    manifest = manifest_module.build_manifest(app)
    tools = manifest["tools"] if isinstance(manifest, dict) else manifest
    published = {str(item["name"]).replace("-", "_") for item in tools}
    assert {"search_assets", "acquire_asset", "asset_browser"} <= published


def test_asset_search_cli_uses_typed_data_transport(app: FastAPI, tmp_path: Path, monkeypatch):
    _seed_project(app.state.settings.project_root)
    _seed_project(tmp_path, "p1")
    _fake_http(monkeypatch)
    result = CliRunner().invoke(
        _cli(),
        ["--project-root", str(tmp_path), "search-assets", "--project-id", "p1",
         "--data", json.dumps({"provider": "polyhaven", "q": "chair", "asset_type": "model", "limit": 5, "page": 1}), "--json"],
    )
    assert result.exit_code == 0, result.stderr or result.output
    payload = json.loads(result.stdout)
    assert payload["provider"] == "polyhaven"
    assert payload["credit"] == "Assets from Poly Haven"
