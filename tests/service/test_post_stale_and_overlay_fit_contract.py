"""Tests for orchestrator/service/POST_STALE_AND_OVERLAY_FIT_CONTRACT.md (step 3e).

Written from the contract, not from the implementation.

A1  the POST_STALE state, its edges, and cut approval blocked by it
A2  which shots a post-only CameraProfile revision marks, and how (journal, gates, scope)
A3  re-degrade of a POST_STALE shot through the real runner and real FFmpeg
A4  a Review recorded before a re-degrade cannot approve the re-degraded output
B1  the text measure agrees with an FFmpeg drawtext render
B2  every in-recording overlay must lie inside the storage frame, for every clock value
B3  refusal at degrade composition (before FFmpeg) and at camera create/revise
B4  the seeded profiles fit with DejaVu Sans Mono

Seams assumed (the contract names none for B1; see the report):

* ``orchestrator.adapters.postprofile.overlay_fit`` exposes one public function whose name
  starts with ``measure`` taking a font path, a font size and a text (parameter names
  containing ``font``, ``size`` and ``text``) and returning ``(width, height)`` or an object
  with ``width``/``height``; and an upper-case module constant whose name contains ``MARGIN``.
* ``ShotStatus.POST_STALE`` and ``orchestrator.domain.state.allowed_targets``.
* Refusals are ``JobError(INVALID_MANIFEST)``.
* The degrade pipeline under test is ``compose_degrade_request`` ->
  ``postprofile_spec_from_camera`` -> ``PostProfileAdapter.degrade`` with an injected ``popen``;
  a refusal must happen before that ``popen`` is ever called.
"""

from __future__ import annotations

import calendar
import hashlib
import importlib
import inspect
import json
import math
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageFont

from orchestrator.adapters.postprofile.runner import PostProfileAdapter
from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import (
    CameraNoiseModel,
    CameraOverlayModel,
    CameraProfile,
    Episode,
    EpisodeOutput,
    PostProfile,
    PostProfileEncode,
    PostProfileOverlay,
    PostProfileOverlayIndicator,
    PostStage,
    Project,
    Review,
    Shot,
    ShotOutput,
    ShotOutputKind,
    Timeline,
    TimelineEntry,
)
from orchestrator.domain.state import (
    ReviewApprovalError,
    TransitionError,
    allowed_targets,
    check_cut_approval,
    check_transition,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings
from orchestrator.service.approval import approve_episode, approve_shot, record_review
from orchestrator.service.compose import compose_degrade_request
from orchestrator.service.dependencies import (
    create_camera_profile,
    create_project,
    resume_invalidations,
    revise_camera_profile,
)
from orchestrator.service.runner import (
    AdapterKind,
    AdapterRegistry,
    _AdapterShim,
    postprofile_spec_from_camera,
)
from orchestrator.store.json_store import ProjectStore
from orchestrator.service.timeline import _shot_output_binding
from tests.service.conftest import bind_cut_receipt

S = ShotStatus
EPISODE = "ep1"
EPOCH = 1131984000  # 2005-11-14 16:00:00 UTC
FONT_RELATIVE = "assets/fonts/overlay.ttf"
_FONT_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fonts"  # DejaVu 2.37, see README
MONO = _FONT_DIR / "DejaVuSansMono.ttf"
SANS = _FONT_DIR / "DejaVuSans.ttf"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="real ffmpeg and ffprobe are required"
)
needs_mono = pytest.mark.skipif(not MONO.is_file(), reason="DejaVu Sans Mono is absent")
needs_sans = pytest.mark.skipif(not SANS.is_file(), reason="DejaVu Sans is absent")


# ---------------------------------------------------------------------------------------
# project builders
# ---------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _ffmpeg_pin() -> str:
    if FFMPEG is None:
        return "ffmpeg version fixture"
    done = subprocess.run([FFMPEG, "-hide_banner", "-version"], capture_output=True, check=True)
    return normalize_ffmpeg_identity(done.stdout.decode("utf-8"))


def _stages(width: int, height: int, *, overlay: bool, indicator: bool) -> list[PostStage]:
    stages = [
        PostStage(name="resample", filter="scale",
                  params={"w": width, "h": height, "flags": "bicubic"}),
        PostStage(name="decimate", filter="fps", params={"fps": 12}),
        PostStage(name="restructure", filter="select", params={"from_schedule": "drop+freeze"}),
        PostStage(name="clock_timebase", filter="setpts", params={"from_schedule": "clock"}),
    ]
    if overlay:
        stages.append(PostStage(name="overlay_clock", filter="drawtext",
                                params={"element": "clock"}, composite_stage="in_recording"))
    if indicator:
        stages.append(PostStage(name="overlay_battery_rec", filter="drawtext",
                                params={"element": "battery_rec"},
                                composite_stage="in_recording"))
    stages.append(PostStage(name="output_timebase", filter="setpts",
                            params={"timebase": "1/output_fps", "pts": "N"}))
    return stages


def _populate(
    root: ProjectRoot,
    store: ProjectStore,
    *,
    resolution: tuple[int, int] = (640, 480),
    font: Path | None = MONO,
    overlay: bool = True,
    overlay_x: int | str = 8,
    overlay_y: int | str = "h-24",
    fontsize: int = 16,
    indicator: tuple[int, int | str, int | str] | None = None,
    camera_fields: dict[str, Any] | None = None,
    clock_epoch: int = EPOCH,
    camera_ids: tuple[str, ...] = ("cam-1",),
) -> None:
    """Project, episode, cameras, one shared PostProfile and (optionally) the font."""
    width, height = resolution
    store.save(Project(project_id="p1", title="Post-stale", output_profile="dvr-2005"),
               expected_version=0)
    store.save(Episode(episode_id=EPISODE, project_id="p1", number=1, duration_target_sec=30),
               expected_version=0)
    for camera_id in camera_ids:
        store.create(CameraProfile(
            camera_id=camera_id, profile_name=camera_id, fps=12.0, storage_resolution=resolution,
            display_aspect_ratio=f"{width}:{height}", **(camera_fields or {}),
        ))
    overlay_record = None
    if overlay:
        overlay_record = PostProfileOverlay(
            camera_id="CAM 03", clock_epoch=clock_epoch, fontsize=fontsize,
            font_relative=FONT_RELATIVE, x=overlay_x, y=overlay_y, placement="in_recording",
            indicator=(PostProfileOverlayIndicator(
                fontsize=indicator[0], x=indicator[1], y=indicator[2], placement="in_recording",
            ) if indicator is not None else None),
        )
    store.save(PostProfile(
        name="dvr-2005", semver="0.2.0", pinned_binaries={"ffmpeg": _ffmpeg_pin()},
        stages=_stages(width, height, overlay=overlay, indicator=indicator is not None),
        output_fps=12, storage_resolution=resolution, sample_aspect_ratio="1/1",
        overlay=overlay_record,
        encode=PostProfileEncode(codec="libx264", preset="medium", crf=30,
                                 pix_fmt="yuv420p", threads=1),
        provenance="post-stale contract fixture",
    ), expected_version=0)
    if font is not None:
        root.write_bytes(FONT_RELATIVE, font.read_bytes())


def _token(store: ProjectStore, shot_id: str) -> int:
    return store.load_with_version(Shot, shot_id, episode_id=EPISODE)[1]


def _status(store: ProjectStore, shot_id: str) -> ShotStatus:
    return store.load(Shot, shot_id, episode_id=EPISODE).status


def _move(store: ProjectStore, shot_id: str, status: ShotStatus, **fields: Any) -> None:
    shot, token = store.load_with_version(Shot, shot_id, episode_id=EPISODE)
    store.update(shot.model_copy(update={"status": status, **fields}), expected_version=token)


_FORWARD = {S.DRAFT: S.PLANNED, S.PLANNED: S.BLOCKING, S.BLOCKING: S.BUILDING,
            S.BUILDING: S.REVIEW}


def _advance(store: ProjectStore, shot_id: str, target: ShotStatus) -> None:
    while _status(store, shot_id) is not target:
        _move(store, shot_id, _FORWARD[_status(store, shot_id)],
              framing_status=FramingStatus.APPROVED)


def _legacy_review(store: ProjectStore, review_id: str, shot_id: str, version: int = 1) -> Review:
    review = Review(
        review_id=review_id, target_id=shot_id, target_version=version, target_kind="shot",
        episode_id=EPISODE, actor="creator", created_at=datetime(2026, 11, 22, tzinfo=UTC),
        approval_state=S.APPROVED,
    )
    store.save(review, expected_version=0)
    return review


def _approve(store: ProjectStore, shot_id: str, review_id: str) -> Shot:
    return approve_shot(store, shot_id, episode_id=EPISODE, review_id=review_id,
                        expected_version=_token(store, shot_id))


def _spec_sha(store: ProjectStore, shot_id: str, run_id: str = "hash-run") -> str:
    """The hash the DEGRADE job records: the camera-derived spec, canonical JSON."""
    request = compose_degrade_request(store, shot_id=shot_id, episode_id=EPISODE, run_id=run_id)
    spec = postprofile_spec_from_camera(request.camera_profile, request.profile)
    canonical = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, allow_nan=False,
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _append_output(root: ProjectRoot, store: ProjectStore, shot_id: str, kind: ShotOutputKind,
                   relative: str, payload: bytes, **fields: Any) -> None:
    root.write_bytes(relative, payload)
    existing = store.list_shot_outputs(shot_id, episode_id=EPISODE)
    store.append_shot_output(
        ShotOutput(
            output_id=f"{kind.value}-{shot_id}-{len(existing)}", episode_id=EPISODE,
            shot_id=shot_id, record_version=1, kind=kind, relative_path=relative,
            content_sha256=hashlib.sha256(payload).hexdigest(), **fields,
        ),
        expected_version=len(existing),
    )


