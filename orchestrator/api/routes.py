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
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    Location,
    PostProfile,
    Project,
    RubricScores,
    Script,
    Shot,
    VoiceConsent,
)
from orchestrator.adapters.perfedit import PauseDetection
from orchestrator.service.audiopost import AudioPostApplyBody, AudioPostProfileCreation
from orchestrator.service.minimax_h3 import H3CancelBody, H3GenerateBody, H3PromoteBody
from orchestrator.service.perfedit import PerformanceEditBody
from orchestrator.service.tts import IndexTts2SpeechBody, PiperSpeechBody


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
    model_config = ConfigDict(extra="forbid")

    location: Location
    expected_version: int = Field(ge=1)


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


class ProjectExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: str = Field(min_length=1)
    cut_output_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    expected_version: int = Field(strict=True, gt=0)


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
    voice_consent: VoiceConsent | None = None
    voice_consent_basis: str | None = None


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
    Operation("health", "GET", "/health", "Check that the pipeline API is up; answers {status: ok} and changes nothing.", False, "api", None, _call("health")),
    Operation("create_project", "POST", "/projects", "Create a project. payload is a bare Project, not wrapped: project_id (also its directory name) and title are required; story_brief, style_defaults, default_duration_sec (default 180) and output_profile (default dvr-2005) are optional. Also seeds the dvr-2005, low-light-bw and consumer-2002 camera and post profiles. Answers 409 if the project already exists.", True, "service.dependencies.create_project / store", Project, _call("create_project")),
    Operation("create_camera_profile", "POST", "/projects/{project_id}/camera-profiles/{camera_id}", "Create a CameraProfile (version 1) for camera_id. payload {camera_profile: CameraProfile}: camera_id must match the path and profile_name is required; lens, distortion, exposure, practical light, mount, codec, bitrate, resolution and fps have defaults. Every project already has dvr-2005, low-light-bw and consumer-2002; an existing camera_id answers 409.", True, "service.dependencies.create_camera_profile", CameraProfileCreation, _call("create_camera_profile")),
    Operation("create_post_profile", "POST", "/projects/{project_id}/post-profiles/{profile_name}", "Create a PostProfile: a versioned CCTV video degradation chain (FFmpeg stages, pinned binaries, output fps and resolution, overlay, encode). payload {post_profile: PostProfile}: name must match profile_name and semver must be MAJOR.MINOR.PATCH. Profiles are never overwritten; an existing name answers 409.", True, "service.dependencies.create_post_profile", PostProfileCreation, _call("create_post_profile")),
    Operation("create_audio_post_profile", "POST", "/projects/{project_id}/audio-post-profiles/{profile_name}", "Create an audio post profile: a versioned, seeded voice degradation chain, pinned to the installed FFmpeg", True, "service.audiopost", AudioPostProfileCreation, _call("create_audio_post_profile")),
    Operation("create_episode", "POST", "/projects/{project_id}/episodes", "Create an episode in an existing project. payload {episode: Episode}: episode_id, project_id (must match the path), number (1 or more) and duration_target_sec (the authoritative runtime). Shots are added later by propose_shots or derive_script_scene_plan. An existing episode_id answers 409.", True, "service.dependencies.create_episode", EpisodeCreation, _call("create_episode")),
    Operation("create_location", "POST", "/projects/{project_id}/locations/{location_id}", "Create a project Location (version 1): the room a shot is set in, with its base shell and asset placements. payload {location: Location}: location_id must match the path; base_shell is american_living_room, scene_look is bright or low_key, and placements position assets by asset_id. An existing location_id answers 409. derive_script_scene_plan creates Locations from Script.scenes itself, so use this only for a standalone room.", True, "service.dependencies.create_location", LocationCreation, _call("create_location")),
    Operation("revise_location", "POST", "/projects/{project_id}/locations/{location_id}/revise", "Revise a Location and invalidate the shots built on it. payload {location, expected_version}: location is the full successor (version + 1, supersedes = the current version, no approval), expected_version is the current mutation token (a 409 version_conflict reports it as actual), and every episode in the project is checked. A rebuild-relevant change marks affected approved shots stale and clears the episode's approved cut. Returns the location and the shot_ids invalidated.", True, "service.dependencies.revise_location", LocationRevision, _call("revise_location")),
    Operation("list_projects", "GET", "/projects", "List every project as its full Project record (read-only). Start here to find a project_id.", False, "store", None, _call("list_projects")),
    Operation("load_project", "GET", "/projects/{project_id}", "Load one project's Project record (read-only); answers 404 if it does not exist.", False, "store", None, _call("load_project")),
    Operation("draft_script", "POST", "/projects/{project_id}/episodes/{episode_id}/scripts/draft", "Save an agent-written Script as version 1; it generates and invalidates nothing. payload {script: Script}: script_id, episode_id (must match the path), logline, beats (beat_id, target_sec, subtlety 1-5, optional narration_text, caption_text and scene_direction), beat_to_shot and scenes (embedded Location blueprints). The episode must exist. An existing script_id answers 409; use revise_script after the first draft.", True, "service.dependencies.draft_script", ScriptDraft, _call("draft_script")),
    Operation("revise_script", "POST", "/projects/{project_id}/episodes/{episode_id}/scripts/revise", "Append a new Script version. payload {script, expected_version}: script is the full successor (version + 1, supersedes = the current version, no approval) and expected_version is the current mutation token (a 409 version_conflict reports it as actual). Approved shots affected by changed beats, beat_to_shot entries or scenes become stale, and episodes with affected shots lose their approved cut; a change that touches no shot (a logline, say) invalidates nothing. Returns the script and the shot_ids invalidated.", True, "service.dependencies.revise_script", ScriptRevision, _call("revise_script")),
    Operation("derive_script_scene_plan", "POST", "/projects/{project_id}/episodes/{episode_id}/scripts/{script_id}/derive", "Turn a structured Script into production records: a Location for each of Script.scenes and one Shot per beat (duration from target_sec; location_id, camera_profile, framing, event_sec and anomaly from the beat's scene_direction), checking each shot's scene plan without rendering. Missing records are created (new shots are draft Blender shots), changed ones get a new version (shots return to draft), and unchanged ones, including approved shots, are kept as they are. Every beat needs a scene_direction and a beat_to_shot entry. payload {expected_version, expected_episode_version, expected_shot_versions, expected_location_versions}: the script and episode mutation tokens, then maps of tokens covering exactly the beat_to_shot shot ids and the Script.scenes location ids (0 for records that do not exist yet). It proposes and revises shots itself, so there is no need to call propose_shots afterwards. Returns what changed, the new tokens and a timing summary against the episode target.", True, "service.script_authoring.derive_script_scene_plan", ScriptDerivationRequest, _call("derive_script_scene_plan")),
    Operation("list_episode_credits", "GET", "/projects/{project_id}/episodes/{episode_id}/credits", "List the licence credits for every asset in an episode's built scenes, plus publication blockers (a licence other than CC0 or CC-BY-4.0, or an unsuitable reuse scope). Read-only, but it fails unless every shot in the episode already has a build_scene output.", False, "service.credits.collect_episode_credits", None, _call("list_episode_credits")),
    Operation("propose_shots", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/propose", "Append new draft Shots (version 1) to an episode and its shot list; generates nothing. payload {shots: [Shot]}: each needs shot_id, episode_id (must match the path) and duration_sec, plus optional location_id, camera_profile, beat_id, narrative_beat, framing, event_sec and anomaly. If any shot_id already exists it answers 409 and writes nothing. Prefer derive_script_scene_plan when the shots come from a script.", True, "service.dependencies.propose_shots", ShotProposal, _call("propose_shots")),
    Operation("revise_shot", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/revise", "Append a new Shot version. payload {shot, expected_version}: shot is the full successor (version + 1, supersedes = the current version, status draft, no approval) whose shot_id and episode_id match the path, and expected_version is the current mutation token (a 409 version_conflict reports it as actual). Changing camera_profile requires clearing camera_profile_version.", True, "service.dependencies.revise_shot", ShotRevision, _call("revise_shot")),
    Operation("search_assets", "GET", "/projects/{project_id}/assets/search", "Search Poly Haven (CC0 models, textures and HDRIs) or Sketchfab (downloadable models only, CC0 or CC-BY-4.0 only); writes nothing. Params: q (required), provider (polyhaven or sketchfab), asset_type (model, texture or hdri), limit (1-50), page (1-3) and categories (slugs from list_asset_categories). Each result has a source_asset_id, licence, attribution and thumbnail_url; pass source_asset_id to asset_preview or acquire_asset.", False, "service.sourcing.search_assets", AssetSearchRequest, _call("search_assets")),
    Operation("list_asset_categories", "GET", "/projects/{project_id}/assets/categories", "List the category slugs a provider offers for an asset_type (Sketchfab offers models only), for filtering search_assets. Read-only.", False, "service.sourcing.list_asset_categories", AssetCategoryRequest, _call("list_asset_categories")),
    Operation("asset_preview", "GET", "/projects/{project_id}/assets/preview", "Fetch one search result's thumbnail as an image (JPEG, PNG or WebP, at most 2 MiB and 2048 px) so you can look at it before acquiring. Params: provider and source_asset_id (for Sketchfab, the 32-hex model uid). Read-only.", False, "service.sourcing.preview_asset", AssetPreviewRequest, _call("asset_preview")),
    Operation("acquire_asset", "POST", "/projects/{project_id}/assets/acquire", "Download one provider asset and register it as a project Asset carrying its licence and attribution. payload: provider, source_asset_id (from search_assets), expected_version 0, asset_type matching the search result, an optional asset_id (generated if omitted), and for Poly Haven resolution (1k, 2k or 4k), map and format. Poly Haven models are repacked as GLB; Sketchfab needs the configured API token and refuses any licence but CC0-1.0 or CC-BY-4.0. Returns the Asset record.", True, "service.sourcing.acquire_asset", AssetAcquireRequest, _call("acquire_asset")),
    Operation("asset_browser", "GET", "/projects/{project_id}/assets/browser", "Render an HTML page for a person: provider search results with thumbnails and licences, plus the project's registered assets and credits. Takes the search_assets params, but q is optional; without it the page shows only the saved assets. Use search_assets for structured results.", False, "service.sourcing.search_assets", AssetBrowserRequest, _call("asset_browser")),
    Operation("create_asset", "POST", "/projects/{project_id}/assets", "Register a project-local asset file", True, "service.assets.register_asset", AssetRegistration, _call("create_asset"), upload=True),
    Operation("build_scene", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/scene", "Build the shot's Blender scene from its Shot, CameraProfile and Location, without rendering. payload {} (the shot comes from the path). The shot needs a positive duration_sec and a resolvable camera_profile; a draft shot moves to blocking on success (a rebuild of a shot already building stays building). Run this before render_preview, render_master and list_episode_credits.", True, "scene builder adapter", OpenRequest, _call("build_scene")),
    Operation("render_preview", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/preview", "Render a quick framing preview (Blender Workbench, flat grey) from the built scene, so framing can be checked before the full render. payload {}. Requires a build_scene output for the current shot version and shot status blocking; if registered assets changed since the build, run build_scene again.", True, "preview renderer adapter", OpenRequest, _call("render_preview")),
    Operation("render_master", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/master", "Render the shot's full master with Blender from the built scene. payload {}. Requires shot status blocking (or building, or failed for a retry), a build_scene output that still matches the shot's Location version, and framing approved or not required; moves the shot to building. A shot already in review needs a revise_shot successor and a new build before it can be rendered again. Follow it with degrade_media.", True, "master renderer adapter", OpenRequest, _call("render_master")),
    Operation("generate_media", "POST", "/projects/{project_id}/media/generate", "Generate still media. Omitting request.models uses the pinned default profile (Z-Image Base + RealGen V2 at 0.6); profile may be z_image_realgen, z_image, or flux2_klein (FLUX.2 klein 4B), and request.prompt is required.", True, "media generation adapter", OpenRequest, _call("generate_media")),
    Operation("degrade_media", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/degrade", "Apply the CCTV degradation (an FFmpeg PostProfile composed from the shot and its CameraProfile) to the shot's latest master. payload {}; the request is rebuilt from stored state. Requires a render_master output for the current shot version, shot status building, and, for profiles with a timestamp overlay such as the default dvr-2005, a font at assets/fonts/overlay.ttf (projects are created without one). Success moves the shot to review.", True, "PostProfile adapter", OpenRequest, _call("degrade_media")),
    Operation("sample_references", "POST", "/projects/{project_id}/references/sample", "Not implemented yet: always answers 501 missing_adapter. Do not call it.", True, "reference sampling adapter", OpenRequest, _call("sample_references")),
    Operation("generate_h3_candidate", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/h3-candidates", "Generate an opt-in MiniMax H3 audiovisual candidate", True, "service.minimax_h3", H3GenerateBody, _call("generate_h3_candidate")),
    Operation("list_h3_candidates", "GET", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/h3-candidates", "List immutable H3 candidate receipts", False, "store", None, _call("list_h3_candidates")),
    Operation("promote_h3_candidate", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/h3-candidates/{candidate_id}/promote", "Promote an approved H3 candidate to a production output", True, "service.minimax_h3", H3PromoteBody, _call("promote_h3_candidate")),
    Operation("cancel_h3_candidate", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/h3-candidates/{job_id}/cancel", "Cancel an H3 candidate job", True, "jobs.queue", H3CancelBody, _call("cancel_h3_candidate")),
    Operation("inspect_h3", "GET", "/models/minimax-h3", "Inspect configured MiniMax H3 runtime", False, "service.minimax_h3", None, _call("inspect_h3")),
    Operation("inspect_tts", "GET", "/models/tts", "Inspect configured text-to-speech runtimes (Piper, IndexTTS-2) and the Piper voice catalog", False, "service.tts", None, _call("inspect_tts")),
    Operation("generate_piper_speech", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/piper", "Synthesise a processed-source voice take (PA, answering machine, broadcast) with Piper on the CPU", True, "service.tts", PiperSpeechBody, _call("generate_piper_speech")),
    Operation("generate_indextts2_speech", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/indextts2", "Synthesise a diegetic voice take with IndexTTS-2 on the GPU, cloning a consented speaker reference and steering emotion", True, "service.tts", IndexTts2SpeechBody, _call("generate_indextts2_speech")),
    Operation("apply_audio_post_profile", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/post", "Degrade a registered voice take with an AudioPostProfile on the CPU; the result is a new voice Asset with the source take's rights", True, "service.audiopost", AudioPostApplyBody, _call("apply_audio_post_profile")),
    Operation("inspect_take_pauses", "GET", "/projects/{project_id}/assets/{asset_id}/pauses", "Find the pauses in a registered voice take (read-only, model-free), so an agent can write a performance edit", False, "service.perfedit", PauseDetection, _call("inspect_take_pauses")),
    Operation("apply_performance_edit", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/edit", "Widen a take's own pauses, add procedural breaths, a gain contour and a false start on the CPU, leaving speech samples bit-identical; the result is a new voice Asset with the source take's rights", True, "service.perfedit", PerformanceEditBody, _call("apply_performance_edit")),
    Operation("report_jobs", "GET", "/projects/{project_id}/jobs", "List every job record in the project, sorted by job_id, with its state (no filtering). Poll this after an asynchronous tool answers 202 with a job_id.", False, "jobs.queue", None, _call("report_jobs")),
    Operation("apply_typed_feedback", "POST", "/projects/{project_id}/feedback", "Not implemented yet: always answers 501 missing_adapter. Do not call it; revise the script, shot or location directly instead.", True, "service.apply_typed_feedback", OpenRequest, _call("apply_typed_feedback")),
    Operation("score_rubric", "POST", "/projects/{project_id}/rubric/score", "Score one sample against the five yes/no realism criteria and report whether it passes; nothing is stored. payload {scores: {mount_inferable, plausible_place, no_unmotivated_movement, stable_light_and_geometry, integrated_anomaly}}, all booleans. passed is true when at least 4 of 5 hold and no_unmotivated_movement is one of them.", True, "domain.entities.RubricScores", RubricRequest, _call("score_rubric")),
    Operation("export", "POST", "/projects/{project_id}/export", "Export an approved episode cut and its portable project source package: delivery.mp4 plus a project snapshot under exports/{export_id}. payload {episode_id, cut_output_id, expected_version}: cut_output_id is a cut in the Episode's outputs and expected_version is the Episode's mutation token. The cut must match the current Timeline and the episode must carry its cut approval.", True, "adapters.export.package", ProjectExportRequest, _call("export")),
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
    "generate_piper_speech",
    "generate_indextts2_speech",
    "apply_audio_post_profile",
    "apply_performance_edit",
})
