"""CPU contracts for the low-key living-room scene variant."""
from __future__ import annotations

import math
import random

import pytest
from pydantic import ValidationError

from orchestrator.adapters.blender.scene import (
    MaterialSpec,
    RenderRequest,
    SceneSpec,
    american_living_room,
    build_scene_plan,
)
from orchestrator.domain.entities import CameraProfile
from tests.adapters.blender.test_living_room import _bounds, _has_downward_support


def _request(scene: SceneSpec, *, camera: CameraProfile | None = None):
    return build_scene_plan(
        RenderRequest(
            shot_id="low-key-shot",
            shot_version=1,
            run_id="low-key-run",
            degrade_seed=17,
            duration_sec=1,
            camera_profile=camera or CameraProfile(camera_id="cam", profile_name="test"),
            scene=scene,
        )
    )


def _silhouette_parts(scene: SceneSpec):
    silhouette = [obj for obj in scene.objects if "silhouette" in obj.name.lower()]
    return {
        part: [obj for obj in silhouette if part in obj.name.lower()]
        for part in ("head", "torso", "leg", "foot")
    }


def test_bright_remains_default_and_low_key_is_a_named_deterministic_variant() -> None:
    default_a, default_b = american_living_room(), american_living_room(look="bright")
    assert default_a == default_b
    assert american_living_room(look="low_key") == american_living_room(look="low_key")
    assert default_a != american_living_room(look="low_key")
    with pytest.raises(ValueError):
        american_living_room(look="noir")


def test_low_key_has_one_positive_practical_and_zero_world_fill() -> None:
    scene = american_living_room(look="low_key")
    positive = [light for light in scene.lights if light.energy > 0]
    assert len(positive) == 1
    assert scene.world_strength == 0
    assert positive[0].name
    # Scene-specific room data is carried into the retained worker plan.
    assert _request(scene)["scene"]["world_strength"] == 0


def test_low_key_wood_panels_preserve_physical_seam_configuration_in_plan() -> None:
    scene = american_living_room(look="low_key")
    panels = [material for material in scene.materials if material.surface == "wood_panel"]
    assert panels, "low-key walls need physical wood-panel materials"
    assert all(material.panel_width == pytest.approx(0.18) for material in panels)
    assert all(material.seam_width == pytest.approx(0.003) for material in panels)
    payload = _request(scene)["scene"]["materials"]
    serialized = [item for item in payload if item.get("surface") == "wood_panel"]
    assert serialized
    assert all(item["panel_width"] == pytest.approx(0.18) for item in serialized)
    assert all(item["seam_width"] == pytest.approx(0.003) for item in serialized)
    roundtrip = MaterialSpec.model_validate(panels[0].model_dump(mode="json"))
    assert roundtrip == panels[0]


@pytest.mark.parametrize("changes", [
    {"panel_width": 0}, {"panel_width": -0.1}, {"panel_width": math.inf},
    {"seam_width": 0}, {"seam_width": -0.001}, {"seam_width": math.nan},
])
def test_panel_dimensions_reject_nonpositive_or_nonfinite_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MaterialSpec(name="panel", color=(0.2, 0.15, 0.1, 1), surface="wood_panel", **changes)


@pytest.mark.parametrize("strength", [-0.01, math.inf, math.nan])
def test_world_strength_rejects_negative_or_nonfinite_values(strength: float) -> None:
    scene = american_living_room()
    with pytest.raises(ValidationError):
        SceneSpec.model_validate({**scene.model_dump(), "world_strength": strength})


def test_world_strength_is_optional_legacy_sentinel_and_nonnegative_when_set() -> None:
    scene = american_living_room()
    assert scene.world_strength is None
    assert american_living_room(look="low_key").world_strength == 0
    assert SceneSpec.model_validate({**scene.model_dump(), "world_strength": 0}).world_strength == 0


def test_low_key_camera_rebuild_inputs_select_monochrome_neutral_exposure() -> None:
    camera = CameraProfile(
        camera_id="cam-night",
        profile_name="night",
        monochrome_night=True,
        exposure_ev=0,
    )
    plan = _request(american_living_room(look="low_key"), camera=camera)
    assert plan["camera"]["monochrome_night"] is True
    assert plan["camera"]["exposure_ev"] == 0


