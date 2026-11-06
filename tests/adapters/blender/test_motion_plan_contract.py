"""Contract tests for profile-gated, deterministic handheld camera motion."""

from __future__ import annotations

import hashlib
import itertools
import json
import math

import pytest
from pydantic import ValidationError

from orchestrator.adapters.blender.scene import (
    CameraKeyframeSpec,
    RenderRequest,
    SceneSpec,
    build_scene_plan,
    synthetic_interior,
)
from orchestrator.domain.base import Invalidation
from orchestrator.domain.entities import CameraProfile, Episode, Project, Shot
from orchestrator.domain.invalidation import field_invalidation
from orchestrator.platform.containment import ProjectRoot
from orchestrator.service.compose import compose_render_request
from orchestrator.service.dependencies import create_project
from orchestrator.store.json_store import ProjectStore

# Pinned by test_anomaly_plan_contract.py for the unchanged static fixture.
_STATIC_PLAN_SHA256 = "92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a"
_CCTV = "cctv"
_HANDHELD = "non_cctv"


def _request(
    *,
    camera_mode: str = _CCTV,
    camera_mount: str | None = None,
    degrade_seed: int = 9,
    motion_seed: int = 17,
    shot_id: str = "shot-1",
    scene: SceneSpec | None = None,
) -> RenderRequest:
    return RenderRequest(
        shot_id=shot_id,
        shot_version=2,
        run_id="run-1",
        degrade_seed=degrade_seed,
        motion_seed=motion_seed,
        duration_sec=6.0,
        camera_profile=CameraProfile(
            camera_id="cam",
            profile_name="test",
            camera_mode=camera_mode,
            camera_mount=camera_mount,
        ),
        scene=scene or synthetic_interior(),
    )


def _legacy_static_request(
    *,
    scene: SceneSpec | None = None,
    camera_profile: CameraProfile | None = None,
) -> RenderRequest:
    """Today's static request shape, used to pin compatibility and attack paths."""
    return RenderRequest(
        shot_id="shot-1",
        shot_version=2,
        run_id="run-1",
        degrade_seed=9,
        duration_sec=1.01,
        camera_profile=camera_profile or CameraProfile(
            camera_id="cam", profile_name="test",
        ),
        scene=scene or synthetic_interior(),
    )


