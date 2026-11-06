"""Independent CPU contracts for the procedural living room.

These checks establish geometry/data invariants, not visual realism. A rendered
frame still needs the creator's visual approval; object counts cannot replace it.
AABB contacts are conservative: curved surfaces and rotated objects still require
render inspection for genuine contact, collision, and visible support.
"""
from __future__ import annotations

import hashlib
import itertools
import math
import random
import re

import pytest
from pydantic import ValidationError

from orchestrator.adapters.blender.scene import (
    LightSpec,
    MaterialSpec,
    ObjectSpec,
    RenderRequest,
    SceneSpec,
    american_living_room,
    build_scene_plan,
)
from orchestrator.domain.entities import CameraProfile
from tests.adapters.blender.test_anomaly_plan_contract import (
    _STATIC_PLAN_SHA256,
    _canonical_plan_bytes,
)
from tests.adapters.blender.test_anomaly_plan_contract import (
    _request as synthetic_request,
)


def _name(obj: ObjectSpec) -> str:
    return re.sub(r"[^a-z0-9]", "", obj.name.lower())


def _bounds(obj: ObjectSpec) -> tuple[tuple[float, float], ...]:
    """World AABB from local full dimensions and XYZ Euler radians."""
    x, y, z = obj.rotation
    cx, cy, cz = math.cos(x), math.cos(y), math.cos(z)
    sx, sy, sz = math.sin(x), math.sin(y), math.sin(z)
    corners = []
    for signs in itertools.product((-1, 1), repeat=3):
        a, b, c = (sign * extent / 2 for sign, extent in zip(signs, obj.dimensions))
        b, c = cx * b - sx * c, sx * b + cx * c
        a, c = cy * a + sy * c, -sy * a + cy * c
        a, b = cz * a - sz * b, sz * a + cz * b
        corners.append(tuple(v + origin for v, origin in zip((a, b, c), obj.location)))
    return tuple((min(p[axis] for p in corners), max(p[axis] for p in corners)) for axis in range(3))


def _floor(scene: SceneSpec) -> ObjectSpec:
    # The living-room surface may be one slab or many planks. Hall flooring is
    # outside the room and can have a different elevation.
    candidates = [obj for obj in scene.objects
                  if "floor" in _name(obj) and "hall" not in _name(obj)]
    assert candidates, "room needs a named living-room floor surface"
    bounds = [_bounds(obj) for obj in candidates]
    tops = [box[2][1] for box in bounds]
    assert max(tops) - min(tops) <= 0.005, "living-room floor must be coplanar"
    aggregate = tuple((min(box[axis][0] for box in bounds),
                       max(box[axis][1] for box in bounds)) for axis in range(3))
    dimensions = tuple(high - low for low, high in aggregate)
    covered_area = sum((box[0][1] - box[0][0]) * (box[1][1] - box[1][0]) for box in bounds)
    assert covered_area >= 0.9 * dimensions[0] * dimensions[1], "floor footprint has large gaps"
    return ObjectSpec(name="Test aggregate living floor", kind="box",
                      location=tuple((low + high) / 2 for low, high in aggregate),
                      dimensions=dimensions, material=candidates[0].material)


def _group(scene: SceneSpec, *labels: str) -> list[ObjectSpec]:
    members = [obj for obj in scene.objects if any(label in _name(obj) for label in labels)]
    assert members, f"missing descriptively named furniture: {labels}"
    return members


def _touch(left: ObjectSpec, right: ObjectSpec, tolerance: float = 0.025) -> bool:
    return all(max(a[0], b[0]) <= min(a[1], b[1]) + tolerance
               for a, b in zip(_bounds(left), _bounds(right)))


_CONTACT_TOLERANCE = 0.005
_NUMERIC_EPSILON = 1e-9


def _horizontal_overlap(left: ObjectSpec, right: ObjectSpec) -> bool:
    return all(max(a[0], b[0]) < min(a[1], b[1]) - _NUMERIC_EPSILON
               for a, b in zip(_bounds(left)[:2], _bounds(right)[:2]))


