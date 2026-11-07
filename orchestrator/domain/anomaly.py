"""Typed, closed vocabulary for one shot's FR-20 anomaly."""
from __future__ import annotations

from itertools import pairwise
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_serializer

Vec3 = tuple[float, float, float]


class _AnomalyModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class AnomalyTiming(_AnomalyModel):
    """Duration after ``Shot.event_sec``; the absolute event time has one owner."""

    duration_sec: float = Field(default=1.0, gt=0)


class _Anomaly(_AnomalyModel):
    timing: AnomalyTiming = Field(default_factory=AnomalyTiming)
    subtlety: StrictInt = Field(default=3, ge=1, le=5)


class _FigureAnomaly(_Anomaly):
    height_m: float = Field(gt=0)
    width_m: float = Field(gt=0)
    depth_m: float = Field(gt=0)
    figure_asset_id: str | None = None
    minimum_distance_m: float = Field(gt=0)
    maximum_on_frame_height_px: StrictInt = Field(gt=0)

    @field_validator("figure_asset_id")
    @classmethod
    def nonempty_figure_asset_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("figure_asset_id must not be empty")
        return value

    @model_serializer(mode="wrap")
    def omit_missing_figure_asset_id(self, handler) -> dict[str, object]:
        payload = handler(self)
        if self.figure_asset_id is None:
            payload.pop("figure_asset_id", None)
        return payload


class StaticSilhouette(_FigureAnomaly):
    kind: Literal["static_silhouette"]
    position: Vec3


class LinearWalkPath(_FigureAnomaly):
    kind: Literal["linear_walk_path"]
    path: tuple[Vec3, ...] = Field(min_length=2)

    @field_validator("path")
    @classmethod
    def path_moves(cls, value: tuple[Vec3, ...]) -> tuple[Vec3, ...]:
        if any(left == right for left, right in pairwise(value)):
            raise ValueError("path must not contain repeated consecutive points")
        return value


class AppearDisappear(_FigureAnomaly):
    kind: Literal["appear_disappear"]
    position: Vec3


class PartialOcclusion(_FigureAnomaly):
    kind: Literal["partial_occlusion"]
    position: Vec3
    occluder: str = Field(min_length=1)

    @field_validator("occluder")
    @classmethod
    def nonempty_occluder(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("occluder must not be empty")
        return value


class LightFlicker(_Anomaly):
    kind: Literal["light_flicker"]
    light_name: str = Field(min_length=1)
    flicker_fraction: float = Field(gt=0, le=1)
    frequency_hz: float = Field(gt=0)

    @field_validator("light_name")
    @classmethod
    def nonempty_light(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("light_name must not be empty")
        return value


class DoorOrObjectMovement(_Anomaly):
    kind: Literal["door_or_object_movement"]
    target_object: str = Field(min_length=1)
    path: tuple[Vec3, ...] = Field(min_length=2)

    @field_validator("target_object")
    @classmethod
    def nonempty_target(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target_object must not be empty")
        return value

    @field_validator("path")
    @classmethod
    def path_moves(cls, value: tuple[Vec3, ...]) -> tuple[Vec3, ...]:
        if any(left == right for left, right in pairwise(value)):
            raise ValueError("path must not contain repeated consecutive points")
        return value


class ObjectAppearingDisappearing(_Anomaly):
    kind: Literal["object_appearing_disappearing"]
    target_object: str = Field(min_length=1)

    @field_validator("target_object")
    @classmethod
    def nonempty_target(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target_object must not be empty")
        return value


AnomalySpec = Annotated[
    StaticSilhouette | LinearWalkPath | AppearDisappear | PartialOcclusion | LightFlicker | DoorOrObjectMovement | ObjectAppearingDisappearing,
    Field(discriminator="kind"),
]


__all__ = [
    "AnomalySpec",
    "AnomalyTiming",
    "AppearDisappear",
    "DoorOrObjectMovement",
    "LightFlicker",
    "LinearWalkPath",
    "ObjectAppearingDisappearing",
    "PartialOcclusion",
    "StaticSilhouette",
]
