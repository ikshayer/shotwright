"""Tests for orchestrator/adapters/export/PLAYBACK_OVERLAY_CONTRACT.md (step 3b).

Written from the contract, not from the implementation. Requirement groups:

R1  an ``on_playback`` clock is drawn per Shot segment during cut assembly, at cut
    resolution, from that Shot's own camera ``overlay_model``; an ``in_recording`` clock is
    never drawn a second time
R2  the on-playback clock is the clock the recording shows: pixel-exact against the same
    Shot degraded with the clock ``in_recording`` (drops, freezes, decimation, a minute
    boundary), the ``clock_offset_seconds`` hook, segment-local time under gaps and trims
R3  step 3e's fit check runs at the *cut* resolution, before any encode and at camera
    create/revise
R4  DejaVu Sans Mono ships as the default overlay font: ``create_project`` installs it, the
    new ``install_default_overlay_font`` operation never overwrites, a tampered bundle is
    refused, missing-font messages name the operation, packages ship the licence
R5  the agent-facing operation summaries

Seams assumed (see the report):

* ``assemble_episode(store, TimelineAssembler(root, settings, control, popen=...), ...)``
  is the real cut path; the pre-assembly fit refusal is a ``JobError`` or ``ValueError``
  raised before any FFmpeg *encode* (an argv with ``-filter_complex``) is launched through
  the assembler's ``popen``.
* the ``clock_offset_seconds`` hook is a keyword of ``assemble_episode`` or of
  ``TimelineAssembler.run`` (found by signature), taking seconds as a number.
* the new operation is ``install_default_overlay_font`` in ``OPERATIONS``, invoked with
  ``{"project_id": ...}`` through ``OperationContext.invoke``.
* the bundle is ``orchestrator/assets/fonts/DejaVuSansMono.ttf`` plus ``LICENSE``.

The degrade and cut encodes use ``crf=0`` (lossless), because a lossy encode cannot make an
overlay drawn before the first encode pixel-identical to one drawn after it. The contract's
pixel-exact comparison is only meaningful over a lossless pipeline.
"""

from __future__ import annotations

import calendar
import hashlib
import inspect
import json
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from orchestrator.adapters.blender.scene import RenderRequest, build_scene_plan, synthetic_interior
from orchestrator.adapters.postprofile.runner import PostProfileAdapter
from orchestrator.adapters.timeline.runner import TimelineAssembler
from orchestrator.api.app import OperationContext, create_app
from orchestrator.api.routes import OPERATIONS
from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import (
    ArtifactScheduleEntry,
    CameraProfile,
    Episode,
    PostProfile,
    PostProfileEncode,
    PostProfileOverlay,
    PostStage,
    Project,
    Shot,
    ShotOutput,
    ShotOutputKind,
    Timeline,
    TimelineEntry,
)
from orchestrator.jobs.process import CpuProcessSupervisor
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings
from orchestrator.service.compose import compose_degrade_request
from orchestrator.service.dependencies import (
    create_camera_profile,
    create_project,
    revise_camera_profile,
)
from orchestrator.service.runner import postprofile_spec_from_camera
from orchestrator.service.timeline import assemble_episode
from orchestrator.store.json_store import ProjectStore

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="real ffmpeg and ffprobe are required"
)

_FIXTURE_FONTS = Path(__file__).resolve().parents[2] / "fixtures" / "fonts"
MONO = _FIXTURE_FONTS / "DejaVuSansMono.ttf"
FIXTURE_LICENSE = _FIXTURE_FONTS / "LICENSE"
MONO_SHA256 = "b4a6c3e4faab8773f4ff761d56451646409f29abedd68f05d38c2df667d3c582"
needs_mono = pytest.mark.skipif(not MONO.is_file(), reason="DejaVu Sans Mono fixture is absent")

REPO = Path(__file__).resolve().parents[3]
EPISODE = "ep1"
FONT_RELATIVE = "assets/fonts/overlay.ttf"
LICENSE_RELATIVE = "assets/fonts/LICENSE-DejaVu.txt"
FPS = 12
EPOCH = calendar.timegm((2005, 11, 14, 16, 0, 0))  # 2005-11-14 16:00:00 UTC, a minute boundary
BOUNDARY_EPOCH = EPOCH - 1  # one second before the minute turns over

REFUSED = (JobError, ValueError)


@pytest.fixture
def tmp_path() -> Path:
    """Contained, task-local scratch roots, as the other cut-assembly tests do."""
    base = Path.cwd() / "_work" / "playback-overlay-tests" / uuid.uuid4().hex
    ProjectRoot(Path.cwd()).make_dir(base.relative_to(Path.cwd()).as_posix())
    return base


# ---------------------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------------------


def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    done = subprocess.run(argv, capture_output=True, check=False, **kwargs)
    assert done.returncode == 0, f"{argv[0]} failed: {done.stderr.decode('utf-8', 'replace')[-1500:]}"
    return done


def _ffmpeg_pin() -> str:
    assert FFMPEG is not None
    raw = _run([FFMPEG, "-hide_banner", "-version"]).stdout.decode("utf-8")
    return normalize_ffmpeg_identity(raw)


def _decode_luma(path: Path, width: int, height: int) -> list[bytes]:
    """Every frame of ``path`` as raw 8-bit luma, in display order."""
    assert FFMPEG is not None
    raw = _run([FFMPEG, "-hide_banner", "-v", "error", "-i", str(path), "-f", "rawvideo",
                "-pix_fmt", "gray", "-"]).stdout
    size = width * height
    assert raw and len(raw) % size == 0, f"{path.name}: {len(raw)} bytes is not whole {width}x{height} frames"
    return [raw[i:i + size] for i in range(0, len(raw), size)]


def _mask(gray: bytes) -> set[int]:
    return {index for index, value in enumerate(gray) if value > 128}


def _iou(a: set[int], b: set[int]) -> float:
    return len(a & b) / max(len(a | b), 1)


