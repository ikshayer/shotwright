"""Adapter routes are present; wired jobs return 202 and absent adapters return 501."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.adapters.blender.scene import build_scene_plan
from orchestrator.api.routes import OPERATIONS as SHARED_OPERATIONS
from orchestrator.domain.entities import (
    Asset,
    CameraProfile,
    Episode,
    PostProfile,
    PostProfileEncode,
    PostProfileOverlay,
    Project,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.service.compose import compose_render_request
from orchestrator.store.json_store import ProjectStore

from .asset_requests import create_asset_request_kwargs
from .conftest import TOKEN, TOKEN_HEADER, find_key, operations, valid_request_kwargs
from .test_parity import _cli, _command_names, _spellings

# Labels map to exact operation_ids from the shared API/CLI registry.
ADAPTER_OPERATIONS = {
    "create asset": "create_asset",
    "build scene": "build_scene",
    "render preview": "render_preview",
    "render master": "render_master",
    "generate media": "generate_media",
    "degrade": "degrade_media",
    "sample references": "sample_references",
    "export": "export",
}


def _matches(app: FastAPI, operation_name: str):
    declared = next((item for item in SHARED_OPERATIONS if item.name == operation_name), None)
    assert declared is not None, f"unknown shared operation_id {operation_name!r}"
    return [
        operation for operation in operations(app)
        if operation.name == declared.name and operation.path == declared.path
    ]


def _seed_wired_route_records(app: FastAPI, *, needs_degrade: bool = False) -> None:
    """The route-table harness substitutes `x` for every path parameter."""
    root = ProjectRoot(app.state.settings.project_root)
    root.make_dir("x")
    project_root = ProjectRoot(root.resolve("x"))
    store = ProjectStore(project_root)
    store.save(Project(project_id="x", title="Route test"), expected_version=0)
    store.save(
        Episode(episode_id="x", project_id="x", number=1, duration_target_sec=10),
        expected_version=0,
    )
    shot = Shot(shot_id="x", episode_id="x", duration_sec=2)
    # Composition resolves the CameraProfile the Shot names, so a Shot without one is an
    # incomplete fixture rather than a routing failure: /scene and /preview answer
    # 404 "Shot 'x' references missing CameraProfile 'dvr-2005'" without it.
    # CameraProfile is a VersionedEntity, so it is created rather than saved; the store
    # refuses save() with "CameraProfile is versioned; use create/update/revise".
    store.create(
        CameraProfile(camera_id=shot.camera_profile, profile_name=shot.camera_profile)
    )
    store.create(shot)

    # Rendering now consumes a scene, so /preview refuses a shot that has none with
    # "has no scene output; run build_scene first". Seed what build_scene would have
    # written, through the real producer rather than a hand-written plan: the consumer
    # checks the plan's shot identity, and the store checks the artifact's sha256.
    scene_rel = f"scenes/{shot.shot_id}/v{shot.version:04d}/seed/scene.json"
    plan = build_scene_plan(
        compose_render_request(
            store, shot_id=shot.shot_id, episode_id=shot.episode_id,
            run_id="seed", mode="preview",
        )
    )
    artifact = json.dumps(
        {"schema_version": 1, "plan": plan},
        ensure_ascii=False,
        sort_keys=True,
    )
    project_root.make_dir(f"scenes/{shot.shot_id}/v{shot.version:04d}/seed")
    project_root.write_text(scene_rel, artifact)
    store.append_shot_output(
        ShotOutput(
            output_id="seed-scene",
            episode_id=shot.episode_id,
            shot_id=shot.shot_id,
            record_version=shot.version,
            kind=ShotOutputKind.SCENE,
            relative_path=scene_rel,
            content_sha256=hashlib.sha256(artifact.encode("utf-8")).hexdigest(),
        ),
        expected_version=0,
    )

    if needs_degrade:
        # degrade_media composes from the current master receipt and the named
        # project profile. The overlay font is profile-owned and must be persisted
        # as a project-relative path with a real fixture file.
        font_relative = "assets/fonts/route-overlay.ttf"
        project_root.write_bytes(font_relative, b"route fixture font")
        store.save(
            PostProfile(
                name="dvr-2005",
                semver="1.0.0",
                output_fps=12,
                storage_resolution=(640, 480),
                sample_aspect_ratio="1/1",
                overlay=PostProfileOverlay(
                    camera_id="CAM 03",
                    clock_epoch=1131984000,
                    fontsize=16,
                    font_relative=font_relative,
                ),
                encode=PostProfileEncode(
                    codec="libx264", preset="medium", crf=30,
                    pix_fmt="yuv420p", threads=1,
                ),
                provenance="API route fixture",
            ),
            expected_version=0,
        )
        master_relative = f"renders/{shot.shot_id}/v{shot.version:04d}/seed/master.mkv"
        master_bytes = b"route fixture master"
        project_root.write_bytes(master_relative, master_bytes)
        store.append_shot_output(
            ShotOutput(
                output_id="seed-master",
                episode_id=shot.episode_id,
                shot_id=shot.shot_id,
                record_version=shot.version,
                kind=ShotOutputKind.MASTER,
                relative_path=master_relative,
                content_sha256=hashlib.sha256(master_bytes).hexdigest(),
                engine_id="BLENDER_EEVEE",
                engine_build="fixture-build",
            ),
            expected_version=1,
        )


def _wired_request_kwargs(app: FastAPI, op) -> dict:
    """Provide the shot binding for the project-only media generation route too."""
    kwargs = valid_request_kwargs(app, op)
    body = kwargs.get("json")
    if isinstance(body, dict):
        body.update(project_id="x", episode_id="x", shot_id="x")
        if op.path.endswith("/media/generate"):
            # CONTRACT A4.7: a request without models is composed from a pinned profile
            # and must carry a prompt; anything less is refused before submission.
            body["request"] = {"prompt": "contract-status probe"}
    return kwargs


WIRED = {"build scene", "render preview", "render master", "generate media", "degrade"}


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
    if operation in WIRED or operation == "create asset":
        _seed_wired_route_records(app, needs_degrade=operation == "degrade")
    for op in ops:
        if operation == "create asset":
            asset = Asset(
                asset_id="route-asset",
                # PNG bytes: declaring "model" is correctly refused, so this
                # exercises a declared type that agrees with the detected kind.
                type="texture",
                generated=False,
                source_url="https://assets.example.invalid/route.png",
                provider="fixture-provider",
                license="CC0",
            )
            request_kwargs = create_asset_request_kwargs(
                app,
                method=op.method,
                path=op.path,
                asset=asset.model_dump(mode="json"),
                filename="route.png",
                content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
            )
        else:
            request_kwargs = (
                _wired_request_kwargs(app, op) if operation in WIRED
                else valid_request_kwargs(app, op)
            )
        resp = client.request(
            op.method, op.url(), headers={TOKEN_HEADER: TOKEN},
            **request_kwargs,
        )
        if operation == "create asset":
            assert resp.status_code in {200, 201}, (
                f"{op.key} did not register a valid PNG: {resp.status_code} {resp.text}"
            )
            project_dir = ProjectRoot(app.state.settings.project_root).resolve("x")
            asset_store = ProjectStore(ProjectRoot(project_dir))
            registered = asset_store.load(Asset, "route-asset")
            assert registered.license == "CC0", (
                f"create_asset lost the supplied license; observed {registered.license!r}"
            )
            assert registered.source_url == "https://assets.example.invalid/route.png", (
                f"create_asset lost the supplied source_url; observed {registered.source_url!r}"
            )
        elif operation == "export":
            assert resp.status_code == 404, f"{op.key} answered {resp.status_code}: {resp.text}"
        elif operation in WIRED:
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
    """Every submission validates synchronously before issuing a job id."""
    for operation in sorted(WIRED):
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
    for label in sorted(WIRED):
        ops = _matches(app, ADAPTER_OPERATIONS[label])
        assert ops, f"no route for contract operation {label!r}"
        for op in ops:
            assert _spellings(op) & flat_cli_names, (
                f"{op.key} ({op.name}) has no matching CLI command"
            )
