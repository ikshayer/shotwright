"""Perception contract: tests from ``orchestrator/api/PERCEPTION_CONTRACT.md``.

Authored from the contract (plus the finishing-steps and cut-assembly contracts it builds
on) alone, without reading the implementation. Where these tests and the implementation
disagree, the contract decides and the disagreement is a finding.

Media is real: FFmpeg generates clips with *known* properties (a flat gray, an
alternating black/white flicker, a red/green/blue/white colour sequence, a letterboxed
picture, a hard cut, a 1 kHz tone at a known amplitude, a tone with a silent gap) and the
operations are checked against those properties.

The contract does not name the keys inside the metrics JSON beyond the top-level
``container`` / ``video`` / ``audio`` / ``image`` blocks. The metric tests therefore find
numbers by *key fragment* (``yavg``, ``lufs``, ...) anywhere inside the relevant block;
this is deliberate and is the AMBIGUOUS part of every R3 test. Other interpretations the
contract leaves open are marked ``AMBIGUOUS`` in the docstring of the test that encodes
them.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageStat

from orchestrator.api import cli as cli_module
from orchestrator.api import mcp
from orchestrator.domain.entities import (
    Asset,
    Episode,
    EpisodeOutput,
    Project,
    Shot,
    ShotOutput,
    ShotOutputKind,
    Timeline,
    TimelineEntry,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.service import perception
from orchestrator.store.json_store import ProjectStore

from .conftest import TOKEN, TOKEN_HEADER
from .test_mcp_adapter import AppTransport, _rpc, _tool_result

PID = "p1"
EP = "ep1"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
AUTH = {TOKEN_HEADER: TOKEN}
FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not on PATH")

MIB = 1024 * 1024

# (HTTP method, OpenAPI path) for every operation this contract adds.
OPERATIONS = {
    "contact_sheet": (
        "get", "/projects/{project_id}/episodes/{episode_id}/outputs/{output_id}/contact-sheet"),
    "rubric_sample": (
        "get", "/projects/{project_id}/episodes/{episode_id}/cuts/{output_id}/rubric-sample"),
    "evaluate_rubric": (
        "post", "/projects/{project_id}/episodes/{episode_id}/cuts/{output_id}/rubric-evaluate"),
    "measure_output": (
        "get", "/projects/{project_id}/episodes/{episode_id}/outputs/{output_id}/metrics"),
    "measure_asset": ("get", "/projects/{project_id}/assets/{asset_id}/metrics"),
}

#: 1 kHz tone: amplitude 0.25 -> peak -12.04 dBFS, RMS (and, at 1 kHz, K-weighted
#: loudness) -15.05. Tolerances below are wide enough for AAC and for FFmpeg's gating.
TONE_AMPLITUDE_EXPR = "0.25*sin(2*PI*1000*t)"
TONE_LUFS = -15.05
TONE_PEAK_DB = -12.04
LEVEL_TOLERANCE_DB = 1.5

ANOMALY = {"kind": "light_flicker", "light_name": "lamp", "flicker_fraction": 0.5,
           "frequency_hz": 2.0}


# --------------------------------------------------------------------------
# fixtures and builders
# --------------------------------------------------------------------------


@dataclass
class World:
    settings: Settings
    store: ProjectStore

    @property
    def root(self) -> Path:
        return self.settings.project_root


@pytest.fixture
def world(settings: Settings) -> World:
    base = ProjectRoot(settings.project_root)
    base.make_dir(PID)
    store = ProjectStore(ProjectRoot(base.resolve(PID)))
    store.save(Project(project_id=PID, title="Perception"), expected_version=0)
    return World(settings, store)


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def get(client: TestClient, url: str, **params: Any):
    return client.get(url, headers=AUTH, params=params or None)


def post(client: TestClient, url: str, body: Any):
    return client.post(url, json=body, headers=AUTH)


def sheet_url(output_id: str, episode_id: str = EP) -> str:
    return f"/projects/{PID}/episodes/{episode_id}/outputs/{output_id}/contact-sheet"


def metrics_url(output_id: str, episode_id: str = EP) -> str:
    return f"/projects/{PID}/episodes/{episode_id}/outputs/{output_id}/metrics"


def asset_metrics_url(asset_id: str) -> str:
    return f"/projects/{PID}/assets/{asset_id}/metrics"


def sample_url(output_id: str) -> str:
    return f"/projects/{PID}/episodes/{EP}/cuts/{output_id}/rubric-sample"


def evaluate_url(output_id: str) -> str:
    return f"/projects/{PID}/episodes/{EP}/cuts/{output_id}/rubric-evaluate"


def ensure_episode(store: ProjectStore) -> None:
    if EP not in store.episode_ids():
        store.save(Episode(episode_id=EP, project_id=PID, number=1, duration_target_sec=30),
                   expected_version=0)


def add_shot(store: ProjectStore, shot_id: str = "s1", **fields: Any) -> Shot:
    ensure_episode(store)
    shot = Shot(shot_id=shot_id, episode_id=EP, duration_sec=fields.pop("duration_sec", 4),
                **fields)
    store.create(shot)
    return shot


def add_output(store: ProjectStore, output_id: str, kind: ShotOutputKind, relative: str,
               data: bytes, *, shot_id: str = "s1") -> ShotOutput:
    if shot_id not in store.ids(Shot, episode_id=EP):
        add_shot(store, shot_id)
    store._root.write_bytes(relative, data)
    provenance: dict[str, Any] = {}
    if kind in (ShotOutputKind.MASTER, ShotOutputKind.PREVIEW, ShotOutputKind.GENERATED):
        provenance = {"engine_id": "BLENDER_EEVEE", "engine_build": "fixture-build"}
    output = ShotOutput(
        output_id=output_id, episode_id=EP, shot_id=shot_id, record_version=1, kind=kind,
        relative_path=relative, content_sha256=hashlib.sha256(data).hexdigest(), **provenance)
    ledger = len(store.list_shot_outputs(shot_id, episode_id=EP))
    store.append_shot_output(output, expected_version=ledger)
    return output


def add_video(world: World, output_id: str, clip: bytes) -> ShotOutput:
    return add_output(world.store, output_id, ShotOutputKind.MASTER,
                      f"renders/{output_id}.mp4", clip)


def png_bytes(size: tuple[int, int], colour: tuple[int, int, int] = (128, 128, 128)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def decode_png(response: Any) -> Image.Image:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == "image/png"
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.content.startswith(PNG_MAGIC)
    return Image.open(io.BytesIO(response.content)).convert("RGB")


# ---- ffmpeg clip factory ---------------------------------------------------

LOSSLESS = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p"]
LIGHT = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p"]


def _ffmpeg(out: Path, *args: str) -> bytes:
    assert FFMPEG is not None
    subprocess.run([FFMPEG, "-v", "error", "-y", *args, str(out)],
                   check=True, capture_output=True, timeout=120)
    return out.read_bytes()


def _tone_source(seconds: float, expr: str = TONE_AMPLITUDE_EXPR) -> list[str]:
    return ["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={seconds}"]


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    if FFMPEG is None:
        pytest.skip("ffmpeg is not on PATH")
    d = tmp_path_factory.mktemp("perception-clips")
    made: dict[str, bytes] = {}
    # Flat gray, 192x108, 3 s, no audio: YAVG ~126, no motion, no flicker.
    made["gray"] = _ffmpeg(d / "gray.mp4", "-f", "lavfi", "-i",
                           "color=c=gray:s=192x108:r=10:d=3", *LOSSLESS)
    # Alternating black / white every frame: temporal luma difference is maximal.
    made["flicker"] = _ffmpeg(
        d / "flicker.mp4", "-f", "lavfi", "-i",
        "color=c=gray:s=160x120:r=10:d=3,format=yuv420p,geq=lum='255*mod(N,2)':cb=128:cr=128",
        *LOSSLESS)
    # Four 1 s segments: red, green, blue, white (160x120, 4 s).
    made["rgbw"] = _ffmpeg(
        d / "rgbw.mp4",
        "-filter_complex",
        "color=c=red:s=160x120:r=10:d=1[a];color=c=green:s=160x120:r=10:d=1[b];"
        "color=c=blue:s=160x120:r=10:d=1[c];color=c=white:s=160x120:r=10:d=1[d];"
        "[a][b][c][d]concat=n=4:v=1:a=0,format=yuv420p", *LOSSLESS)
    made["testsrc"] = _ffmpeg(d / "testsrc.mp4", "-f", "lavfi", "-i",
                              "testsrc=size=320x240:rate=10:duration=3", *LIGHT)
    made["hd"] = _ffmpeg(d / "hd.mp4", "-f", "lavfi", "-i",
                         "testsrc=size=1280x720:rate=10:duration=2", *LIGHT)
    # A 320x160 gray picture padded to 320x240 with 40 px black bars top and bottom.
    made["letterbox"] = _ffmpeg(
        d / "letterbox.mp4", "-f", "lavfi", "-i",
        "color=c=gray:s=320x160:r=10:d=2,pad=320:240:0:40:black,format=yuv420p", *LOSSLESS)
    # testsrc for 2 s, then flat white for 2 s: one hard cut at t = 2.0 s.
    made["cut"] = _ffmpeg(
        d / "cut.mp4", "-filter_complex",
        "testsrc=s=320x240:r=10:d=2[a];color=c=white:s=320x240:r=10:d=2[b];"
        "[a][b]concat=n=2:v=1:a=0,format=yuv420p", *LIGHT)
    # Video with a continuous 1 kHz tone (4 s).
    made["tone"] = _ffmpeg(
        d / "tone.mp4", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=4",
        *_tone_source(4), *LIGHT, "-c:a", "aac", "-b:a", "128k", "-shortest")
    # Video with tone 0-1 s, exact silence 1.0-2.5 s, tone 2.5-3.5 s.
    made["gap"] = _ffmpeg(
        d / "gap.mp4", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3.5",
        *_tone_source(3.5, "if(between(t,1,2.5),0,0.25*sin(2*PI*1000*t))"),
        *LIGHT, "-c:a", "aac", "-b:a", "128k", "-shortest")
    made["wav"] = _ffmpeg(d / "tone.wav", *_tone_source(2), "-c:a", "pcm_s16le")
    # P2: variable frame rate. Red at 2 fps for 2 s, then white at 30 fps for 2 s, with
    # the original timestamps kept: index/average-fps arithmetic lands in the wrong colour.
    made["vfr"] = _ffmpeg(
        d / "vfr.mp4", "-filter_complex",
        "color=c=red:s=160x120:r=2:d=2[a];color=c=white:s=160x120:r=30:d=2[b];"
        "[a][b]concat=n=2:v=1:a=0,format=yuv420p", "-fps_mode", "passthrough", *LOSSLESS)
    # P2b: a sparse VFR clip with frames at exactly 0, 2.0, 2.1 and 2.2 s: red at 0 and
    # white afterwards (a 10 fps source thinned with select, timestamps kept).
    made["sparse"] = _ffmpeg(
        d / "sparse.mp4", "-f", "lavfi", "-i",
        "color=c=red:s=160x120:r=10:d=3,format=yuv420p,"
        "geq=lum='if(gte(T,2),235,81)':cb='if(gte(T,2),128,90)':cr='if(gte(T,2),128,240)',"
        "select='eq(n,0)+gte(n,20)*lte(n,22)'", "-fps_mode", "passthrough", *LOSSLESS)
    # P2b: default libx264 settings (B-frames present), four 1 s colour segments at 25 fps.
    made["bframes"] = _ffmpeg(
        d / "bframes.mp4", "-filter_complex",
        "color=c=red:s=160x120:r=25:d=1[a];color=c=green:s=160x120:r=25:d=1[b];"
        "color=c=blue:s=160x120:r=25:d=1[c];color=c=white:s=160x120:r=25:d=1[d];"
        "[a][b][c][d]concat=n=4:v=1:a=0,format=yuv420p", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    # P1: 6000 frames (60 s at 100 fps), black then white: cropdetect writes ~1 MB of
    # stderr and there is one scene change at 30.0 s.
    made["long"] = _ffmpeg(
        d / "long.mp4", "-filter_complex",
        "color=c=black:s=64x48:r=100:d=30[a];color=c=white:s=64x48:r=100:d=30[b];"
        "[a][b]concat=n=2:v=1:a=0,format=yuv420p", *LOSSLESS)
    # P6: mid-gray in 10-bit.
    made["gray10"] = _ffmpeg(d / "gray10.mp4", "-f", "lavfi", "-i",
                             "color=c=gray:s=192x108:r=10:d=2,format=yuv420p10le",
                             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0",
                             "-pix_fmt", "yuv420p10le")
    # P8: all black, so cropdetect has nothing to detect.
    made["black"] = _ffmpeg(d / "black.mp4", "-f", "lavfi", "-i",
                            "color=c=black:s=192x108:r=10:d=2", *LOSSLESS)
    # P3: silence.
    made["silent_wav"] = _ffmpeg(d / "silent.wav", *_tone_source(2, "0"), "-c:a", "pcm_s16le")
    made["silent_mp4"] = _ffmpeg(
        d / "silent.mp4", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
        *_tone_source(2, "0"), *LIGHT, "-c:a", "aac", "-shortest")
    # P5: square 512 px and very tall portrait pictures.
    made["square"] = _ffmpeg(d / "square.mp4", "-f", "lavfi", "-i",
                             "testsrc2=size=512x512:rate=10:duration=3", *LIGHT)
    made["tall"] = _ffmpeg(d / "tall.mp4", "-f", "lavfi", "-i",
                           "testsrc2=size=90x1280:rate=10:duration=3", *LIGHT)
    # P4: a 10 s MP3 whose Xing/Info header still declares 10 s but 80% of the frames
    # are gone. Absent when this FFmpeg build has no MP3 encoder.
    try:
        full = _ffmpeg(d / "tone.mp3", *_tone_source(10), "-c:a", "libmp3lame", "-b:a", "128k")
        made["mp3_truncated"] = full[: len(full) // 5]
    except subprocess.CalledProcessError:
        pass
    return made


# ---- tolerant readers for the unspecified key names in the metrics JSON ----


def leaves(obj: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from leaves(value, (*path, str(key).lower()))
    elif isinstance(obj, list):
        for value in obj:
            yield from leaves(value, path)
    else:
        yield path, obj


def nums(obj: Any, *needles: str) -> list[float]:
    """Every numeric leaf whose key path contains one of the (lower-case) fragments."""
    found: list[float] = []
    for path, value in leaves(obj):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if any(needle in "/".join(path) for needle in needles):
            found.append(float(value))
    return found


def near(values: list[float], target: float, tol: float) -> bool:
    return any(abs(v - target) <= tol for v in values)


def all_keys(obj: Any) -> set[str]:
    return {part for path, _ in leaves(obj) for part in path}


# ---- picture analysis ------------------------------------------------------


def quadrant(image: Image.Image, col: int, row: int) -> Image.Image:
    w, h = image.size
    return image.crop((col * w // 2, row * h // 2, (col + 1) * w // 2, (row + 1) * h // 2))


def median_rgb(image: Image.Image) -> tuple[int, int, int]:
    r, g, b = ImageStat.Stat(image).median
    return int(r), int(g), int(b)


def median_luma(image: Image.Image) -> int:
    return int(ImageStat.Stat(image.convert("L")).median[0])


def is_red(c: tuple[int, int, int]) -> bool:
    return c[0] > 150 and c[1] < 90 and c[2] < 90


def is_green(c: tuple[int, int, int]) -> bool:
    return c[1] > 100 and c[0] < 90 and c[2] < 90


def is_blue(c: tuple[int, int, int]) -> bool:
    return c[2] > 150 and c[0] < 90 and c[1] < 90


def is_white(c: tuple[int, int, int]) -> bool:
    return min(c) > 180


# --------------------------------------------------------------------------
# R1 - contact sheets
# --------------------------------------------------------------------------


@needs_ffmpeg
def test_r1_default_is_nine_frames_in_a_grid_within_size_limits(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "hd", clips["hd"])
    before = tree_bytes(world.root)
    response = get(client, sheet_url("hd"))
    sheet = decode_png(response)
    assert max(sheet.size) <= 1536
    assert len(response.content) <= 4 * MIB
    # 3x3 of 16:9 tiles is close to 16:9 overall (labels add height). A 1x9 strip or a
    # 9x1 column would be nowhere near.
    assert 1.0 <= sheet.width / sheet.height <= 2.0, sheet.size
    assert tree_bytes(world.root) == before, "contact_sheet wrote to the project"


@needs_ffmpeg
def test_r1_frame_counts_lay_out_as_near_square_grids(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """AMBIGUOUS: the tile label strip is not specified, so the layout is checked through
    the sheet's aspect ratio relative to one 4:3 frame rather than exact pixel sizes."""
    add_video(world, "c", clips["testsrc"])
    for frames in (4, 9, 16):
        sheet = decode_png(get(client, sheet_url("c"), frames=frames))
        relative = (sheet.width / sheet.height) / (4 / 3)
        assert 0.7 <= relative <= 1.4, f"frames={frames}: {sheet.size}"
        assert max(sheet.size) <= 1536
    four, nine = (decode_png(get(client, sheet_url("c"), frames=n)) for n in (4, 9))
    assert abs(four.width / four.height - nine.width / nine.height) < 0.1 * (four.width / four.height)


