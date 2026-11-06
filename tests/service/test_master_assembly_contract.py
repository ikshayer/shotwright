"""End-to-end contract tests for a full-shot lossless master assembly.

Assumptions forced by the assembly contract: master assembly happens inside the existing
Blender render adapter and uses its injected subprocess boundary; FFV1 in Matroska is the
chosen master codec; its FFmpeg command names an image-sequence input and writes the file
that JobRunner records. Tests fake Blender, FFmpeg, and ffprobe processes and use a
15-second/900-frame render to make a one-frame receipt unmistakable.
"""

from __future__ import annotations

import io
import hashlib
import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    Project,
    Shot,
    ShotOutputKind,
)
from orchestrator.service.runner import (
    AdapterKind,
    AdapterRegistry,
    AdapterRequest,
    JobRunner,
    _AdapterShim,
)
from tests.adapters.blender.test_supervision_contract import FakePopen, setup
from tests.adapters.blender.test_scene_contract import request as render_request


_FAKE_FFMPEG_VERSION = b"ffmpeg version fake-assembly-build\n"
_MASTER_BYTES = b"fake FFV1 master representing the complete planned shot"


class _ImmediateProcess:
    pid = 9911

    def __init__(self, *, args: list[str], stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.args = args
        self.returncode = 0
        self._stdout = stdout
        self._stderr = stderr
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = -15

    def __enter__(self) -> _ImmediateProcess:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _RenderAndAssemblyPopen:
    """Use the established Blender fake and record the FFmpeg command/output."""

    def __init__(self) -> None:
        self.blender = FakePopen()
        self.calls: list[list[str]] = []
        self.encode_argv: list[str] | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        observed = [str(value) for value in argv]
        self.calls.append(observed)
        if "--background" in observed:
            return self.blender(argv, **kwargs)

        executable = Path(observed[0]).name.lower()
        if "ffprobe" in executable or "-show_frames" in observed:
            probe = {
                "streams": [{
                    "codec_type": "video", "nb_read_frames": "900",
                    "avg_frame_rate": "60/1", "time_base": "1/60", "start_time": "0",
                }],
                "frames": [{"pts_time": f"{index / 60:.6f}"} for index in range(900)],
                "format": {"duration": "15.000000"},
            }
            return _ImmediateProcess(args=observed, stdout=json.dumps(probe).encode("utf-8"))
        if "-version" in observed:
            return _ImmediateProcess(args=observed, stdout=_FAKE_FFMPEG_VERSION)
        if "-filters" in observed or "-encoders" in observed or "-h" in observed:
            return _ImmediateProcess(args=observed, stdout=b"fake ffmpeg capability response\n")

        if "-c:v" in observed and observed[-1] != "-":
            self.encode_argv = observed
            output = Path(observed[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(_MASTER_BYTES)
        return _ImmediateProcess(args=observed)


@pytest.fixture
def rendered_master(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    popen = _RenderAndAssemblyPopen()
    # Also cover assembly code that uses subprocess.run (which delegates to the module's
    # Popen) instead of the adapter's injected callable. No real process can escape.
    monkeypatch.setattr(subprocess, "Popen", popen)
    root, settings, adapter, queue, _job = setup(tmp_path, popen=popen)
    store = queue.store
    store.save(Project(project_id="p1", title="900-frame master"), expected_version=0)
    store.save(
        Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30),
        expected_version=0,
    )
    camera = CameraProfile(
        camera_id="cam-assembly", profile_name="fps sentinel", fps=7.25,
    )
    store.create(camera)
    shot = Shot(
        shot_id="shot-1", episode_id="ep1", camera_profile=camera.camera_id,
        duration_sec=15.0, degrade_seed=44221,
    )
    store.create(shot)
    request = render_request(
        shot_id=shot.shot_id,
        shot_version=shot.version,
        run_id="run-assembly",
        degrade_seed=shot.degrade_seed,
        duration_sec=shot.duration_sec,
        camera_profile=camera,
    )
    registry = AdapterRegistry({
        AdapterKind.RENDER: _AdapterShim(AdapterKind.RENDER, adapter, settings),
    })
    runner = JobRunner(
        lambda _project_id: store,
        settings,
        queue_for=lambda _project_id: queue,
        adapters=registry,
    )
    try:
        submitted = runner.submit(
            "p1",
            AdapterRequest(
                kind=AdapterKind.RENDER,
                payload=request,
                record_id=shot.shot_id,
                record_version=shot.version,
                episode_id=shot.episode_id,
            ),
        )
        completed = runner.wait("p1", submitted.job_id)
        if completed.state != "completed":
            pytest.fail(
                f"fake 900-frame render did not complete: {completed.error_kind} "
                f"{completed.error_message}"
            )
        outputs = store.list_shot_outputs(
            shot.shot_id, episode_id=shot.episode_id,
            record_version=shot.version, kind=ShotOutputKind.MASTER,
        )
        if len(outputs) != 1:
            pytest.fail(f"expected one master receipt for the fake render, observed {outputs!r}")
        plan_path = Path(popen.blender.calls[0][0][-1])
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        yield {
            "root": root,
            "store": store,
            "shot": shot,
            "request": request,
            "plan": plan,
            "receipt": outputs[0],
            "popen": popen,
        }
    finally:
        runner.close()


def _option_value(argv: list[str], *names: str) -> str | None:
    for index, argument in enumerate(argv[:-1]):
        if argument in names:
            return argv[index + 1]
        for name in names:
            if argument.startswith(name + "="):
                return argument.split("=", 1)[1]
    return None


def _video_filter_text(argv: list[str]) -> str:
    values: list[str] = []
    for index, argument in enumerate(argv[:-1]):
        if argument in {"-vf", "-filter:v", "-filter_complex"}:
            values.append(argv[index + 1])
        elif argument.startswith(("-vf=", "-filter:v=", "-filter_complex=")):
            values.append(argument.split("=", 1)[1])
    return ",".join(values).lower()


def _contained_path(root: Any, path_text: str) -> Path:
    path = Path(path_text)
    relative = path.resolve().relative_to(root.path).as_posix() if path.is_absolute() else path_text
    return root.resolve(relative)


def _master_command(state: dict[str, Any]) -> list[str]:
    command = state["popen"].encode_argv
    assert command is not None, (
        f"no master FFmpeg encode was observed among subprocess calls: {state['popen'].calls!r}"
    )
    return command


def test_master_receipt_hashes_the_file_assembled_from_all_planned_frames(
    rendered_master: dict[str, Any],
) -> None:
    root = rendered_master["root"]
    plan = rendered_master["plan"]
    receipt = rendered_master["receipt"]
    frames = plan["outputs"]["frames"]
    frame_end = plan["frame_end"]
    assert frame_end == 900, f"fixture plan has frame_end={frame_end!r}, expected 900"
    assert len(frames) == frame_end, (
        f"render plan lists {len(frames)} retained frame paths for frame_end={frame_end}"
    )
    last_frame = _contained_path(root, frames[-1])
    assert last_frame.is_file(), (
        f"fake renderer did not produce planned final frame {frames[-1]!r} for frame_end={frame_end}"
    )

    receipt_artifact = root.resolve(receipt.relative_path)
    assert receipt_artifact.is_file(), (
        f"master receipt path {receipt.relative_path!r} resolves to no file"
    )
    planned_frames = {_contained_path(root, relative) for relative in frames}
    assert receipt_artifact not in planned_frames, (
        f"master receipt still points to a single rendered frame {receipt.relative_path!r}; "
        f"the shot plan contains frame_end={frame_end} frames"
    )
    artifact_digest = hashlib.sha256(receipt_artifact.read_bytes()).hexdigest()
    assert receipt.content_sha256 == artifact_digest, (
        f"receipt digest {receipt.content_sha256!r} does not hash its recorded artifact "
        f"({artifact_digest})"
    )

    command = _master_command(rendered_master)
    input_path = _option_value(command, "-i")
    assert input_path is not None and ".exr" in input_path.lower() and re.search(
        r"%0?\d*d|[*?]", input_path,
    ), f"master FFmpeg input {input_path!r} does not name an EXR sequence for all frames"
    encoded_path = Path(command[-1]).resolve()
    assert encoded_path == receipt_artifact.resolve(), (
        f"FFmpeg encoded {str(encoded_path)!r}, but ShotOutput records "
        f"{str(receipt_artifact)!r}"
    )
    frame_limit = _option_value(command, "-frames:v", "-vframes")
    if frame_limit is not None:
        assert int(frame_limit) >= frame_end, (
            f"FFmpeg frame limit is {frame_limit!r}, observed plan.frame_end={frame_end}"
        )
    duration_limit = _option_value(command, "-t", "-to")
    if duration_limit is not None:
        assert float(duration_limit) >= rendered_master["request"].duration_sec, (
            f"FFmpeg duration limit is {duration_limit!r}s, observed shot duration="
            f"{rendered_master['request'].duration_sec}s"
        )


def test_master_encode_uses_a_lossless_ffv1_codec(rendered_master: dict[str, Any]) -> None:
    command = _master_command(rendered_master)
    codec = _option_value(command, "-c:v", "-codec:v")
    assert codec == "ffv1", (
        f"master codec is {codec!r}; contract expects lossless FFV1, argv={command!r}"
    )
    assert Path(command[-1]).suffix.lower() == ".mkv", (
        f"FFV1 master output is {command[-1]!r}, expected a Matroska .mkv file"
    )


def test_master_rate_uses_the_plan_sixty_fps_not_camera_post_fps(
    rendered_master: dict[str, Any],
) -> None:
    command = _master_command(rendered_master)
    plan_fps = rendered_master["plan"]["fps"]
    camera_fps = rendered_master["request"].camera_profile.fps
    assert plan_fps == 60, f"master plan fps is {plan_fps!r}, expected the plan's 60 fps rate"
    rates = [
        value for index, argument in enumerate(command[:-1])
        if argument in {"-framerate", "-r"}
        and (value := command[index + 1])
    ]
    filter_text = _video_filter_text(command)
    filter_rates = re.findall(r"(?:^|[,=:])fps(?:=|:)(\d+(?:\.\d+)?)", filter_text)
    observed_rates = [Fraction(value) for value in rates if _is_fraction(value)]
    observed_rates.extend(Fraction(value) for value in filter_rates)
    assert Fraction(plan_fps, 1) in observed_rates, (
        f"master argv rates {observed_rates!r} omit plan fps={plan_fps}; argv={command!r}"
    )
    assert Fraction(str(camera_fps)) not in observed_rates, (
        f"master argv rates {observed_rates!r} include post-only CameraProfile.fps="
        f"{camera_fps}; argv={command!r}"
    )


def _is_fraction(value: str) -> bool:
    try:
        Fraction(value)
    except (ValueError, ZeroDivisionError):
        return False
    return True


def test_master_argv_applies_an_explicit_linear_transfer_and_integer_bit_depth(
    rendered_master: dict[str, Any],
) -> None:
    command = _master_command(rendered_master)
    video_filter = _video_filter_text(command)
    named_transfer = any(
        target in video_filter
        for target in ("bt709", "smpte170m", "iec61966", "bt2020", "gamma")
    )
    assert "linear" in video_filter and named_transfer, (
        f"master filter does not explicitly transform linear EXR into a named transfer "
        f"function; filter={video_filter!r}, argv={command!r}"
    )

    pixel_format = _option_value(command, "-pix_fmt", "-pixel_format")
    raw_depth = _option_value(command, "-bits_per_raw_sample")
    depth: int | None = int(raw_depth) if raw_depth and raw_depth.isdigit() else None
    if pixel_format is not None:
        match = re.search(r"p(9|10|12|14|16)(?:le|be)?$", pixel_format.lower())
        if match:
            depth = int(match.group(1))
        elif pixel_format.lower().endswith("p") or pixel_format.lower() in {
            "rgb24", "rgba", "bgr24", "bgra",
        }:
            depth = 8
    assert pixel_format is not None or raw_depth is not None, (
        f"master argv leaves output pixel bit depth implicit: {command!r}"
    )
    assert depth is not None and 1 <= depth <= 16, (
        f"master argv pixel format {pixel_format!r} does not declare a supported integer "
        f"bit depth; raw-depth option={raw_depth!r}, argv={command!r}"
    )
