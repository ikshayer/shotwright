"""Shared operation registry for the HTTP API, CLI, OpenAPI, and tool manifest."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str
    summary: str
    mutating: bool
    adapter: str
    invoke: Callable[[Any, dict[str, Any]], Any]


def _call(name: str) -> Callable[[Any, dict[str, Any]], Any]:
    def invoke(context: Any, payload: dict[str, Any]) -> Any:
        return context.invoke(name, payload)

    return invoke


# Paths, CLI names, and tool definitions are all derived from this table.
OPERATIONS = (
    Operation("create_project", "POST", "/projects", "Create a project", True, "store", _call("create_project")),
    Operation("list_projects", "GET", "/projects", "List projects", False, "store", _call("list_projects")),
    Operation("load_project", "GET", "/projects/{project_id}", "Load a project", False, "store", _call("load_project")),
    Operation("draft_script", "POST", "/projects/{project_id}/episodes/{episode_id}/scripts/draft", "Draft a script", True, "service.dependencies.revise_script", _call("draft_script")),
    Operation("revise_script", "POST", "/projects/{project_id}/episodes/{episode_id}/scripts/revise", "Revise a script", True, "service.dependencies.revise_script", _call("revise_script")),
    Operation("propose_shots", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/propose", "Propose shots", True, "service", _call("propose_shots")),
    Operation("revise_shot", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/revise", "Revise a shot", True, "service.dependencies.revise_shot", _call("revise_shot")),
    Operation("search_assets", "GET", "/projects/{project_id}/assets/search", "Search assets", False, "asset search adapter", _call("search_assets")),
    Operation("create_asset", "POST", "/projects/{project_id}/assets", "Create an asset", True, "asset creation adapter", _call("create_asset")),
    Operation("build_scene", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/scene", "Build a scene", True, "scene builder adapter", _call("build_scene")),
    Operation("render_preview", "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/preview", "Render a preview", True, "preview renderer adapter", _call("render_preview")),
    Operation("generate_media", "POST", "/projects/{project_id}/media/generate", "Generate media", True, "media generation adapter", _call("generate_media")),
    Operation("sample_references", "POST", "/projects/{project_id}/references/sample", "Sample references", True, "reference sampling adapter", _call("sample_references")),
    Operation("report_jobs", "GET", "/projects/{project_id}/jobs", "Report jobs", False, "jobs.queue", _call("report_jobs")),
    Operation("apply_typed_feedback", "POST", "/projects/{project_id}/feedback", "Apply typed feedback", True, "service typed feedback adapter", _call("apply_typed_feedback")),
    Operation("score_rubric", "POST", "/projects/{project_id}/rubric/score", "Score rubric criteria", True, "domain.entities.RubricScores", _call("score_rubric")),
    Operation("export", "POST", "/projects/{project_id}/export", "Export a project", True, "export adapter", _call("export")),
)


def tool_manifest() -> list[dict[str, Any]]:
    """Build agent tool definitions from the same registry as the HTTP routes."""
    return [
        {
            "name": item.name,
            "description": item.summary,
            "input_schema": {
                "type": "object",
                "properties": {key: {"type": "string"} for key in _path_parameters(item.path)},
                "additionalProperties": True,
            },
            "http": {"method": item.method, "path": item.path},
        }
        for item in OPERATIONS
    ]


def _path_parameters(path: str) -> list[str]:
    return [part[1:-1] for part in path.split("/") if part.startswith("{") and part.endswith("}")]
