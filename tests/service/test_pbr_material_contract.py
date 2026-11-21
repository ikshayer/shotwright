"""Independent contract tests for registered PBR bindings through persisted scenes."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from orchestrator.adapters.blender.runner import BlenderAdapter
from orchestrator.adapters.blender.scene import (
    CameraProfile,
    RenderRequest,
    build_scene_plan,
    synthetic_interior,
)
from orchestrator.domain import entities
from orchestrator.domain.base import FramingStatus, Invalidation, ShotStatus
from orchestrator.domain.entities import (
    Asset,
    Job,
    Location,
    Review,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.domain.invalidation import field_invalidation
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import parse_settings
from orchestrator.service.approval import approve_shot
from orchestrator.service.compose import compose_render_request
from orchestrator.service.dependencies import revise_location
from orchestrator.store.json_store import ProjectStore
from tests.service.test_asset_contract import (
    _asset,
    _project_store,
    _register_asset,
)

_BASE = {"base_color": "wall-albedo"}


def _png(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(out, format="PNG")
    return out.getvalue()


def _texture_set(**updates):
    model = getattr(entities, "PBRTextureSet", None)
    assert model is not None, f"PBRTextureSet missing from domain entities; observed {model!r}"
    values = {"maps": dict(_BASE)}
    values.update(updates)
    return model.model_validate(values)


def _new_project(path: Path):
    root, store = _project_store(path / "project")
    return root, store


def _register_png(root: ProjectRoot, asset_id: str, color=(128, 128, 128)) -> Asset:
    rel = f"incoming/{asset_id}.png"
    root.write_bytes(rel, _png(color))
    registered = _asset(asset_id=asset_id, asset_type="texture")
    return _register_asset(root, registered, root.resolve(rel))


def _material_name():
    from orchestrator.adapters.blender.scene import american_living_room
    return american_living_room().materials[0].name


def _bind_location(store: ProjectStore, location: Location) -> None:
    store.create(location)
    shot = store.load(Shot, "shot-1", episode_id="ep1")
    store.revise(shot.model_copy(update={**shot.next_version(), "location_id": location.location_id}),
                 expected_version=shot.version)


def _compose(store: ProjectStore, *, built=False):
    return compose_render_request(store, shot_id="shot-1", episode_id="ep1",
                                  run_id="pbr-contract", mode="master", require_built_scene=built)


def test_pbr_texture_set_validation_is_explicit_and_finite():
    model = entities.PBRTextureSet
    for payload in (
        {"maps": {"normal": "x"}},
        {"maps": {"base_color": ""}},
        {"maps": {"base_color": "bad id"}},
        {"maps": {"base_color": "x"}, "uv_scale": [0, 1]},
        {"maps": {"base_color": "x"}, "uv_scale": [float("inf"), 1]},
        {"maps": {"base_color": "x"}, "normal_strength": -1},
        {"maps": {"base_color": "x"}, "ao_strength": 1.1},
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)
    assert _texture_set(maps={"base_color": "same", "normal_gl": "same"}).maps == {
        "base_color": "same", "normal_gl": "same"
    }, "same registered image should be reusable across explicit roles"


def test_location_rejects_map_ids_overlapping_asset_ids():
    with pytest.raises(ValidationError):
        Location(location_id="pbr-room", material_overrides={
            _material_name(): _texture_set(),
        }, asset_ids=["wall-albedo"])


def test_texture_override_rebuild_tag_and_scoped_invalidation(tmp_path: Path):
    from datetime import UTC, datetime
    _root, store = _new_project(tmp_path)
    assert field_invalidation(Location, "material_overrides") is Invalidation.REBUILD, (
        "material_overrides was not tagged rebuild-triggering"
    )
    before = Location(location_id="pbr-room", material_overrides={_material_name(): _texture_set()})
    unrelated_location = Location(location_id="other-room")
    store.create(before)
    store.create(unrelated_location)

    def approved(shot_id: str, location_id: str, review_id: str) -> None:
        shot = Shot(shot_id=shot_id, episode_id="ep1", location_id=location_id, duration_sec=4.0)
        store.create(shot)
        for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
            values = {"status": status}
            if status is ShotStatus.BUILDING:
                values["framing_status"] = FramingStatus.APPROVED
            shot = shot.model_copy(update=values)
            store.update(shot, expected_version=store.load_with_version(Shot, shot_id, episode_id="ep1")[1])
        review = Review(review_id=review_id, target_id=shot_id, target_version=shot.version,
                        target_kind="shot", episode_id="ep1", actor="creator",
                        created_at=datetime.now(UTC), approval_state=ShotStatus.APPROVED)
        store.save(review, expected_version=0)
        approve_shot(store, shot_id, episode_id="ep1", review_id=review_id,
                     expected_version=store.load_with_version(Shot, shot_id, episode_id="ep1")[1])

    approved("dependent", before.location_id, "review-dependent")
    approved("unrelated", unrelated_location.location_id, "review-unrelated")
    current, token = store.load_with_version(Location, before.location_id)
    revised = current.model_copy(update={**current.next_version(), "material_overrides": {
        _material_name(): _texture_set(maps={"base_color": "wall-albedo", "roughness": "wall-r"})
    }})
    stale = revise_location(store, revised, expected_version=token)
    assert stale == ("dependent",), f"location edit did not stale only the approved dependent: {stale!r}"
    assert store.load(Shot, "dependent", episode_id="ep1").status is ShotStatus.STALE, (
        "approved dependent shot was not marked stale"
    )
    assert store.load(Shot, "dependent", episode_id="ep1").approval_review_id == "review-dependent", (
        "prior approval evidence was not retained"
    )
    assert store.load(Shot, "unrelated", episode_id="ep1").status is ShotStatus.APPROVED, (
        "unrelated approved shot was invalidated"
    )


def test_registered_texture_roundtrips_through_persisted_scene_and_adapter_verification(tmp_path: Path):
    root, store = _new_project(tmp_path)
    registered = _register_png(root, "wall-albedo")
    assert store.verify_asset("wall-albedo", usage="texture").checksum == registered.checksum, (
        "texture usage verifier did not preserve the registered checksum"
    )
    with pytest.raises(JobError):
        store.verify_asset("wall-albedo")
    loc = Location(location_id="pbr-room", material_overrides={_material_name(): _texture_set(
        maps={"base_color": "wall-albedo", "normal_gl": "wall-albedo"}
    )})
    _bind_location(store, loc)
    request = _compose(store)
    plan = build_scene_plan(request)
    materials = plan["scene"]["materials"]
    target = next(m for m in materials if m["name"] == _material_name())
    textures = target.get("textures")
    assert textures and textures["maps"] == {"base_color": "wall-albedo", "normal_gl": "wall-albedo"}, (
        f"material map identity missing from plan: {textures!r}"
    )
    binding = textures["bindings"]["base_color"]
    expected_binding = {"asset_id": "wall-albedo", "asset_checksum": registered.checksum,
                        "asset_file_path": registered.file_path}
    assert binding == expected_binding, f"wrong plan binding: {binding!r}"
    assert textures["bindings"]["normal_gl"] == expected_binding, (
        "one registered image could not be reused across explicit texture roles"
    )
    scene_rel = "scenes/shot-1/v0002/pbr-contract/scene.json"
    root.write_text(scene_rel, json.dumps({"schema_version": 1, "plan": plan}, sort_keys=True))
    output = ShotOutput(output_id="pbr-scene", episode_id="ep1", shot_id="shot-1",
                        record_version=2, kind=ShotOutputKind.SCENE, relative_path=scene_rel,
                        content_sha256=hashlib.sha256(root.read_bytes(scene_rel)).hexdigest())
    store.append_shot_output(output, expected_version=0)
    reopened = ProjectStore(ProjectRoot(root.path))
    rebuilt = _compose(reopened, built=True)
    assert rebuilt.scene.materials and next(
        m for m in rebuilt.scene.materials if m.name == _material_name()
    ).textures.maps == {"base_color": "wall-albedo", "normal_gl": "wall-albedo"}
    adapter = BlenderAdapter(ProjectRoot(root.path), parse_settings({"project_root": str(root.path)}))
    verified_plan = build_scene_plan(rebuilt)
    adapter._verify_plan_assets(verified_plan, job=Job(job_id="j-pbr", type="blender_render",
        shot_id="shot-1", run_id="pbr-contract"), bind_paths=True)
    bound = verified_plan["scene"]["materials"]
    target = next(m for m in bound if m["name"] == _material_name())
    for role in ("base_color", "normal_gl"):
        runtime_binding = target["textures"]["bindings"][role]
        assert runtime_binding["asset_path"] == str(root.resolve(registered.file_path)).replace("\\", "/"), (
            f"adapter did not authorize nested {role} texture path: {runtime_binding!r}"
        )


@pytest.mark.parametrize("kind,extension", [("reference", ".png"), ("texture", ".txt")])
def test_wrong_kind_or_extension_is_refused_before_adapter(tmp_path: Path, kind: str, extension: str):
    root, store = _new_project(tmp_path)
    content = _png((1, 2, 3)) if extension == ".png" else b"not an image"
    rel = f"incoming/wrong{extension}"
    root.write_bytes(rel, content)
    asset = _asset(asset_id="wrong-map", asset_type=kind).model_copy(update={
        "file_path": rel, "checksum": hashlib.sha256(content).hexdigest(),
    })
    store.save(asset, expected_version=0)
    loc = Location(location_id="pbr-room", material_overrides={
        _material_name(): _texture_set(maps={"base_color": "wrong-map"})
    })
    _bind_location(store, loc)
    with pytest.raises(JobError) as caught:
        build_scene_plan(_compose(store))
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST, (
        f"wrong texture kind/extension was not INVALID_MANIFEST: {caught.value!r}"
    )
    assert "wrong-map" in str(caught.value), f"bad texture refusal omitted asset id: {caught.value!r}"


def test_registry_tampering_is_rejected_during_composition(tmp_path: Path):
    root, store = _new_project(tmp_path)
    asset = _register_png(root, "wall-albedo")
    rel = asset.file_path
    assert rel
    root.write_bytes(rel, _png((1, 2, 3)))
    loc = Location(location_id="pbr-room", material_overrides={_material_name(): _texture_set()})
    _bind_location(store, loc)
    with pytest.raises(JobError) as caught:
        _compose(store)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST, (
        f"tampered texture was not INVALID_MANIFEST: {caught.value!r}"
    )
    assert "wall-albedo" in str(caught.value), f"tamper refusal omitted texture id: {caught.value!r}"


@pytest.mark.parametrize("bad_kind", ["corrupt", "oversized", "suffix-mismatch", "truncated-jpeg"])
def test_adapter_refuses_corrupt_or_oversized_texture_before_worker(tmp_path: Path, bad_kind: str):
    root, store = _new_project(tmp_path)
    if bad_kind == "corrupt":
        payload = b"\x89PNG\r\n\x1a\n" + b"not-a-real-png" * 3
    elif bad_kind in {"suffix-mismatch", "truncated-jpeg"}:
        out = io.BytesIO()
        Image.new("RGB", (32, 32), (1, 2, 3)).save(out, format="JPEG")
        payload = out.getvalue()
        if bad_kind == "truncated-jpeg":
            payload = payload[:-2]  # remove JPEG EOI; verify() alone accepts this truncation
        # suffix-mismatch deliberately keeps the .png suffix for JPEG bytes
    else:
        # A valid PNG one pixel beyond the configured long-edge limit; it is small
        # enough to keep this a CPU-only metadata verification case.
        cap = parse_settings({"project_root": str(root.path)}).upload.max_image_long_edge_px
        out = io.BytesIO()
        Image.new("RGB", (cap + 1, 1), (1, 2, 3)).save(out, format="PNG")
        payload = out.getvalue()
    extension = ".jpg" if bad_kind == "truncated-jpeg" else ".png"
    rel = f"incoming/{bad_kind}{extension}"
    root.write_bytes(rel, payload)
    asset = _asset(asset_id="bounded-map", asset_type="texture").model_copy(update={
        "file_path": rel, "checksum": hashlib.sha256(payload).hexdigest(),
    })
    store.save(asset, expected_version=0)
    loc = Location(location_id="pbr-room", material_overrides={
        _material_name(): _texture_set(maps={"base_color": "bounded-map"})
    })
    _bind_location(store, loc)
    plan = build_scene_plan(_compose(store))
    adapter = BlenderAdapter(ProjectRoot(root.path), parse_settings({"project_root": str(root.path)}))
    with pytest.raises(JobError) as caught:
        adapter._verify_plan_assets(plan, job=Job(job_id="j-bound", type="blender_render",
            shot_id="shot-1", run_id="pbr-contract"), bind_paths=True)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST, (
        f"{bad_kind} map did not classify as INVALID_MANIFEST: {caught.value!r}"
    )
    assert "bounded-map" in str(caught.value) and "base_color" in str(caught.value), (
        f"{bad_kind} refusal lacked safe asset/role context: {caught.value!r}"
    )



def test_empty_pbr_bindings_preserve_pinned_static_plan_hash():
    request = RenderRequest(
        shot_id="shot-1", shot_version=2, run_id="run-1", degrade_seed=9,
        duration_sec=1.01,
        camera_profile=CameraProfile(camera_id="cam", profile_name="test"),
        scene=synthetic_interior(),
    )
    encoded = json.dumps(build_scene_plan(request), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    observed = hashlib.sha256(encoded).hexdigest()
    expected = "92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a"
    assert observed == expected, f"legacy static worker plan hash changed: {observed}; expected {expected}"


def test_unknown_material_override_is_classified_and_names_target(tmp_path: Path):
    _root, store = _new_project(tmp_path)
    target = "not-an-existing-room-material"
    _bind_location(store, Location(location_id="pbr-room", material_overrides={
        target: _texture_set(),
    }))
    with pytest.raises(JobError) as caught:
        _compose(store)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST and target in str(caught.value), (
        f"unknown material override was not classified/named: {caught.value!r}"
    )



def test_persisted_scene_rejects_conflicting_texture_role_binding(tmp_path: Path):
    root, store = _new_project(tmp_path)
    _register_png(root, "wall-albedo")
    loc = Location(location_id="pbr-room", material_overrides={_material_name(): _texture_set(
        maps={"base_color": "wall-albedo", "normal_gl": "wall-albedo"}
    )})
    _bind_location(store, loc)
    plan = build_scene_plan(_compose(store))
    target = next(m for m in plan["scene"]["materials"] if m["name"] == _material_name())
    target["textures"]["bindings"]["normal_gl"]["asset_id"] = "conflicting-map-id"
    scene_rel = "scenes/shot-1/v0002/conflicting-pbr/scene.json"
    root.write_text(scene_rel, json.dumps({"schema_version": 1, "plan": plan}, sort_keys=True))
    output = ShotOutput(output_id="pbr-conflict", episode_id="ep1", shot_id="shot-1",
                        record_version=2, kind=ShotOutputKind.SCENE, relative_path=scene_rel,
                        content_sha256=hashlib.sha256(root.read_bytes(scene_rel)).hexdigest())
    store.append_shot_output(output, expected_version=0)
    reopened = ProjectStore(ProjectRoot(root.path))
    with pytest.raises(JobError) as caught:
        _compose(reopened, built=True)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST and "normal_gl" in str(caught.value), (
        f"conflicting persisted role binding was not refused with role context: {caught.value!r}"
    )