def test_low_key_silhouette_has_head_torso_legs_and_floor_supported_feet() -> None:
    scene = american_living_room(look="low_key")
    parts = _silhouette_parts(scene)
    assert all(parts.values()), "silhouette needs named head, torso, legs, and feet"
    hall_floor = [obj for obj in scene.objects if "hall floor" in obj.name.lower()]
    assert hall_floor, "silhouette belongs on the hall floor"
    floor = hall_floor[0]
    floor_top = floor.location[2] + floor.dimensions[2] / 2
    for foot in parts["foot"]:
        foot_bottom = foot.location[2] - foot.dimensions[2] / 2
        assert foot_bottom == pytest.approx(floor_top, abs=0.01)


def test_low_key_pendant_point_light_sits_below_the_opaque_shade_and_near_bulb() -> None:
    scene = american_living_room(look="low_key")
    shades = [obj for obj in scene.objects if "shade" in obj.name.lower()]
    bulbs = [obj for obj in scene.objects if "bulb" in obj.name.lower()]
    assert shades and bulbs
    shade_bounds = _bounds(shades[0])
    practicals = [light for light in scene.lights if light.energy > 0]
    assert len(practicals) == 1
    light = practicals[0]
    assert light.kind == "point"
    assert light.location[2] + light.size < shade_bounds[2][0], (
        "point-light center and shadow radius must clear the opaque shade lower lip"
    )
    assert min(math.dist(light.location, bulb.location) for bulb in bulbs) < 0.2


def test_low_key_pendant_cord_and_canopy_are_supported_by_ceiling() -> None:
    scene = american_living_room(look="low_key")
    cords = [obj for obj in scene.objects if "cord" in obj.name.lower()]
    canopies = [obj for obj in scene.objects if "canopy" in obj.name.lower()]
    ceilings = [obj for obj in scene.objects if "ceiling" in obj.name.lower()]
    shades = [obj for obj in scene.objects if "shade" in obj.name.lower()]
    assert cords and canopies and ceilings and shades
    ceiling_bottom = min(_bounds(obj)[2][0] for obj in ceilings)
    canopy_bottom = min(_bounds(obj)[2][0] for obj in canopies)
    assert max(_bounds(obj)[2][1] for obj in canopies) == pytest.approx(
        ceiling_bottom, abs=0.01
    )
    shade_top = max(_bounds(obj)[2][1] for obj in shades)
    for cord in cords:
        cord_bounds = _bounds(cord)
        assert cord_bounds[2][0] <= shade_top + 0.01
        assert cord_bounds[2][1] == pytest.approx(canopy_bottom, abs=0.01)


def test_low_key_ordinary_furniture_keeps_its_bright_ground_support_geometry() -> None:
    bright = american_living_room()
    low_key = american_living_room(look="low_key")

    def ordinary_objects(scene: SceneSpec):
        return {
            obj.name: obj
            for obj in scene.objects
            if any(label in obj.name.lower() for label in ("sofa", "coffee table", "television", "tv "))
            and not any(label in obj.name.lower() for label in ("silhouette", "shade", "cord", "canopy"))
        }

    bright_objects, low_key_objects = ordinary_objects(bright), ordinary_objects(low_key)
    assert bright_objects
    assert bright_objects.keys() <= low_key_objects.keys(), (
        "low-key look must retain ordinary furniture object identities"
    )
    for name, original in bright_objects.items():
        revised = low_key_objects[name]
        assert (revised.kind, revised.location, revised.dimensions, revised.rotation, revised.bevel) == (
            original.kind, original.location, original.dimensions, original.rotation, original.bevel
        ), f"low-key look changed ordinary furniture geometry for {name}"

    bottom_supported_parts = [
        obj for name, obj in low_key_objects.items()
        if any(part in name.lower() for part in ("foot", "leg", "tabletop", "table top", "stand", "base"))
    ]
    assert bottom_supported_parts
    assert all(_has_downward_support(obj, low_key) for obj in bottom_supported_parts), (
        "primary furniture bases, feet, legs, tabletops, and screen stands need bottom support"
    )


def test_low_key_builder_does_not_consume_or_depend_on_global_random_state() -> None:
    random.seed(9881)
    before = random.getstate()
    try:
        first = american_living_room(look="low_key")
        after = random.getstate()
        random.seed(12)
        second = american_living_room(look="low_key")
        assert before == after
        assert first == second
    finally:
        random.setstate(before)
