"""Matched real-queue material proof and review artifact extraction for spike20."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import time
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO))

from orchestrator.api.cli import main as cli_main
from orchestrator.domain.entities import Asset
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import parse_settings
from orchestrator.store.json_store import ProjectStore

SOURCE_PROJECT = "spikes/18_asset_sourcing/_work/20261107T224036_9c46656d/projects/asset_sourcing"
PH_MANIFEST = "assets/provider_cache/manifests/ph_wooden_table_02-e468addbd0234ffaabf83dd09cd2b5bd.json"
FIGURE = Path(r"D:\Work\comfyui-trellis\ComfyUI\output\3D\entity_00001_.glb")
FIGURE_PROVENANCE = Path(r"D:\Work\trellis2-gguf\PROVENANCE.json")
BLENDER = Path(r"D:\Blender\blender.exe")
LOCAL_OVERLAY_FONT = Path(r"C:\Windows\Fonts\consola.ttf")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_json(root: ProjectRoot, rel: str, value: Any) -> None:
    root.write_text(rel, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


class ProductionProof:
    def __init__(self, root: ProjectRoot, run_rel: str, floor: float) -> None:
        self.root, self.run_rel = root, run_rel
        self.source_root = ProjectRoot(root.resolve(SOURCE_PROJECT))
        self.run_id = run_rel.rsplit("/", 1)[-1]
        self.projects_rel = f"{run_rel}/projects"
        self.root.make_dir(self.projects_rel)
        self.root.make_dir(f"{run_rel}/incoming")
        self.root.make_dir(f"{run_rel}/steps")
        self.root.make_dir(f"{run_rel}/runtime/tmp")
        self.root.make_dir(f"{run_rel}/runtime/blender-config")
        self.root.make_dir(f"{run_rel}/runtime/blender-cache")
        os.environ["TMP"] = str(root.resolve(f"{run_rel}/runtime/tmp"))
        os.environ["TEMP"] = os.environ["TMP"]
        os.environ["TMPDIR"] = os.environ["TMP"]
        os.environ["BLENDER_USER_CONFIG"] = str(root.resolve(f"{run_rel}/runtime/blender-config"))
        os.environ["BLENDER_USER_SCRIPTS"] = os.environ["BLENDER_USER_CONFIG"]
        os.environ["BLENDER_USER_DATAFILES"] = str(root.resolve(f"{run_rel}/runtime/blender-cache"))
        self.floor = floor
        self.settings = parse_settings({
            "project_root": str(root.resolve(self.projects_rel)),
            "binaries": {"blender_exe": str(BLENDER)},
            "budget": {"per_shot_wall_clock_seconds": 3600.0,
                       "gpu_minutes_per_shot": 60.0, "gpu_minutes_per_episode": 600.0},
            "resources": {"throughput_floor_per_second": floor},
        })
        self.steps: list[dict[str, Any]] = []
        self.job_reports: dict[str, Any] = {}
        write_json(root, f"{run_rel}/runtime_settings.json", {
            "budget": {"per_shot_wall_clock_seconds": 3600.0},
            "resources": {"throughput_floor_per_second": floor},
            "caveat": "Functional diagnostic override; production defaults unchanged; not resource acceptance."})
        self.source_hashes = self.hash_sources()

    def hash_sources(self) -> dict[str, str]:
        candidates = [
            "orchestrator/domain/entities.py", "orchestrator/adapters/blender/scene.py",
            "orchestrator/adapters/blender/runner.py", "orchestrator/adapters/blender/worker.py",
            "orchestrator/service/compose.py", "orchestrator/store/json_store.py",
            "orchestrator/api/cli.py", "orchestrator/api/routes.py", "orchestrator/api/app.py",
            "orchestrator/service/script_authoring.py", "orchestrator/service/credits.py",
            "orchestrator/service/dependencies.py",
        ]
        return {rel: digest(self.root.resolve(rel)) for rel in candidates if self.root.resolve(rel).is_file()}

    def hash_render_sources(self) -> dict[str, str]:
        render_dependencies = {
            "orchestrator/domain/entities.py", "orchestrator/adapters/blender/scene.py",
            "orchestrator/adapters/blender/runner.py", "orchestrator/adapters/blender/worker.py",
            "orchestrator/service/compose.py", "orchestrator/service/dependencies.py",
            "orchestrator/service/script_authoring.py", "orchestrator/store/json_store.py",
            "orchestrator/api/cli.py", "orchestrator/api/routes.py", "orchestrator/api/app.py",
        }
        return {name: value for name, value in self.hash_sources().items()
                if name in render_dependencies}

    def cli(self, label: str, args: list[str], project_id: str | None = None) -> Any:
        out, err = io.StringIO(), io.StringIO()
        started = time.monotonic()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli_main(args, settings=self.settings)
        record = {"step": label, "project_id": project_id, "status": "completed" if status == 0 else "failed",
                  "exit_code": status, "elapsed_sec": round(time.monotonic() - started, 3),
                  "stdout": out.getvalue(), "stderr": err.getvalue()}
        self.steps.append(record)
        write_json(self.root, f"{self.run_rel}/steps/{len(self.steps):02d}_{label}.json", record)
        if status:
            raise RuntimeError(f"CLI {label} failed; retained step record has output")
        try:
            return json.loads(out.getvalue())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"CLI {label} did not return JSON") from exc

    def register_source_assets(self, project_id: str) -> dict[str, Any]:
        source_store = ProjectStore(self.source_root)
        registry: dict[str, Any] = {}
        # Reuse the previously acquired Poly Haven table and Sketchfab chair; do not download.
        for asset_id in ("ph_wooden_table_02", "sf_selected_model"):
            asset = source_store.load(Asset, asset_id)
            if not asset.file_path or not asset.checksum:
                raise RuntimeError(f"cached source asset {asset_id} has no registered file/hash")
            path = self.source_root.resolve(asset.file_path)
            if digest(path) != asset.checksum:
                raise RuntimeError(f"cached source asset hash mismatch: {asset_id}")
            payload = {key: value for key, value in asset.model_dump(mode="json").items()
                       if key in {"asset_id", "type", "generated", "source_url", "provider", "license",
                                  "model_or_voice_license", "attribution", "attribution_surface_required",
                                  "reuse_scope"} and value is not None}
            self.cli("register-retained-model", ["create-asset", "--json", "--project-id", project_id,
                "--file", str(path), "--data", json.dumps(payload)], project_id)
            copied = ProjectStore(ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}"))).load(Asset, asset_id)
            copied_path = ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}")).resolve(copied.file_path)
            registry[asset_id] = {"source_sha256": asset.checksum, "project_sha256": digest(copied_path),
                                  "license": asset.license, "source_url": asset.source_url,
                                  "attribution": asset.attribution, "path": asset.file_path}
        source_manifest = json.loads(self.source_root.read_text(PH_MANIFEST))
        by_name = {item["name"]: item for item in source_manifest["source_files"]}
        specs = {"base_color": "textures/wooden_table_02_diff_1k.jpg",
                 "normal_gl": "textures/wooden_table_02_nor_gl_1k.jpg",
                 "arm": "textures/wooden_table_02_arm_1k.jpg"}
        map_ids: dict[str, str] = {}
        for role, name in specs.items():
            entry = by_name[name]
            raw = self.source_root.read_bytes(entry["relative_path"])
            if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise RuntimeError(f"cached PBR map failed immutable manifest check: {name}")
            incoming = f"{self.run_rel}/incoming/{Path(name).name}"
            self.root.write_bytes(incoming, raw)
            map_id = f"ph_table_{role}"
            payload = {"asset_id": map_id, "type": "texture", "generated": False,
                       "source_url": source_manifest["source_page"], "provider": "polyhaven",
                       "license": source_manifest["license"], "attribution": source_manifest["attribution"],
                       "attribution_surface_required": True, "reuse_scope": "project", "expected_version": 0}
            self.cli("register-cached-map", ["create-asset", "--json", "--project-id", project_id,
                "--file", str(self.root.resolve(incoming)), "--data", json.dumps(payload)], project_id)
            map_ids[role] = map_id
            registry[map_id] = {"source_sha256": entry["sha256"], "license": source_manifest["license"],
                                "attribution": source_manifest["attribution"], "source_url": source_manifest["source_page"],
                                "reuse_scope": "project", "cache_relative": entry["relative_path"]}
        if not FIGURE.is_file() or not FIGURE_PROVENANCE.is_file():
            raise FileNotFoundError("previously generated figure or its PROVENANCE.json is missing")
        provenance = json.loads(FIGURE_PROVENANCE.read_text(encoding="utf-8"))["licence_note"]
        self.root.write_bytes(f"{self.run_rel}/source_PROVENANCE.json", FIGURE_PROVENANCE.read_bytes())
        payload = {"asset_id": "entity", "type": "model", "generated": True, "provider": "TRELLIS2",
                   "license": "Restricted to internal evaluation; not cleared for redistribution.",
                   "model_or_voice_license": provenance, "reuse_scope": "internal evaluation only"}
        self.cli("register-generated-figure", ["create-asset", "--json", "--project-id", project_id,
            "--file", str(FIGURE), "--data", json.dumps(payload)], project_id)
        registry["entity"] = {"source_sha256": digest(FIGURE), "source_path": str(FIGURE),
                              "license": payload["license"], "reuse_scope": payload["reuse_scope"],
                              "source_PROVENANCE": f"{self.run_rel}/source_PROVENANCE.json"}
        write_json(self.root, f"{self.run_rel}/{project_id}_asset_provenance.json", registry)
        return map_ids

    def register_retained_asset(self, project_id: str, asset_id: str) -> dict[str, Any]:
        source_store = ProjectStore(self.source_root)
        asset = source_store.load(Asset, asset_id)
        if not asset.file_path or not asset.checksum:
            raise RuntimeError(f"cached source asset {asset_id} has no registered file/hash")
        path = self.source_root.resolve(asset.file_path)
        if digest(path) != asset.checksum:
            raise RuntimeError(f"cached source asset hash mismatch: {asset_id}")
        payload = {key: value for key, value in asset.model_dump(mode="json").items()
                   if key in {"asset_id", "type", "generated", "source_url", "provider", "license",
                              "model_or_voice_license", "attribution", "attribution_surface_required",
                              "reuse_scope"} and value is not None}
        self.cli(f"register-retained-{asset_id}", ["create-asset", "--json", "--project-id", project_id,
            "--file", str(path), "--data", json.dumps(payload)], project_id)
        project = ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}"))
        copied = ProjectStore(project).load(Asset, asset_id)
        copied_path = project.resolve(copied.file_path)
        return {"asset_id": asset_id, "type": asset.type, "source_sha256": asset.checksum,
                "project_sha256": digest(copied_path), "source_path": asset.file_path,
                "license": asset.license, "attribution": asset.attribution,
                "source_url": asset.source_url, "reuse_scope": asset.reuse_scope}

    def prepare_final_script(self) -> str:
        project_id = f"scripted_{self.run_id[-8:]}"
        self.cli("create-project", ["create-project", "--json", "--data", json.dumps({
            "project_id": project_id, "title": "Spike20 Script-derived dressed shot",
            "output_profile": "low-light-bw"})], project_id)
        registered = self.register_source_assets(project_id)
        for asset_id in ("ph_brown_planks_03", "ph_studio_small_09"):
            registered[asset_id] = self.register_retained_asset(project_id, asset_id)
        self.install_font(project_id)
        camera_id = "spike20-review-low-key"
        camera = {"camera_id": camera_id, "profile_name": "Spike20 review low-key",
                  "camera_mode": "cctv", "field_of_view_mm": 12.0,
                  "exposure_ev": 0.5, "practical_light_color_k": 3200,
                  "practical_light_intensity": 1.3, "monochrome_night": True,
                  "camera_position": [-1.8, -1.85, 1.95]}
        self.cli("create-camera-profile", ["create-camera-profile", "--json", "--project-id", project_id,
            "--camera-id", camera_id, "--data", json.dumps({"camera_profile": camera})], project_id)
        self.cli("create-episode", ["create-episode", "--json", "--project-id", project_id,
            "--data", json.dumps({"episode": {"episode_id": "ep1", "project_id": project_id,
                "number": 1, "duration_target_sec": 15.0}})], project_id)
        location = {
            "location_id": "dressed_room", "base_shell": "american_living_room", "scene_look": "low_key",
            "placements": [
                {"asset_id": "ph_wooden_table_02", "position": [1.50, -0.80, 0.0],
                 "dimensions": [0.76, 0.47, 0.52], "fit_mode": "contain"},
                {"asset_id": "sf_selected_model", "position": [1.50, 0.16, 0.0],
                 "dimensions": [0.55, 0.51, 0.75], "fit_mode": "contain"},
            ],
            "material_overrides": {"Oak planks": {"maps": {"base_color": "ph_brown_planks_03"},
                "uv_scale": [2.0, 2.0], "normal_strength": 1.0, "ao_strength": 0.0}},
            "environment": {"asset_id": "ph_studio_small_09", "strength": 0.15, "rotation_rad": 0.0},
        }
        anomaly = {"kind": "linear_walk_path", "subtlety": 2, "height_m": 1.8,
                   "width_m": 0.5, "depth_m": 0.35, "minimum_distance_m": 2.0,
                   "maximum_on_frame_height_px": 400,
                   "path": [[0.65, 1.15, 0.0], [0.80, 1.90, 0.0]],
                   "figure_asset_id": "entity", "timing": {"duration_sec": 4.0}}
        script = {"script_id": "sc1", "episode_id": "ep1",
                  "logline": "A sourced living room with a figure crossing the rear of frame.",
                  "beat_to_shot": {"b1": "s1"}, "scenes": [location],
                  "beats": [{"beat_id": "b1", "target_sec": 15.0, "subtlety": 2,
                             "scene_direction": {"location_id": "dressed_room",
                                 "camera_profile": camera_id,
                                 "framing": "Static wide CCTV; sourced table and chair at right foreground; full figure visible near hallway.",
                                 "event_sec": 8.0, "anomaly": anomaly}}]}
        self.cli("draft-script", ["draft-script", "--json", "--project-id", project_id,
            "--episode-id", "ep1", "--data", json.dumps({"script": script})], project_id)
        project_root = ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}"))
        store = ProjectStore(project_root)
        from orchestrator.domain.entities import Episode, Script
        _, script_token = store.load_with_version(Script, "sc1", episode_id="ep1")
        _, episode_token = store.load_with_version(Episode, "ep1")
        derivation = {"expected_version": script_token, "expected_episode_version": episode_token,
                      "expected_shot_versions": {"s1": 0}, "expected_location_versions": {"dressed_room": 0}}
        derivation_result = self.cli("derive-script-scene-plan", ["derive-script-scene-plan", "--json",
            "--project-id", project_id, "--episode-id", "ep1", "--script-id", "sc1",
            "--data", json.dumps(derivation)], project_id)
        build_result = self.cli("build-scene", ["build-scene", "--json", "--project-id", project_id,
            "--episode-id", "ep1", "--shot-id", "s1"], project_id)
        credits = self.cli("list-episode-credits", ["list-episode-credits", "--json",
            "--project-id", project_id, "--episode-id", "ep1"], project_id)
        persisted_script = store.load(Script, "sc1", episode_id="ep1")
        persisted_episode = store.load(Episode, "ep1")
        from orchestrator.domain.entities import Location, Shot, ShotOutputKind
        persisted_shot = store.load(Shot, "s1", episode_id="ep1")
        persisted_location = store.load(Location, "dressed_room")
        scene_outputs = store.list_shot_outputs("s1", episode_id="ep1", kind=ShotOutputKind.SCENE)
        if not scene_outputs:
            raise RuntimeError("Script derivation/build did not persist a scene plan")
        scene_artifact = json.loads(project_root.read_text(scene_outputs[-1].relative_path))
        write_json(self.root, f"{self.run_rel}/final_script_provenance.json", {
            "project_id": project_id, "script": persisted_script.model_dump(mode="json"),
            "episode": persisted_episode.model_dump(mode="json"),
            "shot": persisted_shot.model_dump(mode="json"),
            "location": persisted_location.model_dump(mode="json"),
            "scene_plan": scene_artifact.get("plan"), "scene_receipt": scene_outputs[-1].model_dump(mode="json"),
            "derivation_result": derivation_result, "build_scene_result": build_result,
            "camera_proposal": camera, "asset_records": registered, "episode_credits": credits,
            "lighting_note": "Agent proposal: exposure_ev=0.5, practical_light_intensity=1.3, HDRI strength=0.15. Not a calibrated default.",
            "placement_note": "Contain-fit envelopes use prior measured GLB bounds; verify returned world-bound metadata.",
            "status": "Script-derived Shot and Location; human approval has not occurred."})
        return project_id

    def revise_final_height(self, project_id: str) -> None:
        project_root = ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}"))
        store = ProjectStore(project_root)
        from orchestrator.domain.entities import Episode, Location, Script, Shot, ShotOutputKind
        script, script_token = store.load_with_version(Script, "sc1", episode_id="ep1")
        current_location = script.scenes[0]
        adjusted_placements = []
        for placement in current_location.placements:
            center_z = 0.26 if placement.asset_id == "ph_wooden_table_02" else 0.375
            adjusted_placements.append(placement.model_copy(update={
                "position": (placement.position[0], placement.position[1], center_z)}))
        revised_location = current_location.model_copy(update={"placements": adjusted_placements})
        revised_script = script.model_copy(update={"version": script.version + 1,
            "supersedes": script.version, "scenes": [revised_location]})
        self.cli("revise-script-height", ["revise-script", "--json", "--project-id", project_id,
            "--episode-id", "ep1",
            "--data", json.dumps({"script": revised_script.model_dump(mode="json"),
                                  "expected_version": script_token})], project_id)
        current_script, current_script_token = store.load_with_version(Script, "sc1", episode_id="ep1")
        _, episode_token = store.load_with_version(Episode, "ep1")
        _, shot_token = store.load_with_version(Shot, "s1", episode_id="ep1")
        _, location_token = store.load_with_version(Location, "dressed_room")
        derived = self.cli("derive-script-scene-plan-height", ["derive-script-scene-plan", "--json",
            "--project-id", project_id, "--episode-id", "ep1", "--script-id", "sc1",
            "--data", json.dumps({"expected_version": current_script_token,
                "expected_episode_version": episode_token, "expected_shot_versions": {"s1": shot_token},
                "expected_location_versions": {"dressed_room": location_token}})], project_id)
        built = self.cli("build-scene-height", ["build-scene", "--json", "--project-id", project_id,
            "--episode-id", "ep1", "--shot-id", "s1"], project_id)
        credits = self.cli("list-episode-credits-height", ["list-episode-credits", "--json",
            "--project-id", project_id, "--episode-id", "ep1"], project_id)
        shot = store.load(Shot, "s1", episode_id="ep1")
        location = store.load(Location, "dressed_room")
        outputs = store.list_shot_outputs("s1", episode_id="ep1", record_version=shot.version,
                                          kind=ShotOutputKind.SCENE)
        if not outputs:
            raise RuntimeError("height-corrected Script successor has no built scene plan")
        artifact = json.loads(project_root.read_text(outputs[-1].relative_path))
        write_json(self.root, f"{self.run_rel}/final_script_provenance_v2.json", {
            "script": current_script.model_dump(mode="json"), "shot": shot.model_dump(mode="json"),
            "location": location.model_dump(mode="json"), "scene_plan": artifact.get("plan"),
            "scene_receipt": outputs[-1].model_dump(mode="json"), "derivation_result": derived,
            "build_scene_result": built, "episode_credits": credits,
            "supersedes": {"script_version": script.version, "location_version": 1, "shot_version": 1},
            "placement_centers": {item.asset_id: list(item.position) for item in location.placements},
            "approval": "none; creator review remains pending"})

    def preview_final_geometry(self, project_id: str) -> dict[str, Any]:
        from orchestrator.domain.entities import ShotOutputKind
        self.cli("render-preview-geometry", ["render-preview", "--json", "--project-id", project_id,
            "--episode-id", "ep1", "--shot-id", "s1"], project_id)
        jobs = self.cli("report-jobs-geometry-preview", ["report-jobs", "--json",
            "--project-id", project_id], project_id)
        project = ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}"))
        store = ProjectStore(project)
        previews = store.list_shot_outputs("s1", episode_id="ep1", record_version=2,
                                            kind=ShotOutputKind.PREVIEW)
        if not previews:
            raise RuntimeError("queued Workbench geometry preview has no retained ShotOutput")
        preview = previews[-1]
        metadata_files = sorted(project.resolve("renders/s1/v0002").rglob("metadata.json"),
                                key=lambda path: path.stat().st_mtime)
        if not metadata_files:
            raise RuntimeError("queued preview omitted returned Blender runtime metadata")
        metadata_path = metadata_files[-1]
        metadata = json.loads(project.read_text(metadata_path.relative_to(project.path).as_posix()))
        by_id = {row.get("asset_id"): row for row in metadata.get("asset_bounds", [])}
        missing = {"ph_wooden_table_02", "sf_selected_model"} - set(by_id)
        if missing:
            raise RuntimeError(f"runtime bounds metadata lacks placed assets: {sorted(missing)}")
        checks = {}
        for asset_id in ("ph_wooden_table_02", "sf_selected_model"):
            row = by_id[asset_id]
            minimum_z = float(row["placed_world_min"][2])
            checks[asset_id] = {"fit_mode": row["fit_mode"],
                "source_dimensions": row["source_dimensions"],
                "placed_world_min": row["placed_world_min"],
                "placed_world_max": row["placed_world_max"],
                "placed_dimensions": row["placed_dimensions"],
                "floor_contact_min_z_abs_m": abs(minimum_z)}
            if row["fit_mode"] != "contain" or abs(minimum_z) > 0.01:
                raise RuntimeError(f"placed asset {asset_id} missed its contain/floor-contact check: {checks[asset_id]}")
        preview_path = project.resolve(preview.relative_path)
        result = {"project_id": project_id, "preview_job_report": jobs,
            "preview_output": preview.model_dump(mode="json"),
            "preview_png_sha256": digest(preview_path),
            "runtime_metadata_relative_path": metadata_path.relative_to(project.path).as_posix(),
            "runtime_metadata_sha256": digest(metadata_path), "asset_bounds": checks,
            "anomaly_path": json.loads(self.root.read_text(f"{self.run_rel}/final_script_provenance_v2.json"))[
                "shot"]["anomaly"]["path"],
            "camera_position": json.loads(self.root.read_text(f"{self.run_rel}/final_script_provenance_v2.json"))[
                "scene_plan"]["camera"]["camera_position"],
            "camera_target": json.loads(self.root.read_text(f"{self.run_rel}/final_script_provenance_v2.json"))[
                "scene_plan"]["scene"]["camera_target"],
            "appearance_scope": "queued one-frame Workbench geometry preview; does not validate Eevee/PBR/HDRI look"}
        write_json(self.root, f"{self.run_rel}/geometry_preview.json", result)
        return result

    def resume_final_degrade(self, project_id: str) -> None:
        """Repair only the review CameraProfile's post-only manifest fields, then degrade."""
        from orchestrator.adapters.postprofile.profile import validate_profile
        from orchestrator.domain.base import Invalidation
        from orchestrator.domain.entities import CameraProfile, ShotOutputKind
        from orchestrator.domain.invalidation import diff_camera_profile, field_invalidation
        from orchestrator.service.compose import compose_post_profile_spec
        from orchestrator.service.dependencies import revise_camera_profile

        project = ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}"))
        store = ProjectStore(project)
        current, token = store.load_with_version(CameraProfile, "spike20-review-low-key")
        measured = store.load(CameraProfile, "low-light-bw")
        post_fields = [name for name in CameraProfile.model_fields
                       if field_invalidation(CameraProfile, name) is Invalidation.POST_ONLY]
        changes = {name: getattr(measured, name) for name in post_fields}
        revised = current.model_copy(update={**changes, "version": current.version + 1,
                                             "supersedes": current.version})
        profile_diff = diff_camera_profile(current, revised)
        if profile_diff.requires_rebuild or not profile_diff.post_only or profile_diff.rebuild:
            raise RuntimeError(f"review CameraProfile repair is not post-only: {profile_diff}")
        master_receipts = store.list_shot_outputs("s1", episode_id="ep1", kind=ShotOutputKind.MASTER,
                                                  record_version=2)
        if not master_receipts:
            raise RuntimeError("cannot resume degradation: final master receipt is missing")
        master_path = project.resolve(master_receipts[-1].relative_path)
        master_hash = digest(master_path)
        invalidated = revise_camera_profile(store, revised, expected_version=token, episode_ids=["ep1"])
        if invalidated:
            raise RuntimeError(f"post-only CameraProfile repair unexpectedly invalidated shots: {invalidated}")
        persisted = store.load(CameraProfile, "spike20-review-low-key")
        if persisted.version != current.version + 1:
            raise RuntimeError("post-only CameraProfile successor was not persisted")
        spec = compose_post_profile_spec(store)
        probe = self.probe(master_path)
        validated = validate_profile(spec, persisted, master_frames=int(probe["nb_read_frames"]),
                                      master_rate=Fraction(probe["avg_frame_rate"]))
        if digest(master_path) != master_hash or master_hash != master_receipts[-1].content_sha256:
            raise RuntimeError("post-only profile repair altered or changed the existing master")
        write_json(self.root, f"{self.run_rel}/final_postonly_profile_repair.json", {
            "camera_profile_id": persisted.camera_id, "from_version": current.version,
            "to_version": persisted.version, "changed_post_only_fields": list(profile_diff.post_only),
            "requires_rebuild": profile_diff.requires_rebuild, "invalidated_shots": list(invalidated),
            "camera_storage_resolution": persisted.storage_resolution, "camera_fps": persisted.fps,
            "profile_storage_resolution": spec.storage_resolution, "profile_output_fps": spec.output_fps,
            "validated_master_frames": int(probe["nb_read_frames"]),
            "master_sha256_before_after": master_hash, "validation_result": {
                "master_frames": validated.master_frames, "source_rate": str(validated.source_rate),
                "output_rate": str(validated.output_rate), "decimated_frames": validated.decimated_frames,
                "predicted_frames": validated.predicted_frames,
                "schedule": [event.model_dump(mode="json") for event in validated.schedule]},
            "operation_boundary": "typed service revise_camera_profile + CPU compose/validate; no render"})
        self.cli("degrade-media-resume-final", ["degrade-media", "--json", "--project-id", project_id,
            "--episode-id", "ep1", "--shot-id", "s1"], project_id)
        jobs = self.cli("report-jobs-resume-final", ["report-jobs", "--json",
            "--project-id", project_id], project_id)
        self.collect_final_outputs(project_id, jobs)

    def collect_final_outputs(self, project_id: str, jobs: Any) -> None:
        project = ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}"))
        store = ProjectStore(project)
        receipts = store.list_shot_outputs("s1", episode_id="ep1")
        by_kind = {item.kind.value: item for item in receipts}
        if any(kind not in by_kind for kind in ("scene", "master", "degraded")):
            raise RuntimeError("final Script-derived shot lacks scene/master/degraded outputs")
        master_info = self.probe(project.resolve(by_kind["master"].relative_path))
        delivery_info = self.probe(project.resolve(by_kind["degraded"].relative_path))
        if int(master_info["nb_read_frames"]) != 900:
            raise RuntimeError(f"final master has {master_info.get('nb_read_frames')} frames, expected 900")
        if int(delivery_info["nb_read_frames"]) != 150 or abs(float(delivery_info["duration"]) - 15.0) > 0.001:
            raise RuntimeError(f"final delivery timing mismatch: {delivery_info}")
        write_json(self.root, f"{self.run_rel}/final_delivery.json", {
            "project_id": project_id, "master": {"relative_path": by_kind["master"].relative_path,
                "sha256": by_kind["master"].content_sha256, **master_info},
            "delivery": {"relative_path": by_kind["degraded"].relative_path,
                "sha256": by_kind["degraded"].content_sha256, **delivery_info},
            "job_report": jobs, "approval": "none; creator review remains pending"})
        extract_samples(self.root, self.run_rel, project.path, 15.0, by_kind["master"], by_kind["degraded"])
        self.make_final_sheet(project_id)

    def finish_final_script(self, project_id: str) -> None:
        for stage in ("render-master", "degrade-media"):
            self.cli(stage, [stage, "--json", "--project-id", project_id,
                "--episode-id", "ep1", "--shot-id", "s1"], project_id)
        jobs = self.cli("report-jobs-final", ["report-jobs", "--json", "--project-id", project_id], project_id)
        project = ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}"))
        store = ProjectStore(project)
        receipts = store.list_shot_outputs("s1", episode_id="ep1")
        by_kind = {item.kind.value: item for item in receipts}
        if any(kind not in by_kind for kind in ("scene", "master", "degraded")):
            raise RuntimeError("final Script-derived shot lacks scene/master/degraded outputs")
        master_info = self.probe(project.resolve(by_kind["master"].relative_path))
        delivery_info = self.probe(project.resolve(by_kind["degraded"].relative_path))
        if int(master_info["nb_read_frames"]) != 900:
            raise RuntimeError(f"final master has {master_info.get('nb_read_frames')} frames, expected 900")
        if int(delivery_info["nb_read_frames"]) != 150 or abs(float(delivery_info["duration"]) - 15.0) > 0.001:
            raise RuntimeError(f"final delivery timing mismatch: {delivery_info}")
        write_json(self.root, f"{self.run_rel}/final_delivery.json", {
            "project_id": project_id, "master": {"relative_path": by_kind["master"].relative_path,
                "sha256": by_kind["master"].content_sha256, **master_info},
            "delivery": {"relative_path": by_kind["degraded"].relative_path,
                "sha256": by_kind["degraded"].content_sha256, **delivery_info},
            "job_report": jobs, "approval": "none; creator review remains pending"})
        extract_samples(self.root, self.run_rel, project.path, 15.0, by_kind["master"], by_kind["degraded"])
        self.make_final_sheet(project_id)

    def make_final_sheet(self, project_id: str) -> None:
        from PIL import Image, ImageDraw
        samples = json.loads(self.root.read_text(f"{self.run_rel}/samples.json"))["samples"]
        selected = [item for item in samples if item["source"] == "delivered"]
        project = ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}"))
        receipts = ProjectStore(project).list_shot_outputs("s1", episode_id="ep1")
        delivery = next(item for item in receipts if item.kind.value == "degraded")
        media = project.resolve(delivery.relative_path)
        probe = self.probe(media)
        sar_value = probe.get("sample_aspect_ratio")
        sar = float(Fraction(sar_value.replace(":", "/"))) if sar_value and sar_value not in {"N/A", "0:1"} else 1.0
        tiles = []
        for item in sorted(selected, key=lambda value: value["source_frame_index"]):
            with Image.open(self.root.resolve(item["filename"])) as opened:
                image = opened.convert("RGB")
            image = image.resize((round(image.width * sar), image.height), Image.Resampling.NEAREST)
            times = ", ".join(f"{value:g}s" for value in item["requested_time_sec"])
            tiles.append((image, f"{times} | frame {item['source_frame_index']}"))
        columns, margin, caption = 3, 16, 28
        w = max(image.width for image, _ in tiles); h = max(image.height for image, _ in tiles)
        rows = (len(tiles) + columns - 1) // columns
        sheet = Image.new("RGB", (columns*w+(columns+1)*margin, rows*(h+caption)+(rows+1)*margin), "white")
        draw=ImageDraw.Draw(sheet)
        for i,(image,label) in enumerate(tiles):
            x=margin+(i%columns)*(w+margin); y=margin+(i//columns)*(h+caption+margin)
            sheet.paste(image,(x,y)); draw.text((x,y+h+3),label,fill="black")
        self.root.write_with(f"{self.run_rel}/final_review_sheet.png", lambda target: sheet.save(target,format="PNG"))

    def inspect(self, project_id: str, label: str) -> dict[str, Any]:
        project = ProjectRoot(self.root.resolve(f"{self.projects_rel}/{project_id}"))
        store = ProjectStore(project)
        receipts = store.list_shot_outputs("s1", episode_id="ep1")
        by_kind = {item.kind.value: item for item in receipts}
        if any(kind not in by_kind for kind in ("scene", "master", "degraded")):
            raise RuntimeError(f"{project_id} lacks a scene/master/degraded receipt")
        master, delivery = by_kind["master"], by_kind["degraded"]
        master_path, delivery_path = project.resolve(master.relative_path), project.resolve(delivery.relative_path)
        master_probe = self.probe(master_path)
        delivery_probe = self.probe(delivery_path)
        if int(master_probe["nb_read_frames"]) != 6:
            raise RuntimeError(f"{label} matched master is not six decoded frames: {master_probe}")
        if abs(float(Fraction(delivery_probe["avg_frame_rate"])) * 0.1 - int(delivery_probe["nb_read_frames"])) > 0.1:
            raise RuntimeError(f"{label} delivery frame count/duration mismatch: {delivery_probe}")
        frame_rel = f"{self.run_rel}/matched_{label}.png"
        frame_path = self.root.resolve(frame_rel)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(master_path), "-frames:v", "1", "-y", str(frame_path)],
                       check=True, capture_output=True)
        if not frame_path.is_file():
            raise RuntimeError("FFmpeg did not produce contained matched still")
        rgb = subprocess.run(["ffmpeg", "-v", "error", "-i", str(frame_path), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                             check=True, capture_output=True).stdout
        info = {"label": label, "project_id": project_id,
                "master": {"relative_path": master.relative_path, "sha256": master.content_sha256,
                           **master_probe},
                "delivery": {"relative_path": delivery.relative_path, "sha256": delivery.content_sha256,
                              **delivery_probe},
                "still": frame_rel, "decoded_rgb_sha256": hashlib.sha256(rgb).hexdigest(),
                "job_report": self.job_reports.get(project_id)}
        write_json(self.root, f"{self.run_rel}/matched_{label}.json", info)
        return info

    @staticmethod
    def probe(path: Path) -> dict[str, Any]:
        result = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,avg_frame_rate,nb_read_frames,sample_aspect_ratio,duration",
            "-of", "json", str(path)], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)["streams"][0]

    def install_font(self, project_id: str) -> None:
        if not LOCAL_OVERLAY_FONT.is_file():
            raise FileNotFoundError("local Consolas overlay font is unavailable")
        project_rel = f"{self.projects_rel}/{project_id}"
        project_root = ProjectRoot(self.root.resolve(project_rel))
        project_root.make_dir("assets/fonts")
        project_root.write_bytes("assets/fonts/overlay.ttf", LOCAL_OVERLAY_FONT.read_bytes())
        write_json(self.root, f"{self.run_rel}/{project_id}_font_provenance.json", {
            "source": str(LOCAL_OVERLAY_FONT), "rights": "local internal evaluation only",
            "redistribution": "not cleared", "use": "burned-in review overlay"})

    def run_matched(self, *, resume: bool = False) -> None:
        # Baseline and treatment each receive identical source assets, scripts, shot, profiles,
        # camera, event timing and lighting; only the PBR Location override differs.
        projects = [(label, f"proof_{label}_{self.run_id[-8:]}") for label in ("before", "after")]
        if resume:
            label, pid = projects[0]
            project_path = self.root.resolve(f"{self.projects_rel}/{pid}")
            if not project_path.is_dir():
                raise FileNotFoundError(f"cannot resume; baseline project is missing: {pid}")
            self.install_font(pid)
            self.cli("degrade-media-resume", ["degrade-media", "--json", "--project-id", pid,
                "--episode-id", "ep1", "--shot-id", "s1"], pid)
            self.job_reports[pid] = self.cli("report-jobs-resume", ["report-jobs", "--json",
                "--project-id", pid], pid)
            self.inspect(pid, label)
        for label, pid in projects[1:] if resume else projects:
            # Creation and registration separated so material IDs are available to Location.
            self.cli("create-project", ["create-project", "--json", "--data", json.dumps({
                "project_id": pid, "title": f"Spike20 matched {label}", "output_profile": "low-light-bw"})], pid)
            maps = self.register_source_assets(pid)
            self.install_font(pid)
            self.setup_shot(pid, label, maps if label == "after" else None)
            for stage in ("build-scene", "render-master", "degrade-media"):
                self.cli(stage, [stage, "--json", "--project-id", pid, "--episode-id", "ep1", "--shot-id", "s1"], pid)
            self.job_reports[pid] = self.cli("report-jobs", ["report-jobs", "--json", "--project-id", pid], pid)
            self.inspect(pid, label)

    def setup_shot(self, pid: str, label: str, maps: dict[str, str] | None) -> None:
        location = {"location_id": "living_room", "base_shell": "american_living_room", "scene_look": "low_key"}
        if maps:
            location["material_overrides"] = {"Oak planks": {"maps": maps, "uv_scale": [2.0, 3.0],
                "normal_strength": 0.65, "ao_strength": 0.0}}
        self.cli("create-location", ["create-location", "--json", "--project-id", pid,
            "--location-id", "living_room", "--data", json.dumps({"location": location})], pid)
        self.cli("create-episode", ["create-episode", "--json", "--project-id", pid,
            "--data", json.dumps({"episode": {"episode_id": "ep1", "project_id": pid,
                "number": 1, "duration_target_sec": 0.1}})], pid)
        self.cli("draft-script", ["draft-script", "--json", "--project-id", pid, "--episode-id", "ep1",
            "--data", json.dumps({"script": {"script_id": "sc1", "episode_id": "ep1",
                "logline": "A quiet room; a figure appears at the far end.",
                "beats": [{"beat_id": "b1", "target_sec": 0.05, "subtlety": 2}]}})], pid)
        shot = {"shot_id": "s1", "episode_id": "ep1", "location_id": "living_room",
                "duration_sec": 0.1, "event_sec": 0.05, "camera_profile": "low-light-bw",
                "post_profile": "low-light-bw", "anomaly": {
                    "kind": "linear_walk_path", "subtlety": 2, "height_m": 1.8, "width_m": 0.5,
                    "depth_m": 0.35, "minimum_distance_m": 2.0, "maximum_on_frame_height_px": 400,
                    "path": [[1.5, -1.0, 0.0], [1.5, 1.5, 0.0]], "figure_asset_id": "entity",
                    "timing": {"duration_sec": 0.05}}}
        self.cli("propose-shots", ["propose-shots", "--json", "--project-id", pid,
            "--episode-id", "ep1", "--data", json.dumps({"shots": [shot]})], pid)


