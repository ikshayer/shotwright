"""Contract regressions for FR-20 and animated Blender worker plans.

The contract intentionally leaves the Python API unnamed. These tests assume a
discriminated union named ``AnomalySpec`` in
``orchestrator.adapters.blender.anomaly``, attached to ``Shot.anomaly``. The
``kind`` values and common parameter names below are the test author's concrete
reading of the seven prose primitive names and parameter nouns in the contract.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from orchestrator.adapters.blender.scene import (
    RenderRequest,
    build_scene_plan,
    synthetic_interior,
)
from orchestrator.domain.entities import CameraProfile


_ANOMALY_KINDS = (
    "static_silhouette",
    "linear_walk_path",
    "appear_disappear_between_frames",
    "partial_occlusion",
    "light_flicker",
    "door_or_object_movement",
    "object_appearing_disappearing_between_frames",
)

# This digest freezes today's canonical worker payload for the same fixture
# used in tests/adapters/blender/test_scene_contract.py. Canonical JSON is the
# serialization under comparison; the digest avoids a large inline snapshot.
_STATIC_PLAN_SHA256 = "92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a"


def _request() -> RenderRequest:
    return RenderRequest(
        shot_id="shot-1",
        shot_version=2,
        run_id="run-1",
        degrade_seed=9,
        duration_sec=1.01,
        camera_profile=CameraProfile(camera_id="cam", profile_name="test"),
        scene=synthetic_interior(),
    )


def _canonical_plan_bytes(plan: dict[str, object]) -> bytes:
    return json.dumps(
        plan,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _spec_adapter() -> TypeAdapter[Any]:
    """Load the contract-implied union at test time so missing code is a test failure."""
    try:
        from orchestrator.adapters.blender.anomaly import AnomalySpec
    except ModuleNotFoundError as exc:
        pytest.fail(
            "FR-20 requires the anomaly union module; observed import failure: "
            f"{exc}"
        )
    return TypeAdapter(AnomalySpec)


def _valid_anomaly(kind: str, **changes: object) -> Any:
    # Assumed shared typed fields: path, distance, occluder, subtlety, face
    # height, and on-frame height. event_sec stays on Shot and is deliberately
    # not duplicated in this anomaly object.
    values: dict[str, object] = {
        "kind": kind,
        "path": ((0.0, 0.0, 0.0), (0.5, 0.0, 0.0)),
        "distance_m": 4.0,
        "occluder": "RoomShell",
        "subtlety": 0.25,
        "face_height_px": 0,
        "on_frame_height_px": 240,
    }
    values.update(changes)
    return _spec_adapter().validate_python(values)


def _keyframe_location_samples(value: object) -> list[tuple[int, tuple[float, ...]]]:
    """Read worker keyframes as effective transform samples, ignoring JSON bytes."""
    samples: list[tuple[int, tuple[float, ...]]] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            frame = item.get("frame")
            location = item.get("location")
            if (isinstance(frame, int) and isinstance(location, (list, tuple))
                    and all(isinstance(part, (int, float)) for part in location)):
                samples.append((frame, tuple(float(part) for part in location)))
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return samples


def _keyframe_location_series(value: object) -> list[tuple[str, list[tuple[int, tuple[float, ...]]]]]:
    """Keep keyframes grouped by their worker object so separate statics cannot fake motion."""
    series: list[tuple[str, list[tuple[int, tuple[float, ...]]]]] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            keys = item.get("keyframes")
            if isinstance(keys, (list, tuple)):
                samples = [
                    (key["frame"], tuple(float(part) for part in key["location"]))
                    for key in keys
                    if isinstance(key, dict)
                    and isinstance(key.get("frame"), int)
                    and isinstance(key.get("location"), (list, tuple))
                    and all(isinstance(part, (int, float)) for part in key["location"])
                ]
                if samples:
                    series.append((str(item.get("name", "unnamed")), samples))
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return series


def test_static_scene_plan_remains_byte_identical_to_current_worker_payload() -> None:
    plan = build_scene_plan(_request())
    observed = hashlib.sha256(_canonical_plan_bytes(plan)).hexdigest()

    assert observed == _STATIC_PLAN_SHA256, (
        "static no-anomaly plan changed: observed canonical SHA-256 "
        f"{observed}, expected {_STATIC_PLAN_SHA256}"
    )


def test_two_builds_of_one_render_request_are_byte_identical() -> None:
    request = _request()
    first = _canonical_plan_bytes(build_scene_plan(request))
    second = _canonical_plan_bytes(build_scene_plan(request))

    assert first == second, (
        "same request produced different canonical worker plans; observed "
        f"SHA-256 {hashlib.sha256(first).hexdigest()} and "
        f"{hashlib.sha256(second).hexdigest()}"
    )


def test_linear_walk_plan_contains_distinct_effective_location_samples(tmp_path: Path) -> None:
    """Keyframe locations must differ; keyframe metadata alone is insufficient."""
    from tests.service.test_anomaly_composition_contract import _compose_plan

    plan = _compose_plan(tmp_path, _valid_anomaly("linear_walk_path"), event_sec=1.0)
    samples = _keyframe_location_samples(plan)
    series = _keyframe_location_series(plan)
    varying_series = [
        (name, object_samples)
        for name, object_samples in series
        if len({frame for frame, _location in object_samples}) >= 2
        and len({location for _frame, location in object_samples}) >= 2
    ]

    assert len(samples) >= 2, (
        "linear walk plan had fewer than two worker frame/location samples; "
        f"observed {samples!r}"
    )
    assert varying_series, (
        "linear walk keyframes did not vary one worker object's location across frames; "
        f"observed grouped keyframes {series!r}"
    )


@pytest.mark.parametrize("kind", _ANOMALY_KINDS)
def test_each_fr20_primitive_validates_and_reaches_worker_plan(kind: str, tmp_path: Path) -> None:
    from tests.service.test_anomaly_composition_contract import _compose_plan

    anomaly = _valid_anomaly(kind)
    plan = _compose_plan(tmp_path, anomaly, event_sec=1.0)
    payload = _canonical_plan_bytes(plan).decode("utf-8")

    assert f'"kind":"{kind}"' in payload, (
        f"{kind!r} validated but was absent from the composed worker plan; "
        f"observed plan SHA-256 {hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
    )


def test_anomaly_union_rejects_an_eighth_kind() -> None:
    values = {
        "kind": "teleport",
        "path": ((0.0, 0.0, 0.0), (0.5, 0.0, 0.0)),
        "distance_m": 4.0,
        "occluder": "RoomShell",
        "subtlety": 0.25,
        "face_height_px": 0,
        "on_frame_height_px": 240,
    }
    with pytest.raises(ValidationError) as caught:
        _spec_adapter().validate_python(values)
    observed = str(caught.value)

    assert "kind" in observed and "teleport" in observed, (
        "unknown anomaly kind was rejected without identifying the offending kind; "
        f"observed {observed!r}"
    )


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        # The anomaly contract still requires <=48 px; current PRD §3.2 has
        # replaced that rule with identity destruction on delivered frames.
        ("face_height_px", 49),
        # No numeric policy thresholds are specified. Zero distance is
        # degenerate; 961 px exceeds this fixture's 960 px master frame.
        ("distance_m", 0.0),
        ("on_frame_height_px", 961),
    ],
)
def test_scene_constraints_reject_and_name_the_offending_value(
    field: str, bad_value: int | float,
) -> None:
    # Prove the field and otherwise-valid primitive are accepted first, so an
    # unknown/extra field cannot masquerade as enforcement of its constraint.
    _valid_anomaly("static_silhouette")
    changes = {field: bad_value}
    with pytest.raises(ValidationError) as caught:
        _valid_anomaly("static_silhouette", **changes)
    observed = str(caught.value)

    assert field in observed, (
        f"invalid {field} was rejected without naming the constraint; observed {observed!r}"
    )
    assert str(bad_value) in observed, (
        f"invalid {field} was rejected without naming value {bad_value!r}; "
        f"observed {observed!r}"
    )


def test_event_sec_edits_reveal_timing_without_reauthoring_anomaly(tmp_path: Path) -> None:
    from tests.service.test_anomaly_composition_contract import _compose_plan

    anomaly = _valid_anomaly("appear_disappear_between_frames")
    early = _compose_plan(tmp_path, anomaly, event_sec=0.5)
    late = _compose_plan(tmp_path, anomaly, event_sec=2.0)
    early_samples = _keyframe_location_samples(early)
    late_samples = _keyframe_location_samples(late)
    early_frames = {frame for frame, _location in early_samples}
    late_frames = {frame for frame, _location in late_samples}

    assert _canonical_plan_bytes(early) != _canonical_plan_bytes(late), (
        "changing typed Shot.event_sec did not change the worker plan; observed "
        f"the same SHA-256 {hashlib.sha256(_canonical_plan_bytes(early)).hexdigest()}"
    )
    assert early_frames != late_frames, (
        "event_sec changed serialized plan bytes but not keyframe timing; "
        f"observed early frames {sorted(early_frames)} and late frames {sorted(late_frames)}"
    )
    anomaly_values = anomaly.model_dump(mode="json")
    assert "event_sec" not in anomaly_values, (
        "event_sec was duplicated inside the anomaly specification; "
        f"observed anomaly fields {sorted(anomaly_values)}"
    )
