"""Verify AT-18 with two short real master renders through the default GPU queue.

From the managed worktree, run the existing orchestrator Python with -B and:
spikes/30_at18_found_footage/verify_gpu_reuse.py --coordinate-with D:/Work/shotwright
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO))

from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    Location,
    Project,
    Shot,
    ShotOutputKind,
)
from orchestrator.jobs.gpu_slot import GpuExecutionSlot
from orchestrator.jobs.lease import PynvmlProbe
from orchestrator.jobs.queue import SystemHostResources
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import BinariesSettings, Settings, check_paths_exist
from orchestrator.store.json_store import ProjectStore

_PROJECT = "at18-gpu"
_EPISODE = "ep1"


def _sha256(root: ProjectRoot, relative: str) -> str:
    with root.resolve(relative).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _prepare(store: ProjectStore) -> None:
    store.save(Project(project_id=_PROJECT, title="AT-18 native runtime smoke"), expected_version=0)
    store.save(
        Episode(episode_id=_EPISODE, project_id=_PROJECT, number=1, duration_target_sec=60),
        expected_version=0,
    )
    camera_id = "living-room-fixed"
    store.create(CameraProfile(camera_id=camera_id, profile_name="Fixed room rig"))
    store.create(Location(location_id="living-room", scene_look="low_key", camera_rigs=[camera_id]))
    for count, shot_id in enumerate(("first", "second"), 1):
        store.create(Shot(
            shot_id=shot_id, episode_id=_EPISODE, location_id="living-room",
            camera_id=camera_id, camera_profile=camera_id, duration_sec=count / 60,
            degrade_seed=count,
        ))


def _render_two(
    root: ProjectRoot, store: ProjectStore, settings: Settings, report: dict,
) -> None:
    token = secrets.token_urlsafe(32)
    app = create_app(settings=settings, token=token)
    runner = app.state.context.job_runner
    try:
        with TestClient(app, base_url="http://127.0.0.1",
                        headers={"X-Loopback-Token": token}) as client:
            for shot_id in ("first", "second"):
                report["shots"][shot_id] = result = {}
                for stage in ("scene", "master"):
                    print(f"AT-18 {shot_id} {stage}", flush=True)
                    response = client.post(
                        f"/projects/{_PROJECT}/episodes/{_EPISODE}/shots/{shot_id}/{stage}",
                        json={},
                    )
                    if response.status_code != 202:
                        raise RuntimeError(f"{stage} HTTP {response.status_code}: {response.text}")
                    job = runner.wait(_PROJECT, response.json()["job_id"])
                    result[stage] = job.model_dump(mode="json")
                    if job.state != "completed":
                        raise RuntimeError(
                            f"{shot_id} {stage}: {job.error_kind}: {job.error_message}"
                        )

                outputs = store.list_shot_outputs(
                    shot_id, episode_id=_EPISODE, record_version=1, kind=ShotOutputKind.MASTER,
                )
                assert len(outputs) == 1
                output = outputs[0]
                result["master_receipt"] = output.model_dump(mode="json")
                assert _sha256(root, output.relative_path) == output.content_sha256
                attempt = PurePosixPath(output.relative_path).parent
                result["metadata_path"] = f"{attempt}/metadata.json"
                result["plan_path"] = f"{attempt}/plan.json"
                metadata = json.loads(root.read_text(result["metadata_path"]))
                plan = json.loads(root.read_text(result["plan_path"]))
                assert metadata["status"] == "completed"
                assert len(plan["outputs"]["frames"]) in (1, 2, 3)
                assert "RTX" in metadata["actual_device"].upper()
                result["device"] = metadata["actual_device"]
                result["render_build"] = metadata["build_hash"]
                result["frame_count"] = len(plan["outputs"]["frames"])
                result["shared_base"] = metadata["shared_base"]
                result["shot_blend_path"] = f"{attempt}/scene.blend"
                result["shot_blend_sha256"] = _sha256(root, result["shot_blend_path"])
                base = result["shared_base"]
                assert _sha256(root, base["blend_path"]) == base["sha256"]
                receipt = json.loads(root.read_text(base["receipt_path"]))
                assert receipt == {
                    "schema_version": 1, "key": base["key"], "blend_path": base["blend_path"],
                    "sha256": base["sha256"],
                }
                shared = plan["shared_base"]
                assert shared["key"] == base["key"]
                if shot_id == "first":
                    assert base["action"] == "built" and shared["input"] is None
                    assert Path(shared["output"]).resolve() == root.resolve(base["blend_path"])
                else:
                    assert base["action"] == "reused" and shared["output"] is None
                    assert shared["input"] == {
                        "path": root.resolve(base["blend_path"]).as_posix(), "sha256": base["sha256"],
                    }
                    first = report["shots"]["first"]
                    assert result["shot_blend_path"] != first["shot_blend_path"]
                    assert result["frame_count"] != first["frame_count"]
                    assert {**first["shared_base"], "action": "reused"} == base
    finally:
        runner.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinate-with", type=Path, help="Primary checkout with an existing GPU slot")
    parser.add_argument(
        "--blender-vulkan-device", help="Blender Vulkan adapter selector printed by --help",
    )
    args = parser.parse_args()

    worktree = ProjectRoot(REPO)
    run_name = f"gpu-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(4)}"
    run = f"spikes/30_at18_found_footage/_work/{run_name}"
    projects = worktree.make_dir(f"{run}/projects")
    summary = f"{run}/summary.json"
    report: dict = {
        "status": "failed",
        "project": f"{run}/projects/{_PROJECT}",
        "shots": {},
        "preflight": {"blender_vulkan_device": args.blender_vulkan_device},
    }
    try:
        settings = Settings(
            project_root=projects,
            binaries=BinariesSettings(blender_vulkan_device=args.blender_vulkan_device),
        )
        issues = check_paths_exist(settings)
        if issues:
            raise RuntimeError("; ".join(f"{issue.key} {issue.message}" for issue in issues))

        coordination = nullcontext()
        if args.coordinate_with is not None:
            primary = ProjectRoot(args.coordinate_with)
            if primary.path == REPO or not primary.resolve("_work/gpu_execution.lock").is_file():
                raise RuntimeError("choose a separate primary checkout with its existing GPU slot")
            coordination = GpuExecutionSlot(primary)

        with coordination:
            probe = PynvmlProbe()
            host = SystemHostResources()
            report["preflight"].update({
                "device": asdict(probe.device_memory()),
                "processes": [asdict(process) for process in probe.process_usage()],
                "host_free_ram_gb": host.free_ram_gb(),
                "free_disk_gb": host.free_disk_gb(projects),
            })
            if report["preflight"]["host_free_ram_gb"] < settings.resources.ram_floor_gb:
                raise RuntimeError("free host RAM is below the configured resource floor")
            if report["preflight"]["free_disk_gb"] < settings.budget.min_free_disk_gb:
                raise RuntimeError("free disk is below the configured budget floor")
            root = ProjectRoot(worktree.make_dir(report["project"]))
            store = ProjectStore(root)
            _prepare(store)
            _render_two(root, store, settings, report)
        report["status"] = "completed"
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        path = worktree.write_text(summary, json.dumps(report, indent=2) + "\n")
        print(path, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
