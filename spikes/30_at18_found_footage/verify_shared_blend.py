"""Verify shared Blender room and camera reuse with optional CPU sample renders."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
WORKER = REPO / "orchestrator/adapters/blender/worker.py"
RECEIPT_MARKER = "AT18_PROBE "
PROP = "Remote control"
SAMPLE_SIZE = (160, 120)
SAMPLE_COUNT = 4
SAMPLE_THREADS = 4


def _load_worker():
    spec = importlib.util.spec_from_file_location("shotwright_blender_worker", WORKER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Blender worker {WORKER}")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def _near(left, right) -> bool:
    return len(left) == len(right) and all(
        math.isclose(a, b, abs_tol=1e-6) for a, b in zip(left, right)
    )


def _render_sample(bpy, scene, path: str, *, frame: int) -> dict:
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = SAMPLE_COUNT
    scene.cycles.use_denoising = False
    render = scene.render
    render.resolution_x, render.resolution_y = SAMPLE_SIZE
    render.resolution_percentage = 100
    render.threads_mode = "FIXED"
    render.threads = SAMPLE_THREADS
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGB"
    render.image_settings.color_depth = "8"
    render.filepath = path
    scene.frame_set(frame)
    if scene.cycles.device != "CPU" or render.threads != SAMPLE_THREADS:
        raise RuntimeError("Blender did not accept the CPU sample render settings")
    if "FINISHED" not in bpy.ops.render.render(write_still=True):
        raise RuntimeError("Blender did not finish the CPU sample")
    return {"frame": frame, "engine": render.engine, "device": scene.cycles.device,
            "threads": render.threads, "samples": scene.cycles.samples}


def _child(plan_path: Path) -> None:
    import bpy
    from mathutils import Euler, Matrix, Vector

    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan = payload["plan"]
    worker = _load_worker()
    shared = plan["shared_base"]
    is_reuse = shared["input"] is not None
    if is_reuse:
        worker._open_shared_base(bpy, shared)

        def unexpected_build(*args, **kwargs):
            raise RuntimeError("reusing the base attempted to rebuild geometry, lights, or camera")

        # Neither fixture shot adds mesh or light datablocks. Any builder call
        # during reuse proves that opening the base did not replace reconstruction.
        worker._create_object = unexpected_build
        worker._create_light = unexpected_build
        worker._create_camera = unexpected_build
    else:
        bpy.ops.wm.read_factory_settings(use_empty=True)

    prefs = bpy.context.preferences.filepaths
    for field, value in (
        ("use_auto_save_temporary_files", False),
        ("use_auto_save", False),
        ("save_version", 0),
        ("file_preview_type", "NONE"),
    ):
        if hasattr(prefs, field):
            setattr(prefs, field, value)

    scene = bpy.context.scene
    scene.frame_end = plan["frame_end"]
    if is_reuse and (
        scene.camera.animation_data is not None or scene.objects[PROP].animation_data is not None
    ):
        raise RuntimeError("shot animation leaked into the shared room")
    metadata = {"asset_bounds": []}
    worker._populate_shot(bpy, Matrix, Euler, Vector, scene, plan, metadata)
    prop = scene.objects[PROP]
    object_plan = next(item for item in plan["scene"]["objects"] if item["name"] == PROP)
    checks = object_plan["keyframes"] or [{"frame": 1, "location": object_plan["location"]}]
    prop_samples = []
    for key in checks:
        scene.frame_set(key["frame"])
        measured = tuple(prop.location)
        if not _near(measured, key["location"]):
            raise RuntimeError(f"prop animation mismatch at frame {key['frame']}: {measured}")
        prop_samples.append({"frame": key["frame"], "location": measured})

    camera_samples = []
    for key in plan["scene"]["camera_keyframes"]:
        scene.frame_set(key["frame"])
        measured = tuple(scene.camera.location)
        if not _near(measured, key["location"]):
            raise RuntimeError(f"camera animation mismatch at frame {key['frame']}: {measured}")
        camera_samples.append({"frame": key["frame"], "location": measured})
    if len(camera_samples) < 2 or _near(camera_samples[0]["location"], camera_samples[-1]["location"]):
        raise RuntimeError("the camera did not animate across the shot")

    meshes = []
    for obj in sorted(scene.objects, key=lambda item: item.name):
        if obj.type == "MESH":
            meshes.append((obj.name, [list(vertex.co) for vertex in obj.data.vertices]))
    digest = hashlib.sha256(json.dumps(meshes, separators=(",", ":")).encode("utf-8")).hexdigest()
    scene.frame_set(1)
    if "FINISHED" not in bpy.ops.wm.save_as_mainfile(filepath=payload["shot_blend"], compress=False):
        raise RuntimeError("could not save shot blend")
    evidence = {
        **metadata["shared_base"],
        "pid": os.getpid(),
        "blender": bpy.app.version_string,
        "mesh_count": len(meshes),
        "mesh_sha256": digest,
        "rig": {"name": scene.camera.name, "lens_mm": scene.camera.data.lens},
        "prop_samples": prop_samples,
        "camera_samples": camera_samples,
    }
    if payload["sample_path"] is not None:
        frame = checks[-1]["frame"] if is_reuse else 1
        evidence["sample"] = _render_sample(bpy, scene, payload["sample_path"], frame=frame)
    print(RECEIPT_MARKER + json.dumps(evidence, sort_keys=True), flush=True)


def _fixture_requests() -> tuple:
    from orchestrator.adapters.blender.scene import (
        RenderRequest,
        SharedLocation,
        american_living_room,
    )
    from orchestrator.domain.anomaly import DoorOrObjectMovement
    from orchestrator.domain.entities import CameraProfile

    scene = american_living_room()
    shared = SharedLocation(
        episode_id="structural-smoke",
        location_id="living-room",
        location_version=1,
        camera_id="handheld-a",
        object_names=tuple(item.name for item in scene.objects),
        light_names=tuple(item.name for item in scene.lights),
        material_names=tuple(item.name for item in scene.materials),
    )
    profile = CameraProfile(
        camera_id="consumer-2002",
        profile_name="AT-18 structural smoke",
        camera_mode="non_cctv",
        camera_mount="handheld",
        field_of_view_mm=16,
    )
    common = {
        "shot_version": 1,
        "degrade_seed": 0,
        "camera_profile": profile,
        "scene": scene,
        "shared_location": shared,
    }
    prop_start = next(item.location for item in scene.objects if item.name == PROP)
    prop_end = (prop_start[0] + 0.16, *prop_start[1:])
    motion = DoorOrObjectMovement(
        kind="door_or_object_movement", target_object=PROP, path=(prop_start, prop_end),
    )
    return (
        RenderRequest(**common, shot_id="first", run_id="smoke-first", duration_sec=4.0,
                      motion_seed=1),
        RenderRequest(**common, shot_id="second", run_id="smoke-second", duration_sec=3.0,
                      motion_seed=2, event_sec=0.5, anomaly=motion),
    )


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _run_blender(root, blender: str, plan_path: Path, log_relative: str) -> dict:
    runtime = root.make_dir("runtime")
    env = dict(
        os.environ,
        TEMP=str(runtime),
        TMP=str(runtime),
        TMPDIR=str(runtime),
        XDG_CACHE_HOME=str(root.make_dir("runtime/cache")),
        BLENDER_USER_CONFIG=str(root.make_dir("runtime/config")),
        BLENDER_USER_SCRIPTS=str(root.make_dir("runtime/scripts")),
        BLENDER_USER_DATAFILES=str(root.make_dir("runtime/datafiles")),
        BLENDER_USER_EXTENSIONS=str(root.make_dir("runtime/extensions")),
        PYTHONDONTWRITEBYTECODE="1",
    )
    argv = [
        blender, "--background", "--factory-startup", "--disable-autoexec",
        "--threads", "4", "--python-exit-code", "1", "--python",
        str(Path(__file__).resolve()), "--", "worker", str(plan_path),
    ]

    def write_log(path: Path) -> None:
        with path.open("xb") as log:
            result = subprocess.run(
                argv, cwd=root.path, env=env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, shell=False, timeout=180, check=False,
            )
        if result.returncode != 0:
            raise RuntimeError(f"Blender exited {result.returncode}. See {path}")

    log = root.write_with(log_relative, write_log)
    entries = [
        json.loads(line.removeprefix(RECEIPT_MARKER))
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.startswith(RECEIPT_MARKER)
    ]
    if len(entries) != 1:
        raise RuntimeError(f"expected one Blender structural receipt in {log}")
    return entries[0]


def main() -> None:
    sys.path.insert(0, str(REPO))
    from orchestrator.adapters.blender.scene import build_scene_plan
    from orchestrator.adapters.blender.shared_base import SharedBase, static_scene
    from orchestrator.platform.containment import ProjectRoot

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", default=r"D:\Blender\blender.exe")
    parser.add_argument(
        "--render-samples", action="store_true",
        help="Render one 160x120, four-sample Cycles PNG per shot on the CPU",
    )
    args = parser.parse_args()
    resolved_blender = shutil.which(args.blender)
    if resolved_blender is None:
        raise SystemExit(f"Blender executable not found: {args.blender}")
    blender = str(Path(resolved_blender).resolve())
    repo = ProjectRoot(REPO)
    root = ProjectRoot(repo.make_dir(f"spikes/30_at18_found_footage/_work/reuse-{uuid4().hex[:8]}"))
    print(f"AT-18 evidence directory: {root.path}", flush=True)
    worker_digest = _sha256(WORKER)
    blender_digest = _sha256(Path(blender))
    entries = []
    for request in _fixture_requests():
        plan = build_scene_plan(request)
        attempt = f"renders/{request.shot_id}/v0001/{request.run_id}/attempt-0001"
        shared = SharedBase.prepare(
            root, request, plan, worker_sha256=worker_digest,
            blender_sha256=blender_digest, attempt=attempt,
        )
        plan["shared_base"] = shared.worker_payload(root, static_scene(plan, request.shared_location))
        shot_blend = root.write_with(f"{attempt}/scene.blend", lambda path: None)
        sample_relative = f"{attempt}/sample.png"
        sample_path = root.write_with(sample_relative, lambda path: None) if args.render_samples else None
        payload = {
            "plan": plan, "shot_blend": str(shot_blend),
            "sample_path": str(sample_path) if sample_path is not None else None,
        }
        plan_path = root.write_text(f"{attempt}/plan.json", json.dumps(payload))
        evidence = _run_blender(root, blender, plan_path, f"{attempt}/blender.log")
        if sample_path is not None:
            image = root.read_bytes(sample_relative)
            dimensions = tuple(int.from_bytes(image[start:start + 4], "big") for start in (16, 20))
            if not image.startswith(b"\x89PNG\r\n\x1a\n") or dimensions != SAMPLE_SIZE:
                raise RuntimeError(f"invalid CPU sample PNG {sample_path}")
            evidence["sample"].update(
                relative_path=sample_relative, sha256=hashlib.sha256(image).hexdigest(),
                width=dimensions[0], height=dimensions[1],
            )
        worker_evidence = {name: evidence[name] for name in ("key", "action")}
        receipt = shared.publish(root, shared.verify(root, worker_evidence))
        entries.append(evidence | receipt | {"shot_blend": f"{attempt}/scene.blend"})

    first, second = entries
    if [first["action"], second["action"]] != ["built", "reused"]:
        raise RuntimeError(f"expected one room build and one reuse: {entries}")
    for field in ("key", "blend_path", "sha256", "mesh_count", "mesh_sha256", "rig"):
        if first[field] != second[field]:
            raise RuntimeError(f"shared room or rig mismatch in {field}")
    if first["pid"] == second["pid"]:
        raise RuntimeError("expected two independent Blender processes")
    if _near(first["camera_samples"][0]["location"], second["camera_samples"][0]["location"]):
        raise RuntimeError("shots did not receive independent camera motion")
    if len(second["prop_samples"]) < 2 or _near(
        second["prop_samples"][0]["location"], second["prop_samples"][-1]["location"],
    ):
        raise RuntimeError("second shot did not animate the reused prop")
    verification = {
        "mode": "structural-and-cpu-samples" if args.render_samples else "structural-only",
        "shots": entries,
    }
    report = root.write_text("verification.json", json.dumps(verification, indent=2) + "\n")
    print(f"Verified cross-process room and rig reuse: {report}", flush=True)


if __name__ == "__main__":
    if "--" in sys.argv and sys.argv[sys.argv.index("--") + 1 :][:1] == ["worker"]:
        _child(Path(sys.argv[-1]))
    else:
        main()
