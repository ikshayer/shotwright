"""CameraProfile and PostProfile creation/composition coverage.

The composition contract names the operations but not their route templates,
request envelopes, or composition function signatures. These tests assume item
routes ending in a profile identifier, request envelopes named ``camera_profile``
and ``post_profile``, and direct composition functions
``compose_render_request(store, *, shot_id, episode_id, run_id, mode)`` and
``compose_post_profile_spec(store)``.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.adapters.blender.scene import RenderRequest
from orchestrator.adapters.postprofile.profile import EncodeSpec, OverlaySpec, PostProfileSpec
from orchestrator.domain.entities import (
    ArtifactScheduleEntry,
    CameraProfile,
    Episode,
    PostProfile,
    Project,
    Shot,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.store.json_store import ProjectStore

from .conftest import TOKEN, TOKEN_HEADER, Op, operations
from .test_parity import _cli

PROFILE_OPERATION_NAMES = {"create_camera_profile", "create_post_profile"}


def _profile_operations(app: FastAPI) -> dict[str, Op]:
    found = {op.name: op for op in operations(app) if op.name in PROFILE_OPERATION_NAMES}
    assert set(found) == PROFILE_OPERATION_NAMES, (
        f"profile creation operations missing from route table: {PROFILE_OPERATION_NAMES - set(found)}"
    )
    return found


def _path(op: Op, *, project_id: str, profile_id: str) -> str:
    path = op.path
    profile_params = []
    for parameter in op.route.dependant.path_params:
        if parameter.name == "project_id":
            value = project_id
        else:
            profile_params.append(parameter.name)
            value = profile_id
        path = path.replace("{" + parameter.name + "}", value)
    assert profile_params, (
        f"{op.key} has no URL profile identifier to compare with the body; "
        "the contract requires URL/body identifier matching"
    )
    return path


def _body(name: str, profile_id: str, *, invalid: bool = False) -> dict[str, Any]:
    if name == "create_camera_profile":
        profile: dict[str, Any] = {
            "camera_id": profile_id,
            "profile_name": "Test DVR profile",
        }
        if invalid:
            profile["field_of_view_mm"] = 0
        return {"camera_profile": profile}
    if name == "create_post_profile":
        return {
            "post_profile": {
                "name": profile_id,
                "semver": "not-semver" if invalid else "1.2.3",
                "stages": [],
            }
        }
    raise AssertionError(name)


def _profile_key(name: str, body: dict[str, Any]) -> str:
    envelope = "camera_profile" if name == "create_camera_profile" else "post_profile"
    field = "camera_id" if name == "create_camera_profile" else "name"
    return body[envelope][field]


def _store(root: Path, project_id: str = "p1") -> ProjectStore:
    projects = ProjectRoot(root)
    projects.make_dir(project_id)
    store = ProjectStore(ProjectRoot(projects.resolve(project_id)))
    store.save(Project(project_id=project_id, title="Profile test"), expected_version=0)
    return store


def _load_profile(store: ProjectStore, name: str, profile_id: str):
    if name == "create_camera_profile":
        return store.load(CameraProfile, profile_id)
    return store.load(PostProfile, profile_id)


def _post(client: TestClient, op: Op, body: dict[str, Any], *, profile_id: str) -> Any:
    return client.request(
        op.method,
        _path(op, project_id="p1", profile_id=profile_id),
        json=body,
        headers={TOKEN_HEADER: TOKEN},
    )


@pytest.mark.parametrize("name", sorted(PROFILE_OPERATION_NAMES))
def test_profile_creation_persists_a_readable_project_local_record(
    app: FastAPI, client: TestClient, name: str,
) -> None:
    store = _store(app.state.settings.project_root)
    op = _profile_operations(app)[name]
    body = _body(name, "profile-created")

    response = _post(client, op, body, profile_id="profile-created")

    assert response.status_code == 200, f"{op.key}: {response.status_code} {response.text}"
    created = _load_profile(store, name, _profile_key(name, body))
    assert created.model_dump(mode="json")
    if isinstance(created, CameraProfile):
        assert created.version == 1
    else:
        assert created.name == "profile-created"


@pytest.mark.parametrize("name", sorted(PROFILE_OPERATION_NAMES))
def test_profile_creation_conflicts_without_overwriting_the_original(
    app: FastAPI, client: TestClient, name: str,
) -> None:
    store = _store(app.state.settings.project_root)
    op = _profile_operations(app)[name]
    body = _body(name, "profile-once")
    first = _post(client, op, body, profile_id="profile-once")
    assert first.status_code == 200, first.text
    original = _load_profile(store, name, "profile-once")

    replacement = json.loads(json.dumps(body))
    envelope = "camera_profile" if name == "create_camera_profile" else "post_profile"
    if name == "create_camera_profile":
        replacement[envelope]["profile_name"] = "must not overwrite"
    else:
        replacement[envelope]["semver"] = "9.8.7"
    second = _post(client, op, replacement, profile_id="profile-once")

    assert second.status_code == 409, f"{op.key}: {second.status_code} {second.text}"
    assert second.json().get("expected") == 0, second.text
    after = _load_profile(store, name, "profile-once")
    assert after.model_dump(mode="json") == original.model_dump(mode="json")


@pytest.mark.parametrize("name", sorted(PROFILE_OPERATION_NAMES))
def test_profile_creation_rejects_url_body_identifier_mismatch(
    app: FastAPI, client: TestClient, name: str,
) -> None:
    _store(app.state.settings.project_root)
    op = _profile_operations(app)[name]
    body = _body(name, "body-profile")

    response = _post(client, op, body, profile_id="url-profile")

    assert response.status_code == 400, response.text
    assert response.json().get("error") == "request_mismatch", response.text


@pytest.mark.parametrize("name", sorted(PROFILE_OPERATION_NAMES))
def test_profile_creation_uses_the_domain_entity_validators(
    app: FastAPI, client: TestClient, name: str,
) -> None:
    _store(app.state.settings.project_root)
    op = _profile_operations(app)[name]
    body = _body(name, "invalid-profile", invalid=True)

    response = _post(client, op, body, profile_id="invalid-profile")

    assert response.status_code == 422, response.text
    assert response.json().get("error") == "validation_error", response.text
    message = response.json().get("message", "").lower()
    expected = "greater than 0" if name == "create_camera_profile" else "semver must be"
    assert expected in message, response.text


@pytest.mark.parametrize("name", sorted(PROFILE_OPERATION_NAMES))
def test_profile_creation_enters_the_command_boundary_without_a_cascade(
    app: FastAPI,
    client: TestClient,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(app.state.settings.project_root)
    op = _profile_operations(app)[name]
    body = _body(name, "profile-command")
    active = threading.local()
    original_command = ProjectStore.command
    original_create = ProjectStore.create
    original_save = ProjectStore.save
    calls = {"command": 0, "writes": 0, "cascade": 0}

    @contextmanager
    def tracked_command(self):
        calls["command"] += 1
        with original_command(self):
            active.held = True
            try:
                yield
            finally:
                active.held = False

    def tracked_create(self, entity):
        calls["writes"] += 1
        assert getattr(active, "held", False), "profile creation wrote outside ProjectStore.command()"
        return original_create(self, entity)

    def tracked_save(self, entity, *, expected_version):
        calls["writes"] += 1
        assert getattr(active, "held", False), "profile creation wrote outside ProjectStore.command()"
        return original_save(self, entity, expected_version=expected_version)

    def tracked_cascade(self, plan):
        calls["cascade"] += 1
        raise AssertionError(f"new profile creation must not persist invalidation cascade: {plan}")

    monkeypatch.setattr(ProjectStore, "command", tracked_command)
    monkeypatch.setattr(ProjectStore, "create", tracked_create)
    monkeypatch.setattr(ProjectStore, "save", tracked_save)
    monkeypatch.setattr(ProjectStore, "record_cascade", tracked_cascade)

    response = _post(client, op, body, profile_id="profile-command")

    assert response.status_code == 200, f"{op.key}: {response.status_code} {response.text}"
    assert calls["command"] >= 1
    assert calls["writes"] >= 1
    assert calls["cascade"] == 0


def test_profile_creation_api_and_cli_parity_is_derived_from_the_route_table(
    app: FastAPI,
    client: TestClient,
    tmp_path: Path,
) -> None:
    from click.testing import CliRunner

    entries = _profile_operations(app)
    cli_root = tmp_path / "cli-projects"
    cli_root.mkdir()
    _store(app.state.settings.project_root)
    _store(cli_root)

    for name, op in entries.items():
        body = _body(name, f"parity-{name}")
        profile_id = _profile_key(name, body)
        api_response = _post(client, op, body, profile_id=profile_id)
        assert api_response.status_code == 200, f"API {op.key}: {api_response.text}"

        args = [
            "--project-root", str(cli_root), name,
            "--data", json.dumps(body), "--json",
        ]
        for parameter in op.route.dependant.path_params:
            value = "p1" if parameter.name == "project_id" else profile_id
            args.extend(["--" + parameter.name.replace("_", "-"), value])
        result = CliRunner().invoke(_cli(), args)

        assert result.exit_code == 0, f"CLI {op.key}: {result.stderr or result.output}"
        assert json.loads(result.stdout) == api_response.json()


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    entries: list[tuple[str, str, bytes | None]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative, "directory", None))
        elif path.is_file():
            entries.append((relative, "file", path.read_bytes()))
    return tuple(sorted(entries))


def _json_documents(root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for path in root.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def test_fresh_project_materializes_default_profiles_on_disk_and_composes_without_authoring(
    app: FastAPI,
    client: TestClient,
) -> None:
    from orchestrator.service.compose import compose_post_profile_spec, compose_render_request

    project_id = "fresh-project"
    project = Project(project_id=project_id, title="Fresh project")
    create_project = next(op for op in operations(app) if op.name == "create_project")
    response = client.request(
        create_project.method,
        create_project.path,
        json=project.model_dump(mode="json"),
        headers={TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 200, response.text

    project_root = ProjectRoot(app.state.settings.project_root).resolve(project_id)
    store = ProjectStore(ProjectRoot(project_root))
    saved_project = store.load(Project)
    camera_profile = store.load(CameraProfile, "dvr-2005")
    post_profile = store.load(PostProfile, saved_project.output_profile)
    measured = json.loads(
        (Path(__file__).parents[2] / "spikes" / "01_postprofile_determinism" / "post_profile.json")
        .read_text(encoding="utf-8")
    )

    documents = _json_documents(project_root)
    assert any(document.get("camera_id") == camera_profile.camera_id for document in documents)
    assert any(document.get("name") == saved_project.output_profile for document in documents)
    assert camera_profile.camera_id == "dvr-2005"
    assert post_profile.name == saved_project.output_profile == "dvr-2005"
    assert post_profile.semver == measured["profile_version"]
    assert [stage.name for stage in post_profile.stages] == [stage["name"] for stage in measured["stages"]]
    for actual, source in zip(post_profile.stages, measured["stages"], strict=True):
        expected_params = dict(source.get("params", {}))
        if "from_schedule" in source:
            expected_params["from_schedule"] = source["from_schedule"]
        assert actual.filter == source["filter"]
        assert actual.params == expected_params
        assert actual.seed_source == ("shot_degrade_seed" if source.get("seeded") else None)

    store.save(
        Episode(episode_id="ep-default", project_id=project_id, number=1, duration_target_sec=30),
        expected_version=0,
    )
    shot = Shot(
        shot_id="shot-default",
        episode_id="ep-default",
        camera_profile=camera_profile.camera_id,
        duration_sec=5.625,
        degrade_seed=15062025,
    )
    store.create(shot)

    before = _tree_snapshot(project_root)
    request = compose_render_request(
        store, shot_id=shot.shot_id, episode_id=shot.episode_id,
        run_id="run-fresh-project", mode="master",
    )
    profile_spec = compose_post_profile_spec(store)
    after = _tree_snapshot(project_root)

    assert isinstance(request, RenderRequest)
    assert request.camera_profile == camera_profile
    assert request.duration_sec == 5.625
    assert request.degrade_seed == 15062025
    assert isinstance(profile_spec, PostProfileSpec)
    assert profile_spec.identity == post_profile
    assert profile_spec.output_fps == measured["output_fps"]
    assert profile_spec.storage_resolution == tuple(measured["storage_resolution"])
    assert profile_spec.sample_aspect_ratio == measured["sample_aspect_ratio"]
    assert profile_spec.artifact_schedule == tuple(
        ArtifactScheduleEntry.model_validate(item) for item in measured["artifact_schedule"]
    )
    overlay_stage = next(stage for stage in measured["stages"] if stage["filter"] == "drawtext")
    assert profile_spec.overlay == OverlaySpec(
        **overlay_stage["params"],
        placement="in_recording",
    )
    assert profile_spec.encode == EncodeSpec.model_validate(measured["encode"])
    assert after == before, "composition changed the project tree"

    # Select a second, project-local profile copied from the materialized default.
    # The identity assertion distinguishes using Project.output_profile from always
    # returning a hard-coded dvr-2005 identity.
    selected_profile = post_profile.model_copy(update={"name": "selected-profile"})
    store.save(selected_profile, expected_version=0)
    current_project, mutation_token = store.load_with_version(Project)
    store.save(
        current_project.model_copy(update={"output_profile": selected_profile.name}),
        expected_version=mutation_token,
    )
    before_selected = _tree_snapshot(project_root)
    selected_spec = compose_post_profile_spec(store)
    after_selected = _tree_snapshot(project_root)

    assert selected_spec.identity == selected_profile
    assert selected_spec.identity.name == "selected-profile"
    assert after_selected == before_selected, "PostProfile composition changed the project tree"
