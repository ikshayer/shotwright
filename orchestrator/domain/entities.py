"""The persisted domain model (PRD 10.1).

10.1 is the single normative definition of these fields, so this module mirrors
it directly and deliberately: one module, in the PRD's order, so the
correspondence can be audited by reading them side by side.

Two things here are load-bearing rather than descriptive:

  - `CameraProfile` fields are tagged post-only or rebuild-triggering (4.4).
    The tag lives on the field, so a field cannot be added without classifying
    it, and `invalidation.py` reads the tags back.
  - `Shot.status` is never set directly by an agent pass. `Review` is the only
    writer of approval state (10.1); the shot holds a pointer to the approving
    review, and transitions go through `state.py`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_serializer,
    field_validator,
    model_validator,
)

from .anomaly import AnomalySpec
from .base import (
    Entity,
    EvidenceTier,
    FramingStatus,
    InputKind,
    Invalidation,
    ProductionMethod,
    ReferenceRole,
    ShotStatus,
    VersionedEntity,
    post_only,
    rebuild_triggering,
)

# Motion is currently gated on non_cctv, but camera class and handheld mount are
# separate axes: a tripod-mounted camcorder is non_cctv and should not sway. If
# one is added, use a separate mount/motion attribute rather than redefining this mode.
CameraMode = Literal["cctv", "non_cctv"]
CAMERA_MOTION_MODEL_SEMVER = "0.1.0"

# --------------------------------------------------------------------------
# narrative
# --------------------------------------------------------------------------


class Beat(Entity):
    beat_id: str
    target_sec: float = Field(gt=0)
    subtlety: int = Field(ge=1, le=5)
    anomaly: str | None = None
    narration_text: str | None = None
    caption_text: str | None = None


class Script(VersionedEntity):
    script_id: str
    episode_id: str
    logline: str
    beats: list[Beat] = Field(default_factory=list)
    beat_to_shot: dict[str, str] = Field(default_factory=dict)
    approval_review_id: str | None = None

    @model_validator(mode="after")
    def _beat_ids_unique(self) -> Script:
        ids = [b.beat_id for b in self.beats]
        if len(ids) != len(set(ids)):
            raise ValueError("beat_id must be unique within a Script")
        return self


class Project(Entity):
    project_id: str
    title: str
    story_brief: str = ""
    style_defaults: dict[str, str] = Field(default_factory=dict)
    default_duration_sec: float = Field(180.0, gt=0)
    location_ids: list[str] = Field(default_factory=list)
    output_profile: str = "dvr-2005"


class Episode(Entity):
    episode_id: str
    project_id: str
    number: int = Field(ge=1)
    #: authoritative duration target; Project's is only a default
    duration_target_sec: float = Field(gt=0)
    shot_ids: list[str] = Field(default_factory=list)
    approved_cut_version: int | None = Field(None, ge=1)
    #: 10.1: Shot, Episode and StyleBible store a pointer to the approving
    #: Review. Gate C is recorded here, and any shot entering stale or draft
    #: revokes it (10.2).
    approval_review_id: str | None = None


# --------------------------------------------------------------------------
# production
# --------------------------------------------------------------------------


class ArtifactScheduleEntry(Entity):
    """Emitted by the PostProfile; drives decimation and the overlay clock."""

    type: str  # drop | freeze | timestamp_jump
    start_frame: int = Field(ge=0)
    count: int = Field(ge=1)


class Shot(VersionedEntity):
    shot_id: str
    episode_id: str
    location_id: str | None = None
    camera_id: str | None = None
    camera_profile: str = "dvr-2005"
    # FR-29 pins the rebuild-triggering values from this CameraProfile version
    # version whose rebuild-triggering values the creator accepted. Post-only
    # values still resolve from the latest profile for re-derivation.
    camera_profile_version: int | None = Field(None, ge=1)
    post_profile: str | None = None
    # Legacy scene look applies only when no Location is bound; a Location owns its look.
    scene_look: str | None = None
    camera_motion: str | None = None  # FR-30, non-CCTV profiles only
    beat_id: str | None = None
    narrative_beat: str = ""
    duration_sec: float = Field(gt=0)
    event_sec: float | None = Field(None, ge=0)
    framing: str = ""
    production_method: ProductionMethod | None = None
    fallback_method: ProductionMethod | None = None
    routing_rationale: str = ""
    figure_params: dict[str, float | str | bool] = Field(default_factory=dict)
    anomaly: AnomalySpec | None = None
    asset_ids: list[str] = Field(default_factory=list)
    audio_refs: list[str] = Field(default_factory=list)
    degrade_seed: int = Field(0, ge=0)
    motion_seed: int = Field(0, ge=0)
    motion_model_semver: str = CAMERA_MOTION_MODEL_SEMVER
    artifact_schedule: list[ArtifactScheduleEntry] = Field(default_factory=list)
    degradation_level: int = Field(0, ge=0)
    dependencies: list[str] = Field(default_factory=list)
    status: ShotStatus = ShotStatus.DRAFT
    approval_review_id: str | None = None
    approval_notes: str = ""
    #: FR-29: field names the creator wrote by hand. No agent pass may overwrite
    #: these; a conflict is reported, never silently resolved.
    creator_authored: set[str] = Field(default_factory=set)
    framing_panel_ref: str | None = None
    framing_status: FramingStatus = FramingStatus.NONE

    @model_validator(mode="after")
    def _event_within_duration(self) -> Shot:
        if self.event_sec is not None and self.event_sec > self.duration_sec:
            raise ValueError(
                f"event_sec {self.event_sec} falls outside duration_sec {self.duration_sec}"
            )
        if self.anomaly is not None and self.event_sec is None:
            raise ValueError("Shot.event_sec is required when anomaly is specified")
        return self

    @field_validator("motion_model_semver")
    @classmethod
    def _motion_model_semver_shape(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise ValueError(
                f"motion_model_semver must be MAJOR.MINOR.PATCH, got {value!r}"
            )
        return value


    @model_validator(mode="after")
    def _approved_shots_cite_a_review(self) -> Shot:
        if self.status is ShotStatus.APPROVED and not self.approval_review_id:
            raise ValueError(
                "an approved Shot must carry approval_review_id - Review is the only "
                "writer of approval state (10.1)"
            )
        return self

    @field_serializer("creator_authored")
    def _stable_creator_authored(self, value: set[str]) -> list[str]:
        """Sort on the way out.

        A set serialises in arbitrary order, so the same record would produce
        different bytes on different runs. The project folder is the portable
        artifact and is checksummed (FR-13, AT-09), so stored JSON must be
        stable. Kept as a set in code because membership is the operation.
        """
        return sorted(value)


class ShotOutputKind(StrEnum):
    """The produced-file categories attached to one Shot content version."""

    SCENE = "scene"
    MASTER = "master"
    PREVIEW = "preview"
    STILL = "still"
    DEGRADED = "degraded"


class ShotOutput(Entity):
    """Immutable receipt binding a retained output file to its source Shot version."""

    output_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    episode_id: str = Field(min_length=1)
    shot_id: str = Field(min_length=1)
    record_version: int = Field(ge=1, strict=True)
    kind: ShotOutputKind
    relative_path: str = Field(min_length=1)
    content_sha256: str
    camera_profile_id: str | None = None
    camera_profile_version: int | None = Field(None, ge=1, strict=True)
    engine_id: str | None = None
    engine_build: str | None = None
    profile_semver: str | None = None
    filter_graph_hash: str | None = None
    pinned_binaries: dict[str, str] | None = None
    frame_count: StrictInt | None = Field(None, gt=0)
    output_fps: StrictInt | None = Field(None, gt=0)

    @field_validator("relative_path")
    @classmethod
    def _relative_artifact_path(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("relative_path must not contain NUL")
        posix = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if posix.is_absolute() or windows.is_absolute() or windows.drive:
            raise ValueError("relative_path must be project-relative")
        if any(part == ".." for part in value.replace("\\", "/").split("/")):
            raise ValueError("relative_path must not traverse above its project-relative location")
        return value

    @field_validator("content_sha256", "filter_graph_hash")
    @classmethod
    def _sha256_hex(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("hash must be 64 lowercase hexadecimal characters")
        return value

    @model_validator(mode="after")
    def _provenance_matches_kind(self) -> ShotOutput:
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256):
            raise ValueError("content_sha256 must be 64 lowercase hexadecimal characters")
        if (self.camera_profile_id is None) != (self.camera_profile_version is None):
            raise ValueError("CameraProfile provenance must provide both id and version or neither")
        if (self.frame_count is None) != (self.output_fps is None):
            raise ValueError("degraded output timing must provide both frame_count and output_fps or neither")
        if self.kind is not ShotOutputKind.DEGRADED and (
            self.frame_count is not None or self.output_fps is not None
        ):
            raise ValueError(f"{self.kind.value} output may not carry degraded output timing")
        if self.kind not in {
            ShotOutputKind.SCENE, ShotOutputKind.MASTER, ShotOutputKind.PREVIEW,
        } and self.camera_profile_id is not None:
            raise ValueError(f"{self.kind.value} output may not carry CameraProfile provenance")
        if self.kind is ShotOutputKind.SCENE:
            if (self.engine_id is None) != (self.engine_build is None):
                raise ValueError("scene output must provide both engine_id and engine_build or neither")
            if self.engine_id is not None and (not self.engine_id or not self.engine_build):
                raise ValueError("scene engine provenance fields must be non-empty when provided")
            if any(value is not None for value in
                   (self.profile_semver, self.filter_graph_hash, self.pinned_binaries)):
                raise ValueError("scene outputs may not carry degradation provenance")
        elif self.kind is ShotOutputKind.MASTER:
            if not self.engine_id or not self.engine_build:
                raise ValueError(f"{self.kind.value} output requires engine_id and engine_build")
            if any(value is not None for value in
                   (self.profile_semver, self.filter_graph_hash, self.pinned_binaries)):
                raise ValueError("render outputs may not carry degradation provenance")
        elif self.kind is ShotOutputKind.PREVIEW:
            if not self.engine_id or not self.engine_build:
                raise ValueError(f"{self.kind.value} output requires engine_id and engine_build")
            if any(value is not None for value in
                   (self.profile_semver, self.filter_graph_hash, self.pinned_binaries)):
                raise ValueError("preview outputs may not carry degradation or master conversion provenance")
        elif self.kind is ShotOutputKind.DEGRADED:
            if not self.profile_semver or not self.filter_graph_hash or not self.pinned_binaries:
                raise ValueError("degraded output requires profile_semver, filter_graph_hash, and pinned_binaries")
            if self.engine_id is not None or self.engine_build is not None:
                raise ValueError("degraded output may not carry render provenance")
            if any(not key or not value for key, value in self.pinned_binaries.items()):
                raise ValueError("pinned_binaries keys and values must be non-empty")
        elif any(value is not None for value in (
            self.engine_id, self.engine_build, self.profile_semver, self.filter_graph_hash,
            self.pinned_binaries,
        )):
            raise ValueError("still output has no additional provenance fields")
        return self


class AssetPlacement(BaseModel):
    """A registered geometry asset's declared metre envelope and Location transform."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    asset_id: str
    position: tuple[float, float, float]
    dimensions: tuple[float, float, float]
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @field_validator("asset_id")
    @classmethod
    def nonempty_asset_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("asset_id must not be empty")
        return value

    @field_validator("position", "dimensions", "rotation")
    @classmethod
    def finite_transform(
        cls, value: tuple[float, float, float], info: Any,
    ) -> tuple[float, float, float]:
        if any(not math.isfinite(component) for component in value):
            raise ValueError(f"{info.field_name} values must be finite")
        if info.field_name == "dimensions" and any(component <= 0 for component in value):
            raise ValueError("dimensions must be finite and strictly positive")
        return value


