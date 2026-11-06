"""Compose adapter requests from project-local domain records.

The default Phase 1 scene is a deterministic procedural domestic living-room fixture.
It does not interpret the Shot Script as scene geometry. Previously built scene
artifacts continue to load from their persisted plans when ``require_built_scene`` is set.

These functions accept an already-generated run id and only read through ProjectStore.
They do not acquire the service command lock, mutate records, write files, or derive
adapter-stage seeds.
"""

from __future__ import annotations

import json
from typing import Literal, cast

from orchestrator.adapters.blender.scene import RenderRequest, SceneSpec, american_living_room
from orchestrator.adapters.postprofile.profile import (
    EncodeSpec,
    OverlaySpec,
    PostProfileSpec,
)
from orchestrator.adapters.postprofile.runner import DegradeRequest
from orchestrator.domain.entities import (
    CameraProfile,
    PostProfile,
    Project,
    Shot,
    ShotOutputKind,
)
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.store.json_store import ProjectStore, RecordNotFound


def compose_render_request(
    store: ProjectStore,
    *,
    shot_id: str,
    episode_id: str,
    run_id: str,
    mode: Literal["master", "preview"],
    require_built_scene: bool = False,
) -> RenderRequest:
    """Build the typed Blender request from the current Shot and referenced profile."""
    shot, _mutation_token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
    try:
        camera_profile = store.load(CameraProfile, shot.camera_profile)
    except RecordNotFound as exc:
        raise RecordNotFound(
            f"Shot {shot.shot_id!r} references missing CameraProfile "
            f"{shot.camera_profile!r}"
        ) from exc
    if shot.scene_look is not None and shot.scene_look not in {"bright", "low_key"}:
        raise ValueError(
            f"Shot {shot.shot_id!r} references missing scene look {shot.scene_look!r}"
        )
    if require_built_scene:
        scene, anomaly_precomposed = _load_scene_spec(store, shot)
    else:
        # Temporary selection here; FR-15 (P0) belongs on Shot.location_id resolving to
        # a Location record for intra-episode location and camera reuse. Scene look also
        # belongs on Location, not Shot: lighting is a property of place and time of day.
        if shot.scene_look is None:
            scene = american_living_room()
        else:
            scene = american_living_room(
                look=cast(Literal["bright", "low_key"], shot.scene_look)
            )
        anomaly_precomposed = False
    return RenderRequest(
        shot_id=shot.shot_id,
        shot_version=shot.version,
        run_id=run_id,
        degrade_seed=shot.degrade_seed,
        motion_seed=shot.motion_seed,
        motion_model_semver=shot.motion_model_semver,
        duration_sec=shot.duration_sec,
        camera_profile=camera_profile,
        scene=scene,
        event_sec=shot.event_sec,
        anomaly=shot.anomaly,
        anomaly_precomposed=anomaly_precomposed,
        mode=mode,
        engine="eevee",
    )


def _load_scene_spec(store: ProjectStore, shot: Shot) -> tuple[SceneSpec, bool]:
    outputs = store.list_shot_outputs(
        shot.shot_id, episode_id=shot.episode_id,
        record_version=shot.version, kind=ShotOutputKind.SCENE,
    )
    if not outputs:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"Shot {shot.shot_id!r} version {shot.version} has no scene output; run build_scene first",
        )
    output = outputs[-1]
    try:
        artifact = json.loads(store._root.read_text(output.relative_path, encoding="utf-8"))
        if not isinstance(artifact, dict):
            raise TypeError("scene artifact root must be an object")
        plan = artifact.get("plan")
        if (not isinstance(plan, dict) or plan.get("shot_id") != shot.shot_id
                or plan.get("shot_version") != shot.version):
            raise ValueError("scene plan identity does not match the bound Shot content version")
        # plan["scene"] IS the SceneSpec (build_scene_plan dumps it there). The artifact
        # deliberately carries no sibling "scene_spec" key: two copies of the same spec in
        # one file is two sources of truth, and nothing would keep them agreeing.
        scene = SceneSpec.model_validate(plan.get("scene"))
        stored_anomaly = plan.get("anomaly")
        if shot.anomaly is None:
            if stored_anomaly is not None:
                raise ValueError("scene plan carries an anomaly absent from the current Shot")
            return scene, False
        expected_anomaly = shot.anomaly.model_dump(mode="json")
        if stored_anomaly != expected_anomaly or plan.get("event_sec") != shot.event_sec:
            raise ValueError("scene plan anomaly or event_sec does not match the current Shot")
        return scene, True
    except (OSError, TypeError, ValueError) as exc:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"scene output {output.relative_path!r} is not a valid persisted plan: {exc}",
            shot_id=shot.shot_id,
            cause=exc,
        ) from exc