def extract_samples(root: ProjectRoot, run_rel: str, project_path: Path, duration: float,
                    master: Any, degraded: Any) -> None:
    """Retain selected master and delivery PNGs plus decoder-backed sample metadata."""
    project = ProjectRoot(project_path)
    root.make_dir(f"{run_rel}/samples")
    samples: list[dict[str, Any]] = []
    for label, receipt in (("master", master), ("delivered", degraded)):
        source = project.resolve(receipt.relative_path)
        result = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,nb_read_frames,sample_aspect_ratio",
            "-of", "json", str(source)], check=True, capture_output=True, text=True)
        probe = json.loads(result.stdout)["streams"][0]
        count = int(probe.get("nb_read_frames") or 0)
        fps = float(Fraction(probe["avg_frame_rate"]))
        if count <= 0 or fps <= 0:
            raise RuntimeError(f"{label} artifact has no decoded video frames/rate")
        times = [0.0, 3.0, 4.0, 6.0, 8.4, 9.0, 10.0, 12.0, (count - 1) / fps]
        selected: dict[int, list[float]] = {}
        for at_sec in times:
            index = min(count - 1, max(0, round(at_sec * fps)))
            selected.setdefault(index, []).append(at_sec)
        for index, requested in sorted(selected.items()):
            relative = f"{run_rel}/samples/{label}_{index:06d}.png"
            root.write_with(relative, lambda target: None)
            output = root.resolve(relative)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-vf", rf"select=eq(n\,{index})",
                "-vsync", "0", "-frames:v", "1", "-y", str(output)], check=True, capture_output=True)
            if not output.is_file():
                raise RuntimeError(f"FFmpeg did not write the contained sample {relative}")
            decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-f", "rawvideo",
                "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True).stdout
            samples.append({"filename": relative, "source": label, "source_frame_index": index,
                "source_frame_count": count, "requested_time_sec": requested,
                "width": probe.get("width"), "height": probe.get("height"),
                "sample_aspect_ratio": probe.get("sample_aspect_ratio"),
                "decoded_sha256": hashlib.sha256(decoded).hexdigest()})
    write_json(root, f"{run_rel}/samples.json", {"samples": samples})