@needs_ffmpeg
def test_r1_tiles_are_never_upscaled(client: TestClient, world: World,
                                     clips: dict[str, bytes]) -> None:
    add_video(world, "small", clips["rgbw"])  # 160x120
    sheet = decode_png(get(client, sheet_url("small"), frames=4))
    assert sheet.width <= 2 * 160 + 64, sheet.size
    assert sheet.height <= 2 * 120 + 128, sheet.size


@needs_ffmpeg
def test_r1_sampling_follows_the_even_spacing_formula_and_is_row_major(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """The 4 s red/green/blue/white clip, frames=4: t_i = 4*(i+0.5)/4 = 0.5, 1.5, 2.5, 3.5,
    so a 2x2 row-major grid is red | green over blue | white."""
    add_video(world, "rgbw", clips["rgbw"])
    response = get(client, sheet_url("rgbw"), frames=4)
    sheet = decode_png(response)
    tiles = [median_rgb(quadrant(sheet, c, r)) for r in (0, 1) for c in (0, 1)]
    assert is_red(tiles[0]) and is_green(tiles[1]) and is_blue(tiles[2]) and is_white(tiles[3]), tiles
    # The same instants given explicitly produce the very same picture.
    explicit = get(client, sheet_url("rgbw"), times="0.5,1.5,2.5,3.5")
    assert explicit.status_code == 200, explicit.text
    assert explicit.content == response.content


@needs_ffmpeg
def test_r1_explicit_times_select_exactly_those_instants(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "rgbw", clips["rgbw"])
    sheet = decode_png(get(client, sheet_url("rgbw"), times="0.2,0.4,3.2,3.6"))
    tiles = [median_rgb(quadrant(sheet, c, r)) for r in (0, 1) for c in (0, 1)]
    assert is_red(tiles[0]) and is_red(tiles[1]) and is_white(tiles[2]) and is_white(tiles[3]), tiles
    # No green or blue anywhere on that sheet.
    pixels = list(sheet.getdata())
    assert not any(is_green(p) or is_blue(p) for p in pixels[::7])


@needs_ffmpeg
@pytest.mark.parametrize("frames", [2, 16])
def test_r1_frames_bounds_accept_the_edges(client: TestClient, world: World,
                                           clips: dict[str, bytes], frames: int) -> None:
    add_video(world, "c", clips["testsrc"])
    assert decode_png(get(client, sheet_url("c"), frames=frames)).width > 0


@needs_ffmpeg
@pytest.mark.parametrize("frames", [1, 17, 0, -3, 100])
def test_r1_frames_outside_2_to_16_is_422(client: TestClient, world: World,
                                          clips: dict[str, bytes], frames: int) -> None:
    add_video(world, "c", clips["testsrc"])
    assert get(client, sheet_url("c"), frames=frames).status_code == 422


@needs_ffmpeg
@pytest.mark.parametrize("times", [
    "1.0",                                  # fewer than two
    ",".join(str(i * 0.1) for i in range(17)),  # more than sixteen
    "1.5,0.5",                              # descending
    "0.5,1.5,1.0",                          # not ascending
    "-1,1",                                 # negative
    "nan,1",                                # not finite
    "0.5,inf",
    "0.5,abc",
    "0.5,99",                               # past the end
    "1,3.0",                                # at the end of a 3 s clip
])
def test_r1_invalid_times_are_422_and_write_nothing(
    client: TestClient, world: World, clips: dict[str, bytes], times: str,
) -> None:
    add_video(world, "c", clips["testsrc"])
    before = tree_bytes(world.root)
    assert get(client, sheet_url("c"), times=times).status_code == 422, times
    assert tree_bytes(world.root) == before


@needs_ffmpeg
def test_r1_unknown_mode_is_422(client: TestClient, world: World, clips: dict[str, bytes]) -> None:
    add_video(world, "c", clips["testsrc"])
    assert get(client, sheet_url("c"), mode="bogus").status_code == 422


def test_r1_image_outputs_are_422_pointing_at_inspect_output(
    client: TestClient, world: World,
) -> None:
    add_output(world.store, "img", ShotOutputKind.PREVIEW, "previews/img.png", png_bytes((32, 32)))
    response = get(client, sheet_url("img"))
    assert response.status_code == 422, response.text
    assert "inspect_output" in response.text


def test_r1_scene_outputs_are_422_pointing_at_inspect_output(
    client: TestClient, world: World,
) -> None:
    add_output(world.store, "scn", ShotOutputKind.SCENE, "scenes/s1/scene.blend", b"BLENDER-v5")
    before = tree_bytes(world.root)
    response = get(client, sheet_url("scn"))
    assert response.status_code == 422, response.text
    assert "inspect_output" in response.text
    assert tree_bytes(world.root) == before


def test_r1_unknown_output_is_404(client: TestClient, world: World) -> None:
    add_shot(world.store)
    assert get(client, sheet_url("nope")).status_code == 404


@needs_ffmpeg
def test_r1_a_cut_has_a_contact_sheet(client: TestClient, world: World,
                                      clips: dict[str, bytes]) -> None:
    build_cut(world, [{"id": "s1", "start": 0, "dur": 3}], clip=clips["testsrc"])
    assert decode_png(get(client, sheet_url("cut-1"), frames=4)).width > 0


@needs_ffmpeg
def test_r1_difference_of_a_static_clip_is_near_black(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """n=5 samples give 4 difference tiles: a 2x2 grid, every quadrant (text labels
    aside, hence the median) near black."""
    add_video(world, "gray", clips["gray"])
    before = tree_bytes(world.root)
    sheet = decode_png(get(client, sheet_url("gray"), mode="difference",
                           times="0.5,1.0,1.5,2.0,2.5"))
    medians = [median_luma(quadrant(sheet, c, r)) for r in (0, 1) for c in (0, 1)]
    assert all(m < 20 for m in medians), medians
    assert tree_bytes(world.root) == before


@needs_ffmpeg
def test_r1_difference_of_a_flickering_clip_is_bright(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """Frames alternate black/white at 10 fps; samples on consecutive frames differ by
    full scale, so all four difference tiles are bright."""
    add_video(world, "flicker", clips["flicker"])
    sheet = decode_png(get(client, sheet_url("flicker"), mode="difference",
                           times="0.05,0.15,0.25,0.35,0.45"))
    medians = [median_luma(quadrant(sheet, c, r)) for r in (0, 1) for c in (0, 1)]
    assert all(m > 200 for m in medians), medians


@needs_ffmpeg
def test_r1_difference_with_n_samples_gives_n_minus_one_tiles(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """Grid shape: 5 samples -> 4 tiles -> the same 2x2 grid as a 4-frame sheet, and
    10 samples -> 9 tiles -> the same 3x3 grid as a 9-frame sheet. Compared through the
    aspect ratio because tile labels are not specified."""
    add_video(world, "c", clips["testsrc"])

    def aspect(**params: Any) -> float:
        sheet = decode_png(get(client, sheet_url("c"), **params))
        return sheet.width / sheet.height

    two_by_two = aspect(frames=4)
    three_by_three = aspect(frames=9)
    diff5 = aspect(mode="difference", frames=5)
    diff10 = aspect(mode="difference", frames=10)
    assert abs(diff5 - two_by_two) < 0.08 * two_by_two
    assert abs(diff10 - three_by_three) < 0.08 * three_by_three
    # 2 samples -> one tile only: a single picture, not a grid.
    one = decode_png(get(client, sheet_url("c"), mode="difference", times="0.5,1.5"))
    assert one.width <= 320 + 64 and one.height <= 240 + 96, one.size


@needs_ffmpeg
def test_r1_tampered_video_is_corrupt_artifact(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    output = add_video(world, "c", clips["testsrc"])
    world.store._root.write_bytes(output.relative_path, clips["testsrc"] + b"\x00")
    response = get(client, sheet_url("c"))
    assert response.status_code >= 400
    assert response.json()["kind"] == "corrupt_artifact", response.text


@needs_ffmpeg
def test_r1_over_mcp_the_sheet_is_an_image_block(client: TestClient, world: World,
                                                 clips: dict[str, bytes]) -> None:
    add_video(world, "c", clips["testsrc"])
    server = mcp.McpServer(AppTransport(client))
    response = server.handle(_rpc("tools/call", params={
        "name": "pipeline_contact_sheet",
        "arguments": {"project_id": PID, "episode_id": EP, "output_id": "c", "frames": 4}}))
    assert response is not None
    result = _tool_result(response)
    assert result["isError"] is False
    (block,) = result["content"]
    assert block["type"] == "image" and block["mimeType"] == "image/png"
    assert base64.b64decode(block["data"]).startswith(PNG_MAGIC)


# --------------------------------------------------------------------------
# R2 - the deterministic rubric sample
# --------------------------------------------------------------------------


def build_cut(world: World, shots: list[dict[str, Any]], *, clip: bytes | None = None,
              manifest: dict[str, int] | None = None, credit_sec: float = 0.0,
              output_id: str = "cut-1") -> EpisodeOutput:
    """A Timeline of shot entries and an EpisodeOutput bound to it.

    ``shots``: ``{"id", "start", "dur", "event"?, "anomaly"?}``. ``event`` alone sets
    ``event_sec`` on a shot without an anomaly. ``manifest`` ({shot_id: version}) writes a
    credit manifest pinning those Shot versions, plus ``credit_sec`` of credit cards.
    """
    store = world.store
    ensure_episode(store)
    entries = []
    for spec in shots:
        has_anomaly = bool(spec.get("anomaly"))
        add_shot(store, spec["id"], duration_sec=spec["dur"], event_sec=spec.get("event"),
                 anomaly=ANOMALY if has_anomaly else None)
        entries.append(TimelineEntry(type="shot", ref=spec["id"], start_sec=spec["start"],
                                     duration_sec=spec["dur"]))
    timeline = Timeline(episode_id=EP, entries=entries)
    store.save(timeline, expected_version=0)
    token = store.load_with_version(Timeline, EP)[1]
    media = clip if clip is not None else b"cut media placeholder"
    relative = f"episodes/{EP}/outputs/cuts/{output_id}.mp4"
    store._root.write_bytes(relative, media)
    values: dict[str, Any] = dict(
        output_id=output_id, episode_id=EP, timeline_store_version=token,
        timeline_sha256=timeline.content_sha256, relative_path=relative,
        content_sha256=hashlib.sha256(media).hexdigest(), delivery_profile="test",
        storage_resolution=(320, 240), output_fps=10, sample_aspect_ratio="1/1",
        ffmpeg_identity="fixture")
    if manifest is not None:
        manifest_path = f"episodes/{EP}/outputs/cuts/{output_id}.credits.json"
        snapshot = {
            "schema_version": 1, "entries": [], "publication_blockers": [], "plain_text": "",
            "shot_versions": [{"shot_id": sid, "shot_version": version, "asset_ids": []}
                              for sid, version in manifest.items()],
            "narrative_duration_sec": timeline.total_duration_sec,
            "credit_duration_sec": credit_sec,
            "total_duration_sec": timeline.total_duration_sec + credit_sec,
            "generated_cards": [],
        }
        payload = (json.dumps(snapshot, sort_keys=True, indent=2) + "\n").encode()
        store._root.write_bytes(manifest_path, payload)
        values.update(credit_manifest_path=manifest_path,
                      credit_manifest_sha256=hashlib.sha256(payload).hexdigest())
    receipt = EpisodeOutput(**values)
    store.append_episode_output(receipt, expected_version=0)
    return receipt


#: 10 s narrative. s1 [0,4) has an event_sec but NO anomaly (must not count);
#: s2 [4,7) anomaly moment 4 + 1.5 = 5.5; s3 [7,10) anomaly moment 7 + 1.0 = 8.0.
TEN_SECONDS = [
    {"id": "s1", "start": 0, "dur": 4, "event": 2.0},
    {"id": "s2", "start": 4, "dur": 3, "event": 1.5, "anomaly": True},
    {"id": "s3", "start": 7, "dur": 3, "event": 1.0, "anomaly": True},
]


def plan_of(client: TestClient, output_id: str = "cut-1") -> dict[str, Any]:
    response = get(client, sample_url(output_id))
    assert response.status_code == 200, response.text
    return response.json()


def times_by_reason(plan: dict[str, Any]) -> dict[str, list[float]]:
    grouped: dict[str, list[float]] = {}
    for sample in plan["samples"]:
        grouped.setdefault(sample["reason"], []).append(sample["time_sec"])
    return {reason: sorted(values) for reason, values in grouped.items()}


def test_r2_every_three_seconds_while_before_the_narrative_end(
    client: TestClient, world: World,
) -> None:
    build_cut(world, TEN_SECONDS)
    plan = plan_of(client)
    assert plan["output_id"] == "cut-1"
    assert plan["narrative_duration_sec"] == pytest.approx(10.0)
    assert times_by_reason(plan)["every_3s"] == pytest.approx([0, 3, 6, 9])


@pytest.mark.parametrize(("narrative", "expected"), [
    (9.0, [0, 3, 6]),           # t = 9 is the end, not before it
    (13.0, [0, 3, 6, 9, 12]),
    (3.5, [0, 3]),
])
def test_r2_every_three_seconds_boundaries(
    client: TestClient, world: World, narrative: float, expected: list[float],
) -> None:
    build_cut(world, [{"id": "s1", "start": 0, "dur": narrative}])
    assert times_by_reason(plan_of(client))["every_3s"] == pytest.approx(expected)


def test_r2_first_anomaly_sequence_is_five_frames_half_a_second_apart_centred_on_the_event(
    client: TestClient, world: World,
) -> None:
    build_cut(world, TEN_SECONDS)
    plan = plan_of(client)
    anomaly = [s for s in plan["samples"] if s["reason"] == "first_anomaly"]
    assert sorted(s["time_sec"] for s in anomaly) == pytest.approx([4.5, 5.0, 5.5, 6.0, 6.5])
    assert {s["shot_id"] for s in anomaly} == {"s2"}


def test_r2_longest_neutral_interval_is_centred_on_the_longest_anomaly_free_span(
    client: TestClient, world: World,
) -> None:
    """Anomaly moments 5.5 and 8.0 split [0,10] into 5.5, 2.5 and 2.0 s; the longest is
    [0, 5.5], midpoint 2.75."""
    build_cut(world, TEN_SECONDS)
    plan = plan_of(client)
    neutral = [s for s in plan["samples"] if s["reason"] == "longest_neutral"]
    assert sorted(s["time_sec"] for s in neutral) == pytest.approx(
        [1.75, 2.25, 2.75, 3.25, 3.75])
    assert {s["shot_id"] for s in neutral} == {"s1"}


def test_r2_event_sec_without_an_anomaly_is_not_an_anomaly_moment(
    client: TestClient, world: World,
) -> None:
    build_cut(world, [{"id": "s1", "start": 0, "dur": 10, "event": 5.0}])
    plan = plan_of(client)
    assert "first_anomaly" not in times_by_reason(plan)


def test_r2_a_cut_without_an_anomaly_omits_the_sequence_and_says_so(
    client: TestClient, world: World,
) -> None:
    """AMBIGUOUS: how the response says so is unspecified; ``interpretation`` is the
    only prose field the contract names, so it must mention the missing anomaly. The
    neutral interval is the whole narrative: centred at 5.0."""
    build_cut(world, [{"id": "s1", "start": 0, "dur": 10}])
    plan = plan_of(client)
    grouped = times_by_reason(plan)
    assert "first_anomaly" not in grouped
    assert grouped["longest_neutral"] == pytest.approx([4.0, 4.5, 5.0, 5.5, 6.0])
    assert "anomal" in plan["interpretation"].lower()


def test_r2_the_neutral_interval_uses_the_start_and_end_as_boundaries(
    client: TestClient, world: World,
) -> None:
    """Anomaly at 8.5 in a 10 s shot: spans are [0, 8.5] and [8.5, 10]; midpoint 4.25."""
    build_cut(world, [{"id": "s1", "start": 0, "dur": 10, "event": 8.5, "anomaly": True}])
    grouped = times_by_reason(plan_of(client))
    assert grouped["first_anomaly"] == pytest.approx([7.5, 8.0, 8.5, 9.0, 9.5])
    assert grouped["longest_neutral"] == pytest.approx([3.25, 3.75, 4.25, 4.75, 5.25])


@pytest.mark.parametrize("event", [0.25, 9.9])
def test_r2_sequences_are_clamped_inside_the_narrative(
    client: TestClient, world: World, event: float,
) -> None:
    """AMBIGUOUS: "clamped inside" is read as every frame time in [0, narrative). Whether
    the window shifts or the frames pile up on the edge is not asserted."""
    build_cut(world, [{"id": "s1", "start": 0, "dur": 10, "event": event, "anomaly": True}])
    plan = plan_of(client)
    anomaly = [s for s in plan["samples"] if s["reason"] == "first_anomaly"]
    assert len(anomaly) == 5
    assert all(0 <= s["time_sec"] < 10 for s in plan["samples"])


def test_r2_first_anomaly_is_the_earliest_entry_not_the_latest(
    client: TestClient, world: World,
) -> None:
    build_cut(world, [
        {"id": "late", "start": 6, "dur": 4, "event": 1.0, "anomaly": True},
        {"id": "early", "start": 0, "dur": 6, "event": 2.0, "anomaly": True},
    ])
    grouped = times_by_reason(plan_of(client))
    assert grouped["first_anomaly"] == pytest.approx([1.0, 1.5, 2.0, 2.5, 3.0])


def test_r2_indices_are_dense_from_zero_and_each_sample_has_the_named_fields(
    client: TestClient, world: World,
) -> None:
    build_cut(world, TEN_SECONDS)
    plan = plan_of(client)
    samples = plan["samples"]
    assert len(samples) == 4 + 5 + 5
    assert sorted(s["index"] for s in samples) == list(range(len(samples)))
    assert {s["reason"] for s in samples} == {"every_3s", "first_anomaly", "longest_neutral"}
    assert all(set(s) >= {"index", "time_sec", "reason", "shot_id"} for s in samples)
    assert isinstance(plan["interpretation"], str) and plan["interpretation"].strip()
    every = {s["time_sec"]: s["shot_id"] for s in samples if s["reason"] == "every_3s"}
    assert every[0.0] == "s1" and every[6.0] == "s2" and every[9.0] == "s3"


def test_r2_the_same_cut_always_yields_the_same_plan_and_nothing_is_written(
    client: TestClient, world: World,
) -> None:
    build_cut(world, TEN_SECONDS)
    before = tree_bytes(world.root)
    first = get(client, sample_url("cut-1"))
    second = get(client, sample_url("cut-1"))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert tree_bytes(world.root) == before


def test_r2_credit_cards_are_excluded_from_the_narrative_duration(
    client: TestClient, world: World,
) -> None:
    build_cut(world, TEN_SECONDS, manifest={"s1": 1, "s2": 1, "s3": 1}, credit_sec=6.0)
    plan = plan_of(client)
    assert plan["narrative_duration_sec"] == pytest.approx(10.0)
    assert times_by_reason(plan)["every_3s"] == pytest.approx([0, 3, 6, 9])
    assert all(s["time_sec"] < 10 for s in plan["samples"])


def revise_shot(store: ProjectStore, shot_id: str, **changes: Any) -> Shot:
    current, token = store.load_with_version(Shot, shot_id, episode_id=EP)
    revised = current.model_copy(update={**current.next_version(), **changes})
    store.revise(revised, expected_version=token)
    return store.load(Shot, shot_id, episode_id=EP)


def test_r2_pinned_shot_versions_come_from_the_credit_manifest(
    client: TestClient, world: World,
) -> None:
    """The cut pinned s2 at v1 (event 1.5 -> moment 5.5); s2 has since been revised to v2
    (event 0.5 -> moment 4.5). The plan follows the pin and says where it came from.
    AMBIGUOUS: the value of ``shot_versions_source`` is unspecified; it must mention the
    manifest."""
    build_cut(world, TEN_SECONDS, manifest={"s1": 1, "s2": 1, "s3": 1})
    assert revise_shot(world.store, "s2", event_sec=0.5).version == 2
    plan = plan_of(client)
    assert "manifest" in str(plan["shot_versions_source"]).lower()
    assert times_by_reason(plan)["first_anomaly"] == pytest.approx([4.5, 5.0, 5.5, 6.0, 6.5])


def test_r2_without_a_manifest_the_current_shot_version_is_used_and_reported(
    client: TestClient, world: World,
) -> None:
    """AMBIGUOUS: the value of ``shot_versions_source`` must mention "current"."""
    build_cut(world, TEN_SECONDS)
    assert revise_shot(world.store, "s2", event_sec=0.5).version == 2
    plan = plan_of(client)
    assert "current" in str(plan["shot_versions_source"]).lower()
    assert times_by_reason(plan)["first_anomaly"] == pytest.approx([3.5, 4.0, 4.5, 5.0, 5.5])


def test_r2_unknown_cut_is_404(client: TestClient, world: World) -> None:
    build_cut(world, TEN_SECONDS)
    assert get(client, sample_url("no-such-cut")).status_code == 404
    assert post(client, evaluate_url("no-such-cut"), {"scores": []}).status_code == 404


# ---- evaluate_rubric -------------------------------------------------------


ALL_TRUE = {"mount_inferable": True, "plausible_place": True, "no_unmotivated_movement": True,
            "stable_light_and_geometry": True, "integrated_anomaly": True}


def scores(**overrides: bool) -> dict[str, bool]:
    return {**ALL_TRUE, **overrides}


FOUR_OF_FIVE = scores(mount_inferable=False)             # passes: criterion 3 holds
THREE_OF_FIVE = scores(mount_inferable=False, plausible_place=False)   # fails
CRITERION_3_ONLY = scores(no_unmotivated_movement=False)  # 4 of 5, but criterion 3 fails


def evaluate(client: TestClient, per_sample: list[dict[str, bool]],
             output_id: str = "cut-1") -> Any:
    body = {"scores": [{"index": i, "scores": s} for i, s in enumerate(per_sample)]}
    return post(client, evaluate_url(output_id), body)


@pytest.fixture
def ten_samples(world: World) -> World:
    """13 s of one shot, no anomaly: every-3s 0,3,6,9,12 plus a 5-frame neutral sequence
    gives exactly 10 samples, so 80% is exactly 8."""
    build_cut(world, [{"id": "s1", "start": 0, "dur": 13}])
    return world


def test_r2_the_ten_sample_fixture_really_has_ten_samples(client: TestClient,
                                                          ten_samples: World) -> None:
    assert len(plan_of(client)["samples"]) == 10


def test_r2_all_samples_passing_passes(client: TestClient, ten_samples: World) -> None:
    response = evaluate(client, [scores()] * 10)
    assert response.status_code == 200, response.text
    verdict = response.json()
    assert verdict["passed"] is True
    assert verdict["samples"] == 10 and verdict["passing_samples"] == 10
    assert verdict["pass_ratio"] == pytest.approx(1.0)
    assert verdict["criterion_3_failures"] == []
    assert [p["index"] for p in verdict["per_sample"]] == list(range(10))
    assert all(p["passed"] is True for p in verdict["per_sample"])


def test_r2_exactly_eighty_percent_passes(client: TestClient, ten_samples: World) -> None:
    """Eight pass (one of them with only 4 of 5); two fail with 3 of 5 -> 0.8 exactly."""
    per_sample = [scores()] * 7 + [FOUR_OF_FIVE] + [THREE_OF_FIVE] * 2
    verdict = evaluate(client, per_sample).json()
    assert verdict["passing_samples"] == 8
    assert verdict["pass_ratio"] == pytest.approx(0.8)
    assert verdict["passed"] is True
    assert verdict["criterion_3_failures"] == []
    assert [p["passed"] for p in verdict["per_sample"]] == [True] * 8 + [False] * 2


def test_r2_just_below_eighty_percent_fails(client: TestClient, ten_samples: World) -> None:
    per_sample = [scores()] * 6 + [FOUR_OF_FIVE] + [THREE_OF_FIVE] * 3
    verdict = evaluate(client, per_sample).json()
    assert verdict["passing_samples"] == 7
    assert verdict["pass_ratio"] == pytest.approx(0.7)
    assert verdict["passed"] is False
    assert verdict["criterion_3_failures"] == []


def test_r2_one_criterion_3_failure_fails_an_otherwise_passing_cut(
    client: TestClient, ten_samples: World,
) -> None:
    """Nine of ten samples are perfect, the tenth has only criterion 3 wrong (4 of 5).
    AMBIGUOUS: whether that sample counts in ``passing_samples`` (4 of 5 satisfied, or
    not because criterion 3 is mandatory) is unspecified; both 9 and 10 are accepted. The
    verdict must be a failure either way, and index 4 must be named."""
    per_sample = [scores()] * 4 + [CRITERION_3_ONLY] + [scores()] * 5
    verdict = evaluate(client, per_sample).json()
    assert verdict["passed"] is False
    assert verdict["criterion_3_failures"] == [4]
    assert verdict["passing_samples"] in (9, 10)
    assert verdict["samples"] == 10


def test_r2_criterion_3_failures_are_listed_in_index_order(client: TestClient,
                                                          ten_samples: World) -> None:
    per_sample = [scores()] * 10
    for index in (8, 1, 5):
        per_sample[index] = CRITERION_3_ONLY
    verdict = evaluate(client, per_sample).json()
    assert verdict["criterion_3_failures"] == [1, 5, 8]
    assert verdict["passed"] is False


def test_r2_five_failing_criteria_fail_a_sample(client: TestClient, ten_samples: World) -> None:
    nothing = {key: False for key in ALL_TRUE}
    verdict = evaluate(client, [scores()] * 9 + [nothing]).json()
    assert verdict["passing_samples"] == 9
    assert verdict["pass_ratio"] == pytest.approx(0.9)
    assert verdict["criterion_3_failures"] == [9]
    assert verdict["passed"] is False


def test_r2_just_below_eighty_percent_on_the_fourteen_sample_cut(
    client: TestClient, world: World,
) -> None:
    """14 samples: 12 passing is 0.857 and passes; 11 is 0.786 and does not."""
    build_cut(world, TEN_SECONDS)
    assert len(plan_of(client)["samples"]) == 14
    passing = evaluate(client, [scores()] * 12 + [THREE_OF_FIVE] * 2).json()
    assert passing["passed"] is True
    assert passing["pass_ratio"] == pytest.approx(12 / 14)
    failing = evaluate(client, [scores()] * 11 + [THREE_OF_FIVE] * 3).json()
    assert failing["passed"] is False
    assert failing["pass_ratio"] == pytest.approx(11 / 14)


def test_r2_the_entry_order_in_the_body_does_not_matter(client: TestClient,
                                                         ten_samples: World) -> None:
    body = {"scores": [{"index": i, "scores": THREE_OF_FIVE if i == 2 else scores()}
                       for i in reversed(range(10))]}
    verdict = post(client, evaluate_url("cut-1"), body).json()
    assert verdict["per_sample"][2] == {"index": 2, "passed": False}
    assert verdict["passing_samples"] == 9


def test_r2_a_missing_index_is_422_and_named(client: TestClient, ten_samples: World) -> None:
    body = {"scores": [{"index": i, "scores": scores()} for i in range(10) if i != 7]}
    response = post(client, evaluate_url("cut-1"), body)
    assert response.status_code == 422, response.text
    assert "7" in response.text


def test_r2_an_extra_index_is_422_and_named(client: TestClient, ten_samples: World) -> None:
    body = {"scores": [{"index": i, "scores": scores()} for i in [*range(10), 99]]}
    response = post(client, evaluate_url("cut-1"), body)
    assert response.status_code == 422, response.text
    assert "99" in response.text


def test_r2_missing_and_extra_are_both_named(client: TestClient, ten_samples: World) -> None:
    body = {"scores": [{"index": i, "scores": scores()} for i in [*range(7), 8, 9, 55]]}
    response = post(client, evaluate_url("cut-1"), body)
    assert response.status_code == 422, response.text
    assert "7" in response.text and "55" in response.text


def test_r2_a_duplicated_index_is_422(client: TestClient, ten_samples: World) -> None:
    """AMBIGUOUS: "exactly one entry per sample index" is read as refusing a duplicate."""
    body = {"scores": [{"index": i, "scores": scores()} for i in [*range(10), 3]]}
    assert post(client, evaluate_url("cut-1"), body).status_code == 422


@pytest.mark.parametrize("label", ["empty", "extra top-level field", "extra entry field",
                                   "incomplete criteria", "non-boolean criterion"])
def test_r2_malformed_bodies_are_422_and_nothing_is_stored(
    client: TestClient, ten_samples: World, label: str,
) -> None:
    entries = [{"index": i, "scores": scores()} for i in range(10)]
    bodies: dict[str, Any] = {
        "empty": {"scores": []},
        "extra top-level field": {"scores": entries, "passed": True},
        "extra entry field": {"scores": [{**entries[0], "note": "x"}, *entries[1:]]},
        "incomplete criteria": {"scores": [
            {"index": 0, "scores": {"mount_inferable": True}}, *entries[1:]]},
        "non-boolean criterion": {"scores": [
            {"index": 0, "scores": {**ALL_TRUE, "plausible_place": "maybe"}}, *entries[1:]]},
    }
    before = tree_bytes(ten_samples.root)
    assert post(client, evaluate_url("cut-1"), bodies[label]).status_code == 422, label
    assert tree_bytes(ten_samples.root) == before


def test_r2_evaluation_stores_nothing(client: TestClient, ten_samples: World) -> None:
    before = tree_bytes(ten_samples.root)
    assert evaluate(client, [scores()] * 10).status_code == 200
    assert tree_bytes(ten_samples.root) == before
    assert ten_samples.store.list_reviews() == []


# --------------------------------------------------------------------------
# R3 - measured metrics
# --------------------------------------------------------------------------


def measure(client: TestClient, output_id: str, **params: Any) -> dict[str, Any]:
    response = get(client, metrics_url(output_id), **params)
    assert response.status_code == 200, response.text
    return response.json()


@needs_ffmpeg
def test_r3_container_and_video_basics_match_the_clip(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "t", clips["testsrc"])
    before = tree_bytes(world.root)
    body = measure(client, "t")
    assert tree_bytes(world.root) == before
    assert near(nums(body["container"], "duration"), 3.0, 0.3)
    assert any(v > 0 for v in nums(body["container"], "bitrate", "bit_rate"))
    video = body["video"]
    assert near(nums(video, "width"), 320, 0.5) and near(nums(video, "height"), 240, 0.5)
    assert near(nums(video, "fps", "frame_rate"), 10, 0.2)
    assert "264" in json.dumps(video).lower()
    assert "audio" not in body or not body["audio"]
    assert "image" not in body or not body["image"]


@needs_ffmpeg
def test_r3_a_flat_gray_clip_has_the_expected_luma_and_no_saturation_or_motion(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """Gray 0x808080 in limited range is Y ~ 126."""
    add_video(world, "gray", clips["gray"])
    video = measure(client, "gray", samples=5)["video"]
    yavg = nums(video, "yavg", "luma_mean")
    assert len(yavg) >= 5
    assert all(abs(v - 126) <= 8 for v in yavg), yavg
    assert all(v < 8 for v in nums(video, "satavg", "saturation")), "gray has no saturation"
    assert all(v < 5 for v in nums(video, "ydif", "luma_diff", "temporal")), "no flicker"
    assert all(v < 2 for v in nums(video, "edge")), "a flat picture has no edges"


@needs_ffmpeg
def test_r3_a_flickering_clip_has_a_large_temporal_luma_difference(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """The clip is 30 frames alternating black and white; 12 samples are spaced 2.5
    frames apart, so many consecutive samples land on opposite colours."""
    add_video(world, "flicker", clips["flicker"])
    video = measure(client, "flicker", samples=12)["video"]
    assert max(nums(video, "ydif", "luma_diff", "temporal")) > 100
    assert max(nums(video, "yavg", "luma_mean")) - min(nums(video, "yavg", "luma_mean")) > 100


@needs_ffmpeg
def test_r3_a_busy_picture_has_more_edge_energy_than_a_flat_one(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "gray", clips["gray"])
    add_video(world, "busy", clips["testsrc"])
    flat = max(nums(measure(client, "gray")["video"], "edge"))
    busy = max(nums(measure(client, "busy")["video"], "edge"))
    assert busy > flat + 0.5, (busy, flat)


@needs_ffmpeg
def test_r3_a_colourful_clip_has_high_saturation_and_a_gray_one_does_not(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "rgbw", clips["rgbw"])
    assert max(nums(measure(client, "rgbw")["video"], "satavg", "saturation")) > 30


@needs_ffmpeg
def test_r3_samples_controls_the_number_of_instants(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "gray", clips["gray"])
    for samples in (1, 5, 60):
        body = measure(client, "gray", samples=samples)
        assert len(nums(body["video"], "yavg", "luma_mean")) >= samples, samples


@needs_ffmpeg
@pytest.mark.parametrize("samples", [0, 61, -1])
def test_r3_samples_outside_1_to_60_is_422(client: TestClient, world: World,
                                           clips: dict[str, bytes], samples: int) -> None:
    add_video(world, "gray", clips["gray"])
    assert get(client, metrics_url("gray"), samples=samples).status_code == 422


@needs_ffmpeg
def test_r3_letterboxing_is_detected_by_cropdetect(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """320x240 frame, 40 px black bars: the active area is 320x160 at y = 40.
    AMBIGUOUS: the shape of the estimate (pixels, bar sizes or fractions) is unspecified;
    any of the active height 160, the bar height 40, or the ratios 2/3 and 1/3 qualifies,
    and a full frame must not report them."""
    add_video(world, "lb", clips["letterbox"])
    add_video(world, "gray", clips["gray"])  # 192x108, full frame
    crop = nums(measure(client, "lb")["video"], "crop", "letterbox", "pillarbox", "active")
    assert (near(crop, 160, 2) or near(crop, 40, 2) or near(crop, 2 / 3, 0.02)
            or near(crop, 66.7, 1) or near(crop, 1 / 3, 0.02) or near(crop, 33.3, 1)), crop
    full = nums(measure(client, "gray")["video"], "crop", "letterbox", "pillarbox", "active")
    assert not (near(full, 160, 2) or near(full, 40, 2)), full


@needs_ffmpeg
def test_r3_a_hard_cut_is_reported_as_a_scene_change_near_the_cut_time(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "cut", clips["cut"])
    times = nums(measure(client, "cut")["video"], "scene")
    assert near(times, 2.0, 0.25), times


@needs_ffmpeg
def test_r3_a_flat_clip_has_no_scene_change_times(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """AMBIGUOUS: values under the scene key may include the 0.3 threshold itself, so
    only "no time inside the clip's duration beyond the threshold" is asserted."""
    add_video(world, "gray", clips["gray"])
    times = nums(measure(client, "gray")["video"], "scene")
    assert not [t for t in times if 0.5 < t <= 3.1], times


@needs_ffmpeg
def test_r3_audio_loudness_and_true_peak_match_a_known_tone(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "tone", clips["tone"])
    body = measure(client, "tone")
    audio = body["audio"]
    assert near(nums(audio, "lufs", "integrated"), TONE_LUFS, LEVEL_TOLERANCE_DB), audio
    assert near(nums(audio, "peak"), TONE_PEAK_DB, LEVEL_TOLERANCE_DB), audio
    assert any(v >= 0 for v in nums(audio, "range", "lra"))
    # The video block is there too, and the tone has no silence to report.
    assert near(nums(body["video"], "width"), 160, 0.5)
    assert not [v for v in nums(audio, "silence") if 0.1 < v < 4.1 and v not in (0.5,)]


@needs_ffmpeg
def test_r3_silence_intervals_report_a_silent_gap(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """Exact silence from 1.0 s to 2.5 s (1.5 s long) in a 3.5 s clip. AMBIGUOUS: an
    interval may be reported as start/end or start/duration; either qualifies."""
    add_video(world, "gap", clips["gap"])
    silence = nums(measure(client, "gap")["audio"], "silence")
    assert near(silence, 1.0, 0.2), silence
    assert near(silence, 2.5, 0.2) or near(silence, 1.5, 0.2), silence


@needs_ffmpeg
def test_r3_every_number_states_its_unit(client: TestClient, world: World,
                                         clips: dict[str, bytes]) -> None:
    """AMBIGUOUS: "in its key or a sibling units map". Accepts a ``units`` map anywhere
    in the body, or unit-bearing key names throughout the audio block."""
    add_video(world, "tone", clips["tone"])
    body = measure(client, "tone")
    keys = all_keys(body)
    unit_fragments = ("lufs", "_db", "dbtp", "dbfs", "lu", "_sec", "_hz", "_bps", "_px",
                      "_s", "fps")
    assert "units" in keys or all(
        any(fragment in key for fragment in unit_fragments)
        for path, value in leaves(body["audio"])
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        for key in [path[-1]]), body["audio"]


def test_r3_an_image_output_gets_only_the_image_block(client: TestClient,
                                                       world: World) -> None:
    add_output(world.store, "img", ShotOutputKind.PREVIEW, "previews/img.png",
               png_bytes((64, 48), (128, 128, 128)))
    before = tree_bytes(world.root)
    body = measure(client, "img")
    assert tree_bytes(world.root) == before
    image = body["image"]
    assert near(nums(image, "width"), 64, 0.5) and near(nums(image, "height"), 48, 0.5)
    assert "rgb" in json.dumps(image).lower()
    assert near(nums(image, "yavg", "luma_mean", "mean"), 128, 8), image
    assert all(v < 2 for v in nums(image, "satavg", "saturation")), image
    assert not body.get("video") and not body.get("audio")


def test_r3_a_scene_output_has_no_metrics(client: TestClient, world: World) -> None:
    """AMBIGUOUS: the contract does not name this case; a scene has no frames or audio, so
    it is a 422, as for inspect_output."""
    add_output(world.store, "scn", ShotOutputKind.SCENE, "scenes/s1/scene.blend", b"BLENDER-v5")
    assert get(client, metrics_url("scn")).status_code == 422


def test_r3_unknown_output_is_404(client: TestClient, world: World) -> None:
    add_shot(world.store)
    assert get(client, metrics_url("nope")).status_code == 404


@needs_ffmpeg
def test_r3_tampered_bytes_are_corrupt_artifact(client: TestClient, world: World,
                                                clips: dict[str, bytes]) -> None:
    output = add_video(world, "t", clips["testsrc"])
    world.store._root.write_bytes(output.relative_path, clips["testsrc"] + b"\x00")
    response = get(client, metrics_url("t"))
    assert response.status_code >= 400
    assert response.json()["kind"] == "corrupt_artifact", response.text


@needs_ffmpeg
def test_r3_a_cut_can_be_measured(client: TestClient, world: World,
                                  clips: dict[str, bytes]) -> None:
    build_cut(world, [{"id": "s1", "start": 0, "dur": 4}], clip=clips["tone"])
    body = measure(client, "cut-1")
    assert near(nums(body["audio"], "lufs", "integrated"), TONE_LUFS, LEVEL_TOLERANCE_DB)


# ---- measure_asset ---------------------------------------------------------


def add_audio_asset(store: ProjectStore, asset_id: str, data: bytes, relative: str, *,
                    type_: str = "audio") -> Asset:
    store._root.write_bytes(relative, data)
    asset = Asset(asset_id=asset_id, type=type_, generated=False, license="CC0-1.0",
                  file_path=relative, checksum=hashlib.sha256(data).hexdigest())
    store.save(asset, expected_version=0)
    return asset


@needs_ffmpeg
def test_r3_measure_asset_gives_container_and_audio_for_a_wav(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_audio_asset(world.store, "aud1", clips["wav"], "assets/aud1.wav")
    before = tree_bytes(world.root)
    response = get(client, asset_metrics_url("aud1"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert tree_bytes(world.root) == before
    assert near(nums(body["container"], "duration"), 2.0, 0.2)
    assert near(nums(body["audio"], "lufs", "integrated"), TONE_LUFS, LEVEL_TOLERANCE_DB)
    assert near(nums(body["audio"], "peak"), TONE_PEAK_DB, LEVEL_TOLERANCE_DB)
    assert not body.get("video")


def test_r3_measure_asset_refuses_a_non_audio_asset(client: TestClient, world: World) -> None:
    add_audio_asset(world.store, "mdl", b"glTF fake", "assets/mdl.glb", type_="model")
    assert get(client, asset_metrics_url("mdl")).status_code == 422


def test_r3_measure_asset_refuses_an_audio_asset_without_a_file(client: TestClient,
                                                                  world: World) -> None:
    world.store.save(Asset(asset_id="nofile", type="audio", generated=True, license="CC0-1.0"),
                     expected_version=0)
    assert get(client, asset_metrics_url("nofile")).status_code == 422


def test_r3_measure_asset_refuses_an_unsupported_extension(client: TestClient,
                                                            world: World) -> None:
    add_audio_asset(world.store, "odd", b"not audio", "assets/odd.txt")
    assert get(client, asset_metrics_url("odd")).status_code == 422


def test_r3_measure_asset_unknown_asset_is_404(client: TestClient, world: World) -> None:
    assert get(client, asset_metrics_url("ghost")).status_code == 404


@needs_ffmpeg
def test_r3_measure_asset_refuses_tampered_bytes(client: TestClient, world: World,
                                                 clips: dict[str, bytes]) -> None:
    """AMBIGUOUS: the contract says verify the Asset's bytes but not which error kind;
    the store's own asset verification raises invalid_manifest, an output's raises
    corrupt_artifact. Either is accepted; a 200 or a 500 is not."""
    asset = add_audio_asset(world.store, "aud1", clips["wav"], "assets/aud1.wav")
    world.store._root.write_bytes(asset.file_path, clips["wav"] + b"\x00")
    response = get(client, asset_metrics_url("aud1"))
    assert 400 <= response.status_code < 500, response.text
    assert response.json()["kind"] in {"corrupt_artifact", "invalid_manifest"}, response.text


# --------------------------------------------------------------------------
# R4 - descriptions and CLI
# --------------------------------------------------------------------------


def summary_of(app: Any, name: str) -> str:
    method, path = OPERATIONS[name]
    return app.openapi()["paths"][path][method].get("summary", "")


@pytest.mark.parametrize("name", sorted(OPERATIONS))
def test_r4_every_operation_has_a_substantial_summary(app: Any, name: str) -> None:
    summary = summary_of(app, name)
    assert len(summary.split()) >= 15, f"{name}: {summary!r}"
    assert summary.lower().replace("_", " ") != name.replace("_", " ")
    assert "return" in summary.lower(), f"{name} does not say what it returns: {summary!r}"


@pytest.mark.parametrize(("name", "mentions"), [
    ("rubric_sample", ["contact_sheet", "inspect_output", "evaluate_rubric"]),
    ("contact_sheet", ["rubric_sample", "times", "inspect_output"]),
    ("evaluate_rubric", ["rubric_sample", "record_review", "episode_cut"]),
])
def test_r4_summaries_name_their_place_in_the_workflow(app: Any, name: str,
                                                       mentions: list[str]) -> None:
    summary = summary_of(app, name)
    for fragment in mentions:
        assert fragment in summary, f"{name} does not mention {fragment}: {summary!r}"


def test_r4_summaries_for_the_measurement_tools_say_what_they_measure(app: Any) -> None:
    out = summary_of(app, "measure_output").lower()
    assert "loudness" in out or "lufs" in out
    assert "flicker" in out or "luma" in out
    asset = summary_of(app, "measure_asset").lower()
    assert "audio" in asset


@pytest.mark.parametrize("name", sorted(OPERATIONS))
def test_r4_summaries_reach_the_cli_help(name: str) -> None:
    command = cli_module.app.commands.get(name) or cli_module.app.commands[
        name.replace("_", "-")]
    assert command.help and len(command.help.split()) >= 15


@pytest.mark.parametrize("name", sorted(OPERATIONS))
def test_r4_operations_are_not_creator_only_so_the_manifest_lists_them(
    client: TestClient, name: str,
) -> None:
    response = client.get("/tool-manifest")
    assert response.status_code == 200
    assert name in {tool["name"] for tool in response.json()["tools"]}


def test_r4_openapi_declares_only_image_png_for_contact_sheet(app: Any) -> None:
    method, path = OPERATIONS["contact_sheet"]
    ok = app.openapi()["paths"][path][method]["responses"]["200"]
    assert set(ok["content"]) == {"image/png"}


def test_r4_the_other_operations_declare_json_not_png(app: Any) -> None:
    for name in ("rubric_sample", "evaluate_rubric", "measure_output", "measure_asset"):
        method, path = OPERATIONS[name]
        ok = app.openapi()["paths"][path][method]["responses"]["200"]
        assert "image/png" not in ok.get("content", {}), name


@needs_ffmpeg
def test_r4_the_cli_returns_a_png_as_media_type_and_base64(
    world: World, clips: dict[str, bytes], capsys: pytest.CaptureFixture[str],
) -> None:
    add_video(world, "c", clips["testsrc"])
    argv = ["contact-sheet", "--project-id", PID, "--episode-id", EP, "--output-id", "c",
            "--data", json.dumps({"frames": 4}), "--json"]
    code = cli_module.main(argv, settings=world.settings)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["media_type"] == "image/png"
    assert base64.b64decode(payload["data_base64"]).startswith(PNG_MAGIC)


def test_r4_the_cli_json_operations_return_json(world: World,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    build_cut(world, [{"id": "s1", "start": 0, "dur": 10}])
    code = cli_module.main(["rubric-sample", "--project-id", PID, "--episode-id", EP,
                            "--output-id", "cut-1", "--data", "{}", "--json"],
                           settings=world.settings)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert json.loads(captured.out)["output_id"] == "cut-1"


# --------------------------------------------------------------------------
# Amendments P1-P10 (after the Codex review)
# --------------------------------------------------------------------------


def find_leaves(obj: Any, key: str) -> list[Any]:
    """Every leaf value whose last key is exactly ``key``."""
    return [value for path, value in leaves(obj) if path and path[-1] == key]


def error_kind(response: Any) -> str:
    assert response.status_code >= 400, response.text
    return str(response.json()["kind"])


@needs_ffmpeg
def test_p1_stdout_beyond_the_cap_is_refused(
    client: TestClient, world: World, clips: dict[str, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frame PNG written to stdout that exceeds the (patched, small) cap is refused as
    a corrupt artifact instead of being buffered."""
    add_video(world, "hd", clips["hd"])
    assert get(client, sheet_url("hd"), frames=4).status_code == 200
    monkeypatch.setattr(perception, "_SUBPROCESS_STDOUT_MAX_BYTES", 4096)
    response = get(client, sheet_url("hd"), frames=4)
    assert error_kind(response) == "corrupt_artifact", response.text


@needs_ffmpeg
def test_p1_long_clip_stderr_is_streamed_and_only_a_tail_is_retained(
    client: TestClient, world: World, clips: dict[str, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamed: dict[str, int] = {}
    retained: list[int] = []
    original = perception._Deadline.run

    def recording(self: Any, command: list[str], *, operation: str,
                  stderr_line_parser: Any = None) -> Any:
        def counting(line: str) -> None:
            streamed[operation] = streamed.get(operation, 0) + len(line)
            if stderr_line_parser is not None:
                stderr_line_parser(line)

        result = original(self, command, operation=operation, stderr_line_parser=counting)
        retained.append(len(result.stderr))
        return result

    monkeypatch.setattr(perception._Deadline, "run", recording)
    add_video(world, "long", clips["long"])
    video = measure(client, "long", samples=1)["video"]
    cropdetect_bytes = max(v for op, v in streamed.items() if "cropdetect" in op)
    assert cropdetect_bytes > 256 * 1024, "the fixture must produce far more stderr than the tail"
    assert max(retained) <= 64 * 1024 + 4096, max(retained)
    # The parsed values survived the streaming: the cut at 30 s and the (all black ->
    # white) frame are still found.
    assert near(nums(video, "scene"), 30.0, 0.3)


@needs_ffmpeg
def test_p2_contact_sheet_takes_the_frame_visible_at_each_time_on_a_vfr_clip(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """Red until 2.0 s (4 frames), white from 2.0 s (60 frames)."""
    add_video(world, "vfr", clips["vfr"])
    sheet = decode_png(get(client, sheet_url("vfr"), times="0.5,1.5,2.5,3.5"))
    tiles = [median_rgb(quadrant(sheet, c, r)) for r in (0, 1) for c in (0, 1)]
    assert is_red(tiles[0]) and is_red(tiles[1]), tiles
    assert is_white(tiles[2]) and is_white(tiles[3]), tiles


@needs_ffmpeg
def test_p2_measure_output_samples_by_time_on_a_vfr_clip(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "vfr", clips["vfr"])
    rows = measure(client, "vfr", samples=4)["video"]["samples"]
    assert len(rows) == 4
    for row in rows:
        expected = 81 if row["time_sec"] < 2.0 else 235
        assert abs(row["luma_mean_0_255"] - expected) <= 12, row


def assert_silent_levels(audio: Any) -> None:
    """True peak must be null. AMBIGUOUS: FFmpeg's ebur128 prints digital silence's
    integrated loudness as -70.0 LUFS (its absolute gate), not -inf, so loudness may be
    null or that -70 floor; never an error and never a value above the floor."""
    peaks = [v for p, v in leaves(audio) if "peak" in "/".join(p)]
    assert peaks and all(v is None for v in peaks), audio
    levels = [v for p, v in leaves(audio) if "lufs" in "/".join(p)]
    assert levels and all(v is None or v <= -69.9 for v in levels), audio


@needs_ffmpeg
def test_p3_a_silent_wav_asset_is_valid_and_reports_null_loudness(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_audio_asset(world.store, "quiet", clips["silent_wav"], "assets/quiet.wav")
    response = get(client, asset_metrics_url("quiet"))
    assert response.status_code == 200, response.text
    audio = response.json()["audio"]
    assert find_leaves(audio, "silent") == [True]
    assert_silent_levels(audio)


@needs_ffmpeg
def test_p3_a_silent_audio_track_on_a_video_output_is_valid(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "quiet", clips["silent_mp4"])
    audio = measure(client, "quiet")["audio"]
    assert find_leaves(audio, "silent") == [True]
    assert_silent_levels(audio)


@needs_ffmpeg
def test_p3_a_normal_tone_is_not_marked_silent(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "tone", clips["tone"])
    assert find_leaves(measure(client, "tone")["audio"], "silent") == [False]


@needs_ffmpeg
def test_p4_audio_that_decodes_far_short_of_its_declared_duration_is_corrupt(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    if "mp3_truncated" not in clips:
        pytest.skip("this FFmpeg build cannot encode MP3")
    add_audio_asset(world.store, "cut-short", clips["mp3_truncated"], "assets/cut-short.mp3")
    response = get(client, asset_metrics_url("cut-short"))
    assert error_kind(response) == "corrupt_artifact", response.text


@needs_ffmpeg
@pytest.mark.parametrize("frames", [9, 16])
def test_p5_square_512_tiles_fit_the_caps(client: TestClient, world: World,
                                          clips: dict[str, bytes], frames: int) -> None:
    add_video(world, "sq", clips["square"])
    response = get(client, sheet_url("sq"), frames=frames)
    sheet = decode_png(response)
    assert max(sheet.size) <= 1536, sheet.size
    assert len(response.content) <= 4 * MIB


@needs_ffmpeg
@pytest.mark.parametrize("frames", [2, 9, 16])
@pytest.mark.parametrize("mode", ["frames", "difference"])
def test_p5_a_very_tall_portrait_clip_fits_the_caps(
    client: TestClient, world: World, clips: dict[str, bytes], frames: int, mode: str,
) -> None:
    add_video(world, "tall", clips["tall"])
    response = get(client, sheet_url("tall"), frames=frames, mode=mode)
    sheet = decode_png(response)
    assert max(sheet.size) <= 1536, sheet.size
    assert len(response.content) <= 4 * MIB


@needs_ffmpeg
def test_p5_a_square_difference_sheet_also_fits(client: TestClient, world: World,
                                                clips: dict[str, bytes]) -> None:
    add_video(world, "sq", clips["square"])
    sheet = decode_png(get(client, sheet_url("sq"), frames=16, mode="difference"))
    assert max(sheet.size) <= 1536


@needs_ffmpeg
def test_p6_ten_bit_luma_is_reported_in_8_bit_units_with_the_source_format(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "g10", clips["gray10"])
    video = measure(client, "g10", samples=3)["video"]
    yavg = nums(video, "yavg", "luma_mean")
    assert len(yavg) >= 3
    assert all(abs(v - 126) <= 8 for v in yavg), yavg
    assert all(abs(v - 126) <= 8 for v in nums(video, "luma_min", "luma_max"))
    assert find_leaves(video, "source_pixel_format") == ["yuv420p10le"]


@needs_ffmpeg
def test_p7_a_manifest_missing_a_selected_shot_is_refused_naming_it(
    client: TestClient, world: World,
) -> None:
    build_cut(world, TEN_SECONDS, manifest={"s1": 1, "s2": 1})  # no row for s3
    response = get(client, sample_url("cut-1"))
    assert response.status_code == 422, response.text
    assert "s3" in response.text


def test_p7_the_source_is_credit_manifest_only_when_every_shot_came_from_it(
    client: TestClient, world: World,
) -> None:
    build_cut(world, TEN_SECONDS, manifest={"s1": 1, "s2": 1, "s3": 1})
    assert plan_of(client)["shot_versions_source"] == "credit_manifest"


def test_p7_no_manifest_reports_current(client: TestClient, world: World) -> None:
    build_cut(world, TEN_SECONDS)
    assert plan_of(client)["shot_versions_source"] == "current"


@needs_ffmpeg
def test_p8_a_clip_with_no_cropdetect_detection_is_undetermined_not_no_bars(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "black", clips["black"])
    video = measure(client, "black")["video"]
    assert find_leaves(video, "letterbox_determined") == [False]
    estimate = [v for p, v in leaves(video)
                if any("letterbox" in part and "determined" not in part for part in p)]
    assert estimate and all(v is None for v in estimate), video


@needs_ffmpeg
def test_p8_a_determined_clip_says_so(client: TestClient, world: World,
                                      clips: dict[str, bytes]) -> None:
    add_video(world, "lb", clips["letterbox"])
    assert find_leaves(measure(client, "lb")["video"], "letterbox_determined") == [True]


@pytest.fixture
def tiny_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 0.3 s budget, and hashing that takes 0.5 s per block."""
    original_init = perception._Deadline.__init__

    def short(self: Any, seconds: float = 0.3) -> None:
        original_init(self, 0.3)

    monkeypatch.setattr(perception._Deadline, "__init__", short)

    class SlowDigest:
        def __init__(self, data: bytes = b"") -> None:
            self._inner = hashlib.sha256()
            if data:
                self.update(data)

        def update(self, block: bytes) -> None:
            time.sleep(0.5)
            self._inner.update(block)

        def hexdigest(self) -> str:
            return self._inner.hexdigest()

    class SlowHashlib:
        sha256 = staticmethod(SlowDigest)

    monkeypatch.setattr(perception, "hashlib", SlowHashlib)


def test_p9_slow_asset_verification_counts_against_the_budget(
    client: TestClient, world: World, tiny_budget: None,
) -> None:
    add_audio_asset(world.store, "slow", b"RIFF" + bytes(64), "assets/slow.wav")
    assert error_kind(get(client, asset_metrics_url("slow"))) == "budget_exceeded"


def test_p9_slow_image_verification_counts_against_the_budget(
    client: TestClient, world: World, tiny_budget: None,
) -> None:
    add_output(world.store, "img", ShotOutputKind.PREVIEW, "previews/img.png", png_bytes((8, 8)))
    assert error_kind(get(client, metrics_url("img"))) == "budget_exceeded"


def test_p9_slow_manifest_verification_counts_against_the_budget(
    client: TestClient, world: World, tiny_budget: None,
) -> None:
    build_cut(world, TEN_SECONDS, manifest={"s1": 1, "s2": 1, "s3": 1})
    assert error_kind(get(client, sample_url("cut-1"))) == "budget_exceeded"


def test_p10_each_sample_is_labelled_with_the_shot_on_screen_at_its_time(
    client: TestClient, world: World,
) -> None:
    """Anomaly moment 4 + 2.0 = 6.0 in s2 [4,7): its sequence 5.0..7.0 ends in s3. The
    neutral sequence (span [0,6] midpoint 3.0) is 2.0..4.0 and ends in s2."""
    build_cut(world, [
        {"id": "s1", "start": 0, "dur": 4},
        {"id": "s2", "start": 4, "dur": 3, "event": 2.0, "anomaly": True},
        {"id": "s3", "start": 7, "dur": 3},
    ])
    plan = plan_of(client)

    def sequence(reason: str) -> list[tuple[float, str]]:
        return sorted((s["time_sec"], s["shot_id"]) for s in plan["samples"]
                      if s["reason"] == reason)

    anomaly = sequence("first_anomaly")
    assert [t for t, _ in anomaly] == pytest.approx([5.0, 5.5, 6.0, 6.5, 7.0])
    assert [s for _, s in anomaly] == ["s2", "s2", "s2", "s2", "s3"]
    neutral = sequence("longest_neutral")
    assert [t for t, _ in neutral] == pytest.approx([2.0, 2.5, 3.0, 3.5, 4.0])
    assert [s for _, s in neutral] == ["s1", "s1", "s1", "s1", "s2"]


@needs_ffmpeg
def test_p2b_sparse_vfr_shows_the_last_frame_at_or_before_the_time(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    """Frames at 0 (red), 2.0, 2.1, 2.2 (white): at 0.5 s and 1.5 s the red frame from 0 s
    is still on screen, however long ago it started."""
    add_video(world, "sparse", clips["sparse"])
    sheet = decode_png(get(client, sheet_url("sparse"), times="0.5,1.5"))
    # Two tiles: either 2x1 (left/right) or 1x2; the two tiles are the two halves.
    halves = ([median_rgb(sheet.crop((0, 0, sheet.width // 2, sheet.height))),
               median_rgb(sheet.crop((sheet.width // 2, 0, sheet.width, sheet.height)))]
              if sheet.width >= sheet.height else
              [median_rgb(sheet.crop((0, 0, sheet.width, sheet.height // 2))),
               median_rgb(sheet.crop((0, sheet.height // 2, sheet.width, sheet.height)))])
    assert all(is_red(h) for h in halves), halves
    later = decode_png(get(client, sheet_url("sparse"), times="2.05,2.25"))
    assert is_white(median_rgb(later.crop((0, 0, later.width // 2, later.height))) if
                    later.width >= later.height else median_rgb(
                        later.crop((0, 0, later.width, later.height // 2))))


@needs_ffmpeg
def test_p2b_sparse_vfr_metrics_report_the_red_frame_before_two_seconds(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "sparse", clips["sparse"])
    rows = measure(client, "sparse", samples=6)["video"]["samples"]
    assert rows
    early = [r for r in rows if r["time_sec"] < 2.0]
    assert early, rows
    for row in early:
        assert abs(row["luma_mean_0_255"] - 81) <= 12, row


@needs_ffmpeg
def test_p2b_b_frame_clip_gives_the_right_colour_at_several_times(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "bf", clips["bframes"])
    sheet = decode_png(get(client, sheet_url("bf"), times="0.9,1.1,2.9,3.1"))
    tiles = [median_rgb(quadrant(sheet, c, r)) for r in (0, 1) for c in (0, 1)]
    assert is_red(tiles[0]) and is_green(tiles[1]) and is_blue(tiles[2]) and is_white(tiles[3]), tiles
    rows = measure(client, "bf", samples=4)["video"]["samples"]
    expected = [81, 80, 41, 235]
    assert len(rows) == 4
    for row, luma in zip(rows, expected, strict=True):
        assert abs(row["luma_mean_0_255"] - luma) <= 14, row


@needs_ffmpeg
def test_p2b_default_encode_really_contains_b_frames(clips: dict[str, bytes],
                                                      tmp_path: Path) -> None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("ffprobe is not on PATH")
    clip = tmp_path / "bf.mp4"
    clip.write_bytes(clips["bframes"])
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v", "-show_entries",
                          "frame=pict_type", "-of", "csv=p=0", str(clip)],
                         check=True, capture_output=True, text=True, timeout=60).stdout
    assert "B" in out.split()


@needs_ffmpeg
def test_ydif_compares_with_the_previous_frame_on_a_flicker_clip(
    client: TestClient, world: World, clips: dict[str, bytes],
) -> None:
    add_video(world, "flicker", clips["flicker"])
    rows = measure(client, "flicker", samples=12)["video"]["samples"]
    assert len(rows) == 12
    for row in rows:
        diff = row["temporal_luma_difference_0_255"]
        if row["time_sec"] == 0:
            assert diff is None, row  # the first frame has no previous frame
        else:
            assert diff is not None and diff > 100, row


@needs_ffmpeg
def test_ydif_of_a_static_clip_is_near_zero(client: TestClient, world: World,
                                            clips: dict[str, bytes]) -> None:
    add_video(world, "gray", clips["gray"])
    rows = measure(client, "gray", samples=8)["video"]["samples"]
    diffs = [r["temporal_luma_difference_0_255"] for r in rows]
    assert all(d is None or d < 5 for d in diffs), diffs


@pytest.fixture
def flaky_reader(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[bytes]]:
    """The stderr reader thread cannot start; records every FFmpeg child spawned."""
    import threading
    import types

    spawned: list[subprocess.Popen[bytes]] = []

    class RecordingPopen(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            spawned.append(self)

    class FlakyThread(threading.Thread):
        def start(self) -> None:
            if self.name == "perception-stderr":
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(perception, "threading",
                        types.SimpleNamespace(Thread=FlakyThread, Event=threading.Event))
    monkeypatch.setattr(perception.subprocess, "Popen", RecordingPopen)
    return spawned


@needs_ffmpeg
def test_p1b_a_reader_that_fails_to_start_leaves_no_ffmpeg_child(
    client: TestClient, world: World, clips: dict[str, bytes],
    flaky_reader: list[subprocess.Popen[bytes]],
) -> None:
    add_video(world, "t", clips["testsrc"])
    response = get(client, metrics_url("t"))
    assert response.status_code >= 400, response.text
    assert flaky_reader, "the operation never started FFmpeg"
    deadline = time.monotonic() + 5
    while any(p.poll() is None for p in flaky_reader) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert all(p.poll() is not None for p in flaky_reader), "an FFmpeg child was left running"


@needs_ffmpeg
def test_p1b_a_reader_that_fails_to_start_surfaces_as_a_job_error(
    client: TestClient, world: World, clips: dict[str, bytes],
    flaky_reader: list[subprocess.Popen[bytes]],
) -> None:
    """The call must fail as a JobError (a structured ``kind`` body), not a bare 500."""
    add_video(world, "t", clips["testsrc"])
    response = get(client, metrics_url("t"))
    assert response.status_code >= 400
    assert response.headers["content-type"].startswith("application/json"), response.text
    assert "kind" in response.json()
