from __future__ import annotations

import hashlib
import io
import json
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.adapters.postprofile.profile import (
    EncodeSpec,
    OverlayIndicatorSpec,
    OverlaySpec,
    PostProfileSpec,
    build_filter_graph,
    camera_profile_impact,
    validate_profile,
)
from orchestrator.adapters.postprofile.runner import (
    DegradeRequest,
    PostProfileAdapter,
    build_command,
)
from orchestrator.domain.entities import (
    ArtifactScheduleEntry,
    CameraProfile,
    PostProfile,
    PostStage,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.domain.invalidation import revision_impact
from orchestrator.domain.seeds import INT32_MAX, derive_seed
from orchestrator.platform.containment import ContainmentError, ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings
from orchestrator.service.runner import _provenance

FFMPEG_VERSION = 'ffmpeg version 8.1.1-full_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg developers\r\nbuilt with gcc 15.2.0 (Rev13, Built by MSYS2 project)\r\nconfiguration: --enable-gpl --enable-version3 --enable-static --disable-w32threads --disable-autodetect --enable-cairo --enable-fontconfig --enable-iconv --enable-gnutls --enable-lcms2 --enable-libxml2 --enable-gmp --enable-bzlib --enable-lzma --enable-libsnappy --enable-zlib --enable-librist --enable-libsrt --enable-libssh --enable-libzmq --enable-avisynth --enable-libbluray --enable-libcaca --enable-libdvdnav --enable-libdvdread --enable-sdl2 --enable-libaribb24 --enable-libaribcaption --enable-libdav1d --enable-libdavs2 --enable-libopenjpeg --enable-libquirc --enable-libuavs3d --enable-libxevd --enable-libzvbi --enable-liboapv --enable-libqrencode --enable-librav1e --enable-libsvtav1 --enable-libvvenc --enable-libwebp --enable-libx264 --enable-libx265 --enable-libxavs2 --enable-libxeve --enable-libxvid --enable-libaom --enable-libjxl --enable-libsvtjpegxs --enable-libvpx --enable-mediafoundation --enable-libass --enable-frei0r --enable-libfreetype --enable-libfribidi --enable-libharfbuzz --enable-liblensfun --enable-libvidstab --enable-libvmaf --enable-libzimg --enable-amf --enable-cuda-llvm --enable-cuvid --enable-dxva2 --enable-d3d11va --enable-d3d12va --enable-ffnvcodec --enable-libvpl --enable-nvdec --enable-nvenc --enable-vaapi --enable-libshaderc --enable-vulkan --enable-libplacebo --enable-opencl --enable-libcdio --enable-openal --enable-libgme --enable-libmodplug --enable-libopenmpt --enable-libopencore-amrwb --enable-libmp3lame --enable-libshine --enable-libtheora --enable-libtwolame --enable-libvo-amrwbenc --enable-libcodec2 --enable-libilbc --enable-libgsm --enable-liblc3 --enable-libopencore-amrnb --enable-libopus --enable-libspeex --enable-libvorbis --enable-ladspa --enable-libbs2b --enable-libflite --enable-libmysofa --enable-librubberband --enable-libsoxr --enable-chromaprint --enable-whisper\r\nlibavutil      60. 26.101 / 60. 26.101\r\nlibavcodec     62. 28.101 / 62. 28.101\r\nlibavformat    62. 12.101 / 62. 12.101\r\nlibavdevice    62.  3.101 / 62.  3.101\r\nlibavfilter    11. 14.101 / 11. 14.101\r\nlibswscale      9.  5.101 /  9.  5.101\r\nlibswresample   6.  3.101 /  6.  3.101\r\n\r\nExiting with exit code 0'


def _fake_popen_factory(*, calls=None, start_time="0", invalid_master=False,
                        encode_returncode=0, launch_encode_error=False,
                        encode_stderr=b"decode failed", bad_output_pts=None,
                        invalid_probe_root=False):
    """Return fake Popen responses for the adapter's documented FFmpeg preflight."""
    calls = calls if calls is not None else []
    version = (FFMPEG_VERSION + "\n").encode("utf-8")
    filters = b"""Filters:\n T.. scale\n T.. fps\n T.. select\n T.. loop\n T.. settb\n T.. setpts\n T.. drawtext\n T.. eq\n T.. noise\n T.. unsharp\n T.. setsar\n"""
    encoders = b"Encoders:\n V..... libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10\n"
    help_full = b"-filter:v <filter_graph> ...\n-c:v <codec> ...\n-preset <preset> ...\n-crf <float> ...\n-pix_fmt <format> ...\n-threads <count> ...\n-fflags <flags> ...\n-flags <flags> ...\n"
    noise_help = b"Filter noise\nnoise AVOptions:\n all_seed <int> (from -1 to INT_MAX)\n alls <int> (from 0 to 100)\n allf <flags>\n"
    loop_help = b"Filter loop\nloop AVOptions:\n loop <int> (from -1 to INT_MAX)\n size <int64> (from 0 to 32767)\n start <int64> (from -1 to I64_MAX)\n"
    x264_help = b"Encoder libx264 [libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10]:\n-preset <string> ...\n-crf <float> ...\n-threads <count> ...\n-pix_fmt <format> ...\n"

    class FakeProcess:
        pid = 123
        def __init__(self, argv, **kwargs):
            self.argv = [str(value) for value in argv]
            self.kwargs = kwargs
            calls.append(self.argv)
            self.returncode = 0
            self._stdout = b""
            self._stderr = b""
            if launch_encode_error and "-vf" in self.argv:
                raise OSError("injected encode launch failure")
            if "-vf" in self.argv:
                output = Path(self.argv[-1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"fake encoded output")
                self.returncode = encode_returncode
                if encode_returncode:
                    log_handle = kwargs.get("stdout")
                    if hasattr(log_handle, "write"):
                        log_handle.write(encode_stderr)
                        log_handle.flush()
                    else:
                        self._stderr = encode_stderr
            elif "-version" in self.argv:
                self._stdout = version
            elif "-filters" in self.argv:
                self._stdout = filters
            elif "-encoders" in self.argv:
                self._stdout = encoders
            elif "-h" in self.argv:
                target = self.argv[self.argv.index("-h") + 1]
                self._stdout = {
                    "full": help_full,
                    "filter=noise": noise_help,
                    "filter=loop": loop_help,
                    "encoder=libx264": x264_help,
                }.get(target, b"")
            elif any("ffprobe" in value.lower() for value in self.argv):
                input_path = self.argv[-1]
                is_master = input_path.endswith("master.mkv")
                if is_master and invalid_probe_root:
                    self._stdout = b"[]"
                elif is_master and invalid_master:
                    self._stdout = b'{"streams": []}'
                else:
                    frame_count = 300 if is_master else 54
                    rate = "60/1" if is_master else "12/1"
                    time_base = "1/60" if is_master else "1/12"
                    stream_start = start_time if is_master else "0"
                    pts_count = frame_count
                    if not is_master and bad_output_pts == "count_mismatch":
                        pts_count -= 1
                    frames = ",".join(
                        (
                            '{"pts_time":"nan"}'
                            if index == 0 and not is_master and bad_output_pts == "nonfinite"
                            else f'{{"pts_time":"{index / (60 if is_master else 12):.6f}"}}'
                        )
                        for index in range(pts_count)
                    )
                    self._stdout = json.dumps({
                        "streams": [{
                            "codec_type": "video",
                            "nb_read_frames": str(frame_count),
                            "avg_frame_rate": rate,
                            "time_base": time_base,
                            "start_time": stream_start,
                        }],
                        "frames": json.loads(f"[{frames}]"),
                        "format": {"duration": "5.000000"},
                    }).encode()
            self.stdout = io.BytesIO(self._stdout)
            self.stderr = io.BytesIO(self._stderr)

        def communicate(self, timeout=None):
            return self._stdout, self._stderr
        def wait(self, timeout=None):
            return self.returncode
        def poll(self):
            return self.returncode

    return FakeProcess


def _profile(*, schedule=(), stages=None):
    stage_values = stages or [
        PostStage(name="resample", filter="scale", params={"w": 640, "h": 480, "flags": "bicubic"}),
        PostStage(name="decimate", filter="fps", params={"fps": 12}),
        PostStage(name="restructure", filter="select", params={"from_schedule": "drop+freeze"}),
        PostStage(name="hold_frozen", filter="loop", params={"from_schedule": "freeze"}),
        PostStage(name="clock_timebase", filter="setpts", params={"from_schedule": "clock"}),
        PostStage(name="overlay_burnin", filter="drawtext", params={"camera_id": "CAM 03", "clock_epoch": 1131984000, "fontsize": 16, "x": 8, "y": "h-24"}, composite_stage="in_recording"),
        PostStage(name="output_timebase", filter="setpts", params={"timebase": "1/output_fps", "pts": "N"}),
        PostStage(name="bad_white_balance", filter="eq", params={"brightness": 0.02, "contrast": 1.08, "saturation": 0.75}),
        PostStage(name="sensor_noise", filter="noise", params={"alls": 12, "allf": "t+u"}, seed_source="shot"),
        PostStage(name="cheap_sharpen", filter="unsharp", params={"luma_msize_x": 5, "luma_msize_y": 5, "luma_amount": 0.8}),
    ]
    return PostProfile(name="dvr-2005", semver="0.2.0", stages=stage_values,
                       pinned_binaries={"ffmpeg": FFMPEG_VERSION})


def _spec(*, schedule=(), stages=None, supersedes=None, sar="1/1",
          resolution=(640, 480), output_fps=12):
    if stages is None:
        stages = list(_profile().stages)
        stages = [
            stage.model_copy(update={"params": {**stage.params, "w": resolution[0], "h": resolution[1]}})
            if stage.filter == "scale" else
            stage.model_copy(update={"params": {**stage.params, "fps": output_fps}})
            if stage.filter == "fps" else stage
            for stage in stages
        ]
    return PostProfileSpec(
        identity=_profile(stages=stages), output_fps=output_fps,
        storage_resolution=resolution, sample_aspect_ratio=sar,
        artifact_schedule=tuple(schedule),
        overlay=OverlaySpec(camera_id="CAM 03", clock_epoch=1131984000,
                            fontsize=16, x=8, y="h-24", placement="in_recording"),
        encode=EncodeSpec(codec="libx264", preset="medium", crf=30,
                          pix_fmt="yuv420p", threads=1),
        supersedes=supersedes,
    )


def _camera(*, fps=12.0, schedule=(), resolution=(640, 480), display_ar="4:3"):
    return CameraProfile(camera_id="CAM03", profile_name="dvr-2005", fps=fps,
                         storage_resolution=resolution, display_aspect_ratio=display_ar,
                         frame_drop_schedule=list(schedule))


def _run(root: Path, run_id="run-01"):
    project = ProjectRoot(root)
    project.write_bytes("master.mkv", b"master")
    project.write_bytes("font.ttf", b"font")
    return DegradeRequest(
        shot_id="S003", shot_version=1, run_id=run_id, degrade_seed=20051114,
        master_relative="master.mkv", font_relative="font.ttf",
        profile=_spec(schedule=(ArtifactScheduleEntry(type="drop", start_frame=24, count=6),
                                ArtifactScheduleEntry(type="freeze", start_frame=48, count=11))),
        camera_profile=_camera(schedule=(ArtifactScheduleEntry(type="drop", start_frame=24, count=6),
                                         ArtifactScheduleEntry(type="freeze", start_frame=48, count=11))),
    )


def test_supersedes_is_version_metadata_on_post_profile_spec():
    spec = _spec(supersedes="0.1.0")
    request = DegradeRequest(
        shot_id="S003", shot_version=2, run_id="run-02", degrade_seed=20051114,
        master_relative="master.mkv", font_relative="font.ttf", profile=spec,
        camera_profile=_camera(),
    )
    assert request.profile.supersedes == "0.1.0"


def test_valid_profile_uses_exact_input_rate_for_decimated_frame_count():
    result = validate_profile(_spec(), _camera(), master_frames=300,
                              master_rate=Fraction(30, 1))
    assert result.predicted_frames == 120


def test_valid_profile_uses_the_exact_decimated_bounds_and_emits_schedule():
    schedule = (ArtifactScheduleEntry(type="drop", start_frame=24, count=6),
                ArtifactScheduleEntry(type="freeze", start_frame=48, count=11))
    result = validate_profile(_spec(schedule=schedule), _camera(schedule=schedule), master_frames=300, master_rate=Fraction(60, 1))
    assert result.predicted_frames == 54
    assert [(event.type, event.start_frame, event.count) for event in result.schedule] == [
        ("drop", 24, 6), ("freeze", 48, 11)
    ]
    assert list(result.clock_steps) == [(24, 6)]


@pytest.mark.parametrize("bad", [
    (ArtifactScheduleEntry(type="freeze", start_frame=0, count=1),),
    (ArtifactScheduleEntry(type="freeze", start_frame=48, count=11),),
    (ArtifactScheduleEntry(type="drop", start_frame=48, count=3),),
    (ArtifactScheduleEntry(type="drop", start_frame=20, count=8),
     ArtifactScheduleEntry(type="freeze", start_frame=24, count=2)),
])
def test_invalid_schedule_windows_rejected_at_profile_load(bad):
    with pytest.raises((ValueError, JobError)) as exc:
        validate_profile(_spec(schedule=bad, output_fps=10), _camera(fps=10, schedule=bad),
                         master_frames=300, master_rate=Fraction(60, 1))
    if isinstance(exc.value, JobError):
        assert exc.value.kind is JobErrorKind.INVALID_MANIFEST


def test_unknown_filters_options_and_duplicate_stage_names_are_rejected():
    bad_stages = [PostStage(name="mystery", filter="madeup", params={})]
    with pytest.raises((ValueError, JobError)):
        validate_profile(_spec(stages=bad_stages), _camera(), master_frames=300, master_rate=Fraction(60, 1))
    duplicated = [PostStage(name="same", filter="eq", params={}),
                  PostStage(name="same", filter="eq", params={})]
    with pytest.raises((ValueError, JobError)):
        validate_profile(_spec(stages=duplicated), _camera(), master_frames=300, master_rate=Fraction(60, 1))
    unsafe = [PostStage(name="scale", filter="scale", params={"w": "640;movie=x", "h": 480})]
    with pytest.raises((ValueError, JobError)):
        validate_profile(_spec(stages=unsafe), _camera(), master_frames=300, master_rate=Fraction(60, 1))



def test_profile_rejects_filter_grammar_injection_and_out_of_range_parameters():
    base = list(_profile().stages)
    malicious_noise = [stage.model_copy(update={"params": {**stage.params, "alls": "12:all_seed=7"}})
                       if stage.filter == "noise" else stage for stage in base]
    with pytest.raises((ValueError, JobError)):
        validate_profile(_spec(stages=malicious_noise), _camera(), master_frames=300,
                         master_rate=Fraction(60, 1))
    bad_strength = [stage.model_copy(update={"params": {**stage.params, "alls": 101}})
                    if stage.filter == "noise" else stage for stage in base]
    with pytest.raises((ValueError, JobError)):
        validate_profile(_spec(stages=bad_strength), _camera(), master_frames=300,
                         master_rate=Fraction(60, 1))
    injected_sar = base + [PostStage(name="sar", filter="setsar", params={"sar": "1/1,movie=x"})]
    with pytest.raises((ValueError, JobError)):
        validate_profile(_spec(stages=injected_sar), _camera(), master_frames=300,
                         master_rate=Fraction(60, 1))





def test_colon_sar_is_normalized_to_slash_in_terminal_filter():
    profile = _spec(sar="12:11", resolution=(352, 288))
    checked = validate_profile(profile, _camera(resolution=(352, 288)),
                               master_frames=300, master_rate=Fraction(60, 1))
    graph = build_filter_graph(checked, shot_id="S003", degrade_seed=5,
                               font_path="font.ttf")
    assert graph.rstrip().endswith("setsar=12/11")
    assert not graph.rstrip().endswith("setsar=12:11")

def test_setsar_without_parameter_uses_post_profile_sar():
    base = _spec(sar="12/11", resolution=(352, 288))
    stages = list(base.identity.stages) + [PostStage(name="set_sar", filter="setsar", params={})]
    profile = _spec(stages=stages, sar="12/11", resolution=(352, 288))
    checked = validate_profile(profile, _camera(resolution=(352, 288)), master_frames=300,
                               master_rate=Fraction(60, 1))
    graph = build_filter_graph(checked, shot_id="S003", degrade_seed=3,
                               font_path="font.ttf")
    assert "setsar=12/11" in graph


def test_duplicate_loop_stages_are_rejected():
    schedule = (ArtifactScheduleEntry(type="freeze", start_frame=48, count=2),)
    stages = list(_profile().stages) + [
        PostStage(name="second_freeze_loop", filter="loop", params={"from_schedule": "freeze"})
    ]
    with pytest.raises((ValueError, JobError)):
        validate_profile(_spec(stages=stages, schedule=schedule),
                         _camera(schedule=schedule), master_frames=300,
                         master_rate=Fraction(60, 1))

def test_overlay_config_and_drawtext_stage_must_be_present_together():
    with_drawtext = list(_profile().stages)
    without_drawtext = [stage for stage in with_drawtext if stage.filter != "drawtext"]
    with pytest.raises((ValueError, JobError)):
        validate_profile(_spec(stages=without_drawtext), _camera(), master_frames=300,
                         master_rate=Fraction(60, 1))
    no_overlay_spec = _spec(stages=with_drawtext).model_copy(update={"overlay": None})
    with pytest.raises((ValueError, JobError)):
        validate_profile(no_overlay_spec, _camera(), master_frames=300,
                         master_rate=Fraction(60, 1))
    no_overlay_no_drawtext = _spec(stages=without_drawtext).model_copy(update={"overlay": None})
    checked = validate_profile(no_overlay_no_drawtext, _camera(), master_frames=300,
                               master_rate=Fraction(60, 1))
    assert checked.predicted_frames == 60


def test_battery_rec_indicator_cannot_be_placed_on_playback():
    with pytest.raises(ValidationError):
        OverlayIndicatorSpec(fontsize=12, x=8, y=8, placement="on_playback")


def test_profile_rejects_duplicate_battery_rec_drawtext_stages():
    profile = _spec()
    indicator = OverlayIndicatorSpec(fontsize=12, x=8, y=8)
    overlay = profile.overlay.model_copy(update={"indicator": indicator})
    stages = list(profile.identity.stages)
    clock_index = next(
        index for index, stage in enumerate(stages)
        if stage.filter == "drawtext"
    )
    indicator_stages = [
        PostStage(
            name=f"battery_rec_{number}", filter="drawtext",
            params={"element": "battery_rec"}, composite_stage="in_recording",
        )
        for number in (1, 2)
    ]
    stages[clock_index + 1:clock_index + 1] = indicator_stages
    invalid = profile.model_copy(update={
        "identity": profile.identity.model_copy(update={"stages": stages}),
        "overlay": overlay,
    })
    with pytest.raises((ValueError, JobError)):
        validate_profile(invalid, _camera(), master_frames=300, master_rate=Fraction(60, 1))


def test_profile_rejects_sample_aspect_ratio_that_disagrees_with_display_ratio():
    with pytest.raises((ValueError, JobError)):
        validate_profile(_spec(sar="12/11"), _camera(), master_frames=300,
                         master_rate=Fraction(60, 1))


def test_cif_sample_aspect_ratio_matches_four_by_three_display_ratio():
    checked = validate_profile(_spec(sar="12/11", resolution=(352, 288)),
                               _camera(resolution=(352, 288)), master_frames=300,
                               master_rate=Fraction(60, 1))
    assert checked.predicted_frames == 60


def test_empty_schedule_builds_a_pass_through_graph():
    checked = validate_profile(_spec(), _camera(), master_frames=300,
                               master_rate=Fraction(60, 1))
    graph = build_filter_graph(checked, shot_id="S003", degrade_seed=7,
                               font_path="font.ttf")
    assert isinstance(graph, str) and graph
    assert checked.predicted_frames == 60

@pytest.mark.parametrize("missing", ["restructure", "clock_timebase", "output_timebase"])
def test_profile_requires_schedule_and_both_explicit_timing_stages(missing):
    stages = [stage for stage in _profile().stages if stage.name != missing]
    with pytest.raises((ValueError, JobError)):
        validate_profile(_spec(stages=stages), _camera(), master_frames=300,
                         master_rate=Fraction(60, 1))

def test_graph_is_deterministic_seeded_and_orders_clock_overlay_before_degradation():
    schedule = (ArtifactScheduleEntry(type="drop", start_frame=24, count=6),
                ArtifactScheduleEntry(type="freeze", start_frame=48, count=11))
    checked = validate_profile(_spec(schedule=schedule), _camera(schedule=schedule), master_frames=300, master_rate=Fraction(60, 1))
    graph = build_filter_graph(checked, shot_id="S003", degrade_seed=20051114,
                               font_path="assets/consola.ttf")
    assert graph == build_filter_graph(checked, shot_id="S003", degrade_seed=20051114,
                                       font_path="assets/consola.ttf")
    assert graph.index("scale=") < graph.index("fps=") < graph.index("select=")
    assert graph.index("settb=") < graph.index("drawtext=") < graph.index("noise=")
    expected = derive_seed("S003", 20051114, "sensor_noise", INT32_MAX)
    assert f"all_seed={expected}" in graph
    assert 0 <= expected <= INT32_MAX
    assert "loop=loop=11:size=1:start=42" in graph
    assert "settb=1/12" in graph


def test_filter_graph_escapes_font_path_with_filter_special_characters():
    checked = validate_profile(_spec(), _camera(), master_frames=300, master_rate=Fraction(60, 1))
    graph = build_filter_graph(checked, shot_id="S003", degrade_seed=1,
                               font_path="assets/Camera: 01\\font.ttf")
    assert "drawtext=" in graph
    assert "Camera\\: 01" in graph
    assert "\\\\font.ttf" in graph or "font.ttf" in graph


def test_camera_profile_impact_delegates_tagged_invalidation():
    before = _camera()
    post_only = before.model_copy(update={"fps": 10.0, "version": before.version + 1})
    rebuild = before.model_copy(update={"exposure_ev": 1.0, "version": before.version + 1})
    shot = Shot(shot_id="S003", episode_id="E01", camera_id="CAM03", duration_sec=5)
    assert camera_profile_impact(before, post_only, [shot]) == revision_impact(before, post_only, [shot]) == ()
    assert camera_profile_impact(before, rebuild, [shot]) == revision_impact(before, rebuild, [shot]) == (shot,)


def test_build_command_is_argv_only_and_carries_bitexact_and_explicit_threads():
    argv = build_command(Path("ffmpeg.exe"), master=Path("master.mkv"), output=Path("out.mp4"),
                         filter_graph="scale=640:480", codec="libx264", preset="medium",
                         crf=30, pixel_format="yuv420p", threads=1)
    assert isinstance(argv, list)
    assert all(isinstance(arg, str) for arg in argv)
    assert "-vf" in argv and "-threads" in argv and argv[argv.index("-threads") + 1] == "1"
    assert "+bitexact" in argv
    assert "-y" in argv
    assert "shell=True" not in " ".join(argv)


def test_request_rejects_bad_ids_versions_seeds_and_extra_fields():
    base = {"shot_id": "S003", "shot_version": 1, "run_id": "run-01", "degrade_seed": 1,
            "master_relative": "master.mkv", "font_relative": "font.ttf", "profile": _spec(),
            "camera_profile": _camera()}
    for update in ({"shot_id": "../escape"}, {"shot_version": 0}, {"degrade_seed": -1},
                   {"unexpected": True}):
        with pytest.raises(ValidationError):
            DegradeRequest(**(base | update))


def test_containment_rejects_traversal_and_symlink_escape(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    root = ProjectRoot(root_path)
    with pytest.raises(ContainmentError):
        root.resolve("../outside/master.mkv")
    link = root_path / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable in this environment")
    with pytest.raises(ContainmentError):
        root.resolve("escape/evil.mkv")



class TrackingProjectRoot(ProjectRoot):
    def __init__(self, root):
        super().__init__(root)
        self.write_with_paths = []

    def write_with(self, relative, writer):
        self.write_with_paths.append(relative)
        return super().write_with(relative, writer)

def test_adapter_fake_process_success_records_contained_artifacts_and_metadata(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = TrackingProjectRoot(root_path)
    request = _run(root_path)

    fake_popen = _fake_popen_factory()

    adapter = PostProfileAdapter(root, Settings(project_root=root.path), popen=fake_popen)
    result = adapter.degrade(request)
    assert result.output_path.startswith("post/S003/v0001/run-01/")
    assert result.log_path.startswith(result.output_path.rsplit("/", 1)[0])
    assert result.output_sha256 == hashlib.sha256(b"fake encoded output").hexdigest()
    assert result.filter_graph_hash
    assert result.frame_count == 54
    assert result.output_path.endswith("/degraded.mp4")
    assert result.output_path in root.write_with_paths
    assert root.resolve(result.output_path).is_file()
    assert root.resolve(result.metadata_path).is_file()
    assert root.resolve(result.log_path).is_file()


def test_adapter_runs_profile_without_overlay_or_font(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path, "no-overlay")
    stages = [stage for stage in request.profile.identity.stages if stage.filter != "drawtext"]
    identity = request.profile.identity.model_copy(update={"stages": stages})
    profile = request.profile.model_copy(update={"identity": identity, "overlay": None})
    request = request.model_copy(update={"font_relative": None, "profile": profile})
    root.resolve("font.ttf").unlink()
    calls = []

    result = PostProfileAdapter(
        root,
        Settings(project_root=root.path),
        popen=_fake_popen_factory(calls=calls),
    ).degrade(request)

    assert "drawtext=" not in result.filter_graph
    assert "noise=alls=" in result.filter_graph
    assert any("-vf" in call for call in calls)


def test_request_requires_font_for_drawtext_profile(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    request = _run(root_path, "overlay-needs-font")

    with pytest.raises(ValidationError, match="font_relative is required"):
        DegradeRequest.model_validate(
            request.model_dump(mode="python") | {"font_relative": None}
        )


def test_successful_degrade_result_projects_valid_shot_output_receipt(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path)
    result = PostProfileAdapter(
        root, Settings(project_root=root.path), popen=_fake_popen_factory(),
    ).degrade(request)

    output = ShotOutput(
        output_id="degraded-v1",
        episode_id="ep1",
        shot_id=request.shot_id,
        record_version=request.shot_version,
        kind=ShotOutputKind.DEGRADED,
        relative_path=result.output_path,
        content_sha256=result.output_sha256,
        **_provenance(ShotOutputKind.DEGRADED, result),
    )

    assert output.profile_semver == request.profile.identity.semver
    assert output.pinned_binaries == {
        "ffmpeg": normalize_ffmpeg_identity(result.ffmpeg_identity),
    }
    assert output.filter_graph_hash == result.filter_graph_hash
    assert output.engine_id is None
    assert output.engine_build is None


def test_adapter_rejects_run_collision_without_overwriting_previous_artifacts(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path)
    prior = root.write_text("post/S003/v0001/run-01/marker.txt", "keep")
    with pytest.raises((FileExistsError, ValueError)):
        PostProfileAdapter(root, Settings(project_root=root.path), popen=_fake_popen_factory()).degrade(request)
    assert prior.read_text(encoding="utf-8") == "keep"


def test_adapter_classifies_launch_and_ffmpeg_failures_and_retains_logs(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path, "launch")
    adapter = PostProfileAdapter(root, Settings(project_root=root.path),
                                 popen=_fake_popen_factory(launch_encode_error=True))
    with pytest.raises(JobError) as exc:
        adapter.degrade(request)
    assert exc.value.kind is JobErrorKind.WORKER_UNREACHABLE
    assert exc.value.shot_id == "S003"
    assert exc.value.run_id == "launch"
    assert list(root_path.glob("post/S003/v0001/launch/**/*.log"))

    request = _run(root_path, "nonzero")
    failed_popen = _fake_popen_factory(encode_returncode=17)
    with pytest.raises(JobError) as exc2:
        PostProfileAdapter(root, Settings(project_root=root.path), popen=failed_popen).degrade(request)
    assert exc2.value.kind is JobErrorKind.INTERNAL
    assert exc2.value.__cause__ is not None




def test_non_object_ffprobe_json_is_corrupt_before_run_reservation(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path, "array-probe")
    calls = []
    with pytest.raises(JobError) as exc:
        PostProfileAdapter(root, Settings(project_root=root.path),
                           popen=_fake_popen_factory(calls=calls, invalid_probe_root=True)).degrade(request)
    assert exc.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert not (root_path / "post" / "S003" / "v0001" / "array-probe").exists()
    assert not any("-vf" in call for call in calls)

def test_ffmpeg_disk_full_diagnostic_maps_to_disk_exhausted(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path, "disk-full")
    with pytest.raises(JobError) as exc:
        PostProfileAdapter(root, Settings(project_root=root.path),
                           popen=_fake_popen_factory(encode_returncode=1,
                                                     encode_stderr=b"No space left on device")).degrade(request)
    assert exc.value.kind is JobErrorKind.DISK_EXHAUSTED

def test_master_is_never_modified_and_bad_input_fails_before_ffmpeg(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path, "bad-master")
    before = hashlib.sha256((root_path / "master.mkv").read_bytes()).hexdigest()
    calls = []
    with pytest.raises(JobError) as exc:
        PostProfileAdapter(root, Settings(project_root=root.path),
                           popen=_fake_popen_factory(calls=calls, invalid_master=True)).degrade(request)
    assert exc.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert not any("-vf" in call for call in calls)
    assert hashlib.sha256((root_path / "master.mkv").read_bytes()).hexdigest() == before



def test_ffmpeg_identity_mismatch_is_rejected_before_run_reservation(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path, "pin-mismatch")
    wrong_identity = request.profile.identity.model_copy(
        update={"pinned_binaries": {"ffmpeg": "ffmpeg version 8.1.1"}}
    )
    request = request.model_copy(update={
        "profile": request.profile.model_copy(update={"identity": wrong_identity})
    })
    calls = []
    with pytest.raises(JobError):
        PostProfileAdapter(root, Settings(project_root=root.path),
                          popen=_fake_popen_factory(calls=calls)).degrade(request)
    assert not (root_path / "post" / "S003" / "v0001" / "pin-mismatch").exists()
    assert not any("-vf" in call for call in calls)


@pytest.mark.parametrize("different_build", [False, True])
def test_ffmpeg_identity_normalizes_line_endings_and_remains_exact(tmp_path, different_build):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path, "normalized-pin")
    lf_pin = FFMPEG_VERSION.replace("\r\n", "\n").replace("\r", "\n")
    if different_build:
        # Keep the banner unchanged while changing a meaningful configuration value.
        lf_pin = lf_pin.replace("--enable-cairo", "--disable-cairo")
    pinned_identity = request.profile.identity.model_copy(
        update={"pinned_binaries": {"ffmpeg": lf_pin}}
    )
    request = request.model_copy(update={
        "profile": request.profile.model_copy(update={"identity": pinned_identity})
    })

    adapter = PostProfileAdapter(root, Settings(project_root=root.path),
                                 popen=_fake_popen_factory())
    if different_build:
        with pytest.raises(JobError) as exc:
            adapter.degrade(request)
        assert exc.value.kind is JobErrorKind.INVALID_MANIFEST
        assert not (root_path / "post" / "S003" / "v0001" / "normalized-pin").exists()
    else:
        result = adapter.degrade(request)
        assert result.ffmpeg_identity == lf_pin


@pytest.mark.parametrize("bad_output_pts", ["count_mismatch", "nonfinite"])
def test_invalid_output_pts_evidence_is_corrupt_artifact(tmp_path, bad_output_pts):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path, "bad-pts-" + bad_output_pts)
    calls = []
    with pytest.raises(JobError) as exc:
        PostProfileAdapter(root, Settings(project_root=root.path),
                           popen=_fake_popen_factory(calls=calls, bad_output_pts=bad_output_pts)).degrade(request)
    assert exc.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert any("-vf" in call for call in calls)

def test_no_gpu_or_render_dependency_is_required_for_adapter_import():
    from orchestrator.adapters import postprofile
    assert postprofile is not None


def test_nonzero_master_start_time_is_corrupt_before_run_reservation(tmp_path):
    root_path = tmp_path / "project"
    root_path.mkdir()
    root = ProjectRoot(root_path)
    request = _run(root_path, "nonzero-start")
    calls = []
    fake_popen = _fake_popen_factory(calls=calls, start_time="1.000000")

    with pytest.raises(JobError) as exc:
        PostProfileAdapter(root, Settings(project_root=root.path), popen=fake_popen).degrade(request)
    assert exc.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    run_dir = root_path / "post" / "S003" / "v0001" / "nonzero-start"
    assert not run_dir.exists()
    assert not any("-vf" in call for call in calls)