def sample_live_exr(root: ProjectRoot, run_rel: str, requested_index: int | None = None) -> dict[str, Any]:
    """Convert any already-complete queued Eevee sample frame to a reviewable PNG."""
    project_id = json.loads(root.read_text(f"{run_rel}/final_project.json"))["project_id"]
    project = ProjectRoot(root.resolve(f"{run_rel}/projects/{project_id}"))
    render_root = project.resolve("renders")
    available = []
    for candidate in render_root.rglob("frame_*.exr"):
        match = __import__("re").fullmatch(r"frame_(\d{6})\.exr", candidate.name)
        if match and candidate.is_file() and candidate.stat().st_size > 0:
            available.append((int(match.group(1)), candidate))
    if not available:
        raise FileNotFoundError("queued master has not produced a complete EXR frame yet")
    if requested_index is None:
        index, source = min(available, key=lambda value: value[0])
    else:
        selected = [value for value in available if value[0] == requested_index]
        if not selected:
            raise FileNotFoundError(f"queued master frame {requested_index:06d} is not available yet")
        index, source = selected[0]
    relative_source = source.relative_to(project.path).as_posix()
    root.make_dir(f"{run_rel}/live_samples")
    tag = f"{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}"
    output_rel = f"{run_rel}/live_samples/frame_{index:06d}_{tag}.png"
    root.write_with(output_rel, lambda target: None)
    output = root.resolve(output_rel)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-frames:v", "1", "-y", str(output)],
                   check=True, capture_output=True)
    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("FFmpeg did not decode the selected queued Eevee frame")
    result = {"project_id": project_id, "source_frame_index": index,
              "source_exr_relative_path": relative_source,
              "source_exr_sha256": digest(source), "preview_png": output_rel,
              "preview_png_sha256": digest(output), "diagnostic": "CPU conversion of a completed queued Eevee frame"}
    write_json(root, f"{run_rel}/live_samples/{tag}.json", result)
    return result