def _canonical_plan_bytes(plan: dict[str, object]) -> bytes:
    return json.dumps(
        plan,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _camera_keyframes(plan: dict[str, object]) -> list[dict[str, object]]:
    scene = plan.get("scene")
    assert isinstance(scene, dict), f"worker plan scene was not a mapping: {scene!r}"
    keyframes = scene.get("camera_keyframes", [])
    assert isinstance(keyframes, list), (
        "worker plan camera_keyframes was not a list; observed "
        f"{keyframes!r}"
    )
    return keyframes


def _transforms(plan: dict[str, object]) -> tuple[tuple[int, tuple[float, ...]], ...]:
    frames = _camera_keyframes(plan)
    result = []
    for item in frames:
        frame = item.get("frame")
        location = item.get("location")
        target = item.get("target")
        assert isinstance(frame, int), f"camera keyframe had invalid frame: {item!r}"
        assert isinstance(location, list) and len(location) == 3, (
            f"camera keyframe had invalid location: {item!r}"
        )
        assert isinstance(target, list) and len(target) == 3, (
            f"camera keyframe had invalid target: {item!r}"
        )
        result.append((frame, tuple(float(value) for value in (*location, *target))))
    return tuple(result)


def _assert_no_camera_motion(plan: dict[str, object], route: str) -> None:
    observed = _camera_keyframes(plan)
    assert not observed, (
        f"CCTV {route} produced camera keyframes in the worker plan; observed {observed!r}"
    )


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    magnitude = math.sqrt(sum(value * value for value in vector))
    assert magnitude > 0, f"camera direction had zero length: {vector!r}"
    return tuple(value / magnitude for value in vector)


def _angle_degrees(
    first: tuple[float, float, float], second: tuple[float, float, float],
) -> float:
    dot = sum(left * right for left, right in zip(first, second, strict=True))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def test_unchanged_static_scene_plan_keeps_todays_worker_bytes() -> None:
    request = _legacy_static_request()
    observed = hashlib.sha256(_canonical_plan_bytes(build_scene_plan(request))).hexdigest()

    assert observed == _STATIC_PLAN_SHA256, (
        "unchanged static scene worker payload changed: observed canonical SHA-256 "
        f"{observed}, expected {_STATIC_PLAN_SHA256}"
    )


def test_handheld_plan_moves_the_camera_across_worker_frames() -> None:
    request = _request(camera_mode=_HANDHELD)
    plan = build_scene_plan(request)
    observed = _transforms(plan)
    distinct = {transform for _frame, transform in observed}

    assert len(observed) >= 2, (
        "handheld worker plan had fewer than two camera samples; observed "
        f"{observed!r}"
    )
    assert len(distinct) >= 2, (
        "handheld plan carried keyframe data but no effective camera transform moved; "
        f"observed {observed!r}"
    )


def test_cctv_plan_has_no_camera_motion_keyframes() -> None:
    profile = CameraProfile(camera_id="dvr-2005", profile_name="dvr-2005")
    plan = build_scene_plan(_legacy_static_request(camera_profile=profile))

    _assert_no_camera_motion(plan, "profile mode")


def test_motion_is_repeatable_and_uses_motion_seed_not_degrade_seed() -> None:
    request = _request(camera_mode=_HANDHELD, motion_seed=23, degrade_seed=9)
    first = build_scene_plan(request)
    second = build_scene_plan(request)
    changed_degrade = build_scene_plan(
        _request(camera_mode=_HANDHELD, motion_seed=23, degrade_seed=10)
    )
    changed_motion = build_scene_plan(
        _request(camera_mode=_HANDHELD, motion_seed=24, degrade_seed=9)
    )
    changed_shot = build_scene_plan(
        _request(
            camera_mode=_HANDHELD,
            motion_seed=23,
            degrade_seed=9,
            shot_id="shot-2",
        )
    )

    first_bytes = _canonical_plan_bytes(first)
    second_bytes = _canonical_plan_bytes(second)
    assert first_bytes == second_bytes, (
        "same render request produced different canonical plans including motion: "
        f"observed SHA-256 {hashlib.sha256(first_bytes).hexdigest()} and "
        f"{hashlib.sha256(second_bytes).hexdigest()}"
    )
    assert _transforms(first) == _transforms(changed_degrade), (
        "changing post-only degrade_seed changed camera transforms; observed "
        f"{_transforms(first)!r} versus {_transforms(changed_degrade)!r}"
    )
    assert _transforms(first) != _transforms(changed_motion), (
        "changing motion_seed did not change camera transforms; observed "
        f"{_transforms(first)!r} versus {_transforms(changed_motion)!r}"
    )
    assert _transforms(first) != _transforms(changed_shot), (
        "changing shot_id did not change camera transforms at the same motion_seed; "
        f"observed {_transforms(first)!r} versus {_transforms(changed_shot)!r}"
    )


def test_motion_seed_is_recorded_on_shot() -> None:
    assert "motion_seed" in Shot.model_fields, (
        "Shot has no motion_seed field; observed fields "
        f"{sorted(Shot.model_fields)}"
    )
    shot = Shot(
        shot_id="motion-shot",
        episode_id="episode-1",
        duration_sec=2.0,
        motion_seed=37,
    )

    assert shot.motion_seed == 37, (
        f"Shot did not retain motion_seed=37; observed {shot.motion_seed!r}"
    )


def test_motion_model_semver_is_recorded_on_shot() -> None:
    version_fields = [
        name for name in Shot.model_fields
        if "motion" in name and ("version" in name or "semver" in name)
    ]
    assert len(version_fields) == 1, (
        "Shot must record one camera motion model semver field; observed candidate fields "
        f"{version_fields!r}"
    )
    version_field = version_fields[0]
    shot = Shot(
        shot_id="motion-shot",
        episode_id="episode-1",
        duration_sec=2.0,
        **{version_field: "1.2.3"},
    )

    assert getattr(shot, version_field) == "1.2.3", (
        f"Shot did not retain model semver in {version_field!r}; observed "
        f"{getattr(shot, version_field)!r}"
    )


def test_seeded_profiles_have_closed_cctv_and_non_cctv_modes(tmp_path) -> None:
    store = ProjectStore(ProjectRoot(tmp_path))
    create_project(store, Project(project_id="motion-project", title="Motion contract"))
    profiles = {
        name: store.load(CameraProfile, name)
        for name in ("dvr-2005", "low-light-bw", "consumer-2002")
    }
    observed = {
        name: getattr(profile, "camera_mode", None)
        for name, profile in profiles.items()
    }

    assert observed == {
        "dvr-2005": _CCTV,
        "low-light-bw": _CCTV,
        "consumer-2002": _HANDHELD,
    }, f"seeded camera profile modes were incorrect; observed {observed!r}"


def test_camera_mode_rejects_values_outside_its_closed_vocabulary() -> None:
    assert "camera_mode" in CameraProfile.model_fields, (
        "CameraProfile has no typed camera_mode field; observed fields "
        f"{sorted(CameraProfile.model_fields)}"
    )
    with pytest.raises(ValidationError, match="camera_mode"):
        CameraProfile(
            camera_id="invalid-mode",
            profile_name="invalid-mode",
            camera_mode="unmotivated_dolly",
        )


def test_camera_mode_is_rebuild_triggering() -> None:
    observed = field_invalidation(CameraProfile, "camera_mode")

    assert observed is Invalidation.REBUILD, (
        "camera_mode must invalidate the Blender master; observed invalidation "
        f"{observed!r}"
    )


def test_cctv_mode_overrules_handheld_camera_mount_profile_field() -> None:
    profile = CameraProfile(
        camera_id="dvr-2005",
        profile_name="dvr-2005",
        camera_mount="handheld",
    )
    plan = build_scene_plan(_legacy_static_request(camera_profile=profile))

    _assert_no_camera_motion(plan, "camera_mount='handheld' profile field")


def test_cctv_rejects_or_ignores_a_request_camera_motion_selector() -> None:
    base = _legacy_static_request()
    payload = base.model_dump(mode="python")
    payload["camera_motion"] = "handheld"
    try:
        injected = RenderRequest.model_validate(payload)
    except ValidationError as error:
        observed = str(error)
        assert "camera_motion" in observed, (
            "request rejected an injected camera_motion selector without naming it; "
            f"observed {observed!r}"
        )
        return

    plan = build_scene_plan(injected)
    _assert_no_camera_motion(plan, "request camera_motion selector")


def test_cctv_cannot_be_moved_by_caller_supplied_scene_camera_keyframes() -> None:
    scene = synthetic_interior()
    first_location = scene.camera_location
    second_location = (
        first_location[0] + 0.02,
        first_location[1],
        first_location[2],
    )
    caller_scene = scene.model_copy(update={
        "camera_keyframes": (
            CameraKeyframeSpec(
                frame=1,
                location=first_location,
                target=scene.camera_target,
            ),
            CameraKeyframeSpec(
                frame=61,
                location=second_location,
                target=scene.camera_target,
            ),
        ),
    })
    # This legacy-shaped request deliberately reaches the existing planner even on a
    # tree that has not added camera_mode yet. The seeded profile name supplies the
    # intended CCTV identity once the new mode is introduced.
    profile = CameraProfile(camera_id="dvr-2005", profile_name="dvr-2005")
    try:
        plan = build_scene_plan(_legacy_static_request(
            scene=caller_scene, camera_profile=profile,
        ))
    except (ValidationError, ValueError) as error:
        observed = str(error)
        assert any(word in observed.casefold() for word in ("camera", "motion", "cctv")), (
            "CCTV rejected caller keyframes without identifying camera motion; "
            f"observed {observed!r}"
        )
        return

    _assert_no_camera_motion(plan, "caller-supplied SceneSpec.camera_keyframes")


def test_cctv_shot_camera_motion_text_cannot_override_profile_mode(tmp_path) -> None:
    store = ProjectStore(ProjectRoot(tmp_path))
    create_project(store, Project(project_id="shot-motion", title="Shot motion"))
    store.save(
        Episode(
            episode_id="episode-motion",
            project_id="shot-motion",
            number=1,
            duration_target_sec=10,
        ),
        expected_version=0,
    )
    shot = Shot(
        shot_id="cctv-with-motion-text",
        episode_id="episode-motion",
        duration_sec=2.0,
        camera_profile="dvr-2005",
        camera_motion="handheld",
    )
    store.create(shot)
    request = compose_render_request(
        store,
        shot_id=shot.shot_id,
        episode_id=shot.episode_id,
        run_id="motion-run",
        mode="master",
    )
    plan = build_scene_plan(request)

    _assert_no_camera_motion(plan, "Shot.camera_motion='handheld'")


def test_handheld_transform_amplitude_is_small_and_has_no_dolly_or_whip_pan() -> None:
    request = _request(camera_mode=_HANDHELD, motion_seed=73)
    plan = build_scene_plan(request)
    observed = _transforms(plan)
    frames = _camera_keyframes(plan)
    base_location = request.camera_profile.camera_position or request.scene.camera_location
    base_target = request.scene.camera_target
    base_direction = _unit(tuple(
        target - position for target, position in zip(base_target, base_location, strict=True)
    ))

    assert len(observed) >= 2, (
        "handheld amplitude check needs at least two worker keyframes; observed "
        f"{observed!r}"
    )
    position_offsets = []
    angular_offsets = []
    axial_offsets = []
    directions = []
    locations = []
    for frame, transform in observed:
        location = transform[:3]
        target = transform[3:]
        position_delta = tuple(
            value - base
            for value, base in zip(location, base_location, strict=True)
        )
        position_offsets.append(math.sqrt(sum(value * value for value in position_delta)))
        axial_offsets.append(abs(sum(
            delta * direction
            for delta, direction in zip(position_delta, base_direction, strict=True)
        )))
        direction = _unit(tuple(
            target_value - location_value
            for target_value, location_value in zip(target, location, strict=True)
        ))
        directions.append((frame, direction))
        locations.append((frame, location))
        angular_offsets.append(_angle_degrees(base_direction, direction))

    # Chosen independent envelope: 5 cm translation, 0.5 degree sway, and at most
    # 1 cm of optical-axis displacement. The contract leaves exact amplitudes open.
    assert max(position_offsets) <= 0.05 + 1e-9, (
        "handheld camera exceeded the 5 cm position envelope; observed maximum offset "
        f"{max(position_offsets):.6f} m across frames {observed!r}"
    )
    assert max(angular_offsets) <= 0.5 + 1e-9, (
        "handheld camera exceeded the 0.5 degree orientation envelope; observed maximum "
        f"{max(angular_offsets):.6f} degrees across frames {observed!r}"
    )
    assert max(axial_offsets) <= 0.01 + 1e-9, (
        "handheld camera translated more than 1 cm along its optical axis (dolly); "
        f"observed maximum {max(axial_offsets):.6f} m across frames {observed!r}"
    )
    for (left_frame, left_direction), (right_frame, right_direction) in itertools.pairwise(
        directions,
    ):
        elapsed_sec = (right_frame - left_frame) / 60.0
        assert elapsed_sec > 0, (
            f"handheld camera keyframes were not in strictly increasing frame order; "
            f"observed {left_frame} then {right_frame}"
        )
        rate = _angle_degrees(left_direction, right_direction) / elapsed_sec
        assert rate <= 1.0 + 1e-9, (
            "handheld camera rotation exceeded the 1 degree/second no-whip-pan bound; "
            f"observed {rate:.6f} degrees/second from frame {left_frame} to {right_frame}"
        )
    for (left_frame, left_location), (right_frame, right_location) in itertools.pairwise(
        locations,
    ):
        elapsed_sec = (right_frame - left_frame) / 60.0
        speed = math.dist(left_location, right_location) / elapsed_sec
        assert speed <= 0.05 + 1e-9, (
            "handheld camera translation exceeded the 5 cm/second held-camera bound; "
            f"observed {speed:.6f} m/second from frame {left_frame} to {right_frame}"
        )
    assert all(set(item) <= {"frame", "location", "target"} for item in frames), (
        "camera keyframes exposed a zoom, focus, or second animation parameter; "
        f"observed {frames!r}"
    )
    camera = plan.get("camera")
    assert isinstance(camera, dict), f"worker camera payload was not a mapping: {camera!r}"
    assert camera.get("field_of_view_mm") == request.camera_profile.field_of_view_mm, (
        "handheld motion changed camera field of view (zoom); observed worker camera "
        f"{camera!r}"
    )
