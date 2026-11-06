"""Regressions for composing a post-profile request from project records.

Assumed API: ``compose_degrade_request(store, *, shot_id, episode_id, run_id,
font_relative)``. The assembly contract names the function but leaves its signature and
the source of the overlay font unspecified. This test passes the project-relative font
as an explicit input instead of inventing a project font registry.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

import orchestrator.service.compose as compose_module
from orchestrator.adapters.postprofile.runner import DegradeRequest
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    PostProfile,
    PostProfileEncode,
    PostProfileOverlay,
    Project,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.store.json_store import ProjectStore


def _post_profile() -> PostProfile:
    return PostProfile(
        name="dvr-2005",
        semver="0.2.0",
        output_fps=12,
        storage_resolution=(640, 480),
        sample_aspect_ratio="1/1",
        overlay=PostProfileOverlay(
            camera_id="CAM 03", clock_epoch=1131984000, fontsize=16,
            x=8, y="h-24", placement="in_recording",
        ),
        encode=PostProfileEncode(
            codec="libx264", preset="medium", crf=30,
            pix_fmt="yuv420p", threads=1,
        ),
        provenance="assembly composer fixture",
    )


def _store_with_shot(root_path: Path, *, current_master: bool) -> tuple[ProjectRoot, ProjectStore, Shot]:
    root = ProjectRoot(root_path)
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="Degrade composition", output_profile="dvr-2005"),
               expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30),
               expected_version=0)
    camera = CameraProfile(
        camera_id="cam-1", profile_name="Distinctive post camera", fps=17.25,
    )
    store.create(camera)
    store.save(_post_profile(), expected_version=0)

    original = Shot(
        shot_id="shot-1", episode_id="ep1", camera_profile=camera.camera_id,
        duration_sec=7.375, degrade_seed=918273,
    )
    store.create(original)

    master_relative = "renders/shot-1/v0001/run-render/master.mkv"
    master_bytes = b"fixture master file"
    root.write_bytes(master_relative, master_bytes)
    store.append_shot_output(
        ShotOutput(
            output_id="master-v1", episode_id="ep1", shot_id="shot-1",
            record_version=original.version, kind=ShotOutputKind.MASTER,
            relative_path=master_relative,
            content_sha256=hashlib.sha256(master_bytes).hexdigest(),
            engine_id="BLENDER_EEVEE", engine_build="5.2.0",
        ),
        expected_version=0,
    )

    shot = original
    if not current_master:
        # Leave a valid receipt for v1 but make v2 current. Composition must not bind
        # degradation to the retained, stale master from the prior content version.
        shot = original.model_copy(update={
            **original.next_version(),
            "narrative_beat": "content changed after the old master",
        })
        store.revise(shot, expected_version=1)
        shot = store.load(Shot, shot.shot_id, episode_id=shot.episode_id)

    return root, store, shot


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    """Capture files and directories so even a new empty directory counts as a write."""
    items: list[tuple[str, str, bytes | None]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            items.append((relative, "directory", None))
        elif path.is_file():
            items.append((relative, "file", path.read_bytes()))
    return tuple(sorted(items))


def _compose(store: ProjectStore, shot: Shot, *, font_relative: str) -> DegradeRequest:
    # Run identity is an input, like compose_render_request, so composition does not
    # create a random id or derive the stage seed.
    return compose_module.compose_degrade_request(
        store,
        shot_id=shot.shot_id,
        episode_id=shot.episode_id,
        run_id="run-degrade",
        font_relative=font_relative,
    )


def test_degrade_composition_uses_current_master_shot_seed_and_named_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, shot = _store_with_shot(tmp_path, current_master=True)
    font_relative = "assets/fonts/fixture.ttf"
    root.write_bytes(font_relative, b"fixture font")
    before = _tree_snapshot(root.path)

    def forbidden_command() -> Any:
        pytest.fail("compose_degrade_request acquired the ProjectStore command lock")

    monkeypatch.setattr(store, "command", forbidden_command)
    request = _compose(store, shot, font_relative=font_relative)

    outputs = store.list_shot_outputs(
        shot.shot_id, episode_id=shot.episode_id,
        record_version=shot.version, kind=ShotOutputKind.MASTER,
    )
    assert isinstance(request, DegradeRequest), (
        f"composer returned {type(request).__name__}, expected a typed DegradeRequest"
    )
    assert request.shot_id == shot.shot_id and request.shot_version == shot.version, (
        f"request bound {request.shot_id!r} v{request.shot_version}, expected current "
        f"Shot {shot.shot_id!r} v{shot.version}"
    )
    assert len(outputs) == 1, f"fixture expected one current master receipt, observed {outputs!r}"
    assert request.master_relative == outputs[0].relative_path, (
        f"request resolved master {request.master_relative!r}, current receipt names "
        f"{outputs[0].relative_path!r}"
    )
    assert request.degrade_seed == 918273 == shot.degrade_seed, (
        f"request used degrade_seed={request.degrade_seed}, Shot carries {shot.degrade_seed}"
    )
    assert request.font_relative == font_relative, (
        f"request used font_relative={request.font_relative!r}, caller supplied {font_relative!r}"
    )
    assert request.camera_profile == store.load(CameraProfile, shot.camera_profile), (
        f"request camera_profile={request.camera_profile!r} does not match the Shot reference"
    )
    persisted_profile = store.load(PostProfile, "dvr-2005")
    assert request.profile.identity == persisted_profile, (
        f"request profile identity={request.profile.identity!r}, Project names "
        f"{persisted_profile.name!r}"
    )
    assert _tree_snapshot(root.path) == before, (
        "compose_degrade_request changed project files or directories during composition"
    )


def test_degrade_composition_rejects_only_a_stale_master_and_names_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, shot = _store_with_shot(tmp_path, current_master=False)
    font_relative = "assets/fonts/fixture.ttf"
    root.write_bytes(font_relative, b"fixture font")
    old_outputs = store.list_shot_outputs(
        shot.shot_id, episode_id=shot.episode_id,
        record_version=1, kind=ShotOutputKind.MASTER,
    )
    before = _tree_snapshot(root.path)

    def forbidden_command() -> Any:
        pytest.fail("compose_degrade_request acquired the ProjectStore command lock")

    monkeypatch.setattr(store, "command", forbidden_command)
    with pytest.raises(JobError) as caught:
        _compose(store, shot, font_relative=font_relative)

    message = str(caught.value).lower()
    assert len(old_outputs) == 1, f"fixture expected a retained v1 master, observed {old_outputs!r}"
    assert shot.version == 2, f"fixture current content version is {shot.version}, expected v2"
    assert "master" in message and shot.shot_id in message and str(shot.version) in message, (
        f"missing-current-master error did not name the Shot, version, and reason: {caught.value!r}"
    )
    assert _tree_snapshot(root.path) == before, (
        "failed compose_degrade_request changed project files or directories"
    )