class Location(VersionedEntity):
    location_id: str
    blend_path: str | None = None
    geometry_provenance: str = ""
    # Reserved for non-geometry resources such as textures or HDRIs.
    asset_ids: list[str] = Field(default_factory=list)
    base_shell: Literal["american_living_room"] = rebuild_triggering(
        "american_living_room"
    )
    scene_look: Literal["bright", "low_key"] = rebuild_triggering("bright")
    placements: list[AssetPlacement] = rebuild_triggering(default_factory=list)
    camera_rigs: list[str] = Field(default_factory=list)
    approval_review_id: str | None = None

    @field_validator("asset_ids")
    @classmethod
    def unique_nonempty_asset_ids(cls, value: list[str]) -> list[str]:
        if any(not asset_id.strip() for asset_id in value):
            raise ValueError("asset_ids must contain nonempty ids")
        if len(value) != len(set(value)):
            raise ValueError("asset_ids must be unique")
        return value

    @model_validator(mode="after")
    def _asset_ids_exclude_placed_geometry(self) -> Location:
        """Keep texture/HDRI references separate from geometry placements."""
        overlaps = sorted(set(self.asset_ids) & {item.asset_id for item in self.placements})
        if overlaps:
            raise ValueError(
                "asset_ids is reserved for non-geometry resources and cannot include placed "
                f"geometry ids: {overlaps}"
            )
        return self


