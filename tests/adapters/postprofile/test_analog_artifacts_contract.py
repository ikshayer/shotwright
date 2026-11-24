"""Independent step 3c contract tests for CameraProfile.analog_artifacts.

The test surface is the derived PostProfileSpec, PostProfileAdapter.degrade, its
contained receipt, and the documented ``ntsc-pipe`` raw-rgb24 CLI. These tests
intentionally do not depend on implementation-private settings classes.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import orchestrator.adapters.postprofile.runner as postprofile_runner
from orchestrator.adapters.postprofile.profile import (
    EncodeSpec,
    OverlaySpec,
    PostProfileSpec,
    ProfileValidationError,
    ntsc_settings_from_analog_artifacts,
    validate_ntsc_frame_indices,
    validate_ntsc_geometry,
)
from orchestrator.adapters.postprofile.runner import DegradeRequest, PostProfileAdapter
from orchestrator.domain.entities import CameraProfile, PostProfile, PostStage
from orchestrator.domain.seeds import INT32_MAX, derive_seed
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings
from orchestrator.service.runner import postprofile_spec_from_camera

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="real PostProfile clips require ffmpeg and ffprobe"
)
BUILD_HINT = "tools/build_ntsc_pipe.ps1 (cargo build --release --offline)"
EPOCH = 1131984000
_ROOT = Path(__file__).resolve().parents[3]
_MONO = _ROOT / "tests" / "fixtures" / "fonts" / "DejaVuSansMono.ttf"


def _camera(
    *, fps: float = 6.0, resolution: tuple[int, int] = (320, 240),
    display_aspect_ratio: str = "4:3", **fields: Any,
) -> CameraProfile:
    return CameraProfile(
        camera_id="CAM03", profile_name="analog-contract", fps=fps,
        storage_resolution=resolution, display_aspect_ratio=display_aspect_ratio, **fields,
    )


def _stages(width: int, height: int, fps: int, *, overlay: bool, camera_effects: bool) -> list[PostStage]:
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
            params={"camera_id": "CAM 03", "clock_epoch": EPOCH, "fontsize": 12, "x": 8, "y": 8},
            composite_stage="in_recording",
        ))
    stages.append(PostStage(name="output_timebase", filter="setpts",
                            params={"timebase": "1/output_fps", "pts": "N"}))
    if camera_effects:
        stages.extend([
            PostStage(name="camera_grade", filter="eq",
                      params={"brightness": 0.02, "contrast": 1.08, "saturation": 0.75}),
            PostStage(name="camera_chroma_smear", filter="gblur", params={"sigma": 1.0}),
            PostStage(name="camera_noise", filter="noise", params={"alls": 12, "allf": "t+u"},
                      seed_source="shot"),
            PostStage(name="camera_sharpen", filter="unsharp",
                      params={"luma_msize_x": 5, "luma_msize_y": 5, "luma_amount": 0.8}),
        ])
    return stages


def _pipe_path(settings: Settings) -> Path:
    """The installed path is configured; do not guess a target/release location."""
    value = settings.binaries.ntsc_pipe_exe
    return Path(value).expanduser().resolve()


def _sha_pin(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _settings_with_pipe(settings: Settings, path: Path) -> Settings:
    binaries = settings.binaries.model_copy(update={"ntsc_pipe_exe": path})
    return settings.model_copy(update={"binaries": binaries})


def _profile(
    camera: CameraProfile,
    ffmpeg_pin: str,
    pipe_pin: str | None,
    *,
    overlay: bool = False,
    camera_effects: bool = False,
) -> PostProfileSpec:
    width, height = camera.storage_resolution
    fps = int(camera.fps)
    pins = {"ffmpeg": ffmpeg_pin}
    if pipe_pin is not None:
        pins["ntsc-pipe"] = pipe_pin
    identity = PostProfile(
        name="analog-contract", semver="0.3.0", pinned_binaries=pins,
        stages=_stages(width, height, fps, overlay=overlay, camera_effects=camera_effects),
    )
    return PostProfileSpec(
        identity=identity, output_fps=fps, storage_resolution=(width, height),
        sample_aspect_ratio="1/1",
        overlay=(OverlaySpec(camera_id="CAM 03", clock_epoch=EPOCH, fontsize=12, x=8, y=8,
                             placement="in_recording") if overlay else None),
        encode=EncodeSpec(codec="libx264", preset="ultrafast", crf=24,
                          pix_fmt="yuv420p", threads=1),
    )


@dataclass
class _Run:
    result: Any
    path: Path
    request: DegradeRequest


class _TrackingRoot(ProjectRoot):
    """Keep evidence of output writes passing through ProjectRoot.write_with."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.write_with_paths: list[str] = []

    def write_with(self, relative: str, writer: Any) -> Any:
        self.write_with_paths.append(relative)
        return super().write_with(relative, writer)


class _Lab:
    """A small generated FFV1 master, real FFmpeg, contained run outputs and receipts."""

    def __init__(self, directory: Path, *, tracking: bool = False) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory.resolve()
        self.root = (_TrackingRoot if tracking else ProjectRoot)(self.directory)
        assert FFMPEG is not None and FFPROBE is not None
        self.settings = Settings(project_root=self.directory)
        version = subprocess.run(
            [FFMPEG, "-hide_banner", "-version"], capture_output=True, check=True,
        ).stdout.decode("utf-8")
        self.ffmpeg_pin = normalize_ffmpeg_identity(version)
        self._run_index = 0
        self.master_path = self.directory / "pattern.mkv"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
             "testsrc2=s=320x240:r=24", "-t", "2", "-pix_fmt", "yuv420p", "-c:v", "ffv1",
             "-g", "1", str(self.master_path)],
            capture_output=True, check=True,
        )
        if _MONO.is_file():
            self.root.write_bytes("font.ttf", _MONO.read_bytes())

    def request(
        self,
        camera: CameraProfile,
        *,
        seed: int = 20051114,
        shot_id: str = "S003",
        run_id: str | None = None,
        overlay: bool = False,
        camera_effects: bool = False,
        pipe_pin: str | None = None,
    ) -> DegradeRequest:
        self._run_index += 1
        run_id = run_id or f"run-{self._run_index:03d}"
        spec = _profile(camera, self.ffmpeg_pin, pipe_pin, overlay=overlay,
                        camera_effects=camera_effects)
        derived = postprofile_spec_from_camera(camera, spec)
        return DegradeRequest(
            shot_id=shot_id, shot_version=1, run_id=run_id, degrade_seed=seed,
            master_relative="pattern.mkv", font_relative=("font.ttf" if overlay else None),
            profile=derived, camera_profile=camera,
        )

    def degrade(
        self,
        camera: CameraProfile,
        *,
        seed: int = 20051114,
        shot_id: str = "S003",
        overlay: bool = False,
        camera_effects: bool = False,
        pipe_path: Path | None = None,
        pipe_pin: str | None = None,
        popen: Any = subprocess.Popen,
    ) -> tuple[_Run, Any]:
        if pipe_path is None:
            pipe_path = _pipe_path(self.settings)
        if pipe_pin is None and pipe_path.is_file():
            pipe_pin = _sha_pin(pipe_path)
        settings = _settings_with_pipe(self.settings, pipe_path)
        request = self.request(camera, seed=seed, shot_id=shot_id, overlay=overlay,
                               camera_effects=camera_effects, pipe_pin=pipe_pin)
        result = PostProfileAdapter(self.root, settings, popen=popen).degrade(request)
        return _Run(result, self.root.resolve(result.output_path), request), settings


