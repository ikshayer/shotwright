from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from orchestrator.adapters.blender.scene import (
    KeyframeSpec,
    LightSpec,
    MaterialSpec,
    ObjectSpec,
    RenderRequest,
    SceneSpec,
    build_scene_plan,
    synthetic_interior,
)
from orchestrator.domain.entities import CameraProfile
from orchestrator.domain.seeds import INT32_MAX, derive_seed


def profile(**changes: object) -> CameraProfile:
    return CameraProfile(camera_id="cam", profile_name="test", **changes)


def request(**changes: object) -> RenderRequest:
    scene = SceneSpec(
        materials=(MaterialSpec(name="wall", color=(0.2, 0.2, 0.2, 1)),),
        objects=(ObjectSpec(name="wall", kind="box", location=(0, 0, 0),
                            dimensions=(2, 2, 0.1), material="wall"),),
        lights=(LightSpec(name="key", location=(0, 0, 2), energy=10,
                          color=(1, 0.8, 0.6)),),
        camera_location=(0, -3, 1), camera_target=(0, 0, 0),
    )
    defaults: dict[str, object] = {
        "shot_id": "shot-1", "shot_version": 2, "run_id": "run-1",
        "degrade_seed": 9, "duration_sec": 1.01,
        "camera_profile": profile(), "scene": scene,
    }
    defaults.update(changes)
    return RenderRequest(**defaults)


def test_models_reject_nonfinite_numbers_and_invalid_scene_references() -> None:
    with pytest.raises(ValidationError):
        MaterialSpec(name="bad", color=(math.nan, 0, 0, 1))
    with pytest.raises(ValidationError):
        LightSpec(name="bad", location=(0, 0, 0), energy=-1, color=(0, 0, 0))
    with pytest.raises(ValidationError):
        ObjectSpec(name="bad", kind="box", location=(0, 0, 0),
                   dimensions=(1, 0, 1), material="absent")


def test_scene_rejects_duplicate_names_keyframes_and_coincident_camera() -> None:
    mat = MaterialSpec(name="m", color=(0, 0, 0, 1))
    with pytest.raises(ValidationError):
        ObjectSpec(name="x", kind="box", location=(0, 0, 0), dimensions=(1, 1, 1),
                   material="m", keyframes=(KeyframeSpec(frame=2, location=(1, 0, 0)),
                                             KeyframeSpec(frame=2, location=(2, 0, 0))))
    with pytest.raises(ValidationError):
        SceneSpec(materials=(mat,), objects=(), lights=(), camera_location=(0, 0, 0),
                  camera_target=(0, 0, 0))


def test_master_plan_dimensions_frames_seed_and_rebuild_fields() -> None:
    req = request(camera_profile=profile(storage_resolution=(640, 480), fps=5))
    plan = build_scene_plan(req)
    assert plan["schema_version"] == 1
    assert (plan["width"], plan["height"]) == (1280, 960)
    assert plan["fps"] == 60
    assert (plan["frame_start"], plan["frame_end"]) == (1, 61)
    assert plan["seed"] == derive_seed("shot-1", 9, "blender", INT32_MAX)
    assert "fps" not in plan["camera"]


def test_every_post_only_field_is_excluded_from_plan() -> None:
    base = build_scene_plan(request())
    post_changed = build_scene_plan(request(camera_profile=profile(
        codec="mpeg2video", bitrate_kbps=1200, storage_resolution=(1920, 1080),
        display_aspect_ratio="16:9", fps=25, decimation_pattern="2:3",
        noise_model={"luma": 90}, chroma_smearing=0.3, block_artifacts=0.4,
        sharpening=0.2, grade_white_balance={"red_gain": 1.2},
        overlay_model={"camera_id_text": "CAM 1"}, analog_artifacts={"noise": {"luma": {"intensity": 90.0}}},
        frame_drop_schedule=[{"type": "drop", "start_frame": 1, "count": 2}])))
    assert base == post_changed


def test_master_resolution_is_explicit_and_4_by_3() -> None:
    larger = build_scene_plan(request(master_resolution=(1920, 1440)))
    assert (larger["width"], larger["height"]) == (1920, 1440)
    with pytest.raises(ValidationError):
        request(master_resolution=(1920, 1080))


@pytest.mark.parametrize("requested_engine", ["eevee", "cycles"])
def test_preview_uses_workbench_regardless_of_requested_master_engine(requested_engine: str) -> None:
    preview = build_scene_plan(request(mode="preview", engine=requested_engine, samples=99))
    assert (preview["width"], preview["height"]) == (640, 480)
    assert preview["frame_start"] == preview["frame_end"] == 1
    assert preview["engine"] == "workbench"


@pytest.mark.parametrize("changes", [
    {"shot_id": "../x"}, {"run_id": "run/x"}, {"shot_version": 0},
    {"degrade_seed": -1}, {"duration_sec": 0}, {"duration_sec": math.inf},
    {"samples": 0}, {"gpu_device": ""},
])
def test_request_rejects_invalid_identity_and_ranges(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(**changes)


def test_synthetic_interior_has_explicit_room_scale_and_unique_names() -> None:
    a, b = synthetic_interior(), synthetic_interior()
    assert a == b
    assert len({item.name for item in (*a.objects, *a.lights)}) == len(a.objects) + len(a.lights)
    assert any(max(obj.dimensions) >= 6 for obj in a.objects)
