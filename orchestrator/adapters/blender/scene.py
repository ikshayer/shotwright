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
from orchestrator.domain.entities import (
    CAMERA_MOTION_MODEL_SEMVER,
    CameraProfile,
    EnvironmentBinding,
    PBRTextureSet,
)
from orchestrator.domain.seeds import INT32_MAX, derive_seed, jitter

Vec3 = tuple[float, float, float]
RGB = tuple[float, float, float]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class MaterialSpec(_FrozenModel):
    name: str
    color: tuple[float, float, float, float]
    roughness: float = Field(default=0.7, ge=0, le=1)
    surface: Literal["plain", "wood", "fabric", "plaster", "rug", "wood_panel"] = "plain"
    panel_width: float = Field(default=0.18, gt=0)
    seam_width: float = Field(default=0.003, gt=0)
    texture_scale: Vec3 = (1.0, 1.0, 1.0)
    variation: float = Field(default=0.0, ge=0, le=1)
    bump_strength: float = Field(default=0.0, ge=0, le=1)
    emission_strength: float = Field(default=0.0, ge=0)
    metallic: float = Field(default=0.0, ge=0, le=1)
    textures: PBRTextureSet | None = None

    @field_validator("texture_scale")
    @classmethod
    def valid_texture_scale(cls, value: Vec3) -> Vec3:
        if any(not math.isfinite(v) or v <= 0 for v in value):
            raise ValueError("texture_scale must be finite and positive")
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value

    @field_validator("color")
    @classmethod
    def valid_color(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
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
    kind: Literal["box", "cylinder", "sphere", "cone", "torus"]
    asset_id: str | None = None
    fit_mode: Literal["exact", "contain"] = "exact"
    location: Vec3
    dimensions: Vec3
    material: str | None = None
    rotation: Vec3 = (0.0, 0.0, 0.0)
    bevel: float = Field(default=0.0, ge=0)
    keyframes: tuple[KeyframeSpec, ...] = ()
    visibility_keyframes: tuple[VisibilityKeyframeSpec, ...] = ()

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value

    @field_validator("material")
    @classmethod
    def nonempty_material(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("material must not be empty")
        return value

    @field_validator("dimensions")
    @classmethod
    def positive_dimensions(cls, value: Vec3) -> Vec3:
        if any(not math.isfinite(v) or v <= 0 for v in value):
            raise ValueError("dimensions must be finite and strictly positive")
        return value

    @field_validator("asset_id")
    @classmethod
    def nonempty_asset_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("asset_id must not be empty")
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
    kind: Literal["area", "point"] = "area"
    target: Vec3 | None = None

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
        if self.target is not None and math.dist(self.location, self.target) <= 1e-9:
            raise ValueError("light location and target must not coincide")
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
    world_strength: float | None = Field(default=None, ge=0)
    environment: EnvironmentBinding | None = None

    @model_validator(mode="after")
    def validate_scene(self) -> SceneSpec:
        material_names = [m.name for m in self.materials]
        if len(material_names) != len(set(material_names)):
            raise ValueError("material names must be unique")
        known = set(material_names)
        if self.environment is not None and self.environment.asset_id in {obj.asset_id for obj in self.objects if obj.asset_id is not None} | {asset_id for material in self.materials if material.textures is not None for asset_id in material.textures.maps.values()}:
            raise ValueError("environment asset id must not overlap geometry or material map assets")
        for obj in self.objects:
            if obj.material is None and obj.asset_id is not None:
                continue
            if obj.material not in known:
                raise ValueError(
                    f"object {obj.name!r} references unknown material {obj.material!r}"
                )
        names = [o.name for o in self.objects] + [l.name for l in self.lights]
        if len(names) != len(set(names)):
            raise ValueError("object and light names must be globally unique")
        if math.dist(self.camera_location, self.camera_target) <= 1e-9:
            raise ValueError("camera location and target must not coincide")
        camera_frames = [item.frame for item in self.camera_keyframes]
        if len(camera_frames) != len(set(camera_frames)):
            raise ValueError("duplicate camera keyframe frame")
        return self


class AssetBinding(_FrozenModel):
    """Project-local identity needed to bind a scene asset to its immutable bytes."""

    asset_id: str
    file_path: str
    checksum: str

    @field_validator("asset_id", "file_path")
    @classmethod
    def nonempty_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("asset binding identity fields must not be empty")
        return value

    @field_validator("checksum")
    @classmethod
    def valid_checksum(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("asset binding checksum must be 64 lowercase hexadecimal characters")
        return value


class SharedLocation(_FrozenModel):
    """The versioned room and camera rig this shot can reuse within an Episode."""

    episode_id: str = Field(min_length=1)
    location_id: str = Field(min_length=1)
    location_version: int = Field(ge=1)
    camera_id: str = Field(min_length=1)
    object_names: tuple[str, ...]
    light_names: tuple[str, ...]
    material_names: tuple[str, ...]


class RenderRequest(_FrozenModel):
    shot_id: str
    shot_version: int = Field(ge=1)
    run_id: str
    degrade_seed: int = Field(ge=0)
    motion_seed: int = Field(default=0, ge=0)
    motion_model_semver: str = CAMERA_MOTION_MODEL_SEMVER
    duration_sec: float = Field(gt=0)
    master_resolution: tuple[StrictInt, StrictInt] = (1280, 960)
    camera_profile: CameraProfile
    scene: SceneSpec
    shared_location: SharedLocation | None = None
    asset_bindings: dict[str, AssetBinding] = Field(default_factory=dict)
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

    @field_validator("motion_model_semver")
    @classmethod
    def valid_motion_model_semver(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise ValueError("motion_model_semver must be MAJOR.MINOR.PATCH")
        return value

    @field_validator("master_resolution")
    @classmethod
    def valid_master_resolution(cls, value: tuple[int, int]) -> tuple[int, int]:
        width, height = value
        if (
            not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
            or width < 1280
            or height < 960
            or width * 3 != height * 4
        ):
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

# UNMEASURED PLACEHOLDERS: drift bounds and axial damping for a standing handheld operator.
_HANDHELD_MAX_POSITIONAL_DRIFT_M = 0.025
_HANDHELD_AXIAL_DRIFT_SCALE = 0.4
_HANDHELD_MAX_YAW_DEGREES = 0.35
_HANDHELD_MAX_PITCH_DEGREES = 0.25
_HANDHELD_KEYFRAME_INTERVAL_SECONDS = (2.0, 4.0)


def build_scene_plan(request: RenderRequest) -> dict[str, object]:
    """Build one deterministic worker payload; animation is expressed as keyframes."""
    profile = request.camera_profile
    if profile.camera_mode not in ("cctv", "non_cctv"):
        raise ValueError(f"unsupported camera_mode: {profile.camera_mode!r}")
    if request.scene.camera_keyframes:
        if profile.camera_mode == "cctv":
            raise ValueError("CCTV camera profiles cannot receive camera motion keyframes")
        raise ValueError(
            "handheld camera keyframes are profile-derived; caller-supplied camera keyframes are unsupported"
        )
    width, height = (640, 480) if request.mode == "preview" else request.master_resolution
    camera = _rebuild_camera_fields(profile)
    if (
        camera["camera_position"] is not None
        and math.dist(camera["camera_position"], request.scene.camera_target) <= 1e-9
    ):
        raise ValueError("camera_position override must not coincide with scene camera target")
    validate_camera_placement(request)
    frame_end = 1 if request.mode == "preview" else max(1, math.ceil(request.duration_sec * 60))
    scene_data = _scene_payload(request, camera)
    if profile.camera_mode == "non_cctv":
        scene_data["camera_keyframes"] = [
            keyframe.model_dump(mode="json")
            for keyframe in _handheld_camera_keyframes(request, camera, frame_end)
        ]
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

    referenced_assets: set[str] = set()
    for item in scene_data["objects"]:
        asset_id = item.get("asset_id")
        if asset_id is None:
            continue
        if not isinstance(asset_id, str):
            raise TypeError(f"object {item.get('name')!r} has an invalid asset id")
        referenced_assets.add(asset_id)
        binding = request.asset_bindings.get(asset_id)
        if binding is None:
            raise ValueError(
                f"object {item['name']!r} references unregistered asset {asset_id!r}"
            )
        if binding.asset_id != asset_id:
            raise ValueError(
                f"asset binding key {asset_id!r} does not match its record id "
                f"{binding.asset_id!r}"
            )
        item["asset_checksum"] = binding.checksum
        item["asset_file_path"] = binding.file_path
    for material in plan["scene"]["materials"]:
        textures = material.get("textures")
        if textures is None:
            continue
        maps = textures.get("maps")
        if not isinstance(maps, dict):
            raise TypeError(f"material {material.get('name')!r} has invalid texture maps")
        texture_bindings: dict[str, dict[str, str]] = {}
        for role, asset_id in maps.items():
            referenced_assets.add(asset_id)
            binding = request.asset_bindings.get(asset_id)
            if binding is None or binding.asset_id != asset_id:
                raise ValueError(f"material {material.get('name')!r} map {role!r} references unregistered asset {asset_id!r}")
            texture_bindings[role] = {
                "asset_id": asset_id,
                "asset_checksum": binding.checksum,
                "asset_file_path": binding.file_path,
            }
        textures["bindings"] = texture_bindings
    environment = scene_data.get("environment")
    if environment is not None:
        asset_id = environment["asset_id"]
        referenced_assets.add(asset_id)
        binding = request.asset_bindings.get(asset_id)
        if binding is None or binding.asset_id != asset_id:
            raise ValueError(f"environment references unregistered asset {asset_id!r}")
        environment["binding"] = {"asset_id": asset_id, "asset_checksum": binding.checksum,
                                  "asset_file_path": binding.file_path}
    unused_bindings = set(request.asset_bindings) - referenced_assets
    if unused_bindings:
        raise ValueError(
            "render request carries unused asset binding(s): "
            + ", ".join(sorted(unused_bindings))
        )
    return plan


def validate_camera_placement(request: RenderRequest) -> None:
    """Reject a lens embedded in scene geometry or a view with no visible volume.

    Primitive bounds are evaluated in their object-local coordinates. The room
    shell is an inward-facing enclosure, so it is a valid visible surface but
    does not count as solid geometry surrounding the camera.
    """
    camera = _rebuild_camera_fields(request.camera_profile)
    position = camera["camera_position"] or request.scene.camera_location
    poses = [(position, request.scene.camera_target)]
    if request.camera_profile.camera_mode == "non_cctv":
        frame_end = 1 if request.mode == "preview" else max(1, math.ceil(request.duration_sec * 60))
        poses.extend(
            (keyframe.location, keyframe.target)
            for keyframe in _handheld_camera_keyframes(request, camera, frame_end)
        )
    width, height = (640, 480) if request.mode == "preview" else request.master_resolution
    for camera_position, target in poses:
        _validate_camera_pose(
            request.scene,
            camera_position=camera_position,
            camera_target=target,
            lens_mm=float(camera["field_of_view_mm"]),
            image_width=width,
            image_height=height,
        )


_PLACEMENT_EPSILON_M = 1e-6


def _validate_camera_pose(
    scene: SceneSpec,
    *,
    camera_position: Vec3,
    camera_target: Vec3,
    lens_mm: float,
    image_width: int,
    image_height: int,
) -> None:
    if math.dist(camera_position, camera_target) <= _PLACEMENT_EPSILON_M:
        raise ValueError("camera placement has a degenerate view vector")
    basis = _camera_basis(camera_position, camera_target)
    forward = basis[0]
    nearest_name: str | None = None
    nearest_distance: float | None = None
    visible_objects: list[str] = []

    for obj in scene.objects:
        local_position = _world_to_object_local(camera_position, obj)
        if obj.name.casefold() != "roomshell" and _point_inside_object(local_position, obj):
            interval = _ray_object_interval(
                local_position,
                _world_direction_to_object_local(forward, obj),
                obj,
            )
            surface_distance = (
                interval[1] if interval is not None and interval[1] > 0 else 0.0
            )
            raise ValueError(
                f"camera is inside object {obj.name!r}; nearest surface along the view "
                f"vector is {surface_distance:.3f} m away"
            )

        local_direction = _world_direction_to_object_local(forward, obj)
        interval = _ray_object_interval(local_position, local_direction, obj)
        if interval is not None:
            entry, exit_distance = interval
            surface_distance = entry if entry > _PLACEMENT_EPSILON_M else exit_distance
            if surface_distance >= 0 and (
                nearest_distance is None or surface_distance < nearest_distance
            ):
                nearest_name = obj.name
                nearest_distance = surface_distance

        surface_points = _object_world_surface_samples(obj)
        projected = [
            _project_camera_point(
                point,
                camera_position=camera_position,
                basis=basis,
                lens_mm=lens_mm,
                image_width=image_width,
                image_height=image_height,
            )
            for point in surface_points
        ]
        in_frame = [
            point for point in projected
            if point is not None
            and 0 <= point[0] <= image_width
            and 0 <= point[1] <= image_height
        ]
        if len(in_frame) < 3:
            continue
        left = min(point[0] for point in in_frame)
        right = max(point[0] for point in in_frame)
        top = min(point[1] for point in in_frame)
        bottom = max(point[1] for point in in_frame)
        if right - left > 1.0 and bottom - top > 1.0:
            visible_objects.append(obj.name)

    if nearest_distance is not None and nearest_distance <= _PLACEMENT_EPSILON_M:
        raise ValueError(
            f"camera is touching object {nearest_name!r}; nearest surface along the "
            f"view vector is {nearest_distance:.6f} m away"
        )
    if not visible_objects:
        nearest = (
            f"{nearest_name!r} at {nearest_distance:.3f} m"
            if nearest_name is not None and nearest_distance is not None
            else "no scene surface"
        )
        raise ValueError(
            "camera view has no non-degenerate visible object volume; nearest surface "
            f"along the view vector is {nearest}"
        )


def _world_to_object_local(point: Vec3, obj: ObjectSpec) -> Vec3:
    translated = tuple(value - origin for value, origin in zip(point, obj.location))
    return _inverse_rotate_xyz(translated, obj.rotation)


def _world_direction_to_object_local(direction: Vec3, obj: ObjectSpec) -> Vec3:
    return _inverse_rotate_xyz(direction, obj.rotation)


def _inverse_rotate_xyz(point: Vec3, rotation: Vec3) -> Vec3:
    x, y, z = point
    rx, ry, rz = rotation
    cos_z, sin_z = math.cos(rz), math.sin(rz)
    x, y = cos_z * x + sin_z * y, -sin_z * x + cos_z * y
    cos_y, sin_y = math.cos(ry), math.sin(ry)
    x, z = cos_y * x - sin_y * z, sin_y * x + cos_y * z
    cos_x, sin_x = math.cos(rx), math.sin(rx)
    y, z = cos_x * y + sin_x * z, -sin_x * y + cos_x * z
    return (x, y, z)


def _rotate_xyz(point: Vec3, rotation: Vec3) -> Vec3:
    x, y, z = point
    rx, ry, rz = rotation
    cos_x, sin_x = math.cos(rx), math.sin(rx)
    y, z = cos_x * y - sin_x * z, sin_x * y + cos_x * z
    cos_y, sin_y = math.cos(ry), math.sin(ry)
    x, z = cos_y * x + sin_y * z, -sin_y * x + cos_y * z
    cos_z, sin_z = math.cos(rz), math.sin(rz)
    x, y = cos_z * x - sin_z * y, sin_z * x + cos_z * y
    return (x, y, z)


def _point_inside_object(point: Vec3, obj: ObjectSpec) -> bool:
    half_x, half_y, half_z = (dimension / 2 for dimension in obj.dimensions)
    if obj.kind == "box":
        return abs(point[0]) <= half_x and abs(point[1]) <= half_y and abs(point[2]) <= half_z
    if obj.kind == "cylinder":
        radial = (point[0] / half_x) ** 2 + (point[1] / half_y) ** 2
        return radial <= 1 and abs(point[2]) <= half_z
    if obj.kind == "sphere":
        return sum((coordinate / half) ** 2 for coordinate, half in zip(
            point, (half_x, half_y, half_z)
        )) <= 1
    if obj.kind == "cone":
        if abs(point[2]) > half_z:
            return False
        height_fraction = (point[2] + half_z) / (2 * half_z)
        radius_scale = 1.0 - 0.76 * height_fraction
        radial = (point[0] / (half_x * radius_scale)) ** 2
        radial += (point[1] / (half_y * radius_scale)) ** 2
        return radial <= 1
    if obj.kind == "torus":
        radial = math.sqrt((point[0] / half_x) ** 2 + (point[1] / half_y) ** 2)
        tube = ((radial - 0.4) / 0.1) ** 2 + (point[2] / (half_z * 0.1)) ** 2
        return tube <= 1
    return False


def _ray_object_interval(
    origin: Vec3,
    direction: Vec3,
    obj: ObjectSpec,
) -> tuple[float, float] | None:
    """Return ray entry/exit distances through the primitive's local bounds."""
    half = tuple(dimension / 2 for dimension in obj.dimensions)
    if obj.kind == "sphere":
        scaled_origin = tuple(value / radius for value, radius in zip(origin, half))
        scaled_direction = tuple(value / radius for value, radius in zip(direction, half))
        a = _dot(scaled_direction, scaled_direction)
        b = 2 * _dot(scaled_origin, scaled_direction)
        c = _dot(scaled_origin, scaled_origin) - 1
        discriminant = b * b - 4 * a * c
        if discriminant < 0:
            return None
        root = math.sqrt(discriminant)
        return ((-b - root) / (2 * a), (-b + root) / (2 * a))
    if obj.kind == "cylinder":
        candidates: list[float] = []
        a = (direction[0] / half[0]) ** 2 + (direction[1] / half[1]) ** 2
        b = 2 * (
            origin[0] * direction[0] / half[0] ** 2
            + origin[1] * direction[1] / half[1] ** 2
        )
        c = (origin[0] / half[0]) ** 2 + (origin[1] / half[1]) ** 2 - 1
        discriminant = b * b - 4 * a * c
        if a > _PLACEMENT_EPSILON_M and discriminant >= 0:
            root = math.sqrt(discriminant)
            for distance in ((-b - root) / (2 * a), (-b + root) / (2 * a)):
                z = origin[2] + distance * direction[2]
                if abs(z) <= half[2]:
                    candidates.append(distance)
        if abs(direction[2]) > _PLACEMENT_EPSILON_M:
            for cap in (-half[2], half[2]):
                distance = (cap - origin[2]) / direction[2]
                x = origin[0] + distance * direction[0]
                y = origin[1] + distance * direction[1]
                if (x / half[0]) ** 2 + (y / half[1]) ** 2 <= 1:
                    candidates.append(distance)
        positive = [distance for distance in candidates if distance >= 0]
        if positive:
            return (min(positive), max(positive))
        return None
    if obj.kind == "cone":
        height = half[2] * 2
        scale_at_origin = 1.0 - 0.76 * (origin[2] + half[2]) / height
        scale_per_meter = -0.76 * direction[2] / height
        x_origin, x_direction = origin[0] / half[0], direction[0] / half[0]
        y_origin, y_direction = origin[1] / half[1], direction[1] / half[1]
        a = x_direction**2 + y_direction**2 - scale_per_meter**2
        b = 2 * (
            x_origin * x_direction + y_origin * y_direction
            - scale_at_origin * scale_per_meter
        )
        c = x_origin**2 + y_origin**2 - scale_at_origin**2
        candidates = [
            distance
            for distance in _quadratic_roots(a, b, c)
            if abs(origin[2] + distance * direction[2]) <= half[2]
        ]
        if abs(direction[2]) > _PLACEMENT_EPSILON_M:
            for cap, cap_scale in ((-half[2], 1.0), (half[2], 0.24)):
                distance = (cap - origin[2]) / direction[2]
                x = (origin[0] + distance * direction[0]) / half[0]
                y = (origin[1] + distance * direction[1]) / half[1]
                if x * x + y * y <= cap_scale * cap_scale:
                    candidates.append(distance)
        positive = [distance for distance in candidates if distance >= 0]
        if positive:
            return (min(positive), max(positive))
        return None
    if obj.kind == "torus":
        return _ray_torus_interval(origin, direction, half)
    return _ray_box_interval(origin, direction, half)


def _quadratic_roots(a: float, b: float, c: float) -> tuple[float, ...]:
    if abs(a) <= _PLACEMENT_EPSILON_M:
        return (-c / b,) if abs(b) > _PLACEMENT_EPSILON_M else ()
    discriminant = b * b - 4 * a * c
    if discriminant < 0:
        return ()
    root = math.sqrt(discriminant)
    return ((-b - root) / (2 * a), (-b + root) / (2 * a))


def _ray_torus_interval(
    origin: Vec3,
    direction: Vec3,
    half: tuple[float, float, float],
) -> tuple[float, float] | None:
    bounds = _ray_box_interval(origin, direction, half)
    if bounds is None:
        return None
    start, end = max(0.0, bounds[0]), bounds[1]
    if end <= start:
        return None

    def signed_surface_distance(distance: float) -> float:
        x = (origin[0] + distance * direction[0]) / half[0]
        y = (origin[1] + distance * direction[1]) / half[1]
        z = (origin[2] + distance * direction[2]) / half[2]
        radial = math.sqrt(x * x + y * y)
        return (radial - 0.4) ** 2 + z * z - 0.1**2

    step_size = max(min(half) * 0.02, _PLACEMENT_EPSILON_M)
    samples = min(8192, max(64, math.ceil((end - start) / step_size)))
    distances = [start + (end - start) * index / samples for index in range(samples + 1)]
    roots: list[float] = []
    left = distances[0]
    left_value = signed_surface_distance(left)
    for right in distances[1:]:
        right_value = signed_surface_distance(right)
        if left_value == 0:
            roots.append(left)
        elif (left_value < 0) != (right_value < 0):
            low, high = left, right
            low_value = left_value
            for _ in range(40):
                middle = (low + high) / 2
                middle_value = signed_surface_distance(middle)
                if (low_value < 0) == (middle_value < 0):
                    low, low_value = middle, middle_value
                else:
                    high = middle
            roots.append((low + high) / 2)
        left, left_value = right, right_value
    if left_value == 0:
        roots.append(distances[-1])
    if not roots:
        return None
    return min(roots), max(roots)


def _ray_box_interval(
    origin: Vec3,
    direction: Vec3,
    half: tuple[float, float, float],
) -> tuple[float, float] | None:
    entry, exit_distance = -math.inf, math.inf
    for coordinate, component, extent in zip(origin, direction, half):
        if abs(component) <= _PLACEMENT_EPSILON_M:
            if coordinate < -extent or coordinate > extent:
                return None
            continue
        first = (-extent - coordinate) / component
        second = (extent - coordinate) / component
        entry = max(entry, min(first, second))
        exit_distance = min(exit_distance, max(first, second))
        if entry > exit_distance:
            return None
    if exit_distance < 0:
        return None
    return entry, exit_distance


def _object_world_surface_samples(obj: ObjectSpec) -> tuple[Vec3, ...]:
    """Return deterministic points on the primitive surface for view-volume checks."""
    half = tuple(dimension / 2 for dimension in obj.dimensions)
    local_points: list[Vec3] = []
    angular_steps = 48
    if obj.kind == "box":
        grid = tuple(-1 + 2 * index / 8 for index in range(9))
        for fixed_axis in range(3):
            other_axes = tuple(axis for axis in range(3) if axis != fixed_axis)
            for sign in (-1, 1):
                for first in grid:
                    for second in grid:
                        point = [0.0, 0.0, 0.0]
                        point[fixed_axis] = sign * half[fixed_axis]
                        point[other_axes[0]] = first * half[other_axes[0]]
                        point[other_axes[1]] = second * half[other_axes[1]]
                        local_points.append(tuple(point))
    elif obj.kind == "cylinder":
        for index in range(angular_steps):
            angle = 2 * math.pi * index / angular_steps
            x, y = half[0] * math.cos(angle), half[1] * math.sin(angle)
            for level in (-1, -0.5, 0, 0.5, 1):
                local_points.append((x, y, level * half[2]))
            local_points.extend(((x, y, -half[2]), (x, y, half[2])))
        local_points.extend(((0.0, 0.0, -half[2]), (0.0, 0.0, half[2])))
    elif obj.kind == "sphere":
        latitude_steps = 24
        for latitude_index in range(latitude_steps + 1):
            latitude = -math.pi / 2 + math.pi * latitude_index / latitude_steps
            for longitude_index in range(angular_steps):
                longitude = 2 * math.pi * longitude_index / angular_steps
                local_points.append((
                    half[0] * math.cos(latitude) * math.cos(longitude),
                    half[1] * math.cos(latitude) * math.sin(longitude),
                    half[2] * math.sin(latitude),
                ))
    elif obj.kind == "cone":
        for level_index in range(9):
            level = level_index / 8
            z = -half[2] + 2 * half[2] * level
            radius_scale = 1.0 - 0.76 * level
            for angular_index in range(angular_steps):
                angle = 2 * math.pi * angular_index / angular_steps
                local_points.append((
                    half[0] * radius_scale * math.cos(angle),
                    half[1] * radius_scale * math.sin(angle),
                    z,
                ))
        local_points.append((0.0, 0.0, -half[2]))
    else:
        major_steps, tube_steps = 48, 16
        for major_index in range(major_steps):
            major_angle = 2 * math.pi * major_index / major_steps
            for tube_index in range(tube_steps):
                tube_angle = 2 * math.pi * tube_index / tube_steps
                ring = 0.4 + 0.1 * math.cos(tube_angle)
                local_points.append((
                    half[0] * ring * math.cos(major_angle),
                    half[1] * ring * math.sin(major_angle),
                    half[2] * 0.1 * math.sin(tube_angle),
                ))

    world_points = []
    for point in local_points:
        rotated = _rotate_xyz(point, obj.rotation)
        world_points.append(tuple(
            position + offset for position, offset in zip(obj.location, rotated)
        ))
    return tuple(world_points)


def _project_camera_point(
    point: Vec3,
    *,
    camera_position: Vec3,
    basis: tuple[Vec3, Vec3, Vec3],
    lens_mm: float,
    image_width: int,
    image_height: int,
) -> tuple[float, float] | None:
    forward, right, up = basis
    relative = tuple(value - origin for value, origin in zip(point, camera_position))
    depth = _dot(relative, forward)
    if depth <= _PLACEMENT_EPSILON_M:
        return None
    focal_px = lens_mm * image_width / _CAMERA_SENSOR_WIDTH_MM
    x = image_width / 2 + _dot(relative, right) * focal_px / depth
    y = image_height / 2 - _dot(relative, up) * focal_px / depth
    return x, y


def _handheld_camera_keyframes(
    request: RenderRequest,
    camera: dict[str, object],
    frame_end: int,
) -> tuple[CameraKeyframeSpec, ...]:
    """Create seeded, low-amplitude standing sway for the handheld profile."""
    override = camera["camera_position"]
    base_position = tuple(override) if override is not None else request.scene.camera_location
    base_target = request.scene.camera_target
    base_forward, base_right, base_up = _camera_basis(base_position, base_target)
    target_distance = math.dist(base_position, base_target)
    interval_low, interval_high = _HANDHELD_KEYFRAME_INTERVAL_SECONDS

    def seeded(low: float, high: float, key: str) -> float:
        stage = f"handheld_camera_{request.motion_model_semver}:{key}"
        return jitter(request.shot_id, request.motion_seed, stage, low, high)

    keyframes: list[CameraKeyframeSpec] = []
    frame = 1
    keyframe_index = 0
    while True:
        local_offset = (
            seeded(-1.0, 1.0, f"{keyframe_index}:drift_forward"),
            seeded(-1.0, 1.0, f"{keyframe_index}:drift_right"),
            seeded(-1.0, 1.0, f"{keyframe_index}:drift_up"),
        )
        offset_length = _length(local_offset)
        if offset_length > 1.0:
            local_offset = tuple(component / offset_length for component in local_offset)
        local_offset = (
            local_offset[0] * _HANDHELD_AXIAL_DRIFT_SCALE,
            local_offset[1],
            local_offset[2],
        )
        location = tuple(
            base_position[axis]
            + _HANDHELD_MAX_POSITIONAL_DRIFT_M
            * (
                base_forward[axis] * local_offset[0]
                + base_right[axis] * local_offset[1]
                + base_up[axis] * local_offset[2]
            )
            for axis in range(3)
        )
        yaw = math.radians(
            seeded(
                -_HANDHELD_MAX_YAW_DEGREES,
                _HANDHELD_MAX_YAW_DEGREES,
                f"{keyframe_index}:yaw_degrees",
            )
        )
        pitch = math.radians(
            seeded(
                -_HANDHELD_MAX_PITCH_DEGREES,
                _HANDHELD_MAX_PITCH_DEGREES,
                f"{keyframe_index}:pitch_degrees",
            )
        )
        direction = _unit(
            tuple(
                base_forward[axis]
                + math.tan(yaw) * base_right[axis]
                + math.tan(pitch) * base_up[axis]
                for axis in range(3)
            )
        )
        target = tuple(
            location[axis] + target_distance * direction[axis] for axis in range(3)
        )
        keyframes.append(
            CameraKeyframeSpec(frame=frame, location=location, target=target)
        )
        if frame >= frame_end:
            return tuple(keyframes)

        interval = seeded(
            interval_low,
            interval_high,
            f"{keyframe_index}:interval_seconds",
        )
        next_frame = frame + max(1, round(interval * 60))
        if next_frame > frame_end:
            last_keyframe = keyframes[-1]
            keyframes.append(
                CameraKeyframeSpec(
                    frame=frame_end,
                    location=last_keyframe.location,
                    target=last_keyframe.target,
                )
            )
            return tuple(keyframes)
        frame = next_frame
        keyframe_index += 1


def _scene_payload(request: RenderRequest, camera: dict[str, object]) -> dict[str, object]:
    """Serialize the scene while preserving the pre-animation static payload shape."""
    scene = request.scene
    payload = scene.model_dump(mode="json")
    if scene.camera_keyframes:
        frames = payload["camera_keyframes"]
        if not any(item["frame"] == 1 for item in frames):
            frames.insert(
                0,
                {
                    "frame": 1,
                    "location": camera["camera_position"] or list(scene.camera_location),
                    "target": list(scene.camera_target),
                },
            )
    else:
        payload.pop("camera_keyframes", None)
    for spec, item in zip(scene.objects, payload["objects"], strict=True):
        if spec.asset_id is None:
            item.pop("asset_id", None)
        if spec.fit_mode == "exact":
            item.pop("fit_mode", None)
        if spec.material is None:
            item.pop("material", None)
        if not spec.visibility_keyframes:
            item.pop("visibility_keyframes", None)
        for key, default in (("rotation", [0.0, 0.0, 0.0]), ("bevel", 0.0)):
            if item.get(key) == default:
                item.pop(key, None)
    for spec, item in zip(scene.lights, payload["lights"], strict=True):
        if not spec.energy_keyframes:
            item.pop("energy_keyframes", None)
        for key, default in (("kind", "area"), ("target", None)):
            if item.get(key) == default:
                item.pop(key, None)
    for spec, item in zip(scene.materials, payload["materials"], strict=True):
        if spec.textures is None:
            item.pop("textures", None)
        for key, default in {
            "surface": "plain",
            "texture_scale": [1.0, 1.0, 1.0],
            "variation": 0.0,
            "bump_strength": 0.0,
            "emission_strength": 0.0,
            "metallic": 0.0,
            "panel_width": 0.18,
            "seam_width": 0.003,
        }.items():
            if item.get(key) == default and not (item.get("surface") == "wood_panel" and key in {"panel_width", "seam_width"}):
                item.pop(key, None)
    if scene.world_strength is None:
        payload.pop("world_strength", None)
    if scene.environment is None:
        payload.pop("environment", None)
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
                    anomaly.path,
                    event_frame,
                    end_frame,
                    z_offset=anomaly.height_m / 2,
                    label="linear_walk_path",
                )
                positions = _sample_keyframe_positions(
                    figure_keys,
                    event_frame,
                    end_frame,
                    z_offset=anomaly.height_m / 2,
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
            asset_id=anomaly.figure_asset_id,
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
            )
            if frame_end > 1
            else []
        )
        return

    if isinstance(anomaly, DoorOrObjectMovement):
        target = _find_named_item(scene_data["objects"], anomaly.target_object, "object")
        keys = (
            _path_keyframes(
                anomaly.path, event_frame, end_frame, z_offset=0.0, label="door_or_object_movement"
            )
            if frame_end > 1
            else [{"frame": 1, "location": list(anomaly.path[0])}]
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
    asset_id: str | None = None,
) -> None:
    # UNMEASURED PLACEHOLDER: these normalized body proportions are chosen for
    # visual review. The declared dimensions remain the authoritative envelope.
    components = (
        ("FR20AnomalyFigure", "sphere", (0.62, 0.72, 0.46), (0.0, 0.0, 0.15)),
        ("FR20AnomalyFigureHead", "sphere", (0.42, 0.56, 0.14), (0.0, 0.0, 0.43)),
        ("FR20AnomalyFigureLeftArm", "box", (0.20, 0.66, 0.34), (-0.40, 0.0, 0.15)),
        ("FR20AnomalyFigureRightArm", "box", (0.20, 0.66, 0.34), (0.40, 0.0, 0.15)),
        ("FR20AnomalyFigureLeftLeg", "box", (0.26, 0.58, 0.47), (-0.15, 0.0, -0.265)),
        ("FR20AnomalyFigureRightLeg", "box", (0.26, 0.58, 0.47), (0.15, 0.0, -0.265)),
        ("FR20AnomalyFigureLeftFoot", "box", (0.30, 1.0, 0.045), (-0.15, 0.0, -0.4775)),
        ("FR20AnomalyFigureRightFoot", "box", (0.30, 1.0, 0.045), (0.15, 0.0, -0.4775)),
    )
    component_names = (
        {"FR20AnomalyFigure"}
        if asset_id is not None
        else {component[0] for component in components}
    )
    existing_names = {item["name"] for item in (*scene["objects"], *scene["lights"])}
    collisions = sorted(component_names & existing_names)
    if collisions:
        raise ValueError(f"anomaly figure object name {collisions[0]!r} is already in the scene")
    material_name = "FR20AnomalySilhouette"
    if asset_id is None:
        materials = scene["materials"]
        existing_material = next(
            (item for item in materials if item["name"] == material_name), None
        )
        if existing_material is None:
            materials.append(
                {
                    "name": material_name,
                    "color": [0.015, 0.015, 0.015, 1.0],
                    "roughness": 0.92,
                }
            )
        elif existing_material["color"] != [0.015, 0.015, 0.015, 1.0]:
            raise ValueError(
                f"material {material_name!r} conflicts with the anomaly silhouette material"
            )
    base = (position[0], position[1], position[2] + height_m / 2)
    if asset_id is not None:
        scene["objects"].append(
            {
                "name": "FR20AnomalyFigure",
                # The asset path bypasses primitive creation; this required closed-set
                # discriminator remains inert for imported geometry.
                "kind": "sphere",
                "asset_id": asset_id,
                "location": list(base),
                "dimensions": [width_m, depth_m, height_m],
                "keyframes": [dict(key) for key in keyframes],
                "visibility_keyframes": [dict(key) for key in visibility_keyframes],
            }
        )
        return

    # Each constant offset is applied to the same derived path samples. No part
    # has its own timing or transform, so the silhouette moves as one rigid body.
    for part_name, kind, size_fractions, offset_fractions in components:
        dimensions = (
            size_fractions[0] * width_m,
            size_fractions[1] * depth_m,
            size_fractions[2] * height_m,
        )
        offset = (
            offset_fractions[0] * width_m,
            offset_fractions[1] * depth_m,
            offset_fractions[2] * height_m,
        )
        part_keyframes = [
            {
                **key,
                "location": [key["location"][axis] + offset[axis] for axis in range(3)],
            }
            for key in keyframes
        ]
        scene["objects"].append(
            {
                "name": part_name,
                "kind": kind,
                "location": [base[axis] + offset[axis] for axis in range(3)],
                "dimensions": list(dimensions),
                "material": material_name,
                "keyframes": part_keyframes,
                "visibility_keyframes": [dict(key) for key in visibility_keyframes],
            }
        )


def _validate_figure_geometry(
    request: RenderRequest,
    *,
    positions: tuple[Vec3, ...],
    height_m: float,
    width_m: float,
    depth_m: float,
    minimum_distance_m: float,
    maximum_on_frame_height_px: int,
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
            camera_position,
            base,
            width_m=width_m,
            depth_m=depth_m,
            height_m=height_m,
        )
        if distance_m + 1e-9 < minimum_distance_m:
            raise ValueError(
                f"minimum_distance_m violation at {label}: measured {distance_m:.3f} m, "
                f"required at least {minimum_distance_m:.3f} m"
            )
        subject_height_px = _projected_box_height_px(
            base,
            width_m=width_m,
            depth_m=depth_m,
            height_m=height_m,
            camera_position=camera_position,
            basis=basis,
            lens_mm=lens_mm,
            image_width=width,
            image_height=height,
        )
        if subject_height_px > maximum_on_frame_height_px + 1e-9:
            raise ValueError(
                f"maximum_on_frame_height_px violation at {label}: projected height "
                f"{subject_height_px:.2f} px exceeds {maximum_on_frame_height_px} px"
            )


def _camera_basis(
    camera_position: Vec3 | tuple[float, float, float],
    camera_target: Vec3 | tuple[float, float, float],
) -> tuple[Vec3, Vec3, Vec3]:
    forward = _unit(
        tuple(target - origin for origin, target in zip(camera_position, camera_target))
    )
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
                projected.append(
                    _project_y_px(
                        point,
                        camera_position=camera_position,
                        basis=basis,
                        lens_mm=lens_mm,
                        image_width=image_width,
                        image_height=image_height,
                    )
                )
    return max(projected) - min(projected)


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
    nearest = tuple(
        min(max(value, low), high) for value, (low, high) in zip(camera_position, bounds)
    )
    return math.dist(camera_position, nearest)


def _visibility_window(
    start_frame: int, end_frame: int, *, hide_at_end: bool
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    if start_frame > 1:
        result.append({"frame": 1, "visible": False})
    result.append({"frame": start_frame, "visible": True})
    if hide_at_end and end_frame > start_frame:
        result.append({"frame": end_frame, "visible": False})
    return result


def _path_keyframes(
    path: tuple[Vec3, ...],
    start_frame: int,
    end_frame: int,
    *,
    z_offset: float,
    label: str,
) -> list[dict[str, object]]:
    available = end_frame - start_frame + 1
    if available < len(path):
        raise ValueError(f"{label} timing provides {available} frames for {len(path)} path points")
    keys = []
    for index, point in enumerate(path):
        frame = start_frame + round((end_frame - start_frame) * index / (len(path) - 1))
        keys.append(
            {
                "frame": frame,
                "location": [point[0], point[1], point[2] + z_offset],
            }
        )
    if len({item["frame"] for item in keys}) != len(keys):
        raise ValueError(f"{label} timing maps multiple path points to one frame")
    return keys


def _sample_keyframe_positions(
    keys: list[dict[str, object]],
    start_frame: int,
    end_frame: int,
    *,
    z_offset: float,
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
        sampled.append(
            (
                left[0] + (right[0] - left[0]) * ratio,
                left[1] + (right[1] - left[1]) * ratio,
                left[2] + (right[2] - left[2]) * ratio - z_offset,
            )
        )
    return tuple(sampled)


def _flicker_keyframes(
    *,
    base_energy: float,
    fraction: float,
    frequency_hz: float,
    start_frame: int,
    end_frame: int,
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
    target: dict[str, object],
    field: str,
    keys: list[dict[str, object]],
    label: str,
) -> None:
    existing = list(target.get(field, []))
    frames = {item["frame"] for item in existing}
    collision = sorted(frames.intersection(item["frame"] for item in keys))
    if collision:
        raise ValueError(f"{label} has conflicting {field} at frame(s) {collision}")
    target[field] = sorted((*existing, *keys), key=lambda item: item["frame"])


def _find_named_item(
    items: object,
    name: str,
    kind: str,
    *,
    required: bool = True,
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
        # Mode changes the motion model, not the static Blender camera settings.
        # Keep it out of the worker payload so existing locked-off plan hashes stay stable.
        if name == "camera_mode":
            continue
        if (
            field.json_schema_extra
            and field.json_schema_extra.get("invalidation") == Invalidation.REBUILD.value
        ):
            result[name] = getattr(profile, name)
    expected = {
        "field_of_view_mm",
        "barrel_distortion",
        "exposure_ev",
        "practical_light_color_k",
        "practical_light_intensity",
        "monochrome_night",
        "camera_position",
        "camera_mount",
        "depth_of_field",
    }
    unsupported = set(result) - expected
    if unsupported:
        raise ValueError(f"unsupported rebuild-triggering camera fields: {sorted(unsupported)}")
    for name, value in result.items():
        if (
            isinstance(value, (float, int))
            and not isinstance(value, bool)
            and not math.isfinite(value)
        ):
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


def american_living_room(*, look: Literal["bright", "low_key"] = "bright") -> SceneSpec:
    """Deterministic 5.4 x 4.2 m American timber-house living room."""
    if look not in ("bright", "low_key"):
        raise ValueError(f"unsupported living-room look {look!r}")
    low_key = look == "low_key"
    mats = (
        MaterialSpec(
            name="Warm drywall",
            color=(0.70, 0.65, 0.57, 1),
            roughness=0.88,
            surface="plaster",
            variation=0.06,
            bump_strength=0.012,
        ),
        MaterialSpec(
            name="Ceiling paint",
            color=(0.77, 0.74, 0.67, 1),
            roughness=0.9,
            surface="plaster",
            variation=0.03,
            bump_strength=0.008,
        ),
        MaterialSpec(
            name="Oak planks",
            color=(0.30, 0.16, 0.075, 1),
            roughness=0.4,
            surface="wood",
            texture_scale=(0.15, 7.0, 0.4),
            variation=0.13,
            bump_strength=0.025,
        ),
        MaterialSpec(name="Oak plank variation", color=(0.34, 0.19, 0.09, 1), roughness=0.42, surface="wood", texture_scale=(0.15, 7.0, 0.4), variation=0.1, bump_strength=0.02),
        MaterialSpec(
            name="Honey oak",
            color=(0.34, 0.20, 0.10, 1),
            roughness=0.43,
            surface="wood",
            texture_scale=(0.25, 5.0, 0.5),
            variation=0.1,
            bump_strength=0.02,
        ),
        MaterialSpec(
            name="White trim",
            color=(0.83, 0.80, 0.73, 1),
            roughness=0.68,
            surface="plaster",
            variation=0.02,
        ),
        MaterialSpec(
            name="Sofa fabric",
            color=(0.12, 0.12, 0.12, 1) if low_key else (0.31, 0.32, 0.29, 1),
            roughness=0.93,
            surface="fabric",
            texture_scale=(55, 55, 55),
            variation=0.06,
            bump_strength=0.07,
        ),
        MaterialSpec(
            name="Pillow fabric",
            color=(0.47, 0.39, 0.30, 1),
            roughness=0.94,
            surface="fabric",
            texture_scale=(55, 55, 55),
            variation=0.08,
            bump_strength=0.07,
        ),
        MaterialSpec(
            name="Rug weave",
            color=(0.28, 0.18, 0.14, 1),
            roughness=0.97,
            surface="rug",
            texture_scale=(24, 24, 24),
            variation=0.12,
            bump_strength=0.09,
        ),
        MaterialSpec(
            name="Walnut",
            color=(0.095, 0.056, 0.032, 1),
            roughness=0.34,
            surface="wood",
            variation=0.08,
            bump_strength=0.015,
        ),
        MaterialSpec(
            name="Lamp shade",
            color=(0.065, 0.055, 0.045, 1) if low_key else (1.0, 0.92, 0.8, 1),
            roughness=0.86 if low_key else 0.8,
            emission_strength=0.0 if low_key else 0.65,
        ),
        # Low key makes the window itself the second source rather than placing a lamp
        # behind it: the rear wall is solid and the glass is opaque, so an exterior light
        # is simply occluded -- the same trap that rendered spike 09's first iteration
        # black. Reference 134126's window glows behind its blinds, so an emissive pane
        # is also the faithful model.
        MaterialSpec(
            name="Window glass",
            color=(0.46, 0.52, 0.68, 1) if low_key else (0.35, 0.6, 0.82, 1),
            roughness=1.0 if low_key else 0.2,
            emission_strength=0.28 if low_key else 0.0,
        ),
        MaterialSpec(name="TV screen", color=(0.008, 0.009, 0.012, 1), roughness=0.22),
        MaterialSpec(name="Ceramic", color=(0.42, 0.38, 0.32, 1), roughness=0.3),
    )
    if low_key:
        mats += (
            MaterialSpec(name="Lamp bulb", color=(1.0, 0.92, 0.8, 1), roughness=0.25, emission_strength=4.0),
            MaterialSpec(name="Dark wood panel", color=(0.16, 0.14, 0.12, 1), roughness=0.82, surface="wood_panel", panel_width=0.18, seam_width=0.003, texture_scale=(10.0, 10.0, 0.3), variation=0.12, bump_strength=0.025),
            MaterialSpec(name="Silhouette", color=(0.002, 0.002, 0.002, 1), roughness=1.0),
        )
    o = []
    names = {}

    def unique(n):
        names[n] = names.get(n, 0) + 1
        return n if names[n] == 1 else f"{n} {names[n]:02d}"

    def b(n, l, d, m, v=0, r=(0, 0, 0)):
        o.append(
            ObjectSpec(
                name=unique(n),
                kind="box",
                location=l,
                dimensions=d,
                material=m,
                bevel=v,
                rotation=r,
            )
        )

    def c(n, l, d, m):
        o.append(ObjectSpec(name=unique(n), kind="cylinder", location=l, dimensions=d, material=m))

    # Shell leaves a cased rear hall doorway and a framed window opening.
    # Individual staggered oak boards, with small dark seams between runs.
    for row in range(24):
        y = -2.0125 + row * 0.175
        offset = 0.0 if row % 2 == 0 else 0.9
        for segment in range(-1, 4):
            start = -2.7 + offset + segment * 1.8
            end = start + 1.795
            left, right = max(-2.7, start), min(2.7, end)
            if right > left:
                plank_material = "Oak plank variation" if (row + segment) % 4 == 0 else "Oak planks"
                b("Floor plank", ((left + right) / 2, y, -0.024), (right - left, 0.1735, 0.048), plank_material, 0.001)
    # Rear wall partitions leave exactly x=-1.62..0.08 for the window and
    # x=0.20..1.12 for the hall opening.
    b("Rear wall left", (-2.16, 2.07, 1.22), (1.08, 0.16, 2.44), "Warm drywall")
    b("Rear wall between openings", (0.14, 2.07, 1.22), (0.12, 0.16, 2.44), "Warm drywall")
    b("Rear wall right", (1.94, 2.07, 1.22), (1.52, 0.16, 2.44), "Warm drywall")
    b("Wall below window", (-0.77, 2.07, 0.44), (1.70, 0.16, 0.88), "Warm drywall")
    b("Wall above window", (-0.77, 2.07, 2.30), (1.70, 0.16, 0.28), "Warm drywall")
    b("Hallway header", (0.66, 2.07, 2.18), (1.04, 0.16, 0.52), "Warm drywall")
    b("Left wall", (-2.67, 0, 1.22), (0.16, 4.2, 2.44), "Warm drywall")
    b("Right wall", (2.67, 0, 1.22), (0.16, 4.2, 2.44), "Warm drywall")
    b("Ceiling", (0, 0, 2.43), (5.4, 4.2, 0.10), "Ceiling paint")
    for x in (-2.57, 2.57):
        b("Side baseboard", (x, 0, 0.075), (0.055, 4, 0.15), "White trim", 0.012)
    for x, w in ((-2.16, 1.08), (-0.77, 1.70), (0.14, 0.12), (1.94, 1.52)):
        b("Rear baseboard", (x, 1.975, 0.075), (w, 0.055, 0.15), "White trim", 0.012)
    b("Window glass", (-0.77, 2, 1.48), (1.52, 0.025, 1.12), "Window glass")
    for x in (-1.57, 0.03):
        b("Window casing", (x, 1.91, 1.48), (0.09, 0.13, 1.30), "White trim", 0.015)
    b("Window head casing", (-0.77, 1.91, 2.1), (1.69, 0.13, 0.1), "White trim", 0.015)
    b("Window sill", (-0.77, 1.82, 0.89), (1.78, 0.28, 0.09), "Honey oak", 0.025)
    b("Window muntin", (-0.77, 1.965, 1.48), (0.035, 0.04, 1.12), "White trim")
    b("Window muntin", (-0.77, 1.965, 1.48), (1.52, 0.04, 0.035), "White trim")
    for x in (-1.78, 0.24):
        b("Curtain panel", (x, 1.82, 1.47), (0.34, 0.12, 1.34), "Pillow fabric", 0.06)
        for pleat in range(4):
            b("Curtain fold", (x - 0.12 + pleat * 0.08, 1.745, 1.47), (0.025, 0.035, 1.28), "Pillow fabric", 0.01)
    o.append(ObjectSpec(name=unique("Curtain rod"), kind="cylinder", location=(-0.77, 1.78, 2.17), dimensions=(0.035, 0.035, 2.14), material="Walnut", rotation=(0, 1.5708, 0)))
    for x in (0.17, 1.15):
        b("Hall door casing", (x, 1.91, 1.04), (0.085, 0.13, 2.08), "White trim", 0.012)
    b("Hall casing header", (0.66, 1.91, 2.11), (1.06, 0.13, 0.09), "White trim", 0.012)
    b("Hall floor", (0.66, 2.82, 0.01), (0.92, 1.55, 0.04), "Honey oak")
    b("Hall left return", (0.14, 2.78, 1.22), (0.12, 1.42, 2.44), "Warm drywall")
    b("Hall right return", (1.18, 2.78, 1.22), (0.12, 1.42, 2.44), "Warm drywall")
    b("Hall end wall", (0.66, 3.52, 1.22), (0.92, 0.12, 2.44), "Warm drywall")
    # Sofa, rounded cushions, grounded feet.
    for x in (-1.96, -0.43):
        for y in (0.72, 1.68):
            c("Sofa foot", (x, y, 0.05), (0.09, 0.09, 0.10), "Walnut")
    b("Sofa frame", (-1.19, 1.2, 0.24), (1.95, 1.12, 0.28), "Walnut", 0.08)
    for x in (-1.77, -1.19, -0.61):
        b("Sofa seat cushion", (x, 1.06, 0.43), (0.56, 0.91, 0.18), "Sofa fabric", 0.07)
        b("Sofa back cushion", (x, 1.68, 0.75), (0.56, 0.24, 0.72), "Sofa fabric", 0.09)
    for x in (-1.96, -0.43):
        b("Sofa arm", (x, 1.18, 0.57), (0.32, 1.12, 0.46), "Sofa fabric", 0.10)
    b(
        "Sofa throw pillow",
        (-1.62, 1.36, 0.73),
        (0.49, 0.24, 0.40),
        "Pillow fabric",
        0.12,
        (0.08, -0.1, -0.08),
    )
    b(
        "Sofa throw pillow",
        (-0.91, 1.36, 0.73),
        (0.49, 0.24, 0.40),
        "Pillow fabric",
        0.12,
        (-0.05, 0.09, 0.05),
    )
    b("Living room rug", (0, -0.15, 0.025), (2.65, 1.85, 0.045), "Rug weave", 0.008)
    b("Rug border long", (0, -1.0, 0.050), (2.58, 0.035, 0.005), "Pillow fabric", 0.001)
    b("Rug border long", (0, 0.70, 0.050), (2.58, 0.035, 0.005), "Pillow fabric", 0.001)
    b("Rug border short", (-1.28, -0.15, 0.050), (0.035, 1.70, 0.005), "Pillow fabric", 0.001)
    b("Rug border short", (1.28, -0.15, 0.050), (0.035, 1.70, 0.005), "Pillow fabric", 0.001)
    for x in (-0.60, 0.60):
        for y in (-0.45, 0.15):
            c("Coffee table leg", (x, y, 0.2975), (0.075, 0.075, 0.5), "Walnut")
    b("Coffee table top", (0, -0.15, 0.55), (1.48, 0.92, 0.12), "Honey oak", 0.05)
    b("Coffee table shelf", (0, -0.15, 0.32), (1.18, 0.7, 0.055), "Walnut", 0.02)
    b("Remote control", (-0.42, -0.15, 0.6275), (0.2, 0.075, 0.035), "Walnut", 0.01)
    for index in range(3):
        b("Remote button", (-0.48 + index * 0.045, -0.15, 0.6495), (0.018, 0.035, 0.008), "Ceramic", 0.002)
    c("Coffee mug", (0.35, -0.18, 0.68), (0.12, 0.12, 0.15), "Ceramic")
    o.append(ObjectSpec(name=unique("Coffee mug handle"), kind="torus", location=(0.43, -0.18, 0.68), dimensions=(0.075, 0.06, 0.06), material="Ceramic", rotation=(1.5708, 0, 0)))
    b("TV stand cabinet", (2.34, 0.48, 0.24), (0.48, 1.68, 0.48), "Walnut", 0.07)
    b("TV console top", (2.30, 0.48, 0.49), (0.61, 1.78, 0.075), "Honey oak", 0.025)
    b("Television foot", (2.39, 0.48, 0.72), (0.12, 0.30, 0.40), "Walnut", 0.035)
    b("Television screen", (2.43, 0.48, 1.29), (0.10, 1.42, 0.82), "Walnut", 0.025, (0, 0, -0.07))
    b(
        "Television glass",
        (2.365, 0.48, 1.29),
        (0.018, 1.31, 0.70),
        "TV screen",
        0.008,
        (0, 0, -0.07),
    )
    b("Side table pedestal", (0.10, 1.05, 0.31), (0.42, 0.42, 0.62), "Walnut", 0.04)
    b("Side table top", (0.10, 1.05, 0.66), (0.54, 0.54, 0.08), "Honey oak", 0.03)
    c("Lamp base", (0.10, 1.05, 0.73), (0.22, 0.22, 0.07), "Walnut")
    c("Lamp stem", (0.10, 1.05, 0.94), (0.055, 0.055, 0.36), "Walnut")
    o.append(
        ObjectSpec(
            name=unique("Lamp shade"),
            kind="cone",
            location=(0.10, 1.05, 1.18),
            dimensions=(0.36, 0.36, 0.32),
            material="Lamp shade",
        )
    )
    o.append(ObjectSpec(name=unique("Lamp bulb"), kind="sphere", location=(0.17, 1.05, 0.99), dimensions=(0.055, 0.055, 0.055) if low_key else (0.085, 0.085, 0.085), material="Lamp bulb" if low_key else "Lamp shade"))
    # Open-backed bookcase faces into the room from the rear-right wall.
    for x in (1.35, 2.45):
        b("Bookcase side panel", (x, 1.76, 1.01), (0.055, 0.36, 2.02), "Honey oak", 0.012)
    b("Bookcase back", (1.90, 1.93, 1.01), (1.10, 0.045, 2.02), "Honey oak", 0.008)
    b("Bookcase top", (1.90, 1.76, 2.01), (1.12, 0.36, 0.06), "Honey oak", 0.012)
    b("Bookcase bottom", (1.90, 1.76, 0.03), (1.12, 0.36, 0.06), "Honey oak", 0.012)
    for z in (0.43, 0.91, 1.39, 1.87):
        b("Bookcase shelf", (1.90, 1.76, z), (1.04, 0.32, 0.055), "Walnut", 0.01)
    for n in range(5):
        b("Bookshelf book", (1.52 + n * 0.17, 1.69, 0.6075), (0.12, 0.15, 0.30), "Pillow fabric", 0.012, (0, 0, (n - 2) * 0.035))
    b("Side table book", (-0.07, 0.86, 0.7125), (0.18, 0.14, 0.025), "Pillow fabric", 0.006, (0, 0, -0.08))
    b("Picture frame", (-2.57, -0.15, 1.62), (0.05, 0.82, 0.58), "Honey oak", 0.025)
    b("Framed picture", (-2.535, -0.15, 1.62), (0.012, 0.70, 0.46), "Pillow fabric", 0.008)
    b("Wall outlet plate", (-2.57, 0.72, 0.30), (0.05, 0.12, 0.16), "White trim", 0.01)
    b("Ceiling light canopy", (0, 0.1, 2.35), (0.2, 0.2, 0.035), "Walnut", 0.01)
    c("Ceiling light stem", (0, 0.1, 2.28), (0.035, 0.035, 0.15), "Walnut")
    c("Ceiling light diffuser", (0, 0.1, 2.20), (0.44, 0.44, 0.10), "Lamp shade")
    lights = (
        LightSpec(
            name="Panel0",
            location=(0.22, 1.05, 0.99),
            energy=70,
            color=(1.0, 0.92, 0.8),
            size=0.08,
            kind="point",
        ),
        LightSpec(
            name="Panel1", location=(0, 0.1, 2.11), energy=75, color=(1.0, 0.92, 0.8), size=1.1
        ),
        LightSpec(
            name="Window daylight",
            location=(-0.77, 1.65, 1.65),
            energy=38,
            color=(0.35, 0.6, 1.0),
            size=1.45,
            target=(0, -0.3, 0.9),
        ),
    )
    if low_key:
        # Refinish the room-facing shell as boards. The same material evaluates its
        # board axis from the surface normal, so the rear and side walls both run vertically.
        panel_names = {"Rear wall left", "Rear wall between openings", "Rear wall right", "Wall below window", "Wall above window", "Hallway header", "Left wall", "Right wall", "Hall left return", "Hall right return", "Hall end wall"}
        o = [item.model_copy(update={"material": "Dark wood panel"}) if item.name in panel_names else item for item in o]
        # Hang the existing table lamp over the coffee table, where the bulb and pool
        # are inside the camera's room-facing view. Remove both old fixture assemblies.
        fixture_parts = {"Lamp base", "Lamp stem", "Ceiling light canopy", "Ceiling light stem", "Ceiling light diffuser"}
        adjusted = []
        for item in o:
            if item.name in fixture_parts:
                continue
            if item.name == "Lamp shade":
                item = item.model_copy(update={"location": (-0.55, 0.6, 1.85), "dimensions": (0.38, 0.38, 0.28)})
            elif item.name == "Lamp bulb":
                item = item.model_copy(update={"location": (-0.55, 0.6, 1.65)})
            adjusted.append(item)
        adjusted.extend((
            ObjectSpec(name="Pendant canopy", kind="cylinder", location=(-0.55, 0.6, 2.37), dimensions=(0.16, 0.16, 0.03), material="Walnut"),
            ObjectSpec(name="Pendant cord", kind="cylinder", location=(-0.55, 0.6, 2.1725), dimensions=(0.018, 0.018, 0.365), material="Walnut"),
            ObjectSpec(name="Pendant bulb socket", kind="cylinder", location=(-0.55, 0.6, 1.735), dimensions=(0.022, 0.022, 0.16), material="Walnut"),
        ))
        o = adjusted
        # A partial hunched figure stands inside the hall opening, just behind its right jamb.
        o.extend((
            ObjectSpec(name="Hall silhouette torso", kind="sphere", location=(0.93, 2.45, 1.22), dimensions=(0.28, 0.20, 0.80), material="Silhouette", rotation=(0, 0, -0.04)),
            ObjectSpec(name="Hall silhouette head", kind="sphere", location=(0.94, 2.45, 1.72), dimensions=(0.22, 0.22, 0.22), material="Silhouette"),
            ObjectSpec(name="Hall silhouette left arm", kind="box", location=(0.78, 2.45, 1.20), dimensions=(0.10, 0.18, 0.62), material="Silhouette", rotation=(0, 0, -0.10)),
            ObjectSpec(name="Hall silhouette right arm", kind="box", location=(1.05, 2.45, 1.20), dimensions=(0.10, 0.18, 0.62), material="Silhouette", rotation=(0, 0, 0.12)),
            ObjectSpec(name="Hall silhouette left leg", kind="box", location=(0.87, 2.45, 0.45), dimensions=(0.11, 0.18, 0.84), material="Silhouette"),
            ObjectSpec(name="Hall silhouette right leg", kind="box", location=(0.98, 2.45, 0.45), dimensions=(0.11, 0.18, 0.84), material="Silhouette"),
            ObjectSpec(name="Hall silhouette left foot", kind="box", location=(0.87, 2.34, 0.06), dimensions=(0.16, 0.32, 0.06), material="Silhouette"),
            ObjectSpec(name="Hall silhouette right foot", kind="box", location=(0.98, 2.34, 0.06), dimensions=(0.16, 0.32, 0.06), material="Silhouette"),
        ))
        # Two motivated sources, not one. Measured against reference 134126: a single
        # practical with world_strength 0 crushed 54.8% of the delivered frame to black
        # where the reference crushes 32.0%, so the room lost its readable midtones. The
        # reference has a second source -- its window is brightly backlit behind the
        # blinds -- and restoring that as a real practical lifts the midtones without the
        # flattening that ambient fill would cause. Clipping was never the shortfall: the
        # delivered frame already clipped 0.256% against the reference's 0.068%.
        lights = (
            LightSpec(name="Panel0", location=(-0.50, 0.6, 1.65), energy=145,
                      color=(1.0, 0.92, 0.8), size=0.025, kind="point"),
            LightSpec(name="Window spill", location=(-0.77, 1.65, 1.65), energy=40,
                      color=(0.60, 0.72, 1.0), size=1.45, target=(0, -0.3, 0.9)),
        )
    return SceneSpec(
        materials=mats,
        objects=tuple(o),
        lights=lights,
        camera_location=(-1.8, -1.85, 1.95) if low_key else (-2.48, -1.85, 2.25),
        camera_target=(0.15, 1.4, 1.0) if low_key else (0.2, 0.5, 0.2),
        world_strength=0.0 if low_key else None,
    )
