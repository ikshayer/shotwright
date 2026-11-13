"""Provider sourcing integration tests. Network is replaced only at the HTTP seam."""

from __future__ import annotations

import errno
import hashlib
import json
import struct
import zipfile
from io import BytesIO
from pathlib import Path
from typing import ClassVar

import pytest
from PIL import Image
from pydantic import ValidationError

from orchestrator.adapters.assets.http import AssetHTTP
from orchestrator.adapters.assets.models import AssetAcquireRequest, AssetSearchRequest
from orchestrator.adapters.blender.scene import AssetBinding, ObjectSpec, RenderRequest, SceneSpec
from orchestrator.domain.entities import CameraProfile, Project
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.platform.settings import Settings
from orchestrator.service.assets import AssetRegistrationInvalid
from orchestrator.service.sourcing import acquire_asset, search_assets
from orchestrator.store.json_store import ProjectStore


class FakeHTTP:
    def __init__(self, json_responses: dict[str, object], byte_responses: dict[str, bytes] | None = None):
        self.json_responses = json_responses
        self.byte_responses = byte_responses or {}
        self.json_calls: list[tuple[str, bool]] = []
        self.byte_calls: list[tuple[str, int, bool]] = []

    def get_json(self, url: str, *, authenticated: bool = False) -> dict:
        self.json_calls.append((url, authenticated))
        for suffix, body in self.json_responses.items():
            if url.endswith(suffix):
                return body
        raise AssertionError(f"unexpected metadata URL: {url}")

    def get_bytes(self, url: str, *, max_bytes: int, authenticated: bool = False) -> bytes:
        self.byte_calls.append((url, max_bytes, authenticated))
        if url not in self.byte_responses:
            raise AssertionError(f"unexpected binary URL: {url}")
        payload = self.byte_responses[url]
        assert len(payload) <= max_bytes, "test fixture itself exceeds requested download cap"
        return payload


def _glb_json(data: bytes) -> dict:
    assert data[:4] == b"glTF"
    size, kind = struct.unpack_from("<II", data, 12)
    assert kind == 0x4E4F534A
    return json.loads(data[20:20 + size].rstrip(b" \0"))


def _tiny_glb() -> bytes:
    raw = json.dumps({"asset": {"version": "2.0", "generator": "synthetic"}}, separators=(",", ":")).encode()
    raw += b" " * (-len(raw) % 4)
    return struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(raw)) + struct.pack("<II", len(raw), 0x4E4F534A) + raw


def _glb_with_chunks(document: dict, binary_chunks: list[bytes]) -> bytes:
    raw = json.dumps(document, separators=(",", ":")).encode()
    raw += b" " * (-len(raw) % 4)
    chunks = [struct.pack("<II", len(raw), 0x4E4F534A) + raw]
    for binary in binary_chunks:
        binary += b"\0" * (-len(binary) % 4)
        chunks.append(struct.pack("<II", len(binary), 0x004E4942) + binary)
    total = 12 + sum(map(len, chunks))
    return struct.pack("<4sII", b"glTF", 2, total) + b"".join(chunks)


def _png_fixture() -> bytes:
    stream = BytesIO()
    Image.new("RGBA", (2, 2), (20, 40, 60, 255)).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def project_store(tmp_path: Path) -> tuple[ProjectStore, Settings]:
    (tmp_path / "project").mkdir()
    root = ProjectRoot(tmp_path / "project")
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="test"), expected_version=0)
    return store, Settings(project_root=root.path)


def test_search_uses_current_polyhaven_slug_results_and_returns_typed_credit(project_store) -> None:
    store, settings = project_store
    http = FakeHTTP({
        "/search?q=chair&t=models": {"results": [{"slug": "chair_01", "score": 1.0}]},
        "/info/chair_01": {
            "type": 2, "name": "Chair", "authors": {"maker": "https://example.invalid/maker"},
            "thumbnail_url": "https://cdn.polyhaven.com/thumb.jpg",
        },
    })
    result = search_assets(store, settings, AssetSearchRequest(q="chair"), http=http)
    assert result.provider == "polyhaven" and result.credit == "Assets from Poly Haven"
    assert result.credit_url == "https://polyhaven.com"
    assert len(result.results) == 1
    found = result.results[0]
    assert found.source_asset_id == "chair_01" and found.asset_type == "model"
    assert found.source_url == "https://polyhaven.com/a/chair_01"
    assert found.license == "CC0-1.0" and found.attribution_surface_required is True
    assert not http.byte_calls


