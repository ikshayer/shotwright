"""Creation path coverage from the shared HTTP operation table.

The entry operations persist caller-authored domain entities. These tests exercise the
real API dispatcher and JSON store; no adapter or external process is involved.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.domain.entities import Episode, Project, Script, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.store.json_store import ProjectStore

from .conftest import TOKEN, TOKEN_HEADER, Op, operations, valid_request_kwargs
from .test_parity import _cli

ENTRY_NAMES = {"create_episode", "draft_script", "propose_shots"}
DELIBERATE_501 = {
    "search_assets",
    "create_asset",
    "sample_references",
    "export",
    "apply_typed_feedback",
}


def _entries(app: FastAPI) -> dict[str, Op]:
    found = {op.name: op for op in operations(app) if op.name in ENTRY_NAMES}
    assert set(found) == ENTRY_NAMES, f"entry operations missing from route table: {ENTRY_NAMES - set(found)}"
    return found


def _entry_op(app: FastAPI, name: str) -> Op:
    found = {op.name: op for op in operations(app) if op.name in ENTRY_NAMES}
    assert name in found, f"{name} is missing from the route table"
    return found[name]


def _path(op: Op, **ids: str) -> str:
    path = op.path
    for param in op.route.dependant.path_params:
        path = path.replace("{" + param.name + "}", ids.get(param.name, "x"))
    return path


def _project_store(root: Path, project_id: str = "p1", *, episode: bool = True) -> ProjectStore:
    project_root = ProjectRoot(root)
    project_root.make_dir(project_id)
    store = ProjectStore(ProjectRoot(project_root.resolve(project_id)))
    store.save(Project(project_id=project_id, title="Entry test"), expected_version=0)
    if episode:
        store.save(Episode(
            episode_id="ep1", project_id=project_id, number=1,
            duration_target_sec=30,
        ), expected_version=0)
    return store


def _episode(eid: str = "ep1", pid: str = "p1", *, number: int = 1) -> dict:
    return {
        "episode_id": eid,
        "project_id": pid,
        "number": number,
        "duration_target_sec": 30,
    }


def _script(sid: str = "sc1", eid: str = "ep1", *, duplicate_beats: bool = False) -> dict:
    beats = [
        {"beat_id": "b1", "target_sec": 2, "subtlety": 2},
        {"beat_id": "b1" if duplicate_beats else "b2", "target_sec": 4, "subtlety": 3},
    ]
    return {"script_id": sid, "episode_id": eid, "logline": "A quiet room changes.", "beats": beats}


def _shot(sid: str = "s1", eid: str = "ep1", *, invalid_event: bool = False) -> dict:
    return {
        "shot_id": sid,
        "episode_id": eid,
        "duration_sec": 5,
        "event_sec": 6 if invalid_event else 2,
    }


def _body_for(name: str) -> dict:
    if name == "create_episode":
        return {"episode": _episode()}
    if name == "draft_script":
        return {"script": _script()}
    if name == "propose_shots":
        return {"shots": [_shot()]}
    raise AssertionError(name)


def _read_entry(store: ProjectStore, name: str, body: dict):
    if name == "create_episode":
        return store.load(Episode, body["episode"]["episode_id"])
    if name == "draft_script":
        return store.load(Script, body["script"]["script_id"], episode_id=body["script"]["episode_id"])
    if name == "propose_shots":
        shots = body["shots"]
        return [store.load(Shot, item["shot_id"], episode_id=item["episode_id"]) for item in shots]
    raise AssertionError(name)


def _post(client: TestClient, op: Op, body: dict, **ids: str):
    return client.post(
        _path(op, **ids), json=body, headers={TOKEN_HEADER: TOKEN},
    )


def test_every_route_is_implemented_or_deliberately_501(
    client: TestClient, app: FastAPI
) -> None:
    """Exercise every registered operation; only the contract's five remain 501.

    The cases come from ``operations(app)``. The fixed set names only deliberate
    absences; a newly registered operation is automatically requested and fails if it
    falls through to MissingAdapter/501.
    """
    _project_store(app.state.settings.project_root, "x")
    found = operations(app)
    assert found, "route table is empty"
    assert ENTRY_NAMES <= {op.name for op in found}
    seen_501: set[str] = set()

    for op in found:
        kwargs = valid_request_kwargs(app, op)
        if op.name in ENTRY_NAMES:
            kwargs["json"] = _body_for(op.name)
            body = kwargs["json"]
            if op.name == "create_episode":
                body["episode"]["project_id"] = "x"
            elif op.name == "draft_script":
                body["script"]["episode_id"] = "ep1"
            else:
                for shot in body["shots"]:
                    shot["episode_id"] = "ep1"
        elif op.name in {"build_scene", "render_preview", "generate_media", "degrade_media"}:
            body = kwargs.get("json")
            if isinstance(body, dict):
                body.update(project_id="x", episode_id="ep1", shot_id="s1")
        response = client.request(
            op.method, _path(op, project_id="x", episode_id="ep1", shot_id="s1"),
            headers={TOKEN_HEADER: TOKEN}, **kwargs,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        code = payload.get("error") if isinstance(payload, dict) else None
        if response.status_code == 501 or code == "missing_adapter":
            seen_501.add(op.name)
            assert op.name in DELIBERATE_501, (
                f"{op.key} unexpectedly fell through to missing_adapter: {response.text}"
            )
            assert response.status_code == 501 and code == "missing_adapter", response.text
        else:
            assert op.name not in DELIBERATE_501, (
                f"{op.key} is contractually absent but answered {response.status_code}: {response.text}"
            )

    assert seen_501 == DELIBERATE_501, f"deliberate 501 routes differ: {seen_501 ^ DELIBERATE_501}"


@pytest.mark.parametrize("name", sorted(ENTRY_NAMES))
def test_entry_operation_persists_entity_readable_from_store(
    client: TestClient, app: FastAPI, name: str
) -> None:
    store = _project_store(app.state.settings.project_root, episode=name != "create_episode")
    op = _entry_op(app, name)
    body = _body_for(name)
    if name == "create_episode":
        body = {"episode": _episode("new-ep")}
    response = _post(client, op, body, project_id="p1", episode_id="ep1")
    assert response.status_code == 200, f"{op.key}: {response.status_code} {response.text}"
    loaded = _read_entry(store, name, body)
    records = loaded if isinstance(loaded, list) else [loaded]
    assert records
    if name == "create_episode":
        _, mutation_token = store.load_with_version(Episode, body["episode"]["episode_id"])
        # Episode is unversioned in entities.py; this is its first persisted store token.
        assert mutation_token == 1
    else:
        assert all(record.version == 1 for record in records)
    if isinstance(loaded, list):
        assert [record.shot_id for record in loaded] == [item["shot_id"] for item in body["shots"]]


@pytest.mark.parametrize("name", sorted(ENTRY_NAMES))
def test_entry_creation_conflicts_without_overwriting_original(
    client: TestClient, app: FastAPI, name: str
) -> None:
    store = _project_store(app.state.settings.project_root, episode=name != "create_episode")
    op = _entry_op(app, name)
    body = _body_for(name)
    if name == "create_episode":
        body = {"episode": _episode("new-ep")}
    first = _post(client, op, body, project_id="p1", episode_id="ep1")
    assert first.status_code == 200, first.text
    original = _read_entry(store, name, body)
    changed = json.loads(json.dumps(body))
    entity_key = {"create_episode": "episode", "draft_script": "script"}.get(name)
    if entity_key:
        changed[entity_key]["number" if name == "create_episode" else "logline"] = 9 if name == "create_episode" else "overwrite"
    else:
        changed["shots"][0]["duration_sec"] = 99
    second = _post(client, op, changed, project_id="p1", episode_id="ep1")
    assert second.status_code == 409, f"{op.key}: {second.status_code} {second.text}"
    assert second.json().get("expected") == 0, second.text
    after = _read_entry(store, name, body)
    if isinstance(original, list):
        assert [item.model_dump(mode="json") for item in after] == [item.model_dump(mode="json") for item in original]
    else:
        assert after.model_dump(mode="json") == original.model_dump(mode="json")


@pytest.mark.parametrize("name", ["create_episode", "draft_script", "propose_shots"])
def test_entry_rejects_url_body_identifier_mismatch(
    client: TestClient, app: FastAPI, name: str
) -> None:
    _project_store(app.state.settings.project_root, episode=name != "create_episode")
    op = _entry_op(app, name)
    if name == "create_episode":
        body = {"episode": _episode("ep2", pid="p2")}
    elif name == "draft_script":
        body = {"script": _script(eid="ep2")}
    else:
        body = {"shots": [_shot(eid="ep2")]}
    response = _post(client, op, body, project_id="p1", episode_id="ep1")
    assert response.status_code == 400, response.text
    assert response.json().get("error") == "request_mismatch", response.text


@pytest.mark.parametrize("name", sorted(ENTRY_NAMES))
def test_entry_payload_uses_domain_entity_validation(
    client: TestClient, app: FastAPI, name: str
) -> None:
    _project_store(app.state.settings.project_root, episode=name != "create_episode")
    op = _entry_op(app, name)
    if name == "create_episode":
        body = {"episode": _episode("bad-ep", number=0)}
        message = "greater than or equal to 1"
    elif name == "draft_script":
        body = {"script": _script(duplicate_beats=True)}
        message = "beat_id must be unique within a Script"
    else:
        body = {"shots": [_shot(invalid_event=True)]}
        message = "falls outside duration_sec"
    response = _post(client, op, body, project_id="p1", episode_id="ep1")
    assert response.status_code == 422, response.text
    assert response.json().get("error") == "validation_error", response.text
    assert message in response.json().get("message", ""), response.text


def test_propose_shots_persists_all_and_appends_on_later_proposal(
    client: TestClient, app: FastAPI
) -> None:
    store = _project_store(app.state.settings.project_root)
    op = _entry_op(app, "propose_shots")
    first_body = {
        "shots": [
            _shot("s1") | {
                "camera_profile": "low-light-bw",
                "post_profile": "low-light-bw",
                "scene_look": "low_key",
            },
            _shot("s2"),
        ]
    }
    first = _post(client, op, first_body, project_id="p1", episode_id="ep1")
    assert first.status_code == 200, first.text
    selected = store.load(Shot, "s1", episode_id="ep1")
    assert (selected.camera_profile, selected.post_profile, selected.scene_look) == (
        "low-light-bw", "low-light-bw", "low_key",
    )
    unset = store.load(Shot, "s2", episode_id="ep1")
    assert unset.post_profile is None and unset.scene_look is None
    second_body = {"shots": [_shot("s3"), _shot("s4")]}
    second = _post(client, op, second_body, project_id="p1", episode_id="ep1")
    assert second.status_code == 200, second.text
    assert {sid for sid in ("s1", "s2", "s3", "s4")
            if store.load(Shot, sid, episode_id="ep1").version == 1} == {"s1", "s2", "s3", "s4"}


@pytest.mark.parametrize("name", sorted(ENTRY_NAMES))
def test_creation_holds_project_command_and_does_not_run_invalidation(
    client: TestClient, app: FastAPI, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_store(app.state.settings.project_root, episode=name != "create_episode")
    op = _entry_op(app, name)
    body = _body_for(name)
    if name == "create_episode":
        body = {"episode": _episode("new-ep")}

    active = threading.local()
    original_command = ProjectStore.command
    original_create = ProjectStore.create
    original_save = ProjectStore.save
    calls = {"command": 0, "create": 0, "cascade": 0}

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
        calls["create"] += 1
        assert getattr(active, "held", False), "creation store write ran outside ProjectStore.command()"
        return original_create(self, entity)

    def tracked_save(self, entity, *, expected_version):
        calls["create"] += 1
        assert getattr(active, "held", False), "creation store write ran outside ProjectStore.command()"
        return original_save(self, entity, expected_version=expected_version)

    def tracked_cascade(self, plan):
        calls["cascade"] += 1
        raise AssertionError(f"new-record creation must not persist invalidation cascade: {plan}")

    monkeypatch.setattr(ProjectStore, "command", tracked_command)
    monkeypatch.setattr(ProjectStore, "create", tracked_create)
    monkeypatch.setattr(ProjectStore, "save", tracked_save)
    monkeypatch.setattr(ProjectStore, "record_cascade", tracked_cascade)
    response = _post(client, op, body, project_id="p1", episode_id="ep1")
    assert response.status_code == 200, f"{op.key}: {response.status_code} {response.text}"
    assert calls["command"] >= 1
    assert calls["create"] >= (len(body["shots"]) if name == "propose_shots" else 1)
    assert calls["cascade"] == 0


def test_entry_api_and_cli_return_matching_results_from_route_table(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    """Both surfaces execute each of the three registered entry operations."""
    from click.testing import CliRunner

    entries = _entries(app)
    cli_root = tmp_path / "cli-projects"
    cli_root.mkdir()
    _project_store(app.state.settings.project_root, "p1", episode=False)
    _project_store(cli_root, "p1", episode=False)
    for name in ("create_episode", "draft_script", "propose_shots"):
        op = entries[name]
        body = _body_for(name)
        if name == "create_episode":
            body = {"episode": _episode()}
        api_response = _post(client, op, body, project_id="p1", episode_id="ep1")
        assert api_response.status_code == 200, f"API {op.key}: {api_response.text}"

        args = ["--project-root", str(cli_root), name, "--data", json.dumps(body), "--json"]
        for param in op.route.dependant.path_params:
            args.extend(["--" + param.name.replace("_", "-"), {"project_id": "p1", "episode_id": "ep1"}.get(param.name, "x")])
        result = CliRunner().invoke(_cli(), args)
        assert result.exit_code == 0, f"CLI {op.key}: {result.stderr or result.output}"
        assert json.loads(result.stdout) == api_response.json()