def _bottom_supported(obj: ObjectSpec, supports: list[ObjectSpec]) -> bool:
    bottom = _bounds(obj)[2][0]
    return any(other is not obj and _horizontal_overlap(obj, other)
               and _bounds(other)[2][0] < bottom - _NUMERIC_EPSILON
               and abs(_bounds(other)[2][1] - bottom) <= _CONTACT_TOLERANCE + _NUMERIC_EPSILON
               for other in supports)


def _has_downward_support(obj: ObjectSpec, scene: SceneSpec) -> bool:
    """Follow lower supporting parts to the floor, allowing inserted joints.

    Each joint must overlap in XY, meet vertically, and extend downward. The
    bounded insertion allowance accepts a foot embedded in a screen/base while
    rejecting an arbitrary side contact or a chain that still floats in space.
    """
    ground = [_floor(scene)] + [part for part in scene.objects if "rug" in _name(part)]
    pending = [obj]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current.name in visited:
            continue
        visited.add(current.name)
        if _bottom_supported(current, ground):
            return True
        low, high = _bounds(current)[2]
        insertion_limit = min(0.1, (high - low) / 2)
        pending.extend(part for part in scene.objects
                       if part.name not in visited and _horizontal_overlap(current, part)
                       and _bounds(part)[2][0] < low - _CONTACT_TOLERANCE
                       and low - _CONTACT_TOLERANCE - _NUMERIC_EPSILON
                       <= _bounds(part)[2][1]
                       <= low + insertion_limit + _NUMERIC_EPSILON)
    return False


def _parallel_wall_candidates(scene: SceneSpec, point: tuple[float, float, float],
                              opening_axis: int) -> list[ObjectSpec]:
    depth_axis = 1 - opening_axis
    return [obj for obj in scene.objects if "wall" in _name(obj)
            and not any(label in _name(obj) for label in ("trim", "base", "crown"))
            and obj.dimensions[2] > 1
            and obj.dimensions[opening_axis] > obj.dimensions[depth_axis]
            and abs(obj.location[depth_axis] - point[depth_axis]) < 0.4]


def _assert_open_at_wall_plane(scene: SceneSpec, point: tuple[float, float, float],
                               opening_axis: int, label: str) -> None:
    walls = _parallel_wall_candidates(scene, point, opening_axis)
    assert walls, f"{label} must sit in a wall, with frame/casing in front"
    depth_axis = 1 - opening_axis
    for wall in walls:
        probe = list(point)
        # Decorative casing sits forward of the structural wall. Probe the
        # actual wall slab so a painted-on opening cannot satisfy the check.
        probe[depth_axis] = wall.location[depth_axis]
        assert not all(low + 0.005 < value < high - 0.005
                       for value, (low, high) in zip(probe, _bounds(wall))), (
                           f"{wall.name} fills the claimed {label} opening")


def _request(scene: SceneSpec) -> RenderRequest:
    return RenderRequest(shot_id="living-room", shot_version=1, run_id="cpu-contract",
                         degrade_seed=37, duration_sec=1,
                         camera_profile=CameraProfile(camera_id="cam", profile_name="test"),
                         scene=scene)


def test_builder_is_repeatable_independent_of_global_random_and_json_roundtrips() -> None:
    state = random.getstate()
    try:
        random.seed(1)
        before = random.getstate()
        first = american_living_room()
        assert random.getstate() == before, "builder must use a private seeded RNG"
        random.seed(999)
        second = american_living_room()
    finally:
        random.setstate(state)
    assert first == second
    restored = SceneSpec.model_validate_json(first.model_dump_json())
    assert restored == first
    assert _canonical_plan_bytes(build_scene_plan(_request(restored))) == _canonical_plan_bytes(
        build_scene_plan(_request(first)))


