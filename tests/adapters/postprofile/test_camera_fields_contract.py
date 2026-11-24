"""Tests for orchestrator/adapters/postprofile/CAMERA_FIELDS_CONTRACT.md (step 3a).

Written from the contract, not from the implementation. Requirement groups:

R1  typed camera fields, neutral values, legacy ``bitrate_kbps`` migration
R2  one derived spec: camera fields win, stage names and order, validation cross-check
R3  determinism and one measured effect per field, with real FFmpeg on generated clips
R4  the agent-facing operation summaries

Seams assumed (see the report): ``postprofile_spec_from_camera(camera, spec)`` in
``orchestrator.service.runner`` returns the derived ``PostProfileSpec``;
``validate_profile`` / ``build_filter_graph`` in the postprofile adapter;
``PostProfileAdapter.degrade`` applied to a request whose ``profile`` is the derived spec,
exactly as ``AdapterRunner`` does it; ``OPERATIONS`` in ``orchestrator.api.routes``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import statistics
import subprocess
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.adapters.postprofile.profile import (
    EncodeSpec,
    OverlaySpec,
    PostProfileSpec,
    build_filter_graph,
    camera_profile_impact,
    validate_profile,
)
from orchestrator.adapters.postprofile.runner import DegradeRequest, PostProfileAdapter
from orchestrator.api.routes import OPERATIONS
from orchestrator.domain.base import Invalidation
from orchestrator.domain.entities import (
    CameraProfile,
    PostProfile,
    PostStage,
    Project,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.domain.invalidation import field_invalidation
from orchestrator.domain.seeds import INT32_MAX, derive_seed
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings
from orchestrator.service.dependencies import create_project
from orchestrator.service.runner import postprofile_spec_from_camera
from orchestrator.store.json_store import ProjectStore

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="measured effect tests need real ffmpeg and ffprobe"
)

REFUSED = (ValueError, JobError)  # pydantic.ValidationError is a ValueError

NINE_FIELDS = (
    "bitrate_kbps", "decimation_pattern", "noise_model", "chroma_smearing", "block_artifacts",
    "sharpening", "grade_white_balance", "overlay_model", "analog_artifacts",
)
NEUTRAL = {
    "bitrate_kbps": None, "decimation_pattern": None, "noise_model": {}, "chroma_smearing": 0,
    "block_artifacts": 0, "sharpening": 0, "grade_white_balance": {}, "overlay_model": {},
    "analog_artifacts": {},
}
EPOCH = 1131984000  # 2005-11-14 16:00:00 UTC, the repository's fixture clock


# ---------------------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------------------

def _camera(*, fps: float = 12.0, resolution: tuple[int, int] = (640, 480), **fields) -> CameraProfile:
    return CameraProfile(
        camera_id="CAM03", profile_name="dvr-2005", fps=fps, storage_resolution=resolution,
        display_aspect_ratio="4:3", **fields,
    )


def _stages(width: int, height: int, fps: int, *, overlay: bool, degradation: bool) -> list[PostStage]:
    stages = [
        PostStage(name="resample", filter="scale", params={"w": width, "h": height, "flags": "bicubic"}),
        PostStage(name="decimate", filter="fps", params={"fps": fps}),
        PostStage(name="restructure", filter="select", params={"from_schedule": "drop+freeze"}),
        PostStage(name="hold_frozen", filter="loop", params={"from_schedule": "freeze"}),
        PostStage(name="clock_timebase", filter="setpts", params={"from_schedule": "clock"}),
    ]
    if overlay:
        stages.append(PostStage(
            name="overlay_burnin", filter="drawtext",
            params={"camera_id": "CAM 03", "clock_epoch": EPOCH, "fontsize": 16, "x": 8, "y": "h-24"},
            composite_stage="in_recording",
        ))
    stages.append(PostStage(name="output_timebase", filter="setpts",
                            params={"timebase": "1/output_fps", "pts": "N"}))
    if degradation:
        stages += [
            PostStage(name="bad_white_balance", filter="eq",
                      params={"brightness": 0.02, "contrast": 1.08, "saturation": 0.75}),
            PostStage(name="sensor_noise", filter="noise", params={"alls": 12, "allf": "t+u"},
                      seed_source="shot"),
            PostStage(name="cheap_sharpen", filter="unsharp",
                      params={"luma_msize_x": 5, "luma_msize_y": 5, "luma_amount": 0.8}),
        ]
    return stages


def _spec(camera: CameraProfile, *, pin: str = "ffmpeg version fixture", overlay: bool = False,
          degradation: bool = False) -> PostProfileSpec:
    """The stored PostProfile as an adapter spec, before camera fields are applied."""
    width, height = camera.storage_resolution
    fps = int(camera.fps)
    identity = PostProfile(
        name="dvr-2005", semver="0.2.0", pinned_binaries={"ffmpeg": pin},
        stages=_stages(width, height, fps, overlay=overlay, degradation=degradation),
    )
    return PostProfileSpec(
        identity=identity, output_fps=fps, storage_resolution=(width, height),
        sample_aspect_ratio="1/1",
        overlay=(OverlaySpec(camera_id="CAM 03", clock_epoch=EPOCH, fontsize=16, x=8, y="h-24",
                             placement="in_recording") if overlay else None),
        encode=EncodeSpec(codec="libx264", preset="medium", crf=30, pix_fmt="yuv420p", threads=1),
    )


def _derive(camera: CameraProfile, *, overlay: bool = False, degradation: bool = False) -> PostProfileSpec:
    return postprofile_spec_from_camera(camera, _spec(camera, overlay=overlay, degradation=degradation))


def _checked(spec: PostProfileSpec, camera: CameraProfile, *, rate: int = 60, frames: int | None = None):
    return validate_profile(spec, camera, master_frames=frames or rate * 5,
                            master_rate=Fraction(rate, 1))


def _graph(spec: PostProfileSpec, camera: CameraProfile, *, shot_id: str = "S003",
           seed: int = 20051114, rate: int = 60, frames: int | None = None) -> str:
    return build_filter_graph(_checked(spec, camera, rate=rate, frames=frames), shot_id=shot_id,
                              degrade_seed=seed, font_path="font.ttf")


def _stage_names(spec: PostProfileSpec) -> list[str]:
    return [stage.name for stage in spec.identity.stages]


def _camera_overrides(spec: PostProfileSpec) -> list[dict]:
    """``provenance.camera_overrides`` of the derived spec.

    The contract says "the derived spec's provenance" without saying whether that is a
    field of the adapter spec or of its PostProfile identity, so both are read.
    """
    provenance = getattr(spec, "provenance", None)
    if provenance is None:
        provenance = spec.identity.provenance
    if isinstance(provenance, str):
        try:
            provenance = json.loads(provenance)
        except ValueError:
            return []
    if isinstance(provenance, dict):
        return list(provenance.get("camera_overrides", []))
    return []


def _refused_message(*steps) -> str:
    """Run steps in order; the first to raise must be a refusal. Return its message."""
    for step in steps:
        try:
            step()
        except REFUSED as exc:
            return str(exc).lower()
    pytest.fail("every step accepted a value the contract says is refused")


# ---------------------------------------------------------------------------------------
# R1: typed fields
# ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("field", NINE_FIELDS)
def test_the_nine_fields_stay_post_only_and_never_trigger_a_rerender(field):
    assert field_invalidation(CameraProfile, field) is Invalidation.POST_ONLY
    before = _camera()
    value = {
        "bitrate_kbps": 500, "decimation_pattern": "5:5:6:4", "noise_model": {"luma": 10},
        "chroma_smearing": 2.0, "block_artifacts": 0.5, "sharpening": 1.0,
        "grade_white_balance": {"temperature_k": 4000}, "overlay_model": {"fontsize": 20},
    }.get(field)
    if value is None:  # analog_artifacts uses nested groups; its step 3c invalidation is checked separately.
        return
    after = before.model_copy(update={field: value, "version": before.version + 1})
    shot = Shot(shot_id="S003", episode_id="E01", camera_id="CAM03", duration_sec=5)
    assert camera_profile_impact(before, after, [shot]) == ()


def test_defaults_are_the_neutral_values_and_bitrate_is_off_by_default():
    camera = _camera()
    assert camera.bitrate_kbps is None
    assert camera.decimation_pattern is None
    assert camera.chroma_smearing == 0
    assert camera.block_artifacts == 0
    assert camera.sharpening == 0
    dumped = camera.model_dump(mode="json")
    explicit = _camera(**NEUTRAL).model_dump(mode="json")
    assert explicit == dumped, "explicit neutral values must parse to exactly the defaults"


@pytest.mark.parametrize("field,value", [
    ("bitrate_kbps", 16), ("bitrate_kbps", 800), ("bitrate_kbps", 20000),
    ("decimation_pattern", "5"), ("decimation_pattern", "5:5:6:4"),
    ("decimation_pattern", ":".join(["1"] * 16)),
    ("noise_model", {"luma": 0, "chroma": 100}),
    ("noise_model", {"luma": 50, "chroma": 20, "temporal": False, "uniform": False}),
    ("chroma_smearing", 16), ("chroma_smearing", 0.5),
    ("block_artifacts", 1), ("block_artifacts", 0.25),
    ("sharpening", 3), ("sharpening", 0.8),
    ("grade_white_balance", {"temperature_k": 1000}),
    ("grade_white_balance", {"temperature_k": 40000, "red_gain": 4, "green_gain": 0.25,
                             "blue_gain": 1, "brightness": -1, "contrast": 3, "saturation": 0}),
    ("overlay_model", {"camera_id_text": "CAM 7", "clock_epoch": 0, "clock_format": "%d/%m/%y %I:%M:%S %p",
                       "fontsize": 6, "x": 0, "y": 0, "placement": "on_playback"}),
    ("overlay_model", {"fontsize": 96, "placement": "in_recording"}),
])
def test_in_range_values_are_accepted_and_round_trip_through_json(field, value):
    camera = _camera(**{field: value})
    revived = CameraProfile.model_validate_json(camera.model_dump_json())
    assert revived.model_dump(mode="json") == camera.model_dump(mode="json")
    if not isinstance(value, dict):
        assert getattr(camera, field) == value


@pytest.mark.parametrize("field,value", [
    ("bitrate_kbps", 15), ("bitrate_kbps", 0), ("bitrate_kbps", -1), ("bitrate_kbps", 20001),
    ("bitrate_kbps", "fast"),
    ("decimation_pattern", ""), ("decimation_pattern", "0:5"), ("decimation_pattern", "-5:5"),
    ("decimation_pattern", "a:b"), ("decimation_pattern", "5::5"), ("decimation_pattern", "5:5:"),
    ("decimation_pattern", " 5:5"), ("decimation_pattern", "5.5:5"),
    ("decimation_pattern", ":".join(["1"] * 17)), ("decimation_pattern", 5),
    ("noise_model", {"luma": -1}), ("noise_model", {"luma": 101}), ("noise_model", {"chroma": -1}),
    ("noise_model", {"chroma": 100.5}), ("noise_model", {"luma": "loud"}),
    ("noise_model", {"hue": 5}), ("noise_model", {"luma": 5, "grain": 1}), ("noise_model", [1, 2]),
    ("chroma_smearing", -0.1), ("chroma_smearing", 16.1), ("chroma_smearing", float("nan")),
    ("chroma_smearing", float("inf")),
    ("block_artifacts", -0.01), ("block_artifacts", 1.01),
    ("sharpening", -0.1), ("sharpening", 3.01),
    ("grade_white_balance", {"temperature_k": 999}), ("grade_white_balance", {"temperature_k": 40001}),
    ("grade_white_balance", {"red_gain": 0.24}), ("grade_white_balance", {"green_gain": 4.01}),
    ("grade_white_balance", {"blue_gain": 0}), ("grade_white_balance", {"brightness": 1.01}),
    ("grade_white_balance", {"brightness": -1.01}), ("grade_white_balance", {"contrast": -0.1}),
    ("grade_white_balance", {"contrast": 3.01}), ("grade_white_balance", {"saturation": 3.01}),
    ("grade_white_balance", {"saturation": -0.01}), ("grade_white_balance", {"gamma": 1.2}),
    ("overlay_model", {"fontsize": 5}), ("overlay_model", {"fontsize": 97}),
    ("overlay_model", {"fontsize": "big"}), ("overlay_model", {"x": -1}), ("overlay_model", {"y": -1}),
    ("overlay_model", {"clock_epoch": -1}), ("overlay_model", {"placement": "somewhere"}),
    ("overlay_model", {"camera_id_text": 7}),
])
def test_out_of_range_wrong_typed_and_unknown_values_are_refused(field, value):
    with pytest.raises(ValidationError):
        _camera(**{field: value})


@pytest.mark.parametrize("extra", [
    {"drift_rate": 0.01}, {"drift_rate": 0}, {"clock_instability": 1}, {"instability": True},
    {"instability_events": []}, {"unstable_intervals": [[1, 2]]},
])
def test_overlay_model_refuses_drift_and_instability_keys(extra):
    # Clock drift and timestamp-unstable events were deferred to P1 on 2026-11-05.
    with pytest.raises(ValidationError):
        _camera(overlay_model={"fontsize": 16, **extra})


@pytest.mark.parametrize("clock_format", [
    "%Y-%m-%d %H:%M:%S", "%d/%m/%y %I:%M:%S %p", "%a %b %d [%j]", "(%H.%M,%S)", "Time: %H:%M",
    "%Y %y %m %d %H %M %S %p %I %b %a %j",
])
def test_clock_format_accepts_the_documented_character_set(clock_format):
    camera = _camera(overlay_model={"clock_format": clock_format})
    assert camera.model_dump(mode="json")["overlay_model"]["clock_format"] == clock_format


@pytest.mark.parametrize("clock_format", [
    "%H:%M:%S'", "%H:%M:%S':fontsize=99", "it's %H", "back\\slash", "%H\\:%M", "%{pts}", "%{",
    "%{metadata\\:x}", "%H;%M", "%H;movie=x", "%H:%M:%S,drawtext=text=evil", "text=%H", '%H"',
    "%H\n%M", "%%", "%Z", "%z", "%e", "100%", "%", "%H{%M}", "%H|%M", "%H`%M", "%H$M",
])
def test_clock_format_refuses_drawtext_escaping_attempts_and_unlisted_conversions(clock_format):
    with pytest.raises(ValidationError):
        _camera(overlay_model={"clock_format": clock_format})


def test_typed_non_empty_analog_artifacts_derives_the_ntsc_stage():
    camera = _camera(analog_artifacts={"head_switching": {"height": 8}})
    derived = _derive(camera)
    checked = _checked(derived, camera, rate=60)
    filters = [stage.filter for stage in derived.identity.stages]

    assert camera.analog_artifacts.active
    assert filters.index("camera_analog_artifacts") == len(filters) - 1
    assert "camera_analog_artifacts" in [stage.filter for stage in checked.profile.identity.stages]


def test_decimation_pattern_whose_mean_disagrees_with_source_over_fps_is_refused_naming_rates():
    # 60 fps source decimated to 12 fps spans 5 source frames per output frame; this
    # pattern's mean is 4.
    camera = _camera(fps=12.0, decimation_pattern="4:4:4:4")
    message = _refused_message(lambda: _checked(_derive(camera), camera, rate=60))
    assert "12" in message
    assert any(token in message for token in ("60", "5", "4")), message
    # The same camera with a matching pattern validates.
    good = _camera(fps=12.0, decimation_pattern="5:5:6:4")
    _checked(_derive(good), good, rate=60)


def test_decimation_pattern_mean_must_match_exactly_not_approximately():
    camera = _camera(fps=12.0, decimation_pattern="5:5:5:6")  # mean 5.25
    _refused_message(lambda: _checked(_derive(camera), camera, rate=60))


# ---- legacy bitrate_kbps ---------------------------------------------------------------

def _rewrite_camera_record(root: ProjectRoot, camera_id: str, **changes) -> None:
    directory = root.resolve(f"camera_profiles/{camera_id}")
    files = sorted(directory.glob("v*.json"))
    assert files, "store.create wrote no camera profile file to rewrite"
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(changes)
        root.write_text(path.relative_to(root.path).as_posix(), json.dumps(record))


@pytest.mark.parametrize("legacy_schema_version", [1, 2])
def test_camera_profile_on_disk_before_the_change_reads_bitrate_as_none(tmp_path, legacy_schema_version):
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    store.create(CameraProfile(camera_id="legacy", profile_name="legacy"))
    _rewrite_camera_record(root, "legacy", schema_version=legacy_schema_version, bitrate_kbps=800)

    loaded = store.load(CameraProfile, "legacy")

    assert loaded.bitrate_kbps is None, (
        "a camera profile written before bitrate_kbps took effect must not start encoding "
        "at 800 kbps silently"
    )


@pytest.mark.parametrize("claimed_schema_version", [1, 2])
def test_a_request_carrying_an_old_schema_version_keeps_its_explicit_bitrate(claimed_schema_version):
    # Found in the 2026-11-22 real run: revising a legacy profile by editing its stored
    # JSON (schema_version 1) and setting bitrate_kbps=250 stored None. The legacy mapping
    # belongs to the store's read of an old record, never to an incoming request.
    record = _camera().model_dump(mode="json")
    record.update(schema_version=claimed_schema_version, bitrate_kbps=250)

    camera = CameraProfile.model_validate(record)

    assert camera.bitrate_kbps == 250
    assert camera.schema_version > 2


def test_new_camera_profile_writes_record_the_new_schema_version_and_keep_their_bitrate(tmp_path):
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    current = CameraProfile(camera_id="fresh", profile_name="fresh", bitrate_kbps=500)
    assert current.schema_version > 2, "CameraProfile's schema version must be bumped past 2"
    store.create(current)

    on_disk = json.loads(sorted(root.resolve("camera_profiles/fresh").glob("v*.json"))[0].read_text("utf-8"))
    assert on_disk["schema_version"] == current.schema_version
    assert store.load(CameraProfile, "fresh").bitrate_kbps == 500


def test_seeded_default_profiles_set_none_of_the_nine_fields(tmp_path):
    store = ProjectStore(ProjectRoot(tmp_path))
    create_project(store, Project(project_id="p1", title="Seeded"))
    defaults = _camera().model_dump(mode="json")
    for camera_id in ("dvr-2005", "low-light-bw"):
        seeded = store.load(CameraProfile, camera_id).model_dump(mode="json")
        for field in NINE_FIELDS:
            assert seeded[field] == defaults[field], f"{camera_id} sets {field}"


# ---------------------------------------------------------------------------------------
# R1/R2: neutral values change nothing
# ---------------------------------------------------------------------------------------

def test_neutral_fields_derive_the_same_spec_and_filter_graph_hash_as_no_fields():
    plain, neutral = _camera(), _camera(**NEUTRAL)
    stored = _spec(plain, overlay=True, degradation=True)
    derived_plain = postprofile_spec_from_camera(plain, stored)
    derived_neutral = postprofile_spec_from_camera(neutral, stored)

    assert derived_neutral == derived_plain
    assert derived_neutral.identity.stages == stored.identity.stages, "neutral fields added or removed a stage"
    assert _camera_overrides(derived_neutral) == []
    hashes = {
        hashlib.sha256(_graph(spec, camera).encode()).hexdigest()
        for spec, camera in ((derived_plain, plain), (derived_neutral, neutral))
    }
    assert len(hashes) == 1


# ---------------------------------------------------------------------------------------
# R2: camera fields win, names, order, cross-check
# ---------------------------------------------------------------------------------------

FAMILIES = [
    ("noise_model", {"luma": 30, "chroma": 10}, "sensor_noise"),
    ("sharpening", 1.5, "cheap_sharpen"),
    ("grade_white_balance", {"contrast": 1.2}, "bad_white_balance"),
    ("decimation_pattern", "5:5:6:4", "decimate"),
    ("overlay_model", {"camera_id_text": "CAM 9", "fontsize": 20, "x": 12, "y": 30}, "overlay_burnin"),
]


@pytest.mark.parametrize("field,value,stored_stage", FAMILIES, ids=[f[0] for f in FAMILIES])
def test_camera_field_replaces_the_stored_stage_of_its_family_and_records_it(field, value, stored_stage):
    camera = _camera(**{field: value})
    stored = _spec(camera, overlay=True, degradation=True)
    derived = postprofile_spec_from_camera(camera, stored)
    before, after = _stage_names(stored), _stage_names(derived)

    assert stored_stage not in after, f"stored {stored_stage!r} survived next to the camera {field}"
    camera_stages = [name for name in after if name.startswith(f"camera_{field}")]
    assert camera_stages, f"no stage named camera_{field}; stages are {after}"
    assert {"field": field, "replaced_stage": stored_stage} in _camera_overrides(derived)
    if field != "grade_white_balance":
        assert camera_stages == [f"camera_{field}"]
        assert after.index(f"camera_{field}") == before.index(stored_stage), "not under the same position"
    # Everything the camera did not override is untouched and in its original relative order.
    kept = [name for name in before if name != stored_stage]
    assert [name for name in after if not name.startswith("camera_")] == kept
    # The derived spec is valid on its own.
    _checked(derived, camera)


def test_camera_noise_strengths_beat_the_stored_noise_in_the_built_graph():
    camera = _camera(noise_model={"luma": 30, "chroma": 10})
    graph = _graph(_derive(camera, overlay=True, degradation=True), camera)
    assert "c0s=30" in graph and "c1s=10" in graph and "c2s=10" in graph
    assert "alls=12" not in graph


def test_camera_sharpening_and_eq_beat_the_stored_stages_in_the_built_graph():
    camera = _camera(sharpening=1.5, grade_white_balance={"brightness": 0.1, "contrast": 1.2,
                                                           "saturation": 0.5})
    graph = _graph(_derive(camera, overlay=True, degradation=True), camera)
    assert "unsharp=5:5:1.5" in graph and "5:5:0.8" not in graph
    assert "contrast=1.2" in graph and "contrast=1.08" not in graph
    assert "saturation=0.5" in graph and "saturation=0.75" not in graph


def test_overlay_model_overrides_the_clock_overlay_fields():
    camera = _camera(overlay_model={"camera_id_text": "CAM 9", "clock_epoch": 86400, "fontsize": 20,
                                     "x": 12, "y": 30})
    overlay = _derive(camera, overlay=True).overlay
    assert overlay is not None
    assert (overlay.camera_id, overlay.clock_epoch, overlay.fontsize, overlay.x, overlay.y) == (
        "CAM 9", 86400, 20, 12, 30)
    assert overlay.placement == "in_recording"
    graph = _graph(_derive(camera, overlay=True), camera)
    assert "CAM 9" in graph and "86400" in graph and "fontsize=20" in graph


def test_overlay_model_can_move_the_clock_to_playback_and_keeps_the_profile_valid():
    camera = _camera(overlay_model={"placement": "on_playback"})
    derived = _derive(camera, overlay=True)
    assert derived.overlay is not None and derived.overlay.placement == "on_playback"
    assert "drawtext" not in _graph(derived, camera), "an on_playback clock must not be burned in"


def test_partial_white_balance_only_replaces_the_eq_stage_when_an_eq_value_is_set():
    only_temperature = _camera(grade_white_balance={"temperature_k": 4500})
    derived = _derive(only_temperature, overlay=True, degradation=True)
    assert "bad_white_balance" in _stage_names(derived), "temperature alone must leave the stored eq"
    assert not any(o["replaced_stage"] == "bad_white_balance" for o in _camera_overrides(derived))
    assert [s.filter for s in derived.identity.stages if s.filter == "colortemperature"] == ["colortemperature"]
    assert not any(s.filter == "colorchannelmixer" for s in derived.identity.stages)

    only_gain = _camera(grade_white_balance={"red_gain": 1.2})
    derived = _derive(only_gain, overlay=True, degradation=True)
    assert "bad_white_balance" in _stage_names(derived)
    assert any(s.filter == "colorchannelmixer" for s in derived.identity.stages)
    assert not any(s.filter == "colortemperature" for s in derived.identity.stages)


def test_active_fields_without_a_stored_stage_are_added_without_recording_an_override():
    camera = _camera(noise_model={"luma": 20}, sharpening=1.0, chroma_smearing=3,
                     grade_white_balance={"temperature_k": 5000, "red_gain": 1.1, "contrast": 1.1})
    derived = _derive(camera)  # stored profile has no noise, unsharp or eq stage
    names = _stage_names(derived)
    for field in ("noise_model", "sharpening", "chroma_smearing"):
        assert f"camera_{field}" in names
    assert any(n.startswith("camera_grade_white_balance") for n in names)
    assert _camera_overrides(derived) == []
    _checked(derived, camera)


def _rank(stage: PostStage) -> int:
    """The contract's order: scale, fps/decimation, select, loop, clock setpts, drawtext,
    setpts, colortemperature, colorchannelmixer, eq, chroma smearing, noise, unsharp, setsar."""
    if stage.name == "camera_chroma_smearing":
        return 10
    if stage.name == "camera_decimation_pattern":
        return 1
    if stage.filter == "setpts":
        return 4 if stage.params.get("from_schedule") == "clock" else 6
    return {"scale": 0, "fps": 1, "select": 2, "loop": 3, "drawtext": 5, "colortemperature": 7,
            "colorchannelmixer": 8, "eq": 9, "noise": 11, "unsharp": 12, "setsar": 13}.get(
        stage.filter, -1)


def test_stage_order_follows_the_contract_with_every_field_active():
    camera = _camera(
        fps=12.0, bitrate_kbps=400, decimation_pattern="5:5:6:4", noise_model={"luma": 20, "chroma": 10},
        chroma_smearing=3, block_artifacts=0.5, sharpening=1.2,
        grade_white_balance={"temperature_k": 4500, "red_gain": 1.1, "contrast": 1.1},
        overlay_model={"camera_id_text": "CAM 7", "clock_format": "%H:%M:%S"},
    )
    derived = _derive(camera, overlay=True, degradation=True)
    ranks = [_rank(stage) for stage in derived.identity.stages]
    unknown = [s.name for s, r in zip(derived.identity.stages, ranks, strict=True) if r < 0]
    assert not unknown, f"stages the contract's order does not place: {unknown}"
    assert ranks == sorted(ranks), f"order violated: {_stage_names(derived)}"
    filters = [s.filter for s in derived.identity.stages
               if s.filter in {"colortemperature", "colorchannelmixer", "eq"}]
    assert filters == ["colortemperature", "colorchannelmixer", "eq"]
    # Encode-level fields go to the encode arguments, never to the filtergraph.
    assert not any("bitrate" in name or "block" in name for name in _stage_names(derived))
    _checked(derived, camera)


@pytest.mark.parametrize("field,value", [
    ("noise_model", {"luma": 30}), ("sharpening", 1.5), ("chroma_smearing", 4.0),
    ("grade_white_balance", {"contrast": 1.3}), ("grade_white_balance", {"temperature_k": 3000}),
    ("grade_white_balance", {"red_gain": 1.4}), ("decimation_pattern", "5:5:6:4"),
    ("overlay_model", {"camera_id_text": "CAM 9"}), ("overlay_model", {"clock_epoch": 5}),
    ("bitrate_kbps", 400), ("block_artifacts", 0.5),
], ids=lambda value: str(value)[:24])
def test_validate_profile_rejects_a_spec_that_disagrees_with_an_active_camera_field(field, value):
    camera = _camera(**{field: value})
    stored_not_derived = _spec(camera, overlay=True, degradation=True)
    with pytest.raises(REFUSED):
        _checked(stored_not_derived, camera)
    # ...while the properly derived spec for the same camera is accepted.
    _checked(postprofile_spec_from_camera(camera, stored_not_derived), camera)


def test_validate_profile_rejects_a_derived_spec_whose_camera_stage_was_tampered_with():
    camera = _camera(sharpening=1.5)
    derived = _derive(camera, degradation=True)
    stages = [
        stage.model_copy(update={"params": {**stage.params, "luma_amount": 0.1}})
        if stage.name == "camera_sharpening" else stage
        for stage in derived.identity.stages
    ]
    tampered = derived.model_copy(update={"identity": derived.identity.model_copy(update={"stages": stages})})
    with pytest.raises(REFUSED):
        _checked(tampered, camera)


# ---------------------------------------------------------------------------------------
# R3 (pure): seeds
# ---------------------------------------------------------------------------------------

def test_noise_seed_is_derived_from_the_camera_stage_name_and_clamped():
    camera = _camera(noise_model={"luma": 30, "chroma": 10})
    derived = _derive(camera, overlay=True, degradation=True)
    graph = _graph(derived, camera, shot_id="S003", seed=20051114)
    seed = derive_seed("S003", 20051114, "camera_noise_model", INT32_MAX)
    assert 0 <= seed <= INT32_MAX
    assert re.search(rf"(?:all|c\d)_seed={seed}(?!\d)", graph), graph
    stored_seed = derive_seed("S003", 20051114, "sensor_noise", INT32_MAX)
    assert stored_seed == seed or f"seed={stored_seed}" not in graph, "the replaced stage's seed leaked"


def test_noise_seed_follows_shot_id_and_degrade_seed():
    camera = _camera(noise_model={"luma": 30})
    derived = _derive(camera)
    base = _graph(derived, camera, shot_id="S003", seed=1)
    assert base == _graph(derived, camera, shot_id="S003", seed=1)
    assert base != _graph(derived, camera, shot_id="S003", seed=2)
    assert base != _graph(derived, camera, shot_id="S004", seed=1)
    for shot_id, seed in (("S003", 1), ("S004", 1), ("S003", 2)):
        expected = derive_seed(shot_id, seed, "camera_noise_model", INT32_MAX)
        assert re.search(rf"seed={expected}(?!\d)", _graph(derived, camera, shot_id=shot_id, seed=seed))


# ---------------------------------------------------------------------------------------
# R3 (measured, real FFmpeg)
# ---------------------------------------------------------------------------------------

def _font_bytes() -> bytes:
    """A real TrueType font without shipping one: Pillow's bundled default, else a system one."""
    try:
        from PIL import ImageFont

        path = ImageFont.load_default(24).path
        if hasattr(path, "getvalue"):
            data = path.getvalue()
            if data[:4] == b"\x00\x01\x00\x00":
                return data
    except Exception:  # noqa: BLE001 - any failure here just selects the next candidate
        pass
    for candidate in ("C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/arial.ttf"):
        if Path(candidate).is_file():
            return Path(candidate).read_bytes()
    pytest.skip("no TrueType font available for the overlay measurement")


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    done = subprocess.run(argv, capture_output=True, check=False, **kwargs)
    assert done.returncode == 0, f"{argv[0]} failed: {done.stderr.decode('utf-8', 'replace')[-1500:]}"
    return done


