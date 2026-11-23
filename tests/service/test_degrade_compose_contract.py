"""Regressions for composing a post-profile request from project records.

The overlay font belongs to the persisted PostProfile, so composition takes no font
argument and resolves the project-relative font path from that profile.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import orchestrator.service.compose as compose_module
from orchestrator.adapters.postprofile.runner import DegradeRequest, PostProfileAdapter
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
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.service.dependencies import create_project
from orchestrator.store.json_store import ProjectStore, RecordNotFound

_FONT_RELATIVE = "assets/fonts/fixture.ttf"
_DEJAVU_MONO = Path(__file__).resolve().parents[1] / "fixtures" / "fonts" / "DejaVuSansMono.ttf"


def _install_real_font(root: ProjectRoot) -> None:
    if not _DEJAVU_MONO.is_file():
        pytest.skip(f"fixture font is absent: {_DEJAVU_MONO}")
    root.write_bytes(_FONT_RELATIVE, _DEJAVU_MONO.read_bytes())


def _post_profile(*, font_relative: str | None = _FONT_RELATIVE) -> PostProfile:
    return PostProfile(
        name="dvr-2005",
        semver="0.2.0",
        output_fps=12,
        storage_resolution=(640, 480),
        sample_aspect_ratio="1/1",
        overlay=PostProfileOverlay(
            camera_id="CAM 03", clock_epoch=1131984000, fontsize=16,
            x=8, y="h-24", placement="in_recording", font_relative=font_relative,
        ),
        encode=PostProfileEncode(
            codec="libx264", preset="medium", crf=30,
            pix_fmt="yuv420p", threads=1,
        ),
        provenance="assembly composer fixture",
    )


def _store_with_shot(
    root_path: Path,
    *,
    current_master: bool,
    font_relative: str | None = _FONT_RELATIVE,
    shot_post_profile: str | None = None,
    create_shot_post_profile: bool = True,
) -> tuple[ProjectRoot, ProjectStore, Shot]:
    root = ProjectRoot(root_path)
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="Degrade composition", output_profile="dvr-2005"),
               expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30),
               expected_version=0)
    camera = CameraProfile(
        camera_id="cam-1", profile_name="Distinctive post camera", fps=15.0,
    )
    store.create(camera)
    store.save(_post_profile(font_relative=font_relative), expected_version=0)
    if shot_post_profile is not None and create_shot_post_profile:
        selected_profile = _post_profile(font_relative=font_relative).model_copy(
            update={"name": shot_post_profile}
        )
        store.save(selected_profile, expected_version=0)
    if font_relative is not None:
        root.write_bytes(font_relative, b"fixture font")

    original = Shot(
        shot_id="shot-1", episode_id="ep1", camera_profile=camera.camera_id,
        duration_sec=7.375, degrade_seed=918273, post_profile=shot_post_profile,
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


def _compose(store: ProjectStore, shot: Shot) -> DegradeRequest:
    # Run identity is an input, like compose_render_request, so composition does not
    # create a random id or derive the stage seed.
    return compose_module.compose_degrade_request(
        store,
        shot_id=shot.shot_id,
        episode_id=shot.episode_id,
        run_id="run-degrade",
    )


def test_degrade_composition_uses_current_master_shot_seed_and_named_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, shot = _store_with_shot(tmp_path, current_master=True)
    _install_real_font(root)
    before = _tree_snapshot(root.path)

    def forbidden_command() -> Any:
        pytest.fail("compose_degrade_request acquired the ProjectStore command lock")

    monkeypatch.setattr(store, "command", forbidden_command)
    request = _compose(store, shot)

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
    persisted_profile = store.load(PostProfile, "dvr-2005")
    assert request.font_relative == persisted_profile.overlay.font_relative, (
        f"request font_relative={request.font_relative!r} does not match profile-owned "
        f"font_relative={persisted_profile.overlay.font_relative!r}"
    )
    assert request.camera_profile == store.load(CameraProfile, shot.camera_profile), (
        f"request camera_profile={request.camera_profile!r} does not match the Shot reference"
    )
    assert request.profile.identity == persisted_profile, (
        f"request profile identity={request.profile.identity!r}, Project names "
        f"{persisted_profile.name!r}"
    )
    assert _tree_snapshot(root.path) == before, (
        "compose_degrade_request changed project files or directories during composition"
    )


def test_degrade_composition_uses_the_shot_profile_override(
    tmp_path: Path,
) -> None:
    root, store, shot = _store_with_shot(
        tmp_path, current_master=True, shot_post_profile="low-light-bw",
    )
    _install_real_font(root)

    selected = compose_module.compose_post_profile_spec(store, shot=shot)
    request = _compose(store, shot)

    assert selected.identity.name == "low-light-bw"
    assert request.profile.identity == selected.identity
    assert store.load(Project).output_profile == "dvr-2005"


def test_degrade_composition_rejects_a_missing_shot_profile_without_fallback(
    tmp_path: Path,
) -> None:
    _root, store, shot = _store_with_shot(
        tmp_path,
        current_master=True,
        shot_post_profile="missing-profile",
        create_shot_post_profile=False,
    )

    with pytest.raises(RecordNotFound) as caught:
        _compose(store, shot)

    assert str(caught.value) == "Shot 'shot-1' references missing PostProfile 'missing-profile'"


def test_degrade_composition_rejects_only_a_stale_master_and_names_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, shot = _store_with_shot(tmp_path, current_master=False)
    old_outputs = store.list_shot_outputs(
        shot.shot_id, episode_id=shot.episode_id,
        record_version=1, kind=ShotOutputKind.MASTER,
    )
    before = _tree_snapshot(root.path)

    def forbidden_command() -> Any:
        pytest.fail("compose_degrade_request acquired the ProjectStore command lock")

    monkeypatch.setattr(store, "command", forbidden_command)
    with pytest.raises(JobError) as caught:
        _compose(store, shot)

    message = str(caught.value).lower()
    assert len(old_outputs) == 1, f"fixture expected a retained v1 master, observed {old_outputs!r}"
    assert shot.version == 2, f"fixture current content version is {shot.version}, expected v2"
    assert "master" in message and shot.shot_id in message and str(shot.version) in message, (
        f"missing-current-master error did not name the Shot, version, and reason: {caught.value!r}"
    )
    assert _tree_snapshot(root.path) == before, (
        "failed compose_degrade_request changed project files or directories"
    )


def test_degrade_composition_rejects_a_profile_without_its_font(
    tmp_path: Path,
) -> None:
    _root, store, shot = _store_with_shot(
        tmp_path, current_master=True, font_relative=None,
    )

    with pytest.raises(ValueError) as caught:
        _compose(store, shot)

    message = str(caught.value)
    assert "dvr-2005" in message and "font_relative" in message, (
        f"fontless profile error must name profile 'dvr-2005' and missing field "
        f"'font_relative', observed {message!r}"
    )
    assert (
        'No profile migration is registered. To repair it, set overlay.font_relative to '
        '"assets/fonts/overlay.ttf" in post_profiles/dvr-2005.json, then place a font you '
        'are licensed to use at assets/fonts/overlay.ttf.'
    ) in message, f"fontless profile error omitted the explicit repair: {message!r}"


def test_new_project_reaches_adapter_missing_its_creator_supplied_font(tmp_path: Path) -> None:
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    project = create_project(store, Project(project_id="p1", title="New project"))
    store.save(
        Episode(episode_id="ep1", project_id=project.project_id, number=1, duration_target_sec=30),
        expected_version=0,
    )
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=5)
    store.create(shot)
    master_relative = "renders/s1/v0001/seed/master.mkv"
    master_bytes = b"fixture current master"
    root.write_bytes(master_relative, master_bytes)
    store.append_shot_output(
        ShotOutput(
            output_id="seed-master",
            episode_id="ep1",
            shot_id="s1",
            record_version=shot.version,
            kind=ShotOutputKind.MASTER,
            relative_path=master_relative,
            content_sha256=hashlib.sha256(master_bytes).hexdigest(),
            engine_id="BLENDER_EEVEE",
            engine_build="fixture-build",
        ),
        expected_version=0,
    )

    font_relative = "assets/fonts/overlay.ttf"
    profile = store.load(PostProfile, "dvr-2005")
    assert profile.overlay is not None
    assert profile.overlay.font_relative == font_relative
    assert root.resolve("assets/fonts").is_dir()
    assert not root.resolve(font_relative).exists()

    request = _compose(store, shot)
    assert isinstance(request, DegradeRequest), (
        f"new project's font path was rejected during composition: {request!r}"
    )
    assert request.font_relative == font_relative

    with pytest.raises(JobError) as caught:
        PostProfileAdapter(root, Settings(project_root=root.path)).degrade(request)

    assert caught.value.kind is JobErrorKind.MISSING_MODEL_ARTIFACT
    assert str(caught.value) == f"overlay font is missing: {font_relative}"


def test_project_store_rejects_font_paths_outside_the_project_on_save_and_load(
    tmp_path: Path,
) -> None:
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    safe = _post_profile(font_relative=None)
    unsafe_overlay = safe.overlay.model_copy(update={"font_relative": "../outside.ttf"})
    unsafe = safe.model_copy(update={"overlay": unsafe_overlay})

    with pytest.raises(JobError) as save_error:
        store.save(unsafe, expected_version=0)
    assert save_error.value.kind is JobErrorKind.INVALID_MANIFEST, (
        f"unsafe profile font save failed as {save_error.value.kind}, expected containment "
        "to reject it as INVALID_MANIFEST"
    )
    assert "overlay.font_relative" in str(save_error.value), (
        f"unsafe profile font save did not name the declared path: {save_error.value!r}"
    )

    # Simulate an older or edited JSON record that bypassed save-time validation. The
    # write itself still goes through ProjectRoot; ProjectStore.load must revalidate it.
    raw = unsafe.model_dump(mode="json")
    raw["_store_version"] = 1
    root.write_text("post_profiles/dvr-2005.json", json.dumps(raw))
    with pytest.raises(JobError) as load_error:
        store.load(PostProfile, "dvr-2005")
    assert load_error.value.kind is JobErrorKind.INVALID_MANIFEST, (
        f"unsafe profile font load failed as {load_error.value.kind}, expected containment "
        "to reject it as INVALID_MANIFEST"
    )
    assert "overlay.font_relative" in str(load_error.value), (
        f"unsafe profile font load did not name the declared path: {load_error.value!r}"
    )