class CameraProfile(VersionedEntity):
    """4.4's split is encoded in the field tags, not in prose.

    FR-07 may only promise "no re-render" for post-only fields; FR-11's
    dependency graph keys invalidation off these tags.
    """

    camera_id: str
    profile_name: str
    camera_mode: CameraMode = rebuild_triggering("cctv")

    # --- rebuild-triggering: requires a Blender re-render ---
    field_of_view_mm: float = rebuild_triggering(12.0, gt=0)
    barrel_distortion: float = rebuild_triggering(0.0)
    exposure_ev: float = rebuild_triggering(0.0)
    practical_light_color_k: int = rebuild_triggering(3200, gt=0)
    practical_light_intensity: float = rebuild_triggering(1.0, ge=0)
    monochrome_night: bool = rebuild_triggering(False)
    camera_position: tuple[float, float, float] | None = rebuild_triggering(None)
    camera_mount: str | None = rebuild_triggering(None)
    depth_of_field: float | None = rebuild_triggering(None)

    # --- post-only: re-derived from the existing master ---
    codec: str = post_only("libx264")
    bitrate_kbps: int = post_only(800, gt=0)
    storage_resolution: tuple[int, int] = post_only((640, 480))
    display_aspect_ratio: str = post_only("4:3")
    fps: float = post_only(12.0, gt=0)
    decimation_pattern: str | None = post_only(None)
    noise_model: dict[str, float] = post_only(default_factory=dict)
    chroma_smearing: float = post_only(0.0, ge=0)
    block_artifacts: float = post_only(0.0, ge=0)
    sharpening: float = post_only(0.0, ge=0)
    grade_white_balance: dict[str, float] = post_only(default_factory=dict)
    overlay_model: dict[str, str | int | float] = post_only(default_factory=dict)
    analog_artifacts: dict[str, float] = post_only(default_factory=dict)
    frame_drop_schedule: list[ArtifactScheduleEntry] = post_only(default_factory=list)


