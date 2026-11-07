"""FR-03 asset registration and scene-use seam tests through ``create_asset``."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import struct
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.adapters.blender.scene import (
    ObjectSpec,
    RenderRequest,
    SceneSpec,
    build_scene_plan,
    synthetic_interior,
)
from orchestrator.api.app import create_app
from orchestrator.domain.entities import (
    Asset,
    CameraProfile,
    Episode,
    Project,
    Shot,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.service.compose import compose_render_request
from orchestrator.store.json_store import ProjectStore
from tests.api.asset_requests import create_asset_request_kwargs

_STATIC_PLAN_SHA256 = "92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a"
_ASSET_ID = "asset-figure-1"
_SOURCE_URL = "https://assets.example.invalid/models/figure.glb"
_API_TOKEN = "asset-contract-test-token"


def _canonical_plan_bytes(plan: dict[str, object]) -> bytes:
    return json.dumps(
        plan,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    """Capture files and directories so an empty directory also counts as a write."""
    items: list[tuple[str, str, bytes | None]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            items.append((relative, "directory", None))
        elif path.is_file():
            items.append((relative, "file", path.read_bytes()))
    return tuple(sorted(items))


def _tiny_glb(bounds: tuple[float, float, float]) -> bytes:
    """Build a valid GLB 2.0 containing one indexed cuboid, without Blender/network."""
    x, y, z = bounds
    vertices = (
        (-x / 2, -y / 2, -z / 2), (x / 2, -y / 2, -z / 2),
        (x / 2, y / 2, -z / 2), (-x / 2, y / 2, -z / 2),
        (-x / 2, -y / 2, z / 2), (x / 2, -y / 2, z / 2),
        (x / 2, y / 2, z / 2), (-x / 2, y / 2, z / 2),
    )
    indices = (
        0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7,
        0, 1, 5, 0, 5, 4, 1, 2, 6, 1, 6, 5,
        2, 3, 7, 2, 7, 6, 3, 0, 4, 3, 4, 7,
    )
    position_bytes = b"".join(struct.pack("<3f", *vertex) for vertex in vertices)
    index_bytes = b"".join(struct.pack("<H", index) for index in indices)
    binary = position_bytes + index_bytes
    document: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "asset-contract-test"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(position_bytes), "target": 34962},
            {
                "buffer": 0,
                "byteOffset": len(position_bytes),
                "byteLength": len(index_bytes),
                "target": 34963,
            },
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": len(vertices),
                "type": "VEC3",
                "min": [-x / 2, -y / 2, -z / 2],
                "max": [x / 2, y / 2, z / 2],
            },
            {
                "bufferView": 1,
                "componentType": 5123,
                "count": len(indices),
                "type": "SCALAR",
            },
        ],
    }
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    binary_chunk = binary + b"\0" * ((-len(binary)) % 4)
    total_length = 12 + 8 + len(json_chunk) + 8 + len(binary_chunk)
    return b"".join((
        struct.pack("<4sII", b"glTF", 2, total_length),
        struct.pack("<I4s", len(json_chunk), b"JSON"), json_chunk,
        struct.pack("<I4s", len(binary_chunk), b"BIN\0"), binary_chunk,
    ))


def test_fixture_is_a_well_formed_glb_2_container() -> None:
    blob = _tiny_glb((0.5, 1.0, 2.0))
    magic, version, declared_length = struct.unpack("<4sII", blob[:12])
    json_length, json_kind = struct.unpack("<I4s", blob[12:20])
    document = json.loads(blob[20:20 + json_length])
    binary_header = 20 + json_length
    binary_length, binary_kind = struct.unpack("<I4s", blob[binary_header:binary_header + 8])

    assert (magic, version, declared_length) == (b"glTF", 2, len(blob)), (
        "fixture did not have a consistent GLB 2.0 header; "
        f"observed {(magic, version, declared_length)!r} for {len(blob)} bytes"
    )
    assert json_kind == b"JSON" and binary_kind == b"BIN\0", (
        "fixture chunks were not JSON followed by BIN; "
        f"observed {json_kind!r} and {binary_kind!r}"
    )
    assert binary_header + 8 + binary_length == len(blob), (
        "fixture binary chunk length did not end at the GLB boundary; "
        f"observed end {binary_header + 8 + binary_length}, file size {len(blob)}"
    )
    assert document["asset"]["version"] == "2.0" and len(document["accessors"]) == 2, (
        "fixture JSON did not describe a glTF 2.0 mesh with positions and indices; "
        f"observed asset={document.get('asset')!r}, accessors={document.get('accessors')!r}"
    )


def _asset(
    asset_id: str = _ASSET_ID,
    *,
    license: str = "CC0",
    source_url: str | None = _SOURCE_URL,
    generated: bool = False,
    asset_type: str = "model",
    attribution_surface_required: bool = True,
) -> Asset:
    return Asset(
        asset_id=asset_id,
        type=asset_type,
        generated=generated,
        source_url=source_url,
        provider="fixture-provider",
        license=license,
        attribution_surface_required=attribution_surface_required,
    )


def _create_asset_response(root: ProjectRoot, asset: Asset, source_path: Path):
    app = create_app(settings=Settings(project_root=root.path.parent), token=_API_TOKEN)
    route = next((
        item for item in app.routes
        if getattr(item, "path", None) == "/projects/{project_id}/assets"
        and "POST" in getattr(item, "methods", set())
    ), None)
    assert route is not None, "create_asset API route was absent from the app route table"
    kwargs = create_asset_request_kwargs(
        app,
        method="POST",
        path=route.path,
        asset=asset.model_dump(mode="json"),
        filename=source_path.name,
        content=source_path.read_bytes(),
    )
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as client:
        return client.post(
            route.path.replace("{project_id}", "p1"),
            headers={"X-Loopback-Token": _API_TOKEN},
            **kwargs,
        )


def _register_asset(root: ProjectRoot, asset: Asset, source_path: Path) -> Asset:
    response = _create_asset_response(root, asset, source_path)
    assert response.status_code in {200, 201}, (
        f"create_asset did not register {asset.asset_id!r}: "
        f"status={response.status_code}, body={response.text!r}"
    )
    return ProjectStore(ProjectRoot(root.path)).load(Asset, asset.asset_id)


def _project_store(path: Path, *, asset_ids: list[str] | None = None) -> tuple[ProjectRoot, ProjectStore]:
    path.mkdir(parents=True, exist_ok=True)
    base = ProjectRoot(path)
    base.make_dir("p1")
    root = ProjectRoot(base.resolve("p1"))
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="Asset fixture"), expected_version=0)
    store.save(
        Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30),
        expected_version=0,
    )
    store.create(CameraProfile(camera_id="cam-1", profile_name="Asset fixture camera"))
    store.create(Shot(
        shot_id="shot-1",
        episode_id="ep1",
        camera_profile="cam-1",
        duration_sec=4.0,
        asset_ids=list(asset_ids or []),
    ))
    return root, store


def _seed_asset_file(
    root: ProjectRoot,
    asset: Asset,
    relative_path: str,
    content: bytes,
) -> Asset:
    root.write_bytes(relative_path, content)
    return asset.model_copy(update={
        "file_path": relative_path,
        "checksum": hashlib.sha256(content).hexdigest(),
    })


def _refuse_asset_geometry(
    store: ProjectStore,
    asset_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    asset_record: Asset | None = None,
) -> str:
    if asset_record is not None:
        original_load = store.load

        def load_with_fixture(entity_type, entity_id, **kwargs):
            if entity_type is Asset and entity_id == asset_id:
                return asset_record
            return original_load(entity_type, entity_id, **kwargs)

        monkeypatch.setattr(store, "load", load_with_fixture)
    scene = _asset_scene(asset_id)
    compose_module = importlib.import_module("orchestrator.service.compose")
    monkeypatch.setattr(compose_module, "american_living_room", lambda **_kwargs: scene)
    try:
        request = compose_render_request(
            store,
            shot_id="shot-1",
            episode_id="ep1",
            run_id="run-refused-geometry",
            mode="master",
        )
        build_scene_plan(request)
    except Exception as error:  # noqa: BLE001 - refusal type is not fixed by this contract
        return str(error)
    pytest.fail(f"asset {asset_id!r} was accepted as geometry; observed worker plan {request!r}")


def _asset_fit_function():
    worker_path = Path(__file__).resolve().parents[2] / "orchestrator" / "adapters" / "blender" / "worker.py"
    spec = importlib.util.spec_from_file_location("asset_worker_fit_test", worker_path)
    assert spec is not None and spec.loader is not None, (
        f"could not load standalone Blender worker at {worker_path}; observed spec={spec!r}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "asset_fit_factors", None)
    assert callable(function), (
        f"worker did not expose module-level asset_fit_factors; observed {function!r}"
    )
    return function


def _asset_scene(
    asset_id: str,
    *,
    dimensions: tuple[float, float, float] = (0.4, 0.7, 1.8),
) -> SceneSpec:
    base = synthetic_interior()
    material_name = base.materials[0].name
    asset_object = ObjectSpec(
        name="ImportedFigure",
        kind="box",
        asset_id=asset_id,
        location=(4.0, 4.0, 0.0),
        dimensions=dimensions,
        material=material_name,
    )
    return base.model_copy(update={"objects": (asset_object,)})


def _asset_plan(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    authored_bounds: tuple[float, float, float],
) -> tuple[bytes, str, tuple[float, float, float]]:
    root, store = _project_store(path, asset_ids=[_ASSET_ID])
    raw = _tiny_glb(authored_bounds)
    source_rel = "incoming/figure.glb"
    root.write_bytes(source_rel, raw)
    registered = _register_asset(root, _asset(), root.resolve(source_rel))
    scene = _asset_scene(_ASSET_ID)
    compose_module = importlib.import_module("orchestrator.service.compose")
    monkeypatch.setattr(compose_module, "american_living_room", lambda **_kwargs: scene)
    request = compose_render_request(
        store,
        shot_id="shot-1",
        episode_id="ep1",
        run_id="run-asset",
        mode="master",
    )
    plan = build_scene_plan(request)
    return _canonical_plan_bytes(plan), registered.checksum or "", scene.objects[0].dimensions


def test_scene_without_assets_keeps_the_pinned_worker_plan_bytes() -> None:
    request = RenderRequest(
        shot_id="shot-1",
        shot_version=2,
        run_id="run-1",
        degrade_seed=9,
        duration_sec=1.01,
        camera_profile=CameraProfile(camera_id="cam", profile_name="test"),
        scene=synthetic_interior(),
    )
    observed = hashlib.sha256(_canonical_plan_bytes(build_scene_plan(request))).hexdigest()

    assert observed == _STATIC_PLAN_SHA256, (
        "assetless scene worker payload changed from the pinned plan; "
        f"observed SHA-256 {observed}, expected {_STATIC_PLAN_SHA256}"
    )


@pytest.mark.parametrize(
    ("license", "source_url", "missing_field"),
    [("", _SOURCE_URL, "license"), ("CC0", None, "source")],
    ids=["missing-license", "missing-source"],
)
def test_register_refuses_missing_reuse_right_or_source_without_writing(
    tmp_path: Path, license: str, source_url: str | None, missing_field: str,
) -> None:
    root, _store = _project_store(tmp_path / "project")
    content = _tiny_glb((0.5, 1.0, 2.0))
    root.write_bytes("incoming/figure.glb", content)
    before = _tree_snapshot(root.path)

    response = _create_asset_response(
        root,
        _asset(license=license, source_url=source_url),
        root.resolve("incoming/figure.glb"),
    )
    observed = response.text
    assert response.status_code in {400, 422}, (
        f"create_asset accepted missing {missing_field} provenance: "
        f"status={response.status_code}, body={observed!r}"
    )
    assert missing_field in observed.lower(), (
        "provenance rejection did not name the missing reuse right or source; "
        f"observed {observed!r}"
    )
    assert _tree_snapshot(root.path) == before, (
        "rejected asset registration changed project files or directories; "
        f"observed before={before!r}, after={_tree_snapshot(root.path)!r}"
    )


def test_generated_asset_without_source_url_registers_with_license(tmp_path: Path) -> None:
    root, _store = _project_store(tmp_path / "project")
    content = _tiny_glb((0.5, 1.0, 2.0))
    root.write_bytes("incoming/generated.glb", content)

    registered = _register_asset(
        root,
        _asset(asset_id="generated-figure", source_url=None, generated=True),
        root.resolve("incoming/generated.glb"),
    )

    assert registered.generated is True, (
        f"generated flag did not survive registration; observed {registered.generated!r}"
    )
    assert registered.source_url is None, (
        f"generated asset acquired a source_url; observed {registered.source_url!r}"
    )
    assert registered.license == "CC0", (
        f"generated asset did not retain its required license; observed {registered.license!r}"
    )


def test_gltf_registration_is_refused_for_external_buffers(tmp_path: Path) -> None:
    root, _store = _project_store(tmp_path / "project")
    gltf = json.dumps({
        "asset": {"version": "2.0"},
        "buffers": [{"uri": "mesh.bin", "byteLength": 4}],
    }).encode("utf-8")
    root.write_bytes("incoming/figure.gltf", gltf)
    before = _tree_snapshot(root.path)

    response = _create_asset_response(
        root,
        _asset(asset_id="external-gltf"),
        root.resolve("incoming/figure.gltf"),
    )
    observed = response.text.lower()
    assert response.status_code in {400, 422}, (
        f"create_asset accepted non-self-contained .gltf: "
        f"status={response.status_code}, body={response.text!r}"
    )
    assert ".gltf" in observed and "external" in observed and "buffer" in observed, (
        f".gltf refusal did not name external buffers; observed {response.text!r}"
    )
    assert _tree_snapshot(root.path) == before, (
        "rejected .gltf registration changed project files or directories; "
        f"observed before={before!r}, after={_tree_snapshot(root.path)!r}"
    )


def test_blend_geometry_is_refused_for_executable_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store = _project_store(tmp_path / "project", asset_ids=["unsafe-blend"])
    seeded = _seed_asset_file(
        root,
        _asset(asset_id="unsafe-blend"),
        "assets/models/unsafe-blend/unsafe.blend",
        b"fixture blend content",
    )

    observed = _refuse_asset_geometry(
        store,
        "unsafe-blend",
        monkeypatch,
        asset_record=seeded,
    ).lower()
    assert "unsafe-blend" in observed and ".blend" in observed, (
        f"geometry refusal did not name the asset and .blend format; observed {observed!r}"
    )
    assert any(reason in observed for reason in ("python", "executable", "script")), (
        f".blend refusal did not explain its executable-content risk; observed {observed!r}"
    )


def test_registered_asset_is_contained_hashed_and_keeps_attribution_obligation(
    tmp_path: Path,
) -> None:
    root, _store = _project_store(tmp_path / "project")
    content = _tiny_glb((0.5, 1.0, 2.0))
    root.write_bytes("incoming/figure.glb", content)

    registered = _register_asset(root, _asset(), root.resolve("incoming/figure.glb"))

    expected_checksum = hashlib.sha256(content).hexdigest()
    assert registered.checksum == expected_checksum, (
        "registered checksum did not hash the copied GLB bytes; "
        f"observed {registered.checksum!r}, expected {expected_checksum!r}"
    )
    assert registered.file_path is not None, (
        f"registered Asset has no project-relative file_path: {registered!r}"
    )
    registered_path = root.resolve(registered.file_path)
    assert registered_path.is_file(), (
        "registered file_path did not resolve to a contained project file; "
        f"observed {registered.file_path!r} -> {registered_path!s}"
    )
    assert registered_path.read_bytes() == content, (
        "registered project copy differs from the source GLB; "
        f"observed SHA-256 {hashlib.sha256(registered_path.read_bytes()).hexdigest()}"
    )

    reopened = ProjectStore(ProjectRoot(root.path)).load(Asset, _ASSET_ID)
    assert reopened.attribution_surface_required is True, (
        "attribution_surface_required was lost across the store round trip; "
        f"observed {reopened.attribution_surface_required!r}"
    )


def test_checksum_is_in_the_composed_worker_plan_and_changes_plan_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, first_checksum, _dimensions = _asset_plan(
        tmp_path / "first", monkeypatch, authored_bounds=(0.5, 1.0, 2.0),
    )
    second, second_checksum, _dimensions = _asset_plan(
        tmp_path / "second", monkeypatch, authored_bounds=(0.75, 1.0, 2.0),
    )

    assert first_checksum != second_checksum, (
        "different GLB bytes unexpectedly registered the same checksum; "
        f"observed {first_checksum!r} and {second_checksum!r}"
    )
    assert first_checksum.encode("ascii") in first, (
        "asset checksum was absent from the composed worker payload; "
        f"observed plan SHA-256 {hashlib.sha256(first).hexdigest()}"
    )
    assert second_checksum.encode("ascii") in second, (
        "changed asset checksum was absent from its composed worker payload; "
        f"observed plan SHA-256 {hashlib.sha256(second).hexdigest()}"
    )
    assert first != second, (
        "changing bytes for the same asset id did not change worker plan bytes; "
        f"observed identical plan SHA-256 {hashlib.sha256(first).hexdigest()}"
    )


def test_same_asset_request_builds_identical_worker_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store = _project_store(tmp_path / "project", asset_ids=[_ASSET_ID])
    root.write_bytes("incoming/figure.glb", _tiny_glb((0.5, 1.0, 2.0)))
    _register_asset(root, _asset(), root.resolve("incoming/figure.glb"))
    scene = _asset_scene(_ASSET_ID)
    compose_module = importlib.import_module("orchestrator.service.compose")
    monkeypatch.setattr(compose_module, "american_living_room", lambda **_kwargs: scene)

    def make_plan() -> bytes:
        request = compose_render_request(
            store,
            shot_id="shot-1",
            episode_id="ep1",
            run_id="run-deterministic",
            mode="master",
        )
        return _canonical_plan_bytes(build_scene_plan(request))

    first = make_plan()
    second = make_plan()
    assert first == second, (
        "two builds of one asset request were not byte-identical; "
        f"observed SHA-256 {hashlib.sha256(first).hexdigest()} and "
        f"{hashlib.sha256(second).hexdigest()}"
    )


def test_png_registers_but_is_refused_as_geometry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = "texture-png"
    root, store = _project_store(tmp_path / "project", asset_ids=[asset_id])
    content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    root.write_bytes("incoming/texture.png", content)

    registered = _register_asset(
        root,
        _asset(asset_id=asset_id, asset_type="image"),
        root.resolve("incoming/texture.png"),
    )
    assert registered.license == "CC0", (
        f"PNG registration did not retain its license; observed {registered.license!r}"
    )

    observed = _refuse_asset_geometry(store, asset_id, monkeypatch).lower()
    assert asset_id in observed and ".png" in observed, (
        f"PNG geometry refusal did not name the asset and format; observed {observed!r}"
    )


@pytest.mark.parametrize(
    ("source_min", "source_max", "desired", "expected_factors"),
    [
        ((0.0, 0.0, 0.0), (2.0, 2.0, 2.0), (1.0, 1.0, 1.0), (0.5, 0.5, 0.5)),
        ((-1.0, 0.0, 2.0), (3.0, 4.0, 8.0), (2.0, 1.0, 3.0), (0.5, 0.25, 0.5)),
    ],
    ids=["uniform-bounds", "non-uniform-bounds-and-target"],
)
def test_asset_fit_factors_make_mesh_envelope_match_declared_dimensions(
    source_min: tuple[float, float, float],
    source_max: tuple[float, float, float],
    desired: tuple[float, float, float],
    expected_factors: tuple[float, float, float],
) -> None:
    fit = _asset_fit_function()
    observed = fit(source_min, source_max, desired)
    assert observed == pytest.approx(expected_factors), (
        f"asset_fit_factors returned {observed!r}; expected per-axis factors {expected_factors!r}"
    )
    observed_envelope = tuple(
        (source_max[index] - source_min[index]) * observed[index]
        for index in range(3)
    )
    assert observed_envelope == pytest.approx(desired), (
        f"scaled source bounds produced envelope {observed_envelope!r}; "
        f"declared dimensions were {desired!r}"
    )


@pytest.mark.parametrize(
    ("source_min", "source_max", "axis"),
    [
        ((0.0, 0.0, 0.0), (1e-9, 1.0, 1.0), "x"),
        ((0.0, 0.0, 0.0), (1.0, 0.0, 1.0), "y"),
        ((0.0, 0.0, 0.0), (1.0, 1.0, float("inf")), "z"),
    ],
    ids=["extent-at-threshold", "zero-extent", "nonfinite-extent"],
)
def test_asset_fit_refuses_degenerate_extent_and_names_axis(
    source_min: tuple[float, float, float],
    source_max: tuple[float, float, float],
    axis: str,
) -> None:
    fit = _asset_fit_function()
    with pytest.raises(ValueError) as caught:
        fit(source_min, source_max, (1.0, 1.0, 1.0))
    observed = str(caught.value).lower()
    assert axis in observed, (
        f"degenerate {axis}-axis extent refusal did not name the axis; observed {observed!r}"
    )


def test_checksum_mismatch_fails_at_asset_use_without_project_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store = _project_store(tmp_path / "project", asset_ids=[_ASSET_ID])
    root.write_bytes("incoming/figure.glb", _tiny_glb((0.5, 1.0, 2.0)))
    registered = _register_asset(root, _asset(), root.resolve("incoming/figure.glb"))
    assert registered.file_path is not None, (
        f"registered asset has no file_path for use-time verification: {registered!r}"
    )
    root.write_bytes(registered.file_path, b"tampered asset bytes")
    scene = _asset_scene(_ASSET_ID)
    compose_module = importlib.import_module("orchestrator.service.compose")
    monkeypatch.setattr(compose_module, "american_living_room", lambda **_kwargs: scene)
    before = _tree_snapshot(root.path)

    with pytest.raises(Exception) as caught:
        compose_render_request(
            store,
            shot_id="shot-1",
            episode_id="ep1",
            run_id="run-mismatch",
            mode="master",
        )
    observed = str(caught.value)
    assert _ASSET_ID in observed, (
        "checksum mismatch did not identify the asset; "
        f"observed {observed!r}"
    )
    assert any(term in observed.lower() for term in ("checksum", "hash")), (
        "asset use failed for a reason other than the checksum mismatch; "
        f"observed {observed!r}"
    )
    assert _tree_snapshot(root.path) == before, (
        "failed asset use changed project files or directories; "
        f"observed before={before!r}, after={_tree_snapshot(root.path)!r}"
    )


def test_create_asset_is_exposed_as_a_mutating_api_operation() -> None:
    from orchestrator.api.routes import OPERATIONS

    operation = next((item for item in OPERATIONS if item.name == "create_asset"), None)
    assert operation is not None, (
        "OPERATIONS did not expose the contract's create_asset operation; "
        f"observed {[item.name for item in OPERATIONS]!r}"
    )
    assert operation.mutating, (
        "create_asset API operation was not marked mutating; "
        f"observed mutating={operation.mutating!r}"
    )