@pytest.fixture
def lab(tmp_path: Path) -> _Lab:
    if FFMPEG is None or FFPROBE is None:
        pytest.skip("real ffmpeg and ffprobe are required")
    return _Lab(tmp_path / "project")


def _require_built_pipe(settings: Settings) -> Path:
    path = _pipe_path(settings)
    if not path.is_file():
        pytest.skip(f"ntsc-pipe is not built; run {BUILD_HINT}")
    return path


class _RecordingPopen:
    def __init__(self, real: Any = subprocess.Popen) -> None:
        self.real = real
        self.calls: list[tuple[list[str], dict[str, Any], Any]] = []

    def __call__(self, argv: Any, **kwargs: Any) -> Any:
        args = [str(value) for value in argv]
        process = self.real(args, **kwargs)
        self.calls.append((args, kwargs.copy(), process))
        return process

    @property
    def commands(self) -> list[list[str]]:
        return [call[0] for call in self.calls]


def _ntsc_command(argv: list[str]) -> bool:
    return "--settings" in argv and "--width" in argv and "--seed" in argv


def _captured_settings_path(recorder: _RecordingPopen) -> Path:
    command = next((argv for argv in recorder.commands if _ntsc_command(argv)), None)
    assert command is not None, "active analog_artifacts did not launch the configured ntsc-pipe"
    return Path(command[command.index("--settings") + 1]).resolve()


def _filter_commands(recorder: _RecordingPopen) -> list[tuple[list[str], str]]:
    result = []
    for argv in recorder.commands:
        if "-vf" in argv:
            result.append((argv, argv[argv.index("-vf") + 1]))
    return result


