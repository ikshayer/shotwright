"""Regressions for analog processing and its service boundaries."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import pytest

import orchestrator.adapters.postprofile.runner as postprofile_runner
from orchestrator.adapters.postprofile.profile import (
    build_filter_graph_parts,
    validate_profile,
)
from orchestrator.adapters.postprofile.runner import (
    DegradeRequest,
    PostProfileAdapter,
    build_analog_encoder_command,
)
from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import (
    CameraProfile,
    Job,
    PostProfile,
    PostProfileEncode,
    PostStage,
    Project,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.jobs.journal import read_job
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.compose import compose_post_profile_spec
from orchestrator.service.dependencies import create_post_profile
from orchestrator.service.runner import AdapterRequest, postprofile_spec_from_camera
from orchestrator.store.json_store import ProjectStore, RecordNotFound
from tests.adapters.postprofile import test_analog_artifacts_contract as analog_cases
from tests.service.conftest import Environment
from tests.service.test_adapter_integration import _shot


def test_split_graph_sets_non_square_sar_once_and_forces_yuv_before_chroma() -> None:
    camera = CameraProfile(
        camera_id="cam", profile_name="CCTV", storage_resolution=(352, 288),
        display_aspect_ratio="4:3", chroma_smearing=1.0,
        analog_artifacts={"head_switching": {"height": 4}},
    )
    base = analog_cases._profile(camera, "fixture", None)
    spec = postprofile_spec_from_camera(camera, base)
    assert all(stage.filter != "setsar" for stage in spec.identity.stages)
    validated = validate_profile(spec, camera, master_frames=120, master_rate=Fraction(60))

    producer, consumer = build_filter_graph_parts(validated, shot_id="s1", degrade_seed=17)

    assert "setsar" not in producer
    assert consumer == "setsar=12/11"
    assert "format=pix_fmts=yuv420p,gblur=" in producer


@pytest.mark.parametrize("block_artifacts", [0.0, 0.5])
def test_raw_x264_encoder_disables_mbtree_and_retains_the_quantizer_floor(block_artifacts: float) -> None:
    command = build_analog_encoder_command(
        Path("ffmpeg"), output=Path("degraded.mp4"), width=352, height=288, frame_rate=12,
        filter_graph="setsar=12/11", codec="libx264", preset="medium", crf=23,
        pixel_format="yuv420p", threads=1, block_artifacts=block_artifacts,
    )

    values = [command[index + 1] for index, value in enumerate(command) if value == "-x264-params"]
    options, = values
    assert "mbtree=0" in options.split(":")
    assert (f"qpmin={round(18 + 33 * block_artifacts)}" in options.split(":")) == bool(block_artifacts)


def test_external_stage_marker_is_rejected_in_authored_and_persisted_profiles(tmp_path: Path) -> None:
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="Fixture", output_profile="tape"), expected_version=0)
    profile = PostProfile(
        name="tape", semver="1.0.0", stages=[
            PostStage(name="camera_analog_artifacts", filter="camera_analog_artifacts"),
        ],
        output_fps=12, storage_resolution=(352, 288), sample_aspect_ratio="12/11",
        encode=PostProfileEncode(codec="libx264", preset="medium", crf=23, pix_fmt="yuv420p", threads=1),
    )

    with pytest.raises(ValueError, match=r"CameraProfile\.analog_artifacts"):
        create_post_profile(store, profile)
    with pytest.raises(RecordNotFound):
        store.load(PostProfile, "tape")

    # The generic persistence layer can contain records saved before the service guard.
    store.save(profile, expected_version=0)
    with pytest.raises(ValueError, match=r"CameraProfile\.analog_artifacts"):
        compose_post_profile_spec(store)


@analog_cases.needs_ffmpeg
def test_pin_mismatch_names_the_build_script(tmp_path: Path) -> None:
    lab = analog_cases._Lab(tmp_path / "project")
    executable = Path(sys.executable).resolve()
    settings = analog_cases._settings_with_pipe(lab.settings, executable)
    request = lab.request(analog_cases._settings_camera(), pipe_pin="sha256:" + "0" * 64)
    recorder = analog_cases._RecordingPopen()
    with pytest.raises(JobError) as caught:
        PostProfileAdapter(lab.root, settings, popen=recorder).degrade(request)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "tools/build_ntsc_pipe.ps1" in caught.value.message
    assert recorder.calls == []


@analog_cases.needs_ffmpeg
def test_exact_frame_transfer_leaves_finalization_to_deadline_and_retains_settings_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab = analog_cases._Lab(tmp_path / "project")
    executable = Path(sys.executable).resolve()
    settings = analog_cases._settings_with_pipe(lab.settings, executable)
    request = lab.request(analog_cases._settings_camera(), pipe_pin=analog_cases._sha_pin(executable))
    script = (
        "import sys, time; data = sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); "
        "time.sleep(60)"
    )
    transfer_complete = threading.Event()
    # The lab master is two seconds, and this profile emits six 320x240 RGB24 frames per second.
    expected_bytes = 12 * 320 * 240 * 3
    children: list[subprocess.Popen[bytes]] = []

    class EncoderInput:
        def __init__(self, stream: BinaryIO) -> None:
            self.stream = stream
            self.written = 0

        def write(self, block: bytes) -> int:
            written = self.stream.write(block)
            self.written += written
            if self.written == expected_bytes:
                self.stream.flush()
                transfer_complete.set()
            return written

        def close(self) -> None:
            self.stream.close()

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        if analog_cases._ntsc_command(argv):
            child = subprocess.Popen([sys.executable, "-u", "-c", script], **kwargs)
            # Let the pump receive the final short read while this child holds stdout open.
            assert child.stdout is not None
            child.stdout = child.stdout.detach()
        else:
            child = subprocess.Popen(argv, **kwargs)
        children.append(child)
        if "pipe:0" in argv:
            assert child.stdin is not None
            child.stdin = EncoderInput(child.stdin)
        return child

    deadline = time.monotonic() + 25.0
    finalization_checks = 0

    def monotonic() -> float:
        if finalization_checks == 3:
            return deadline + 1.0
        if finalization_checks:
            # Jump past startup grace and the point where 12 frames would fall below 30 fps.
            return max(time.monotonic(), deadline - 5.0)
        return time.monotonic()

    def sleep(seconds: float) -> None:
        nonlocal finalization_checks
        if transfer_complete.is_set():
            finalization_checks += 1
        time.sleep(seconds)

    monkeypatch.setattr(postprofile_runner, "time", SimpleNamespace(monotonic=monotonic, sleep=sleep))
    adapter = PostProfileAdapter(lab.root, settings, popen=popen)
    adapter.set_deadline(deadline)
    with pytest.raises(JobError) as caught:
        adapter.degrade(request)

    assert transfer_complete.is_set(), "the encoder did not receive all expected raw frames"
    assert finalization_checks == 3
    assert caught.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert "deadline" in caught.value.message
    assert all(child.poll() is not None for child in children)
    analog_cases._assert_failed_run_has_no_video(lab, request.run_id)
    metadata = json.loads(lab.root.read_bytes(f"post/S003/v0001/{request.run_id}/metadata.json"))
    provenance = metadata["ntsc_pipe"]
    assert provenance["source_version"] == "0.9.7"
    assert provenance["crate_version"] == "0.1.2"
    assert provenance["settings_sha256"] == hashlib.sha256(
        lab.root.read_bytes(provenance["settings_path"]),
    ).hexdigest()


def test_active_supervised_degrade_journals_cancellation_after_child_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = Environment(tmp_path / "project")
    started = threading.Event()
    stop_observed = threading.Event()
    teardown_gate = threading.Event()
    stopped = threading.Event()
    stop_check: Callable[[], bool] | None = None

    def set_cancel_check(check: Callable[[], bool]) -> None:
        nonlocal stop_check
        stop_check = check

    def invoke(request: AdapterRequest, _job: Job, *, queue: object) -> None:
        assert isinstance(request.payload, DegradeRequest)
        assert request.payload.camera_profile.analog_artifacts.active
        started.set()
        while stop_check is not None and not stop_check():
            if teardown_gate.wait(0.01):
                raise AssertionError("test released teardown before the stop request was observed")
        assert stop_check is not None
        stop_observed.set()
        assert teardown_gate.wait(10), "test did not release child teardown"
        stopped.set()
        raise JobError(JobErrorKind.CANCELLED, "supervised children stopped")

    monkeypatch.setattr(env.adapter, "set_cancel_check", set_cancel_check, raising=False)
    monkeypatch.setattr(env.adapter, "invoke", invoke)
    canceller = None
    try:
        shot = _shot(env.store, status=ShotStatus.BUILDING)
        camera, camera_token = env.store.load_with_version(CameraProfile, shot.camera_profile)
        tape = analog_cases._settings_camera().analog_artifacts
        env.store.revise(camera.model_copy(update={
            **camera.next_version(), "analog_artifacts": tape,
        }), expected_version=camera_token)
        env.store.save(PostProfile(
            name="dvr-2005", semver="1.0.0", output_fps=12, storage_resolution=(640, 480),
            sample_aspect_ratio="1/1", stages=analog_cases._stages(
                640, 480, 12, overlay=False, camera_effects=False,
            ),
            encode=PostProfileEncode(
                codec="libx264", preset="medium", crf=23, pix_fmt="yuv420p", threads=1,
            ),
        ), expected_version=0)
        master_relative = "renders/s1/v0001/fixture/master.mkv"
        master = b"fixture master for the supervised adapter double"
        env.root.write_bytes(master_relative, master)
        env.store.append_shot_output(ShotOutput(
            output_id="fixture-master", episode_id=shot.episode_id, shot_id=shot.shot_id,
            record_version=shot.version, kind=ShotOutputKind.MASTER, relative_path=master_relative,
            content_sha256=hashlib.sha256(master).hexdigest(), engine_id="BLENDER_EEVEE",
            engine_build="fixture",
        ), expected_version=0)
        job = env.runner.submit("p1", env.request(kind=env.AdapterKind.DEGRADE))
        assert started.wait(5), "DEGRADE did not enter the supervised adapter"
        cancelled: list[Job] = []
        canceller = threading.Thread(
            target=lambda: cancelled.append(env.runner.cancel("p1", job.job_id)), daemon=True,
        )
        canceller.start()
        assert stop_observed.wait(5), "adapter did not receive the cancellation request"
        record = read_job(env.root, job.job_id)
        assert record is not None and record["state"] == "running"
        assert canceller.is_alive()
        assert not stopped.is_set()

        teardown_gate.set()
        canceller.join(5)
        assert not canceller.is_alive()
        assert stopped.is_set()
        returned, = cancelled
        assert returned.state == "cancelled"
        record = read_job(env.root, job.job_id)
        assert record is not None and record["state"] == "cancelled"
        assert record["error_kind"] == JobErrorKind.CANCELLED.value
    finally:
        teardown_gate.set()
        if canceller is not None:
            canceller.join(5)
        env.runner.close()