def test_scene_names_material_references_and_dimensions_are_valid() -> None:
    scene = american_living_room()
    names = [obj.name for obj in (*scene.objects, *scene.lights)]
    assert len(names) == len(set(names))
    materials = {material.name for material in scene.materials}
    assert len(materials) == len(scene.materials)
    assert all(obj.material in materials for obj in scene.objects)
    assert all(math.isfinite(value) and value > 0
               for obj in scene.objects for value in obj.dimensions)
    assert all(math.isfinite(value) for obj in scene.objects
               for value in (*obj.location, *obj.rotation))
    assert not any(obj.keyframes or obj.visibility_keyframes for obj in scene.objects)


def test_room_uses_domestic_metric_extents_and_fixed_corner_camera() -> None:
    scene = american_living_room()
    floor = _floor(scene)
    floor_bounds = _bounds(floor)
    floor_top = floor_bounds[2][1]
    # Broad tolerances allow construction thickness and ordinary layout revisions.
    assert 5.0 <= floor.dimensions[0] <= 5.8
    assert 3.8 <= floor.dimensions[1] <= 4.6
    ceiling = [obj for obj in scene.objects if "ceiling" in _name(obj)
               and obj.dimensions[0] > 4 and obj.dimensions[1] > 3]
    assert ceiling, "room requires a full ceiling"
    ceiling_bottom = min(_bounds(obj)[2][0] for obj in ceiling)
    assert 2.3 <= ceiling_bottom - floor_top <= 2.6
    x, y, z = scene.camera_location
    assert 2.1 <= z - floor_top <= 2.44
    assert z < ceiling_bottom
    for axis, coordinate in enumerate((x, y)):
        low, high = floor_bounds[axis]
        assert low <= coordinate <= high
        assert min(coordinate - low, high - coordinate) <= 1.0, "CCTV mount belongs near a corner"
    assert scene.camera_target[2] < z
    assert not scene.camera_keyframes
    payload = build_scene_plan(_request(scene))["scene"]
    assert not payload.get("camera_keyframes")


@pytest.mark.parametrize("labels", [
    ("sofa", "couch"), ("coffeetable",), ("tvstand", "tvconsole", "mediaconsole"),
    ("sidetable", "endtable"), ("bookcase", "bookshelf"),
])
def test_furniture_groups_have_floor_contact_and_connected_components(labels: tuple[str, ...]) -> None:
    scene = american_living_room()
    members = _group(scene, *labels)
    floor_top = _bounds(_floor(scene))[2][1]
    assert all(_bounds(obj)[2][0] >= floor_top - _CONTACT_TOLERANCE - _NUMERIC_EPSILON
               for obj in members), f"{labels} sinks below floor"
    # Grounding can be on the floor or a rug, but a free 4cm gap is not contact.
    floor_supports = [_floor(scene)] + [obj for obj in scene.objects
                                       if "rug" in _name(obj)]
    reached = {index for index, obj in enumerate(members)
               if _bottom_supported(obj, floor_supports)}
    assert reached, f"{labels} has no floor or rug support"
    # Conservative AABB assembly graph: bottom support for books and functional
    # props is checked separately; this graph does not certify physical collision.
    while True:
        added = {index for index, obj in enumerate(members) if index not in reached
                 and any(_touch(obj, members[other]) for other in reached)}
        if not added:
            break
        reached.update(added)
    assert len(reached) == len(members), f"disconnected {labels} components: " + repr(
        [obj.name for index, obj in enumerate(members) if index not in reached])


