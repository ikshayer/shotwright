from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.adapters.postprofile.profile import ProfileValidationError
from orchestrator.domain.entities import (
    AnalogArtifacts,
    CameraProfile,
    Episode,
    PostProfile,
    PostProfileEncode,
    Project,
    Shot,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.service.compose import derive_postprofile_spec_for_shot, postprofile_spec_sha256
from orchestrator.service.dependencies import revise_camera_profile, validated_cascades
from orchestrator.store.json_store import ProjectStore

PIN = "sha256:" + "a" * 64
OTHER_PIN = "sha256:" + "b" * 64


@pytest.mark.parametrize(
    ("profile_pin", "camera_pin", "resolved_pin"),
    [(None, PIN, PIN), (PIN, None, PIN), (PIN, PIN, PIN), (None, None, None)],
)
def test_pin_resolves_from_the_versioned_camera_or_selected_postprofile(
    tmp_path: Path, profile_pin: str | None, camera_pin: str | None, resolved_pin: str | None,
) -> None:
    store, shot = _existing_project(tmp_path, profile_pin=profile_pin)
    profile_path = tmp_path / "post_profiles/legacy.json"
    original_profile = profile_path.read_bytes()
    camera, token = store.load_with_version(CameraProfile, "hall")
    revised = camera.model_copy(update={
        **camera.next_version(),
        "analog_artifacts": AnalogArtifacts(vhs={}, ntsc_pipe_sha256=camera_pin),
    })

    revise_camera_profile(store, revised, expected_version=token)

    reopened = ProjectStore(ProjectRoot(tmp_path))
    assert reopened.load(Shot, "s1", episode_id="ep1") == shot
    loaded, base, derived = derive_postprofile_spec_for_shot(reopened, shot)
    assert loaded.analog_artifacts.ntsc_pipe_sha256 == camera_pin
    assert base.identity.pinned_binaries.get("ntsc-pipe") == profile_pin
    assert derived.identity.pinned_binaries.get("ntsc-pipe") == resolved_pin
    assert profile_path.read_bytes() == original_profile
    assert len(validated_cascades(reopened)) == 1


def test_conflicting_pins_are_refused_during_derivation(tmp_path: Path) -> None:
    store, shot = _existing_project(tmp_path, profile_pin=PIN)
    original, token = store.load_with_version(CameraProfile, "hall")
    camera = original.model_copy(update={
        **original.next_version(),
        "analog_artifacts": AnalogArtifacts(vhs={}, ntsc_pipe_sha256=OTHER_PIN),
    })

    with pytest.raises(ProfileValidationError, match="ntsc-pipe"):
        derive_postprofile_spec_for_shot(store, shot, profile_override=camera)
    with pytest.raises(ProfileValidationError, match="ntsc-pipe"):
        revise_camera_profile(store, camera, expected_version=token)
    assert store.load_with_version(CameraProfile, "hall") == (original, token)


def test_pin_alone_leaves_the_neutral_spec_hash_unchanged(tmp_path: Path) -> None:
    store, shot = _existing_project(tmp_path, profile_pin=None)
    camera, _, baseline = derive_postprofile_spec_for_shot(store, shot)
    pin_only = camera.model_copy(update={"analog_artifacts": AnalogArtifacts(ntsc_pipe_sha256=PIN)})
    _, _, derived = derive_postprofile_spec_for_shot(store, shot, profile_override=pin_only)

    assert not pin_only.analog_artifacts.active
    assert postprofile_spec_sha256(derived) == postprofile_spec_sha256(baseline)
    assert "ntsc-pipe" not in derived.identity.pinned_binaries


@pytest.mark.parametrize("value", [123, "a" * 64, "sha256:" + "A" * 64, PIN + "\n"])
def test_camera_pin_requires_exact_lowercase_sha256(value: object) -> None:
    with pytest.raises(ValidationError):
        AnalogArtifacts.model_validate({"vhs": {}, "ntsc_pipe_sha256": value})


def _existing_project(tmp_path: Path, *, profile_pin: str | None) -> tuple[ProjectStore, Shot]:
    store = ProjectStore(ProjectRoot(tmp_path))
    store.save(Project(project_id="p1", title="Test", output_profile="legacy"), expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30), expected_version=0)
    store.create(CameraProfile(camera_id="hall", profile_name="Hall"))
    store.save(PostProfile(
        name="legacy", semver="1.0.0", output_fps=12, storage_resolution=(640, 480),
        sample_aspect_ratio="1/1", pinned_binaries={"ntsc-pipe": profile_pin} if profile_pin else {},
        encode=PostProfileEncode(codec="libx264", crf=24, pix_fmt="yuv420p", threads=1),
    ), expected_version=0)
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=5, camera_profile="hall")
    store.create(shot)
    return store, shot