def _walk_pairs(value: Any):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).lower().replace("_", ""), nested
            yield from _walk_pairs(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_pairs(nested)


def _contains_setting(document: Any, key: str, expected: Any) -> bool:
    normalized = key.lower().replace("_", "")
    for candidate_key, actual in _walk_pairs(document):
        if candidate_key != normalized:
            continue
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            if isinstance(actual, (int, float)) and not isinstance(actual, bool):
                if math.isclose(float(actual), float(expected), rel_tol=0, abs_tol=1e-6):
                    return True
        elif actual == expected:
            return True
    return False


def _raw_cli(
    exe: Path, settings: Path, frames: bytes, *, width: int = 320, height: int = 240,
    count: int | None = None, seed: int = 7, start_index: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    argv = [str(exe), "--width", str(width), "--height", str(height), "--seed", str(seed),
            "--settings", str(settings)]
    if count is not None:
        argv.extend(["--frames", str(count)])
    if start_index is not None:
        argv.extend(["--frame-index", str(start_index)])
    return subprocess.run(argv, input=frames, capture_output=True, check=False, timeout=30)


def _settings_camera(**updates: Any) -> CameraProfile:
    value = {
        "head_switching": {
            "height": 17, "offset": 9, "horizontal_shift": -31,
            "position": 43.5, "jitter": 22.5,
        },
        "vhs": {
            "tape_speed": "EP", "chroma_loss": 67.5,
            "edge_wave": {"intensity": 8.5, "speed": 7.25, "frequency": 0.37, "detail": 4},
        },
        "tracking_noise": {
            "height": 19, "wave_intensity": 27.5, "snow_intensity": 0.7,
            "snow_anisotropy": 63.5, "noise_intensity": 54.5,
        },
        "signal": {
            "composite_sharpening": 0.8, "luma_smear": 0.35,
            "phase_error": 35.5, "phase_noise": 42.5,
            "horizontal_delay": 13, "vertical_delay": -7,
            "chroma_lowpass_in": "Light", "chroma_lowpass_out": "Full",
            "chroma_demodulation": "TwoD", "input_luma_filter": "Notch",
            "field_mode": "InterleavedUpper", "filter_type": "Butterworth",
            "vertical_chroma_blend": True,
        },
        "noise": {
            "composite": {"intensity": 38.5, "frequency": 0.73, "detail": 4},
            "luma": {"intensity": 22.5, "frequency": 0.46, "detail": 3},
            "chroma": {"intensity": 17.5, "frequency": 0.29, "detail": 2},
            "snow_intensity": 28.5, "snow_anisotropy": 64.5,
        },
        "ringing": {"frequency": 41.5, "power": 7, "intensity": 6.5},
    }
    for group, fields in updates.items():
        value[group] = fields
    return _camera(analog_artifacts=value)


# R1, R2: {} is neutral, has no marker, and leaves the old graph and output hash intact.


@needs_ffmpeg
def test_empty_analog_model_is_inactive_and_neutral_graph_hashes_are_byte_identical(lab: _Lab) -> None:
    default_camera = _camera()
    empty_camera = _camera(analog_artifacts={})
    assert not isinstance(empty_camera.analog_artifacts, dict)
    assert all(value is None for value in empty_camera.analog_artifacts.model_dump().values())

    first_recorder = _RecordingPopen()
    first, _ = lab.degrade(default_camera, popen=first_recorder)
    second_recorder = _RecordingPopen()
    second, _ = lab.degrade(empty_camera, popen=second_recorder)

    assert not any(_ntsc_command(argv) for argv in first_recorder.commands + second_recorder.commands)
    assert first.result.filter_graph == second.result.filter_graph
    assert first.result.filter_graph_hash == second.result.filter_graph_hash
    assert first.result.output_sha256 == second.result.output_sha256
    assert hashlib.sha256(first.result.filter_graph.encode()).hexdigest() == first.result.filter_graph_hash


# R1: all model levels reject extras; finite numeric values stay inside their documented ranges.


@pytest.mark.parametrize("analog", [
    {"unexpected": 1},
    {"scale_settings": {}},
    {"random_seed": 5},
    {"head_switching": {"unexpected": 1}},
    {"tracking_noise": {"unexpected": 1}},
    {"signal": {"unexpected": 1}},
    {"noise": {"unexpected": 1}},
    {"ringing": {"unexpected": 1}},
    {"vhs": {"unexpected": 1}},
    {"vhs": {"edge_wave": {"unexpected": 1}}},
    {"vhs": {"sharpen": {"unexpected": 1}}},
    {"noise": {"composite": {"unexpected": 1}}},
    {"noise": {"luma": {"unexpected": 1}}},
    {"noise": {"chroma": {"unexpected": 1}}},
])
def test_analog_artifacts_and_nested_groups_forbid_unknown_keys(analog: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _camera(analog_artifacts=analog)


def test_analog_artifacts_groups_are_frozen_and_documented_endpoints_are_accepted() -> None:
    camera = _camera(analog_artifacts={
        "head_switching": {"height": 0, "offset": 24, "horizontal_shift": -100,
                           "position": 0, "jitter": 100},
        "tracking_noise": {"height": 120, "wave_intensity": -50, "snow_intensity": 1,
                            "snow_anisotropy": 100, "noise_intensity": 0},
        "vhs": {"tape_speed": "SP", "chroma_loss": 0,
                "edge_wave": {"intensity": 0, "speed": 0, "frequency": 0, "detail": 1},
                "sharpen": {"intensity": 0, "frequency": 0.5}},
        "signal": {"composite_sharpening": -1, "luma_smear": 1},
        "noise": {"composite": {"intensity": 0, "frequency": 0, "detail": 1},
                  "luma": {"intensity": 100, "frequency": 1, "detail": 5},
                  "chroma": {"intensity": 0, "frequency": 0.5, "detail": 1},
                  "snow_intensity": 0, "snow_anisotropy": 100},
        "ringing": {"frequency": 100, "power": 10, "intensity": 0},
    })
    with pytest.raises((ValidationError, TypeError)):
        camera.analog_artifacts.ringing.intensity = 1


@pytest.mark.parametrize("analog", [
    {"head_switching": {"height": 25}},
    {"head_switching": {"offset": -1}},
    {"head_switching": {"horizontal_shift": 101}},
    {"head_switching": {"position": -0.1}},
    {"head_switching": {"jitter": 100.1}},
    {"tracking_noise": {"height": 121}},
    {"tracking_noise": {"wave_intensity": -50.1}},
    {"tracking_noise": {"snow_intensity": 1.1}},
    {"tracking_noise": {"snow_anisotropy": -0.1}},
    {"tracking_noise": {"noise_intensity": 100.1}},
    {"vhs": {"tape_speed": "AUTO"}},
    {"vhs": {"chroma_loss": 100.1}},
    {"vhs": {"edge_wave": {"intensity": 20.1}}},
    {"vhs": {"edge_wave": {"speed": -0.1}}},
    {"vhs": {"edge_wave": {"frequency": 0.501}}},
    {"vhs": {"edge_wave": {"detail": 0}}},
    {"vhs": {"sharpen": {"intensity": 5.1}}},
    {"vhs": {"sharpen": {"frequency": 0.49}}},
    {"signal": {"composite_sharpening": 2.01}},
    {"signal": {"luma_smear": -0.01}},
    {"signal": {"phase_error": 100.1}},
    {"signal": {"phase_noise": -0.1}},
    {"signal": {"horizontal_delay": 40.1}},
    {"signal": {"vertical_delay": -20.1}},
    {"signal": {"chroma_lowpass_out": "Heavy"}},
    {"signal": {"chroma_demodulation": "Invalid"}},
    {"signal": {"chroma_lowpass_in": "Heavy"}},
    {"signal": {"input_luma_filter": "Adaptive"}},
    {"signal": {"field_mode": "Progressive"}},
    {"signal": {"filter_type": "Gaussian"}},
    {"noise": {"composite": {"intensity": 100.1}}},
    {"noise": {"composite": {"frequency": -0.1}}},
    {"noise": {"luma": {"frequency": 1.01}}},
    {"noise": {"luma": {"intensity": -0.1}}},
    {"noise": {"chroma": {"frequency": 0.501}}},
    {"noise": {"chroma": {"detail": 0}}},
    {"noise": {"chroma": {"intensity": 100.1}}},
    {"noise": {"snow_intensity": 100.1}},
    {"noise": {"snow_anisotropy": -0.1}},
    {"ringing": {"frequency": 100.1}},
    {"ringing": {"power": 0.9}},
    {"ringing": {"intensity": 10.1}},
    {"ringing": {"intensity": float("nan")}},
    {"ringing": {"intensity": float("inf")}},
])
def test_analog_artifacts_reject_out_of_range_and_nonfinite_values(analog: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _camera(analog_artifacts=analog)


# R3, R4: the real executable sees the derived seed, translates typed controls, and processes
# exactly one rgb24 output frame for every complete input frame at its assigned frame index.


@needs_ffmpeg
def test_real_pipe_settings_translation_seed_and_receipt_bind_the_executable(lab: _Lab) -> None:
    executable = _require_built_pipe(lab.settings)
    pin = _sha_pin(executable)
    camera = _settings_camera()
    recorder = _RecordingPopen()
    run, _settings = lab.degrade(camera, seed=982341, popen=recorder,
                                 pipe_path=executable, pipe_pin=pin)

    command = next(argv for argv in recorder.commands if _ntsc_command(argv))
    expected_seed = derive_seed("S003", 982341, "camera_analog_artifacts", INT32_MAX)
    assert command[command.index("--seed") + 1] == str(expected_seed)
    assert 0 <= expected_seed <= INT32_MAX
    settings_path = _captured_settings_path(recorder)
    assert settings_path.is_file()
    frame_argument = int(command[command.index("--frames") + 1])
    assert frame_argument == run.result.frame_count
    if "--frame-index" in command:
        assert command[command.index("--frame-index") + 1] == "0"
    settings_json = json.loads(settings_path.read_text(encoding="utf-8"))
    # Assert the source-pinned crate keys and units after the public controls have
    # been translated (enum codes and percentage-to-ratio conversions included).
    for key, value in (
        ("vhs_tape_speed", 3),  # EP -> none=0, SP=1, LP=2, EP=3
        ("vhs_chroma_loss", 0.675),
        ("tracking_noise_wave_intensity", 27.5),
        ("tracking_noise_snow_intensity", 0.7),
        ("tracking_noise_noise_intensity", 0.545),
        ("ringing_frequency", 0.415),
        ("head_switching_height", 17),
        ("chroma_delay_horizontal", 13),
        ("chroma_phase_error", 0.355),
        ("ringing_scale", 6.5),
    ):
        assert _contains_setting(settings_json, key, value), (
            f"typed analog_artifacts setting {key}={value!r} did not reach ntsc-rs JSON")
    assert run.result.pinned_binaries["ntsc-pipe"] == pin
    metadata = json.loads(lab.root.read_bytes(run.result.metadata_path).decode("utf-8"))
    assert metadata["pinned_binaries"]["ntsc-pipe"] == pin


@needs_ffmpeg
def test_raw_frame_protocol_preserves_byte_counts_and_frame_index_independence(lab: _Lab) -> None:
    executable = _require_built_pipe(lab.settings)
    recorder = _RecordingPopen()
    run, _settings = lab.degrade(_settings_camera(), popen=recorder,
                                 pipe_path=executable, pipe_pin=_sha_pin(executable))
    settings_path = _captured_settings_path(recorder)
    width, height, frame_count = 320, 240, 5
    frame_bytes = width * height * 3

    def make_frame(index: int) -> bytes:
        return bytes((offset * 13 + index * 47) % 256 for offset in range(frame_bytes))

    original = b"".join(make_frame(index) for index in range(frame_count))
    changed_prefix = b"".join(make_frame(index + 80) for index in range(frame_count - 1))
    changed = changed_prefix + make_frame(frame_count - 1)
    args = dict(width=width, height=height, count=frame_count, seed=7)
    first = _raw_cli(executable, settings_path, original, **args)
    second = _raw_cli(executable, settings_path, changed, **args)
    assert first.returncode == second.returncode == 0, (first.stderr, second.stderr)
    assert len(first.stdout) == len(second.stdout) == frame_count * frame_bytes

    # Frame 4 is stable when all preceding input frames change, and a one-frame window
    # explicitly starting at index 4 yields that same frame's output.
    last_start = (frame_count - 1) * frame_bytes
    last_end = frame_count * frame_bytes
    assert first.stdout[last_start:last_end] == second.stdout[last_start:last_end]
    frame_four = _raw_cli(executable, settings_path, make_frame(4), width=width, height=height,
                          count=1, seed=7, start_index=4)
    frame_five = _raw_cli(executable, settings_path, make_frame(4), width=width, height=height,
                          count=1, seed=7, start_index=5)
    assert frame_four.returncode == frame_five.returncode == 0
    assert frame_four.stdout == first.stdout[last_start:last_end]
    assert frame_five.stdout != frame_four.stdout, "the requested frame index did not affect output"

    short = _raw_cli(executable, settings_path, make_frame(0), width=width, height=height,
                     count=2, seed=7)
    extra = _raw_cli(executable, settings_path, make_frame(0) + make_frame(1) + make_frame(2),
                     width=width, height=height, count=2, seed=7)
    partial = _raw_cli(executable, settings_path, make_frame(0) + b"x", width=width,
                       height=height, count=None, seed=7)
    assert short.returncode != 0 and b"ended after" in short.stderr
    assert extra.returncode != 0 and b"more bytes" in extra.stderr
    assert partial.returncode != 0 and b"complete rgb24 frame" in partial.stderr
    assert run.result.frame_count > 0


@needs_ffmpeg
def test_real_binary_and_ffmpeg_are_repeatable_and_different_seeds_change_pixels(lab: _Lab) -> None:
    executable = _require_built_pipe(lab.settings)
    pin = _sha_pin(executable)
    camera = _settings_camera()
    first, _ = lab.degrade(camera, seed=20051114, pipe_path=executable, pipe_pin=pin)
    repeated, _ = lab.degrade(camera, seed=20051114, pipe_path=executable, pipe_pin=pin)
    other_seed, _ = lab.degrade(camera, seed=7, pipe_path=executable, pipe_pin=pin)
    neutral, _ = lab.degrade(_camera(), seed=20051114)

    assert first.result.output_sha256 == repeated.result.output_sha256
    assert first.path.read_bytes() == repeated.path.read_bytes()
    assert first.result.filter_graph == repeated.result.filter_graph
    assert first.result.filter_graph_hash == repeated.result.filter_graph_hash
    assert first.result.output_sha256 != other_seed.result.output_sha256
    assert first.result.output_sha256 != neutral.result.output_sha256


# R2, final creator decisions: camera effects precede ntsc-rs; burned-in text is processed;
# playback text stays outside this degrade.


@needs_ffmpeg
def test_tape_stage_follows_camera_effects_and_precedes_consumer_encode(lab: _Lab) -> None:
    executable = _require_built_pipe(lab.settings)
    recorder = _RecordingPopen()
    camera = _camera(
        analog_artifacts=_settings_camera(
            vhs={"tape_speed": "LP", "edge_wave": {"intensity": 9, "speed": 2,
                                                        "frequency": 0.25, "detail": 3}},
        ).analog_artifacts,
        grade_white_balance={"temperature_k": 4500, "red_gain": 1.1, "contrast": 1.1},
        chroma_smearing=3, noise_model={"luma": 18, "chroma": 12}, sharpening=0.8,
    )
    run, _settings = lab.degrade(camera, camera_effects=True, popen=recorder,
                                 pipe_path=executable, pipe_pin=_sha_pin(executable))
    commands = _filter_commands(recorder)
    assert len(commands) == 2, f"expected producer and consumer FFmpeg filter plans, got {commands!r}"
    producer_argv, producer_graph = next(
        (argv, graph) for argv, graph in commands if "colortemperature" in graph
    )
    consumer_argv, consumer_graph = next(
        (argv, graph) for argv, graph in commands if "setsar" in graph
    )
    for name in ("colortemperature", "colorchannelmixer", "eq", "gblur", "noise", "unsharp"):
        assert name in producer_graph, f"3a camera effect {name} is missing before ntsc-pipe"
    assert producer_graph.index("colortemperature") < producer_graph.index("gblur")
    assert producer_graph.index("gblur") < producer_graph.index("noise")
    assert producer_graph.index("noise") < producer_graph.index("unsharp")
    assert "drawtext" not in producer_graph or producer_graph.index("drawtext") < producer_graph.index("unsharp")
    assert "setsar" not in producer_graph
    assert "setsar" in consumer_graph
    assert "scale=" in producer_graph
    assert "analog_artifacts" not in producer_graph
    pipe_argv = next(argv for argv in recorder.commands if _ntsc_command(argv))
    assert pipe_argv[pipe_argv.index("--width") + 1] == "320"
    assert pipe_argv[pipe_argv.index("--height") + 1] == "240"
    for argv in (producer_argv, consumer_argv):
        assert "+bitexact" in argv
    assert consumer_argv[consumer_argv.index("-threads") + 1] == "1"
    for argv, kwargs, _process in recorder.calls:
        if "-i" in argv or _ntsc_command(argv):
            assert kwargs.get("shell", False) is False
    commands_started = [argv for argv in recorder.commands if _ntsc_command(argv)]
    assert len(commands_started) == 1
    assert commands_started[0][0] == str(executable)
    assert run.result.output_path.endswith("degraded.mp4")


@needs_ffmpeg
def test_in_recording_overlay_is_tape_processed_but_on_playback_is_not(lab: _Lab) -> None:
    executable = _require_built_pipe(lab.settings)
    pin = _sha_pin(executable)
    if not _MONO.is_file():
        pytest.skip("DejaVu Sans Mono fixture font is absent")
    tape = _settings_camera(vhs={"tape_speed": "EP", "chroma_loss": 80})
    no_tape_overlay, _ = lab.degrade(
        _camera(overlay_model={"camera_id_text": "CAM 03", "clock_epoch": EPOCH,
                               "clock_format": "%H:%M:%S", "fontsize": 12, "x": 8, "y": 8}),
        overlay=True,
    )
    no_tape_base, _ = lab.degrade(_camera())
    tape_overlay, _ = lab.degrade(
        _camera(overlay_model={"camera_id_text": "CAM 03", "clock_epoch": EPOCH,
                               "clock_format": "%H:%M:%S", "fontsize": 12, "x": 8, "y": 8},
                analog_artifacts=tape.analog_artifacts),
        overlay=True, pipe_path=executable, pipe_pin=pin,
    )
    active_base, _ = lab.degrade(tape, pipe_path=executable, pipe_pin=pin)

    def first_rgb(path: Path) -> bytes:
        done = subprocess.run(
            [FFMPEG, "-hide_banner", "-v", "error", "-i", str(path), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True,
        )
        return done.stdout

    clock_without_tape = first_rgb(no_tape_overlay.path)
    clock_with_tape = first_rgb(tape_overlay.path)
    base_with_tape = first_rgb(active_base.path)
    # The clock occupies the fixture's upper-left inset. Its additive pixel delta changes
    # after the tape stage, which distinguishes a pre-tape burn-in from a post-tape overlay.
    roi = [(y * 320 + x) * 3 for y in range(6, 30) for x in range(6, 210)]
    plain_base = first_rgb(no_tape_base.path)
    clock_mask = [offset for offset in roi if any(
        abs(clock_without_tape[offset + channel] - plain_base[offset + channel]) > 8
        for channel in range(3)
    )]
    assert clock_mask, "fixture clock did not produce measurable overlay pixels"
    delta_without_tape = sum(
        sum(abs(clock_without_tape[offset + channel] - plain_base[offset + channel])
            for channel in range(3)) for offset in clock_mask
    )
    delta_with_tape = sum(
        sum(abs(clock_with_tape[offset + channel] - base_with_tape[offset + channel])
            for channel in range(3)) for offset in clock_mask
    )
    assert delta_without_tape > 0 and delta_with_tape > 0
    assert delta_without_tape != delta_with_tape, "in_recording clock pixels bypassed ntsc-rs"

    playback_overlay = _camera(overlay_model={
        "camera_id_text": "PLAYBACK LABEL", "clock_epoch": EPOCH,
        "clock_format": "%H:%M:%S", "fontsize": 12, "x": 8, "y": 8,
        "placement": "on_playback",
    }, analog_artifacts=tape.analog_artifacts)
    playback_run, _ = lab.degrade(
        playback_overlay, overlay=True, pipe_path=executable, pipe_pin=pin,
    )
    no_overlay_run, _ = lab.degrade(tape, pipe_path=executable, pipe_pin=pin)
    assert playback_run.result.output_sha256 == no_overlay_run.result.output_sha256
    assert playback_run.path.read_bytes() == no_overlay_run.path.read_bytes()


# R4, R5, R6, R2: receipts, preflight failures, child teardown and containment.


@needs_ffmpeg
def test_settings_change_the_spec_receipt_hash_and_all_job_files_stay_contained(
    tmp_path: Path,
) -> None:
    if FFMPEG is None or FFPROBE is None:
        pytest.skip("real ffmpeg and ffprobe are required")
    lab = _Lab(tmp_path / "project", tracking=True)
    executable = _require_built_pipe(lab.settings)
    pin = _sha_pin(executable)
    camera_a = _settings_camera()
    camera_b = _settings_camera(ringing={"frequency": 44.5, "power": 7, "intensity": 6.5})
    recorder = _RecordingPopen()
    first, _ = lab.degrade(camera_a, pipe_path=executable, pipe_pin=pin, popen=recorder)
    second, _ = lab.degrade(camera_b, pipe_path=executable, pipe_pin=pin)

    assert first.result.postprofile_spec_sha256 != second.result.postprofile_spec_sha256
    assert first.result.pinned_binaries["ntsc-pipe"] == pin
    settings_path = _captured_settings_path(recorder)
    relative_settings = settings_path.relative_to(lab.directory).as_posix()
    assert settings_path.is_file() and relative_settings in lab.root.write_with_paths
    for relative in (first.result.output_path, first.result.log_path, first.result.metadata_path,
                     relative_settings):
        assert relative in lab.root.write_with_paths
        assert ".." not in Path(relative).parts
        assert lab.root.resolve(relative).is_relative_to(lab.directory)
    for relative in lab.root.write_with_paths:
        assert ".." not in Path(relative).parts
        assert lab.root.resolve(relative).is_relative_to(lab.directory)
    run_dir = lab.root.resolve(first.result.output_path).parent
    actual_run_files = {
        path.relative_to(lab.directory).as_posix()
        for path in run_dir.rglob("*") if path.is_file()
    }
    assert actual_run_files <= set(lab.root.write_with_paths), (
        "job files were written without ProjectRoot.write_with: "
        f"{sorted(actual_run_files - set(lab.root.write_with_paths))}")


@needs_ffmpeg
@pytest.mark.parametrize(
    "mode,expected_kind", [("missing", JobErrorKind.WORKER_UNREACHABLE),
                            ("hash", JobErrorKind.INVALID_MANIFEST)],
)
def test_missing_or_mismatched_pipe_fails_before_any_ffmpeg_process(
    lab: _Lab, mode: str, expected_kind: JobErrorKind,
) -> None:
    if mode == "missing":
        executable = lab.directory / "not-built" / "ntsc-pipe.exe"
        pin = "sha256:" + "0" * 64
    else:
        executable = Path(sys.executable).resolve()
        pin = "sha256:" + "0" * 64
    request = lab.request(_settings_camera(), run_id=f"preflight-{mode}", pipe_pin=pin)
    settings = _settings_with_pipe(lab.settings, executable)
    recorder = _RecordingPopen()

    with pytest.raises(JobError) as caught:
        PostProfileAdapter(lab.root, settings, popen=recorder).degrade(request)

    assert caught.value.kind is expected_kind
    message = str(caught.value).lower()
    if mode == "missing":
        assert "tools/build_ntsc_pipe.ps1" in message
    else:
        assert "sha" in message or "hash" in message
    assert recorder.calls == [], f"FFmpeg launched before {mode} ntsc-pipe preflight completed"


def _child_mode_popen(mode: str):
    """Replace just ntsc-pipe with a controllable child; keep real FFmpeg around it."""
    started = threading.Event()
    calls: list[tuple[list[str], Any]] = []
    real = subprocess.Popen
    if mode in {"fail", "protocol", "pass"}:
        exit_code = {"fail": 23, "protocol": 2, "pass": 0}[mode]
        script = (
            "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); "
            f"sys.stdout.buffer.flush(); sys.exit({exit_code})"
        )
    elif mode == "hang":
        script = "import time\nwhile True: time.sleep(0.05)"
    else:
        raise ValueError(f"unknown controllable child mode: {mode}")

    def popen(argv: Any, **kwargs: Any) -> Any:
        args = [str(value) for value in argv]
        if _ntsc_command(args):
            child = real([sys.executable, "-u", "-c", script], **kwargs)
            calls.append((args, child))
            started.set()
            return child
        child = real(args, **kwargs)
        if "-i" in args:
            calls.append((args, child))
        return child

    return popen, started, calls


def _assert_failed_run_has_no_video(lab: _Lab, run_id: str) -> None:
    run_dir = lab.root.resolve(f"post/S003/v0001/{run_id}")
    if run_dir.exists():
        video_artifacts = [
            path for path in run_dir.rglob("*") if path.is_file()
            and (path.suffix.lower() in {".mp4", ".mkv", ".mov", ".avi"}
                 or ".mp4" in path.name.lower() or ".mkv" in path.name.lower())
        ]
        assert not video_artifacts, f"failed/cancelled/expired run left partial video: {video_artifacts}"


def _assert_failure_metadata_and_logs(lab: _Lab, run_id: str) -> None:
    run_dir = lab.root.resolve(f"post/S003/v0001/{run_id}")
    assert (run_dir / "metadata.json").is_file(), "failed run metadata was not retained"
    assert list(run_dir.rglob("*.log")), "failed run did not retain stage diagnostics"


@needs_ffmpeg
def test_ntsc_subprocess_failure_is_classified_and_leaves_no_child_or_partial_artifact(lab: _Lab) -> None:
    executable = Path(sys.executable).resolve()
    pin = _sha_pin(executable)
    popen, _started, children = _child_mode_popen("fail")
    request = lab.request(_settings_camera(), run_id="child-failure", pipe_pin=pin)

    with pytest.raises(JobError) as caught:
        PostProfileAdapter(lab.root, _settings_with_pipe(lab.settings, executable),
                           popen=popen).degrade(request)

    assert isinstance(caught.value.kind, JobErrorKind)
    assert "23" in str(caught.value) or "ntsc" in str(caught.value).lower()
    _assert_failed_run_has_no_video(lab, request.run_id)
    _assert_failure_metadata_and_logs(lab, request.run_id)
    assert children and all(process.poll() is not None for _argv, process in children)


@needs_ffmpeg
def test_parent_cancellation_stops_all_three_children_without_partial_artifact(lab: _Lab) -> None:
    executable = Path(sys.executable).resolve()
    pin = _sha_pin(executable)
    popen, started, children = _child_mode_popen("hang")
    request = lab.request(_settings_camera(), run_id="cancelled-run", pipe_pin=pin)
    adapter = PostProfileAdapter(lab.root, _settings_with_pipe(lab.settings, executable), popen=popen)
    cancel = threading.Event()
    adapter.set_cancel_check(cancel.is_set)
    outcome: list[BaseException] = []

    def invoke() -> None:
        try:
            adapter.degrade(request)
        except Exception as exc:  # collect across the worker thread for assertion below.
            outcome.append(exc)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    assert started.wait(15), "the controllable ntsc-pipe child was never launched"
    cancel.set()
    worker.join(20)
    assert not worker.is_alive(), "adapter did not return after parent cancellation"
    assert len(outcome) == 1 and isinstance(outcome[0], JobError)
    assert outcome[0].kind is JobErrorKind.CANCELLED
    _assert_failed_run_has_no_video(lab, request.run_id)
    _assert_failure_metadata_and_logs(lab, request.run_id)
    assert children and all(process.poll() is not None for _argv, process in children)


@needs_ffmpeg
def test_per_shot_deadline_stops_children_and_maps_to_budget_exceeded(lab: _Lab) -> None:
    executable = Path(sys.executable).resolve()
    pin = _sha_pin(executable)
    popen, started, children = _child_mode_popen("hang")
    request = lab.request(_settings_camera(), run_id="deadline-run", pipe_pin=pin)
    adapter = PostProfileAdapter(lab.root, _settings_with_pipe(lab.settings, executable), popen=popen)
    # JobQueue hands its monotonic per-shot deadline to the adapter supervisor.
    adapter.set_deadline(time.monotonic() + 1.0)
    outcome: list[BaseException] = []

    def invoke() -> None:
        try:
            adapter.degrade(request)
        except Exception as exc:  # collect across the worker thread for assertion below.
            outcome.append(exc)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    assert started.wait(15), "the controllable ntsc-pipe child was never launched"
    worker.join(20)
    assert not worker.is_alive(), "adapter did not return after the per-shot deadline"
    assert len(outcome) == 1 and isinstance(outcome[0], JobError)
    assert outcome[0].kind is JobErrorKind.BUDGET_EXCEEDED
    _assert_failed_run_has_no_video(lab, request.run_id)
    _assert_failure_metadata_and_logs(lab, request.run_id)
    assert children and all(process.poll() is not None for _argv, process in children)


# Review fixes


def test_partial_analog_settings_emit_the_complete_pinned_ntsc_default_baseline() -> None:
    artifacts = _camera(analog_artifacts={
        "head_switching": {"height": 17},
    }).analog_artifacts
    settings = ntsc_settings_from_analog_artifacts(artifacts)

    expected_defaults = {
        "use_field": 4,
        "filter_type": 1,
        "input_luma_filter": 2,
        "chroma_lowpass_in": 2,
        "chroma_demodulation": 1,
        "chroma_lowpass_out": 2,
        "luma_smear": 0.5,
        "composite_preemphasis": 1.0,
        "head_switching": True,
        "head_switching_height": 17,
        "head_switching_offset": 3,
        "head_switching_horizontal_shift": 72.0,
        "head_switching_start_mid_line": True,
        "head_switching_mid_line_position": 0.95,
        "head_switching_mid_line_jitter": 0.03,
        "tracking_noise": True,
        "tracking_noise_height": 12,
        "tracking_noise_wave_intensity": 15.0,
        "tracking_noise_snow_intensity": 0.025,
        "tracking_noise_snow_anisotropy": 0.25,
        "tracking_noise_noise_intensity": 0.25,
        "composite_noise": True,
        "composite_noise_intensity": 0.05,
        "composite_noise_frequency": 0.5,
        "composite_noise_detail": 1,
        "luma_noise": True,
        "luma_noise_intensity": 0.01,
        "luma_noise_frequency": 0.5,
        "luma_noise_detail": 1,
        "chroma_noise": True,
        "chroma_noise_intensity": 0.1,
        "chroma_noise_frequency": 0.05,
        "chroma_noise_detail": 2,
        "snow_intensity": 0.00025,
        "snow_anisotropy": 0.5,
        "chroma_phase_noise_intensity": 0.001,
        "chroma_phase_error": 0.0,
        "chroma_delay_horizontal": 0.0,
        "chroma_delay_vertical": 0,
        "ringing": True,
        "ringing_frequency": 0.45,
        "ringing_power": 4.0,
        "ringing_scale": 4.0,
        "vhs_settings": True,
        "vhs_tape_speed": 2,
        "vhs_chroma_loss": 0.000025,
        "vhs_sharpen_enabled": True,
        "vhs_sharpen": 0.25,
        "vhs_sharpen_frequency": 1.0,
        "vhs_edge_wave_enabled": True,
        "vhs_edge_wave": 0.5,
        "vhs_edge_wave_speed": 4.0,
        "vhs_edge_wave_frequency": 0.05,
        "vhs_edge_wave_detail": 2,
        "vhs_chroma_vert_blend": False,
        "scale_settings": True,
        "bandwidth_scale": 1.0,
        "vertical_scale": 1.0,
        "scale_with_video_size": False,
    }

    assert settings == {"version": 1, **expected_defaults}
    assert "random_seed" not in settings


@needs_ffmpeg
def test_ntsc_binary_remains_write_locked_through_process_creation(lab: _Lab) -> None:
    if os.name != "nt":
        pytest.skip("Windows sharing flags protect the executable on the production host")
    executable = lab.directory / "verified-ntsc-pipe.exe"
    lab.root.write_bytes("verified-ntsc-pipe.exe", b"pinned executable bytes")
    pin = _sha_pin(executable)
    request = lab.request(_settings_camera(), run_id="launch-lock", pipe_pin=pin)
    settings = _settings_with_pipe(lab.settings, executable)

    def replace_during_creation(argv: Any, **_kwargs: Any) -> object:
        assert Path(argv[0]).resolve() == executable
        with pytest.raises(PermissionError):
            executable.write_bytes(b"replacement executable bytes")
        return object()

    adapter = PostProfileAdapter(lab.root, settings, popen=replace_during_creation)
    child = adapter._launch_pipeline_child("ntsc-pipe", [str(executable)], {}, request)

    assert child is not None
    assert _sha_pin(executable) == pin


def test_ntsc_geometry_validation_rejects_one_row_interleaved_fields() -> None:
    artifacts = _camera(analog_artifacts={
        "signal": {"field_mode": "InterleavedUpper", "vertical_chroma_blend": True},
    }).analog_artifacts

    with pytest.raises(ProfileValidationError, match="2 rows in each processed field"):
        validate_ntsc_geometry(4, 2, artifacts)
    with pytest.raises(ProfileValidationError, match="width of at least 4"):
        validate_ntsc_geometry(3, 4, artifacts)
    validate_ntsc_geometry(4, 4, artifacts)


@needs_ffmpeg
def test_degrade_rejects_unsafe_ntsc_geometry_before_reserving_a_run(lab: _Lab) -> None:
    camera = _camera(
        resolution=(4, 2), display_aspect_ratio="2:1",
        analog_artifacts={"signal": {"vertical_chroma_blend": True}},
    )
    executable = Path(sys.executable).resolve()
    request = lab.request(camera, run_id="unsafe-ntsc-geometry", pipe_pin=_sha_pin(executable))
    recorder = _RecordingPopen()
    adapter = PostProfileAdapter(
        lab.root, _settings_with_pipe(lab.settings, executable), popen=recorder,
    )

    with pytest.raises(JobError) as caught:
        adapter.degrade(request)

    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "2 rows in each processed field" in caught.value.message
    assert not any(_ntsc_command(argv) for argv in recorder.commands)
    assert not lab.root.resolve("post/S003/v0001/unsafe-ntsc-geometry").exists()


@needs_ffmpeg
def test_ntsc_protocol_exit_code_maps_to_corrupt_artifact_and_removes_partial_output(
    lab: _Lab,
) -> None:
    executable = Path(sys.executable).resolve()
    pin = _sha_pin(executable)
    popen, _started, children = _child_mode_popen("protocol")
    request = lab.request(_settings_camera(), run_id="raw-protocol-error", pipe_pin=pin)

    with pytest.raises(JobError) as caught:
        PostProfileAdapter(lab.root, _settings_with_pipe(lab.settings, executable),
                           popen=popen).degrade(request)

    assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert "ntsc-pipe exited with status 2" in str(caught.value)
    _assert_failed_run_has_no_video(lab, request.run_id)
    assert children and all(process.poll() is not None for _argv, process in children)


@needs_ffmpeg
def test_output_probe_diagnostic_write_failure_still_removes_partial_video(lab: _Lab) -> None:
    executable = Path(sys.executable).resolve()
    pin = _sha_pin(executable)
    popen, _started, children = _child_mode_popen("pass")
    request = lab.request(_settings_camera(), run_id="diagnostic-enospc", pipe_pin=pin)
    adapter = PostProfileAdapter(lab.root, _settings_with_pipe(lab.settings, executable), popen=popen)
    probe_failed = False
    original_probe = adapter._probe

    def fail_output_probe(path: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal probe_failed
        if path.name == "degraded.partial.mp4":
            probe_failed = True
            raise JobError(
                JobErrorKind.CORRUPT_ARTIFACT,
                "synthetic output probe failure",
                shot_id=request.shot_id,
                run_id=request.run_id,
            )
        return original_probe(path, *args, **kwargs)

    original_write_with = lab.root.write_with

    def fail_failure_metadata_write(relative: str, writer: Any) -> Any:
        if probe_failed and relative.endswith("/metadata.json"):
            raise OSError(errno.ENOSPC, "synthetic diagnostic metadata ENOSPC")
        return original_write_with(relative, writer)

    adapter._probe = fail_output_probe  # type: ignore[method-assign]
    lab.root.write_with = fail_failure_metadata_write  # type: ignore[method-assign]

    with pytest.raises(JobError) as caught:
        adapter.degrade(request)

    assert caught.value.kind is JobErrorKind.DISK_EXHAUSTED
    assert probe_failed
    _assert_failed_run_has_no_video(lab, request.run_id)
    assert children and all(process.poll() is not None for _argv, process in children)


@needs_ffmpeg
def test_throughput_floor_stops_a_stalled_processor_after_startup_grace(
    lab: _Lab, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(postprofile_runner, "_NTSC_PIPE_STARTUP_GRACE_SECONDS", 0.1)
    executable = Path(sys.executable).resolve()
    pin = _sha_pin(executable)
    popen, started, children = _child_mode_popen("hang")
    request = lab.request(_settings_camera(), run_id="throughput-floor", pipe_pin=pin)
    adapter = PostProfileAdapter(lab.root, _settings_with_pipe(lab.settings, executable), popen=popen)
    cancel = threading.Event()
    adapter.set_cancel_check(cancel.is_set)
    outcome: list[BaseException] = []

    def invoke() -> None:
        try:
            adapter.degrade(request)
        except Exception as exc:  # collect across the worker thread for the assertion below.
            outcome.append(exc)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    assert started.wait(15), "the controllable ntsc-pipe child was never launched"
    watchdog_started_at = time.monotonic()
    worker.join(2)
    if worker.is_alive():
        cancel.set()
        worker.join(15)

    assert not worker.is_alive(), "throughput watchdog did not release the stalled pipeline"
    assert len(outcome) == 1 and isinstance(outcome[0], JobError)
    assert outcome[0].kind is JobErrorKind.BUDGET_EXCEEDED
    assert "throughput" in outcome[0].message.lower()
    assert time.monotonic() - watchdog_started_at < 2
    _assert_failed_run_has_no_video(lab, request.run_id)
    assert children and all(process.poll() is not None for _argv, process in children)


def test_ntsc_build_script_forces_the_ignored_local_cargo_target_directory() -> None:
    script = (_ROOT / "tools" / "build_ntsc_pipe.ps1").read_text(encoding="utf-8")

    assert '$targetDirectory = Join-Path $crateDirectory "target"' in script
    assert '$releaseBinary = Join-Path $targetDirectory "release\\ntsc-pipe.exe"' in script
    assert '$cargoArgs = @("build", "--release", "--locked", "--target-dir", $targetDirectory)' in script


def test_interleaved_frame_index_validation_checks_the_crate_doubling_boundary() -> None:
    artifacts = _camera(analog_artifacts={
        "signal": {"field_mode": "InterleavedUpper"},
    }).analog_artifacts
    rust_usize_max = (1 << (8 * struct.calcsize("P"))) - 1
    highest_valid = (rust_usize_max - 1) // 2

    validate_ntsc_frame_indices(highest_valid, 1, artifacts)
    with pytest.raises(ProfileValidationError, match="interleaved field frame index"):
        validate_ntsc_frame_indices(highest_valid + 1, 1, artifacts)