def test_rug_is_on_floor_and_clutter_has_support() -> None:
    scene = american_living_room()
    floor_top = _bounds(_floor(scene))[2][1]
    rugs = _group(scene, "rug")
    floor = _floor(scene)
    supported_rugs = [obj for obj in rugs if _bottom_supported(obj, [floor])]
    while True:
        added = [obj for obj in rugs if obj not in supported_rugs
                 and _bottom_supported(obj, supported_rugs)]
        if not added:
            break
        supported_rugs.extend(added)
    assert len(supported_rugs) == len(rugs), "rug assembly has floating components: " + repr(
        [obj.name for obj in rugs if obj not in supported_rugs])
    clutter = [obj for obj in scene.objects
               if any(label in _name(obj) for label in ("clutter", "mug", "remote"))
               or "book" in _name(obj).replace("bookcase", "").replace("bookshelf", "")]
    assert clutter, "room needs functional table or shelf props"
    for obj in clutter:
        bottom = _bounds(obj)[2][0]
        assert bottom > floor_top + 0.15, "functional clutter belongs on a table or shelf"
        if "mug" in _name(obj) and "handle" in _name(obj):
            body_name = _name(obj).replace("handle", "")
            bodies = [body for body in clutter if _name(body) == body_name
                      and "handle" not in _name(body)]
            assert any(_bottom_supported(body, list(scene.objects))
                       and _touch(obj, body, _NUMERIC_EPSILON) for body in bodies), (
                           f"{obj.name} must attach to its bottom-supported mug body")
        else:
            assert _bottom_supported(obj, list(scene.objects)), f"{obj.name} has no bottom support"


def test_architecture_has_window_sill_trim_and_a_real_doorway_gap() -> None:
    scene = american_living_room()
    assert _group(scene, "window")
    assert _group(scene, "sill")
    assert any(any(label in _name(obj) for label in ("trim", "baseboard", "crown"))
               for obj in scene.objects)
    jambs = [obj for obj in scene.objects if "door" in _name(obj)
             and any(label in _name(obj) for label in ("jamb", "casing"))
             and obj.dimensions[2] > 1.5
             and obj.dimensions[2] > 4 * max(obj.dimensions[:2])]
    assert len(jambs) >= 2, "door opening needs two vertical frame jambs or casings"
    left, right = max(itertools.combinations(jambs, 2),
                      key=lambda pair: math.dist(pair[0].location[:2], pair[1].location[:2]))
    opening_axis = max(range(2), key=lambda axis: abs(left.location[axis] - right.location[axis]))
    opening = tuple((a + b) / 2 for a, b in zip(left.location, right.location))
    probe = (opening[0], opening[1], _bounds(_floor(scene))[2][1] + 0.9)
    _assert_open_at_wall_plane(scene, probe, opening_axis, "doorway")
    glazing = [obj for obj in scene.objects if "window" in _name(obj) and "glass" in _name(obj)]
    assert glazing, "window requires glazing inside its opening"
    for window in glazing:
        axis = max(range(2), key=lambda index: window.dimensions[index])
        _assert_open_at_wall_plane(scene, window.location, axis, "window")


def test_television_has_bottom_support_or_a_wall_mount() -> None:
    scene = american_living_room()
    screens = [obj for obj in scene.objects
               if any(label in _name(obj) for label in ("television", "tv"))
               and any(label in _name(obj) for label in ("screen", "display"))]
    assert screens, "room needs a television display"
    walls = [obj for obj in scene.objects if "wall" in _name(obj)]
    mounts = [obj for obj in scene.objects
              if any(label in _name(obj) for label in ("mount", "bracket"))]
    for screen in screens:
        assert _has_downward_support(screen, scene) or any(
            _touch(screen, mount, _CONTACT_TOLERANCE)
            and any(_touch(mount, wall, _CONTACT_TOLERANCE) for wall in walls)
            for mount in mounts), f"{screen.name} has no stand support or wall mount"


def test_material_payload_has_textured_surfaces_and_motivated_warm_cool_lights() -> None:
    scene = american_living_room()
    materials = {material.name: material for material in scene.materials}
    used = {obj.material for obj in scene.objects}
    surfaces = {materials[name].surface for name in used}
    assert {"wood", "fabric", "plaster", "rug"} <= surfaces
    textured = [materials[name] for name in used if materials[name].surface != "plain"]
    assert all(material.variation > 0 for material in textured)
    assert any(material.bump_strength > 0 for material in textured)
    payload = build_scene_plan(_request(scene))["scene"]
    assert any(item.get("surface") == "wood" and item.get("variation", 0) > 0
               for item in payload["materials"])
    warm = [light for light in scene.lights if light.energy > 0 and light.color[0] > light.color[2]]
    cool = [light for light in scene.lights if light.energy > 0 and light.color[2] > light.color[0]]
    assert warm and cool, "practical and window pools need distinct color temperatures"
    emitters = [obj for obj in scene.objects if materials[obj.material].emission_strength > 0]
    assert emitters, "a practical light needs visible emitting geometry"
    assert any(math.dist(light.location, obj.location) <= 0.75
               for light in warm for obj in emitters), "warm practical is detached from its fixture"


