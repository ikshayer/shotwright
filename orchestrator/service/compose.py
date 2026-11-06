"""Compose adapter requests from project-local domain records.

The Phase 1 scene uses ``synthetic_interior()``, the measured spike room. This does
not implement FR-04's requirement to build a believable hallway from a text brief;
the Script is not interpreted as scene geometry here.

These functions accept an already-generated run id and only read through ProjectStore.
They do not acquire the service command lock, mutate records, write files, or derive
adapter-stage seeds.
"""

from __future__ import annotations

from typing import Literal

from orchestrator.adapters.blender.scene import RenderRequest, synthetic_interior
from orchestrator.adapters.postprofile.profile import (
    EncodeSpec,
    OverlaySpec,
    PostProfileSpec,
)
from orchestrator.domain.entities import CameraProfile, PostProfile, Project, Shot
from orchestrator.store.json_store import ProjectStore, RecordNotFound


def compose_render_request(
    store: ProjectStore,
    *,
    shot_id: str,
    episode_id: str,
    run_id: str,
    mode: Literal["master", "preview"],
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
    return RenderRequest(
        shot_id=shot.shot_id,
        shot_version=shot.version,
        run_id=run_id,
        degrade_seed=shot.degrade_seed,
        duration_sec=shot.duration_sec,
        camera_profile=camera_profile,
        scene=synthetic_interior(),
        mode=mode,
        engine="eevee",
    )


def compose_post_profile_spec(store: ProjectStore) -> PostProfileSpec:
    """Build the adapter value from the profile named by ``Project.output_profile``."""
    project = store.load(Project)
    try:
        identity = store.load(PostProfile, project.output_profile)
    except RecordNotFound as exc:
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
            identity.overlay.model_dump(mode="python", exclude={"schema_version"})
        )
                 if identity.overlay is not None else None),
        encode=EncodeSpec.model_validate(
            identity.encode.model_dump(mode="python", exclude={"schema_version"})
        ),
    )