@pytest.mark.parametrize("payload", [
    {"q": " "}, {"q": "x", "unexpected": True}, {"q": "x", "limit": 51},
    {"q": "x", "page": 4}, {"q": "x", "provider": "other"},
])
def test_search_request_rejects_blank_unknown_and_unbounded_values(payload: dict) -> None:
    with pytest.raises(ValidationError):
        AssetSearchRequest.model_validate(payload)


def test_acquire_rejects_nonzero_expected_version_before_any_provider_request(project_store) -> None:
    store, settings = project_store
    http = FakeHTTP({})
    request = AssetAcquireRequest(
        provider="polyhaven", source_asset_id="wooden_table_02", expected_version=1,
    )
    with pytest.raises(AssetRegistrationInvalid):
        acquire_asset(store, settings, request, http=http)
    assert http.json_calls == [] and http.byte_calls == [], (
        "expected_version conflict should be rejected before any provider access"
    )


def test_sketchfab_identifier_is_exactly_32_hex_characters() -> None:
    with pytest.raises(ValidationError):
        AssetAcquireRequest(provider="sketchfab", source_asset_id="not-a-uid", expected_version=0)
    valid = AssetAcquireRequest(
        provider="sketchfab", source_asset_id="a" * 32, expected_version=0,
    )
    assert valid.source_asset_id == "a" * 32


def test_polyhaven_multifile_model_is_registered_standalone_and_bindable(project_store) -> None:
    store, settings = project_store
    gltf = json.dumps({
        "asset": {"version": "2.0"},
        "buffers": [{"uri": "wooden_table_02.bin", "byteLength": 3}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 3}],
        "images": [{"uri": "textures/albedo.png", "mimeType": "image/png"}],
        "materials": [{"name": "Oak"}],
        "meshes": [{"primitives": [{"attributes": {}, "material": 0}]}],
    }).encode()
    model_files = gltf
    bin_bytes, tex_bytes = b"abc", _png_fixture()
    main_url, bin_url, tex_url = "https://dl.polyhaven.org/model.gltf", "https://dl.polyhaven.org/mesh.bin", "https://dl.polyhaven.org/albedo.png"
    def desc(url: str, value: bytes) -> dict:
        return {"url": url, "size": len(value), "md5": hashlib.md5(value).hexdigest()}
    http = FakeHTTP({
        "/info/wooden_table_02": {"type": 2, "name": "Wooden table", "authors": {"maker": "https://polyhaven.com"}},
        "/files/wooden_table_02": {"gltf": {"1k": {"gltf": {
            **desc(main_url, model_files), "include": {
                "wooden_table_02.bin": desc(bin_url, bin_bytes),
                "textures/albedo.png": desc(tex_url, tex_bytes),
            },
        }}}},
    }, {main_url: model_files, bin_url: bin_bytes, tex_url: tex_bytes})
    asset = acquire_asset(store, settings, AssetAcquireRequest(
        provider="polyhaven", source_asset_id="wooden_table_02", expected_version=0,
    ), http=http)
    assert asset.generated is False and asset.provider == "polyhaven"
    assert asset.license == "CC0-1.0" and asset.attribution_surface_required is True
    assert asset.checksum and len(asset.checksum) == 64
    normalized = store._root.read_bytes(asset.file_path)
    doc = _glb_json(normalized)
    assert not doc.get("images", [{}])[0].get("uri", "").startswith("textures/")
    assert doc["asset"].get("generator") or doc["asset"]["version"] == "2.0"

    reopened = ProjectStore(ProjectRoot(store._root.path))
    reread = reopened.load(type(asset), asset.asset_id)
    assert reread.checksum == asset.checksum
    assert reopened._root.read_bytes(reread.file_path) == normalized
    assert not any("signed" in p.name.lower() for p in reopened._root.path.rglob("*"))

    # A concrete imported-asset ObjectSpec is resolved by the actual scene-plan builder.
    obj = ObjectSpec(name="Sourced prop", kind="box", asset_id=asset.asset_id,
                     location=(0, 0, 0), dimensions=(1, 1, 1))
    scene = SceneSpec(
        materials=(), objects=(obj,), lights=(), camera_location=(0, -5, 2), camera_target=(0, 0, 0),
    )
    request = RenderRequest(
        shot_id="shot1", shot_version=1, run_id="sourcingtest", degrade_seed=0,
        duration_sec=1, camera_profile=CameraProfile(camera_id="cam1", profile_name="test"),
        scene=scene, asset_bindings={asset.asset_id: AssetBinding(
            asset_id=asset.asset_id, file_path=reread.file_path, checksum=reread.checksum,
        )},
    )
    from orchestrator.adapters.blender.scene import build_scene_plan
    plan = build_scene_plan(request)
    assert asset.asset_id in json.dumps(plan, default=str)


