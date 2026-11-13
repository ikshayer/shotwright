"""The shared, typed operation registry for HTTP, CLI, and agent tools."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator.adapters.assets.models import (
    AssetAcquireRequest,
    AssetCategoryRequest,
    AssetPreviewRequest,
    AssetSearchRequest,
)
from orchestrator.service.minimax_h3 import H3CancelBody, H3GenerateBody
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    Location,
    PostProfile,
    Project,
    RubricScores,
    Script,
    Shot,
)


class OpenRequest(BaseModel):
    """Typed envelope for operations whose adapters are not implemented yet."""

    model_config = ConfigDict(extra="allow")

    project_id: str | None = None
    episode_id: str | None = None
    shot_id: str | None = None


class ScriptRevision(BaseModel):
    script: Script
    expected_version: int


class ShotRevision(BaseModel):
    shot: Shot
    expected_version: int


class EpisodeCreation(BaseModel):
    episode: Episode


class LocationCreation(BaseModel):
    location: Location


class LocationRevision(BaseModel):
    location: Location
    expected_version: int = Field(ge=1)
    episode_ids: list[str]


class CameraProfileCreation(BaseModel):
    camera_profile: CameraProfile


class PostProfileCreation(BaseModel):
    post_profile: PostProfile


class ScriptDraft(BaseModel):
    script: Script


class ShotProposal(BaseModel):
    shots: list[Shot]


class RubricRequest(BaseModel):
    scores: RubricScores


class ScriptDerivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_episode_version: int = Field(ge=1)
    expected_shot_versions: dict[str, int]
    expected_location_versions: dict[str, int]

    @field_validator("expected_shot_versions", "expected_location_versions")
    @classmethod
    def nonnegative_versions(cls, value: dict[str, int]) -> dict[str, int]:
        if any(type(version) is not int or version < 0 for version in value.values()):
            raise ValueError("expected versions must be nonnegative integers")
        return value


class AssetBrowserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["polyhaven", "sketchfab"] = "polyhaven"
    q: str | None = Field(None, max_length=200)
    asset_type: Literal["model", "texture", "hdri"] = "model"
    limit: int = Field(10, ge=1, le=50)
    page: int = Field(1, ge=1, le=3)
    categories: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("categories")
    @classmethod
    def safe_categories(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("categories must be unique")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item) for item in value):
            raise ValueError("categories must be safe slugs")
        return value


class AssetRegistration(BaseModel):
    """Provenance fields submitted alongside one permitted local asset file."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str | None = None
    type: str | None = None
    generated: bool = False
    source_url: str | None = None
    provider: str | None = None
    license: str | None = None
    model_or_voice_license: str | None = None
    attribution: str | None = None
    attribution_surface_required: bool = False
    reuse_scope: str = "project"
    expected_version: int = Field(default=0, ge=0)


#: Leading bytes carried for magic-byte detection; enough for every signature
#: platform.containment recognises.
MAGIC_HEADER_BYTES = 64


@dataclass(frozen=True)
class UploadedFile:
    """What upload validation needs about one uploaded file.

    The bytes are not buffered in this record. Registration receives either the CLI's
    source path or the multipart parser's seekable spool and copies it in bounded chunks
    through ProjectRoot.
    """

    filename: str
    #: The leading bytes, enough for magic-byte detection.
    head: bytes
    #: The real size as the multipart parser measured it, not the number of bytes read.
    size_bytes: int
    #: CLI source path, or the already-spooled multipart file stream.
    source_path: Path | None = None
    stream: BinaryIO | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str
    summary: str
    mutating: bool
    adapter: str
    request_model: type[BaseModel] | None
    invoke: Callable[[Any, dict[str, Any]], Any]
    #: Declares a multipart surface carrying uploaded files instead of a JSON body.
    upload: bool = False


def _call(name: str) -> Callable[[Any, dict[str, Any]], Any]:
    def invoke(context: Any, payload: dict[str, Any]) -> Any:
        return context.invoke(name, payload)

    return invoke


def path_parameters(path: str) -> list[str]:
    """The ``{name}`` segments of a route path, in order."""
    return [part[1:-1] for part in path.split("/") if part.startswith("{") and part.endswith("}")]