@pytest.mark.parametrize("changes", [
    {"variation": -0.01}, {"variation": 1.01}, {"bump_strength": -0.01},
    {"bump_strength": 1.01}, {"metallic": -0.01}, {"metallic": 1.01},
    {"emission_strength": -1}, {"texture_scale": (1, 0, 1)},
    {"texture_scale": (1, -1, 1)}, {"texture_scale": (1, math.inf, 1)},
    {"variation": math.nan}, {"emission_strength": math.inf}, {"surface": "unknown"},
])
def test_material_extensions_reject_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MaterialSpec(name="m", color=(0.5, 0.5, 0.5, 1), **changes)


@pytest.mark.parametrize("changes", [
    {"bevel": -0.01}, {"bevel": math.inf}, {"rotation": (0, math.nan, 0)},
    {"rotation": (math.inf, 0, 0)}, {"kind": "unsupported"},
])
def test_geometry_extensions_reject_invalid_values(changes: dict[str, object]) -> None:
    values = {"name": "o", "kind": "box", "location": (0, 0, 0),
              "dimensions": (1, 1, 1), "material": "m"}
    values.update(changes)
    with pytest.raises(ValidationError):
        ObjectSpec(**values)


@pytest.mark.parametrize("changes", [
    {"kind": "unsupported"}, {"target": (0, math.inf, 0)}, {"target": (math.nan, 0, 0)},
    {"target": (0, 0, 2)},
])
def test_light_extensions_reject_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LightSpec(name="light", location=(0, 0, 2), energy=20, color=(1, 1, 1), **changes)


@pytest.mark.parametrize("kind", ["box", "cylinder", "sphere", "cone", "torus"])
def test_new_geometry_and_material_data_survive_worker_plan(kind: str) -> None:
    material = MaterialSpec(name="m", color=(0.5, 0.4, 0.3, 1), surface="wood",
                            texture_scale=(2, 3, 4), variation=0.2, bump_strength=0.05,
                            emission_strength=0.3, metallic=0.6)
    obj = ObjectSpec(name="o", kind=kind, location=(0, 0, 1), dimensions=(0.6, 0.7, 0.8),
                     material="m", rotation=(0.1, 0.2, math.pi / 2), bevel=0.025)
    light = LightSpec(name="light", kind="point", location=(0, 0, 2), energy=20,
                      color=(1, 0.8, 0.6), target=(0, 0, 0))
    scene = SceneSpec(materials=(material,), objects=(obj,), lights=(light,),
                      camera_location=(-2, -2, 2), camera_target=(0, 0, 0))
    payload = build_scene_plan(_request(scene))["scene"]
    assert payload["objects"][0]["kind"] == kind
    assert payload["objects"][0]["dimensions"] == [0.6, 0.7, 0.8]
    assert payload["objects"][0]["rotation"] == [0.1, 0.2, math.pi / 2]
    assert payload["objects"][0]["bevel"] == 0.025
    for field in ("surface", "texture_scale", "variation", "bump_strength", "emission_strength", "metallic"):
        assert payload["materials"][0][field] == material.model_dump(mode="json")[field]
    assert payload["lights"][0]["kind"] == "point"
    assert payload["lights"][0]["target"] == [0, 0, 0]


def test_synthetic_fixture_keeps_its_existing_canonical_worker_plan() -> None:
    observed = hashlib.sha256(_canonical_plan_bytes(build_scene_plan(synthetic_request()))).hexdigest()
    assert observed == _STATIC_PLAN_SHA256, f"legacy worker bytes changed: {observed}"