def _add_master(root: ProjectRoot, store: ProjectStore, shot_id: str,
                payload: bytes = b"fixture master") -> str:
    relative = f"renders/{shot_id}/v0001/run-render/master.mkv"
    _append_output(root, store, shot_id, ShotOutputKind.MASTER, relative, payload,
                   engine_id="BLENDER_EEVEE", engine_build="5.2.0")
    return relative


def _add_degraded(root: ProjectRoot, store: ProjectStore, shot_id: str,
                  spec_sha: str | None) -> None:
    n = len(store.list_shot_outputs(shot_id, episode_id=EPISODE))
    _append_output(
        root, store, shot_id, ShotOutputKind.DEGRADED,
        f"post/{shot_id}/v0001/seed-{n}/degraded.mp4", f"degraded {n}".encode(),
        profile_semver="0.2.0", filter_graph_hash="a" * 64, postprofile_spec_sha256=spec_sha,
        pinned_binaries={"ffmpeg": "fixture"}, frame_count=24, output_fps=12,
    )


def _add_shot(
    root: ProjectRoot,
    store: ProjectStore,
    shot_id: str,
    status: ShotStatus,
    *,
    camera: str = "cam-1",
    post_profile: str | None = None,
    receipt: str = "derived",
    duration: float = 2.0,
    master_payload: bytes = b"fixture master",
) -> None:
    """A shot at ``status``. receipt: derived | stale | legacy | none."""
    store.create(Shot(shot_id=shot_id, episode_id=EPISODE, camera_id=camera,
                      camera_profile=camera, post_profile=post_profile,
                      duration_sec=duration, degrade_seed=7))
    if status is not S.PLANNED:
        _add_master(root, store, shot_id, master_payload)
    def add_receipt(kind: str) -> None:
        sha = {"derived": lambda: _spec_sha(store, shot_id), "stale": lambda: "0" * 64,
               "legacy": lambda: None}[kind]()
        _add_degraded(root, store, shot_id, sha)

    if receipt != "none" and status is not S.PLANNED:
        # approve_shot re-checks the latest receipt against the spec derived now, so an
        # APPROVED shot is approved over a current receipt and only then given the stale one.
        add_receipt("derived" if status is S.APPROVED else receipt)
    if status is S.APPROVED:
        _advance(store, shot_id, S.REVIEW)
        _legacy_review(store, f"r-{shot_id}", shot_id)
        _approve(store, shot_id, f"r-{shot_id}")
        if receipt in {"stale", "legacy"}:
            add_receipt(receipt)
    else:
        _advance(store, shot_id, status)


def _revise(store: ProjectStore, camera_id: str = "cam-1", **updates: Any) -> tuple[str, ...]:
    before, token = store.load_with_version(CameraProfile, camera_id)
    revised = before.model_copy(update={**before.next_version(), **updates})
    return revise_camera_profile(store, revised, expected_version=token)


def _gate(store: ProjectStore) -> None:
    episode, token = store.load_with_version(Episode, EPISODE)
    store.save(episode.model_copy(update={
        "approved_cut_version": 4, "approved_cut_output_id": "cut-x",
        "approval_review_id": "gate-x",
    }), expected_version=token)


def _gate_cleared(store: ProjectStore) -> bool:
    episode = store.load(Episode, EPISODE)
    return (episode.approved_cut_version, episode.approved_cut_output_id,
            episode.approval_review_id) == (None, None, None)


@pytest.fixture
def lab(tmp_path: Path) -> tuple[ProjectRoot, ProjectStore]:
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    root = ProjectRoot(tmp_path / "project")
    store = ProjectStore(root)
    _populate(root, store, camera_ids=("cam-1", "cam-2"))
    return root, store


# ---------------------------------------------------------------------------------------
# A1: the state and its edges
# ---------------------------------------------------------------------------------------


def test_a1_post_stale_is_a_status_with_the_contract_value() -> None:
    assert S.POST_STALE.value == "post_stale"


def test_a1_new_edges_exist_and_no_others() -> None:
    assert allowed_targets(S.APPROVED) == frozenset({S.STALE, S.POST_STALE})
    assert allowed_targets(S.REVIEW) == frozenset({S.APPROVED, S.REJECTED, S.POST_STALE})
    assert allowed_targets(S.POST_STALE) == frozenset({S.BUILDING, S.STALE})
    # Fix round 3: a DEGRADE that completes against an obsolete spec goes straight from
    # building to post_stale in one persisted transition.
    assert allowed_targets(S.BUILDING) == frozenset({S.REVIEW, S.FAILED, S.POST_STALE})
    for source in S:
        if source not in {S.APPROVED, S.REVIEW, S.BUILDING}:
            assert S.POST_STALE not in allowed_targets(source), (
                f"{source.value} must not lead to post_stale"
            )


@pytest.mark.parametrize("target", [t for t in S if t not in {S.BUILDING, S.STALE}])
def test_a1_post_stale_cannot_reach_anything_but_building_or_stale(target: ShotStatus) -> None:
    with pytest.raises(TransitionError):
        check_transition(S.POST_STALE, target)


def test_a1_post_stale_to_stale_and_building_and_entry_edges_are_legal() -> None:
    for source, target in ((S.APPROVED, S.POST_STALE), (S.REVIEW, S.POST_STALE),
                           (S.POST_STALE, S.BUILDING), (S.POST_STALE, S.STALE)):
        check_transition(source, target)


def test_a1_cut_approval_is_blocked_by_a_post_stale_shot() -> None:
    shot = Shot(shot_id="s1", episode_id=EPISODE, duration_sec=1, status=S.POST_STALE)
    with pytest.raises(ReviewApprovalError) as caught:
        check_cut_approval(pending=False, shots=[shot])
    assert "s1" in str(caught.value) and "post_stale" in str(caught.value)


