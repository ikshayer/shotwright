"""Pure, serializable scene descriptions and Blender render planning."""
from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

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


class ObjectSpec(_FrozenModel):
    name: str
    kind: Literal["box", "cylinder"]
    location: Vec3
    dimensions: Vec3
    material: str
    keyframes: tuple[KeyframeSpec, ...] = ()

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
        return self


class LightSpec(_FrozenModel):
    name: str
    location: Vec3
    energy: float = Field(ge=0)
    color: RGB
    size: float = Field(default=1.1, gt=0)

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


class SceneSpec(_FrozenModel):
    materials: tuple[MaterialSpec, ...]
    objects: tuple[ObjectSpec, ...]
    lights: tuple[LightSpec, ...]
    camera_location: Vec3
    camera_target: Vec3

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


def build_scene_plan(request: RenderRequest) -> dict[str, object]:
    """Build a deterministic worker payload; post-only profile fields are excluded."""
    profile = request.camera_profile
    width, height = (640, 480) if request.mode == "preview" else request.master_resolution
    camera = _rebuild_camera_fields(profile)
    if camera["camera_position"] is not None and math.dist(camera["camera_position"], request.scene.camera_target) <= 1e-9:
        raise ValueError("camera_position override must not coincide with scene camera target")
    return {
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
        "frame_end": 1 if request.mode == "preview" else max(1, math.ceil(request.duration_sec * 60)),
        "samples": 1 if request.mode == "preview" else request.samples,
        "gpu_device": request.gpu_device,
        "scene": request.scene.model_dump(mode="json"),
        "camera": camera,
    }


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