class PostStage(Entity):
    """One ordered stage of the degradation chain (FR-21)."""

    name: str
    filter: str
    params: dict[str, float | int | str] = Field(default_factory=dict)
    #: where this stage's randomness comes from; None for deterministic stages
    seed_source: str | None = None
    #: FR-21: overlays declare which side of degradation they sit on
    composite_stage: str | None = None  # in_recording | on_playback


class PostProfileOverlayIndicator(Entity):
    """A second, fixed battery/REC graphic burned into the recording."""

    kind: Literal["battery_rec"] = "battery_rec"
    fontsize: StrictInt = Field(gt=0)
    x: StrictInt | str
    y: StrictInt | str
    placement: Literal["in_recording"] = "in_recording"


class PostProfileOverlay(Entity):
    """Persisted overlay configuration, including its recorded/composited side."""

    camera_id: str
    clock_epoch: int
    fontsize: StrictInt = Field(gt=0)
    # Optional only so existing schema-version-1 profiles remain readable. Degradation
    # composition rejects a missing path with the owning profile name; it never chooses
    # a process-local font. ProjectStore validates any supplied value via ProjectRoot.
    font_relative: str | None = Field(None, min_length=1)
    x: StrictInt | str = 8
    y: StrictInt | str = "h-24"
    placement: Literal["in_recording", "on_playback"] = "in_recording"
    indicator: PostProfileOverlayIndicator | None = None