def test_polyhaven_corrupt_cached_source_fails_loudly_instead_of_redownloading(project_store) -> None:
    store, settings = project_store
    payloads = {
        "https://dl.polyhaven.org/model.gltf": json.dumps({"asset": {"version": "2.0"}}).encode(),
        "https://dl.polyhaven.org/mesh.bin": b"abc",
    }
    def desc(url: str) -> dict:
        data = payloads[url]
        return {"url": url, "size": len(data), "md5": hashlib.md5(data).hexdigest()}
    http = FakeHTTP({
        "/info/wooden_table_02": {"type": 2, "name": "Table", "authors": {}},
        "/files/wooden_table_02": {"gltf": {"1k": {"gltf": {
            **desc("https://dl.polyhaven.org/model.gltf"),
            "include": {"mesh.bin": desc("https://dl.polyhaven.org/mesh.bin")},
        }}}},
    }, payloads)
    acquire_asset(store, settings, AssetAcquireRequest(
        provider="polyhaven", source_asset_id="wooden_table_02", asset_id="cache-one", expected_version=0,
    ), http=http)
    cache_dirs = list(store._root.path.rglob("provider_cache"))
    assert cache_dirs, "successful provider fetch should use the documented project-local cache"
    cached_files = [item for directory in cache_dirs for item in directory.rglob("*") if item.is_file()]
    assert cached_files, "provider cache should contain content-addressed bytes"
    cached_files[0].write_bytes(b"tampered")
    calls_before = len(http.byte_calls)
    with pytest.raises(JobError):
        acquire_asset(store, settings, AssetAcquireRequest(
            provider="polyhaven", source_asset_id="wooden_table_02", asset_id="cache-two", expected_version=0,
        ), http=http)
    assert len(http.byte_calls) == calls_before, "corrupt cached data must fail without silent refetch"


def test_manifest_write_failure_does_not_register_partial_asset(project_store, monkeypatch) -> None:
    store, settings = project_store
    gltf = json.dumps({"asset": {"version": "2.0"}}).encode()
    url = "https://dl.polyhaven.org/model.gltf"
    desc = {"url": url, "size": len(gltf), "md5": hashlib.md5(gltf).hexdigest(), "include": {"placeholder": {}}}
    # Use a valid required include payload; it is intentionally not referenced by the scene.
    desc["include"] = {"unused.bin": {
        "url": "https://dl.polyhaven.org/unused.bin", "size": 1,
        "md5": hashlib.md5(b"x").hexdigest(),
    }}
    http = FakeHTTP({
        "/info/wooden_table_02": {"type": 2, "name": "Table", "authors": {}},
        "/files/wooden_table_02": {"gltf": {"1k": {"gltf": desc}}},
    }, {url: gltf, "https://dl.polyhaven.org/unused.bin": b"x"})
    original = ProjectRoot.write_text
    def fail_manifest(root, relative, text, *, encoding="utf-8"):
        normalized = relative.replace("\\", "/").lower()
        if "assets/provider_cache" in normalized and normalized.endswith(".json"):
            raise OSError(errno.ENOSPC, "synthetic manifest disk failure")
        return original(root, relative, text, encoding=encoding)
    monkeypatch.setattr(ProjectRoot, "write_text", fail_manifest)
    original_bytes = ProjectRoot.write_bytes
    def fail_manifest_bytes(root, relative, data):
        normalized = relative.replace("\\", "/").lower()
        if "assets/provider_cache" in normalized and normalized.endswith(".json"):
            raise OSError(errno.ENOSPC, "synthetic manifest disk failure")
        return original_bytes(root, relative, data)
    monkeypatch.setattr(ProjectRoot, "write_bytes", fail_manifest_bytes)
    with pytest.raises(JobError):
        acquire_asset(store, settings, AssetAcquireRequest(
            provider="polyhaven", source_asset_id="wooden_table_02", asset_id="manifest-fail",
            expected_version=0,
        ), http=http)
    assert not (store._root.path / "assets" / "registry" / "manifest-fail.json").exists()


