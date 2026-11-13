"""HTTP, browser and CLI surface contract tests for public asset features."""
from __future__ import annotations

import importlib
import io
import uuid
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from PIL import Image

from orchestrator.domain.entities import Project
from orchestrator.platform.containment import ProjectRoot
from orchestrator.store.json_store import ProjectStore

from .conftest import operations


@pytest.fixture
def project_root():
    root = Path.cwd() / "_work" / "production-feature-tests" / ("api-" + uuid.uuid4().hex)
    ProjectRoot(Path.cwd()).make_dir(root.relative_to(Path.cwd()).as_posix())
    return root


def _op(app: FastAPI, name: str):
    return next(item for item in operations(app) if item.name == name)


def _seed(root: Path):
    parent = ProjectRoot(root)
    parent.make_dir("p1")
    store = ProjectStore(ProjectRoot(parent.resolve("p1")))
    store.save(Project(project_id="p1", title="production feature api"), expected_version=0)
    return store


_buf = io.BytesIO()
Image.new("RGB", (2, 1), (30, 40, 50)).save(_buf, format="PNG")
_PNG = _buf.getvalue()


class FakeHTTP:
    instances: ClassVar[list[FakeHTTP]] = []
    def __init__(self, settings):
        self.calls = []
        type(self).instances.append(self)
    def get_json(self, url, *, authenticated=False):
        self.calls.append((url, authenticated))
        path = urlparse(url).path
        if path.startswith("/taxonomy/"):
            return {"categories": [{"id": "dfe0e21f-ebb5-57e6-b76e-1b2c58bae1a3", "name": "Furniture", "children": []}]}
        if path == "/search":
            return {"results": [{"slug": "chair-safe", "score": 1.0}]}
        if path == "/info/chair-safe":
            return {"name": "Chair <script>", "type": 2, "category_id": "dfe0e21f-ebb5-57e6-b76e-1b2c58bae1a3",
                    "authors": {"Fixture Maker": "https://polyhaven.com"},
                    "thumbnail_url": "https://cdn.polyhaven.com/thumb.png"}
        raise AssertionError(f"unexpected URL: {url}")
    def get_bytes(self, url, *, max_bytes, authenticated=False):
        self.calls.append((url, authenticated))
        assert len(_PNG) <= max_bytes
        return _PNG


def _install(monkeypatch):
    import orchestrator.adapters.assets.http as http_module
    from orchestrator.service import sourcing
    monkeypatch.setattr(http_module, "AssetHTTP", FakeHTTP)
    monkeypatch.setattr(sourcing, "AssetHTTP", FakeHTTP)
    FakeHTTP.instances.clear()


def test_category_operation_and_repeated_category_search_query(app, client, monkeypatch):
    _seed(app.state.settings.project_root)
    _install(monkeypatch)
    names = {o.name for o in operations(app)}
    assert "list_asset_categories" in names
    assert "asset_preview" in names
    categories = _op(app, "list_asset_categories")
    response = client.get(categories.path.replace("{project_id}", "p1"), params={"provider": "polyhaven", "asset_type": "model"})
    assert response.status_code == 200, response.text
    assert response.json()["categories"][0]["slug"] == "dfe0e21f-ebb5-57e6-b76e-1b2c58bae1a3"
    search = _op(app, "search_assets")
    response = client.get(search.path.replace("{project_id}", "p1"), params=[("provider", "polyhaven"), ("q", "chair"), ("asset_type", "model"), ("categories", "dfe0e21f-ebb5-57e6-b76e-1b2c58bae1a3")])
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["source_asset_id"] == "chair-safe"


def test_preview_http_is_binary_decoded_and_has_exact_mime(app, client, monkeypatch):
    _seed(app.state.settings.project_root)
    _install(monkeypatch)
    route = _op(app, "asset_preview")
    response = client.get(route.path.replace("{project_id}", "p1"), params={"provider": "polyhaven", "source_asset_id": "chair-safe"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == "image/png"
    assert response.content == _PNG
    assert "nosniff" in response.headers.get("x-content-type-options", "").lower()
    assert all(not authenticated for instance in FakeHTTP.instances for _, authenticated in instance.calls)


def test_browser_uses_only_local_preview_url_and_keeps_credits_escaped(app, client, monkeypatch):
    _seed(app.state.settings.project_root)
    _install(monkeypatch)
    browser = _op(app, "asset_browser")
    response = client.get(browser.path.replace("{project_id}", "p1"), params={"provider": "polyhaven", "q": "chair", "asset_type": "model", "limit": 5, "page": 1})
    assert response.status_code == 200, response.text
    body = response.text
    assert "/assets/preview?" in body or "/assets/preview/" in body
    assert "cdn.polyhaven.com/thumb.png" not in body
    assert "Fixture Maker" in body and "Poly Haven" in body
    assert "Chair &lt;script&gt;" in body and "Chair <script>" not in body
    assert 'loading=\"lazy\"' in body or "loading='lazy'" in body
    assert 'alt="' in body or "alt=\'" in body


def test_categories_bad_request_and_unauthorized_mutation_guard(app, client, monkeypatch):
    _seed(app.state.settings.project_root)
    _install(monkeypatch)
    search = _op(app, "search_assets")
    bad = client.get(search.path.replace("{project_id}", "p1"), params={"provider": "polyhaven", "q": "chair", "asset_type": "model", "categories": "../private"})
    assert bad.status_code == 422
    acquire = _op(app, "acquire_asset")
    no_token = client.post(acquire.path.replace("{project_id}", "p1"), content=b"not-json")
    assert no_token.status_code == 401
    assert FakeHTTP.instances == []


def test_read_only_category_and_preview_do_not_change_registry(app, client, monkeypatch):
    store = _seed(app.state.settings.project_root)
    _install(monkeypatch)
    before = sorted(path.relative_to(store._root.path).as_posix() for path in store._root.path.rglob("*") if path.is_file())
    for name, params in (("list_asset_categories", {"provider": "polyhaven", "asset_type": "model"}),
                         ("asset_preview", {"provider": "polyhaven", "source_asset_id": "chair-safe"})):
        route = _op(app, name)
        response = client.get(route.path.replace("{project_id}", "p1"), params=params)
        assert response.status_code == 200, response.text
    after = sorted(path.relative_to(store._root.path).as_posix() for path in store._root.path.rglob("*") if path.is_file())
    assert after == before


def test_new_asset_operations_are_in_tool_manifest(app):
    manifest_module = importlib.import_module("orchestrator.api.manifest")
    manifest = manifest_module.build_manifest(app)
    tools = manifest["tools"] if isinstance(manifest, dict) else manifest
    published = {str(item["name"]).replace("-", "_") for item in tools}
    assert {"list_asset_categories", "asset_preview"} <= published


def test_blank_browser_form_does_not_fetch_categories_or_search(app, client, monkeypatch):
    _seed(app.state.settings.project_root)
    _install(monkeypatch)
    browser = _op(app, "asset_browser")
    response = client.get(browser.path.replace("{project_id}", "p1"),
                          params={"provider": "polyhaven", "asset_type": "model"})
    assert response.status_code == 200, response.text
    assert FakeHTTP.instances == []