def compose_post_profile_spec(
    store: ProjectStore, *, shot: Shot | None = None,
) -> PostProfileSpec:
    """Build the adapter value from a Shot override or ``Project.output_profile``."""
    project = store.load(Project)
    profile_name = (
        shot.post_profile
        if shot is not None and shot.post_profile is not None
        else project.output_profile
    )
    try:
        identity = store.load(PostProfile, profile_name)
    except RecordNotFound as exc:
        if shot is not None and shot.post_profile is not None:
            raise RecordNotFound(
                f"Shot {shot.shot_id!r} references missing PostProfile "
                f"{shot.post_profile!r}"
            ) from exc
        raise RecordNotFound(
            f"Project output_profile {project.output_profile!r} has no project-local PostProfile record"
        ) from exc

    missing = [name for name in ("output_fps", "storage_resolution", "sample_aspect_ratio", "encode")
               if getattr(identity, name) is None]
    if missing:
        raise ValueError(
            f"PostProfile {identity.name!r} cannot be composed; missing persisted fields: "
            + ", ".join(missing)
        )
    unsupported_events = sorted({
        event.type for event in identity.artifact_schedule if event.type not in {"drop", "freeze"}
    })
    if unsupported_events:
        raise ValueError(
            f"PostProfile {identity.name!r} uses unsupported artifact schedule type(s): "
            + ", ".join(unsupported_events)
        )
    if identity.overlay is not None and identity.overlay.placement != "in_recording":
        raise ValueError(
            f"PostProfile {identity.name!r} uses unsupported overlay placement "
            f"{identity.overlay.placement!r}; only 'in_recording' is supported"
        )

    return PostProfileSpec(
        identity=identity,
        output_fps=identity.output_fps,
        storage_resolution=identity.storage_resolution,
        sample_aspect_ratio=identity.sample_aspect_ratio,
        artifact_schedule=tuple(identity.artifact_schedule),
        overlay=(OverlaySpec.model_validate(
            identity.overlay.model_dump(
                mode="python", exclude={"schema_version", "font_relative"}
            )
        )
                 if identity.overlay is not None else None),
        encode=EncodeSpec.model_validate(
            identity.encode.model_dump(mode="python", exclude={"schema_version"})
        ),
    )


def compose_degrade_request(
    store: ProjectStore,
    *,
    shot_id: str,
    episode_id: str,
    run_id: str,
) -> DegradeRequest:
    """Build degradation from the current Shot, its master receipt, and project profile.

    This is a read-only composition step. The caller supplies the run id; the font
    comes from the selected PostProfile and seed derivation remains inside the adapter.
    """
    shot, _mutation_token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
    masters = store.list_shot_outputs(
        shot.shot_id,
        episode_id=shot.episode_id,
        record_version=shot.version,
        kind=ShotOutputKind.MASTER,
    )
    if not masters:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"Shot {shot.shot_id!r} version {shot.version} has no master output; run render_master first",
            shot_id=shot.shot_id,
        )
    try:
        camera_profile = store.load(CameraProfile, shot.camera_profile)
    except RecordNotFound as exc:
        raise RecordNotFound(
            f"Shot {shot.shot_id!r} references missing CameraProfile {shot.camera_profile!r}"
        ) from exc
    profile = compose_post_profile_spec(store, shot=shot)
    overlay = profile.identity.overlay
    if overlay is not None and not overlay.font_relative:
        raise ValueError(
            f"PostProfile {profile.identity.name!r} cannot be composed; missing persisted field: "
            "overlay.font_relative. No profile migration is registered. "
            "To repair it, set overlay.font_relative to \"assets/fonts/overlay.ttf\" in "
            f"post_profiles/{profile.identity.name}.json, then place a font you are licensed "
            "to use at assets/fonts/overlay.ttf."
        )
    return DegradeRequest(
        shot_id=shot.shot_id,
        shot_version=shot.version,
        run_id=run_id,
        degrade_seed=shot.degrade_seed,
        master_relative=masters[-1].relative_path,
        font_relative=overlay.font_relative if overlay is not None else None,
        camera_profile=camera_profile,
        profile=profile,
    )