def test_http_manual_redirect_strips_auth_and_rejects_unapproved_host(monkeypatch) -> None:
    opened = []
    class Response:
        status = 302
        headers: ClassVar[dict[str, str]] = {"Location": "https://media.sketchfab.com/file"}
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, size=-1): return b""
    class Final(Response):
        status = 200
        headers: ClassVar[dict[str, str]] = {}
        def __init__(self): self.read_once = False
        def read(self, size=-1):
            if self.read_once: return b""
            self.read_once = True
            return b"ok"
    def opener(request, timeout):
        opened.append(request)
        return Response() if len(opened) == 1 else Final()
    settings = Settings(project_root=Path.cwd(), credentials={"sketchfab": "TEST_SKETCHFAB_TOKEN"})
    monkeypatch.setenv("TEST_SKETCHFAB_TOKEN", "synthetic-test-token")
    client = AssetHTTP(settings, opener=opener, sleep=lambda _: None)
    assert client.get_bytes("https://api.sketchfab.com/v3/models/abc", max_bytes=20, authenticated=True) == b"ok"
    assert "Authorization" in opened[0].headers
    assert all("Authorization" not in request.headers for request in opened[1:])

    before = len(opened)
    def bad_opener(request, timeout):
        opened.append(request)
        response = Response()
        response.headers = {"Location": "https://127.0.0.1/private"}
        return response
    bad = AssetHTTP(settings, opener=bad_opener, sleep=lambda _: None)
    with pytest.raises(JobError):
        bad.get_bytes("https://api.sketchfab.com/v3/models/x", max_bytes=20, authenticated=True)
    assert len(opened) == before + 1, "unapproved redirect target must not be opened"


def test_zip_fixtures_can_represent_traversal_symlink_duplicate_and_bomb_cases() -> None:
    """Build tiny hostile archives for the service-level archive rejection tests."""
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.gltf", b"{}")
        symlink = zipfile.ZipInfo("links/scene.gltf")
        symlink.external_attr = 0o120777 << 16
        zf.writestr(symlink, b"{}")
        zf.writestr("Scene.gltf", b"{}")
        zf.writestr("scene.gltf", b"{}")
        zf.writestr("huge.bin", b"x" * 1024)
    assert len(archive.getvalue()) < 4096


@pytest.mark.parametrize(("status", "expected_calls"), [(429, 3), (404, 1)])
def test_http_retry_policy_retries_rate_limit_but_not_terminal_4xx(
    status: int, expected_calls: int, monkeypatch,
) -> None:
    calls = []
    sleeps = []
    class Response:
        def __init__(self):
            self.status = status
            self.headers = {"Retry-After": "999"}
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, _size=-1): return b""
    def opener(request, timeout):
        calls.append(request)
        return Response()
    settings = Settings(project_root=Path.cwd(), asset_providers={"max_attempts": 3})
    client = AssetHTTP(settings, opener=opener, sleep=sleeps.append)
    with pytest.raises(JobError):
        client.get_bytes("https://api.polyhaven.com/search?q=chair&t=models", max_bytes=32)
    assert len(calls) == expected_calls
    assert all(0 <= delay <= 60 for delay in sleeps)


@pytest.mark.parametrize("retry_after", [None, "not-a-number"])
def test_http_rate_limit_without_usable_retry_after_uses_positive_bounded_backoff(retry_after, monkeypatch) -> None:
    sleeps = []
    class Response:
        status = 429
        def __init__(self): self.headers = {} if retry_after is None else {"Retry-After": retry_after}
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, _size=-1): return b""
    settings = Settings(project_root=Path.cwd(), asset_providers={"max_attempts": 2})
    client = AssetHTTP(settings, opener=lambda *_args, **_kwargs: Response(), sleep=sleeps.append)
    with pytest.raises(JobError):
        client.get_bytes("https://api.polyhaven.com/search?q=x&t=models", max_bytes=32)
    assert sleeps and all(0 < delay <= 60 for delay in sleeps)