class PostProfileEncode(Entity):
    """Persisted encoder settings used to compose the PostProfile adapter request."""

    codec: str = Field(min_length=1)
    preset: str = "medium"
    crf: StrictInt = Field(ge=0, le=51)
    pix_fmt: str = Field(min_length=1)
    threads: StrictInt = Field(ge=1)


class PostProfile(Entity):
    name: str
    semver: str
    stages: list[PostStage] = Field(default_factory=list)
    #: pinned identities of the binaries this profile was authored against
    pinned_binaries: dict[str, str] = Field(default_factory=dict)
    filter_graph_hash: str | None = None
    #: Adapter configuration is persisted with this ordinary project-local record.
    #: None means an authored record is incomplete and cannot yet be composed.
    output_fps: StrictInt | None = Field(None, gt=0)
    storage_resolution: tuple[StrictInt, StrictInt] | None = None
    sample_aspect_ratio: str | None = None
    artifact_schedule: list[ArtifactScheduleEntry] = Field(default_factory=list)
    overlay: PostProfileOverlay | None = None
    encode: PostProfileEncode | None = None
    provenance: str = ""

    @field_validator("semver")
    @classmethod
    def _semver_shape(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"semver must be MAJOR.MINOR.PATCH, got {v!r}")
        return v


class TimelineEntry(Entity):
    type: str  # shot | card | pause
    ref: str | None = None
    start_sec: float = Field(ge=0)
    duration_sec: float = Field(gt=0)


