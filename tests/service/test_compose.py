"""Composition tests from COMPOSITION_CONTRACT.md.

These tests treat the service composer as a pure adapter between the project-local
JSON store and typed adapter requests. The composition API is not named in the
contract, so the tests assume ``compose_render_request(store, episode_id, shot_id,
mode=...)`` and ``compose_post_profile_spec(store)``.

The bound Phase 1 scene is the measured ``synthetic_interior()`` fixture. It is
not FR-04's future procedural hallway builder driven by a text brief.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator.adapters.blender.runner import BlenderAdapter
from orchestrator.adapters.blender.scene import RenderRequest, SceneSpec, synthetic_interior
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    Job,
    Project,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import parse_settings
from orchestrator.service.compose import compose_render_request
from orchestrator.store.json_store import ProjectStore


def _store_with_shot(
    root: Path,
    *,
    camera_profile: str = "cam-profile-1",
    create_camera_profile: bool = True,
) -> tuple[ProjectStore, Shot, int]:
    store = ProjectStore(ProjectRoot(root))
    store.save(Project(project_id="p1", title="Composition fixture"), expected_version=0)
    store.save(
        Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30),
        expected_version=0,
    )
    if create_camera_profile:
        store.create(CameraProfile(
            camera_id=camera_profile,
            profile_name="Fixture CCTV",
            field_of_view_mm=21.25,
        ))

    original = Shot(
        shot_id="shot-1",
        episode_id="ep1",
        camera_profile=camera_profile,
        duration_sec=4.0,
        degrade_seed=11,
    )
    store.create(original)

    # Advance only the mutation token once with an allowed lifecycle-only edit,
    # then add a content successor. The resulting content version is 2 while the
    # store mutation token is 3, so a composer cannot accidentally pass by using
    # one number for both contracts.
    store.update(
        original.model_copy(update={"approval_notes": "mutation-token-only edit"}),
        expected_version=1,
    )
    current = store.load(Shot, original.shot_id, episode_id=original.episode_id)
    successor = current.model_copy(
        update={
            **current.next_version(),
            "duration_sec": 7.375,
            "degrade_seed": 918273,
        }
    )
    store.revise(successor, expected_version=2)
    shot, mutation_token = store.load_with_version(
        Shot, original.shot_id, episode_id=original.episode_id,
    )
    return store, shot, mutation_token


def _compose_render(
    store: ProjectStore, shot: Shot, *, mode: str = "master", run_id: str = "run-aaaa"
) -> RenderRequest:
    # Keyword-only, and run_id is supplied by the caller: composition stays pure, so the
    # determinism assertion below is about composition rather than about a patched RNG.
    return compose_render_request(
        store, shot_id=shot.shot_id, episode_id=shot.episode_id, run_id=run_id, mode=mode
    )


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    """Capture directories and file bytes so new empty dirs also count as writes."""
    items: list[tuple[str, str, bytes | None]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            items.append((relative, "directory", None))
        elif path.is_file():
            items.append((relative, "file", path.read_bytes()))
    return tuple(sorted(items))


def test_render_composition_uses_shot_values_and_content_version(tmp_path: Path) -> None:
    store, shot, mutation_token = _store_with_shot(tmp_path)

    assert shot.version == 2
    assert mutation_token == 3
    assert shot.version != mutation_token

    request = _compose_render(store, shot)

    assert isinstance(request, RenderRequest)
    assert request.shot_id == shot.shot_id
    assert request.shot_version == shot.version
    assert request.shot_version != mutation_token
    assert request.degrade_seed == 918273
    assert request.duration_sec == 7.375
    assert request.camera_profile == store.load(CameraProfile, shot.camera_profile)
    assert request.camera_profile.field_of_view_mm == 21.25
    assert isinstance(request.scene, SceneSpec)
    assert request.scene == synthetic_interior()
    assert request.engine == "eevee"


def test_render_composition_is_deterministic_except_for_run_id(tmp_path: Path) -> None:
    store, shot, _ = _store_with_shot(tmp_path)

    # Two DIFFERENT run ids: every other field must still match, which is a stronger
    # claim than two identical calls happening to agree.
    first = _compose_render(store, shot, run_id="run-first")
    second = _compose_render(store, shot, run_id="run-second")

    first_values = first.model_dump(mode="json")
    second_values = second.model_dump(mode="json")
    assert first_values.pop("run_id") == "run-first"
    assert second_values.pop("run_id") == "run-second"
    assert first_values == second_values

    round_tripped = RenderRequest.model_validate(first.model_dump(mode="python"))
    assert round_tripped == first


def test_render_preview_and_master_compose_different_modes(tmp_path: Path) -> None:
    store, shot, _ = _store_with_shot(tmp_path)

    master = _compose_render(store, shot, mode="master")
    preview = _compose_render(store, shot, mode="preview")

    assert master.mode == "master"
    assert preview.mode == "preview"


def test_missing_camera_profile_names_the_missing_record(tmp_path: Path) -> None:
    store, shot, _ = _store_with_shot(
        tmp_path,
        camera_profile="missing-camera-profile",
        create_camera_profile=False,
    )

    with pytest.raises(Exception) as caught:
        _compose_render(store, shot)

    assert "missing-camera-profile" in str(caught.value)
    assert not isinstance(caught.value, ValidationError)
    assert "validation errors for RenderRequest" not in str(caught.value)


def test_render_consumes_the_scene_artifact_build_scene_actually_wrote(tmp_path: Path) -> None:
    """Producer and consumer must agree on the scene artifact's shape.

    Separating assembly from rendering split one piece of knowledge across two modules:
    `assemble_scene` writes the artifact and `_load_scene_spec` reads it. Every test for
    each half passed while they disagreed about which key holds the SceneSpec, because
    nothing round-tripped. This asserts the round trip, so the next disagreement fails
    here instead of at a 422 from /preview.
    """
    root = ProjectRoot(tmp_path)
    store, shot, _ = _store_with_shot(tmp_path)
    blender_exe = root.write_bytes("fake-blender.exe", b"not an executable")
    settings = parse_settings({
        "project_root": str(root.path),
        "binaries": {"blender_exe": str(blender_exe)},
    })

    def forbidden_popen(argv: list[str], **_kwargs: Any) -> Any:
        raise AssertionError(f"scene assembly must not launch a subprocess: {argv!r}")

    request = _compose_render(store, shot, run_id="run-scene")
    adapter = BlenderAdapter(root, settings, popen=forbidden_popen)
    result = adapter.assemble_scene(
        request, Job(job_id="job-scene", type="scene", shot_id=shot.shot_id, run_id="run-scene"),
    )

    # Persist the receipt the way JobRunner does, so the consumer finds it by ledger.
    store.append_shot_output(
        ShotOutput(
            output_id="scene-1",
            episode_id=shot.episode_id,
            shot_id=shot.shot_id,
            record_version=shot.version,
            kind=ShotOutputKind.SCENE,
            relative_path=result.scene_path,
            content_sha256=hashlib.sha256(
                root.resolve(result.scene_path).read_bytes()
            ).hexdigest(),
        ),
        expected_version=0,
    )

    consumed = compose_render_request(
        store, shot_id=shot.shot_id, episode_id=shot.episode_id,
        run_id="run-render", mode="master", require_built_scene=True,
    )

    assert consumed.scene == request.scene, (
        "the SceneSpec a render consumes is not the one build_scene persisted"
    )