def _sketchfab_http(*, license_uid: str, license_slug: str, download_url: str, download_bytes: bytes) -> FakeHTTP:
    model_uid = "a" * 32
    cc0_uid = "7c23a1ba438d4306920229c12afcb5f9"
    by_uid = "322a749bcfa841b29dff1e8a1bb74b0b"
    catalogue = [
        {"uri": f"https://api.sketchfab.com/v3/licenses/{cc0_uid}", "slug": "cc0", "url": "https://creativecommons.org/publicdomain/zero/1.0/"},
        {"uri": f"https://api.sketchfab.com/v3/licenses/{by_uid}", "slug": "by", "url": "https://creativecommons.org/licenses/by/4.0/"},
    ]
    model = {
        "uid": model_uid, "name": "Synthetic chair", "isDownloadable": True,
        "user": {"displayName": "Example Artist", "profileUrl": "https://sketchfab.com/example"},
        "license": {"uid": license_uid, "slug": license_slug},
    }
    return FakeHTTP({
        f"/v3/models/{model_uid}": model,
        "/v3/licenses": {"results": catalogue},
        f"/v3/models/{model_uid}/download": {"glb": {"url": download_url, "size": len(download_bytes)}},
    }, {download_url: download_bytes})


@pytest.mark.parametrize(
    ("uid", "slug", "license_text"),
    [
        ("7c23a1ba438d4306920229c12afcb5f9", "cc0", "CC0-1.0"),
        ("322a749bcfa841b29dff1e8a1bb74b0b", "by", "CC-BY-4.0"),
    ],
)
def test_sketchfab_acquires_only_catalogue_authorized_licenses_with_complete_attribution(
    project_store, uid: str, slug: str, license_text: str,
) -> None:
    store, settings = project_store
    blob = _tiny_glb()
    url = "https://media.sketchfab.com/signed/model.glb"
    http = _sketchfab_http(license_uid=uid, license_slug=slug, download_url=url, download_bytes=blob)
    asset = acquire_asset(store, settings, AssetAcquireRequest(
        provider="sketchfab", source_asset_id="a" * 32, asset_id=f"sf-{slug}", expected_version=0,
    ), http=http)
    assert asset.license == license_text
    assert asset.generated is False and asset.provider == "sketchfab"
    assert "Synthetic chair" in (asset.attribution or "")
    assert "Example Artist" in (asset.attribution or "")
    assert "https://sketchfab.com/3d-models/" in (asset.source_url or "")
    if slug == "by":
        assert "https://creativecommons.org/licenses/by/4.0/" in (asset.attribution or "")
        assert asset.attribution_surface_required is True
    else:
        assert asset.attribution_surface_required is False
    # Signed download endpoints must never be persisted to the registry or project files.
    serialized = json.dumps(asset.model_dump(mode="json"))
    assert "signed/model.glb" not in serialized
    assert all("signed/model.glb" not in p.read_text(encoding="utf-8", errors="ignore")
               for p in store._root.path.rglob("*") if p.is_file())


@pytest.mark.parametrize(
    ("uid", "slug"),
    [
        ("11111111111111111111111111111111", "cc-by"),  # label/slug is not catalogue authority
        ("322a749bcfa841b29dff1e8a1bb74b0b", "by-nc"),
        ("ffffffffffffffffffffffffffffffff", "unknown"),
    ],
)
def test_sketchfab_refuses_unapproved_or_uncatalogued_license_before_download(
    project_store, uid: str, slug: str,
) -> None:
    store, settings = project_store
    http = _sketchfab_http(
        license_uid=uid, license_slug=slug, download_url="https://media.sketchfab.com/signed/no.glb",
        download_bytes=_tiny_glb(),
    )
    with pytest.raises(JobError):
        acquire_asset(store, settings, AssetAcquireRequest(
            provider="sketchfab", source_asset_id="a" * 32, expected_version=0,
        ), http=http)
    assert http.byte_calls == [], "license refusal must precede all binary model downloads"


