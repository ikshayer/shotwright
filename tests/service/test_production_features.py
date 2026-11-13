"""Blind contract tests for overnight production follow-through features."""
from __future__ import annotations

import base64
import hashlib
import io
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from PIL import Image
from pydantic import ValidationError

from orchestrator.adapters.assets.models import AssetSearchRequest
from orchestrator.domain.entities import (
    CameraProfile,
    EnvironmentBinding,
    Episode,
    Location,
    Project,
    Shot,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore


def _png(size=(2, 1), color=(1, 2, 3)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


class ProviderHTTP:
    """Only the network boundary is replaced; registry/store remains real."""
    def __init__(self, settings, *, thumb_url="https://cdn.polyhaven.com/thumb.png", payload=None):
        self.calls = []
        self.thumb_url = thumb_url
        self.payload = _png() if payload is None else payload

    def get_json(self, url, *, authenticated=False):
        self.calls.append((url, authenticated))
        path = urlparse(url).path
        if path == "/search":
            # Deliberately includes a nonmatching leading candidate; category filtering
            # must happen before offset/page slicing rather than after pagination.
            return {"results": [{"slug": "other-first"}, {"slug": "chair-second"}]}
        if path.startswith("/taxonomy/"):
            return {"categories": [{"id": "dfe0e21f-ebb5-57e6-b76e-1b2c58bae1a3", "name": "Furniture", "children": []}]}
        if path == "/info/other-first":
            return {"name": "Other", "type": 2, "category_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "thumbnail_url": self.thumb_url}
        if path == "/info/chair-second":
            return {"name": "Chair", "type": 2, "category_id": "dfe0e21f-ebb5-57e6-b76e-1b2c58bae1a3", "thumbnail_url": self.thumb_url}
        raise AssertionError(f"unexpected metadata URL {url}")

    def get_bytes(self, url, *, max_bytes, authenticated=False):
        self.calls.append((url, authenticated))
        self.last_max_bytes = max_bytes
        return self.payload


def _store():
    root = Path.cwd() / "_work" / "production-feature-tests" / ("service-" + uuid.uuid4().hex)
    ProjectRoot(Path.cwd()).make_dir(root.relative_to(Path.cwd()).as_posix())
    store = ProjectStore(ProjectRoot(root))
    store.save(Project(project_id="p1", title="features"), expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=10), expected_version=0)
    store.create(CameraProfile(camera_id="cam1", profile_name="feature camera"))
    store.create(Shot(shot_id="shot1", episode_id="ep1", camera_profile="cam1", duration_sec=4, degrade_seed=1))
    return store, Settings(project_root=root)


def test_categories_are_bounded_typed_and_read_only(monkeypatch):
    store, settings = _store()
    from orchestrator.service import sourcing
    monkeypatch.setattr(sourcing, "AssetHTTP", ProviderHTTP)
    before = sorted(p.relative_to(store._root.path).as_posix() for p in store._root.path.rglob("*") if p.is_file())
    from orchestrator.service import sourcing
    result = sourcing.list_asset_categories(store, settings, sourcing.AssetCategoryRequest(provider="polyhaven", asset_type="model"))
    assert result["provider"] == "polyhaven" and result["asset_type"] == "model"
    assert {item["slug"] for item in result["categories"]} == {"dfe0e21f-ebb5-57e6-b76e-1b2c58bae1a3"}
    assert result["categories"][0]["count"] is None
    after = sorted(p.relative_to(store._root.path).as_posix() for p in store._root.path.rglob("*") if p.is_file())
    assert after == before


@pytest.mark.parametrize("categories", [["../private"], ["bad slug"], ["a", "a"], ["x" * 65]])
def test_search_category_slugs_are_safe_and_unique(categories):
    with pytest.raises(ValidationError):
        AssetSearchRequest(provider="polyhaven", q="chair", asset_type="model", categories=categories)


def test_category_filter_precedes_page_slice_and_polyhaven_query_is_encoded(monkeypatch):
    store, settings = _store()
    from orchestrator.service import sourcing
    monkeypatch.setattr(sourcing, "AssetHTTP", ProviderHTTP)
    http = ProviderHTTP(settings)
    result = sourcing.search_assets(
        store, settings,
        AssetSearchRequest(provider="polyhaven", q="chair & table", asset_type="model", categories=["dfe0e21f-ebb5-57e6-b76e-1b2c58bae1a3"], page=1, limit=1),
        http=http,
    )
    assert [item.source_asset_id for item in result.results] == ["chair-second"]
    search_url = next(url for url, _ in http.calls if urlparse(url).path == "/search")
    assert "chair%20%26%20table" in search_url or "chair+%26+table" in search_url
    assert all(not authenticated for _, authenticated in http.calls)


def test_preview_returns_verified_image_bytes_and_credit_without_writing():
    store, settings = _store()
    payload = _png((3, 2))
    http = ProviderHTTP(settings, payload=payload)
    before = list(store._root.path.rglob("*"))
    from orchestrator.service import sourcing
    result = sourcing.preview_asset(store, settings, sourcing.AssetPreviewRequest(provider="polyhaven", source_asset_id="chair-second"), http=http)
    assert result.media_type == "image/png" and (result.width, result.height) == (3, 2)
    assert base64.b64decode(result.data_base64) == payload
    assert result.credit and result.credit_url
    assert list(store._root.path.rglob("*")) == before
    assert all(not authenticated for _, authenticated in http.calls)


@pytest.mark.parametrize("url", [
    "https://polyhaven.com/thumb.png", "https://cdn.polyhaven.com.evil.test/thumb.png",
    "https://user@cdn.polyhaven.com/thumb.png?token=secret", "http://cdn.polyhaven.com/thumb.png",
])
def test_preview_refuses_unapproved_or_tokenized_thumbnail_hosts(url):
    store, settings = _store()
    with pytest.raises(JobError) as caught:
        from orchestrator.service import sourcing
        sourcing.preview_asset(store, settings, sourcing.AssetPreviewRequest(provider="polyhaven", source_asset_id="chair-second"), http=ProviderHTTP(settings, thumb_url=url))
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


def test_polyhaven_webp_thumbnail_query_is_allowed_but_mime_comes_from_decoded_bytes():
    store, settings = _store()
    payload = _png((3, 2))
    http = ProviderHTTP(settings, thumb_url=(
        "https://cdn.polyhaven.com/thumb.png?width=256&height=256&format=webp&v=005eb841"
    ), payload=payload)
    from orchestrator.service import sourcing
    result = sourcing.preview_asset(store, settings, sourcing.AssetPreviewRequest(
        provider="polyhaven", source_asset_id="chair-second"), http=http)
    assert result.media_type == "image/png"
    assert base64.b64decode(result.data_base64) == payload


def test_preview_rejects_bad_image_and_overlong_edge_and_overbyte_payload():
    store, settings = _store()
    bad = ProviderHTTP(settings, payload=b"not an image")
    with pytest.raises(JobError) as caught:
        from orchestrator.service import sourcing
        sourcing.preview_asset(store, settings, sourcing.AssetPreviewRequest(provider="polyhaven", source_asset_id="chair-second"), http=bad)
    assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    huge = ProviderHTTP(settings, payload=_png((2049, 1)))
    with pytest.raises(JobError) as caught:
        from orchestrator.service import sourcing
        sourcing.preview_asset(store, settings, sourcing.AssetPreviewRequest(provider="polyhaven", source_asset_id="chair-second"), http=huge)
    assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    oversized = ProviderHTTP(settings, payload=b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(JobError) as caught:
        from orchestrator.service import sourcing
        sourcing.preview_asset(store, settings, sourcing.AssetPreviewRequest(
            provider="polyhaven", source_asset_id="chair-second"), http=oversized)
    assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert oversized.last_max_bytes == 2 * 1024 * 1024


def test_preview_without_thumbnail_metadata_reports_unavailable_variant():
    store, settings = _store()
    http = ProviderHTTP(settings, thumb_url=None)
    from orchestrator.service import sourcing
    with pytest.raises(JobError) as caught:
        sourcing.preview_asset(store, settings, sourcing.AssetPreviewRequest(
            provider="polyhaven", source_asset_id="chair-second"), http=http)
    assert caught.value.kind is JobErrorKind.PROVIDER_HTTP_4XX
    assert [urlparse(url).path for url, _ in http.calls] == ["/info/chair-second"]


def test_sketchfab_preview_is_public_and_never_sends_credentials():
    store, settings = _store()
    # A model metadata fixture with a public thumbnail must be sufficient; preview
    # must not call its authenticated download endpoint.
    class SFHTTP(ProviderHTTP):
        def get_json(self, url, *, authenticated=False):
            self.calls.append((url, authenticated))
            if urlparse(url).path == "/v3/models/" + "a" * 32:
                return {"name": "Safe model", "uid": "a" * 32,
                        "user": {"displayName": "Maker", "profileUrl": "https://sketchfab.com/maker"},
                        "license": {"slug": "cc0"},
                        "thumbnails": {"images": [{"width": 640, "height": 480, "url": "https://media.sketchfab.com/thumb.jpg"}]}}
            raise AssertionError(f"unexpected URL {url}")
    from orchestrator.service import sourcing
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sourcing, "AssetHTTP", SFHTTP)
    try:
        http = SFHTTP(settings)
        result = sourcing.preview_asset(store, settings, sourcing.AssetPreviewRequest(provider="sketchfab", source_asset_id="a" * 32), http=http)
        assert result.media_type == "image/png" and base64.b64decode(result.data_base64) == http.payload
        assert all(not authenticated for _, authenticated in http.calls)
        assert not any("/download" in url for url, _ in http.calls)
    finally:
        monkeypatch.undo()


def test_contain_fit_is_uniform_and_exact_remains_axiswise():
    import importlib.util
    worker_path = Path(__file__).resolve().parents[2] / "orchestrator" / "adapters" / "blender" / "worker.py"
    spec = importlib.util.spec_from_file_location("production_fit_worker", worker_path)
    assert spec and spec.loader
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    source_min, source_max, desired = (0, 0, 0), (2, 1, 4), (1, 1, 1)
    exact = worker.asset_fit_factors(source_min, source_max, desired)
    contain = worker.asset_fit_factors(source_min, source_max, desired, fit_mode="contain")
    assert exact == pytest.approx((0.5, 1, 0.25))
    assert contain == pytest.approx((0.25, 0.25, 0.25))
    assert all((factor * extent) <= target for factor, extent, target in zip(contain, (2, 1, 4), desired))


def test_environment_binding_rejects_nonfinite_or_negative_and_location_overlap():
    with pytest.raises(ValidationError):
        EnvironmentBinding(asset_id="room-hdri", strength=float("nan"))
    with pytest.raises(ValidationError):
        EnvironmentBinding(asset_id="room-hdri", strength=-0.1)
    with pytest.raises(ValidationError):
        EnvironmentBinding(asset_id="room-hdri", rotation_rad=float("inf"))
    with pytest.raises(ValidationError):
        Location(location_id="room", asset_ids=["room-hdri"], environment=EnvironmentBinding(asset_id="room-hdri"))


def test_environment_binding_is_frozen_and_unknown_fields_rejected():
    binding = EnvironmentBinding(asset_id="room-hdri", strength=0.8, rotation_rad=0.3)
    with pytest.raises(ValidationError):
        EnvironmentBinding(asset_id="room-hdri", unknown="not allowed")
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        binding.strength = 2


def test_hdr_environment_preflight_uses_real_ffprobe_with_default_settings():
    """Exercise the adapter's actual decoder path without Blender or GPU work."""
    import shutil

    from orchestrator.adapters.blender.runner import BlenderAdapter
    from orchestrator.adapters.blender.scene import build_scene_plan
    from orchestrator.domain.entities import Asset, Job, Location, Shot
    from orchestrator.platform.settings import parse_settings
    from orchestrator.service.compose import compose_render_request

    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe is unavailable on this machine")
    store, _ = _store()
    root = store._root
    # Valid uncompressed, single-pixel Radiance RGBE; no Pillow HDR decoder needed.
    radiance = b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 1 +X 1\n" + bytes((128, 96, 64, 129))
    relative = "assets/test-environment.hdr"
    root.write_bytes(relative, radiance)
    asset = Asset(asset_id="test-environment", type="hdri", generated=False,
                   file_path=relative, checksum=hashlib.sha256(radiance).hexdigest(),
                   provider="polyhaven", license="CC0-1.0", attribution="Poly Haven",
                   source_url="https://polyhaven.com/a/test-environment")
    store.save(asset, expected_version=0)
    room = Location(location_id="hdr-room", environment=EnvironmentBinding(
        asset_id=asset.asset_id, strength=0.7, rotation_rad=0.25))
    store.create(room)
    current = store.load(Shot, "shot1", episode_id="ep1")
    store.revise(current.model_copy(update={**current.next_version(), "location_id": room.location_id, "asset_ids": []}),
                 expected_version=current.version)
    shot = store.load(Shot, "shot1", episode_id="ep1")
    request = compose_render_request(store, shot_id=shot.shot_id, episode_id=shot.episode_id,
                                     run_id="hdr-preflight", mode="master")
    plan = build_scene_plan(request)
    assert plan["scene"]["environment"]["binding"]["asset_id"] == asset.asset_id
    # The default Settings executable is the FFmpeg binary. Preflight still must
    # resolve and invoke its sibling ffprobe, then bind the authorized HDR path.
    default_settings = parse_settings({"project_root": str(root.path)})
    adapter = BlenderAdapter(ProjectRoot(root.path), default_settings)
    adapter._verify_plan_assets(plan, job=Job(job_id="hdr-preflight-job", type="build_scene",
        shot_id=shot.shot_id, episode_id=shot.episode_id, run_id=request.run_id), bind_paths=True)
    binding = plan["scene"]["environment"]["binding"]
    assert binding["asset_path"] == str(root.resolve(relative)).replace("\\", "/")