class Timeline(Entity):
    episode_id: str
    entries: list[TimelineEntry] = Field(default_factory=list)
    audio_tracks: list[str] = Field(default_factory=list)

    @property
    def total_duration_sec(self) -> float:
        """Derived, never stored: a stored total can contradict the entries."""
        return max((e.start_sec + e.duration_sec for e in self.entries), default=0.0)

    @property
    def content_sha256(self) -> str:
        """Stable digest of the declarative cut, independent of store metadata."""
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class EpisodeOutput(Entity):
    """Immutable receipt for a rendered episode cut, separate from per-shot outputs."""

    output_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    episode_id: str = Field(min_length=1)
    kind: Literal["cut"] = "cut"
    timeline_store_version: StrictInt = Field(ge=1)
    timeline_sha256: str
    relative_path: str = Field(min_length=1)
    content_sha256: str
    delivery_profile: str = Field(min_length=1)
    storage_resolution: tuple[StrictInt, StrictInt]
    output_fps: StrictInt = Field(gt=0)
    sample_aspect_ratio: str = Field(min_length=1)
    ffmpeg_identity: str = Field(min_length=1)

    @field_validator("storage_resolution")
    @classmethod
    def _positive_storage_resolution(cls, value: tuple[int, int]) -> tuple[int, int]:
        if len(value) != 2 or any(dimension <= 0 for dimension in value):
            raise ValueError("storage_resolution must contain two positive dimensions")
        return value

    @field_validator("relative_path")
    @classmethod
    def _relative_artifact_path(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("relative_path must not contain NUL")
        posix = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if posix.is_absolute() or windows.is_absolute() or windows.drive:
            raise ValueError("relative_path must be project-relative")
        if any(part == ".." for part in value.replace("\\", "/").split("/")):
            raise ValueError("relative_path must not traverse above its project-relative location")
        return value

    @field_validator("content_sha256", "timeline_sha256")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("hash must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("sample_aspect_ratio")
    @classmethod
    def _positive_sample_aspect_ratio(cls, value: str) -> str:
        parts = value.replace(":", "/").split("/")
        if (len(parts) != 2 or not all(part.isdigit() for part in parts)
                or any(int(part) <= 0 for part in parts)):
            raise ValueError("sample_aspect_ratio must be a positive N:D ratio")
        return value


# --------------------------------------------------------------------------
# provenance and rights
# --------------------------------------------------------------------------


class Asset(Entity):
    asset_id: str
    type: str
    generated: bool
    source_url: str | None = None
    provider: str | None = None
    license: str
    #: FR-16: the weights' or voice's own licence, not merely the engine's
    model_or_voice_license: str | None = None
    file_path: str | None = None
    checksum: str | None = None
    attribution: str | None = None
    #: Poly Haven's API requires visible credit even though assets are CC0
    attribution_surface_required: bool = False
    reuse_scope: str = "project"


class Attestation(Entity):
    """Tier 3 only (6.2): the creator's explicit, recorded act."""

    authorised_by: str
    method: str
    recorded_at: date


class Reference(Entity):
    reference_id: str
    input_kind: InputKind
    role: ReferenceRole
    owner_scope: str = "project"
    path_or_url: str
    start_sec: float | None = Field(None, ge=0)
    end_sec: float | None = Field(None, ge=0)
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    importance: str = "secondary"
    analysis_status: str = "not_inspected"
    permitted_metadata: dict[str, str] = Field(default_factory=dict)
    fetched_at: date | None = None
    inspection_tier: int = Field(1, ge=1, le=3)
    copy_retained: bool = False
    attestation: Attestation | None = None
    rights_status: str = ""

    @model_validator(mode="after")
    def _tier_three_requires_attestation(self) -> Reference:
        if self.inspection_tier == 3 and self.attestation is None:
            raise ValueError(
                "inspection_tier 3 requires a recorded attestation (6.2) - it is an "
                "explicit act of the creator, not a setting"
            )
        if (
            self.start_sec is not None
            and self.end_sec is not None
            and self.end_sec <= self.start_sec
        ):
            raise ValueError("end_sec must follow start_sec")
        return self


class StyleClaim(Entity):
    claim: str
    evidence_tier: EvidenceTier
    reference_id: str | None = None
    timestamp_sec: float | None = Field(None, ge=0)


class StyleBible(VersionedEntity):
    project_id: str
    camera: list[StyleClaim] = Field(default_factory=list)
    composition: list[StyleClaim] = Field(default_factory=list)
    lighting: list[StyleClaim] = Field(default_factory=list)
    temporal_artifacts: list[StyleClaim] = Field(default_factory=list)
    sound_and_pacing: list[StyleClaim] = Field(default_factory=list)
    desired: list[StyleClaim] = Field(default_factory=list)
    avoided: list[StyleClaim] = Field(default_factory=list)
    approval_review_id: str | None = None


# --------------------------------------------------------------------------
# execution and review
# --------------------------------------------------------------------------


class Job(Entity):
    job_id: str
    type: str
    required_vram_gb: float = Field(0.0, ge=0)
    required_ram_gb: float = Field(0.0, ge=0)
    required_disk_gb: float = Field(0.0, ge=0)
    lease_holder_pid: int | None = None
    state: str = "queued"
    progress: float = Field(0.0, ge=0, le=1)
    attempts: int = Field(0, ge=0)
    error_kind: str | None = None
    error_message: str | None = None
    error_cause: dict[str, str] | None = None
    error_traceback: str | None = None
    error_traceback_truncated: bool = False
    error_origin: str | None = None
    resource_metrics: dict[str, float] = Field(default_factory=dict)
    log_path: str | None = None
    shot_id: str | None = None
    #: FR-26 budgets GPU-minutes per shot AND per episode, so a job must know
    #: which episode it belongs to or the episode ceiling cannot be enforced.
    episode_id: str | None = None
    run_id: str | None = None


class GenerationRun(Entity):
    run_id: str
    tool: str
    tool_version: str
    #: prompt/script, or the resolved FFmpeg filter graph for a post pass
    invocation: str
    seed: int | None = None
    settings: dict[str, str | int | float] = Field(default_factory=dict)
    input_paths: list[str] = Field(default_factory=list)
    output_paths: list[str] = Field(default_factory=list)
    log_path: str | None = None
    status: str = "pending"
    resource_metrics: dict[str, float] = Field(default_factory=dict)


class RubricScores(Entity):
    """4.3's five binary criteria, scored per sample.

    The names are 4.3's verbatim, in its order. They are not interchangeable
    with similar-sounding ones: criterion 2 is about the *place* reading as
    real, 4 is about stability *across the sequence*, and 5 is about the
    anomaly sitting inside the recording rather than over it.
    """

    #: 1 - a reviewer can state where the camera is mounted
    mount_inferable: bool
    #: 2 - the room reads as an everyday real location, not a set
    plausible_place: bool
    #: 3 - no dolly, rack focus, shake or zoom the profile does not explain
    no_unmotivated_movement: bool
    #: 4 - no flicker, swimming surfaces or warping across the sequence
    stable_light_and_geometry: bool
    #: 5 - the horror element sits inside the recording, not pasted over it
    integrated_anomaly: bool

    def passed(self) -> bool:
        """One sample passes at >=4 of 5, with criterion 3 mandatory (4.3).

        This is the per-sample half of the rule. The cut-level rule - >=80% of
        sampled frames passing - is aggregation and belongs to FR-22's scorer,
        which consumes these.
        """
        if not self.no_unmotivated_movement:
            return False
        return sum(
            (
                self.mount_inferable,
                self.plausible_place,
                self.no_unmotivated_movement,
                self.stable_light_and_geometry,
                self.integrated_anomaly,
            )
        ) >= 4


class Review(Entity):
    """The only writer of approval state (10.1)."""

    review_id: str
    target_id: str
    target_version: int = Field(ge=1)
    created_at: datetime
    comment: str = ""
    approval_state: ShotStatus
    rubric_scores: list[RubricScores] = Field(default_factory=list)
    waiver_text: str | None = None
    created_revision: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def _only_terminal_opinions(self) -> Review:
        allowed = {ShotStatus.APPROVED, ShotStatus.REJECTED}
        if self.approval_state not in allowed:
            raise ValueError(
                f"a Review records approval or rejection, not {self.approval_state.value}"
            )
        return self


class Export(Entity):
    export_id: str
    output_path: str
    codec: str
    resolution: tuple[int, int]
    frame_rate: float = Field(gt=0)
    post_profile_version: str
    ffmpeg_version: str
    filter_graph_hash: str
    manifest_checksum: str
    validation_results: dict[str, bool] = Field(default_factory=dict)


#: Every field of CameraProfile must carry a 4.4 tag. Checked at import so a
#: new untagged field fails loudly rather than defaulting to a silent class.
def _assert_camera_profile_fully_tagged() -> None:
    untagged = []
    inherited = set(VersionedEntity.model_fields) | {"camera_id", "profile_name"}
    for name, info in CameraProfile.model_fields.items():
        if name in inherited:
            continue
        extra = info.json_schema_extra or {}
        if not isinstance(extra, dict) or "invalidation" not in extra:
            untagged.append(name)
    if untagged:
        raise RuntimeError(
            "CameraProfile fields lack a 4.4 invalidation tag: "
            + ", ".join(sorted(untagged))
            + " - use post_only() or rebuild_triggering()"
        )


_assert_camera_profile_fully_tagged()

__all__ = [
    "ArtifactScheduleEntry", "Asset", "AssetPlacement", "Attestation", "Beat", "CameraMode", "CameraProfile",
    "Episode", "EpisodeOutput", "Export", "GenerationRun", "Invalidation", "Job", "Location",
    "PostProfile", "PostProfileEncode", "PostProfileOverlay", "PostStage", "Project",
    "Reference", "Review", "RubricScores",
    "Script", "Shot", "ShotOutput", "ShotOutputKind", "StyleBible", "StyleClaim",
    "Timeline", "TimelineEntry",
]
