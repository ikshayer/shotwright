"""The shared, typed operation registry for HTTP, CLI, and agent tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from orchestrator.domain.entities import Episode, Project, RubricScores, Script, Shot


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


class ScriptDraft(BaseModel):
    script: Script


class ShotProposal(BaseModel):
    shots: list[Shot]


class RubricRequest(BaseModel):
    scores: RubricScores


#: Leading bytes carried for magic-byte detection; enough for every signature
#: platform.containment recognises.
MAGIC_HEADER_BYTES = 64


@dataclass(frozen=True)
class UploadedFile:
    """What upload validation needs about one uploaded file.

    The file's bytes are deliberately NOT carried. Validation needs only the magic-byte
    header and the true size, so buffering whole files would let a caller decide how
    much memory this process uses — multiplied by the number of parts in the request —
    and buy nothing while the storing adapter is absent. The adapter, when it exists,
    must stream the body to a contained path rather than take bytes from here.
    """

    filename: str
    #: The leading bytes, enough for magic-byte detection.
    head: bytes
    #: The real size as the multipart parser measured it, not the number of bytes read.
    size_bytes: int


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
    Operation("create_project", "POST", "/projects", "Create a project", True, "store", Project, _call("create_project")),
    Operation("create_episode", "POST", "/projects/{project_id}/episodes", "Create an episode", True, "service.dependencies.create_episode", EpisodeCreation, _call("create_episode")),
    Operation("list_projects", "GET", "/projects", "List projects", False, "store", None, _call("list_projects")),
    Operation("load_project", "GET", "/projects/{project_id}", "Load a project", False, "store", None, _call("load_project")),
    Operation("draft_script", "POST", "/projects/{project_id}/episodes/{episode_id}/scripts/draft", "Persist an agent-authored script", True, "service.dependencies.draft_script", ScriptDraft, _call("draft_script")),
    Operation("revise_script", "POST", "/projects/{project_id}/episodes/{episode_id}/scripts/revise", "Revise a script", True, "service.dependencies.revise_script", ScriptRevision, _call("revise_script")),
    Operation("propose_shots", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/propose", "Persist agent-authored shots", True, "service.dependencies.propose_shots", ShotProposal, _call("propose_shots")),
    Operation("revise_shot", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/revise", "Revise a shot", True, "service.dependencies.revise_shot", ShotRevision, _call("revise_shot")),
    Operation("search_assets", "GET", "/projects/{project_id}/assets/search", "Search assets", False, "asset search adapter", OpenRequest, _call("search_assets")),
    Operation("create_asset", "POST", "/projects/{project_id}/assets", "Create an asset", True, "asset creation adapter", None, _call("create_asset"), upload=True),
    Operation("build_scene", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/scene", "Build a scene", True, "scene builder adapter", OpenRequest, _call("build_scene")),
    Operation("render_preview", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/preview", "Render a preview", True, "preview renderer adapter", OpenRequest, _call("render_preview")),
    Operation("generate_media", "POST", "/projects/{project_id}/media/generate", "Generate media", True, "media generation adapter", OpenRequest, _call("generate_media")),
    Operation("degrade_media", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/degrade", "Degrade a shot master", True, "PostProfile adapter", OpenRequest, _call("degrade_media")),
    Operation("sample_references", "POST", "/projects/{project_id}/references/sample", "Sample references", True, "reference sampling adapter", OpenRequest, _call("sample_references")),
    Operation("report_jobs", "GET", "/projects/{project_id}/jobs", "Report jobs", False, "jobs.queue", None, _call("report_jobs")),
    Operation("apply_typed_feedback", "POST", "/projects/{project_id}/feedback", "Apply typed feedback", True, "service.apply_typed_feedback", OpenRequest, _call("apply_typed_feedback")),
    Operation("score_rubric", "POST", "/projects/{project_id}/rubric/score", "Score rubric criteria", True, "domain.entities.RubricScores", RubricRequest, _call("score_rubric")),
    Operation("export", "POST", "/projects/{project_id}/export", "Export a project", True, "export adapter", OpenRequest, _call("export")),
)
