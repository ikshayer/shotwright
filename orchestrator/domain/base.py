"""Shared vocabulary for the domain model.

Enumerations here are closed sets drawn from the PRD. They are deliberately in
one place because several of them are referenced by both entities and the
behaviour modules (state, invalidation), and duplicating them would create the
second source of truth that 10.0 exists to prevent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FieldSerializationInfo,
    field_serializer,
    field_validator,
)

LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2


class ShotStatus(StrEnum):
    """States from 10.2. They bind to `(shot_id, version)`, not to a shot."""

    DRAFT = "draft"
    PLANNED = "planned"
    BLOCKING = "blocking"
    BUILDING = "building"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    STALE = "stale"


class FramingStatus(StrEnum):
    """FR-29. Pending framing blocks production; none and approved pass."""

    NONE = "none"
    PENDING = "pending"
    APPROVED = "approved"


class ReferenceRole(StrEnum):
    """FR-29's role exclusion: framing panels must not feed style extraction."""

    MOOD = "mood"
    FRAMING = "framing"


class InputKind(StrEnum):
    IMAGE_UPLOAD = "image_upload"
    VIDEO_UPLOAD = "video_upload"
    HOSTED_URL = "hosted_url"


class ProductionMethod(StrEnum):
    """The five routing options of 9."""

    BLENDER = "blender"
    IMAGE_COMPOSITE = "image_composite"
    BLENDER_WITH_INSERT = "blender_with_insert"
    IMAGE_TO_VIDEO = "image_to_video"
    SOURCED_MEDIA = "sourced_media"


class EvidenceTier(StrEnum):
    """Per-claim provenance in the style bible (FR-18)."""

    CREATOR = "creator"
    OBSERVED = "observed"
    REPORTED = "reported"
    INFERRED = "inferred"


class Invalidation(StrEnum):
    """4.4's per-field split, which keys the dependency graph.

    POST_ONLY changes are re-derived from the existing master.
    REBUILD changes invalidate renders through the dependency graph.
    """

    POST_ONLY = "post_only"
    REBUILD = "rebuild"


def post_only(default: Any = ..., **kwargs: Any) -> Any:
    """Declare a field re-derivable without a Blender render (4.4)."""
    return _tagged(default, Invalidation.POST_ONLY, **kwargs)


def rebuild_triggering(default: Any = ..., **kwargs: Any) -> Any:
    """Declare a field whose change invalidates renders (4.4)."""
    return _tagged(default, Invalidation.REBUILD, **kwargs)


def _tagged(default: Any, kind: Invalidation, **kwargs: Any) -> Any:
    """Attach the invalidation class to the field itself.

    The tag lives with the field definition rather than in a side table, so a
    field cannot be added without classifying it. `invalidation.py` reads these
    back; nothing else should need to.
    """
    extra = dict(kwargs.pop("json_schema_extra", {}) or {})
    extra["invalidation"] = kind.value
    if "default_factory" in kwargs:
        if default is not ...:
            raise TypeError("a tagged field cannot specify both a default and a default_factory")
        return Field(
            default_factory=kwargs.pop("default_factory"),
            json_schema_extra=extra,
            **kwargs,
        )
    return Field(default, json_schema_extra=extra, **kwargs)


class Entity(BaseModel):
    """Base for every persisted record.

    `schema_version` travels with the data because the project folder is the
    portable artifact (10.0, AT-09) and must be readable by a later build.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: int = SCHEMA_VERSION

    @field_validator("schema_version", mode="before")
    @classmethod
    def _migrate_schema_version(cls, value: Any) -> int:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        if value == LEGACY_SCHEMA_VERSION:
            # Version 2 adds the native Export layout. Existing entity payloads
            # retain their fields and lifecycle metadata through the migration.
            return 2
        if value != SCHEMA_VERSION:
            raise ValueError(f"schema_version {value} has no migration to {SCHEMA_VERSION}")
        return value

    @field_serializer("schema_version")
    def _serialize_schema_version(self, value: int, info: FieldSerializationInfo) -> int:
        current = self._migrate_schema_version(value)
        if isinstance(info.context, dict) and info.context.get("entity_schema_version") == LEGACY_SCHEMA_VERSION:
            return LEGACY_SCHEMA_VERSION
        return current

    def schema_v1_payload(self) -> dict[str, Any]:
        """Render a JSON value for comparisons against exact legacy receipts and journals."""
        return self.model_dump(mode="json", context={"entity_schema_version": LEGACY_SCHEMA_VERSION})


class VersionedEntity(Entity):
    """Append-only versioning (10.2).

    A revision creates a new record with `supersedes` set; the superseded
    record and its artifacts are retained, never overwritten.
    """

    version: int = Field(1, ge=1)
    supersedes: int | None = Field(None, ge=1)

    def next_version(self) -> dict[str, Any]:
        """Field overrides for the successor to this record."""
        return {"version": self.version + 1, "supersedes": self.version}