def _decode(path: Path, pix_fmt: str, *, frames: int, width: int = 320, height: int = 240) -> list[bytes]:
    size = {"gray": width * height, "yuv420p": width * height * 3 // 2, "rgb24": width * height * 3}[pix_fmt]
    raw = _run([FFMPEG, "-hide_banner", "-v", "error", "-i", str(path), "-frames:v", str(frames),
                "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"]).stdout
    assert len(raw) >= size * frames, f"decoded {len(raw) // size} of {frames} frames from {path.name}"
    return [raw[i * size:(i + 1) * size] for i in range(frames)]


def _planes(frame: bytes, width: int = 320, height: int = 240) -> tuple[bytes, bytes, bytes]:
    luma, quarter = width * height, width * height // 4
    return frame[:luma], frame[luma:luma + quarter], frame[luma + quarter:]


@dataclass
class Degraded:
    result: object
    path: Path


class _Lab:
    """One project directory holding generated masters, a font and every degrade run."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.dir = directory
        self.root = ProjectRoot(directory)
        self.root.write_bytes("font.ttf", _font_bytes())
        raw = _run([FFMPEG, "-hide_banner", "-version"]).stdout.decode("utf-8")
        self.pin = normalize_ffmpeg_identity(raw)
        self._runs = 0

    def master(self, name: str, source: str, *, seconds: float) -> str:
        relative = f"{name}.mkv"
        if not (self.dir / relative).exists():
            _run([FFMPEG, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", source,
                  "-t", str(seconds), "-pix_fmt", "yuv420p", "-c:v", "ffv1", "-g", "1",
                  str(self.dir / relative)])
        return relative

    def degrade(self, master: str, camera: CameraProfile, *, overlay: bool = False,
                degradation: bool = False, shot_id: str = "S003", seed: int = 20051114) -> Degraded:
        self._runs += 1
        stored = _spec(camera, pin=self.pin, overlay=overlay, degradation=degradation)
        derived = postprofile_spec_from_camera(camera, stored)  # as AdapterRunner does
        request = DegradeRequest(
            shot_id=shot_id, shot_version=1, run_id=f"run-{self._runs:03d}", degrade_seed=seed,
            master_relative=master, font_relative="font.ttf", profile=derived, camera_profile=camera,
        )
        result = PostProfileAdapter(self.root, Settings(project_root=self.dir)).degrade(request)
        return Degraded(result, self.dir / result.output_path)


@pytest.fixture(scope="module")
def lab(tmp_path_factory) -> _Lab:
    if FFMPEG is None or FFPROBE is None:
        pytest.skip("real ffmpeg and ffprobe are required")
    return _Lab(tmp_path_factory.mktemp("camera-fields-lab"))


def _cif(**fields) -> CameraProfile:
    return _camera(fps=12.0, resolution=(320, 240), **fields)


# ---- bitrate -------------------------------------------------------------------------

def _kbps(path: Path) -> float:
    probe = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                  "default=nw=1:nk=1", str(path)])
    return path.stat().st_size * 8 / float(probe.stdout) / 1000


@needs_ffmpeg
def test_bitrate_kbps_makes_the_encode_rate_controlled_within_25_percent(lab):
    noise = lab.master(
        "highmotion", "nullsrc=s=320x240:r=24,format=yuv420p,geq=lum='random(1)*255':cb=128:cr=128",
        seconds=8)
    baseline = _kbps(lab.degrade(noise, _cif()).path)
    for target in (150, 600):
        measured = _kbps(lab.degrade(noise, _cif(bitrate_kbps=target)).path)
        assert 0.75 * target <= measured <= 1.25 * target, f"target {target} kbps, measured {measured:.0f}"
    assert baseline > 1.25 * 600, f"the CRF baseline ({baseline:.0f} kbps) cannot tell the targets apart"


# ---- decimation ----------------------------------------------------------------------

_COUNTER = ("nullsrc=s=320x240:r=30,format=yuv420p,"
            "geq=lum='if(mod(floor(N/pow(2,floor(X/40))),2),235,16)':cb=128:cr=128")


def _source_indices(path: Path, count: int) -> list[int]:
    indices = []
    for frame in _decode(path, "gray", frames=count):
        row = 120 * 320
        indices.append(sum((frame[row + k * 40 + 20] > 128) << k for k in range(8)))
    return indices


@needs_ffmpeg
def test_decimation_pattern_keeps_frames_on_the_uneven_cadence(lab):
    master = lab.master("counter", _COUNTER, seconds=3)  # 90 source frames at 30 fps
    uniform = lab.degrade(master, _camera(fps=6.0, resolution=(320, 240)))
    uneven = lab.degrade(master, _camera(fps=6.0, resolution=(320, 240), decimation_pattern="5:5:6:4"))

    assert uneven.result.frame_count == uniform.result.frame_count == 18
    assert uneven.result.pts_delta_distribution == uniform.result.pts_delta_distribution
    flat = _source_indices(uniform.path, 18)
    assert {b - a for a, b in zip(flat, flat[1:], strict=False)} == {5}, f"uniform baseline: {flat}"
    kept = _source_indices(uneven.path, 18)
    deltas = [b - a for a, b in zip(kept, kept[1:], strict=False)]
    pattern = (5, 5, 6, 4)
    assert any(
        all(delta == pattern[(i + shift) % 4] for i, delta in enumerate(deltas)) for shift in range(4)
    ), f"kept source frames {kept} (deltas {deltas}) do not cycle through 5:5:6:4"


# ---- noise ---------------------------------------------------------------------------

def _plane_std(path: Path, plane: int) -> float:
    frames = _decode(path, "yuv420p", frames=6)[2:]
    return statistics.fmean(statistics.pstdev(_planes(frame)[plane]) for frame in frames)


@needs_ffmpeg
def test_noise_model_raises_luma_and_chroma_noise_with_their_own_strengths(lab):
    flat = lab.master("flatgray", "color=c=0x808080:s=320x240:r=24", seconds=1)
    results = {
        name: lab.degrade(flat, _cif(**({"noise_model": model} if model else {}))).path
        for name, model in {
            "none": None, "luma_lo": {"luma": 40, "chroma": 0}, "luma_hi": {"luma": 90, "chroma": 0},
            "chroma_lo": {"luma": 0, "chroma": 40}, "chroma_hi": {"luma": 0, "chroma": 90},
        }.items()
    }
    luma = {k: _plane_std(p, 0) for k, p in results.items()}
    chroma = {k: statistics.fmean((_plane_std(p, 1), _plane_std(p, 2))) for k, p in results.items()}

    assert luma["none"] < 1.0 and chroma["none"] < 1.0, "the neutral baseline of a flat clip is not flat"
    assert luma["luma_lo"] > luma["none"] + 1.0
    assert luma["luma_hi"] > 1.2 * luma["luma_lo"]
    assert chroma["chroma_lo"] > chroma["none"] + 0.5
    assert chroma["chroma_hi"] > 1.2 * chroma["chroma_lo"]
    # Each strength drives its own component.
    assert chroma["luma_hi"] < chroma["chroma_hi"]
    assert luma["chroma_hi"] < luma["luma_hi"]


# ---- chroma smearing -----------------------------------------------------------------

_CHROMA_EDGES = ("nullsrc=s=320x240:r=24,format=yuv420p,"
                 "geq=lum='if(gte(X,100),200,50)':cb='if(gte(X,80),192,64)':cr='if(gte(Y,60),192,64)'")


def _edge_width(profile: list[float]) -> float:
    steps = [abs(b - a) for a, b in zip(profile, profile[1:], strict=False)]
    low = statistics.fmean(profile[len(profile) // 8: len(profile) // 3])
    high = statistics.fmean(profile[-len(profile) // 3: -len(profile) // 8])
    return abs(high - low) / max(max(steps), 1e-6)


def _chroma_edge_metrics(path: Path) -> tuple[float, float, bytes]:
    luma, u, v = _planes(_decode(path, "yuv420p", frames=4)[3])
    rows = range(10, 50)  # chroma plane is 160 wide x 120 high; the cb edge runs down x = 80
    u_profile = [statistics.fmean(u[r * 160 + x] for r in rows) for x in range(160)]
    columns = range(10, 150)  # the cr edge runs across y = 60
    v_profile = [statistics.fmean(v[y * 160 + c] for c in columns) for y in range(120)]
    return _edge_width(u_profile), _edge_width(v_profile), luma


@needs_ffmpeg
def test_chroma_smearing_widens_horizontal_chroma_edges_only(lab):
    master = lab.master("chromaedges", _CHROMA_EDGES, seconds=1)
    base_h, base_v, base_luma = _chroma_edge_metrics(lab.degrade(master, _cif()).path)
    previous = base_h
    for amount in (4, 12):
        h, v, luma = _chroma_edge_metrics(lab.degrade(master, _cif(chroma_smearing=amount)).path)
        assert h > previous + 0.5, f"smearing {amount} did not widen the horizontal chroma edge ({h:.1f})"
        assert abs(v - base_v) <= 1.0, f"vertical chroma edge changed: {base_v:.2f} -> {v:.2f}"
        mean_abs_luma_change = statistics.fmean(abs(a - b) for a, b in zip(luma, base_luma, strict=True))
        assert mean_abs_luma_change <= 2.0, f"luma changed by {mean_abs_luma_change:.2f} levels"
        previous = h
    assert previous >= 1.8 * base_h and previous >= 3.0


# ---- block artifacts -----------------------------------------------------------------

_SMOOTH = ("nullsrc=s=320x240:r=24,format=yuv420p,"
           "geq=lum='128+50*sin(X/37+N/9)*cos(Y/29)+6*sin(X/5)*sin(Y/7+N/5)':cb=128:cr=128")


def _blockiness(path: Path) -> float:
    """Mean luma step across 8x8 column boundaries over the mean step elsewhere."""
    boundary = interior = 0.0
    frames = _decode(path, "gray", frames=8)[2:]
    for frame in frames:
        sums = [0] * 320
        for y in range(240):
            row = frame[y * 320:(y + 1) * 320]
            sums = [s + abs(a - b) for s, a, b in zip(sums, row, row[:1] + row[:-1], strict=True)]
        boundary += statistics.fmean(sums[x] for x in range(8, 320, 8))
        interior += statistics.fmean(sums[x] for x in range(1, 320) if x % 8)
    return boundary / (interior + 0.05 * 240 * len(frames))


def _psnr_against_master(path: Path, master: Path) -> float:
    """Mean luma+chroma PSNR of a 12 fps degrade against the master resampled the same way."""
    result = _run([FFMPEG, "-hide_banner", "-i", str(path), "-i", str(master), "-lavfi",
                   "[1:v]fps=12[ref];[0:v][ref]psnr", "-f", "null", "-"])
    match = re.search(r"average:([0-9.]+)", result.stderr.decode("utf-8", "replace"))
    assert match, "ffmpeg psnr printed no average"
    return float(match.group(1))


@needs_ffmpeg
def test_block_artifacts_raise_block_boundary_discontinuity_monotonically(lab):
    # Measured 2026-11-22 (FFmpeg 8.1.1, libx264, this clip): quantisation error rises
    # monotonically with strength (PSNR 46.4 -> 44.9 -> 39.5 -> 33.7 dB at 0/.4/.7/1), but
    # the 8x8 boundary ratio is noisy between neighbouring strengths (1.68 at .4, 1.53 at
    # .7, 2.34 at 1, even at constant QP), so it is checked against neutral and end to end.
    master = lab.master("smooth", _SMOOTH, seconds=1)
    neutral_path = lab.degrade(master, _cif()).path
    neutral = _blockiness(neutral_path)
    paths = {s: lab.degrade(master, _cif(block_artifacts=s)).path for s in (0.4, 0.7, 1.0)}
    scores = {s: _blockiness(p) for s, p in paths.items()}
    psnr = {s: _psnr_against_master(p, lab.dir / master) for s, p in paths.items()}
    psnr_neutral = _psnr_against_master(neutral_path, lab.dir / master)

    assert psnr_neutral > psnr[0.4] > psnr[0.7] > psnr[1.0], f"error not monotonic: {psnr_neutral}, {psnr}"
    assert all(score > 1.2 * neutral for score in scores.values()), f"neutral {neutral:.3f} vs {scores}"
    assert scores[1.0] > 1.2 * scores[0.4], f"not rising end to end: {scores}"
    assert scores[1.0] > 1.5 * neutral, f"neutral {neutral:.3f} vs 1.0 {scores[1.0]:.3f}"


# ---- sharpening ----------------------------------------------------------------------

def _edge_energy(path: Path) -> float:
    energy = 0
    for frame in _decode(path, "gray", frames=4)[1:]:
        for y in range(0, 240, 2):
            row = frame[y * 320:(y + 1) * 320]
            energy += sum(abs(a - b) for a, b in zip(row, row[1:], strict=False))
    return float(energy)


@needs_ffmpeg
def test_sharpening_raises_edge_energy_with_amount(lab):
    master = lab.master("softstripes", "nullsrc=s=320x240:r=24,format=yuv420p,"
                        "geq=lum='128+60*sin(X/2)':cb=128:cr=128", seconds=1)
    energy = {a: _edge_energy(lab.degrade(master, _cif(**({"sharpening": a} if a else {}))).path)
              for a in (0, 1.0, 2.5)}
    assert energy[1.0] > 1.05 * energy[0], energy
    assert energy[2.5] > 1.05 * energy[1.0], energy


# ---- white balance -------------------------------------------------------------------

def _mean_rgb(path: Path) -> tuple[float, float, float]:
    frame = _decode(path, "rgb24", frames=4)[3]
    count = len(frame) // 3
    return tuple(sum(frame[c::3]) / count for c in range(3))


def _mean_planes(path: Path) -> tuple[float, float, float]:
    luma, u, v = _planes(_decode(path, "yuv420p", frames=4)[3])
    return statistics.fmean(luma), statistics.fmean(u), statistics.fmean(v)


@needs_ffmpeg
def test_white_balance_temperature_gains_and_eq_move_the_picture_as_documented(lab):
    gray = lab.master("midgray", "color=c=0x646464:s=320x240:r=24", seconds=1)
    base = _mean_rgb(lab.degrade(gray, _cif()).path)

    def rgb(**wb):
        return _mean_rgb(lab.degrade(gray, _cif(grade_white_balance=wb)).path)

    warm, cool = rgb(temperature_k=3000), rgb(temperature_k=12000)
    assert warm[0] / warm[2] > 1.3 * base[0] / base[2], (base, warm)
    assert cool[0] / cool[2] < 0.85 * base[0] / base[2], (base, cool)

    red = rgb(red_gain=1.5)
    assert red[0] / base[0] == pytest.approx(1.5, abs=0.12)
    assert red[1] == pytest.approx(base[1], rel=0.05) and red[2] == pytest.approx(base[2], rel=0.05)
    blue = rgb(blue_gain=0.6)
    assert blue[2] / base[2] == pytest.approx(0.6, abs=0.1)
    assert blue[0] == pytest.approx(base[0], rel=0.05) and blue[1] == pytest.approx(base[1], rel=0.05)

    base_luma = _mean_planes(lab.degrade(gray, _cif()).path)[0]
    brighter = _mean_planes(lab.degrade(gray, _cif(grade_white_balance={"brightness": 0.2})).path)[0]
    flatter = _mean_planes(lab.degrade(gray, _cif(grade_white_balance={"contrast": 2.0})).path)[0]
    assert brighter > base_luma + 25
    assert flatter < base_luma - 15  # contrast pivots on mid-grey, so a darker-than-mid image darkens

    colored = lab.master("brick", "color=c=0xC04030:s=320x240:r=24", seconds=1)

    def chroma_offset(path: Path) -> float:
        _, u, v = _mean_planes(path)
        return abs(u - 128) + abs(v - 128)

    assert chroma_offset(lab.degrade(colored, _cif()).path) > 20
    assert chroma_offset(lab.degrade(colored, _cif(grade_white_balance={"saturation": 0})).path) < 4


# ---- overlay -------------------------------------------------------------------------

def _mask(gray: bytes) -> set[int]:
    return {i for i, value in enumerate(gray) if value > 128}


def _iou(a: set[int], b: set[int]) -> float:
    return len(a & b) / max(len(a | b), 1)


def _reference_text_mask(lab: _Lab, text: str, *, fontsize: int, x: int, y: int) -> set[int]:
    """The same text drawn by the same font in isolation, at the same place."""
    (lab.dir / "reference_text.txt").write_text(text, encoding="utf-8")
    drawtext = (f"drawtext=fontfile=font.ttf:textfile=reference_text.txt:x={x}:y={y}:"
                f"fontsize={fontsize}:fontcolor=white:box=1:boxcolor=black@0.4")
    raw = _run([FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=12",
                "-vf", drawtext, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
               cwd=lab.dir).stdout
    return _mask(raw)


@needs_ffmpeg
@pytest.mark.parametrize("clock_format", ["%Y-%m-%d %H:%M:%S", "%d/%m/%y %I:%M:%S %p"])
def test_overlay_text_matches_clock_format_rendered_in_isolation(lab, clock_format):
    master = lab.master("blackout", "color=c=black:s=320x240:r=24", seconds=2)
    camera = _cif(overlay_model={"camera_id_text": "CAM 7", "clock_epoch": EPOCH, "clock_format": clock_format,
                                  "fontsize": 16, "x": 8, "y": 8})
    degraded = lab.degrade(master, camera, overlay=True)
    assert degraded.result.frame_count == 24
    frame = _decode(degraded.path, "gray", frames=13)[12]  # pts 1.000 s at 12 fps
    seen = _mask(frame)
    assert seen, "no overlay text reached the picture"

    def expected(offset: int, fmt: str = clock_format) -> set[int]:
        text = f"CAM 7  {time.strftime(fmt, time.gmtime(EPOCH + offset))}"
        return _reference_text_mask(lab, text, fontsize=16, x=8, y=8)

    exact = _iou(seen, expected(1))
    near_miss = _iou(seen, expected(38))  # a different minute and second
    other_layout = _iou(seen, expected(1, "%a %b %j [%H.%M.%S]"))
    assert exact >= 0.5, f"overlay differs from the isolated rendering of its clock_format (IoU {exact:.2f})"
    assert exact > near_miss + 0.03, (exact, near_miss)
    assert exact > other_layout + 0.1, (exact, other_layout)


# ---- determinism ---------------------------------------------------------------------

def _everything_active() -> CameraProfile:
    return _camera(
        fps=6.0, resolution=(320, 240), bitrate_kbps=400, decimation_pattern="5:5:6:4",
        noise_model={"luma": 25, "chroma": 15}, chroma_smearing=4, block_artifacts=0.5, sharpening=1.2,
        grade_white_balance={"temperature_k": 4500, "red_gain": 1.1, "contrast": 1.1},
        overlay_model={"camera_id_text": "CAM 7", "clock_epoch": EPOCH, "clock_format": "%H:%M:%S",
                       "fontsize": 14, "x": 8, "y": 8},
    )


@needs_ffmpeg
def test_degrade_with_every_field_active_is_byte_identical_and_follows_the_seed(lab):
    master = lab.master("testpattern", "testsrc2=s=320x240:r=30", seconds=2)
    camera = _everything_active()
    first = lab.degrade(master, camera, overlay=True, degradation=True)
    second = lab.degrade(master, camera, overlay=True, degradation=True)
    other_seed = lab.degrade(master, camera, overlay=True, degradation=True, seed=7)
    other_shot = lab.degrade(master, camera, overlay=True, degradation=True, shot_id="S004")

    assert first.path != second.path
    assert first.result.output_sha256 == second.result.output_sha256
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.result.filter_graph == second.result.filter_graph
    assert first.result.filter_graph_hash == second.result.filter_graph_hash
    assert other_seed.result.output_sha256 != first.result.output_sha256
    assert other_shot.result.output_sha256 != first.result.output_sha256
    # The receipt hash is the hash of the graph the derived spec produced.
    derived = postprofile_spec_from_camera(
        camera, _spec(camera, pin=lab.pin, overlay=True, degradation=True))
    assert hashlib.sha256(first.result.filter_graph.encode()).hexdigest() == first.result.filter_graph_hash
    assert first.result.filter_graph == _graph(derived, camera, rate=30, frames=60)


# ---------------------------------------------------------------------------------------
# R4: agent-facing descriptions
# ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("operation", ["create_camera_profile", "revise_camera_profile"])
def test_camera_profile_operation_summaries_describe_the_nine_fields(operation):
    summary = next(op.summary for op in OPERATIONS if op.name == operation)
    lowered = summary.lower()
    assert re.search(r"opt[- ]in", lowered), "summary must say every field is opt-in"
    assert "override" in lowered and "family" in lowered, "summary must say camera fields override a stage family"
    assert "ntscrs" in lowered and "analog_artifacts" in lowered
    assert "neutral" in lowered
    for field in NINE_FIELDS:
        assert field in summary, f"{operation} does not describe {field}"


# ---------------------------------------------------------------------------------------
# Fix round: regression tests for six confirmed defects
# ---------------------------------------------------------------------------------------

# ---- 1 and 4: drawtext escaping, and every accepted value builds and renders ------------

HOSTILE_IDS = [
    "x':dummy:expansion=none:text='y", "O'HARE", "a\\b", "a\\\\b", "50%", "a,b;c[d]", "%{pts}",
]
CONTROL_IDS = ["CAM\n7", "CAM\r7", "CAM\t7", "CAM\x007", "CAM\x1b7", "\n"]
ACCEPTED_TEXT = [
    ("CAM 7", "%a %b %d [%j]"), ("CAM [7]", "%H:%M:%S"), ("CAM 7", "(%H:%M:%S)"),
    ("CAM 7", "%Y/%m/%d, %p"),
]


def _literal_text_mask(lab: _Lab, text: str, *, fontsize: int, x: int, y: int) -> set[int]:
    """``text`` drawn literally (expansion=none, from a file) by the same font in isolation."""
    (lab.dir / "literal_text.txt").write_text(text, encoding="utf-8")
    drawtext = (f"drawtext=fontfile=font.ttf:textfile=literal_text.txt:expansion=none:x={x}:y={y}:"
                f"fontsize={fontsize}:fontcolor=white:box=1:boxcolor=black@0.4")
    raw = _run([FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=12",
                "-vf", drawtext, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
               cwd=lab.dir).stdout
    return _mask(raw)


def _overlay_camera(camera_id_text: str, clock_format: str | None) -> CameraProfile:
    return _cif(overlay_model={"camera_id_text": camera_id_text, "clock_epoch": EPOCH,
                                "clock_format": clock_format, "fontsize": 12, "x": 8, "y": 8})


def _rendered_overlay(lab: _Lab, camera_id_text: str, clock_format: str | None):
    """The overlay mask of a degrade, or None if the value was refused before FFmpeg ran.

    A refusal is only accepted at camera validation or profile validation. Once the profile
    validates, the degrade itself must succeed: an FFmpeg failure is the defect.
    """
    try:
        camera = _overlay_camera(camera_id_text, clock_format)
        _checked(_derive(camera, overlay=True), camera)
    except REFUSED:
        return None
    master = lab.master("blackout", "color=c=black:s=320x240:r=24", seconds=2)
    degraded = lab.degrade(master, camera, overlay=True)
    assert degraded.result.frame_count == 24
    return _mask(_decode(degraded.path, "gray", frames=13)[12])  # pts 1.000 s at 12 fps


@needs_ffmpeg
@pytest.mark.parametrize("camera_id_text", HOSTILE_IDS, ids=repr)
def test_hostile_camera_id_text_renders_as_literal_text_or_is_refused(lab, camera_id_text):
    clock_format = "%H:%M:%S"
    seen = _rendered_overlay(lab, camera_id_text, clock_format)
    if seen is None:
        return  # refused up front is an allowed outcome
    clock = time.strftime(clock_format, time.gmtime(EPOCH + 1))
    exact = _iou(seen, _literal_text_mask(lab, f"{camera_id_text}  {clock}", fontsize=12, x=8, y=8))
    decoy = _iou(seen, _literal_text_mask(lab, f"CAM 7  {clock}", fontsize=12, x=8, y=8))
    clockless = _iou(seen, _literal_text_mask(lab, camera_id_text, fontsize=12, x=8, y=8))
    assert exact >= 0.5, f"{camera_id_text!r} did not render literally (IoU {exact:.2f})"
    assert exact > decoy + 0.03 and exact > clockless + 0.03, (exact, decoy, clockless)


@pytest.mark.parametrize("camera_id_text", [*HOSTILE_IDS, "CAM[O'HARE]", "CAM: 7", "A, B"], ids=repr)
def test_hostile_camera_id_text_without_a_clock_format_renders_literally_or_is_refused(lab, camera_id_text):
    # Re-review 2 (2026-11-22): with no clock_format, text main refused took main's
    # legacy serialization and broke FFmpeg parsing. drawtext's default gmtime format
    # is "%Y-%m-%d %H:%M:%S".
    seen = _rendered_overlay(lab, camera_id_text, None)
    if seen is None:
        return
    clock = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(EPOCH + 1))
    exact = _iou(seen, _literal_text_mask(lab, f"{camera_id_text}  {clock}", fontsize=12, x=8, y=8))
    decoy = _iou(seen, _literal_text_mask(lab, f"CAM 7  {clock}", fontsize=12, x=8, y=8))
    assert exact >= 0.5, f"{camera_id_text!r} did not render literally (IoU {exact:.2f})"
    assert exact > decoy + 0.03, (exact, decoy)


@pytest.mark.parametrize("camera_id_text", CONTROL_IDS, ids=repr)
def test_control_characters_in_camera_id_text_are_refused(camera_id_text):
    holder: list[CameraProfile] = []

    def construct():
        holder.append(_overlay_camera(camera_id_text, "%H:%M:%S"))

    def derive_and_validate():
        _checked(_derive(holder[0], overlay=True), holder[0])

    _refused_message(construct, derive_and_validate)


@pytest.mark.parametrize("camera_id_text,clock_format", ACCEPTED_TEXT, ids=lambda v: str(v)[:20])
def test_every_accepted_overlay_text_builds_a_filter_graph(camera_id_text, clock_format):
    camera = _overlay_camera(camera_id_text, clock_format)  # accepted by camera validation...
    graph = _graph(_derive(camera, overlay=True), camera)  # ...so it must validate and build
    assert "drawtext" in graph


@needs_ffmpeg
@pytest.mark.parametrize("camera_id_text,clock_format", ACCEPTED_TEXT, ids=lambda v: str(v)[:20])
def test_every_accepted_overlay_text_renders_its_literal_text(lab, camera_id_text, clock_format):
    seen = _rendered_overlay(lab, camera_id_text, clock_format)
    assert seen is not None, "camera validation accepted the value, so the pipeline must too"
    text = f"{camera_id_text}  {time.strftime(clock_format, time.gmtime(EPOCH + 1))}"
    exact = _iou(seen, _literal_text_mask(lab, text, fontsize=12, x=8, y=8))
    assert exact >= 0.5, f"{text!r} rendered differently from its literal (IoU {exact:.2f})"


# ---- 2: white balance must not bleed into chroma smearing or luma noise ------------------

WB = {"temperature_k": 4500, "red_gain": 1.1}


@needs_ffmpeg
def test_chroma_smearing_after_white_balance_leaves_luma_alone(lab):
    master = lab.master("chromaedges", _CHROMA_EDGES, seconds=1)
    plain_h, plain_v, plain_luma = _chroma_edge_metrics(
        lab.degrade(master, _cif(grade_white_balance=WB)).path)
    h, v, luma = _chroma_edge_metrics(
        lab.degrade(master, _cif(grade_white_balance=WB, chroma_smearing=12)).path)

    assert h > plain_h + 0.5, f"smearing had no horizontal chroma effect ({plain_h:.1f} -> {h:.1f})"
    assert abs(v - plain_v) <= 1.0, f"vertical chroma edge changed: {plain_v:.2f} -> {v:.2f}"
    change = statistics.fmean(abs(a - b) for a, b in zip(luma, plain_luma, strict=True))
    assert change <= 2.0, f"smearing under white balance changed luma by {change:.2f} levels"


@needs_ffmpeg
def test_luma_noise_after_white_balance_lands_on_luma_not_chroma(lab):
    flat = lab.master("flatgray", "color=c=0x808080:s=320x240:r=24", seconds=1)
    quiet = lab.degrade(flat, _cif(grade_white_balance=WB)).path
    noisy = lab.degrade(flat, _cif(grade_white_balance=WB, noise_model={"luma": 60, "chroma": 0})).path

    assert _plane_std(noisy, 0) > _plane_std(quiet, 0) + 1.0, "luma noise did not reach Y"
    for plane in (1, 2):
        assert _plane_std(noisy, plane) <= _plane_std(quiet, plane) + 0.5, (
            f"luma-only noise leaked into chroma plane {plane}")


# ---- 3: legacy dict migration -------------------------------------------------------------

def _legacy_store(tmp_path):
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    store.create(CameraProfile(camera_id="legacy", profile_name="legacy"))
    return root, store


@pytest.mark.parametrize("legacy_schema_version", [1, 2])
def test_legacy_float_valued_dict_fields_load_as_integers(tmp_path, legacy_schema_version):
    root, store = _legacy_store(tmp_path)
    _rewrite_camera_record(root, "legacy", schema_version=legacy_schema_version,
                           noise_model={"luma": 4.0}, grade_white_balance={"temperature_k": 4500.0})

    dumped = store.load(CameraProfile, "legacy").model_dump(mode="json")

    assert dumped["noise_model"]["luma"] == 4 and type(dumped["noise_model"]["luma"]) is int
    assert dumped["grade_white_balance"]["temperature_k"] == 4500
    assert type(dumped["grade_white_balance"]["temperature_k"]) is int


@pytest.mark.parametrize("field,value,named", [
    ("noise_model", {"luma": 4.5}, "luma"),
    ("noise_model", {"chroma": 10.25}, "chroma"),
    ("noise_model", {"grain": 1.0}, "grain"),
    ("grade_white_balance", {"temperature_k": 4500.5}, "temperature_k"),
    ("grade_white_balance", {"gamma": 1.0}, "gamma"),
], ids=lambda v: str(v)[:24])
def test_legacy_non_integral_or_unknown_dict_values_fail_naming_the_field(tmp_path, field, value, named):
    root, store = _legacy_store(tmp_path)
    _rewrite_camera_record(root, "legacy", schema_version=2, **{field: value})

    with pytest.raises(REFUSED) as caught:
        store.load(CameraProfile, "legacy")

    message = str(caught.value).lower()
    assert named in message or field in message, message


# Re-review 2 (2026-11-22): a pre-v3 record is held to the types main read it with, so
# the migration neither revives a record main refused nor refuses one main loaded.
@pytest.mark.parametrize("field,value", [
    ("bitrate_kbps", 0), ("bitrate_kbps", -5), ("bitrate_kbps", "fast"),
    ("grade_white_balance", {"temperature_k": "warm"}),
    ("noise_model", {"luma": "loud"}),
], ids=lambda v: str(v)[:24])
def test_legacy_values_main_refused_stay_refused(tmp_path, field, value):
    root, store = _legacy_store(tmp_path)
    _rewrite_camera_record(root, "legacy", schema_version=2, **{field: value})

    with pytest.raises(REFUSED) as caught:
        store.load(CameraProfile, "legacy")

    assert field in str(caught.value), caught.value


def test_legacy_numeric_strings_main_coerced_still_load(tmp_path):
    root, store = _legacy_store(tmp_path)
    _rewrite_camera_record(root, "legacy", schema_version=2, overlay_model={"fontsize": "12"},
                           noise_model={"luma": "4"}, grade_white_balance={"red_gain": "1.5"})

    loaded = store.load(CameraProfile, "legacy")

    assert loaded.overlay_model.fontsize == 12
    assert loaded.noise_model.luma == 4
    assert loaded.grade_white_balance.red_gain == 1.5
    assert loaded.bitrate_kbps is None


def test_legacy_numeric_strings_are_checked_exactly_and_numeric_ids_load_as_text(tmp_path):
    root, store = _legacy_store(tmp_path)
    _rewrite_camera_record(root, "legacy", schema_version=2, overlay_model={"fontsize": "16.0000000000000001"})
    with pytest.raises(REFUSED) as caught:
        store.load(CameraProfile, "legacy")
    assert "fontsize" in str(caught.value)

    _rewrite_camera_record(root, "legacy", schema_version=2,
                           overlay_model={"camera_id_text": 123, "clock_epoch": "9007199254740993"})
    loaded = store.load(CameraProfile, "legacy")
    assert loaded.overlay_model.camera_id_text == "123"
    assert loaded.overlay_model.clock_epoch == 9007199254740993


# ---- 5: composite_stage only belongs on drawtext ------------------------------------------

def _with_composite_stage(spec: PostProfileSpec, stage_name: str, side: str) -> PostProfileSpec:
    stages = [stage.model_copy(update={"composite_stage": side}) if stage.name == stage_name else stage
              for stage in spec.identity.stages]
    return spec.model_copy(update={"identity": spec.identity.model_copy(update={"stages": stages})})


@pytest.mark.parametrize("side", ["on_playback", "in_recording"])
@pytest.mark.parametrize("fields", [
    {"sharpening": 1.5}, {"noise_model": {"luma": 20, "chroma": 10}}, {"chroma_smearing": 3},
    {"grade_white_balance": {"contrast": 1.2}}, {"grade_white_balance": {"temperature_k": 4500}},
    {"decimation_pattern": "5:5:6:4"},
], ids=lambda v: next(iter(v)) if isinstance(v, dict) else v)
def test_composite_stage_on_a_camera_non_drawtext_stage_is_rejected(fields, side):
    camera = _camera(**fields)
    derived = _derive(camera, degradation=True)
    targets = [s.name for s in derived.identity.stages
               if s.name.startswith("camera_") and s.filter != "drawtext"]
    assert targets
    for name in targets:
        with pytest.raises(REFUSED):
            _checked(_with_composite_stage(derived, name, side), camera)


def test_composite_stage_on_a_stored_non_drawtext_stage_is_rejected():
    camera = _camera()
    derived = _derive(camera, overlay=True, degradation=True)
    _checked(derived, camera)  # the unmodified spec is valid
    for name in ("sensor_noise", "cheap_sharpen", "bad_white_balance"):
        with pytest.raises(REFUSED):
            _checked(_with_composite_stage(derived, name, "on_playback"), camera)


def test_camera_overlay_stage_marked_on_playback_while_the_camera_says_in_recording_is_rejected():
    camera = _camera(overlay_model={"camera_id_text": "CAM 9", "placement": "in_recording"})
    derived = _derive(camera, overlay=True)
    with pytest.raises(REFUSED):
        _checked(_with_composite_stage(derived, "camera_overlay_model", "on_playback"), camera)


# ---- 6: the receipt binds the whole derived spec ------------------------------------------

def _retained_spec_digests(degraded: Degraded, lab: _Lab) -> tuple[dict, set[str]]:
    """The derived spec kept in the run metadata, and every plausible canonical-JSON digest of it.

    The canonical form is not specified, so sorted-key compact JSON in both ASCII modes is
    accepted, as is the digest of any string the metadata retains verbatim.
    """
    metadata = json.loads((lab.dir / degraded.result.metadata_path).read_text(encoding="utf-8"))
    spec = metadata["derived_spec"]
    digests = {
        hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=ascii_only).encode("utf-8")).hexdigest()
        for ascii_only in (True, False)
    }
    digests |= {hashlib.sha256(value.encode("utf-8")).hexdigest()
                for value in metadata.values() if isinstance(value, str)}
    return spec, digests


@needs_ffmpeg
def test_degrade_result_spec_sha256_is_the_digest_of_the_retained_derived_spec(lab):
    master = lab.master("flatgray", "color=c=0x808080:s=320x240:r=24", seconds=1)
    camera = _cif(bitrate_kbps=400, block_artifacts=0.4, sharpening=1.0)
    degraded = lab.degrade(master, camera)

    digest = degraded.result.postprofile_spec_sha256
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    spec, digests = _retained_spec_digests(degraded, lab)
    assert digest in digests, "the receipt hash is not the digest of the derived spec retained in metadata"
    expected = postprofile_spec_from_camera(camera, _spec(camera, pin=lab.pin))
    assert spec == expected.model_dump(mode="json"), "the retained spec is not the derived spec"
    assert digest == lab.degrade(master, camera).result.postprofile_spec_sha256, "not deterministic"


@needs_ffmpeg
@pytest.mark.parametrize("field,low,high", [
    ("bitrate_kbps", 300, 900), ("block_artifacts", 0.2, 0.8),
])
def test_spec_sha256_changes_with_encode_fields_while_the_filter_graph_hash_does_not(lab, field, low, high):
    master = lab.master("flatgray", "color=c=0x808080:s=320x240:r=24", seconds=1)
    first = lab.degrade(master, _cif(**{field: low})).result
    second = lab.degrade(master, _cif(**{field: high})).result

    assert first.filter_graph_hash == second.filter_graph_hash, f"{field} must not touch the graph"
    assert first.postprofile_spec_sha256 != second.postprofile_spec_sha256, (
        f"changing {field} left the receipt's spec hash unchanged")


def _degraded_output(**extra) -> ShotOutput:
    return ShotOutput(
        output_id="degraded-v1", episode_id="ep1", shot_id="S003", record_version=1,
        kind=ShotOutputKind.DEGRADED, relative_path="runs/degraded.mp4", content_sha256="a" * 64,
        profile_semver="0.2.0", filter_graph_hash="b" * 64, pinned_binaries={"ffmpeg": "fixture"},
        **extra,
    )


def test_shot_output_without_the_spec_hash_still_validates_and_defaults_to_none():
    assert _degraded_output().postprofile_spec_sha256 is None


def test_shot_output_accepts_a_64_hex_spec_hash_and_refuses_anything_else():
    assert _degraded_output(postprofile_spec_sha256="c" * 64).postprofile_spec_sha256 == "c" * 64
    for bad in ("c" * 63, "C" * 64, "g" * 64, "", "sha256:" + "c" * 64):
        with pytest.raises(ValidationError):
            _degraded_output(postprofile_spec_sha256=bad)


# ---------------------------------------------------------------------------------------
# Fix round 2
# ---------------------------------------------------------------------------------------

# ---- 1: neutral profiles change nothing ---------------------------------------------------

# The clock overlay exactly as `main` serializes it (git show main:orchestrator/adapters/
# postprofile/profile.py, build_filter_graph, drawtext branch, computed by hand) for
# camera_id "CAM 03", clock_epoch 1131984000, x=8, y="h-24", fontsize 16, font "font.ttf".
MAIN_CLOCK_DRAWTEXT = (
    "drawtext=fontfile='font.ttf':text='CAM 03  %{pts\\:gmtime\\:1131984000}':"
    "x=8:y=h-24:fontsize=16:fontcolor=white:box=1:boxcolor=black@0.4"
)


def _stored_spec_from_project(store: ProjectStore, name: str) -> PostProfileSpec:
    """The stored PostProfile as an adapter spec, built as service/compose.py builds it."""
    identity = store.load(PostProfile, name)
    return PostProfileSpec(
        identity=identity, output_fps=identity.output_fps,
        storage_resolution=identity.storage_resolution,
        sample_aspect_ratio=identity.sample_aspect_ratio,
        artifact_schedule=tuple(identity.artifact_schedule),
        overlay=(OverlaySpec.model_validate(identity.overlay.model_dump(
            mode="python", exclude={"schema_version": True, "font_relative": True,
                                    "indicator": {"schema_version"}}))
                 if identity.overlay is not None else None),
        encode=EncodeSpec.model_validate(
            identity.encode.model_dump(mode="python", exclude={"schema_version"})),
    )


@pytest.mark.parametrize("profile_id", ["dvr-2005", "low-light-bw", "consumer-2002"])
def test_seeded_profiles_with_neutral_camera_fields_build_the_stored_graph_unchanged(tmp_path, profile_id):
    store = ProjectStore(ProjectRoot(tmp_path))
    create_project(store, Project(project_id="p1", title="Seeded"))
    camera = store.load(CameraProfile, profile_id)
    stored = _stored_spec_from_project(store, profile_id)
    derived = postprofile_spec_from_camera(camera, stored)

    kwargs = dict(shot_id="S003", degrade_seed=20051114, font_path="font.ttf")
    validated = dict(master_frames=3600, master_rate=Fraction(60, 1))
    stored_graph = build_filter_graph(validate_profile(stored, camera, **validated), **kwargs)
    derived_graph = build_filter_graph(validate_profile(derived, camera, **validated), **kwargs)

    assert derived_graph == stored_graph
    assert "format=" not in derived_graph, "a neutral camera must not add a format stage"
    assert derived.identity.stages == stored.identity.stages


def test_the_clock_overlay_serializes_exactly_as_on_main():
    camera = _camera()
    graph = _graph(_derive(camera, overlay=True), camera)
    assert MAIN_CLOCK_DRAWTEXT in graph


def test_dvr_2005_seeded_clock_overlay_serializes_exactly_as_on_main(tmp_path):
    store = ProjectStore(ProjectRoot(tmp_path))
    create_project(store, Project(project_id="p1", title="Seeded"))
    camera = store.load(CameraProfile, "dvr-2005")
    stored = _stored_spec_from_project(store, "dvr-2005")
    graph = build_filter_graph(
        validate_profile(stored, camera, master_frames=3600, master_rate=Fraction(60, 1)),
        shot_id="S003", degrade_seed=20051114, font_path="font.ttf")
    assert MAIN_CLOCK_DRAWTEXT in graph


def test_a_format_stage_appears_only_when_an_rgb_stage_precedes_a_yuv_plane_stage():
    only_rgb = _camera(grade_white_balance={"temperature_k": 4500})
    assert "format=" not in _graph(_derive(only_rgb), only_rgb)

    yuv_only = _camera(noise_model={"luma": 10}, sharpening=1.0)
    assert "format=" not in _graph(_derive(yuv_only), yuv_only)

    both = _camera(grade_white_balance={"temperature_k": 4500}, noise_model={"luma": 10})
    graph = _graph(_derive(both), both)
    assert "format=" in graph
    assert graph.index("colortemperature") < graph.index("format=") < graph.index("noise=")


# ---- 2: legacy schema-2 booleans and integral floats --------------------------------------

@pytest.mark.parametrize("legacy_schema_version", [1, 2])
def test_legacy_bool_valued_noise_flags_and_integral_float_overlay_fields_load(tmp_path, legacy_schema_version):
    root, store = _legacy_store(tmp_path)
    _rewrite_camera_record(root, "legacy", schema_version=legacy_schema_version,
                           noise_model={"temporal": 0.0, "uniform": 1.0},
                           overlay_model={"fontsize": 12.0, "x": 8.0})

    dumped = store.load(CameraProfile, "legacy").model_dump(mode="json")

    assert dumped["noise_model"]["temporal"] is False
    assert dumped["noise_model"]["uniform"] is True
    assert dumped["overlay_model"]["fontsize"] == 12 and type(dumped["overlay_model"]["fontsize"]) is int
    assert dumped["overlay_model"]["x"] == 8 and type(dumped["overlay_model"]["x"]) is int


@pytest.mark.parametrize("field,value,named", [
    ("noise_model", {"temporal": 0.5}, "temporal"),
    ("noise_model", {"uniform": 2}, "uniform"),
    ("overlay_model", {"fontsize": 12.5}, "fontsize"),
    ("overlay_model", {"x": 8.5}, "x"),
], ids=lambda v: str(v)[:24])
def test_legacy_fractional_flags_and_overlay_numbers_fail_naming_the_field(tmp_path, field, value, named):
    root, store = _legacy_store(tmp_path)
    _rewrite_camera_record(root, "legacy", schema_version=2, **{field: value})

    with pytest.raises(REFUSED) as caught:
        store.load(CameraProfile, "legacy")

    assert named in str(caught.value).lower()


# ---- 3: camera_id_text whitespace --------------------------------------------------------

@pytest.mark.parametrize("text", [" CAM", "CAM ", "  CAM 03  ", "\tCAM", "CAM "], ids=repr)
def test_camera_id_text_with_leading_or_trailing_whitespace_is_refused_naming_the_field(text):
    message = _refused_message(lambda: _camera(overlay_model={"camera_id_text": text}))
    assert "camera_id_text" in message


@pytest.mark.parametrize("text", ["CAM 03", "A B  C"], ids=repr)
def test_camera_id_text_with_interior_spaces_is_accepted(text):
    camera = _camera(overlay_model={"camera_id_text": text})
    assert camera.overlay_model.camera_id_text == text


# ---- 4: PostProfile overlay font path -----------------------------------------------------

def _post_profile_with_font(font_relative: str) -> PostProfile:
    return PostProfile(
        name="font-check", semver="0.1.0", pinned_binaries={"ffmpeg": "ffmpeg version fixture"},
        stages=_stages(320, 240, 12, overlay=True, degradation=False),
        overlay={"camera_id": "CAM 03", "clock_epoch": EPOCH, "fontsize": 16,
                 "font_relative": font_relative},
    )


@pytest.mark.parametrize("font_relative", [
    "O'HARE.ttf", "a b.ttf", "a:b.ttf", "a,b.ttf", "a[1].ttf", "a;b.ttf", "a\\b.ttf", "café.ttf",
    "../overlay.ttf", "assets/../overlay.ttf", "/assets/fonts/overlay.ttf", "a%b.ttf",
], ids=repr)
def test_overlay_font_relative_outside_the_safe_path_alphabet_is_refused(font_relative):
    message = _refused_message(lambda: _post_profile_with_font(font_relative))
    assert "font" in message


@pytest.mark.parametrize("font_relative", ["assets/fonts/overlay.ttf", "fonts/Overlay-2_b.v1.ttf", "f.ttf"])
def test_overlay_font_relative_in_the_safe_alphabet_is_accepted(font_relative):
    assert _post_profile_with_font(font_relative).overlay.font_relative == font_relative


# ---- 5: libx264rgb with rgb24 --------------------------------------------------------------

def _rgb_derived(**fields) -> tuple[PostProfileSpec, CameraProfile]:
    camera = _camera(codec="libx264rgb", **fields)
    stored = _spec(camera).model_copy(update={"encode": EncodeSpec(
        codec="libx264rgb", preset="medium", crf=30, pix_fmt="rgb24", threads=1)})
    derived = postprofile_spec_from_camera(camera, stored)
    _checked(derived, camera)
    return derived, camera


@pytest.mark.parametrize("fields", [{}, {"bitrate_kbps": 500}, {"grade_white_balance": {"temperature_k": 4500}}],
                         ids=str)
def test_libx264rgb_with_rgb24_is_accepted_when_no_component_effect_is_active(fields):
    derived, _camera_used = _rgb_derived(**fields)
    assert derived.encode.pix_fmt == "rgb24"


@pytest.mark.parametrize("fields", [{"noise_model": {"luma": 10}}, {"chroma_smearing": 2.0}], ids=str)
def test_libx264rgb_with_rgb24_is_refused_naming_pix_fmt_when_a_component_effect_is_active(fields):
    message = _refused_message(lambda: _rgb_derived(**fields))
    assert "pix_fmt" in message


# ---- 6: noise parameter families ----------------------------------------------------------

def _spec_with_noise(params: dict) -> tuple[PostProfileSpec, CameraProfile]:
    camera = _camera()
    stored = _spec(camera)
    noise = PostStage(name="sensor_noise", filter="noise", params=params, seed_source="shot")
    # The stored stage goes before the final setpts, where the stage order puts noise.
    stages = list(stored.identity.stages) + [noise]
    return stored.model_copy(update={"identity": stored.identity.model_copy(update={"stages": stages})}), camera


@pytest.mark.parametrize("params", [
    {"alls": 12, "c0s": 40}, {"allf": "t+u", "c0s": 40}, {"alls": 12, "allf": "t+u", "c1s": 5},
    {"alls": 12, "c2s": 5},
], ids=str)
def test_a_noise_stage_mixing_all_and_per_component_parameters_is_refused(params):
    spec, camera = _spec_with_noise(params)
    message = _refused_message(lambda: _checked(spec, camera))
    assert "noise" in message and ("c0s" in message or "c1s" in message or "c2s" in message
                                   or "component" in message or "mix" in message)


@pytest.mark.parametrize("params", [{"c0s": 40}, {"alls": 12, "allf": "t+u"}], ids=str)
def test_a_noise_stage_using_one_parameter_family_is_accepted(params):
    spec, camera = _spec_with_noise(params)
    assert "noise=" in _graph(spec, camera)


# ---- 7: on_playback overlay with a clock_format -------------------------------------------

def test_on_playback_overlay_with_clock_format_is_accepted_in_step_3b():
    camera = _camera(overlay_model={"placement": "on_playback", "clock_format": "%H:%M:%S"})
    derived = _derive(camera, overlay=True)
    assert derived.overlay is not None
    assert derived.overlay.placement == "on_playback"
    assert derived.overlay.clock_format == "%H:%M:%S"
    _checked(derived, camera)


def test_on_playback_overlay_without_clock_format_is_still_accepted():
    camera = _camera(overlay_model={"placement": "on_playback"})
    derived = _derive(camera, overlay=True)
    assert derived.overlay is not None and derived.overlay.placement == "on_playback"
    _checked(derived, camera)


# ---- 8: decimation timing matches uniform decimation --------------------------------------

def _frame_durations(path: Path) -> list[float]:
    out = _run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=duration_time",
                "-of", "csv=p=0", str(path)]).stdout.decode().split()
    # ffprobe 8.1 CSV rows carry a trailing separator ("0.100000,").
    return [float(value.rstrip(",")) for value in out]


def _format_duration(path: Path) -> float:
    out = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                str(path)]).stdout.decode()
    return float(out)


@needs_ffmpeg
def test_decimation_pattern_matches_uniform_decimation_in_frame_count_durations_and_total_duration(lab):
    master = lab.master("sixty", "testsrc2=s=320x240:r=60", seconds=2)  # 120 frames at 60 fps
    uniform = lab.degrade(master, _camera(fps=10.0, resolution=(320, 240)))
    uneven = lab.degrade(master, _camera(fps=10.0, resolution=(320, 240), decimation_pattern="5:7:6:6"))

    uniform_durations = _frame_durations(uniform.path)
    uneven_durations = _frame_durations(uneven.path)
    assert len(uneven_durations) == len(uniform_durations) == 20
    assert uneven_durations == pytest.approx([0.1] * 20, abs=1e-6), "every frame, the last included, lasts 1/fps"
    assert uniform_durations == pytest.approx([0.1] * 20, abs=1e-6)
    assert _format_duration(uneven.path) == pytest.approx(_format_duration(uniform.path), abs=1e-6)


# ---- 9: bitrate is a target the quantiser floor can undercut ------------------------------

@pytest.mark.parametrize("operation", ["create_camera_profile", "revise_camera_profile"])
def test_camera_profile_summaries_say_block_artifacts_can_hold_the_bitrate_below_target(operation):
    summary = next(op.summary for op in OPERATIONS if op.name == operation).lower()
    assert re.search(r"quanti[sz]er", summary), "summary must mention the quantiser floor"
    assert "below" in summary, "summary must say the bitrate can fall below bitrate_kbps"
    sentence = next((s for s in re.split(r"(?<=[.;])\s", summary) if re.search(r"quanti[sz]er", s)), "")
    assert "block_artifacts" in sentence and "bitrate" in sentence, sentence
