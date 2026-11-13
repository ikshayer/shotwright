"""Independent, read-only verifier for one persisted asset round-trip run.

Usage: python spikes/17_asset_roundtrip/verify_run.py --run-relative spikes/17_asset_roundtrip/_work/<id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestrator.domain.entities import (
    Asset,
    Episode,
    Location,
    PostProfile,
    Project,
    Shot,
    ShotOutputKind,
)
from orchestrator.jobs.journal import read_job
from orchestrator.platform.containment import ProjectRoot
from orchestrator.store.json_store import ProjectStore


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,nb_read_frames,duration",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    data = json.loads(proc.stdout)
    require(data.get("streams"), f"ffprobe found no video stream in {path}")
    return data


def observed_rate(stream: dict[str, Any], label: str) -> Fraction:
    value = stream.get("avg_frame_rate")
    require(isinstance(value, str) and value not in {"0/0", "N/A"},
            f"{label} has no measured avg_frame_rate; observed {value!r}")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise AssertionError(f"{label} avg_frame_rate is invalid: {value!r}") from exc


def rate_matches(value: Any, expected: Fraction, label: str) -> None:
    require(isinstance(value, str), f"{label} has no persisted average frame rate")
    try:
        observed = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise AssertionError(f"{label} average frame rate is invalid: {value!r}") from exc
    require(observed == expected, f"{label} average frame rate {observed} differs from media rate {expected}")


def observed_duration(probe: dict[str, Any], stream: dict[str, Any], label: str) -> float:
    value = stream.get("duration") or probe.get("format", {}).get("duration")
    require(value is not None, f"{label} has no measured duration")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"{label} duration is invalid: {value!r}") from exc


def sibling_relative(relative: str, name: str) -> str:
    parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
    return f"{parent}/{name}" if parent else name


def read_json(root: ProjectRoot, relative: str) -> dict[str, Any]:
    value = json.loads(root.read_text(relative, encoding="utf-8"))
    require(isinstance(value, dict), f"persisted JSON is not an object: {relative}")
    return value


def decode_frame(path: Path, index: int) -> bytes:
    # Decode to stdout; the verifier creates no files and performs no rendering.
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{index})",
         "-vsync", "0", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True, capture_output=True,
    )
    return proc.stdout


def contained_file(root: ProjectRoot, relative: str) -> Path:
    path = root.resolve(relative)
    require(path.is_file(), f"persisted artifact is missing: {relative}")
    return path


def find_project(run_dir: Path) -> Path:
    # Discover from persisted layout; evidence.json is deliberately not a locator.
    found = [p.parent for p in run_dir.rglob("project.json") if p.is_file()]
    require(len(found) == 1, f"expected one project.json beneath run, found {len(found)}")
    return found[0]


def verify(run_relative: str) -> list[str]:
    repo_root = ProjectRoot(REPO)
    run_dir = repo_root.resolve(run_relative)
    require(run_dir.is_dir(), f"run directory does not exist: {run_relative}")
    project_path = find_project(run_dir)
    project_root = ProjectRoot(project_path)
    store = ProjectStore(project_root)
    findings: list[str] = []

    if not (run_dir / "evidence.json").is_file():
        findings.append("evidence.json absent; independent checks continued from persisted project")

    # Locate the generated asset by its persisted registry entry, not evidence claims.
    asset = store.load(Asset, "entity")
    require(asset is not None, "registered generated asset 'entity' was not found")
    assert asset is not None
    require(asset.file_path and asset.checksum, "registered asset lacks path or checksum")
    asset_path = contained_file(project_root, asset.file_path)
    actual_asset_hash = sha256(asset_path)
    require(actual_asset_hash == asset.checksum, "registered asset checksum does not match copied bytes")
    source = Path(r"D:\Work\comfyui-trellis\ComfyUI\output\3D\entity_00001_.glb")
    if source.is_file():
        require(sha256(source) == actual_asset_hash, "registered asset bytes differ from source generator bytes")
    else:
        findings.append("original source GLB unavailable; project copy hash verified only")
    retained_provenance = run_dir / "source_PROVENANCE.json"
    require(retained_provenance.is_file(), "retained source_PROVENANCE.json is missing from the run")
    provenance = json.loads(retained_provenance.read_text(encoding="utf-8"))
    license_note = provenance.get("licence_note")
    require(isinstance(license_note, str) and bool(license_note),
            "retained PROVENANCE.json has no non-empty licence_note")
    require(asset.model_or_voice_license == license_note,
            "registered model_or_voice_license does not exactly match retained PROVENANCE licence_note")
    if source.is_file():
        external_provenance = Path(r"D:\Work\trellis2-gguf\PROVENANCE.json")
        if external_provenance.is_file():
            require(sha256(external_provenance) == sha256(retained_provenance),
                    "retained PROVENANCE.json differs from source generator PROVENANCE.json")
        else:
            findings.append("LIMIT: source generator PROVENANCE.json unavailable; retained copy and registered licence note verified")
    require(asset.generated, "asset generated flag is false")
    require("internal" in asset.license.lower() and "evaluation" in asset.license.lower(),
            "asset license does not record internal evaluation restriction")
    require("quant" in asset.model_or_voice_license.lower() and "verify" in asset.model_or_voice_license.lower(),
            "asset model_or_voice_license omits the quantisation or distribution caveat")
    findings.append(f"asset {asset.asset_id}: copied bytes SHA-256 {actual_asset_hash} verified")

    # Find the persisted shot and its bound Location.
    episodes_path = project_root.resolve("episodes")
    episode_ids = sorted(path.name for path in episodes_path.iterdir() if path.is_dir())
    episodes = [store.load(Episode, item) for item in episode_ids]
    shots: list[Shot] = []
    for episode in episodes:
        shots.extend(store.load(Shot, shot_id, episode_id=episode.episode_id)
                     for shot_id in episode.shot_ids)
    target = [shot for shot in shots if shot.anomaly is not None
              and getattr(shot.anomaly, "figure_asset_id", None) == asset.asset_id]
    require(len(target) == 1, f"expected one shot naming the figure asset, found {len(target)}")
    shot = target[0]
    require(bool(shot.location_id), "shot has no Location binding")
    location = store.load(Location, shot.location_id)
    require(location.scene_look == "low_key", "persisted Location is not low_key")

    outputs = store.list_shot_outputs(shot.shot_id, episode_id=shot.episode_id,
                                      record_version=shot.version)
    scene_receipts = [o for o in outputs if o.kind is ShotOutputKind.SCENE]
    master_receipts = [o for o in outputs if o.kind is ShotOutputKind.MASTER]
    degraded_receipts = [o for o in outputs if o.kind is ShotOutputKind.DEGRADED]
    require(scene_receipts and master_receipts and degraded_receipts,
            "current shot version lacks scene, master, or degraded output receipt")
    scene_output, master_output, degraded_output = scene_receipts[-1], master_receipts[-1], degraded_receipts[-1]
    for output in (scene_output, master_output, degraded_output):
        require(output.record_version == shot.version,
                f"{output.kind.value} receipt is not bound to current shot version")

    scene_file = contained_file(project_root, scene_output.relative_path)
    require(sha256(scene_file) == scene_output.content_sha256, "scene receipt hash mismatch")
    scene_doc = json.loads(scene_file.read_text(encoding="utf-8"))
    plan = scene_doc.get("plan", {})
    scene = plan.get("scene", {})
    objects = scene.get("objects", [])
    figures = [obj for obj in objects if obj.get("name") == "FR20AnomalyFigure"]
    require(len(figures) == 1, f"persisted scene has {len(figures)} FR20AnomalyFigure objects")
    require(figures[0].get("asset_id") == asset.asset_id, "scene figure references wrong asset")
    require(figures[0].get("asset_checksum") == actual_asset_hash,
            "persisted figure asset_checksum does not match registered asset bytes")
    require(figures[0].get("asset_file_path") == asset.file_path,
            "persisted figure asset_file_path does not match registered asset path")
    proxies = [obj for obj in objects if obj.get("name", "").startswith("FR20AnomalyFigure")
               and obj.get("name") != "FR20AnomalyFigure"]
    require(not proxies, f"persisted scene contains proxy figure parts: {[o.get('name') for o in proxies]}")
    findings.append("persisted scene contains one asset-backed FR20AnomalyFigure and no proxy parts")
    scene_run_id = scene_output.relative_path.split("/")[-2]
    require(plan.get("run_id") == scene_run_id,
            "scene plan run_id does not match the run directory named by its receipt")

    master_path = contained_file(project_root, master_output.relative_path)
    degraded_path = contained_file(project_root, degraded_output.relative_path)
    require(sha256(master_path) == master_output.content_sha256, "master receipt hash mismatch")
    require(sha256(degraded_path) == degraded_output.content_sha256, "degraded receipt hash mismatch")
    master_probe, degraded_probe = ffprobe(master_path), ffprobe(degraded_path)
    ms, ds = master_probe["streams"][0], degraded_probe["streams"][0]
    expected_master_size = (plan.get("width"), plan.get("height"))
    require((ms["width"], ms["height"]) == expected_master_size,
            f"master dimensions {(ms['width'], ms['height'])} differ from persisted plan {expected_master_size}")
    master_rate = observed_rate(ms, "master stream")
    require(master_rate == Fraction(plan.get("fps", 0), 1),
            f"master avg_frame_rate {master_rate} differs from persisted plan fps={plan.get('fps')}")
    master_count = ms.get("nb_read_frames") or ms.get("nb_frames")
    degraded_count = ds.get("nb_read_frames") or ds.get("nb_frames")
    master_frames = int(master_count) if str(master_count).isdigit() else None
    degraded_frames = int(degraded_count) if str(degraded_count).isdigit() else None
    require(master_frames is not None and master_frames > 0, "master decoded frame count is missing or zero")
    require(degraded_frames is not None and degraded_frames > 0,
            "degraded decoded frame count is missing or zero")
    plan_fps = int(plan.get("fps", 0))
    expected_master_frames = math.ceil(shot.duration_sec * plan_fps - 1e-9)
    require(plan.get("frame_end") == expected_master_frames,
            f"scene plan frame_end={plan.get('frame_end')} differs from shot duration-derived {expected_master_frames}")
    require(master_frames == expected_master_frames,
            f"decoded master has {master_frames} frames; shot duration and scene plan require {expected_master_frames}")
    master_duration = observed_duration(master_probe, ms, "master stream")
    require(abs(master_duration - master_frames / float(master_rate)) <= 1 / float(master_rate),
            f"master duration {master_duration:.6f}s is outside one-frame tolerance for {master_frames} frames")

    # Compare delivery metadata against the persisted profile, not evidence.json.
    project = store.load(Project)
    profile_name = shot.post_profile or project.output_profile
    post_profile = store.load(PostProfile, profile_name)
    if post_profile.storage_resolution:
        require((ds["width"], ds["height"]) == tuple(post_profile.storage_resolution),
                "degraded dimensions differ from persisted PostProfile storage_resolution")
    require(post_profile.output_fps is not None, "persisted PostProfile has no output_fps")
    degraded_rate = observed_rate(ds, "degraded stream")
    require(degraded_rate == Fraction(post_profile.output_fps, 1),
            f"degraded avg_frame_rate {degraded_rate} differs from persisted output_fps={post_profile.output_fps}")

    # Persisted post-profile metadata binds the actual delivery to its source master.
    degraded_meta_rel = sibling_relative(degraded_output.relative_path, "metadata.json")
    degraded_meta = read_json(project_root, degraded_meta_rel)
    require(degraded_meta.get("status") == "completed", "degradation metadata is not completed")
    require(degraded_meta.get("run_id") == degraded_output.relative_path.split("/")[-2],
            "degradation metadata run_id differs from receipt run directory")
    require(degraded_meta.get("shot_id") == shot.shot_id and
            degraded_meta.get("shot_version") == shot.version,
            "degradation metadata does not bind the current shot version")
    require(degraded_meta.get("master_path") == master_output.relative_path,
            "degradation metadata does not name the current master receipt as input")
    require(degraded_meta.get("output_path") == degraded_output.relative_path,
            "degradation metadata output path differs from degraded receipt")
    require(degraded_meta.get("profile_name") == post_profile.name and
            degraded_meta.get("profile_semver") == post_profile.semver,
            "degradation metadata profile identity differs from persisted PostProfile")
    require(degraded_meta.get("filter_graph_hash") == degraded_output.filter_graph_hash,
            "degradation metadata filter graph hash differs from degraded receipt")
    require(degraded_meta.get("output_sha256") == degraded_output.content_sha256,
            "degradation metadata output hash differs from degraded receipt")
    require(degraded_meta.get("predicted_frames") == degraded_frames,
            "degradation metadata predicted frame count differs from decoded output")
    output_probe = degraded_meta.get("output_probe", {})
    require(isinstance(output_probe, dict) and output_probe.get("frame_count") == degraded_frames,
            "persisted output_probe frame count differs from decoded degraded output")
    rate_matches(output_probe.get("avg_frame_rate"), degraded_rate,
                 "persisted output_probe")
    require((output_probe.get("width"), output_probe.get("height")) == (ds["width"], ds["height"]),
            "persisted output_probe dimensions differ from ffprobe on delivered media")
    require(degraded_output.profile_semver == post_profile.semver and
            degraded_output.pinned_binaries == post_profile.pinned_binaries,
            "degraded receipt profile/binary provenance differs from persisted PostProfile")
    degraded_duration = observed_duration(degraded_probe, ds, "degraded stream")
    require(abs(degraded_duration - degraded_frames / float(degraded_rate)) <= 1 / float(degraded_rate),
            f"degraded duration {degraded_duration:.6f}s is outside one-frame tolerance for {degraded_frames} frames")
    require(abs(degraded_duration - shot.duration_sec) <= 1 / float(degraded_rate),
            f"degraded duration {degraded_duration:.6f}s differs from persisted shot duration {shot.duration_sec}s beyond one output frame")
    expected_delivery_count = math.ceil(shot.duration_sec * post_profile.output_fps - 1e-9)
    # A drop schedule can remove frames explicitly; the persisted prediction below is
    # authoritative in that case, while this duration-derived value remains a bound.
    drop_count = sum(int(event.get("count", 0)) for event in degraded_meta.get("artifact_schedule", [])
                     if isinstance(event, dict) and event.get("type") == "drop")
    require(expected_delivery_count - drop_count <= degraded_frames <= expected_delivery_count,
            f"decoded delivery count {degraded_frames} is inconsistent with shot duration/profile fps "
            f"({expected_delivery_count} frames before {drop_count} scheduled drops)")
    source_probe = degraded_meta.get("source_probe", {})
    if isinstance(source_probe, dict):
        require(source_probe.get("width") == ms["width"] and source_probe.get("height") == ms["height"],
                "degradation source_probe dimensions differ from decoded master")
        rate_matches(source_probe.get("avg_frame_rate"), master_rate,
                     "degradation source_probe")
        require(source_probe.get("frame_count") == master_frames,
                "degradation source_probe frame count differs from decoded master")

    # Renderer metadata, persisted plan and master assembly argv provide the available
    # render lineage. GenerationRun itself is returned in memory and has no store layout.
    render_meta_rel = sibling_relative(master_output.relative_path, "metadata.json")
    render_meta = read_json(project_root, render_meta_rel)
    require(render_meta.get("status") == "completed", "renderer metadata is not completed")
    require(render_meta.get("run_id") == master_output.relative_path.split("/")[-3],
            "renderer metadata run_id differs from master receipt run directory")
    require(render_meta.get("shot_id") == shot.shot_id and
            render_meta.get("shot_version") == shot.version,
            "renderer metadata does not bind the current shot version")
    require(render_meta.get("plan_sha256"), "renderer metadata has no plan hash")
    renderer_argv = render_meta.get("argv")
    require(isinstance(renderer_argv, list) and renderer_argv,
            "renderer metadata has no recorded Blender argv")
    plan_path = Path(str(renderer_argv[-1]))
    try:
        plan_rel = plan_path.resolve().relative_to(project_root.path.resolve()).as_posix()
    except ValueError as exc:
        raise AssertionError("renderer argv plan path escapes project root") from exc
    plan_file = contained_file(project_root, plan_rel)
    require(sha256(plan_file) == render_meta["plan_sha256"],
            "renderer plan file hash differs from renderer metadata")
    render_plan = json.loads(plan_file.read_text(encoding="utf-8"))
    require(render_plan.get("run_id") == render_meta.get("run_id"),
            "renderer plan run_id differs from renderer metadata")
    for field in ("shot_id", "shot_version", "anomaly", "event_sec", "width", "height", "fps", "frame_end"):
        require(render_plan.get(field) == plan.get(field),
                f"renderer plan field {field!r} differs from the persisted scene plan")
    render_scene = render_plan.get("scene", {})
    render_objects = render_scene.get("objects", []) if isinstance(render_scene, dict) else []
    normalized_render_scene = dict(render_scene) if isinstance(render_scene, dict) else {}
    normalized_objects = [dict(item) for item in render_objects if isinstance(item, dict)]
    for item in normalized_objects:
        item.pop("asset_path", None)  # render plan adds the contained absolute import path
    normalized_render_scene["objects"] = normalized_objects
    require(normalized_render_scene == scene,
            "renderer plan scene differs from persisted scene beyond resolved asset import paths")
    render_figure = next((item for item in render_objects
                          if isinstance(item, dict) and item.get("name") == "FR20AnomalyFigure"), None)
    require(render_figure is not None and
            Path(render_figure.get("asset_path", "")).resolve() == asset_path.resolve(),
            "renderer plan did not resolve the registered figure import path to retained project bytes")
    script_index = renderer_argv.index("--python") + 1 if "--python" in renderer_argv else -1
    require(script_index > 0, "renderer argv omits the Blender worker script")
    script_path = Path(str(renderer_argv[script_index]))
    try:
        script_rel = script_path.resolve().relative_to(project_root.path.resolve()).as_posix()
    except ValueError as exc:
        raise AssertionError("renderer argv Blender script path escapes project root") from exc
    script_file = contained_file(project_root, script_rel)
    require(sha256(script_file) == render_meta.get("script_sha256"),
            "renderer worker script hash differs from renderer metadata")
    master_assembly = render_meta.get("master_assembly", {})
    assembly_argv = master_assembly.get("argv") if isinstance(master_assembly, dict) else None
    require(isinstance(assembly_argv, list) and assembly_argv,
            "renderer metadata has no master FFmpeg argv")
    require(Path(assembly_argv[-1]).resolve() == master_path.resolve(),
            "master FFmpeg argv output does not match the master receipt file")
    require(master_assembly.get("frame_count") == master_frames and
            master_assembly.get("frame_rate") == plan_fps,
            "master FFmpeg metadata frame count/rate differs from decoded master and plan")
    for option, expected in (("-framerate", str(plan_fps)), ("-frames:v", str(master_frames))):
        require(option in assembly_argv and assembly_argv[assembly_argv.index(option) + 1] == expected,
                f"master FFmpeg argv does not bind {option} to {expected}")
    require("-i" in assembly_argv and assembly_argv[assembly_argv.index("-i") + 1] == str(
        (project_root.path / master_assembly.get("input_pattern", "")).resolve()
    ), "master FFmpeg argv input does not match persisted assembly input_pattern")
    findings.append("scene, renderer plan/worker metadata, master FFmpeg argv and delivery metadata link through persisted paths and hashes")
    findings.append("LIMIT: GenerationRun is not persisted as a project record; verifier checked retained renderer and degradation metadata instead")
    findings.append("LIMIT: post-profile metadata persists the filter graph and hash but not the exact FFmpeg argv")

    neutral = decode_frame(master_path, 0)
    timing = getattr(shot.anomaly, "timing", None)
    require(timing is not None and shot.event_sec is not None,
            "persisted anomaly shot lacks event timing")
    midpoint_sec = shot.event_sec + timing.duration_sec / 2
    anomaly_index = max(0, math.ceil(midpoint_sec * 60 - 1e-9))
    if master_frames is not None:
        anomaly_index = min(master_frames - 1, anomaly_index)
    anomaly = decode_frame(master_path, anomaly_index)
    require(neutral and anomaly, "could not decode neutral/anomaly master frames")
    require(neutral != anomaly, "decoded neutral and anomaly master frames are pixel-identical")
    findings.append(f"master stream {ms['width']}x{ms['height']} {ms.get('avg_frame_rate')} fps, "
                    f"{master_frames or 'unknown'} frames; delivered stream {ds['width']}x{ds['height']} "
                    f"{ds.get('avg_frame_rate')} fps, {degraded_frames or 'unknown'} frames")
    if master_frames == 6 and degraded_frames == 1:
        findings.append("LIMIT: six-frame master degraded to one output frame; delivered motion is not established")

    # Read persisted job journals directly. Errors and lease ownership are read from
    # journal snapshots; SQLite is intentionally not used as the authority.
    jobs_dir = project_root.resolve("state/jobs")
    job_ids = [p.stem for p in jobs_dir.glob("*.jsonl")] if jobs_dir.is_dir() else []
    job_ids += [p.stem for p in jobs_dir.glob("*.json")] if jobs_dir.is_dir() else []
    jobs = [read_job(project_root, job_id) for job_id in sorted(set(job_ids))]
    related = [job for job in jobs if job and job.get("shot_id") == shot.shot_id]
    require(related, "no persisted job snapshots found for target shot")
    for job in related:
        require(job.get("state") == "completed", f"job {job.get('job_id')} state is {job.get('state')}")
        require(not job.get("error_kind") and not job.get("error_message"),
                f"completed job {job.get('job_id')} retains terminal error fields")
        require(job.get("lease_holder_pid") is None,
                f"job {job.get('job_id')} still records a GPU lease holder")
    expected_job_runs = {
        "scene": scene_run_id,
        "render": render_meta["run_id"],
        "degrade": degraded_meta["run_id"],
    }
    for job_type, run_id in expected_job_runs.items():
        matches = [job for job in related if job.get("type") == job_type and job.get("run_id") == run_id]
        require(matches, f"persisted {job_type} job does not link to artifact run_id {run_id}")
    findings.append(f"{len(related)} persisted jobs for shot completed without terminal errors or held leases")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-relative", required=True, help="run directory relative to repository root")
    args = parser.parse_args()
    try:
        for finding in verify(args.run_relative):
            label = "LIMIT" if finding.startswith("LIMIT:") else "PASS"
            print(f"{label}: {finding.removeprefix('LIMIT: ').strip()}")
    except Exception as exc:  # noqa: BLE001 - CLI must report verification failures and exit nonzero
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