@pytest.mark.parametrize("requested_type", ["texture", "hdri"])
def test_sketchfab_model_cannot_be_acquired_as_another_asset_type(
    project_store, requested_type: str,
) -> None:
    store, settings = project_store
    http = _sketchfab_http(
        license_uid="7c23a1ba438d4306920229c12afcb5f9", license_slug="cc0",
        download_url="https://media.sketchfab.com/signed/model.glb", download_bytes=_tiny_glb(),
    )
    with pytest.raises(ValidationError):
        acquire_asset(store, settings, AssetAcquireRequest(
            provider="sketchfab", source_asset_id="a" * 32, asset_type=requested_type,
            expected_version=0,
        ), http=http)
    assert http.byte_calls == []


@pytest.mark.parametrize("member", ["../escape.gltf", "scene.gltf", "Scene.gltf"])
def test_sketchfab_archive_rejects_traversal_symlink_and_case_duplicate_before_registration(
    project_store, member: str,
) -> None:
    store, settings = project_store
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        if member == "scene.gltf":
            info = zipfile.ZipInfo(member)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, b"{}")
        else:
            zf.writestr(member, b"{}")
            if member == "Scene.gltf":
                zf.writestr("scene.gltf", b"{}")
    blob = archive.getvalue()
    http = _sketchfab_http(
        license_uid="7c23a1ba438d4306920229c12afcb5f9", license_slug="cc0",
        download_url="https://media.sketchfab.com/signed/model.zip", download_bytes=blob,
    )
    # ZIP descriptor is the supported glTF fallback when GLB is absent.
    http.json_responses["/v3/models/" + "a" * 32 + "/download"] = {
        "gltf": {"url": "https://media.sketchfab.com/signed/model.zip", "size": len(blob)},
    }
    with pytest.raises(JobError):
        acquire_asset(store, settings, AssetAcquireRequest(
            provider="sketchfab", source_asset_id="a" * 32, expected_version=0,
        ), http=http)
    assert not list(store._root.path.glob("assets/registry/*")), "malformed archives must not register assets"


def test_sketchfab_fetches_fresh_download_metadata_for_each_acquisition(project_store) -> None:
    store, settings = project_store
    model_uid = "a" * 32
    blob = _tiny_glb()
    http = _sketchfab_http(
        license_uid="7c23a1ba438d4306920229c12afcb5f9", license_slug="cc0",
        download_url="https://media.sketchfab.com/signed/one.glb", download_bytes=blob,
    )
    first = acquire_asset(store, settings, AssetAcquireRequest(
        provider="sketchfab", source_asset_id=model_uid, asset_id="first", expected_version=0,
    ), http=http)
    second_url = "https://media.sketchfab.com/signed/two.glb"
    http.byte_responses[second_url] = blob
    http.json_responses[f"/v3/models/{model_uid}/download"] = {"glb": {"url": second_url, "size": len(blob)}}
    second = acquire_asset(store, settings, AssetAcquireRequest(
        provider="sketchfab", source_asset_id=model_uid, asset_id="second", expected_version=0,
    ), http=http)
    assert first.asset_id != second.asset_id
    assert [call[0] for call in http.byte_calls] == [
        "https://media.sketchfab.com/signed/one.glb", second_url,
    ]


@pytest.mark.parametrize("malformed_glb", [
    _glb_with_chunks({"asset": {"version": "2.0"}, "buffers": [{"byteLength": 1_000_000_000}]}, []),
    _glb_with_chunks({
        "asset": {"version": "2.0"}, "buffers": [{"byteLength": 1}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 2}],
    }, [b"x"]),
    _glb_with_chunks({"asset": {"version": "2.0"}, "buffers": [{"byteLength": 1}]}, [b"x", b"y"]),
])
def test_sketchfab_direct_glb_rejects_missing_or_invalid_bin_chunks_before_registration(
    project_store, malformed_glb: bytes,
) -> None:
    store, settings = project_store
    download_url = "https://media.sketchfab.com/signed/malformed.glb"
    http = _sketchfab_http(
        license_uid="7c23a1ba438d4306920229c12afcb5f9", license_slug="cc0",
        download_url=download_url, download_bytes=malformed_glb,
    )
    with pytest.raises(JobError):
        acquire_asset(store, settings, AssetAcquireRequest(
            provider="sketchfab", source_asset_id="a" * 32, asset_id="invalid-direct-glb",
            expected_version=0,
        ), http=http)
    assert not (store._root.path / "assets" / "registry" / "invalid-direct-glb.json").exists()