# This is the sole operation list. OpenAPI, CLI commands, and tool definitions
# are generated from these entries.
OPERATIONS = (
    Operation("health", "GET", "/health", "Check API health", False, "api", None, _call("health")),
    Operation("create_project", "POST", "/projects", "Create a project", True, "service.dependencies.create_project / store", Project, _call("create_project")),
    Operation("create_camera_profile", "POST", "/projects/{project_id}/camera-profiles/{camera_id}", "Create a camera profile", True, "service.dependencies.create_camera_profile", CameraProfileCreation, _call("create_camera_profile")),
    Operation("create_post_profile", "POST", "/projects/{project_id}/post-profiles/{profile_name}", "Create a post profile", True, "service.dependencies.create_post_profile", PostProfileCreation, _call("create_post_profile")),
    Operation("create_episode", "POST", "/projects/{project_id}/episodes", "Create an episode", True, "service.dependencies.create_episode", EpisodeCreation, _call("create_episode")),
    Operation("create_location", "POST", "/projects/{project_id}/locations/{location_id}", "Create a project Location with its base shell and placements", True, "service.dependencies.create_location", LocationCreation, _call("create_location")),
    Operation("revise_location", "POST", "/projects/{project_id}/locations/{location_id}/revise", "Revise a Location and invalidate dependent shots", True, "service.dependencies.revise_location", LocationRevision, _call("revise_location")),
    Operation("list_projects", "GET", "/projects", "List projects", False, "store", None, _call("list_projects")),
    Operation("load_project", "GET", "/projects/{project_id}", "Load a project", False, "store", None, _call("load_project")),
    Operation("draft_script", "POST", "/projects/{project_id}/episodes/{episode_id}/scripts/draft", "Persist an agent-authored script", True, "service.dependencies.draft_script", ScriptDraft, _call("draft_script")),
    Operation("revise_script", "POST", "/projects/{project_id}/episodes/{episode_id}/scripts/revise", "Revise a script", True, "service.dependencies.revise_script", ScriptRevision, _call("revise_script")),
    Operation("derive_script_scene_plan", "POST", "/projects/{project_id}/episodes/{episode_id}/scripts/{script_id}/derive", "Derive scene plans from structured script direction", True, "service.script_authoring.derive_script_scene_plan", ScriptDerivationRequest, _call("derive_script_scene_plan")),
    Operation("list_episode_credits", "GET", "/projects/{project_id}/episodes/{episode_id}/credits", "List episode asset credits and publication blockers", False, "service.credits.collect_episode_credits", None, _call("list_episode_credits")),
    Operation("propose_shots", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/propose", "Persist agent-authored shots", True, "service.dependencies.propose_shots", ShotProposal, _call("propose_shots")),
    Operation("revise_shot", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/revise", "Revise a shot", True, "service.dependencies.revise_shot", ShotRevision, _call("revise_shot")),
    Operation("search_assets", "GET", "/projects/{project_id}/assets/search", "Search provider assets", False, "service.sourcing.search_assets", AssetSearchRequest, _call("search_assets")),
    Operation("list_asset_categories", "GET", "/projects/{project_id}/assets/categories", "List provider asset categories", False, "service.sourcing.list_asset_categories", AssetCategoryRequest, _call("list_asset_categories")),
    Operation("asset_preview", "GET", "/projects/{project_id}/assets/preview", "Fetch a bounded provider asset thumbnail", False, "service.sourcing.preview_asset", AssetPreviewRequest, _call("asset_preview")),
    Operation("acquire_asset", "POST", "/projects/{project_id}/assets/acquire", "Acquire and register a provider asset", True, "service.sourcing.acquire_asset", AssetAcquireRequest, _call("acquire_asset")),
    Operation("asset_browser", "GET", "/projects/{project_id}/assets/browser", "Browse provider assets and project credits", False, "service.sourcing.search_assets", AssetBrowserRequest, _call("asset_browser")),
    Operation("create_asset", "POST", "/projects/{project_id}/assets", "Register a project-local asset file", True, "service.assets.register_asset", AssetRegistration, _call("create_asset"), upload=True),
    Operation("build_scene", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/scene", "Build a scene", True, "scene builder adapter", OpenRequest, _call("build_scene")),
    Operation("render_preview", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/preview", "Render a preview", True, "preview renderer adapter", OpenRequest, _call("render_preview")),
    Operation("render_master", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/master", "Render a master", True, "master renderer adapter", OpenRequest, _call("render_master")),
    Operation("generate_media", "POST", "/projects/{project_id}/media/generate", "Generate media", True, "media generation adapter", OpenRequest, _call("generate_media")),
    Operation("degrade_media", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/degrade", "Degrade a shot master", True, "PostProfile adapter", OpenRequest, _call("degrade_media")),
    Operation("sample_references", "POST", "/projects/{project_id}/references/sample", "Sample references", True, "reference sampling adapter", OpenRequest, _call("sample_references")),
    Operation("generate_h3_candidate", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/h3-candidates", "Generate an opt-in MiniMax H3 audiovisual candidate", True, "service.minimax_h3", H3GenerateBody, _call("generate_h3_candidate")),
    Operation("list_h3_candidates", "GET", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/h3-candidates", "List immutable H3 candidate receipts", False, "store", None, _call("list_h3_candidates")),
    Operation("cancel_h3_candidate", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/h3-candidates/{job_id}/cancel", "Cancel an H3 candidate job", True, "jobs.queue", H3CancelBody, _call("cancel_h3_candidate")),
    Operation("inspect_h3", "GET", "/models/minimax-h3", "Inspect configured MiniMax H3 runtime", False, "service.minimax_h3", None, _call("inspect_h3")),
    Operation("report_jobs", "GET", "/projects/{project_id}/jobs", "Report jobs", False, "jobs.queue", None, _call("report_jobs")),
    Operation("apply_typed_feedback", "POST", "/projects/{project_id}/feedback", "Apply typed feedback", True, "service.apply_typed_feedback", OpenRequest, _call("apply_typed_feedback")),
    Operation("score_rubric", "POST", "/projects/{project_id}/rubric/score", "Score rubric criteria", True, "domain.entities.RubricScores", RubricRequest, _call("score_rubric")),
    Operation("export", "POST", "/projects/{project_id}/export", "Export a project", True, "export adapter", OpenRequest, _call("export")),
)

#: Operations that submit work to the JobQueue and answer 202 with a job record rather
#: than performing the work inline (CONTRACT.md Amendment 5). Declared once, because this
#: set was previously spelled out at three separate call sites: adding `render_master` to
#: the submission gate but not to the two status sites produced a route that queued the
#: job correctly and then reported 200, and only a conformance test caught it.
ASYNC_ADAPTER_OPERATIONS = frozenset({
    "build_scene",
    "render_preview",
    "render_master",
    "generate_media",
    "degrade_media",
    "generate_h3_candidate",
})