def test_a1_approve_episode_refuses_while_a_shot_is_post_stale(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s1", S.REVIEW)
    _move(store, "s1", S.POST_STALE)
    review_id = _make_cut(root, store)
    before = store.load_with_version(Episode, EPISODE)
    with pytest.raises(JobError) as caught:
        approve_episode(store, EPISODE, cut_output_id="cut-1", review_id=review_id,
                        expected_version=before[1])
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load_with_version(Episode, EPISODE) == before


# ---------------------------------------------------------------------------------------
# A2: which shots are marked
# ---------------------------------------------------------------------------------------


def test_a2_post_only_revision_marks_approved_and_review_shots_post_stale(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s-approved", S.APPROVED)
    _add_shot(root, store, "s-review", S.REVIEW)
    _add_shot(root, store, "s-building", S.BUILDING)
    _add_shot(root, store, "s-planned", S.PLANNED)
    _add_shot(root, store, "s-other", S.APPROVED, camera="cam-2")
    _gate(store)
    assert store.load(Shot, "s-approved", episode_id=EPISODE).approval_review_id == "r-s-approved"

    marked = _revise(store, bitrate_kbps=900)

    assert set(marked) == {"s-approved", "s-review"} and len(marked) == 2
    assert _status(store, "s-approved") is S.POST_STALE
    assert _status(store, "s-review") is S.POST_STALE
    assert store.load(Shot, "s-approved", episode_id=EPISODE).approval_review_id is None
    assert store.load(Shot, "s-approved", episode_id=EPISODE).version == 1
    assert _gate_cleared(store)
    assert _status(store, "s-building") is S.BUILDING
    assert _status(store, "s-planned") is S.PLANNED
    assert _status(store, "s-other") is S.APPROVED
    assert store.cascades(pending_only=True) == ()


def test_a2_post_only_revision_that_leaves_the_derived_spec_identical_marks_nothing(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s-approved", S.APPROVED)
    _add_shot(root, store, "s-review", S.REVIEW)
    _gate(store)
    # luma = chroma = 0 keeps the noise model inactive, so the derived spec cannot change.
    marked = _revise(store, noise_model=CameraNoiseModel(luma=0, chroma=0, temporal=False))
    assert marked == ()
    assert _status(store, "s-approved") is S.APPROVED
    assert _status(store, "s-review") is S.REVIEW
    assert not _gate_cleared(store)


def test_a2_receipt_without_a_spec_hash_counts_as_differing(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s-legacy", S.REVIEW, receipt="legacy")
    # Even a change that cannot alter the spec differs from "no recorded hash".
    marked = _revise(store, noise_model=CameraNoiseModel(luma=0, chroma=0, temporal=False))
    assert marked == ("s-legacy",)
    assert _status(store, "s-legacy") is S.POST_STALE


def test_a2_a_shot_without_a_degraded_output_is_not_marked(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s-nodegrade", S.REVIEW, receipt="none")
    assert _revise(store, bitrate_kbps=900) == ()
    assert _status(store, "s-nodegrade") is S.REVIEW


def test_a2_the_latest_degraded_output_is_the_one_compared(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s1", S.REVIEW, receipt="stale")
    _add_degraded(root, store, "s1", _spec_sha(store, "s1"))  # newest receipt is current
    assert _revise(store, noise_model=CameraNoiseModel(luma=0, chroma=0, temporal=False)) == ()
    assert _status(store, "s1") is S.REVIEW


def test_a2_spec_hash_does_not_depend_on_run_specific_values(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s1", S.REVIEW)
    assert _spec_sha(store, "s1", "run-one") == _spec_sha(store, "s1", "run-two")


def test_a2_mixed_post_only_and_rebuild_diff_keeps_rebuild_behaviour(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s-approved", S.APPROVED)
    _add_shot(root, store, "s-review", S.REVIEW)
    marked = _revise(store, bitrate_kbps=900, field_of_view_mm=20.0)
    assert marked == ("s-approved",)
    assert _status(store, "s-approved") is S.STALE
    assert _status(store, "s-review") is S.REVIEW


def test_a2_cascade_plan_rows_carry_scope_post(lab: tuple[ProjectRoot, ProjectStore]) -> None:
    root, store = lab
    _add_shot(root, store, "s-approved", S.APPROVED)
    _add_shot(root, store, "s-review", S.REVIEW)
    _add_shot(root, store, "s-building", S.BUILDING)
    _revise(store, bitrate_kbps=900)
    plan, = store.cascades()
    assert {row["shot_id"] for row in plan["affected"]} == {"s-approved", "s-review"}
    assert all(row.get("scope") == "post" for row in plan["affected"])
    assert store.cascades(pending_only=True) == ()


def test_a2_interrupted_post_cascade_is_completed_by_resume_invalidations(
    lab: tuple[ProjectRoot, ProjectStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store = lab
    _add_shot(root, store, "s-approved", S.APPROVED)
    _add_shot(root, store, "s-review", S.REVIEW)
    _gate(store)
    original = store.update
    calls = 0

    def fail_second_post_stale_write(entity: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        if isinstance(entity, Shot) and entity.status is S.POST_STALE:
            calls += 1
            if calls == 2:
                raise OSError("injected failure while marking")
        return original(entity, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "update", fail_second_post_stale_write)
        with pytest.raises(JobError) as caught:
            _revise(store, bitrate_kbps=900)
    assert "resume_invalidations" in str(caught.value)

    reopened = ProjectStore(ProjectRoot(root.path))
    pending, = reopened.cascades(pending_only=True)
    assert {row["shot_id"] for row in pending["affected"]} == {"s-approved", "s-review"}
    assert all(row.get("scope") == "post" for row in pending["affected"])
    assert _gate_cleared(reopened)
    statuses = [_status(reopened, "s-approved"), _status(reopened, "s-review")]
    assert S.POST_STALE in statuses and statuses.count(S.POST_STALE) == 1, statuses

    assert resume_invalidations(reopened) == (pending["cascade_id"],)
    assert resume_invalidations(reopened) == ()
    assert _status(reopened, "s-approved") is S.POST_STALE
    assert _status(reopened, "s-review") is S.POST_STALE
    assert reopened.cascades(pending_only=True) == ()
    assert reopened.versions(CameraProfile, "cam-1") == [1, 2]


# ---------------------------------------------------------------------------------------
# A3: re-degrade through the real runner and real FFmpeg
# ---------------------------------------------------------------------------------------


class _SpyRegistry(AdapterRegistry):
    def __init__(self, adapters: dict[AdapterKind, Any]) -> None:
        super().__init__(adapters)
        self.requested: list[AdapterKind] = []

    def get(self, kind: AdapterKind) -> Any:
        self.requested.append(kind)
        return super().get(kind)


class _StatusSpyShim(_AdapterShim):
    """The real DEGRADE adapter, recording the Shot's status as FFmpeg is about to run."""

    store: ProjectStore
    seen: list[ShotStatus]

    def invoke(self, request: Any, job: Any, *, queue: Any) -> Any:
        self.seen.append(self.store.load(Shot, request.record_id, episode_id=EPISODE).status)
        return super().invoke(request, job, queue=queue)


def _generate_master(path: Path) -> bytes:
    assert FFMPEG is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=s=640x480:r=24:d=2", "-pix_fmt", "yuv420p", "-c:v", "ffv1", "-g", "1",
         str(path)], capture_output=True, check=True,
    )
    return path.read_bytes()


def _post_stale_shot_with_real_master(env: Any) -> _StatusSpyShim:
    _populate(env.root, env.store)
    master = env.root.path / "renders" / "s1" / "v0001" / "run-render" / "master.mkv"
    payload = _generate_master(master)
    env.store.create(Shot(shot_id="s1", episode_id=EPISODE, camera_id="cam-1",
                          camera_profile="cam-1", duration_sec=2.0, degrade_seed=7))
    _add_master(env.root, env.store, "s1", payload)  # same relative path rewrites the file
    _add_degraded(env.root, env.store, "s1", _spec_sha(env.store, "s1"))
    _advance(env.store, "s1", S.REVIEW)
    assert _revise(env.store, bitrate_kbps=900) == ("s1",)
    assert _status(env.store, "s1") is S.POST_STALE
    shim = _StatusSpyShim(AdapterKind.DEGRADE, PostProfileAdapter(env.root, env.settings),
                          env.settings)
    shim.store, shim.seen = env.store, []
    env.runner.adapters = _SpyRegistry({AdapterKind.DEGRADE: shim})
    return shim


def _submit_degrade(env: Any) -> Any:
    job = env.runner.submit("p1", env.request(
        kind=AdapterKind.DEGRADE, record_id="s1", record_version=1, episode_id=EPISODE,
    ))
    env.runner.drain()
    return env.persisted_job(job.job_id)


@needs_ffmpeg
@needs_mono
def test_a3_degrade_media_moves_post_stale_through_building_to_review_without_rendering(
    env: Any,
) -> None:
    shim = _post_stale_shot_with_real_master(env)
    masters_before = env.store.list_shot_outputs(
        "s1", episode_id=EPISODE, kind=ShotOutputKind.MASTER)

    job = _submit_degrade(env)

    assert job.state == "completed", job.model_dump()
    assert shim.seen == [S.BUILDING], "the job must move POST_STALE -> BUILDING before FFmpeg"
    assert _status(env.store, "s1") is S.REVIEW
    degraded = env.store.list_shot_outputs(
        "s1", episode_id=EPISODE, kind=ShotOutputKind.DEGRADED)
    assert len(degraded) == 2
    assert degraded[-1].postprofile_spec_sha256 == _spec_sha(env.store, "s1")
    assert degraded[-1].postprofile_spec_sha256 != degraded[0].postprofile_spec_sha256
    assert env.store.list_shot_outputs(
        "s1", episode_id=EPISODE, kind=ShotOutputKind.MASTER) == masters_before
    assert env.root.resolve(degraded[-1].relative_path).stat().st_size > 0
    assert env.runner.adapters.requested == [AdapterKind.DEGRADE]  # no render job
    assert env.store.load(Shot, "s1", episode_id=EPISODE).version == 1


@needs_ffmpeg
@needs_mono
def test_a3_a_failed_re_degrade_moves_the_shot_to_failed(env: Any) -> None:
    # A missing master is refused before any job is accepted (the Shot then stays
    # POST_STALE), so the failure is injected inside the job, after POST_STALE -> BUILDING.
    shim = _post_stale_shot_with_real_master(env)

    def failing_invoke(request: Any, job: Any, *, queue: Any) -> Any:
        shim.seen.append(env.store.load(Shot, request.record_id, episode_id=EPISODE).status)
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "injected FFmpeg failure")

    shim.invoke = failing_invoke  # type: ignore[method-assign]

    job = _submit_degrade(env)

    assert job.state == "failed", job.model_dump()
    assert shim.seen == [S.BUILDING]
    assert _status(env.store, "s1") is S.FAILED
    assert env.runner.adapters.requested == [AdapterKind.DEGRADE]


@needs_ffmpeg
@needs_mono
def test_a3_a_missing_master_is_refused_before_a_job_and_leaves_the_shot_post_stale(env: Any) -> None:
    _post_stale_shot_with_real_master(env)
    master, = env.store.list_shot_outputs(
        "s1", episode_id=EPISODE, kind=ShotOutputKind.MASTER)
    env.root.resolve(master.relative_path).unlink()

    with pytest.raises(JobError):
        _submit_degrade(env)

    assert _status(env.store, "s1") is S.POST_STALE


# ---------------------------------------------------------------------------------------
# A4: an old approval cannot approve the re-degraded output
# ---------------------------------------------------------------------------------------


def _record(store: ProjectStore, shot_id: str) -> Review:
    return record_review(store, target_kind="shot", target_id=shot_id, target_version=1,
                         episode_id=EPISODE, actor="creator", approval_state=S.APPROVED)


def _post_stale_cycle(store: ProjectStore, shot_id: str, root: ProjectRoot) -> None:
    """A post-only revision, then the re-degrade: a new receipt of the now-derived spec, and
    BUILDING -> REVIEW."""
    assert _revise(store, bitrate_kbps=900 + store.versions(CameraProfile, "cam-1")[-1]) == (
        shot_id,)
    assert _status(store, shot_id) is S.POST_STALE
    _move(store, shot_id, S.BUILDING)
    _add_degraded(root, store, shot_id, _spec_sha(store, shot_id))
    _move(store, shot_id, S.REVIEW)


def test_a4_record_review_stamps_the_shot_lifecycle_token(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s1", S.REVIEW)
    token = _token(store, "s1")
    review = _record(store, "s1")
    assert review.created_revision == token
    assert store.load(Review, review.review_id).created_revision == token


def test_a4_a_review_recorded_before_the_re_degrade_is_refused_and_a_later_one_succeeds(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s1", S.REVIEW)
    old = _record(store, "s1")
    _post_stale_cycle(store, "s1", root)

    with pytest.raises(JobError) as caught:
        _approve(store, "s1", old.review_id)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert _status(store, "s1") is S.REVIEW

    fresh = _record(store, "s1")
    assert fresh.created_revision == _token(store, "s1")
    approved = _approve(store, "s1", fresh.review_id)
    assert approved.status is S.APPROVED and approved.approval_review_id == fresh.review_id


def test_a4_a_review_recorded_while_post_stale_is_also_refused(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s1", S.REVIEW)
    assert _revise(store, bitrate_kbps=900) == ("s1",)
    during = _record(store, "s1")
    _move(store, "s1", S.BUILDING)
    _add_degraded(root, store, "s1", _spec_sha(store, "s1"))
    _move(store, "s1", S.REVIEW)
    with pytest.raises(JobError):
        _approve(store, "s1", during.review_id)


def test_a4_an_approval_does_not_survive_a_post_stale_cycle(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s1", S.REVIEW)
    first = _record(store, "s1")
    _approve(store, "s1", first.review_id)
    _post_stale_cycle(store, "s1", root)
    with pytest.raises(JobError):
        _approve(store, "s1", first.review_id)
    second = _record(store, "s1")
    assert _approve(store, "s1", second.review_id).approval_review_id == second.review_id


def test_a4_a_legacy_review_still_approves_a_shot_that_was_never_post_stale(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s1", S.REVIEW)
    legacy = _legacy_review(store, "legacy", "s1")
    assert legacy.created_revision is None
    assert _approve(store, "s1", "legacy").status is S.APPROVED


def test_a4_a_legacy_review_is_refused_once_the_version_has_been_post_stale(
    lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = lab
    _add_shot(root, store, "s1", S.REVIEW)
    _legacy_review(store, "legacy", "s1")
    _post_stale_cycle(store, "s1", root)
    with pytest.raises(JobError) as caught:
        _approve(store, "s1", "legacy")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


# ---------------------------------------------------------------------------------------
# B: overlay fit
# ---------------------------------------------------------------------------------------


def _fit_module() -> Any:
    try:
        return importlib.import_module("orchestrator.adapters.postprofile.overlay_fit")
    except ModuleNotFoundError:
        pytest.fail("orchestrator.adapters.postprofile.overlay_fit does not exist")


def _margin() -> float:
    module = _fit_module()
    for name in dir(module):
        value = getattr(module, name)
        if (name.isupper() and "MARGIN" in name and isinstance(value, (int, float))
                and not isinstance(value, bool)):
            return float(value)
    pytest.fail("overlay_fit exposes no upper-case *MARGIN* constant")


def _measure(font: Path, fontsize: int, text: str) -> tuple[float, float]:
    module = _fit_module()
    function = next((getattr(module, name) for name in sorted(dir(module))
                     if name.startswith("measure") and callable(getattr(module, name))), None)
    if function is None:
        pytest.fail("overlay_fit exposes no public function named measure*")
    values = {"font": font, "size": fontsize, "text": text}
    kwargs = {name: value for name in inspect.signature(function).parameters
              for key, value in values.items() if key in name.lower()}
    result = function(**kwargs) if len(kwargs) == 3 else function(font, fontsize, text)
    if isinstance(result, (tuple, list)):
        return float(result[0]), float(result[1])
    for width_name, height_name in (("width", "height"), ("w", "h"), ("text_w", "text_h")):
        if hasattr(result, width_name) and hasattr(result, height_name):
            return float(getattr(result, width_name)), float(getattr(result, height_name))
    pytest.fail(f"cannot read a width and height from {result!r}")


def _lit_box(workdir: Path, font: Path, fontsize: int, text: str) -> tuple[int, int]:
    """Render with FFmpeg drawtext, white text on a white box on black; measure the lit box."""
    assert FFMPEG is not None
    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(font, workdir / "font.ttf")
    (workdir / "text.txt").write_text(text, encoding="utf-8")
    width, height = 1400, 160
    raw = subprocess.run(
        [FFMPEG, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c=black:s={width}x{height}:d=1",
         "-vf", "drawtext=fontfile=font.ttf:textfile=text.txt:expansion=none:x=20:y=30:"
                f"fontsize={fontsize}:fontcolor=white:box=1:boxcolor=white",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        cwd=workdir, capture_output=True, check=True,
    ).stdout
    image = Image.frombytes("L", (width, height), raw).point(lambda v: 255 if v > 127 else 0)
    left, top, right, bottom = image.getbbox()
    return right - left, bottom - top


_CALIBRATION_TEXTS = (
    "CAM 07 STAIRWELL  11/14/2005 04:00:00 PM",
    "REC [||||]",
    "CAM 03  Mon Nov 14 16:00:00 2005 gjpqy",
)


@needs_ffmpeg
@pytest.mark.parametrize("font_name", ["mono", "sans"])
@pytest.mark.parametrize("fontsize", [12, 16, 24, 32])
@pytest.mark.parametrize("text", _CALIBRATION_TEXTS)
def test_b1_measured_box_agrees_with_an_ffmpeg_drawtext_render_within_the_margin(
    tmp_path: Path, font_name: str, fontsize: int, text: str,
) -> None:
    font = MONO if font_name == "mono" else SANS
    if not font.is_file():
        pytest.skip(f"{font.name} is absent")
    margin = _margin()
    # Amended 2026-11-23: the measurement renders with the pinned FFmpeg, so it is exact and
    # the margin is 0; agreement is checked against an independent render below.
    assert margin >= 0
    lit_w, lit_h = _lit_box(tmp_path, font, fontsize, text)
    measured_w, measured_h = _measure(font, fontsize, text)
    assert abs(measured_w - lit_w) <= margin, (
        f"{font_name} {fontsize}px {text!r}: measured width {measured_w}, FFmpeg {lit_w}, "
        f"margin {margin}")
    assert abs(measured_h - lit_h) <= margin, (
        f"{font_name} {fontsize}px {text!r}: measured height {measured_h}, FFmpeg {lit_h}, "
        f"margin {margin}")


@dataclass
class _Outcome:
    error: JobError | None
    launches: int

    @property
    def refused(self) -> bool:
        return self.error is not None

    @property
    def message(self) -> str:
        assert self.error is not None
        return str(self.error)


class _FfmpegLaunched(BaseException):
    """Raised by the injected popen: reaching it means every pre-flight check passed."""


class _CountingPopen:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, argv: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise _FfmpegLaunched(str(argv[0]))


def _degrade_pipeline(root: ProjectRoot, store: ProjectStore, shot_id: str = "shot-1") -> _Outcome:
    popen = _CountingPopen()
    try:
        request = compose_degrade_request(store, shot_id=shot_id, episode_id=EPISODE,
                                          run_id="run-fit")
        derived = postprofile_spec_from_camera(request.camera_profile, request.profile)
        request = request.model_copy(update={"profile": derived})
        PostProfileAdapter(root, Settings(project_root=root.path), popen=popen).degrade(request)
    except _FfmpegLaunched:
        return _Outcome(None, popen.calls)
    except JobError as error:
        assert error.kind is JobErrorKind.INVALID_MANIFEST, str(error)
        assert popen.calls == 0, "a refusal must happen before FFmpeg (or ffprobe) is launched"
        return _Outcome(error, popen.calls)
    pytest.fail("the degrade pipeline neither refused nor reached the FFmpeg launch")


def _b_lab(tmp_path: Path, name: str = "lab", **options: Any) -> tuple[ProjectRoot, ProjectStore]:
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    root = ProjectRoot(tmp_path / name)
    store = ProjectStore(root)
    duration = options.pop("duration", 2.0)
    _populate(root, store, **options)
    _add_shot(root, store, "shot-1", S.BUILDING, receipt="none", duration=duration)
    return root, store


def _overlay_model(**fields: Any) -> dict[str, Any]:
    return {"overlay_model": CameraOverlayModel(**fields)}


@needs_mono
def test_b2_a_fitting_overlay_passes_every_check(tmp_path: Path) -> None:
    root, store = _b_lab(tmp_path)
    assert not _degrade_pipeline(root, store).refused


@needs_mono
def test_b2_a_clock_line_that_overflows_the_right_edge_is_refused(tmp_path: Path) -> None:
    root, store = _b_lab(
        tmp_path, resolution=(352, 288),
        camera_fields=_overlay_model(camera_id_text="CAM 07 STAIRWELL",
                                     clock_format="%m/%d/%Y %I:%M:%S %p", fontsize=16),
    )
    outcome = _degrade_pipeline(root, store)
    assert outcome.refused and "clock" in outcome.message.lower()


@needs_mono
@pytest.mark.parametrize("y", ["h-4", 400, "h-text_h+30"])
def test_b2_an_overlay_pushed_below_the_frame_is_refused(tmp_path: Path, y: int | str) -> None:
    root, store = _b_lab(tmp_path, resolution=(352, 288), overlay_y=y)
    outcome = _degrade_pipeline(root, store)
    assert outcome.refused and "clock" in outcome.message.lower()


@needs_mono
@pytest.mark.parametrize("x", [-5, "0-6", "w-text_w+40"])
def test_b2_an_overlay_left_of_or_beyond_the_frame_horizontally_is_refused(
    tmp_path: Path, x: int | str,
) -> None:
    root, store = _b_lab(tmp_path, overlay_x=x)
    assert _degrade_pipeline(root, store).refused


@needs_mono
@pytest.mark.parametrize("position", [{"overlay_y": "h-text_h-8"}, {"overlay_x": "w-text_w-8"},
                                      {"overlay_y": "h-2*text_h"}])
def test_b2_position_expressions_bind_w_h_text_w_and_text_h(
    tmp_path: Path, position: dict[str, str],
) -> None:
    root, store = _b_lab(tmp_path, **position)
    assert not _degrade_pipeline(root, store).refused


@needs_mono
def test_b2_the_battery_rec_indicator_is_checked_too(tmp_path: Path) -> None:
    root, store = _b_lab(tmp_path, indicator=(12, 8, 8))
    assert not _degrade_pipeline(root, store).refused
    for index, position in enumerate(((8, "h-3"), ("w-4", 8))):
        root, store = _b_lab(tmp_path, f"bad-{index}", indicator=(12, *position))
        outcome = _degrade_pipeline(root, store)
        assert outcome.refused and "battery" in outcome.message.lower(), position


def _friday_epoch() -> int:
    return calendar.timegm((2005, 11, 11, 12, 0, 0))  # 2005-11-11 was a Friday


def _weekday_lab(tmp_path: Path, name: str, width: int, **options: Any) -> Any:
    return _b_lab(
        tmp_path, name, resolution=(width, 288), font=SANS, overlay_y=8,
        fontsize=40, clock_epoch=_friday_epoch(), duration=3.5 * 86400,
        camera_fields=_overlay_model(camera_id_text="A", clock_format="%a"), **options,
    )


def _sans_width(text: str) -> float:
    return ImageFont.truetype(str(SANS), 40).getlength(text)


@needs_sans
def test_b2_the_widest_clock_over_the_shots_range_is_what_must_fit(tmp_path: Path) -> None:
    # The epoch shows "Fri", the narrowest weekday; the shot runs through Monday.
    narrow = _sans_width("A  Fri")
    assert _sans_width("A  Mon") - narrow > 25, "fixture assumption: Mon is clearly wider"
    width = int(math.ceil(8 + narrow + 6)) // 2 * 2 + 2
    root, store = _weekday_lab(tmp_path, "overflows-later", width)
    outcome = _degrade_pipeline(root, store)
    assert outcome.refused, "a value the clock displays later in the shot overflows the frame"


@needs_sans
def test_b2_a_frame_wide_enough_for_every_displayable_value_passes(tmp_path: Path) -> None:
    widest = max(_sans_width(f"A  {day}") for day in
                 ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
    width = int(math.ceil(8 + widest + _margin() + 12)) // 2 * 2
    root, store = _weekday_lab(tmp_path, "fits-everything", width)
    assert not _degrade_pipeline(root, store).refused


# ---------------------------------------------------------------------------------------
# B3: refusal at composition, camera create/revise
# ---------------------------------------------------------------------------------------

_REAL_RUN = _overlay_model(camera_id_text="CAM 07 STAIRWELL",
                           clock_format="%m/%d/%Y %I:%M:%S %p", fontsize=16)


@needs_mono
def test_b3_composition_refuses_before_ffmpeg_and_names_everything(tmp_path: Path) -> None:
    root, store = _b_lab(tmp_path, resolution=(352, 288), camera_fields=_REAL_RUN)
    outcome = _degrade_pipeline(root, store)
    assert outcome.refused and outcome.launches == 0
    message = outcome.message
    lower = message.lower()
    assert "clock" in lower
    assert "352" in message and "288" in message
    assert "overlay_model.fontsize" in message and "shorten" in lower and "move" in lower
    # 40 characters of DejaVu Sans Mono at 16 px are 385 px wide.
    margin = _margin()
    assert any(abs(int(number) - 385) <= margin + 3 for number in re.findall(r"\d+", message)), (
        f"the measured width (about 385 px) is not named: {message}")
    # The position is named too: the clock sits at x=8.
    assert re.search(r"\b8\b", message)


@needs_mono
def test_b3_the_named_fontsize_is_the_largest_that_fits(tmp_path: Path) -> None:
    root, store = _b_lab(tmp_path, resolution=(352, 288), camera_fields=_REAL_RUN)
    message = _degrade_pipeline(root, store).message
    found = re.search(r"(?:largest|maximum|biggest|max)[^0-9]{0,100}?(\d+)", message, re.I)
    assert found, f"no largest fitting font size is named: {message}"
    fitting = int(found.group(1))
    assert 6 <= fitting < 16

    def at(size: int) -> bool:
        fields = _overlay_model(camera_id_text="CAM 07 STAIRWELL",
                                clock_format="%m/%d/%Y %I:%M:%S %p", fontsize=size)
        root, store = _b_lab(tmp_path, f"size-{size}", resolution=(352, 288),
                             camera_fields=fields)
        return _degrade_pipeline(root, store).refused

    assert not at(fitting), f"font size {fitting} was named as fitting but is refused"
    assert at(fitting + 1), f"font size {fitting + 1} fits, so {fitting} is not the largest"


@needs_mono
def test_b3_revise_camera_profile_refuses_an_overflowing_overlay_model(
    tmp_path: Path,
) -> None:
    _root, store = _b_lab(tmp_path, resolution=(352, 288))
    before = store.load_with_version(CameraProfile, "cam-1")
    overflow = before[0].model_copy(update={
        **before[0].next_version(), **_REAL_RUN,
    })
    with pytest.raises((JobError, ValueError)) as caught:
        revise_camera_profile(store, overflow, expected_version=before[1])
    if isinstance(caught.value, JobError):
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    message = str(caught.value)
    assert "clock" in message.lower() and "352" in message and "overlay_model.fontsize" in message
    assert store.versions(CameraProfile, "cam-1") == [1]
    assert store.load_with_version(CameraProfile, "cam-1") == before
    assert store.cascades() == ()

    fits = before[0].model_copy(update={
        **before[0].next_version(), **_overlay_model(fontsize=12),
    })
    revise_camera_profile(store, fits, expected_version=before[1])
    assert store.versions(CameraProfile, "cam-1") == [1, 2]


def test_b3_revise_says_the_fit_is_undecidable_when_the_font_is_missing(tmp_path: Path) -> None:
    _root, store = _b_lab(tmp_path, resolution=(352, 288), font=None)
    before = store.load_with_version(CameraProfile, "cam-1")
    overflow = before[0].model_copy(update={**before[0].next_version(), **_REAL_RUN})
    result = revise_camera_profile(store, overflow, expected_version=before[1])
    # Not refused, not silently accepted: the response must say it could not decide.
    assert store.versions(CameraProfile, "cam-1") == [1, 2]
    assert result.overlay_fit["status"] == "undecidable", (
        f"the response did not say the overlay fit is undecidable: {result.overlay_fit!r}")


@needs_mono
def test_b3_create_camera_profile_refuses_an_overflowing_overlay_model(tmp_path: Path) -> None:
    (tmp_path / "create").mkdir(parents=True, exist_ok=True)
    root = ProjectRoot(tmp_path / "create")
    store = ProjectStore(root)
    _populate(root, store, resolution=(352, 288), camera_ids=("cam-1",))
    _add_shot(root, store, "shot-new", S.BUILDING, camera="cam-new", receipt="none")

    def camera(**fields: Any) -> CameraProfile:
        return CameraProfile(camera_id="cam-new", profile_name="new", fps=12.0,
                             storage_resolution=(352, 288), display_aspect_ratio="352:288",
                             **fields)

    with pytest.raises((JobError, ValueError)) as caught:
        create_camera_profile(store, camera(**_REAL_RUN))
    message = str(caught.value)
    assert "clock" in message.lower() and "352" in message and "overlay_model.fontsize" in message
    assert store.versions(CameraProfile, "cam-new") == []
    create_camera_profile(store, camera(**_overlay_model(fontsize=12)))
    assert store.versions(CameraProfile, "cam-new") == [1]


# ---------------------------------------------------------------------------------------
# B4: the seeded profiles
# ---------------------------------------------------------------------------------------


@needs_mono
@pytest.mark.parametrize("profile", ["dvr-2005", "low-light-bw", "consumer-2002"])
def test_b4_each_seeded_profile_fits_with_dejavu_sans_mono(tmp_path: Path, profile: str) -> None:
    (tmp_path / "seeded").mkdir(parents=True, exist_ok=True)
    root = ProjectRoot(tmp_path / "seeded")
    store = ProjectStore(root)
    create_project(store, Project(project_id="p1", title="Seeded"))
    store.save(Episode(episode_id=EPISODE, project_id="p1", number=1, duration_target_sec=30),
               expected_version=0)
    root.write_bytes(FONT_RELATIVE, MONO.read_bytes())
    store.create(Shot(shot_id="shot-1", episode_id=EPISODE, camera_id=profile,
                      camera_profile=profile, post_profile=profile, duration_sec=10.0))
    _add_master(root, store, "shot-1")
    outcome = _degrade_pipeline(root, store)
    assert not outcome.refused, outcome.message if outcome.refused else ""


# ---------------------------------------------------------------------------------------
# Fix round 2: eight confirmed defects found by independent review
#
# Written from the fix brief (ps-fix2-prompt.md), not from the implementation.
#
# Seams assumed:
# * ``overlay_fit.FFMPEG_POPEN`` is the only way the fit check launches FFmpeg, and its
#   ``argv[0]`` is the executable the caller configured.
# * ``revise_camera_profile`` / ``create_camera_profile`` take ``ffmpeg_exe=``; the runner reads
#   ``self.settings.binaries.ffmpeg_exe`` when composing a DEGRADE request.
# * A refused cut approval or degrade submission raises ``JobError(INVALID_MANIFEST)``.
# * ``Settings`` sections are mutable, so ``runner.settings`` may be replaced by a copy.
# ---------------------------------------------------------------------------------------

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@pytest.fixture
def plain_lab(tmp_path: Path) -> tuple[ProjectRoot, ProjectStore]:
    """Two cameras and a PostProfile without overlays: nothing here can launch FFmpeg."""
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    root = ProjectRoot(tmp_path / "project")
    store = ProjectStore(root)
    _populate(root, store, overlay=False, font=None, camera_ids=("cam-1", "cam-2"))
    return root, store


def _make_cut(root: ProjectRoot, store: ProjectStore, output_id: str = "cut-1",
              *, bound: bool = True) -> str:
    """A bound cut receipt over every Shot of the Episode plus an approving Review; returns its id.

    The cut's sidecar binds each Shot's latest DEGRADED output for its current version, as an
    assembly of that moment would have; whether the Shot and output are still current is left
    for approval to decide. ``bound=False`` fabricates a legacy receipt with no sidecar.
    """
    shot_ids = sorted(store.ids(Shot, episode_id=EPISODE))
    timeline = Timeline(episode_id=EPISODE, entries=[
        TimelineEntry(type="shot", ref=shot_id, start_sec=index * 2.0, duration_sec=2.0)
        for index, shot_id in enumerate(shot_ids)
    ])
    for expected in range(3):
        store.save(timeline, expected_version=expected)
    timeline = store.load(Timeline, EPISODE)
    payload = b"cut"
    relative = f"episodes/{EPISODE}/outputs/cuts/{output_id}.mp4"
    root.write_bytes(relative, payload)
    rows = [
        _shot_output_binding(shot_id, store.list_shot_outputs(
            shot_id, episode_id=EPISODE, record_version=store.load(
                Shot, shot_id, episode_id=EPISODE).version,
            kind=ShotOutputKind.DEGRADED)[-1])
        for shot_id in shot_ids
    ]
    receipt = EpisodeOutput(
        output_id=output_id, episode_id=EPISODE, timeline_store_version=3,
        timeline_sha256=timeline.content_sha256, relative_path=relative,
        content_sha256=hashlib.sha256(payload).hexdigest(), delivery_profile="test",
        storage_resolution=(64, 48), output_fps=12, sample_aspect_ratio="1/1",
        ffmpeg_identity="test",
    )
    if bound:
        receipt = bind_cut_receipt(store, receipt, shot_rows=rows)
    store.append_episode_output(receipt, expected_version=len(store.list_episode_outputs(EPISODE)))
    return record_review(
        store, target_kind="episode_cut", target_id=EPISODE, target_version=3,
        episode_id=EPISODE, actor="creator", approval_state=S.APPROVED,
    ).review_id


def _approve_cut(store: ProjectStore, review_id: str, output_id: str = "cut-1") -> Episode:
    _episode, token = store.load_with_version(Episode, EPISODE)
    return approve_episode(store, EPISODE, cut_output_id=output_id, review_id=review_id,
                           expected_version=token)


def _fake_degrade_result(spec_sha: str) -> dict[str, Any]:
    return {"profile_semver": "1.0.0", "filter_graph_hash": "a" * 64,
            "postprofile_spec_sha256": spec_sha, "pinned_binaries": {"ffmpeg": "fake-build"},
            "frame_count": 60, "output_fps": 12}


# --- 1: a post-only revision landing while a DEGRADE job is queued or running -------------


def _run_gated_degrade(env: Any, *, revise_while_running: bool) -> Any:
    """Submit a DEGRADE job, optionally revise the camera while it is held, then drain."""
    _populate(env.root, env.store, overlay=False, font=None)
    _add_shot(env.root, env.store, "s1", S.BUILDING, receipt="none")
    submitted_sha = _spec_sha(env.store, "s1")
    env.adapter.result_value = _fake_degrade_result(submitted_sha)
    entered, gate = threading.Event(), threading.Event()
    real_invoke = env.adapter.invoke

    def gated(request: Any, job: Any, *, queue: Any) -> Any:
        entered.set()
        assert gate.wait(10), "the test never released the held DEGRADE job"
        return real_invoke(request, job, queue=queue)

    env.adapter.invoke = gated
    job = env.runner.submit("p1", env.request(
        kind=AdapterKind.DEGRADE, record_id="s1", record_version=1, episode_id=EPISODE))
    assert entered.wait(10), "the DEGRADE job never reached the adapter"
    if revise_while_running:
        assert _revise(env.store, bitrate_kbps=900) == ()  # a BUILDING shot is not marked yet
        assert _spec_sha(env.store, "s1") != submitted_sha
    gate.set()
    env.runner.drain()
    return env.persisted_job(job.job_id), submitted_sha


def test_fix2_item1_a_post_only_revision_during_a_degrade_job_ends_post_stale(env: Any) -> None:
    job, submitted_sha = _run_gated_degrade(env, revise_while_running=True)
    assert job.state == "completed", job.model_dump()
    degraded = env.store.list_shot_outputs(
        "s1", episode_id=EPISODE, kind=ShotOutputKind.DEGRADED)
    assert [row.postprofile_spec_sha256 for row in degraded] == [submitted_sha], (
        "the output of the job must be recorded, not discarded")
    assert _spec_sha(env.store, "s1") != submitted_sha
    assert _status(env.store, "s1") is S.POST_STALE, "obsolete output must not reach REVIEW"


def test_fix2_item1_control_an_unrevised_degrade_job_still_ends_in_review(env: Any) -> None:
    job, submitted_sha = _run_gated_degrade(env, revise_while_running=False)
    assert job.state == "completed", job.model_dump()
    assert _spec_sha(env.store, "s1") == submitted_sha
    assert _status(env.store, "s1") is S.REVIEW


# --- 2: cut approval and the currency of every Shot's degraded output ---------------------


def test_fix2_item2_pure_cut_guard_rejects_building_and_failed_shots() -> None:
    for status in (S.BUILDING, S.FAILED):
        shot = Shot(shot_id="s1", episode_id=EPISODE, duration_sec=1, status=status)
        with pytest.raises(ReviewApprovalError) as caught:
            check_cut_approval(pending=False, shots=[shot])
        assert "s1" in str(caught.value) and status.value in str(caught.value)


@pytest.mark.parametrize("status", [S.BUILDING, S.FAILED])
def test_fix2_item2_cut_approval_refuses_a_shot_re_degrading_after_post_stale(
    plain_lab: tuple[ProjectRoot, ProjectStore], status: ShotStatus,
) -> None:
    root, store = plain_lab
    _add_shot(root, store, "s1", S.REVIEW)
    assert _revise(store, bitrate_kbps=900) == ("s1",)
    _move(store, "s1", S.BUILDING)
    if status is S.FAILED:
        _move(store, "s1", S.FAILED)
    review_id = _make_cut(root, store)
    before = store.load_with_version(Episode, EPISODE)
    with pytest.raises(JobError) as caught:
        _approve_cut(store, review_id)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "s1" in str(caught.value)
    assert store.load_with_version(Episode, EPISODE) == before
    assert _gate_cleared(store)


@pytest.mark.parametrize("receipt", ["stale", "legacy"])
@pytest.mark.parametrize("status", [S.REVIEW, S.APPROVED])
def test_fix2_item2_cut_approval_refuses_a_degraded_output_that_is_no_longer_current(
    plain_lab: tuple[ProjectRoot, ProjectStore], status: ShotStatus, receipt: str,
) -> None:
    root, store = plain_lab
    _add_shot(root, store, "s1", status, receipt=receipt)
    review_id = _make_cut(root, store)
    with pytest.raises(JobError) as caught:
        _approve_cut(store, review_id)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "s1" in str(caught.value)
    assert _gate_cleared(store)


@pytest.mark.parametrize("status", [S.REVIEW, S.APPROVED])
def test_fix2_item2_control_a_cut_over_current_degraded_outputs_is_approved(
    plain_lab: tuple[ProjectRoot, ProjectStore], status: ShotStatus,
) -> None:
    root, store = plain_lab
    _add_shot(root, store, "s1", status)
    review_id = _make_cut(root, store)
    approved = _approve_cut(store, review_id)
    assert approved.approved_cut_output_id == "cut-1"


def test_fix2_item2_an_old_cut_is_refused_after_the_shot_is_re_degraded(
    plain_lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    # The cut receipt must bind the Shot outputs it used (or the gate must otherwise detect
    # a newer DEGRADED output). Here a re-degrade appends a newer output after the cut.
    root, store = plain_lab
    _add_shot(root, store, "s1", S.APPROVED)
    review_id = _make_cut(root, store)
    _add_degraded(root, store, "s1", _spec_sha(store, "s1"))
    with pytest.raises(JobError) as caught:
        _approve_cut(store, review_id)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert _gate_cleared(store)


# --- 3: every rendered candidate is checked at its own box and position -------------------


@lru_cache(maxsize=1)
def _clock_boxes_at_40() -> dict[str, tuple[int, int]]:
    boxes = {}
    for day in _WEEKDAYS:
        width, height = _measure(SANS, 40, f"A  {day}")
        boxes[day] = (int(width), int(height))
    return boxes


def _even_ceil(value: float) -> int:
    return int(math.ceil(value)) // 2 * 2 + 2


def _clock_lab(
    tmp_path: Path, name: str, width: int, height: int, x: int | str, y: int | str,
    *, fontsize: int = 40, duration: float = 6 * 86400.0,
) -> Any:
    return _b_lab(
        tmp_path, name, resolution=(width, height), font=SANS, overlay_x=x, overlay_y=y,
        fontsize=fontsize, clock_epoch=_friday_epoch(), duration=duration,
        camera_fields=_overlay_model(camera_id_text="A", clock_format="%a"),
    )


# 2005-03-31 23:59:00 UTC: a two-minute %b clock shows "Mar" then "Apr". In DejaVu Sans
# at 40 px "A  Mar" is the wider string (129x29) and "A  Apr" the taller (122x37), so the
# widest candidate alone does not decide vertical fit. (Weekday names have no descenders.)
_MARCH_31_2359 = 1112313540


@needs_ffmpeg
@needs_sans
def test_fix2_item3_a_narrower_but_taller_candidate_clipped_vertically_is_refused(
    tmp_path: Path,
) -> None:
    widest_w, widest_h = (int(v) for v in _measure(SANS, 40, "A  Mar"))
    tallest_w, tallest_h = (int(v) for v in _measure(SANS, 40, "A  Apr"))
    assert widest_w > tallest_w and tallest_h > widest_h, "fixture assumption"
    margin = int(math.ceil(_margin()))
    height = 288
    width = _even_ceil(8 + widest_w + margin + 8)

    def lab(name: str, y: int) -> Any:
        return _b_lab(
            tmp_path, name, resolution=(width, height), font=SANS, overlay_x=8, overlay_y=y,
            fontsize=40, clock_epoch=_MARCH_31_2359, duration=120.0,
            camera_fields=_overlay_model(camera_id_text="A", clock_format="%b"),
        )

    # The widest string just fits vertically; the taller, narrower one is clipped.
    root, store = lab("clipped", height - widest_h - margin)
    outcome = _degrade_pipeline(root, store)
    assert outcome.refused and "clock" in outcome.message.lower()

    root, store = lab("tall-fits", height - tallest_h - margin)
    assert not _degrade_pipeline(root, store).refused


@needs_ffmpeg
@needs_sans
def test_fix2_item3_a_narrower_candidate_with_a_negative_x_is_refused(tmp_path: Path) -> None:
    boxes = _clock_boxes_at_40()
    widest_w = max(w for w, _h in boxes.values())
    narrowest_w = min(w for w, _h in boxes.values())
    assert widest_w - narrowest_w >= 2, "fixture assumption: the weekday strings differ in width"
    margin = int(math.ceil(_margin()))

    # x = 2*text_w - K: the narrowest string starts at x = -2, the widest well inside.
    k_bad = 2 * narrowest_w + 2
    width = _even_ceil(3 * widest_w - k_bad + margin)
    root, store = _clock_lab(tmp_path, "negative-x", width, 288, f"2*text_w-{k_bad}", 8)
    assert _degrade_pipeline(root, store).refused

    # Control: shift K so that the narrowest string starts at x = 2; everything fits.
    k_ok = 2 * narrowest_w - 2 - margin
    width = _even_ceil(3 * widest_w - k_ok + margin)
    root, store = _clock_lab(tmp_path, "positive-x", width, 288, f"2*text_w-{k_ok}", 8)
    assert not _degrade_pipeline(root, store).refused


# --- 4: degrade_media is refused while a cascade is pending -------------------------------


def test_fix2_item4_degrade_submission_is_refused_while_a_cascade_is_pending(
    env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _populate(env.root, env.store, overlay=False, font=None)
    _add_shot(env.root, env.store, "s1", S.REVIEW)
    _add_shot(env.root, env.store, "s2", S.BUILDING, receipt="none")

    def fail_to_complete(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("injected cascade completion failure")

    with monkeypatch.context() as patch:
        patch.setattr(env.store, "complete_cascade", fail_to_complete)
        with pytest.raises(JobError):
            _revise(env.store, bitrate_kbps=900)
    assert env.store.cascades(pending_only=True), "the cascade must be left pending"
    statuses = {sid: _status(env.store, sid) for sid in ("s1", "s2")}

    for shot_id in ("s1", "s2"):
        with pytest.raises(JobError) as caught:
            env.runner.submit("p1", env.request(
                kind=AdapterKind.DEGRADE, record_id=shot_id, record_version=1,
                episode_id=EPISODE))
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
        assert "resume_invalidations" in str(caught.value)
    env.runner.drain()
    assert env.adapter.invocations == 0
    assert {sid: _status(env.store, sid) for sid in ("s1", "s2")} == statuses


# --- 5: the A2 comparison launches no FFmpeg; the fit check uses the configured one -------


class _RecordingPopen:
    """Records ``argv[0]`` of every fit-check launch and runs the real process."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.executables: list[str] = []

    def __call__(self, argv: Any, **kwargs: Any) -> Any:
        self.executables.append(str(argv[0]))
        return self.real(argv, **kwargs)


def test_fix2_item5_the_a2_comparison_launches_no_ffmpeg_and_marks_the_right_shots(
    plain_lab: tuple[ProjectRoot, ProjectStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store = plain_lab
    _add_shot(root, store, "s1", S.REVIEW, camera="cam-1")
    _add_shot(root, store, "s2", S.APPROVED, camera="cam-1")
    _add_shot(root, store, "other", S.REVIEW, camera="cam-2")
    popen = _CountingPopen()
    monkeypatch.setattr(_fit_module(), "FFMPEG_POPEN", popen)
    assert sorted(_revise(store, bitrate_kbps=900)) == ["s1", "s2"]
    assert popen.calls == 0
    assert _status(store, "other") is S.REVIEW


@needs_ffmpeg
@needs_mono
def test_fix2_item5_revise_and_create_camera_profile_fit_with_the_configured_ffmpeg(
    lab: tuple[ProjectRoot, ProjectStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store = lab
    assert FFMPEG is not None
    configured = Path(FFMPEG)
    assert str(configured) != "ffmpeg", "the configured executable must differ from the default"
    _add_shot(root, store, "s1", S.REVIEW)
    _add_shot(root, store, "s-new", S.BUILDING, camera="cam-new", receipt="none")
    recorder = _RecordingPopen(_fit_module().FFMPEG_POPEN)
    monkeypatch.setattr(_fit_module(), "FFMPEG_POPEN", recorder)

    before, token = store.load_with_version(CameraProfile, "cam-1")
    revised = before.model_copy(update={**before.next_version(), "bitrate_kbps": 900})
    assert revise_camera_profile(store, revised, expected_version=token,
                                 ffmpeg_exe=configured) == ("s1",)
    assert recorder.executables, "the revise-time fit check must have launched FFmpeg"
    assert set(recorder.executables) == {str(configured)}

    recorder.executables.clear()
    create_camera_profile(store, CameraProfile(
        camera_id="cam-new", profile_name="new", fps=12.0, storage_resolution=(640, 480),
        display_aspect_ratio="640:480"), ffmpeg_exe=configured)
    assert recorder.executables, "the create-time fit check must have launched FFmpeg"
    assert set(recorder.executables) == {str(configured)}


@needs_ffmpeg
@needs_mono
def test_fix2_item5_degrade_composition_in_the_runner_uses_the_configured_ffmpeg(
    env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert FFMPEG is not None
    configured = Path(FFMPEG)
    assert str(configured) != "ffmpeg"
    _populate(env.root, env.store)
    _add_shot(env.root, env.store, "s1", S.BUILDING, receipt="none")
    env.runner.settings = env.settings.model_copy(update={
        "binaries": env.settings.binaries.model_copy(update={"ffmpeg_exe": configured}),
    })
    recorder = _RecordingPopen(_fit_module().FFMPEG_POPEN)
    monkeypatch.setattr(_fit_module(), "FFMPEG_POPEN", recorder)
    env.runner.submit("p1", env.request(
        kind=AdapterKind.DEGRADE, record_id="s1", record_version=1, episode_id=EPISODE))
    env.runner.drain()
    assert recorder.executables, "composition must run the overlay fit check"
    assert set(recorder.executables) == {str(configured)}


# --- 6: an unchanged script derivation does not rebuild a post-staled shot ----------------


def test_fix2_item6_unchanged_script_derivation_leaves_a_post_staled_shot_alone(
    plain_lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    from orchestrator.domain.entities import Beat, BeatSceneDirection, Location, Script
    from orchestrator.service.script_authoring import derive_script_scene_plan
    from orchestrator.store.json_store import RecordNotFound

    root, store = plain_lab
    beat = Beat(beat_id="b1", target_sec=4, subtlety=2, scene_direction=BeatSceneDirection(
        location_id="room1", camera_profile="cam-1", framing="wide", event_sec=2))
    store.create(Script(
        script_id="script1", episode_id=EPISODE, logline="A quiet room",
        beats=[beat.model_copy(update={"narration_text": "The night clerk notices a shape."})],
        beat_to_shot={"b1": "shot1"}, scenes=[Location(location_id="room1")],
    ))

    def derive() -> dict[str, Any]:
        def token(entity: Any, record_id: str, **kwargs: Any) -> int:
            try:
                return store.load_with_version(entity, record_id, **kwargs)[1]
            except RecordNotFound:  # a missing record is token 0 for a first derivation
                return 0
        return derive_script_scene_plan(
            store, episode_id=EPISODE, script_id="script1",
            expected_version=token(Script, "script1", episode_id=EPISODE),
            expected_episode_version=token(Episode, EPISODE),
            expected_shot_versions={"shot1": token(Shot, "shot1", episode_id=EPISODE)},
            expected_location_versions={"room1": token(Location, "room1")},
        )

    assert derive()["changed"]
    _add_master(root, store, "shot1")
    _add_degraded(root, store, "shot1", _spec_sha(store, "shot1"))
    _advance(store, "shot1", S.REVIEW)
    assert _revise(store, bitrate_kbps=900) == ("shot1",)
    assert store.cascades(pending_only=True) == () and store.cascades()  # completed post cascade
    assert _status(store, "shot1") is S.POST_STALE
    token_before = _token(store, "shot1")

    result = derive()

    assert result["changed"] == [], "an unchanged derivation must not need a rebuild"
    shot = store.load(Shot, "shot1", episode_id=EPISODE)
    assert shot.version == 1 and shot.status is S.POST_STALE
    assert _token(store, "shot1") == token_before


# --- 7: the named font size is the largest that fits, even when fit is not monotonic ------


@needs_ffmpeg
@needs_sans
def test_fix2_item7_the_refusal_names_the_largest_size_that_actually_fits(
    tmp_path: Path,
) -> None:
    # One displayable string ("A  Fri"). With x = 2*text_w - K, small sizes start at x < 0
    # and large ones overflow the right edge, so fit is a window, not a threshold:
    # size 40 fails on the right edge, sizes below ~30 fail on the left edge.
    def width(size: int) -> int:
        return int(_measure(SANS, size, "A  Fri")[0])

    margin = int(math.ceil(_margin()))
    largest_fitting, window_floor = 36, 30
    k = 2 * width(window_floor)
    frame_width = 3 * width(largest_fitting) - k + margin
    if frame_width % 2:
        k += 1
        frame_width -= 1
    assert 3 * width(largest_fitting + 1) - k + margin > frame_width, (
        "fixture assumption: the next size overflows")
    assert 2 * width(largest_fitting) - k >= 0, "fixture assumption: the size fits on the left"

    calls = iter(range(1_000))

    def refused_at(size: int) -> Any:
        root, store = _clock_lab(tmp_path, f"size-{size}-{next(calls)}", frame_width, 288,
                                 f"2*text_w-{k}", 8, fontsize=size, duration=2.0)
        return _degrade_pipeline(root, store)

    outcome = refused_at(40)
    assert outcome.refused, "size 40 must overflow the right edge"
    found = re.search(r"(?:largest|maximum|biggest|max)[^0-9]{0,100}?(\d+)", outcome.message,
                      re.I)
    assert found, f"no largest fitting font size is named: {outcome.message}"
    named = int(found.group(1))
    assert named == largest_fitting, outcome.message
    assert not refused_at(named).refused, f"size {named} was named as fitting but is refused"
    for size in range(named + 1, 97):
        assert refused_at(size).refused, f"size {size} fits, so {named} is not the largest"


# --- 8: a Shot's camera is the one DEGRADE resolves (camera_profile), not camera_id -------


def _split_camera_shot(root: ProjectRoot, store: ProjectStore) -> None:
    """camera_id names cam-1, camera_profile names cam-2; DEGRADE resolves cam-2."""
    store.create(Shot(shot_id="s1", episode_id=EPISODE, camera_id="cam-1",
                      camera_profile="cam-2", duration_sec=2.0, degrade_seed=7))
    _add_master(root, store, "s1")
    _add_degraded(root, store, "s1", _spec_sha(store, "s1"))
    _advance(store, "s1", S.REVIEW)


def test_fix2_item8_revising_the_camera_id_camera_does_not_mark_the_shot(
    plain_lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = plain_lab
    _split_camera_shot(root, store)
    assert _revise(store, "cam-1", bitrate_kbps=900) == ()
    assert _status(store, "s1") is S.REVIEW
    assert _spec_sha(store, "s1") == store.list_shot_outputs(
        "s1", episode_id=EPISODE, kind=ShotOutputKind.DEGRADED)[-1].postprofile_spec_sha256


def test_fix2_item8_revising_the_camera_profile_camera_marks_the_shot(
    plain_lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = plain_lab
    _split_camera_shot(root, store)
    assert _revise(store, "cam-2", bitrate_kbps=900) == ("s1",)
    assert _status(store, "s1") is S.POST_STALE


# ---------------------------------------------------------------------------------------
# Fix round 3: single-step post-stale completion and immutable cut input provenance
# ---------------------------------------------------------------------------------------


def test_fix3_item1_obsolete_degrade_has_no_intermediate_review_snapshot(env: Any) -> None:
    job, _submitted_sha = _run_gated_degrade(env, revise_while_running=True)
    assert job.state == "completed", job.model_dump()
    shot, _token = env.store.load_with_version(Shot, "s1", episode_id=EPISODE)
    directory = env.store._directory_of(shot)
    snapshots = [
        env.store._read_record(Shot, relative)[0]
        for _snapshot_token, relative in env.store._lifecycle_files(directory, shot.version)
    ]
    assert shot.status is S.POST_STALE
    assert snapshots[-1].status is S.POST_STALE
    assert all(snapshot.status is not S.REVIEW for snapshot in snapshots)


def test_fix3_item1_approve_shot_rechecks_the_current_derived_degrade_spec(
    plain_lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    root, store = plain_lab
    _add_shot(root, store, "s1", S.REVIEW, receipt="stale")
    review = _record(store, "s1")

    with pytest.raises(JobError) as caught:
        _approve(store, "s1", review.review_id)

    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "current spec" in str(caught.value)
    assert _status(store, "s1") is S.REVIEW


def _fix3_add_bound_cut(
    root: ProjectRoot,
    store: ProjectStore,
    output_id: str,
) -> tuple[EpisodeOutput, str]:
    from orchestrator.store.json_store import RecordNotFound

    try:
        timeline, timeline_version = store.load_with_version(Timeline, EPISODE)
    except RecordNotFound:
        timeline = Timeline(episode_id=EPISODE, entries=[])
        timeline_version = store.save(timeline, expected_version=0)
    payload = f"bound cut {output_id}".encode("utf-8")
    relative = f"episodes/{EPISODE}/outputs/cuts/{output_id}.mp4"
    input_relative = f"episodes/{EPISODE}/outputs/cuts/{output_id}.inputs.json"
    manifest = {
        "schema_version": 1,
        "episode_id": EPISODE,
        "cut_output_id": output_id,
        "cut_path": relative,
        "cut_sha256": hashlib.sha256(payload).hexdigest(),
        "timeline_store_version": timeline_version,
        "timeline_sha256": timeline.content_sha256,
        "shot_outputs": [],
    }
    input_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    root.write_bytes(relative, payload)
    root.write_bytes(input_relative, input_bytes)
    receipt = EpisodeOutput(
        output_id=output_id,
        episode_id=EPISODE,
        timeline_store_version=timeline_version,
        timeline_sha256=timeline.content_sha256,
        relative_path=relative,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        delivery_profile="test",
        storage_resolution=(64, 48),
        output_fps=12,
        sample_aspect_ratio="1/1",
        ffmpeg_identity="test",
        input_manifest_path=input_relative,
        input_manifest_sha256=hashlib.sha256(input_bytes).hexdigest(),
    )
    store.append_episode_output(
        receipt, expected_version=len(store.list_episode_outputs(EPISODE)),
    )
    review = record_review(
        store,
        target_kind="episode_cut",
        target_id=EPISODE,
        target_version=timeline_version,
        episode_id=EPISODE,
        actor="creator",
        approval_state=S.APPROVED,
    )
    return receipt, review.review_id


def test_fix3_item2_approval_and_export_check_sidecar_bytes_against_receipt(
    env: Any,
) -> None:
    from orchestrator.adapters.export.package import export_project

    _populate(env.root, env.store, overlay=False, font=None)
    receipt, review_id = _fix3_add_bound_cut(env.root, env.store, "bound-cut")
    input_path = receipt.input_manifest_path
    assert input_path is not None
    original = env.root.read_bytes(input_path)
    env.root.write_bytes(input_path, original + b" ")
    _episode, token = env.store.load_with_version(Episode, EPISODE)

    with pytest.raises(JobError, match="input manifest hash mismatch"):
        approve_episode(
            env.store, EPISODE, cut_output_id=receipt.output_id, review_id=review_id,
            expected_version=token,
        )

    env.root.write_bytes(input_path, original)
    _episode, token = env.store.load_with_version(Episode, EPISODE)
    approved = approve_episode(
        env.store, EPISODE, cut_output_id=receipt.output_id, review_id=review_id,
        expected_version=token,
    )
    assert approved.approved_cut_output_id == receipt.output_id
    env.root.write_bytes(input_path, original + b" ")
    _approved_episode, export_token = env.store.load_with_version(Episode, EPISODE)

    with pytest.raises(JobError, match="input manifest hash mismatch"):
        export_project(
            env.store, env.settings, episode_id=EPISODE, cut_output_id=receipt.output_id,
            expected_version=export_token,
        )


def test_fix3_item3_legacy_cut_approval_and_existing_approval_export_are_refused(
    env: Any,
) -> None:
    from orchestrator.adapters.export.package import export_project

    _populate(env.root, env.store, overlay=False, font=None)
    review_id = _make_cut(env.root, env.store, bound=False)
    episode, token = env.store.load_with_version(Episode, EPISODE)

    with pytest.raises(JobError, match="predates output binding.*assemble_cut"):
        approve_episode(
            env.store, EPISODE, cut_output_id="cut-1", review_id=review_id,
            expected_version=token,
        )

    episode = episode.model_copy(update={
        "approved_cut_version": 3,
        "approved_cut_output_id": "cut-1",
        "approval_review_id": review_id,
    })
    token = env.store.save(episode, expected_version=token)
    with pytest.raises(JobError, match="predates output binding.*assemble_cut"):
        export_project(
            env.store, env.settings, episode_id=EPISODE, cut_output_id="cut-1",
            expected_version=token,
        )


def test_fix3_item4_export_plan_includes_sidecars_for_all_retained_cuts(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.adapters.export.package as package

    _populate(env.root, env.store, overlay=False, font=None)
    first, review_id = _fix3_add_bound_cut(env.root, env.store, "bound-cut-1")
    second, _other_review = _fix3_add_bound_cut(env.root, env.store, "bound-cut-2")
    _approve_cut(env.store, review_id, output_id=first.output_id)
    # Fix round 4 passes retained sidecars into plan_snapshot as extra_files, so they go
    # through the snapshot's exclusion checks; the stub returns them as the real one does.
    monkeypatch.setattr(package, "plan_snapshot", lambda _store, *, extra_files=(): (
        package.SnapshotFile("project.json", "a" * 64),
        package.SnapshotFile(first.relative_path, first.content_sha256),
        *extra_files,
    ))
    copied: dict[str, str | None] = {}

    def capture_copy(_source: Any, _destination: Any, item: Any) -> dict[str, str]:
        copied[item.relative_path] = item.expected_sha256
        return {"path": item.relative_path, "sha256": item.expected_sha256 or "b" * 64}

    class StopBeforeDelivery(Exception):
        pass

    def stop_before_delivery(*_args: Any, **_kwargs: Any) -> None:
        raise StopBeforeDelivery

    monkeypatch.setattr(package, "_copy_file", capture_copy)
    monkeypatch.setattr(package, "_verify_reopen", stop_before_delivery)

    with pytest.raises(StopBeforeDelivery):
        package.export_project(
            env.store, env.settings, episode_id=EPISODE, cut_output_id=first.output_id,
            expected_version=env.store.load_with_version(Episode, EPISODE)[1],
        )

    assert copied[first.input_manifest_path] == first.input_manifest_sha256
    assert copied[second.input_manifest_path] == second.input_manifest_sha256


# --- Parent (2026-11-23): a post-stale Shot outside the cut still blocks cut approval -------


def test_parent_a_post_stale_shot_outside_the_cut_still_blocks_cut_approval(
    plain_lab: tuple[ProjectRoot, ProjectStore],
) -> None:
    # Before cuts were bound to their inputs, cut approval refused while ANY current Shot
    # of the Episode was stale or a draft. Binding narrowed the check to the Shots in the
    # cut's Timeline; the Episode-wide rule (now including post_stale) is restored.
    root, store = plain_lab
    _add_shot(root, store, "s1", S.APPROVED)
    review_id = _make_cut(root, store)  # the Timeline holds s1 only
    _add_shot(root, store, "s2", S.REVIEW, camera="cam-2")
    assert _revise(store, "cam-2", bitrate_kbps=900) == ("s2",)
    assert _status(store, "s2") is S.POST_STALE
    with pytest.raises(JobError) as caught:
        _approve_cut(store, review_id)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "s2" in str(caught.value) and "post_stale" in str(caught.value)
    assert _gate_cleared(store)
