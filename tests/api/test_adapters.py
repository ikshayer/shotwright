"""Adapter routes are present; wired jobs return 202 and absent adapters return 501."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.domain.entities import Episode, Project, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.store.json_store import ProjectStore

from .conftest import TOKEN, TOKEN_HEADER, find_key, operations, valid_request_kwargs
from .test_parity import _cli, _command_names, _spellings

# One pattern per adapter operation, matched against route path + function name.
ADAPTER_OPERATIONS = {
    "search assets": r"search.*asset|asset.*search",
    "create asset": r"(create|add|new).*asset|asset.*(create|add|new)",
    "build scene": r"scene",
    "render preview": r"preview|render",
    "generate media": r"generat",
    "degrade": r"degrad|post.?profile",
    "sample references": r"sample|reference",
    "export": r"export",
}


def _matches(app: FastAPI, pattern: str):
    rx = re.compile(pattern, re.IGNORECASE)
    return [o for o in operations(app) if rx.search(f"{o.path} {o.name}")]


def _seed_wired_route_records(app: FastAPI) -> None:
    """The route-table harness substitutes `x` for every path parameter."""
    root = ProjectRoot(app.state.settings.project_root)
    root.make_dir("x")
    store = ProjectStore(ProjectRoot(root.resolve("x")))
    store.save(Project(project_id="x", title="Route test"), expected_version=0)
    store.save(
        Episode(episode_id="x", project_id="x", number=1, duration_target_sec=10),
        expected_version=0,
    )
    store.create(Shot(shot_id="x", episode_id="x", duration_sec=2))


def _wired_request_kwargs(app: FastAPI, op) -> dict:
    """Provide the shot binding for the project-only media generation route too."""
    kwargs = valid_request_kwargs(app, op)
    body = kwargs.get("json")
    if isinstance(body, dict):
        body.update(project_id="x", episode_id="x", shot_id="x")
    return kwargs


@pytest.mark.parametrize("operation", list(ADAPTER_OPERATIONS))
def test_adapter_operation_is_routed_not_omitted(app: FastAPI, operation: str) -> None:
    assert _matches(app, ADAPTER_OPERATIONS[operation]), f"no route for contract operation {operation!r}"


@pytest.mark.parametrize("operation", list(ADAPTER_OPERATIONS))
def test_adapter_operation_returns_contract_status(
    client: TestClient, app: FastAPI, operation: str
) -> None:
    """The app fixture constructs the default app with only settings and auth token."""
    ops = _matches(app, ADAPTER_OPERATIONS[operation])
    assert ops, f"no route for contract operation {operation!r}"
    if operation in {"build scene", "render preview", "generate media", "degrade"}:
        _seed_wired_route_records(app)
    for op in ops:
        resp = client.request(
            op.method, op.url(), headers={TOKEN_HEADER: TOKEN},
            **(_wired_request_kwargs(app, op) if operation in {
                "build scene", "render preview", "generate media", "degrade"
            } else valid_request_kwargs(app, op))
        )
        if operation in {"build scene", "render preview", "generate media", "degrade"}:
            assert resp.status_code == 202, f"{op.key} answered {resp.status_code}: {resp.text}"
            job_ids = [v for v in find_key(resp.json(), lambda k: "job" in k.lower() and "id" in k.lower())
                       if isinstance(v, str) and v]
            assert job_ids, f"{op.key}: 202 response has no job id: {resp.text}"
            states = find_key(resp.json(), lambda k: k.lower() == "state")
            assert "queued" in states, f"{op.key}: 202 response is not queued: {resp.text}"
        else:
            assert resp.status_code == 501, f"{op.key} answered {resp.status_code}: {resp.text}"
            names = [v for v in find_key(resp.json(), lambda k: "adapter" in k.lower()) if isinstance(v, str) and v]
            assert names, f"{op.key}: 501 body does not name the missing adapter: {resp.text}"
        assert TOKEN not in resp.text


def test_all_wired_routes_return_404_for_missing_project(
    client: TestClient, app: FastAPI
) -> None:
    """All four submissions validate synchronously before issuing a job id."""
    wired = ("build scene", "render preview", "generate media", "degrade")
    for operation in wired:
        ops = _matches(app, ADAPTER_OPERATIONS[operation])
        assert ops, f"no route for contract operation {operation!r}"
        for op in ops:
            resp = client.request(
                op.method, op.url(), headers={TOKEN_HEADER: TOKEN},
                **_wired_request_kwargs(app, op)
            )
            assert resp.status_code == 404, f"{op.key} answered {resp.status_code}: {resp.text}"
            assert TOKEN not in resp.text


def test_newly_wired_routes_have_matching_cli_commands(app: FastAPI) -> None:
    """The wired operations stay on the route table's shared API/CLI surface."""
    cli_names = _command_names(_cli())
    flat_cli_names = cli_names | {name.replace(" ", "-") for name in cli_names}
    for label in ("build scene", "render preview", "generate media", "degrade"):
        ops = _matches(app, ADAPTER_OPERATIONS[label])
        assert ops, f"no route for contract operation {label!r}"
        for op in ops:
            assert _spellings(op) & flat_cli_names, (
                f"{op.key} ({op.name}) has no matching CLI command"
            )