def positive_finite(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be finite and positive") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def _resolve_run(root: ProjectRoot, run_relative: str) -> str:
    relative = run_relative.replace("\\", "/")
    path = root.resolve(relative)
    work_root = root.resolve("spikes/20_overnight_production/_work")
    try:
        path.relative_to(work_root)
    except ValueError as exc:
        raise ValueError("run path must be beneath spike20 _work") from exc
    if not path.is_dir():
        raise FileNotFoundError(relative)
    return relative


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument("--resume", metavar="RUN_RELATIVE",
                        help="resume an existing matched run after a harness prerequisite failure")
    phases.add_argument("--prepare-final", metavar="RUN_RELATIVE",
                        help="create the Script-derived dressed shot and build its scene")
    phases.add_argument("--revise-final-height", metavar="RUN_RELATIVE",
                        help="append corrected placement centers to Script and derive/build successor versions")
    phases.add_argument("--preview-final-geometry", metavar="RUN_RELATIVE",
                        help="queue one-frame geometry preview and retain measured asset bounds")
    phases.add_argument("--refresh-final-freeze", metavar="RUN_RELATIVE",
                        help="record parent-approved v3 source hashes after the known authoring boundary correction")
    phases.add_argument("--finish-final", metavar="RUN_RELATIVE",
                        help="queue the final 15-second master and degradation")
    phases.add_argument("--resume-final-degrade", metavar="RUN_RELATIVE",
                        help="resume only final degradation after a retained manifest validation failure")
    phases.add_argument("--sample-live", metavar="RUN_RELATIVE",
                        help="convert a complete queued master EXR into a contained review PNG")
    parser.add_argument("--sample-live-frame", type=int,
                        help="select a one-based frame index for --sample-live; default is the earliest available")
    parser.add_argument("--throughput-floor", type=positive_finite, default=0.0002487528445856717,
                        help="positive local diagnostic floor from spike17; defaults unchanged")
    args = parser.parse_args()
    root = ProjectRoot(REPO)
    stage_option = (args.prepare_final or args.revise_final_height or args.preview_final_geometry
                    or args.refresh_final_freeze or args.finish_final or args.resume_final_degrade
                    or args.sample_live)
    if args.resume:
        run_rel = _resolve_run(root, args.resume)
    elif stage_option:
        run_rel = _resolve_run(root, stage_option)
    else:
        run_rel = f"spikes/20_overnight_production/_work/{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        if root.resolve(run_rel).exists():
            raise FileExistsError(run_rel)
        root.make_dir(run_rel)
    print(f"Run directory: {run_rel}", flush=True)
    if args.sample_live:
        if args.sample_live_frame is not None and args.sample_live_frame < 1:
            parser.error("--sample-live-frame must be a positive one-based index")
        result = sample_live_exr(root, run_rel, args.sample_live_frame)
        print(json.dumps(result, ensure_ascii=False))
        return
    proof = ProductionProof(root, run_rel, args.throughput_floor)
    if args.prepare_final:
        before_rel = f"{run_rel}/final_source_hashes_before.json"
        if root.resolve(before_rel).exists():
            raise FileExistsError("final source freeze is already recorded for this run")
        write_json(root, before_rel, proof.hash_sources())
        project_id = proof.prepare_final_script()
        write_json(root, f"{run_rel}/final_project.json", {"project_id": project_id,
            "approval": "not approved; human review pending"})
        print(f"Script-derived scene prepared: {project_id}")
        return
    if args.preview_final_geometry:
        render_before = json.loads(root.read_text(f"{run_rel}/final_render_hashes_v2_before.json"))
        if proof.hash_render_sources() != render_before:
            raise RuntimeError("frozen render dependencies changed before geometry preview")
        project_id = json.loads(root.read_text(f"{run_rel}/final_project.json"))["project_id"]
        result = proof.preview_final_geometry(project_id)
        after = proof.hash_render_sources()
        write_json(root, f"{run_rel}/geometry_preview_render_hashes_after.json", after)
        if after != render_before:
            raise RuntimeError("render dependencies changed during geometry preview")
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.revise_final_height:
        render_before_rel = f"{run_rel}/final_render_hashes_v2_before.json"
        all_before_rel = f"{run_rel}/final_source_hashes_v2_before.json"
        if root.resolve(render_before_rel).exists() or root.resolve(all_before_rel).exists():
            if not root.resolve(render_before_rel).is_file() or not root.resolve(all_before_rel).is_file():
                raise RuntimeError("v2 source snapshot is incomplete; preserve it for review")
            render_before = json.loads(root.read_text(render_before_rel))
            all_before = json.loads(root.read_text(all_before_rel))
            if proof.hash_render_sources() != render_before:
                raise RuntimeError("render dependency source changed since v2 snapshot")
        else:
            render_before = proof.hash_render_sources()
            all_before = proof.hash_sources()
            write_json(root, render_before_rel, render_before)
            write_json(root, all_before_rel, all_before)
        project_id = json.loads(root.read_text(f"{run_rel}/final_project.json"))["project_id"]
        proof.revise_final_height(project_id)
        render_after = proof.hash_render_sources()
        all_after = proof.hash_sources()
        write_json(root, f"{run_rel}/final_render_hashes_v2_after.json", render_after)
        write_json(root, f"{run_rel}/final_source_hashes_v2_after.json", all_after)
        if render_after != render_before:
            raise RuntimeError("render dependency source changed during v2 Script derivation/build")
        write_json(root, f"{run_rel}/final_source_hash_review_v2.json", {
            "render_dependencies_unchanged": True,
            "other_source_changes": sorted(name for name in all_before
                if all_before.get(name) != all_after.get(name)),
            "all_sources_unchanged": all_before == all_after})
        return
    if args.refresh_final_freeze:
        old_render = json.loads(root.read_text(f"{run_rel}/final_render_hashes_v2_before.json"))
        current_render = proof.hash_render_sources()
        changed = sorted(name for name in old_render
                         if old_render.get(name) != current_render.get(name))
        if changed != ["orchestrator/service/script_authoring.py"]:
            raise RuntimeError(f"unexpected render-source change set for v3 snapshot: {changed}")
        write_json(root, f"{run_rel}/final_render_hashes_v3_before.json", current_render)
        write_json(root, f"{run_rel}/final_source_hashes_v3_before.json", proof.hash_sources())
        write_json(root, f"{run_rel}/final_source_hash_refresh_v3.json", {
            "reason": "parent-approved nonvisual authoring boundary correction",
            "changed_render_sources": changed,
            "prior_render_hashes": old_render,
            "current_render_hashes": current_render,
            "master_visual_dependencies_unchanged": all(
                old_render.get(name) == current_render.get(name)
                for name in old_render if name != "orchestrator/service/script_authoring.py")})
        return
    if args.resume_final_degrade:
        version = "v3" if root.resolve(f"{run_rel}/final_render_hashes_v3_before.json").is_file() else "v2"
        frozen = json.loads(root.read_text(f"{run_rel}/final_render_hashes_{version}_before.json"))
        if proof.hash_render_sources() != frozen:
            raise RuntimeError("frozen render dependencies changed before degradation-only resume")
        project_id = json.loads(root.read_text(f"{run_rel}/final_project.json"))["project_id"]
        proof.resume_final_degrade(project_id)
        after = proof.hash_render_sources()
        write_json(root, f"{run_rel}/final_render_hashes_{version}_after.json", after)
        if after != frozen:
            raise RuntimeError("render source dependencies changed during degradation-only resume")
        print(f"Final degradation resumed without rendering: {run_rel}")
        return
    if args.finish_final:
        version = "v3" if root.resolve(f"{run_rel}/final_render_hashes_v3_before.json").is_file() else "v2"
        render_before_rel = f"{run_rel}/final_render_hashes_{version}_before.json"
        all_before_rel = f"{run_rel}/final_source_hashes_{version}_before.json"
        if not root.resolve(render_before_rel).is_file():
            raise FileNotFoundError(f"{version} render dependency freeze has not been recorded")
        render_before = json.loads(root.read_text(render_before_rel))
        render_now = proof.hash_render_sources()
        if render_now != render_before:
            raise RuntimeError("frozen render dependencies changed since height-corrected scene preparation")
        project_id = json.loads(root.read_text(f"{run_rel}/final_project.json"))["project_id"]
        proof.finish_final_script(project_id)
        render_after = proof.hash_render_sources()
        all_after = proof.hash_sources()
        write_json(root, f"{run_rel}/final_render_hashes_{version}_after.json", render_after)
        write_json(root, f"{run_rel}/final_source_hashes_{version}_after.json", all_after)
        if render_after != render_before:
            raise RuntimeError("render dependency source changed during final queued render/degradation")
        all_before = json.loads(root.read_text(all_before_rel))
        write_json(root, f"{run_rel}/final_source_hash_review_{version}.json", {
            "render_dependencies_unchanged": True,
            "other_source_changes": sorted(name for name in all_before
                if all_before.get(name) != all_after.get(name)),
            "all_sources_unchanged": all_before == all_after})
        print(f"Final Script-derived shot complete: {run_rel}")
        return
    before_rel = f"{run_rel}/source_hashes_before.json"
    if args.resume:
        if not root.resolve(before_rel).is_file():
            raise FileNotFoundError("resume run has no original source hash record")
        proof.source_hashes = json.loads(root.read_text(before_rel))
    else:
        write_json(root, before_rel, proof.source_hashes)
    try:
        proof.run_matched(resume=bool(args.resume))
        after = proof.hash_sources()
        write_json(root, f"{run_rel}/source_hashes_after.json", after)
        evidence = {"run_relative": run_rel, "source_hashes_before": proof.source_hashes,
                    "source_hashes_after": after, "source_hashes_unchanged": proof.source_hashes == after,
                    "settings": {"throughput_floor_per_second": args.throughput_floor,
                                 "wall_clock_seconds": 3600, "caveat": "diagnostic functional proof; no default budget/AT-15 acceptance"},
                    "steps": [{"step": item["step"], "project_id": item["project_id"],
                               "status": item["status"], "elapsed_sec": item["elapsed_sec"]} for item in proof.steps]}
        write_json(root, f"{run_rel}/evidence.json", evidence)
        from PIL import Image, ImageDraw
        before = Image.open(root.resolve(f"{run_rel}/matched_before.png")).convert("RGB")
        after_img = Image.open(root.resolve(f"{run_rel}/matched_after.png")).convert("RGB")
        if before.size != after_img.size:
            raise RuntimeError("matched frame dimensions differ")
        sheet = Image.new("RGB", (before.width * 2, before.height + 32), "white")
        sheet.paste(before, (0, 32)); sheet.paste(after_img, (before.width, 32))
        draw = ImageDraw.Draw(sheet); draw.text((8, 8), "Before: fixture material", fill="black")
        draw.text((before.width + 8, 8), "After: registered PH PBR maps", fill="black")
        root.write_with(f"{run_rel}/matched_material_review.png", lambda target: sheet.save(target, format="PNG"))
        print(f"Matched proof retained: {run_rel}")
    except BaseException as exc:
        write_json(root, f"{run_rel}/failure.json", {"type": type(exc).__name__, "message": str(exc)})
        raise


if __name__ == "__main__":
    main()
