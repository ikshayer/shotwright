"""Pure, serializable scene descriptions and Blender render planning."""
from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from orchestrator.domain.anomaly import (
    AnomalySpec,
    AppearDisappear,
    DoorOrObjectMovement,
    LightFlicker,
    LinearWalkPath,
    ObjectAppearingDisappearing,
    PartialOcclusion,
    StaticSilhouette,
)
from orchestrator.domain.base import Invalidation
from orchestrator.domain.entities import CameraProfile
from orchestrator.domain.seeds import INT32_MAX, derive_seed

Vec3 = tuple[float, float, float]
RGB = tuple[float, float, float]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class MaterialSpec(_FrozenModel):
    name: str
    color: tuple[float, float, float, float]
    roughness: float = Field(default=0.7, ge=0, le=1)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value

    @field_validator("color")
    @classmethod
    def valid_color(cls, value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        if any(not math.isfinite(c) or not 0 <= c <= 1 for c in value):
            raise ValueError("color channels must be finite and in [0, 1]")
        return value


class KeyframeSpec(_FrozenModel):
    frame: int = Field(ge=1)
    location: Vec3


class VisibilityKeyframeSpec(_FrozenModel):
    frame: int = Field(ge=1)
    visible: bool


class ObjectSpec(_FrozenModel):
    name: str
    kind: Literal["box", "cylinder"]
    location: Vec3
    dimensions: Vec3
    material: str
    keyframes: tuple[KeyframeSpec, ...] = ()
    visibility_keyframes: tuple[VisibilityKeyframeSpec, ...] = ()

    @field_validator("name", "material")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value

    @field_validator("dimensions")
    @classmethod
    def positive_dimensions(cls, value: Vec3) -> Vec3:
        if any(not math.isfinite(v) or v <= 0 for v in value):
            raise ValueError("dimensions must be finite and strictly positive")
        return value

    @model_validator(mode="after")
    def unique_keyframes(self) -> ObjectSpec:
        frames = [item.frame for item in self.keyframes]
        if len(frames) != len(set(frames)):
            raise ValueError("duplicate keyframe frame")
        visibility_frames = [item.frame for item in self.visibility_keyframes]
        if len(visibility_frames) != len(set(visibility_frames)):
            raise ValueError("duplicate visibility keyframe frame")
        return self


class LightEnergyKeyframeSpec(_FrozenModel):
    frame: int = Field(ge=1)
    energy: float = Field(ge=0)


class LightSpec(_FrozenModel):
    name: str
    location: Vec3
    energy: float = Field(ge=0)
    color: RGB
    size: float = Field(default=1.1, gt=0)
    energy_keyframes: tuple[LightEnergyKeyframeSpec, ...] = ()

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value

    @field_validator("color")
    @classmethod
    def valid_color(cls, value: RGB) -> RGB:
        if any(not math.isfinite(c) or not 0 <= c <= 1 for c in value):
            raise ValueError("color channels must be finite and in [0, 1]")
        return value

    @model_validator(mode="after")
    def unique_energy_frames(self) -> LightSpec:
        frames = [item.frame for item in self.energy_keyframes]
        if len(frames) != len(set(frames)):
            raise ValueError("duplicate light energy keyframe frame")
        return self


class CameraKeyframeSpec(_FrozenModel):
    frame: int = Field(ge=1)
    location: Vec3
    target: Vec3

    @model_validator(mode="after")
    def camera_looks_somewhere(self) -> CameraKeyframeSpec:
        if math.dist(self.location, self.target) <= 1e-9:
            raise ValueError("camera keyframe location and target must not coincide")
        return self


class SceneSpec(_FrozenModel):
    materials: tuple[MaterialSpec, ...]
    objects: tuple[ObjectSpec, ...]
    lights: tuple[LightSpec, ...]
    camera_location: Vec3
    camera_target: Vec3
    camera_keyframes: tuple[CameraKeyframeSpec, ...] = ()

    @model_validator(mode="after")
    def validate_scene(self) -> SceneSpec:
        material_names = [m.name for m in self.materials]
        if len(material_names) != len(set(material_names)):
            raise ValueError("material names must be unique")
        known = set(material_names)
        for obj in self.objects:
            if obj.material not in known:
                raise ValueError(f"object {obj.name!r} references unknown material {obj.material!r}")
        names = [o.name for o in self.objects] + [l.name for l in self.lights]
        if len(names) != len(set(names)):
            raise ValueError("object and light names must be globally unique")
        if math.dist(self.camera_location, self.camera_target) <= 1e-9:
            raise ValueError("camera location and target must not coincide")
        camera_frames = [item.frame for item in self.camera_keyframes]
        if len(camera_frames) != len(set(camera_frames)):
            raise ValueError("duplicate camera keyframe frame")
        return self


class RenderRequest(_FrozenModel):
    shot_id: str
    shot_version: int = Field(ge=1)
    run_id: str
    degrade_seed: int = Field(ge=0)
    duration_sec: float = Field(gt=0)
    master_resolution: tuple[StrictInt, StrictInt] = (1280, 960)
    camera_profile: CameraProfile
    scene: SceneSpec
    event_sec: float | None = Field(default=None, ge=0)
    anomaly: AnomalySpec | None = None
    anomaly_precomposed: bool = False
    mode: Literal["master", "preview"] = "master"
    engine: Literal["eevee", "cycles"] = "eevee"
    samples: int = Field(default=32, ge=1)
    gpu_device: str = "NVIDIA GeForce RTX 5070 Ti Laptop GPU"

    @field_validator("shot_id", "run_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("IDs must match [A-Za-z0-9_-]+")
        return value

    @field_validator("duration_sec")
    @classmethod
    def finite_duration(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("duration_sec must be finite and positive")
        return value

    @field_validator("gpu_device")
    @classmethod
    def nonempty_gpu(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("gpu_device must not be empty")
        return value

    @field_validator("master_resolution")
    @classmethod
    def valid_master_resolution(cls, value: tuple[int, int]) -> tuple[int, int]:
        width, height = value
        if (not isinstance(width, int) or isinstance(width, bool) or not isinstance(height, int)
                or isinstance(height, bool) or width < 1280 or height < 960 or width * 3 != height * 4):
            raise ValueError("master_resolution must be positive 4:3 and at least 1280x960")
        return value

    @model_validator(mode="after")
    def event_matches_anomaly(self) -> RenderRequest:
        if self.event_sec is not None and (
            not math.isfinite(self.event_sec) or self.event_sec > self.duration_sec
        ):
            raise ValueError("event_sec must be finite and within duration_sec")
        if self.anomaly is not None and self.event_sec is None:
            raise ValueError("event_sec is required when anomaly is specified")
        if self.anomaly_precomposed and self.anomaly is None:
            raise ValueError("anomaly_precomposed requires an anomaly")
        return self


_CAMERA_SENSOR_WIDTH_MM = 36.0
_FACE_HEIGHT_LIMIT_PX = 48.0


def build_scene_plan(request: RenderRequest) -> dict[str, object]:
    """Build one deterministic worker payload; animation is expressed as keyframes."""
    profile = request.camera_profile
    width, height = (640, 480) if request.mode == "preview" else request.master_resolution
    camera = _rebuild_camera_fields(profile)
    if camera["camera_position"] is not None and math.dist(camera["camera_position"], request.scene.camera_target) <= 1e-9:
        raise ValueError("camera_position override must not coincide with scene camera target")
    frame_end = 1 if request.mode == "preview" else max(1, math.ceil(request.duration_sec * 60))
    scene_data = _scene_payload(request, camera)
    plan: dict[str, object] = {
        "schema_version": 1,
        "shot_id": request.shot_id,
        "shot_version": request.shot_version,
        "run_id": request.run_id,
        "seed": derive_seed(request.shot_id, request.degrade_seed, "blender", INT32_MAX),
        "engine": "workbench" if request.mode == "preview" else request.engine,
        "mode": request.mode,
        "width": width,
        "height": height,
        "fps": 60,
        "frame_start": 1,
        "frame_end": frame_end,
        "samples": 1 if request.mode == "preview" else request.samples,
        "gpu_device": request.gpu_device,
        "scene": scene_data,
        "camera": camera,
    }
    if request.anomaly is not None:
        assert request.event_sec is not None
        if not request.anomaly_precomposed:
            _apply_anomaly(
                scene_data,
                request=request,
                camera=camera,
                width=request.master_resolution[0],
                height=request.master_resolution[1],
                frame_end=frame_end,
            )
        plan["event_sec"] = request.event_sec
        plan["anomaly"] = request.anomaly.model_dump(mode="json")
    return plan


def _scene_payload(request: RenderRequest, camera: dict[str, object]) -> dict[str, object]:
    """Serialize the scene while preserving the pre-animation static payload shape."""
    scene = request.scene
    payload = scene.model_dump(mode="json")
    if scene.camera_keyframes:
        frames = payload["camera_keyframes"]
        if not any(item["frame"] == 1 for item in frames):
            frames.insert(0, {
                "frame": 1,
                "location": camera["camera_position"] or list(scene.camera_location),
                "target": list(scene.camera_target),
            })
    else:
        payload.pop("camera_keyframes", None)
    for spec, item in zip(scene.objects, payload["objects"], strict=True):
        if not spec.visibility_keyframes:
            item.pop("visibility_keyframes", None)
    for spec, item in zip(scene.lights, payload["lights"], strict=True):
        if not spec.energy_keyframes:
            item.pop("energy_keyframes", None)
    return payload


def _apply_anomaly(
    scene_data: dict[str, object],
    *,
    request: RenderRequest,
    camera: dict[str, object],
    width: int,
    height: int,
    frame_end: int,
) -> None:
    anomaly = request.anomaly
    assert anomaly is not None and request.event_sec is not None
    event_frame = _frame_at(request.event_sec, frame_end)
    end_frame = _frame_at(request.event_sec + anomaly.timing.duration_sec, frame_end)

    if isinstance(anomaly, (StaticSilhouette, LinearWalkPath, AppearDisappear, PartialOcclusion)):
        if isinstance(anomaly, LinearWalkPath):
            if frame_end > 1:
                figure_keys = _path_keyframes(
                    anomaly.path, event_frame, end_frame, z_offset=anomaly.height_m / 2,
                    label="linear_walk_path",
                )
                positions = _sample_keyframe_positions(
                    figure_keys, event_frame, end_frame, z_offset=anomaly.height_m / 2,
                )
            else:
                figure_keys = []
                positions = (anomaly.path[0],)
        else:
            positions = (anomaly.position,)
            figure_keys = []
        _validate_figure_geometry(
            request,
            positions=positions,
            height_m=anomaly.height_m,
            width_m=anomaly.width_m,
            depth_m=anomaly.depth_m,
            minimum_distance_m=anomaly.minimum_distance_m,
            maximum_on_frame_height_px=anomaly.maximum_on_frame_height_px,
            face_height_m=anomaly.face_height_m,
            camera=camera,
            width=width,
            height=height,
        )
        if isinstance(anomaly, PartialOcclusion):
            _find_named_item(scene_data["objects"], anomaly.occluder, "occluder")
        if isinstance(anomaly, LinearWalkPath):
            visibility = _visibility_window(event_frame, frame_end, hide_at_end=False)
        else:
            visibility = _visibility_window(event_frame, end_frame, hide_at_end=True)
        _append_figure(
            scene_data,
            position=(anomaly.path[0] if isinstance(anomaly, LinearWalkPath) else anomaly.position),
            height_m=anomaly.height_m,
            width_m=anomaly.width_m,
            depth_m=anomaly.depth_m,
            keyframes=figure_keys,
            visibility_keyframes=visibility,
        )
        return

    if isinstance(anomaly, LightFlicker):
        target = _find_named_item(scene_data["lights"], anomaly.light_name, "light")
        base_energy = float(target["energy"])
        if base_energy <= 0:
            raise ValueError(f"light_flicker target {anomaly.light_name!r} has zero energy")
        target["energy_keyframes"] = (
            _flicker_keyframes(
                base_energy=base_energy,
                fraction=anomaly.flicker_fraction,
                frequency_hz=anomaly.frequency_hz,
                start_frame=event_frame,
                end_frame=end_frame,
            ) if frame_end > 1 else []
        )
        return

    if isinstance(anomaly, DoorOrObjectMovement):
        target = _find_named_item(scene_data["objects"], anomaly.target_object, "object")
        keys = (
            _path_keyframes(anomaly.path, event_frame, end_frame, z_offset=0.0,
                            label="door_or_object_movement")
            if frame_end > 1 else [{"frame": 1, "location": list(anomaly.path[0])}]
        )
        _merge_keyframes(target, "keyframes", keys, anomaly.target_object)
        return

    if isinstance(anomaly, ObjectAppearingDisappearing):
        target = _find_named_item(scene_data["objects"], anomaly.target_object, "object")
        keys = _visibility_window(event_frame, end_frame, hide_at_end=True)
        _merge_keyframes(target, "visibility_keyframes", keys, anomaly.target_object)
        return

    raise TypeError(f"unsupported anomaly type {type(anomaly).__name__}")


def _append_figure(
    scene: dict[str, object],
    *,
    position: Vec3,
    height_m: float,
    width_m: float,
    depth_m: float,
    keyframes: list[dict[str, object]],
    visibility_keyframes: list[dict[str, object]],
) -> None:
    name = "FR20AnomalyFigure"
    if any(item["name"] == name for item in (*scene["objects"], *scene["lights"])):
        raise ValueError(f"anomaly figure object name {name!r} is already in the scene")
    material_name = "FR20AnomalySilhouette"
    materials = scene["materials"]
    existing_material = next((item for item in materials if item["name"] == material_name), None)
    if existing_material is None:
        materials.append({
            "name": material_name,
            "color": [0.015, 0.015, 0.015, 1.0],
            "roughness": 0.92,
        })
    elif existing_material["color"] != [0.015, 0.015, 0.015, 1.0]:
        raise ValueError(f"material {material_name!r} conflicts with the anomaly silhouette material")
    base = (position[0], position[1], position[2] + height_m / 2)
    scene["objects"].append({
        "name": name,
        "kind": "box",
        "location": list(base),
        "dimensions": [width_m, depth_m, height_m],
        "material": material_name,
        "keyframes": keyframes,
        "visibility_keyframes": visibility_keyframes,
    })


def _validate_figure_geometry(
    request: RenderRequest,
    *,
    positions: tuple[Vec3, ...],
    height_m: float,
    width_m: float,
    depth_m: float,
    minimum_distance_m: float,
    maximum_on_frame_height_px: int,
    face_height_m: float | None,
    camera: dict[str, object],
    width: int,
    height: int,
) -> None:
    lens_mm = float(camera["field_of_view_mm"])
    camera_position = camera["camera_position"] or request.scene.camera_location
    camera_target = request.scene.camera_target
    basis = _camera_basis(camera_position, camera_target)
    for index, base in enumerate(positions):
        label = f"subject position {index}"
        distance_m = _distance_to_box(
            camera_position, base, width_m=width_m, depth_m=depth_m, height_m=height_m,
        )
        if distance_m + 1e-9 < minimum_distance_m:
            raise ValueError(
                f"minimum_distance_m violation at {label}: measured {distance_m:.3f} m, "
                f"required at least {minimum_distance_m:.3f} m"
            )
        subject_height_px = _projected_box_height_px(
            base, width_m=width_m, depth_m=depth_m, height_m=height_m,
            camera_position=camera_position, basis=basis, lens_mm=lens_mm,
            image_width=width, image_height=height,
        )
        if subject_height_px > maximum_on_frame_height_px + 1e-9:
            raise ValueError(
                f"maximum_on_frame_height_px violation at {label}: projected height "
                f"{subject_height_px:.2f} px exceeds {maximum_on_frame_height_px} px"
            )
        if face_height_m is not None:
            face_height_px = _projected_vertical_span_px(
                (base[0], base[1], base[2] + height_m - face_height_m),
                (base[0], base[1], base[2] + height_m),
                camera_position=camera_position, basis=basis, lens_mm=lens_mm,
                image_width=width, image_height=height,
            )
            if face_height_px > _FACE_HEIGHT_LIMIT_PX + 1e-9:
                raise ValueError(
                    f"face_height_px violation at {label} for face_height_m {face_height_m:.3f} m: "
                    f"projected face height "
                    f"{face_height_px:.2f} px exceeds {_FACE_HEIGHT_LIMIT_PX:.0f} px"
                )


def _camera_basis(
    camera_position: Vec3 | tuple[float, float, float],
    camera_target: Vec3 | tuple[float, float, float],
) -> tuple[Vec3, Vec3, Vec3]:
    forward = _unit(tuple(target - origin for origin, target in zip(camera_position, camera_target)))
    world_up = (0.0, 0.0, 1.0)
    right = _cross(forward, world_up)
    if _length(right) <= 1e-9:
        right = _cross(forward, (0.0, 1.0, 0.0))
    right = _unit(right)
    up = _unit(_cross(right, forward))
    return forward, right, up


def _projected_box_height_px(
    base: Vec3,
    *,
    width_m: float,
    depth_m: float,
    height_m: float,
    camera_position: Vec3 | tuple[float, float, float],
    basis: tuple[Vec3, Vec3, Vec3],
    lens_mm: float,
    image_width: int,
    image_height: int,
) -> float:
    projected = []
    for x in (-width_m / 2, width_m / 2):
        for y in (-depth_m / 2, depth_m / 2):
            for z in (0.0, height_m):
                point = (base[0] + x, base[1] + y, base[2] + z)
                projected.append(_project_y_px(
                    point, camera_position=camera_position, basis=basis,
                    lens_mm=lens_mm, image_width=image_width, image_height=image_height,
                ))
    return max(projected) - min(projected)


def _projected_vertical_span_px(
    bottom: Vec3,
    top: Vec3,
    *,
    camera_position: Vec3 | tuple[float, float, float],
    basis: tuple[Vec3, Vec3, Vec3],
    lens_mm: float,
    image_width: int,
    image_height: int,
) -> float:
    low = _project_y_px(bottom, camera_position=camera_position, basis=basis,
                        lens_mm=lens_mm, image_width=image_width, image_height=image_height)
    high = _project_y_px(top, camera_position=camera_position, basis=basis,
                         lens_mm=lens_mm, image_width=image_width, image_height=image_height)
    return abs(high - low)


def _project_y_px(
    point: Vec3,
    *,
    camera_position: Vec3 | tuple[float, float, float],
    basis: tuple[Vec3, Vec3, Vec3],
    lens_mm: float,
    image_width: int,
    image_height: int,
) -> float:
    forward, _right, up = basis
    relative = tuple(p - c for p, c in zip(point, camera_position))
    depth = _dot(relative, forward)
    if depth <= 1e-9:
        raise ValueError(f"subject geometry is behind the camera (camera depth {depth:.3f} m)")
    focal_px = lens_mm * image_width / _CAMERA_SENSOR_WIDTH_MM
    return image_height / 2 - _dot(relative, up) * focal_px / depth


def _distance_to_box(
    camera_position: Vec3 | tuple[float, float, float],
    base: Vec3,
    *,
    width_m: float,
    depth_m: float,
    height_m: float,
) -> float:
    bounds = (
        (base[0] - width_m / 2, base[0] + width_m / 2),
        (base[1] - depth_m / 2, base[1] + depth_m / 2),
        (base[2], base[2] + height_m),
    )
    nearest = tuple(min(max(value, low), high) for value, (low, high) in zip(camera_position, bounds))
    return math.dist(camera_position, nearest)


def _visibility_window(start_frame: int, end_frame: int, *, hide_at_end: bool) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    if start_frame > 1:
        result.append({"frame": 1, "visible": False})
    result.append({"frame": start_frame, "visible": True})
    if hide_at_end and end_frame > start_frame:
        result.append({"frame": end_frame, "visible": False})
    return result


def _path_keyframes(
    path: tuple[Vec3, ...], start_frame: int, end_frame: int, *, z_offset: float, label: str,
) -> list[dict[str, object]]:
    available = end_frame - start_frame + 1
    if available < len(path):
        raise ValueError(
            f"{label} timing provides {available} frames for {len(path)} path points"
        )
    keys = []
    for index, point in enumerate(path):
        frame = start_frame + round((end_frame - start_frame) * index / (len(path) - 1))
        keys.append({
            "frame": frame,
            "location": [point[0], point[1], point[2] + z_offset],
        })
    if len({item["frame"] for item in keys}) != len(keys):
        raise ValueError(f"{label} timing maps multiple path points to one frame")
    return keys


def _sample_keyframe_positions(
    keys: list[dict[str, object]], start_frame: int, end_frame: int, *, z_offset: float,
) -> tuple[Vec3, ...]:
    locations = [(item["frame"], item["location"]) for item in keys]
    sampled: list[Vec3] = []
    segment = 0
    for frame in range(start_frame, end_frame + 1):
        while segment < len(locations) - 2 and frame > locations[segment + 1][0]:
            segment += 1
        left_frame, left = locations[segment]
        right_frame, right = locations[min(segment + 1, len(locations) - 1)]
        if right_frame == left_frame:
            ratio = 0.0
        else:
            ratio = (frame - left_frame) / (right_frame - left_frame)
        sampled.append((
            left[0] + (right[0] - left[0]) * ratio,
            left[1] + (right[1] - left[1]) * ratio,
            left[2] + (right[2] - left[2]) * ratio - z_offset,
        ))
    return tuple(sampled)


def _flicker_keyframes(
    *, base_energy: float, fraction: float, frequency_hz: float, start_frame: int, end_frame: int,
) -> list[dict[str, object]]:
    frame_span = end_frame - start_frame
    if frame_span < 2:
        raise ValueError("light_flicker timing must span at least two rendered frames")
    intervals = min(frame_span, max(2, round(frame_span / 60 * frequency_hz * 2)))
    if intervals % 2:
        intervals = intervals - 1 if intervals > 2 else 2
    keys = []
    for index in range(intervals + 1):
        frame = start_frame + round(frame_span * index / intervals)
        energy = base_energy * (1 - fraction) if index % 2 else base_energy
        keys.append({"frame": frame, "energy": energy})
    return keys


def _merge_keyframes(
    target: dict[str, object], field: str, keys: list[dict[str, object]], label: str,
) -> None:
    existing = list(target.get(field, []))
    frames = {item["frame"] for item in existing}
    collision = sorted(frames.intersection(item["frame"] for item in keys))
    if collision:
        raise ValueError(f"{label} has conflicting {field} at frame(s) {collision}")
    target[field] = sorted((*existing, *keys), key=lambda item: item["frame"])


def _find_named_item(
    items: object, name: str, kind: str, *, required: bool = True,
) -> dict[str, object] | None:
    for item in items:
        if item["name"] == name:
            return item
    if required:
        raise ValueError(f"anomaly references missing {kind} {name!r}")
    return None


def _frame_at(seconds: float, frame_end: int) -> int:
    return min(frame_end, max(1, math.ceil(seconds * 60 - 1e-9) + 1))


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _cross(left: tuple[float, float, float], right: tuple[float, float, float]) -> Vec3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _length(vector: tuple[float, float, float]) -> float:
    return math.sqrt(_dot(vector, vector))


def _unit(vector: tuple[float, float, float]) -> Vec3:
    magnitude = _length(vector)
    if magnitude <= 1e-12:
        raise ValueError("camera direction must have nonzero length")
    return tuple(component / magnitude for component in vector)


def _rebuild_camera_fields(profile: CameraProfile) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, field in CameraProfile.model_fields.items():
        if field.json_schema_extra and field.json_schema_extra.get("invalidation") == Invalidation.REBUILD.value:
            result[name] = getattr(profile, name)
    expected = {"field_of_view_mm", "barrel_distortion", "exposure_ev", "practical_light_color_k",
                "practical_light_intensity", "monochrome_night", "camera_position", "camera_mount", "depth_of_field"}
    unsupported = set(result) - expected
    if unsupported:
        raise ValueError(f"unsupported rebuild-triggering camera fields: {sorted(unsupported)}")
    for name, value in result.items():
        if isinstance(value, (float, int)) and not isinstance(value, bool) and not math.isfinite(value):
            raise ValueError(f"camera rebuild field {name} must be finite")
    if result["depth_of_field"] is not None and result["depth_of_field"] <= 0:
        raise ValueError("depth_of_field must be positive when specified")
    if not 1000 <= result["practical_light_color_k"] <= 40000:
        raise ValueError("practical_light_color_k must be in the supported [1000, 40000] range")
    if not -1 <= result["barrel_distortion"] <= 1:
        raise ValueError("barrel_distortion must be in the supported [-1, 1] range")
    position = result["camera_position"]
    if position is not None and any(not math.isfinite(v) for v in position):
        raise ValueError("camera_position must contain only finite coordinates")
    return result


def synthetic_interior() -> SceneSpec:
    """Measured spike-02 fixture, with the documented unit-cube half-size correction.

    The spike scaled a size=1 cube by (W/2,D/2,H/2), yielding a room half the
    stated 6x4x2.7m dimensions. Here explicit dimensions are full extents.
    """
    import random

    rng = random.Random(1977)
    mats = (
        MaterialSpec(name="Plaster", color=(0.52, 0.5, 0.47, 1), roughness=0.78),
        MaterialSpec(name="Lino", color=(0.3, 0.29, 0.28, 1), roughness=0.42),
        MaterialSpec(name="Clutter", color=(0.36, 0.33, 0.3, 1), roughness=0.65),
    )
    objs: list[ObjectSpec] = [
        ObjectSpec(name="RoomShell", kind="box", location=(0, 0, 1.35), dimensions=(6, 4, 2.7), material="Plaster"),
        ObjectSpec(name="Floor", kind="box", location=(0, 0, -0.025), dimensions=(6, 4, 0.05), material="Lino"),
    ]
    cam = (-2.75, -1.75, 2.52)
    count = 0
    tries = 0
    while count < 22 and tries < 400:
        tries += 1
        kind_idx = count % 3
        x, y = rng.uniform(-2.5, 2.5), rng.uniform(-1.5, 1.5)
        if math.dist((x, y, 0.5), cam) < 1.6:
            continue
        if kind_idx == 0:
            side = rng.uniform(0.25, 0.7)
            item = ObjectSpec(name=f"Clutter{count:02d}", kind="box", location=(x, y, 0.3),
                              dimensions=(side, side, side), material="Clutter")
        elif kind_idx == 1:
            radius, depth = rng.uniform(0.1, 0.25), rng.uniform(0.4, 1.1)
            item = ObjectSpec(name=f"Clutter{count:02d}", kind="cylinder", location=(x, y, 0.45),
                              dimensions=(radius * 2, radius * 2, depth), material="Clutter")
        else:
            side = rng.uniform(0.4, 1.0)
            item = ObjectSpec(name=f"Clutter{count:02d}", kind="box", location=(x, y, 0.5),
                              dimensions=(side, side, side), material="Clutter")
        objs.append(item)
        count += 1
    if count != 22:
        raise ValueError("could not place all 22 seeded props with camera clearance")
    lights = (
        LightSpec(name="Panel0", location=(-1.6, 0.1, 2.62), energy=120, color=(1, 0.86, 0.68), size=1.1),
        LightSpec(name="Panel1", location=(1.7, 0.1, 2.62), energy=120, color=(0.80, 0.88, 1), size=1.1),
        LightSpec(name="PanelFar", location=(0.2, 1.1, 2.62), energy=110, color=(1, 0.9, 0.75), size=1.1),
    )
    return SceneSpec(materials=mats, objects=tuple(objs), lights=lights,
                     camera_location=cam, camera_target=(0.3, 0.2, 1.15))