def _bbox_height(mask: set[int], width: int) -> int:
    rows = {index // width for index in mask}
    return max(rows) - min(rows) + 1


def _reference_mask(workdir: Path, text: str, *, fontsize: int, x: int, y: int,
                    size: tuple[int, int]) -> set[int]:
    """``text`` drawn in isolation by the same font on black, at the same place and size."""
    assert FFMPEG is not None
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "font.ttf").write_bytes(MONO.read_bytes())
    (workdir / "reference.txt").write_text(text, encoding="utf-8")
    width, height = size
    drawtext = (f"drawtext=fontfile=font.ttf:textfile=reference.txt:expansion=none:x={x}:y={y}:"
                f"fontsize={fontsize}:fontcolor=white:box=1:boxcolor=black@0.4")
    raw = _run([FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                f"color=c=black:s={width}x{height}:r={FPS}", "-vf", drawtext, "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "gray", "-"], cwd=workdir).stdout
    return _mask(raw)


def _clock(epoch: int, seconds: int, fmt: str) -> str:
    return time.strftime(fmt, time.gmtime(epoch + seconds))


def _strip(frame: bytes, width: int, rows: int) -> bytes:
    """The overlay box region: the top ``rows`` rows across the full width."""
    return frame[:width * rows]


# ---------------------------------------------------------------------------------------
# project builder: real store, real degrade, real cut assembly
# ---------------------------------------------------------------------------------------


def _camera(camera_id: str, resolution: tuple[int, int], *,
            schedule: Sequence[ArtifactScheduleEntry] = (),
            overlay: dict[str, Any] | None = None, **fields: Any) -> CameraProfile:
    width, height = resolution
    return CameraProfile(
        camera_id=camera_id, profile_name=camera_id, fps=float(FPS), storage_resolution=resolution,
        display_aspect_ratio=f"{width}:{height}", frame_drop_schedule=list(schedule),
        overlay_model=overlay or {}, **fields,
    )


def _overlay(placement: str, *, text: str = "CAM 03", epoch: int = EPOCH,
             fmt: str | None = "%H:%M:%S", fontsize: int = 14, x: int = 8, y: int = 8) -> dict[str, Any]:
    model: dict[str, Any] = {"camera_id_text": text, "clock_epoch": epoch, "fontsize": fontsize,
                             "x": x, "y": y, "placement": placement}
    if fmt is not None:
        model["clock_format"] = fmt
    return model


def _stages(resolution: tuple[int, int]) -> list[PostStage]:
    width, height = resolution
    return [
        PostStage(name="resample", filter="scale", params={"w": width, "h": height, "flags": "bicubic"}),
        PostStage(name="decimate", filter="fps", params={"fps": FPS}),
        PostStage(name="restructure", filter="select", params={"from_schedule": "drop+freeze"}),
        PostStage(name="hold_frozen", filter="loop", params={"from_schedule": "freeze"}),
        PostStage(name="clock_timebase", filter="setpts", params={"from_schedule": "clock"}),
        PostStage(name="overlay_clock", filter="drawtext", params={"element": "clock"},
                  composite_stage="in_recording"),
        PostStage(name="output_timebase", filter="setpts",
                  params={"timebase": "1/output_fps", "pts": "N"}),
    ]


@dataclass(frozen=True)
class _ShotSpec:
    shot_id: str
    camera: CameraProfile
    source: str = "black"  # "black" (24 fps) or "pattern" (testsrc2, 30 fps)
    seconds: float = 1.5


@dataclass
class _Studio:
    root: ProjectRoot
    store: ProjectStore
    settings: Settings
    specs: tuple[_ShotSpec, ...]
    degraded: dict[str, ShotOutput] = field(default_factory=dict)


def _advance_to_review(store: ProjectStore, shot_id: str) -> None:
    for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        shot, token = store.load_with_version(Shot, shot_id, episode_id=EPISODE)
        store.update(shot.model_copy(update={"status": status, "framing_status": FramingStatus.APPROVED}),
                     expected_version=token)


def _append(store: ProjectStore, shot_id: str, output: ShotOutput) -> None:
    existing = store.list_shot_outputs(shot_id, episode_id=EPISODE)
    store.append_shot_output(output, expected_version=len(existing))


def _add_master(studio_root: ProjectRoot, store: ProjectStore, spec: _ShotSpec,
                resolution: tuple[int, int]) -> str:
    assert FFMPEG is not None
    relative = f"renders/{spec.shot_id}/v0001/run-render/master.mkv"
    studio_root.make_dir(f"renders/{spec.shot_id}/v0001/run-render")
    width, height = resolution
    source = (f"color=c=black:s={width}x{height}:r=24" if spec.source == "black"
              else f"testsrc2=s={width}x{height}:r=30")
    _run([FFMPEG, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", source,
          "-t", str(spec.seconds), "-pix_fmt", "yuv420p", "-c:v", "ffv1", "-g", "1",
          str(studio_root.resolve(relative))])
    payload = studio_root.resolve(relative).read_bytes()
    _append(store, spec.shot_id, ShotOutput(
        output_id=f"master-{spec.shot_id}", episode_id=EPISODE, shot_id=spec.shot_id,
        record_version=1, kind=ShotOutputKind.MASTER, relative_path=relative,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        engine_id="BLENDER_EEVEE", engine_build="5.2.0",
    ))
    return relative


def _add_scene(studio_root: ProjectRoot, store: ProjectStore, shot: Shot, camera: CameraProfile) -> None:
    request = RenderRequest(
        shot_id=shot.shot_id, shot_version=shot.version, run_id=f"scene-{shot.shot_id}",
        degrade_seed=shot.degrade_seed, duration_sec=shot.duration_sec, camera_profile=camera,
        scene=synthetic_interior(),
    )
    relative = f"scenes/{shot.shot_id}/v{shot.version:04d}/playback-fixture/scene.json"
    payload = json.dumps({"schema_version": 1, "plan": build_scene_plan(request)},
                         sort_keys=True).encode("utf-8")
    studio_root.write_bytes(relative, payload)
    _append(store, shot.shot_id, ShotOutput(
        output_id=f"scene-{shot.shot_id}", episode_id=EPISODE, shot_id=shot.shot_id,
        record_version=shot.version, kind=ShotOutputKind.SCENE, relative_path=relative,
        content_sha256=hashlib.sha256(payload).hexdigest(),
    ))


def _degrade(studio: _Studio, spec: _ShotSpec) -> ShotOutput:
    """The production composition and adapter, then the receipt the degrade job records."""
    request = compose_degrade_request(studio.store, shot_id=spec.shot_id, episode_id=EPISODE,
                                      run_id=f"run-{spec.shot_id}")
    derived = postprofile_spec_from_camera(request.camera_profile, request.profile)
    result = PostProfileAdapter(studio.root, studio.settings).degrade(
        request.model_copy(update={"profile": derived}))
    output = ShotOutput(
        output_id=f"degraded-{spec.shot_id}", episode_id=EPISODE, shot_id=spec.shot_id,
        record_version=1, kind=ShotOutputKind.DEGRADED, relative_path=result.output_path,
        content_sha256=result.output_sha256, profile_semver=result.profile_semver,
        filter_graph_hash=result.filter_graph_hash,
        postprofile_spec_sha256=result.postprofile_spec_sha256,
        pinned_binaries=result.pinned_binaries, frame_count=result.frame_count,
        output_fps=result.output_fps,
    )
    _append(studio.store, spec.shot_id, output)
    return output


def _build_studio(
    directory: Path, *, cut_resolution: tuple[int, int], shots: Sequence[_ShotSpec],
    stage: str = "degraded", font: bytes | None = None, font_relative: str | None = FONT_RELATIVE,
    schedule: Sequence[ArtifactScheduleEntry] = (), delivery_profile: bool = True,
) -> _Studio:
    """A project whose Shots reach ``stage``: "shots" | "master" | "degraded" (review)."""
    directory.mkdir(parents=True, exist_ok=True)
    root = ProjectRoot(directory)
    store = ProjectStore(root)
    settings = Settings(project_root=directory)
    pin = _ffmpeg_pin()
    encode = PostProfileEncode(codec="libx264", preset="ultrafast", crf=0, pix_fmt="yuv420p", threads=1)
    store.save(Project(project_id="p1", title="Playback overlay", output_profile="delivery"),
               expected_version=0)
    store.save(Episode(episode_id=EPISODE, project_id="p1", number=1, duration_target_sec=30,
                       shot_ids=[spec.shot_id for spec in shots]), expected_version=0)
    overlay = PostProfileOverlay(camera_id="CAM 03", clock_epoch=EPOCH, fontsize=16,
                                 font_relative=font_relative, x=8, y=8, placement="in_recording")
    degrade_resolution = shots[0].camera.storage_resolution
    store.save(PostProfile(
        name="shotprof", semver="0.2.0", pinned_binaries={"ffmpeg": pin},
        stages=_stages(degrade_resolution), output_fps=FPS, storage_resolution=degrade_resolution,
        sample_aspect_ratio="1/1", artifact_schedule=list(schedule), overlay=overlay,
        encode=encode, provenance="playback overlay contract fixture",
    ), expected_version=0)
    if delivery_profile:
        store.save(PostProfile(
            name="delivery", semver="1.0.0", pinned_binaries={"ffmpeg": pin}, stages=[],
            output_fps=FPS, storage_resolution=cut_resolution, sample_aspect_ratio="1/1",
            overlay=overlay, encode=encode, provenance="playback overlay contract delivery",
        ), expected_version=0)
    if font is not None:
        root.write_bytes(font_relative or FONT_RELATIVE, font)
    studio = _Studio(root, store, settings, tuple(shots))
    for camera_id in dict.fromkeys(spec.camera.camera_id for spec in shots):
        store.create(next(spec.camera for spec in shots if spec.camera.camera_id == camera_id))
    for spec in shots:
        shot = Shot(shot_id=spec.shot_id, episode_id=EPISODE, camera_id=spec.camera.camera_id,
                    camera_profile=spec.camera.camera_id, post_profile="shotprof",
                    duration_sec=spec.seconds, degrade_seed=7)
        store.create(shot)
        if stage == "shots":
            continue
        _add_scene(root, store, shot, spec.camera)
        _add_master(root, store, spec, degrade_resolution)
        if stage == "degraded":
            studio.degraded[spec.shot_id] = _degrade(studio, spec)
            _advance_to_review(store, spec.shot_id)
    return studio


class _SpyPopen:
    """Delegates to ``subprocess.Popen`` and records every argv the assembler launches."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(self, argv: Any, **kwargs: Any) -> Any:
        self.argvs.append([str(part) for part in argv])
        return subprocess.Popen(argv, **kwargs)

    @property
    def encodes(self) -> int:
        return sum(1 for argv in self.argvs if "-filter_complex" in argv)


def _sequential_timeline(studio: _Studio) -> Timeline:
    entries: list[TimelineEntry] = []
    cursor = 0.0
    for spec in studio.specs:
        receipt = studio.degraded[spec.shot_id]
        assert receipt.frame_count is not None and receipt.output_fps is not None
        duration = receipt.frame_count / receipt.output_fps
        entries.append(TimelineEntry(type="shot", ref=spec.shot_id, start_sec=cursor,
                                     duration_sec=duration))
        cursor += duration
    return Timeline(episode_id=EPISODE, entries=entries)


def _assemble(studio: _Studio, *, timeline: Timeline | None = None, output_id: str = "cut-1",
              popen: Any = None, **kwargs: Any) -> Any:
    encoder = TimelineAssembler(studio.root, studio.settings, CpuProcessSupervisor(),
                                popen=popen or subprocess.Popen)
    return assemble_episode(studio.store, encoder, episode_id=EPISODE,
                            timeline=timeline or _sequential_timeline(studio),
                            run_id=f"run-{output_id}", output_id=output_id, **kwargs)


def _cut_frames(studio: _Studio, receipt: Any, size: tuple[int, int]) -> list[bytes]:
    return _decode_luma(studio.root.resolve(receipt.relative_path), *size)


def _degraded_frames(studio: _Studio, shot_id: str, size: tuple[int, int]) -> list[bytes]:
    return _decode_luma(studio.root.resolve(studio.degraded[shot_id].relative_path), *size)


# ---------------------------------------------------------------------------------------
# R1: where the overlay is drawn
# ---------------------------------------------------------------------------------------


@needs_ffmpeg
@needs_mono
def test_r1_the_clock_is_in_the_cut_at_cut_resolution_and_not_in_the_degraded_output(tmp_path):
    degrade_size, cut_size = (352, 288), (704, 576)
    camera = _camera("cam-a", degrade_size,
                     overlay=_overlay("on_playback", text="CAM 7", fontsize=16, x=8, y=8))
    studio = _build_studio(tmp_path / "p", cut_resolution=cut_size, font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", camera)])
    receipt = _assemble(studio)

    assert tuple(receipt.storage_resolution) == cut_size
    degraded = _degraded_frames(studio, "s1", degrade_size)
    assert max(max(frame) for frame in degraded) < 40, "the degraded output carries drawn text"

    frames = _cut_frames(studio, receipt, cut_size)
    width = cut_size[0]
    seen = _mask(frames[13])  # 13 / 12 fps = 1.08 s into the shot
    assert seen, "no on_playback clock reached the assembled cut"
    text = f"CAM 7  {_clock(EPOCH, 1, '%H:%M:%S')}"
    at_cut = _reference_mask(tmp_path / "ref", text, fontsize=16, x=8, y=8, size=cut_size)
    upscaled = _reference_mask(tmp_path / "ref", text, fontsize=32, x=16, y=16, size=cut_size)
    assert _iou(seen, at_cut) >= 0.9, "the clock is not the crisp fontsize-16 drawing at cut resolution"
    assert _iou(seen, upscaled) < 0.5
    # The glyph height is measured, not inferred: fontsize at cut resolution, not 2x.
    assert abs(_bbox_height(seen, width) - _bbox_height(at_cut, width)) <= 1
    assert _bbox_height(upscaled, width) >= 1.7 * _bbox_height(seen, width)
    assert _mask(frames[0]) != seen, "the clock did not advance between 0.00 s and 1.08 s"


@needs_ffmpeg
@needs_mono
def test_r1_each_shot_uses_its_own_cameras_overlay_model(tmp_path):
    size = (352, 288)
    alpha = _camera("cam-a", size, overlay=_overlay(
        "on_playback", text="ALPHA", epoch=EPOCH, fmt="%H:%M:%S", fontsize=14, x=8, y=8))
    bravo = _camera("cam-b", size, overlay=_overlay(
        "on_playback", text="BRAVO 2", epoch=EPOCH + 7200, fmt="%d/%m/%y %I:%M:%S %p",
        fontsize=12, x=12, y=40))
    studio = _build_studio(tmp_path / "p", cut_resolution=size, font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", alpha), _ShotSpec("s2", bravo)])
    receipt = _assemble(studio)
    frames = _cut_frames(studio, receipt, size)
    first = studio.degraded["s1"].frame_count
    assert first is not None and len(frames) == first + (studio.degraded["s2"].frame_count or 0)

    expected_a = _reference_mask(
        tmp_path / "ref", f"ALPHA  {_clock(EPOCH, 1, '%H:%M:%S')}", fontsize=14, x=8, y=8, size=size)
    expected_b = _reference_mask(
        tmp_path / "ref", f"BRAVO 2  {_clock(EPOCH + 7200, 1, '%d/%m/%y %I:%M:%S %p')}",
        fontsize=12, x=12, y=40, size=size)
    segment_a, segment_b = _mask(frames[13]), _mask(frames[first + 13])
    assert _iou(segment_a, expected_a) >= 0.9, "segment 1 does not show camera A's clock"
    assert _iou(segment_b, expected_b) >= 0.9, "segment 2 does not show camera B's clock"
    assert _iou(segment_a, expected_b) < 0.3 and _iou(segment_b, expected_a) < 0.3


@needs_ffmpeg
@needs_mono
def test_r1_clock_format_is_accepted_with_on_playback_and_honoured(tmp_path):
    # 3a refused clock_format on an on_playback overlay "until step 3b". That refusal is
    # lifted: assembly accepts it and the cut carries the formatted text.
    size = (352, 288)
    fmt = "%a %b %d [%j] %I:%M:%S %p"
    camera = _camera("cam-a", size, overlay=_overlay("on_playback", text="CAM 9", fmt=fmt,
                                                     fontsize=12, x=6, y=6))
    studio = _build_studio(tmp_path / "p", cut_resolution=size, font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", camera)])
    frames = _cut_frames(studio, _assemble(studio), size)
    expected = _reference_mask(tmp_path / "ref", f"CAM 9  {_clock(EPOCH, 1, fmt)}",
                               fontsize=12, x=6, y=6, size=size)
    other = _reference_mask(tmp_path / "ref", f"CAM 9  {_clock(EPOCH, 1, '%Y-%m-%d %H:%M:%S')}",
                            fontsize=12, x=6, y=6, size=size)
    assert _iou(_mask(frames[13]), expected) >= 0.9
    assert _iou(_mask(frames[13]), other) < 0.6


@needs_ffmpeg
@needs_mono
def test_r1_an_in_recording_overlay_is_not_drawn_a_second_time_at_cut_time(tmp_path):
    size = (352, 288)
    camera = _camera("cam-a", size, overlay=_overlay("in_recording", text="CAM 5"))
    studio = _build_studio(tmp_path / "p", cut_resolution=size, font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", camera)])
    degraded = _degraded_frames(studio, "s1", size)
    assert _mask(degraded[13]), "fixture: the in_recording clock should be burned into the clip"
    cut = _cut_frames(studio, _assemble(studio), size)
    assert cut == degraded, "the cut differs from the recording: the overlay was drawn again"


# ---------------------------------------------------------------------------------------
# R2: the clock must be the clock the recording shows
# ---------------------------------------------------------------------------------------

PATTERN_SIZE = (320, 240)
BOX_ROWS = 40  # overlay at y=8, fontsize 14: the box lies inside the top 40 rows

_DROP_AND_FREEZE = (
    ArtifactScheduleEntry(type="drop", start_frame=6, count=3),
    ArtifactScheduleEntry(type="freeze", start_frame=14, count=2),
)
_SCENARIOS: dict[str, tuple[tuple[ArtifactScheduleEntry, ...], dict[str, Any]]] = {
    "drop_and_freeze": (_DROP_AND_FREEZE, {}),
    "decimation_pattern": ((), {"decimation_pattern": "2:3"}),  # mean 2.5 = 30 fps / 12 fps
    "minute_boundary_only": ((), {}),
}


def _equivalence_studio(directory: Path, scenario: str, placement: str, *,
                        epoch: int = BOUNDARY_EPOCH) -> _Studio:
    schedule, extra = _SCENARIOS[scenario]
    camera = _camera("cam-a", PATTERN_SIZE, schedule=schedule,
                     overlay=_overlay(placement, epoch=epoch), **extra)
    return _build_studio(directory, cut_resolution=PATTERN_SIZE, font=MONO.read_bytes(),
                         shots=[_ShotSpec("s1", camera, source="pattern", seconds=2)],
                         schedule=schedule)


@needs_ffmpeg
@needs_mono
@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_r2_on_playback_clock_is_pixel_identical_to_the_in_recording_clock(tmp_path, scenario):
    width = PATTERN_SIZE[0]
    recorded = _equivalence_studio(tmp_path / "recorded", scenario, "in_recording")
    composited = _equivalence_studio(tmp_path / "composited", scenario, "on_playback")
    cut_in = _cut_frames(recorded, _assemble(recorded), PATTERN_SIZE)
    cut_on = _cut_frames(composited, _assemble(composited), PATTERN_SIZE)

    assert len(cut_on) == len(cut_in) and len(cut_in) >= 20
    strips_in = [_strip(frame, width, BOX_ROWS) for frame in cut_in]
    strips_on = [_strip(frame, width, BOX_ROWS) for frame in cut_on]
    differing = [index for index, (a, b) in enumerate(zip(strips_in, strips_on, strict=True)) if a != b]
    assert not differing, f"overlay box differs from the recording in frames {differing[:12]}"
    # The comparison has teeth: the clock advances through the minute boundary, and the
    # on_playback degrade really left the overlay out.
    assert strips_in[0] != strips_in[-1]
    clip = _degraded_frames(composited, "s1", PATTERN_SIZE)
    assert any(_strip(a, width, BOX_ROWS) != b for a, b in zip(clip, strips_on, strict=True)), (
        "the on_playback degrade output already carried the clock, or the cut drew nothing")


def _offset_route() -> str:
    if "clock_offset_seconds" in inspect.signature(assemble_episode).parameters:
        return "service"
    if "clock_offset_seconds" in inspect.signature(TimelineAssembler.run).parameters:
        return "encoder"
    pytest.fail("neither assemble_episode nor TimelineAssembler.run takes clock_offset_seconds "
                "(PLAYBACK_OVERLAY_CONTRACT R2 requires the explicit hook)")


class _OffsetEncoder:
    def __init__(self, inner: TimelineAssembler, seconds: float) -> None:
        self.inner, self.seconds = inner, seconds

    def run(self, **kwargs: Any) -> Any:
        return self.inner.run(**kwargs, clock_offset_seconds=self.seconds)


def _assemble_with_offset(studio: _Studio, seconds: float, output_id: str) -> Any:
    if _offset_route() == "service":
        return _assemble(studio, output_id=output_id, clock_offset_seconds=seconds)
    inner = TimelineAssembler(studio.root, studio.settings, CpuProcessSupervisor())
    return assemble_episode(studio.store, _OffsetEncoder(inner, seconds), episode_id=EPISODE,
                            timeline=_sequential_timeline(studio), run_id=f"run-{output_id}",
                            output_id=output_id)


@needs_ffmpeg
@needs_mono
def test_r2_clock_offset_seconds_shifts_the_shown_time_by_exactly_that_many_seconds(tmp_path):
    width = PATTERN_SIZE[0]
    base = _equivalence_studio(tmp_path / "base", "minute_boundary_only", "on_playback")
    shifted_epoch = _equivalence_studio(
        tmp_path / "epoch", "minute_boundary_only", "on_playback", epoch=BOUNDARY_EPOCH + 5)
    plain = _cut_frames(base, _assemble(base), PATTERN_SIZE)
    offset = _cut_frames(base, _assemble_with_offset(base, 5, "cut-offset"), PATTERN_SIZE)
    reference = _cut_frames(shifted_epoch, _assemble(shifted_epoch), PATTERN_SIZE)

    strips = [_strip(frame, width, BOX_ROWS) for frame in offset]
    assert strips == [_strip(frame, width, BOX_ROWS) for frame in reference], (
        "an offset of 5 s does not equal a clock epoch 5 s later")
    assert strips != [_strip(frame, width, BOX_ROWS) for frame in plain], "the offset changed nothing"


@needs_ffmpeg
@needs_mono
def test_r2_gaps_and_trims_never_leak_cut_time_into_the_segments_clock(tmp_path):
    # Timeline entries carry no in-point (start_sec and duration_sec only), so a trim keeps
    # the *first* frames of the clip and a clip may be used twice. Each segment's clock
    # starts from its own first frame, however late it sits in the cut.
    size = (352, 288)
    camera = _camera("cam-a", size, overlay=_overlay("on_playback", text="CAM 4", fontsize=14))
    studio = _build_studio(tmp_path / "p", cut_resolution=size, font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", camera, seconds=2)])
    timeline = Timeline(episode_id=EPISODE, entries=[
        TimelineEntry(type="shot", ref="s1", start_sec=0.5, duration_sec=1.5),  # 0.5 s gap first
        TimelineEntry(type="shot", ref="s1", start_sec=2.0, duration_sec=1.5),  # the clip again
    ])
    frames = _cut_frames(studio, _assemble(studio, timeline=timeline), size)
    gap, segment = 6, 18  # frames at 12 fps
    assert len(frames) >= gap + 2 * segment - 2

    def shown(cut_frame: int) -> set[int]:
        return _mask(frames[cut_frame])

    def expected(second: int) -> set[int]:
        return _reference_mask(tmp_path / "ref", f"CAM 4  {_clock(EPOCH, second, '%H:%M:%S')}",
                               fontsize=14, x=8, y=8, size=size)

    # Local frame 8 is 0.67 s in (+0 s); cut time would say 1.17 s or 2.67 s.
    assert _iou(shown(gap + 8), expected(0)) >= 0.9, "first segment shows cut time, not its own"
    assert _iou(shown(gap + segment + 8), expected(0)) >= 0.9, "reused clip did not restart its clock"
    # Local frame 13 is 1.08 s in (+1 s).
    assert _iou(shown(gap + 13), expected(1)) >= 0.9
    assert _iou(shown(gap + segment + 13), expected(1)) >= 0.9
    assert not _mask(frames[2]), "the gap carries an overlay"


# ---------------------------------------------------------------------------------------
# R3: overlay fit at cut resolution
# ---------------------------------------------------------------------------------------

# 40 characters of DejaVu Sans Mono at 16 px are about 385 px wide.
_WIDE = _overlay("on_playback", text="CAM 07 STAIRWELL", fmt="%m/%d/%Y %I:%M:%S %p",
                 fontsize=16, x=8, y=8)


@needs_ffmpeg
@needs_mono
def test_r3_an_overlay_that_fits_the_degrade_frame_but_not_the_cut_is_refused_before_any_encode(tmp_path):
    camera = _camera("cam-a", (704, 576), overlay=_WIDE)  # 393 px fits 704, not 352
    studio = _build_studio(tmp_path / "p", cut_resolution=(352, 288), font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", camera)])
    spy = _SpyPopen()
    with pytest.raises(REFUSED) as caught:
        _assemble(studio, popen=spy)
    message = str(caught.value)
    assert spy.encodes == 0, "FFmpeg encode was launched before the fit refusal"
    assert "clock" in message.lower() and "352" in message and "288" in message
    assert "overlay_model.fontsize" in message and "shorten" in message.lower()
    assert not studio.store.list_episode_outputs(EPISODE)


@needs_ffmpeg
@needs_mono
def test_r3_an_overlay_that_overflows_the_degrade_frame_but_fits_the_cut_is_accepted(tmp_path):
    camera = _camera("cam-a", (352, 288), overlay=_WIDE)  # on_playback: not drawn in the 352 clip
    studio = _build_studio(tmp_path / "p", cut_resolution=(704, 576), font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", camera)])
    receipt = _assemble(studio)
    assert _mask(_cut_frames(studio, receipt, (704, 576))[13])


def _revision(store: ProjectStore, camera_id: str, **updates: Any) -> tuple[CameraProfile, int]:
    before, token = store.load_with_version(CameraProfile, camera_id)
    return before.model_copy(update={**before.next_version(), **updates}), token


@needs_ffmpeg
@needs_mono
def test_r3_revise_camera_profile_checks_on_playback_overlays_against_the_cut_resolution(tmp_path):
    camera = _camera("cam-a", (704, 576))  # fits its own frame; the cut is 352x288
    studio = _build_studio(tmp_path / "p", cut_resolution=(352, 288), font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", camera)], stage="shots")
    overflow, token = _revision(studio.store, "cam-a", overlay_model=_WIDE)
    with pytest.raises(REFUSED) as caught:
        revise_camera_profile(studio.store, overflow, expected_version=token)
    message = str(caught.value)
    assert "clock" in message.lower() and "352" in message and "overlay_model.fontsize" in message
    assert studio.store.versions(CameraProfile, "cam-a") == [1]

    fits, token = _revision(studio.store, "cam-a", overlay_model=_overlay(
        "on_playback", text="CAM 07 STAIRWELL", fmt="%m/%d/%Y %I:%M:%S %p", fontsize=12))
    result = revise_camera_profile(studio.store, fits, expected_version=token)
    assert studio.store.versions(CameraProfile, "cam-a") == [1, 2]
    assert result.overlay_fit["status"] == "checked"


@needs_ffmpeg
@needs_mono
def test_r3_revise_accepts_an_overlay_that_only_overflows_the_degrade_frame(tmp_path):
    camera = _camera("cam-a", (352, 288))
    studio = _build_studio(tmp_path / "p", cut_resolution=(704, 576), font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", camera)], stage="shots")
    wide, token = _revision(studio.store, "cam-a", overlay_model=_WIDE)
    result = revise_camera_profile(studio.store, wide, expected_version=token)
    assert studio.store.versions(CameraProfile, "cam-a") == [1, 2]
    assert result.overlay_fit["status"] == "checked"


@needs_ffmpeg
@needs_mono
def test_r3_create_camera_profile_refuses_an_overflowing_on_playback_overlay(tmp_path):
    studio = _build_studio(tmp_path / "p", cut_resolution=(352, 288), font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", _camera("cam-a", (352, 288)))], stage="shots")
    studio.store.create(Shot(shot_id="s-new", episode_id=EPISODE, camera_id="cam-new",
                             camera_profile="cam-new", post_profile="shotprof", duration_sec=1.5))
    with pytest.raises(REFUSED) as caught:
        create_camera_profile(studio.store, _camera("cam-new", (352, 288), overlay=_WIDE))
    message = str(caught.value)
    assert "clock" in message.lower() and "352" in message and "overlay_model.fontsize" in message
    assert studio.store.versions(CameraProfile, "cam-new") == []
    create_camera_profile(studio.store, _camera("cam-new", (352, 288), overlay=_overlay(
        "on_playback", text="CAM 07 STAIRWELL", fmt="%m/%d/%Y %I:%M:%S %p", fontsize=12)))
    assert studio.store.versions(CameraProfile, "cam-new") == [1]


@needs_ffmpeg
@pytest.mark.parametrize("case", ["font_missing", "cut_resolution_unknown"])
def test_r3_camera_revision_reports_undecidable_when_the_fit_cannot_be_decided(tmp_path, case):
    font = MONO.read_bytes() if case == "cut_resolution_unknown" and MONO.is_file() else None
    camera = _camera("cam-a", (704, 576))
    studio = _build_studio(tmp_path / "p", cut_resolution=(352, 288), font=font,
                           shots=[_ShotSpec("s1", camera)], stage="shots",
                           delivery_profile=case != "cut_resolution_unknown")
    wide, token = _revision(studio.store, "cam-a", overlay_model=_WIDE)
    result = revise_camera_profile(studio.store, wide, expected_version=token)
    assert studio.store.versions(CameraProfile, "cam-a") == [1, 2], "an undecidable fit refused the revision"
    assert result.overlay_fit["status"] == "undecidable", result.overlay_fit


# ---------------------------------------------------------------------------------------
# R4: the default project font
# ---------------------------------------------------------------------------------------

BUNDLE = REPO / "orchestrator" / "assets" / "fonts"
OPERATION = "install_default_overlay_font"


def _context(tmp_path: Path) -> tuple[OperationContext, Path]:
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    settings = Settings(project_root=root)
    return OperationContext(settings, ProjectRoot(root)), root


def _new_project(context: OperationContext, root: Path, project_id: str = "p1") -> Path:
    context.invoke("create_project", {"project_id": project_id, "title": "Font"})
    return root / project_id


def test_r4_the_bundle_ships_the_pinned_font_and_its_licence_as_package_data():
    font = BUNDLE / "DejaVuSansMono.ttf"
    assert font.is_file(), "orchestrator/assets/fonts/DejaVuSansMono.ttf is not bundled"
    assert hashlib.sha256(font.read_bytes()).hexdigest() == MONO_SHA256
    assert (BUNDLE / "LICENSE").read_bytes() == FIXTURE_LICENSE.read_bytes()
    assert "assets" in (REPO / "pyproject.toml").read_text(encoding="utf-8"), (
        "pyproject.toml package-data does not mention the assets directory")


def test_r4_create_project_installs_the_font_and_licence_byte_identically(tmp_path):
    store = ProjectStore(ProjectRoot(tmp_path))
    create_project(store, Project(project_id="p1", title="Seeded"))
    font = tmp_path / FONT_RELATIVE
    assert font.is_file()
    assert hashlib.sha256(font.read_bytes()).hexdigest() == MONO_SHA256
    assert (tmp_path / LICENSE_RELATIVE).read_bytes() == FIXTURE_LICENSE.read_bytes()
    for name in ("dvr-2005", "low-light-bw"):
        overlay = store.load(PostProfile, name).overlay
        assert overlay is not None and overlay.font_relative == FONT_RELATIVE
    # Consumer-2002 deliberately has no clock overlay, so it has no project font path.
    assert store.load(PostProfile, "consumer-2002").overlay is None


def test_r4_install_default_overlay_font_installs_into_a_project_without_one(tmp_path):
    context, root = _context(tmp_path)
    project = _new_project(context, root)
    (project / FONT_RELATIVE).unlink()
    (project / LICENSE_RELATIVE).unlink()

    context.invoke(OPERATION, {"project_id": "p1"})

    assert hashlib.sha256((project / FONT_RELATIVE).read_bytes()).hexdigest() == MONO_SHA256
    assert (project / LICENSE_RELATIVE).read_bytes() == FIXTURE_LICENSE.read_bytes()


@pytest.mark.parametrize("existing", [b"a creator's own font bytes", MONO.read_bytes() if MONO.is_file() else b"x"])
def test_r4_install_default_overlay_font_never_overwrites_and_names_the_existing_digest(tmp_path, existing):
    context, root = _context(tmp_path)
    project = _new_project(context, root)
    (project / FONT_RELATIVE).write_bytes(existing)
    licence_before = (project / LICENSE_RELATIVE).read_bytes() if (project / LICENSE_RELATIVE).exists() else None

    with pytest.raises(Exception) as caught:  # noqa: B017, PT011 - the error type is not fixed by the contract
        context.invoke(OPERATION, {"project_id": "p1"})

    assert hashlib.sha256(existing).hexdigest() in str(caught.value), (
        "the refusal does not name the existing font's SHA-256")
    assert (project / FONT_RELATIVE).read_bytes() == existing
    if licence_before is not None:
        assert (project / LICENSE_RELATIVE).read_bytes() == licence_before


def test_r4_the_operation_is_in_the_api_operation_table_and_routed(tmp_path):
    operation = next((item for item in OPERATIONS if item.name == OPERATION), None)
    assert operation is not None, f"{OPERATION} is not in OPERATIONS"
    assert operation.mutating and "{project_id}" in operation.path
    app = create_app(settings=Settings(project_root=tmp_path), token="t0k3n-correct-horse-battery-staple")
    assert OPERATION in {getattr(route, "name", None) for route in app.routes}


def _tamper_bundle(monkeypatch, workdir: Path) -> None:
    """Point every bundled-font path at a modified copy and every pinned digest at a wrong one."""
    context, root = _context(workdir / "prime")
    prime = _new_project(context, root, "prime")
    (prime / FONT_RELATIVE).unlink()
    (prime / LICENSE_RELATIVE).unlink()
    context.invoke(OPERATION, {"project_id": "prime"})  # imports whatever implements it
    bad = workdir / "tampered"
    bad.mkdir(parents=True)
    data = bytearray(MONO.read_bytes())
    data[-1] ^= 0xFF
    (bad / "DejaVuSansMono.ttf").write_bytes(bytes(data))
    shutil.copy(FIXTURE_LICENSE, bad / "LICENSE")
    patched = 0
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("orchestrator"):
            continue
        for attribute, value in list(vars(module).items()):
            if isinstance(value, Path) and value.name == "DejaVuSansMono.ttf":
                monkeypatch.setattr(module, attribute, bad / "DejaVuSansMono.ttf")
                patched += 1
            elif isinstance(value, Path) and value.name == "fonts" and value.parent.name == "assets":
                monkeypatch.setattr(module, attribute, bad)
                patched += 1
    assert patched, "could not find the bundled-font path in any orchestrator module to tamper with"


def test_r4_a_tampered_bundled_font_is_refused_and_never_copied(tmp_path, monkeypatch):
    context, root = _context(tmp_path)
    project = _new_project(context, root)
    (project / FONT_RELATIVE).unlink()
    (project / LICENSE_RELATIVE).unlink()
    _tamper_bundle(monkeypatch, tmp_path / "tamper")

    with pytest.raises(Exception):  # noqa: B017, PT011 - the error type is not fixed by the contract
        context.invoke(OPERATION, {"project_id": "p1"})
    assert not (project / FONT_RELATIVE).exists(), "a font failing the pinned digest was installed"

    with pytest.raises(Exception):  # noqa: B017, PT011
        create_project(ProjectStore(ProjectRoot(tmp_path / "fresh")), Project(project_id="p2", title="T"))
    assert not (tmp_path / "fresh" / FONT_RELATIVE).exists()


@needs_ffmpeg
@needs_mono
def test_r4_the_degrade_missing_font_message_names_the_operation(tmp_path):
    camera = _camera("cam-a", (352, 288), overlay=_overlay("in_recording"))
    studio = _build_studio(tmp_path / "p", cut_resolution=(352, 288), font=None,
                           shots=[_ShotSpec("s1", camera)], stage="master")
    with pytest.raises((JobError, ValueError)) as caught:
        request = compose_degrade_request(studio.store, shot_id="s1", episode_id=EPISODE, run_id="r")
        derived = postprofile_spec_from_camera(request.camera_profile, request.profile)
        PostProfileAdapter(studio.root, studio.settings).degrade(
            request.model_copy(update={"profile": derived}))
    assert OPERATION in str(caught.value)


@needs_ffmpeg
def test_r4_the_composition_missing_font_message_names_the_operation(tmp_path):
    camera = _camera("cam-a", (352, 288), overlay=_overlay("in_recording"))
    studio = _build_studio(tmp_path / "p", cut_resolution=(352, 288), font=None, font_relative=None,
                           shots=[_ShotSpec("s1", camera)], stage="master")
    with pytest.raises((JobError, ValueError)) as caught:
        compose_degrade_request(studio.store, shot_id="s1", episode_id=EPISODE, run_id="r")
    assert OPERATION in str(caught.value)


@needs_ffmpeg
@needs_mono
def test_r4_cut_assembly_without_the_font_refuses_naming_the_operation_and_never_drops_the_clock(tmp_path):
    camera = _camera("cam-a", (352, 288), overlay=_overlay("on_playback"))
    studio = _build_studio(tmp_path / "p", cut_resolution=(352, 288), font=MONO.read_bytes(),
                           shots=[_ShotSpec("s1", camera)])
    (tmp_path / "p" / FONT_RELATIVE).unlink()
    spy = _SpyPopen()
    with pytest.raises(REFUSED) as caught:
        _assemble(studio, popen=spy)
    assert OPERATION in str(caught.value)
    assert spy.encodes == 0


def test_r4_an_export_snapshot_that_ships_the_font_ships_the_licence(tmp_path):
    from orchestrator.adapters.export.snapshot import plan_snapshot

    store = ProjectStore(ProjectRoot(tmp_path))
    create_project(store, Project(project_id="p1", title="Seeded"))
    shipped = {item.relative_path for item in plan_snapshot(store)}
    assert FONT_RELATIVE in shipped
    assert LICENSE_RELATIVE in shipped, "the snapshot ships the font without its licence"


# ---------------------------------------------------------------------------------------
# R5: descriptions
# ---------------------------------------------------------------------------------------


def _summary(name: str) -> str:
    operation = next((item for item in OPERATIONS if item.name == name), None)
    assert operation is not None, f"{name} is not an operation"
    return operation.summary


def test_r5_assemble_cut_says_where_on_playback_overlays_are_drawn():
    text = _summary("assemble_cut").lower()
    assert "on_playback" in text and "draw" in text
    assert "cut resolution" in text or "at cut time" in text or "cut time" in text
    assert "review" in text


@pytest.mark.parametrize("name", ["create_post_profile", "create_camera_profile",
                                  "revise_camera_profile"])
def test_r5_profile_operations_say_clock_format_works_for_both_placements(name):
    text = _summary(name)
    assert "clock_format" in text and "on_playback" in text and "in_recording" in text


@pytest.mark.parametrize("name", ["create_project", "create_post_profile"])
def test_r5_project_and_profile_operations_name_the_default_font_and_how_to_install_it(name):
    text = _summary(name)
    assert "DejaVu Sans Mono" in text and OPERATION in text


def test_r5_the_new_operation_describes_itself():
    text = _summary(OPERATION)
    assert "DejaVu Sans Mono" in text and "assets/fonts/overlay.ttf" in text
    assert "overwrit" in text.lower() or "refus" in text.lower()


# ---------------------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------------------


@needs_ffmpeg
@needs_mono
@pytest.mark.parametrize("cut_fps", [24, 30])
def test_review_r2_ffmpeg_frame_selection_matches_the_playback_clock_through_eof(
    tmp_path, cut_fps,
):
    camera_in = _camera("cam-a", PATTERN_SIZE, overlay=_overlay("in_recording", epoch=BOUNDARY_EPOCH))
    camera_on = _camera("cam-a", PATTERN_SIZE, overlay=_overlay("on_playback", epoch=BOUNDARY_EPOCH))
    recorded = _build_studio(
        tmp_path / f"recorded-{cut_fps}", cut_resolution=PATTERN_SIZE, font=MONO.read_bytes(),
        shots=[_ShotSpec("s1", camera_in, source="pattern", seconds=2)],
    )
    composited = _build_studio(
        tmp_path / f"composited-{cut_fps}", cut_resolution=PATTERN_SIZE, font=MONO.read_bytes(),
        shots=[_ShotSpec("s1", camera_on, source="pattern", seconds=2)],
    )
    for studio in (recorded, composited):
        delivery = studio.store.load(PostProfile, "delivery")
        cut_profile = delivery.model_copy(update={
            "name": f"delivery-{cut_fps}", "output_fps": cut_fps,
        })
        studio.store.save(cut_profile, expected_version=0)
        project, token = studio.store.load_with_version(Project, "p1")
        studio.store.save(
            project.model_copy(update={"output_profile": cut_profile.name}),
            expected_version=token,
        )

    cut_in = _cut_frames(recorded, _assemble(recorded), PATTERN_SIZE)
    cut_on = _cut_frames(composited, _assemble(composited), PATTERN_SIZE)
    width = PATTERN_SIZE[0]
    strips_in = [_strip(frame, width, BOX_ROWS) for frame in cut_in]
    strips_on = [_strip(frame, width, BOX_ROWS) for frame in cut_on]
    differing = [index for index, (left, right) in enumerate(zip(strips_in, strips_on, strict=True))
                 if left != right]
    assert not differing, (
        f"{cut_fps} fps cut selected the wrong degraded-frame clock at {differing[:12]}"
    )
    assert len(strips_on) == 2 * cut_fps
    # At 30 fps, the last cut frame's nearest source index lies past the final
    # degraded frame and must use FFmpeg's EOF-held frame clock.
    if cut_fps == 30:
        assert strips_on[-1] == strips_on[-2]


@needs_ffmpeg
@needs_mono
def test_review_input_manifest_binds_playback_font_bytes_and_clock_offset(tmp_path):
    camera = _camera("cam-a", PATTERN_SIZE,
                     overlay=_overlay("on_playback", epoch=BOUNDARY_EPOCH))
    studio = _build_studio(tmp_path / "p", cut_resolution=PATTERN_SIZE,
                           font=MONO.read_bytes(), shots=[_ShotSpec("s1", camera)])
    receipt = _assemble(studio, output_id="bound-playback", clock_offset_seconds=5)
    assert receipt.input_manifest_path is not None
    manifest_bytes = studio.root.read_bytes(receipt.input_manifest_path)
    manifest = json.loads(manifest_bytes)
    assert manifest["schema_version"] == 2
    assert manifest["clock_offset_seconds"] == 5
    assert manifest["playback_overlay_fonts"] == [{
        "shot_id": "s1",
        "font_relative": FONT_RELATIVE,
        "font_sha256": hashlib.sha256(MONO.read_bytes()).hexdigest(),
    }]

    from orchestrator.service.timeline import validate_cut_input_manifest

    timeline = studio.store.load(Timeline, EPISODE)
    validate_cut_input_manifest(studio.store, receipt, timeline)

    studio.root.write_bytes(FONT_RELATIVE, b"replaced font bytes")
    with pytest.raises(JobError, match="playback overlay font dependencies changed"):
        validate_cut_input_manifest(studio.store, receipt, timeline)

    studio.root.write_bytes(FONT_RELATIVE, MONO.read_bytes())
    manifest["clock_offset_seconds"] = 6
    studio.root.write_bytes(
        receipt.input_manifest_path,
        (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    with pytest.raises(JobError, match="SHA-256 does not match its receipt"):
        validate_cut_input_manifest(studio.store, receipt, timeline)


ARIAL = Path("C:/Windows/Fonts/arial.ttf")
needs_arial = pytest.mark.skipif(not ARIAL.is_file(), reason="Arial is required for the offset-width regression")


@needs_ffmpeg
@needs_arial
def test_review_r3_cut_fit_checks_clock_offset_candidates_before_encode(tmp_path):
    epoch = calendar.timegm((2005, 11, 14, 11, 59, 59))
    camera = _camera(
        "cam-a", (104, 80),
        overlay=_overlay("on_playback", text="CAM", epoch=epoch, fmt="%I",
                         fontsize=24, x=11, y=8),
    )
    studio = _build_studio(
        tmp_path / "p", cut_resolution=(104, 80), font=ARIAL.read_bytes(),
        shots=[_ShotSpec("s1", camera, seconds=0.5)],
    )
    _assemble(studio, output_id="no-offset")

    spy = _SpyPopen()
    with pytest.raises(REFUSED) as caught:
        _assemble(studio, output_id="offset", clock_offset_seconds=1, popen=spy)
    assert "clock" in str(caught.value).lower()
    assert "104x80" in str(caught.value)
    assert spy.encodes == 0, "FFmpeg encode was launched before the offset-aware fit refusal"


@needs_ffmpeg
@needs_mono
def test_review_r3_camera_creation_checks_the_default_profile_without_matching_shots(tmp_path):
    studio = _build_studio(
        tmp_path / "p", cut_resolution=(352, 288), font=MONO.read_bytes(),
        shots=[_ShotSpec("s1", _camera("cam-a", (352, 288)))], stage="shots",
    )
    project, token = studio.store.load_with_version(Project, "p1")
    studio.store.save(project.model_copy(update={"output_profile": "shotprof"}),
                      expected_version=token)

    with pytest.raises(REFUSED) as caught:
        create_camera_profile(
            studio.store,
            _camera("cam-new", (352, 288), overlay=_WIDE),
        )
    assert "clock" in str(caught.value).lower()
    assert "352" in str(caught.value)
    assert studio.store.versions(CameraProfile, "cam-new") == []


@needs_mono
@pytest.mark.parametrize("font_relative", [FONT_RELATIVE, "assets/fonts/custom.ttf"])
def test_review_r4_export_snapshot_embeds_default_font_licence_without_source_sidecar(
    tmp_path, font_relative,
):
    from orchestrator.adapters.export.package import _copy_file
    from orchestrator.adapters.export.snapshot import plan_snapshot

    camera = _camera("cam-a", (352, 288), overlay=_overlay("on_playback"))
    studio = _build_studio(
        tmp_path / "p", cut_resolution=(352, 288), font=MONO.read_bytes(),
        font_relative=font_relative, shots=[_ShotSpec("s1", camera)], stage="shots",
    )
    assert not studio.root.resolve(LICENSE_RELATIVE).exists()
    files = plan_snapshot(studio.store)
    license_item = next(item for item in files if item.relative_path == LICENSE_RELATIVE)
    expected_license = FIXTURE_LICENSE.read_bytes()
    assert license_item.embedded_content == expected_license
    assert license_item.expected_sha256 == hashlib.sha256(expected_license).hexdigest()

    studio.root.make_dir("package")
    packaged = ProjectRoot(studio.root.resolve("package"))
    _copy_file(studio.root, packaged, license_item)
    assert packaged.read_bytes(LICENSE_RELATIVE) == expected_license
    assert not studio.root.resolve(LICENSE_RELATIVE).exists(), "snapshot altered the source project"
