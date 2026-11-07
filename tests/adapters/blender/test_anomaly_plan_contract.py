"""Contract regressions for FR-20 and animated Blender worker plans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from orchestrator.adapters.blender.scene import (
    ObjectSpec,
    RenderRequest,
    SceneSpec,
    build_scene_plan,
    synthetic_interior,
)
from orchestrator.domain.anomaly import AnomalySpec, AnomalyTiming
from orchestrator.domain.entities import CameraProfile

_ANOMALY_KINDS = (
    "static_silhouette",
    "linear_walk_path",
    "appear_disappear",
    "partial_occlusion",
    "light_flicker",
    "door_or_object_movement",
    "object_appearing_disappearing",
)

# This digest freezes today's canonical worker payload for the same fixture
# used in tests/adapters/blender/test_scene_contract.py. Canonical JSON is the
# serialization under comparison; the digest avoids a large inline snapshot.
_STATIC_PLAN_SHA256 = "92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a"

_TEST_OCCLUDER = "AnomalyTestOccluder"
_TEST_TARGET = "AnomalyTestTarget"


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


def _spec_adapter() -> TypeAdapter[AnomalySpec]:
    """Validate the real domain-level discriminated union."""
    return TypeAdapter(AnomalySpec)


def _valid_anomaly(kind: str, **changes: object) -> AnomalySpec:
    values: dict[str, object] = {"kind": kind, "timing": AnomalyTiming(), "subtlety": 2}
    if kind in {"static_silhouette", "appear_disappear", "partial_occlusion"}:
        values.update({
            "position": (0.0, 1.0, 0.0),
            "height_m": 1.8,
            "width_m": 0.4,
            "depth_m": 0.3,
            "minimum_distance_m": 1.0,
            "maximum_on_frame_height_px": 960,
        })
        if kind == "partial_occlusion":
            values["occluder"] = _TEST_OCCLUDER
    if kind == "linear_walk_path":
        values.update({
            "path": ((-0.5, 0.8, 0.0), (0.5, 0.8, 0.0)),
            "height_m": 1.8,
            "width_m": 0.4,
            "depth_m": 0.3,
            "minimum_distance_m": 1.0,
            "maximum_on_frame_height_px": 960,
        })
    if kind == "light_flicker":
        values.update({"light_name": "Panel0", "flicker_fraction": 0.25, "frequency_hz": 2.0})
    if kind == "door_or_object_movement":
        values.update({
            "target_object": _TEST_TARGET,
            "path": ((0.0, 0.0, 1.35), (0.5, 0.0, 1.35)),
        })
    if kind == "object_appearing_disappearing":
        values["target_object"] = _TEST_TARGET
    values.update(changes)
    return _spec_adapter().validate_python(values)


def _scene_with_anomaly_targets() -> SceneSpec:
    """Return an owned room fixture with objects for the object-bound anomalies."""
    scene = synthetic_interior()
    targets = (
        ObjectSpec(
            name=_TEST_OCCLUDER,
            kind="box",
            location=(-0.92, -0.1, 0.85),
            dimensions=(0.8, 0.2, 1.4),
            material="Clutter",
        ),
        ObjectSpec(
            name=_TEST_TARGET,
            kind="box",
            location=(0.3, -0.2, 0.45),
            dimensions=(0.3, 0.2, 0.3),
            material="Clutter",
        ),
    )
    return scene.model_copy(update={"objects": (*scene.objects, *targets)})


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


def _anomaly_figure_objects(value: object) -> list[dict[str, object]]:
    """Return worker objects emitted for the FR-20 figure, independent of scene nesting."""
    figures: list[dict[str, object]] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            name = item.get("name")
            if (
                isinstance(name, str)
                and name.startswith("FR20AnomalyFigure")
                and isinstance(item.get("dimensions"), (list, tuple))
                and isinstance(item.get("location"), (list, tuple))
            ):
                figures.append(item)
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return figures


def _triplet(item: object, field: str) -> tuple[float, float, float]:
    value = item.get(field) if isinstance(item, dict) else None
    assert isinstance(value, (list, tuple)) and len(value) == 3, (
        f"figure object had no 3D {field}; observed {item!r}"
    )
    assert all(isinstance(part, (int, float)) for part in value), (
        f"figure object had nonnumeric {field}; observed {value!r}"
    )
    return tuple(float(part) for part in value)  # type: ignore[return-value]


def _visibility_keyframe_series(value: object) -> list[tuple[str, list[tuple[int, bool]]]]:
    """Keep visibility changes grouped by their worker object."""
    series: list[tuple[str, list[tuple[int, bool]]]] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            keys = item.get("visibility_keyframes")
            if isinstance(keys, (list, tuple)):
                samples = [
                    (key["frame"], key["visible"])
                    for key in keys
                    if isinstance(key, dict)
                    and isinstance(key.get("frame"), int)
                    and isinstance(key.get("visible"), bool)
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


def test_two_builds_of_one_linear_walk_request_are_byte_identical() -> None:
    request = _request().model_copy(update={
        "anomaly": _valid_anomaly("linear_walk_path"),
        "event_sec": 0.5,
    })
    first = _canonical_plan_bytes(build_scene_plan(request))
    second = _canonical_plan_bytes(build_scene_plan(request))

    assert first == second, (
        "same linear_walk_path request produced different canonical worker plans; "
        f"observed SHA-256 {hashlib.sha256(first).hexdigest()} and "
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


def test_anomaly_figure_is_a_humanoid_silhouette_inside_its_declared_envelope(
    tmp_path: Path,
) -> None:
    from tests.service.test_anomaly_composition_contract import _compose_plan

    anomaly = _valid_anomaly("static_silhouette")
    plan = _compose_plan(tmp_path, anomaly, event_sec=0.5)
    figures = _anomaly_figure_objects(plan)

    assert len(figures) >= 6, (
        "FR-20 figure was not composed from a torso, head, two arms, and two legs; "
        f"observed figure objects {[item.get('name') for item in figures]!r}"
    )
    kinds = [item.get("kind") for item in figures]
    assert kinds.count("sphere") >= 2 and kinds.count("box") >= 4, (
        "FR-20 humanoid silhouette did not use smooth sphere masses and simple limb boxes; "
        f"observed kinds {kinds!r}"
    )
    part_names = [str(item.get("name", "")).casefold() for item in figures]
    for part in ("torso", "head", "arm", "leg"):
        is_named_part = any(part in name for name in part_names)
        if part == "torso":
            is_named_part = is_named_part or "fr20anomalyfigure" in part_names
        assert is_named_part, (
            f"FR-20 silhouette plan omitted a named {part} part; observed {part_names!r}"
        )
    forbidden_face_parts = ("face", "eye", "mouth", "nose")
    assert not any(token in name for token in forbidden_face_parts for name in part_names), (
        f"FR-20 silhouette plan included face detail; observed {part_names!r}"
    )

    position = anomaly.position
    x_min = position[0] - anomaly.width_m / 2
    x_max = position[0] + anomaly.width_m / 2
    y_min = position[1] - anomaly.depth_m / 2
    y_max = position[1] + anomaly.depth_m / 2
    z_min = position[2]
    z_max = position[2] + anomaly.height_m
    observed_extents: list[tuple[float, float, float, float, float, float]] = []
    for item in figures:
        location = _triplet(item, "location")
        dimensions = _triplet(item, "dimensions")
        part_min = tuple(location[index] - dimensions[index] / 2 for index in range(3))
        part_max = tuple(location[index] + dimensions[index] / 2 for index in range(3))
        observed_extents.append((*part_min, *part_max))
        assert part_min[0] >= x_min - 1e-6 and part_max[0] <= x_max + 1e-6, (
            f"figure part exceeded declared width_m envelope: {item!r}"
        )
        assert part_min[1] >= y_min - 1e-6 and part_max[1] <= y_max + 1e-6, (
            f"figure part exceeded declared depth_m envelope: {item!r}"
        )
        assert part_min[2] >= z_min - 1e-6 and part_max[2] <= z_max + 1e-6, (
            f"figure part exceeded declared height_m envelope: {item!r}"
        )

    figure_z_min = min(extents[2] for extents in observed_extents)
    figure_z_max = max(extents[5] for extents in observed_extents)
    assert figure_z_min == pytest.approx(z_min, abs=1e-6), (
        f"figure no longer rests at the declared position; observed bottom {figure_z_min}"
    )
    assert figure_z_max - figure_z_min == pytest.approx(anomaly.height_m, abs=1e-6), (
        "humanoid figure height did not preserve the declared bounding-box envelope; "
        f"observed {figure_z_max - figure_z_min:.6f} m, expected {anomaly.height_m:.6f} m"
    )


def test_linear_walk_moves_every_silhouette_part_as_one_rigid_body(tmp_path: Path) -> None:
    from tests.service.test_anomaly_composition_contract import _compose_plan

    anomaly = _valid_anomaly("linear_walk_path")
    plan = _compose_plan(tmp_path, anomaly, event_sec=1.0)
    figures = _anomaly_figure_objects(plan)

    assert len(figures) >= 6, (
        "linear walk plan did not contain all humanoid silhouette parts; observed "
        f"{[item.get('name') for item in figures]!r}"
    )
    keyframes_by_name: dict[str, dict[int, tuple[float, float, float]]] = {}
    visibility_by_name: dict[str, object] = {}
    for item in figures:
        name = str(item.get("name"))
        raw_keys = item.get("keyframes")
        assert isinstance(raw_keys, (list, tuple)), (
            f"anomaly figure part {name!r} had no motion keyframes; observed {raw_keys!r}"
        )
        locations: dict[int, tuple[float, float, float]] = {}
        for key in raw_keys:
            assert isinstance(key, dict), f"invalid keyframe on {name!r}: {key!r}"
            frame = key.get("frame")
            assert isinstance(frame, int), f"invalid frame on {name!r}: {key!r}"
            locations[frame] = _triplet(key, "location")
        assert len(locations) >= 2, (
            f"anomaly figure part {name!r} did not receive the full walk path; "
            f"observed {locations!r}"
        )
        keyframes_by_name[name] = locations
        visibility_by_name[name] = item.get("visibility_keyframes")

    reference_name = next(iter(keyframes_by_name))
    reference = keyframes_by_name[reference_name]
    reference_frames = set(reference)
    reference_first_frame = min(reference_frames)
    reference_start = reference[reference_first_frame]
    for name, locations in keyframes_by_name.items():
        assert set(locations) == reference_frames, (
            f"figure parts used different animation frames: {reference_name!r} has "
            f"{sorted(reference_frames)!r}, {name!r} has {sorted(locations)!r}"
        )
        part_start = locations[reference_first_frame]
        for frame in sorted(reference_frames):
            expected_delta = tuple(
                reference[frame][index] - reference_start[index] for index in range(3)
            )
            actual_delta = tuple(
                locations[frame][index] - part_start[index] for index in range(3)
            )
            assert actual_delta == pytest.approx(expected_delta, abs=1e-6), (
                f"figure part {name!r} moved independently at frame {frame}; "
                f"expected shared translation {expected_delta!r}, got {actual_delta!r}"
            )

    assert len({repr(value) for value in visibility_by_name.values()}) == 1, (
        "figure parts do not share one visibility window; observed "
        f"{visibility_by_name!r}"
    )
    reference_last = reference[max(reference_frames)]
    observed_walk = tuple(reference_last[index] - reference_start[index] for index in range(3))
    expected_walk = tuple(anomaly.path[-1][index] - anomaly.path[0][index] for index in range(3))
    assert observed_walk == pytest.approx(expected_walk, abs=1e-6), (
        f"rigid anomaly figure did not traverse the declared path; "
        f"observed {observed_walk!r}, expected {expected_walk!r}"
    )


def test_linear_walk_still_rejects_declared_geometry_constraint_violations(
    tmp_path: Path,
) -> None:
    from tests.service.test_anomaly_composition_contract import _compose_plan

    anomaly = _valid_anomaly("linear_walk_path", minimum_distance_m=100.0)
    with pytest.raises(ValueError, match="minimum_distance_m"):
        _compose_plan(tmp_path, anomaly, event_sec=1.0)


def test_linear_walk_still_rejects_projected_height_over_the_declared_limit(
    tmp_path: Path,
) -> None:
    from tests.service.test_anomaly_composition_contract import _compose_plan

    anomaly = _valid_anomaly("linear_walk_path", maximum_on_frame_height_px=1)
    with pytest.raises(ValueError, match="exceeds 1 px"):
        _compose_plan(tmp_path, anomaly, event_sec=1.0)


@pytest.mark.parametrize("kind", _ANOMALY_KINDS)
def test_each_fr20_primitive_validates_and_reaches_worker_plan(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.service.test_anomaly_composition_contract import _compose_plan

    object_bound_kinds = {
        "partial_occlusion",
        "door_or_object_movement",
        "object_appearing_disappearing",
    }
    if kind in object_bound_kinds:
        # Keep this composer-to-worker test independent of the composer's room choice.
        # These cases need named objects, so supply a scene that belongs to this test.
        from orchestrator.service import compose as compose_service

        monkeypatch.setattr(compose_service, "american_living_room", _scene_with_anomaly_targets)

    anomaly = _valid_anomaly(kind)
    plan = _compose_plan(tmp_path, anomaly, event_sec=1.0)
    payload = _canonical_plan_bytes(plan).decode("utf-8")

    assert f'"kind":"{kind}"' in payload, (
        f"{kind!r} validated but was absent from the composed worker plan; "
        f"observed plan SHA-256 {hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
    )


def test_anomaly_union_rejects_an_eighth_kind() -> None:
    values = {"kind": "teleport"}
    with pytest.raises(ValidationError) as caught:
        _spec_adapter().validate_python(values)
    observed = str(caught.value)

    assert "kind" in observed and "teleport" in observed, (
        "unknown anomaly kind was rejected without identifying the offending kind; "
        f"observed {observed!r}"
    )


@pytest.mark.parametrize(
    ("field", "bad_value", "diagnostic"),
    [
        ("minimum_distance_m", 100.0, "required at least 100.000 m"),
        ("maximum_on_frame_height_px", 1, "exceeds 1 px"),
    ],
)
def test_scene_constraints_reject_and_name_the_offending_value(
    field: str, bad_value: float, diagnostic: str, tmp_path: Path,
) -> None:
    from tests.service.test_anomaly_composition_contract import _compose_plan

    # Exercise actual projected geometry. Positive thresholds are valid model
    # values; scene planning must reject them without silently clamping either.
    anomaly = _valid_anomaly("static_silhouette", **{field: bad_value})
    assert getattr(anomaly, field) == bad_value, (
        f"valid {field} threshold changed before planning; observed {getattr(anomaly, field)!r}"
    )
    with pytest.raises(ValueError) as caught:
        _compose_plan(tmp_path, anomaly, event_sec=0.5)
    observed = str(caught.value)

    assert field in observed, (
        f"scene geometry violation did not name {field}; observed {observed!r}"
    )
    assert diagnostic in observed, (
        f"scene geometry violation did not name offending value {bad_value!r}; "
        f"observed {observed!r}"
    )


def test_event_sec_edits_reveal_timing_without_reauthoring_anomaly(tmp_path: Path) -> None:
    from tests.service.test_anomaly_composition_contract import _compose_plan

    anomaly = _valid_anomaly("appear_disappear")
    early = _compose_plan(tmp_path, anomaly, event_sec=0.5)
    late = _compose_plan(tmp_path, anomaly, event_sec=2.0)
    early_series = _visibility_keyframe_series(early)
    late_series = _visibility_keyframe_series(late)
    early_samples = next((samples for name, samples in early_series if name == "FR20AnomalyFigure"), [])
    late_samples = next((samples for name, samples in late_series if name == "FR20AnomalyFigure"), [])

    assert _canonical_plan_bytes(early) != _canonical_plan_bytes(late), (
        "changing typed Shot.event_sec did not change the worker plan; observed "
        f"the same SHA-256 {hashlib.sha256(_canonical_plan_bytes(early)).hexdigest()}"
    )
    assert early_samples and late_samples and early_samples != late_samples, (
        "event_sec changed serialized plan bytes but not anomaly visibility timing; "
        f"observed early {early_samples!r} and late {late_samples!r}"
    )
    assert early_samples[1][0] == 31 and late_samples[1][0] == 121, (
        "Shot.event_sec did not set the anomaly reveal frame; "
        f"observed early {early_samples!r} and late {late_samples!r}"
    )
    anomaly_values = anomaly.model_dump(mode="json")
    assert "event_sec" not in anomaly_values, (
        "event_sec was duplicated inside the anomaly specification; "
        f"observed anomaly fields {sorted(anomaly_values)}"
    )


def test_typed_anomaly_timing_duration_edits_reveal_window(tmp_path: Path) -> None:
    from tests.service.test_anomaly_composition_contract import _compose_plan

    original = _valid_anomaly("appear_disappear")
    extended = original.model_copy(update={"timing": AnomalyTiming(duration_sec=1.5)})
    assert original.model_dump(mode="json", exclude={"timing"}) == extended.model_dump(
        mode="json", exclude={"timing"},
    ), "editing AnomalyTiming.duration_sec changed fields outside the typed timing parameter"

    short_plan = _compose_plan(tmp_path, original, event_sec=0.5)
    long_plan = _compose_plan(tmp_path, extended, event_sec=0.5)
    short_series = _visibility_keyframe_series(short_plan)
    long_series = _visibility_keyframe_series(long_plan)
    short_samples = next((samples for name, samples in short_series if name == "FR20AnomalyFigure"), [])
    long_samples = next((samples for name, samples in long_series if name == "FR20AnomalyFigure"), [])

    assert short_samples and long_samples and short_samples[0][0] == long_samples[0][0], (
        "changing AnomalyTiming.duration_sec moved the reveal start; "
        f"observed short {short_samples!r} and long {long_samples!r}"
    )
    assert short_samples[-1][0] != long_samples[-1][0], (
        "changing typed AnomalyTiming.duration_sec did not change the reveal end; "
        f"observed short {short_samples!r} and long {long_samples!r}"
    )
