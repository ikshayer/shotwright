"""Validated public requests and provider-neutral asset records."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Provider = Literal["polyhaven", "sketchfab"]
AssetType = Literal["model", "texture", "hdri"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AssetSearchRequest(_Frozen):
    provider: Provider = "polyhaven"
    q: str = Field(min_length=1, max_length=200)
    asset_type: AssetType = "model"
    limit: int = Field(default=10, ge=1, le=50)
    page: int = Field(default=1, ge=1, le=3)
    categories: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("categories")
    @classmethod
    def safe_categories(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("categories must be unique")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item) for item in value):
            raise ValueError("categories must be safe slugs")
        return value

    @field_validator("q")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("q must not be blank")
        return value.strip()


class AssetCategoryRequest(_Frozen):
    provider: Provider = "polyhaven"
    asset_type: AssetType = "model"

    @model_validator(mode="after")
    def supported_provider_type(self) -> AssetCategoryRequest:
        if self.provider == "sketchfab" and self.asset_type != "model":
            raise ValueError("Sketchfab categories support models only")
        return self


class AssetPreviewRequest(_Frozen):
    provider: Provider
    source_asset_id: str

    @field_validator("source_asset_id")
    @classmethod
    def safe_source_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("source_asset_id has an unsafe form")
        return value

    @model_validator(mode="after")
    def provider_id_shape(self) -> AssetPreviewRequest:
        if self.provider == "sketchfab" and not re.fullmatch(r"[0-9a-fA-F]{32}", self.source_asset_id):
            raise ValueError("Sketchfab source_asset_id must be a 32 character model UID")
        return self


class AssetPreviewResult(_Frozen):
    provider: Provider
    source_asset_id: str
    media_type: Literal["image/jpeg", "image/png", "image/webp"]
    width: int = Field(gt=0, le=2048)
    height: int = Field(gt=0, le=2048)
    data_base64: str
    credit: str
    credit_url: str


class AssetAcquireRequest(_Frozen):
    provider: Provider
    source_asset_id: str
    asset_id: str | None = None
    asset_type: AssetType = "model"
    resolution: Literal["1k", "2k", "4k"] = "1k"
    map: str = "Diffuse"
    format: Literal["auto", "jpg", "png", "hdr", "exr"] = "auto"
    expected_version: int = Field(ge=0)

    @field_validator("source_asset_id")
    @classmethod
    def safe_source_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("source_asset_id has an unsafe form")
        return value

    @field_validator("asset_id")
    @classmethod
    def safe_asset_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise ValueError("asset_id has an unsafe form")
        return value

    @field_validator("map")
    @classmethod
    def safe_map(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise ValueError("map must be a safe identifier")
        return value

    @model_validator(mode="after")
    def provider_id_shape(self) -> AssetAcquireRequest:
        if self.provider == "sketchfab" and not re.fullmatch(r"[0-9a-fA-F]{32}", self.source_asset_id):
            raise ValueError("Sketchfab source_asset_id must be a 32 character model UID")
        if self.provider == "sketchfab" and self.asset_type != "model":
            raise ValueError("Sketchfab acquisition supports models only")
        if self.provider == "polyhaven" and self.asset_type == "model" and self.format not in {"auto", "jpg", "png"}:
            raise ValueError("model format must be auto, jpg or png")
        return self


class ProviderAsset(_Frozen):
    provider: Provider
    source_asset_id: str
    name: str
    asset_type: AssetType
    source_url: str
    license: str
    attribution: str
    attribution_surface_required: bool
    thumbnail_url: str | None = None


class AssetSearchResult(_Frozen):
    results: list[ProviderAsset]
    provider: str
    credit: str
    credit_url: str
    page: int
    has_more: bool


class ProviderConfigurationError(ValueError):
    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(message)
