"""Contract tests for mixed-profile episode assembly.

The service composes a timeline with ``compose_timeline(store, *, episode_id)``
and persists/assembles it through ``assemble_episode(store, encoder, *, episode_id)``.

* The timeline consumes each shot's current ``DEGRADED`` ShotOutput. Its source
  format is the PostProfile named by ``Shot.post_profile`` (or the project's
  output profile when that field is unset), and the artifact's encoded SAR agrees
  with that profile.
* The returned episode receipt exposes ``relative_path`` and ``content_sha256``;
  this test deliberately does not require it to be a per-shot ShotOutput.
* The chosen policy is one CFR output at the project's output profile, with all
  input clips conformed and re-encoded in one FFmpeg output pass, including a clip
  whose complete input spec already matches delivery.

The fake encoder runs the production command builder and creates a deterministic
output file. No FFmpeg binary, GPU, renderer, or network is used.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from orchestrator.adapters.blender.scene import RenderRequest, build_scene_plan, synthetic_interior
from orchestrator.adapters.postprofile.profile import PostProfileSpec
from orchestrator.adapters.timeline.runner import (
    MediaInfo,
    TimelineAssemblyResult,
    build_assembly_command,
)
from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import (
    Beat,
    CameraProfile,
    Episode,
    PostProfile,
    PostProfileEncode,
    Project,
    Review,
    Script,
    Shot,
    ShotOutput,
    ShotOutputKind,
    Timeline,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.service.approval import approve_shot
from orchestrator.service.dependencies import propose_shots
from orchestrator.service.timeline import compose_timeline
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict


@pytest.fixture
def tmp_path() -> Path:
    """Use contained, task-local scratch roots instead of the inaccessible system temp."""
    base = Path.cwd() / "_work" / "timeline-assembly-tests" / uuid.uuid4().hex
    ProjectRoot(Path.cwd()).make_dir(base.relative_to(Path.cwd()).as_posix())
    return base

_EPISODE_ID = "episode-1"
_OUTPUT_BYTES = b"fake assembled episode bytes"
_LOWLIGHT_SHOT = "shot-lowlight"
_SCRIPT_ORDER = (_LOWLIGHT_SHOT, "shot-dvr", "shot-consumer")


class _FakeEncoder:
    """Exercise the real command builder and return a deterministic cut artifact."""

    def __init__(self, store: ProjectStore, root: ProjectRoot) -> None:
        self.store = store
        self.root = root
        self.commands: list[list[str]] = []
        self.timelines_at_encode: list[Timeline | None] = []

    def run(
        self,
        *,
        timeline: Timeline,
        shot_outputs: dict[str, ShotOutput],
        profile: PostProfileSpec,
        output_relative: str,
        log_relative: str,
        font_relative: str | None = None,
        budget_seconds: float | None = None,
    ) -> TimelineAssemblyResult:
        del font_relative, budget_seconds
        try:
            persisted = self.store.load(Timeline, _EPISODE_ID)
        except RecordNotFound:
            persisted = None
        self.timelines_at_encode.append(persisted)

        media_info = {
            "shot-dvr": MediaInfo(640, 480, Fraction(1), Fraction(12), Fraction(3)),
            _LOWLIGHT_SHOT: MediaInfo(
                352, 288, Fraction(12, 11), Fraction(10), Fraction(3),
            ),
            "shot-consumer": MediaInfo(640, 480, Fraction(1), Fraction(30), Fraction(3)),
        }
        source_paths = {
            shot_id: self.root.resolve(receipt.relative_path)
            for shot_id, receipt in shot_outputs.items()
        }
        command = build_assembly_command(
            Path("ffmpeg"),
            timeline=timeline,
            shot_outputs=shot_outputs,
            media_info=media_info,
            source_paths=source_paths,
            profile=profile,
            output=self.root.resolve(output_relative),
        )
        self.commands.append(command)
        self.root.write_bytes(output_relative, _OUTPUT_BYTES)
        return TimelineAssemblyResult(
            relative_path=output_relative,
            log_path=log_relative,
            content_sha256=hashlib.sha256(_OUTPUT_BYTES).hexdigest(),
            ffmpeg_identity="fixture-build",
            argv=tuple(command),
        )

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


def _approve_fixture_shot(store: ProjectStore, shot: Shot) -> Shot:
    for status in (
        ShotStatus.PLANNED,
        ShotStatus.BLOCKING,
        ShotStatus.BUILDING,
        ShotStatus.REVIEW,
    ):
        current, token = store.load_with_version(
            Shot, shot.shot_id, episode_id=shot.episode_id,
        )
        changes: dict[str, Any] = {"status": status}
        if status is ShotStatus.BUILDING:
            changes["framing_status"] = FramingStatus.APPROVED
        store.update(current.model_copy(update=changes), expected_version=token)

    review_id = f"review-{shot.shot_id}"
    store.save(
        Review(
            review_id=review_id,
            target_id=shot.shot_id,
            target_version=shot.version,
            created_at=datetime.now(UTC),
            approval_state=ShotStatus.APPROVED,
        ),
        expected_version=0,
    )
    _, token = store.load_with_version(Shot, shot.shot_id, episode_id=shot.episode_id)
    return approve_shot(
        store,
        shot.shot_id,
        episode_id=shot.episode_id,
        review_id=review_id,
        expected_version=token,
    )


def _fixture(
    root_path: Path,
    *,
    delivery_profile: PostProfile | None = None,
    omit_output_for: str | None = None,
    legacy_output_for: str | None = None,
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
        shot = _approve_fixture_shot(store, shot)

        # Every timeline fixture shot owns a real persisted, version-matched scene plan.
        # This pure Python path exercises composition provenance without Blender/GPU work.
        render_request = RenderRequest(
            shot_id=shot.shot_id,
            shot_version=shot.version,
            run_id=f"timeline-scene-{shot_id}",
            degrade_seed=shot.degrade_seed,
            duration_sec=shot.duration_sec,
            camera_profile=CameraProfile(camera_id=f"camera-{shot_id}", profile_name="fixture"),
            scene=synthetic_interior(),
        )
        scene_relative = f"scenes/{shot_id}/v{shot.version:04d}/timeline-fixture/scene.json"
        scene_bytes = json.dumps(
            {"schema_version": 1, "plan": build_scene_plan(render_request)},
            sort_keys=True,
        ).encode("utf-8")
        root.write_bytes(scene_relative, scene_bytes)
        store.append_shot_output(
            ShotOutput(
                output_id=f"scene-{shot_id}",
                episode_id=_EPISODE_ID,
                shot_id=shot_id,
                record_version=shot.version,
                kind=ShotOutputKind.SCENE,
                relative_path=scene_relative,
                content_sha256=hashlib.sha256(scene_bytes).hexdigest(),
            ),
            expected_version=0,
        )
        if shot_id == omit_output_for:
            continue

        relative_path = f"delivered/{shot_id}.mp4"
        clip_bytes = f"delivered clip for {shot_id}".encode()
        root.write_bytes(relative_path, clip_bytes)
        output_timing = {} if shot_id == legacy_output_for else {
            "frame_count": int(shot.duration_sec * profiles[profile_name].output_fps),
            "output_fps": profiles[profile_name].output_fps,
        }
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
                **output_timing,
            ),
            expected_version=1,
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
        "encoder": _FakeEncoder(store, root),
        "delivery": delivery,
    }


def _assemble(fixture: dict[str, Any]) -> Any:
    from orchestrator.service.timeline import assemble_episode, compose_timeline

    timeline = compose_timeline(fixture["store"], episode_id=_EPISODE_ID)
    return assemble_episode(
        fixture["store"],
        fixture["encoder"],
        episode_id=_EPISODE_ID,
        timeline=timeline,
    )


def _encode_commands(encoder: _FakeEncoder) -> list[list[str]]:
    return [command for command in encoder.commands if encoder._is_encode(command)]


def _graphs(commands: list[list[str]]) -> str:
    values: list[str] = []
    filter_options = {"-filter_complex", "-vf", "-filter:v"}
    for command in commands:
        for index, option in enumerate(command[:-1]):
            if option in filter_options:
                values.append(command[index + 1])
    return "\n".join(values).lower()


def test_proposed_shots_persist_episode_order_for_timeline_composition(
    tmp_path: Path,
) -> None:
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    store.save(
        Episode(
            episode_id=_EPISODE_ID,
            project_id="project-1",
            number=1,
            duration_target_sec=9,
        ),
        expected_version=0,
    )

    proposed_ids = ("shot-proposed-first", "shot-proposed-second", "shot-proposed-third")
    first_proposal = [
        Shot(shot_id=shot_id, episode_id=_EPISODE_ID, duration_sec=3)
        for shot_id in proposed_ids[:2]
    ]
    later_proposal = [
        Shot(shot_id=proposed_ids[2], episode_id=_EPISODE_ID, duration_sec=3)
    ]
    propose_shots(store, first_proposal)
    propose_shots(store, later_proposal)

    for shot_id in proposed_ids:
        shot = _approve_fixture_shot(store, store.load(Shot, shot_id, episode_id=_EPISODE_ID))
        relative_path = f"delivered/{shot_id}.mp4"
        clip_bytes = f"delivered clip for {shot_id}".encode()
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
                frame_count=36,
                output_fps=12,
            ),
            expected_version=0,
        )

    timeline = compose_timeline(store, episode_id=_EPISODE_ID)
    assert tuple(entry.ref for entry in timeline.entries) == proposed_ids

    episode = store.load(Episode, _EPISODE_ID)
    assert tuple(episode.shot_ids) == proposed_ids

    with pytest.raises(VersionConflict):
        propose_shots(
            store,
            [Shot(shot_id=proposed_ids[0], episode_id=_EPISODE_ID, duration_sec=3)],
        )
    assert tuple(store.load(Episode, _EPISODE_ID).shot_ids) == proposed_ids


def test_mixed_profiles_conform_cif_by_display_aspect_ratio(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _assemble(fixture)
    commands = _encode_commands(fixture["encoder"])
    graph = _graphs(commands)

    source_display_ar = Fraction(352 * 12, 288 * 11)
    delivery_display_ar = Fraction(640 * 1, 480 * 1)
    source_storage_ar = Fraction(352, 288)
    assert source_display_ar == delivery_display_ar == Fraction(4, 3), (
        "the CIF source is 352x288 storage with SAR 12:11, so its display ratio is "
        f"4:3; observed source={source_display_ar}, delivery={delivery_display_ar}"
    )
    assert source_storage_ar != source_display_ar, (
        "the fixture must distinguish storage aspect ratio from display aspect ratio"
    )
    assert len(commands) == 1, (
        f"expected one final encode for the mixed episode, observed {len(commands)}: "
        f"{commands!r}"
    )
    command = commands[0]
    inputs = [command[index + 1] for index, part in enumerate(command[:-1]) if part == "-i"]
    cif_path = str(fixture["root"].resolve(f"delivered/{_LOWLIGHT_SHOT}.mp4"))
    cif_input_index = inputs.index(cif_path)
    cif_branch = next(
        branch for branch in graph.split(";")
        if branch.startswith(f"[{cif_input_index}:v:0]")
    )
    assert re.search(r"scale\s*=\s*640\s*:\s*480", cif_branch), (
        "the 4:3 CIF source should fill the 640x480 delivery frame after SAR-aware "
        f"conforming; observed CIF branch {cif_branch!r}"
    )
    assert "pad=640:480" not in cif_branch, (
        "a CIF source with 4:3 display AR must not be letterboxed based on its "
        f"11:9 storage dimensions; observed CIF branch {cif_branch!r}"
    )
    assert "setsar=1/1" in cif_branch or re.search(r"setsar\s*=\s*1(?:[,;\]])", cif_branch), (
        "the final square-pixel delivery must carry SAR 1:1; observed filter graph "
        f"{cif_branch!r}"
    )


def test_conforming_filter_graph_does_not_sharpen_or_denoise(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _assemble(fixture)
    graph = _graphs(_encode_commands(fixture["encoder"]))
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

    observed = fixture["encoder"].timelines_at_encode
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


def test_legacy_delivered_receipt_without_timing_fails_with_rerun_remediation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, legacy_output_for="shot-dvr")

    with pytest.raises(JobError) as caught:
        compose_timeline(fixture["store"], episode_id=_EPISODE_ID)

    message = str(caught.value)
    assert "Shot 'shot-dvr'" in message
    assert "duration" in message or "timing" in message
    assert "run degrade_media again" in message


def test_delivery_spec_comes_from_projects_output_profile(tmp_path: Path) -> None:
    custom_delivery = _profile(
        "custom-delivery", resolution=(800, 600), fps=24, sar="1/1",
    )
    fixture = _fixture(tmp_path, delivery_profile=custom_delivery)
    _assemble(fixture)
    commands = _encode_commands(fixture["encoder"])
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
    commands = _encode_commands(fixture["encoder"])
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
