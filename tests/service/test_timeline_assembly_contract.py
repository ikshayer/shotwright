"""Contract tests for mixed-profile episode assembly.

API assumptions (the timeline contract leaves these names open):

* ``orchestrator.service.timeline.assemble_timeline(store, *, episode_id,
  control)`` assembles synchronously. ``control`` implements the existing worker
  supervisor shape: ``stopped()``, ``launch(argv, **kwargs)`` and
  ``clear_process(process)``.
* The timeline consumes each shot's current ``DEGRADED`` ShotOutput. Its source
  format is the PostProfile named by ``Shot.post_profile`` (or the project's
  output profile when that field is unset), and the artifact's encoded SAR agrees
  with that profile.
* The returned episode receipt exposes ``relative_path`` and ``content_sha256``;
  this test deliberately does not require it to be a per-shot ShotOutput.
* The chosen policy is one CFR output at the project's output profile, with all
  input clips conformed and re-encoded in one FFmpeg output pass, including a clip
  whose complete input spec already matches delivery.

The fake supervisor captures composed argv and creates a deterministic output
file. No FFmpeg binary, GPU, renderer, or network is used.
"""

from __future__ import annotations

import hashlib
import io
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from orchestrator.domain.entities import (
    Beat,
    Episode,
    PostProfile,
    PostProfileEncode,
    Project,
    Script,
    Shot,
    ShotOutput,
    ShotOutputKind,
    Timeline,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.store.json_store import ProjectStore

_EPISODE_ID = "episode-1"
_OUTPUT_BYTES = b"fake assembled episode bytes"
_LOWLIGHT_SHOT = "shot-lowlight"
_SCRIPT_ORDER = (_LOWLIGHT_SHOT, "shot-dvr", "shot-consumer")


class _CompletedProcess:
    """Small Popen-compatible success result for the supervised FFmpeg call."""

    pid = 4321

    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        return b"", b""

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = -15


class _FakeControl:
    """Capture supervised argv and make the final MP4 exist for receipt hashing."""

    def __init__(self, store: ProjectStore, root: ProjectRoot) -> None:
        self.store = store
        self.root = root
        self.commands: list[list[str]] = []
        self.timelines_at_encode: list[Timeline | None] = []
        self.cleared: list[Any] = []

    def stopped(self) -> bool:
        return False

    def launch(self, argv: list[str], **_kwargs: Any) -> _CompletedProcess:
        command = [str(part) for part in argv]
        self.commands.append(command)
        if self._is_encode(command):
            try:
                timeline = self.store.load(Timeline, _EPISODE_ID)
            except Exception:
                timeline = None
            self.timelines_at_encode.append(timeline)

            output = Path(command[-1])
            if output.suffix.lower() == ".mp4":
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(_OUTPUT_BYTES)
        return _CompletedProcess()

    def clear_process(self, process: Any) -> None:
        self.cleared.append(process)

    @staticmethod
    def _is_encode(command: list[str]) -> bool:
        return "-filter_complex" in command and "-i" in command


def _profile(
    name: str,
    *,
    resolution: tuple[int, int],
    fps: int,
    sar: str,
) -> PostProfile:
    return PostProfile(
        name=name,
        semver="1.0.0",
        output_fps=fps,
        storage_resolution=resolution,
        sample_aspect_ratio=sar,
        encode=PostProfileEncode(
            codec="libx264", preset="fast", crf=24,
            pix_fmt="yuv420p", threads=1,
        ),
    )


def _fixture(
    root_path: Path,
    *,
    delivery_profile: PostProfile | None = None,
    omit_output_for: str | None = None,
) -> dict[str, Any]:
    root = ProjectRoot(root_path)
    store = ProjectStore(root)
    delivery = delivery_profile or _profile(
        "delivery-dvr", resolution=(640, 480), fps=12, sar="1/1",
    )
    store.save(
        Project(project_id="project-1", title="timeline fixture",
                output_profile=delivery.name),
        expected_version=0,
    )
    # The persisted Episode order intentionally differs from Script beat order.
    store.save(
        Episode(
            episode_id=_EPISODE_ID,
            project_id="project-1",
            number=1,
            duration_target_sec=9,
            shot_ids=("shot-consumer", "shot-dvr", _LOWLIGHT_SHOT),
        ),
        expected_version=0,
    )

    profiles = {
        delivery.name: delivery,
        "dvr-2005": _profile(
            "dvr-2005", resolution=(640, 480), fps=12, sar="1/1",
        ),
        "low-light-bw": _profile(
            "low-light-bw", resolution=(352, 288), fps=10, sar="12/11",
        ),
        "consumer-2002": _profile(
            "consumer-2002", resolution=(640, 480), fps=30, sar="1/1",
        ),
    }
    saved_profile_names: set[str] = set()
    for profile in profiles.values():
        # PostProfile is unversioned and cannot be enumerated through store.ids.
        if profile.name not in saved_profile_names:
            store.save(profile, expected_version=0)
            saved_profile_names.add(profile.name)

    shot_rows = (
        ("shot-dvr", "dvr-2005", "beat-dvr"),
        (_LOWLIGHT_SHOT, "low-light-bw", "beat-lowlight"),
        ("shot-consumer", "consumer-2002", "beat-consumer"),
    )
    for shot_id, profile_name, beat_id in shot_rows:
        shot = Shot(
            shot_id=shot_id,
            episode_id=_EPISODE_ID,
            post_profile=profile_name,
            beat_id=beat_id,
            duration_sec=3,
        )
        store.create(shot)
        if shot_id == omit_output_for:
            continue

        relative_path = f"delivered/{shot_id}.mp4"
        clip_bytes = f"delivered clip for {shot_id}".encode("utf-8")
        root.write_bytes(relative_path, clip_bytes)
        store.append_shot_output(
            ShotOutput(
                output_id=f"delivered-{shot_id}",
                episode_id=_EPISODE_ID,
                shot_id=shot_id,
                record_version=shot.version,
                kind=ShotOutputKind.DEGRADED,
                relative_path=relative_path,
                content_sha256=hashlib.sha256(clip_bytes).hexdigest(),
                profile_semver="1.0.0",
                filter_graph_hash="a" * 64,
                pinned_binaries={"ffmpeg": "fixture-build"},
            ),
            expected_version=0,
        )

    ordered_beats = (
        Beat(beat_id="beat-lowlight", target_sec=3, subtlety=1),
        Beat(beat_id="beat-dvr", target_sec=3, subtlety=2),
        Beat(beat_id="beat-consumer", target_sec=3, subtlety=3),
    )
    store.create(Script(
        script_id="script-1",
        episode_id=_EPISODE_ID,
        logline="three camera formats",
        beats=list(ordered_beats),
        beat_to_shot={
            "beat-lowlight": _LOWLIGHT_SHOT,
            "beat-dvr": "shot-dvr",
            "beat-consumer": "shot-consumer",
        },
    ))
    return {
        "root": root,
        "store": store,
        "control": _FakeControl(store, root),
        "delivery": delivery,
    }


def _assemble(fixture: dict[str, Any]) -> Any:
    # Lazy import keeps this test file collectable before the parallel implementation
    # adds the API, so pytest reports individual contract failures rather than a
    # collection error for the whole module.
    from orchestrator.service.timeline import assemble_timeline

    return assemble_timeline(
        fixture["store"],
        episode_id=_EPISODE_ID,
        control=fixture["control"],
    )


def _encode_commands(control: _FakeControl) -> list[list[str]]:
    return [command for command in control.commands if control._is_encode(command)]


def _graphs(commands: list[list[str]]) -> str:
    values: list[str] = []
    filter_options = {"-filter_complex", "-vf", "-filter:v"}
    for command in commands:
        for index, option in enumerate(command[:-1]):
            if option in filter_options:
                values.append(command[index + 1])
    return "\n".join(values).lower()


def test_mixed_profiles_conform_cif_by_display_aspect_ratio(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _assemble(fixture)
    commands = _encode_commands(fixture["control"])
    graph = _graphs(commands)

    source_display_ar = Fraction(352 * 12, 288 * 11)
    delivery_display_ar = Fraction(640 * 1, 480 * 1)
    assert source_display_ar == delivery_display_ar == Fraction(4, 3), (
        "the CIF source is 352x288 storage with SAR 12:11, so its display ratio is "
        f"4:3; observed source={source_display_ar}, delivery={delivery_display_ar}"
    )
    assert len(commands) == 1, (
        f"expected one final encode for the mixed episode, observed {len(commands)}: "
        f"{commands!r}"
    )
    assert "force_original_aspect_ratio=decrease" in graph, (
        "the CIF branch must fit using the input display aspect ratio; observed filter "
        f"graph {graph!r}"
    )
    assert re.search(r"scale\s*=\s*640\s*:\s*480", graph), (
        "the 4:3 CIF source should fill the 640x480 delivery frame after SAR-aware "
        f"conforming; observed filter graph {graph!r}"
    )
    assert "setsar=1/1" in graph or re.search(r"setsar\s*=\s*1(?:[,;\]])", graph), (
        "the final square-pixel delivery must carry SAR 1:1; observed filter graph "
        f"{graph!r}"
    )


def test_conforming_filter_graph_does_not_sharpen_or_denoise(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _assemble(fixture)
    graph = _graphs(_encode_commands(fixture["control"]))
    forbidden = re.compile(
        r"(?:^|[,;\[])\s*(?:unsharp|cas|smartblur|hqdn3d|nlmeans|atadenoise|"
        r"bm3d|vaguedenoiser|removegrain|deband)\s*(?:=|[,;\]])",
        re.IGNORECASE,
    )
    assert forbidden.search(graph) is None, (
        "conforming must preserve the source's coarse camera texture; found a "
        f"sharpening/denoising filter in {graph!r}"
    )


def test_timeline_is_readable_in_script_order_before_encode(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _assemble(fixture)

    observed = fixture["control"].timelines_at_encode
    assert len(observed) == 1 and observed[0] is not None, (
        "the episode Timeline was not readable from project JSON at FFmpeg launch; "
        f"observed {observed!r}"
    )
    timeline = observed[0]
    entry_shots = tuple(entry.ref for entry in timeline.entries if entry.type == "shot")
    assert entry_shots == _SCRIPT_ORDER, (
        "Timeline entries must prefer Script beat order when beat_to_shot supplies it; "
        f"observed {entry_shots!r}"
    )
    persisted = fixture["store"].load(Timeline, _EPISODE_ID)
    assert persisted == timeline, (
        "the persisted Timeline could not be reopened as standalone project data; "
        f"launch observed {timeline!r}, store read {persisted!r}"
    )


def test_shot_without_delivered_output_fails_with_shot_and_remediation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, omit_output_for=_LOWLIGHT_SHOT)

    with pytest.raises(JobError) as caught:
        _assemble(fixture)

    message = str(caught.value)
    assert (
        f"Shot '{_LOWLIGHT_SHOT}' has no degraded output; run degrade_media first"
        in message
    ), (
        "a missing delivered clip must name its shot and the next pipeline action in "
        f"the established 'has no output; run ... first' shape; observed {message!r}"
    )


def test_delivery_spec_comes_from_projects_output_profile(tmp_path: Path) -> None:
    custom_delivery = _profile(
        "custom-delivery", resolution=(800, 600), fps=24, sar="1/1",
    )
    fixture = _fixture(tmp_path, delivery_profile=custom_delivery)
    _assemble(fixture)
    commands = _encode_commands(fixture["control"])
    graph = _graphs(commands)
    command_text = " ".join(" ".join(command) for command in commands)

    assert "scale=800:600" in graph or re.search(r"scale\s*=\s*800\s*:\s*600", graph), (
        "the output geometry must come from Project.output_profile='custom-delivery'; "
        f"observed filter graph {graph!r}"
    )
    assert "fps=24" in graph or re.search(r"(?:^|\s)-r\s+24(?:\s|$)", command_text), (
        "the chosen CFR policy must use custom-delivery.output_fps=24; "
        f"observed argv {command_text!r}"
    )
    assert "setsar=1/1" in graph or re.search(r"setsar\s*=\s*1(?:[,;\]])", graph), (
        "the output sample aspect ratio must come from custom-delivery; "
        f"observed filter graph {graph!r}"
    )


def test_matching_and_nonmatching_inputs_share_the_pinned_reencode_policy(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    _assemble(fixture)
    commands = _encode_commands(fixture["control"])
    command_text = " ".join(" ".join(command) for command in commands)

    assert len(commands) == 1, (
        "the selected policy is one final encode for all conforming inputs, including "
        f"the exact-match dvr shot; observed {len(commands)} encode commands: {commands!r}"
    )
    for shot_id in ("shot-dvr", _LOWLIGHT_SHOT, "shot-consumer"):
        assert shot_id in command_text, (
            f"the common delivery encode omitted input {shot_id!r}; observed "
            f"argv {command_text!r}"
        )
    assert not re.search(r"(?:-c:v|-codec:v)\s+copy(?:\s|$)", command_text), (
        "matching and nonmatching clips are deliberately re-encoded to the same "
        f"delivery policy; observed argv {command_text!r}"
    )


def test_episode_receipt_hashes_the_produced_file(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receipt = _assemble(fixture)
    relative_path = receipt.relative_path
    produced = fixture["root"].read_bytes(relative_path)
    observed_hash = hashlib.sha256(produced).hexdigest()

    assert produced == _OUTPUT_BYTES, (
        "the fake FFmpeg process did not create the file named by the episode receipt; "
        f"observed bytes {produced!r} at {relative_path!r}"
    )
    assert receipt.content_sha256 == observed_hash, (
        "the episode-level receipt must hash the complete produced file; "
        f"observed receipt hash {receipt.content_sha256!r}, actual {observed_hash!r}"
    )
