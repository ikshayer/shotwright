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

from orchestrator.adapters.blender.scene import (
    AssetBinding,
    ObjectSpec,
    RenderRequest,
    SceneSpec,
    SharedLocation,
    american_living_room,
    validate_camera_placement,
)
from orchestrator.adapters.postprofile.profile import (
    EncodeSpec,
    OverlaySpec,
    PostProfileSpec,
)
from orchestrator.adapters.postprofile.runner import DegradeRequest
from orchestrator.domain.base import Invalidation
from orchestrator.domain.entities import (
    Asset,
    CameraProfile,
    Location,
    PostProfile,
    Project,
    Shot,
    ShotOutputKind,
)
from orchestrator.domain.invalidation import field_invalidation
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
    camera_profile = camera_profile_for_render(store, shot)
    if (
        shot.location_id is None
        and shot.scene_look is not None
        and shot.scene_look not in {"bright", "low_key"}
    ):
        raise ValueError(
            f"Shot {shot.shot_id!r} references missing scene look {shot.scene_look!r}"
        )
    location = None
    location_scene = None
    if shot.location_id is not None and (mode == "master" or not require_built_scene):
        location = _load_location_for_shot(store, shot)
        location_scene = compose_scene_for_location(location)
    if require_built_scene:
        loaded_scene = _load_scene_spec(store, shot)
        # Retain compatibility with callers and test doubles written before the
        # persisted scene artifact began carrying asset bindings.
        scene, anomaly_precomposed = loaded_scene[:2]
        saved_asset_bindings = loaded_scene[2] if len(loaded_scene) > 2 else {}
    else:
        scene = location_scene if location_scene is not None else _legacy_scene_for_shot(shot)
        anomaly_precomposed = False
        saved_asset_bindings = {}
    asset_bindings = _asset_bindings_for_scene(
        store,
        scene,
        figure_asset_id=getattr(shot.anomaly, "figure_asset_id", None),
    )
    if require_built_scene:
        current_asset_bindings = {
            asset_id: (binding.checksum, binding.file_path)
            for asset_id, binding in asset_bindings.items()
        }
        if current_asset_bindings != saved_asset_bindings:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"built scene for Shot {shot.shot_id!r} has asset checksums or paths "
                "that no longer match the project registry; rebuild the scene",
            )
    shared_location = None
    if mode == "master" and location is not None and location_scene is not None:
        if require_built_scene and not _matches_location_scene(scene, location_scene):
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"built scene for Shot {shot.shot_id!r} no longer matches "
                f"Location {location.location_id!r} version {location.version}; rebuild the scene",
                shot_id=shot.shot_id,
            )
        shared_location = SharedLocation(
            episode_id=shot.episode_id,
            location_id=location.location_id,
            location_version=location.version,
            camera_id=shot.camera_id or camera_profile.camera_id,
            object_names=tuple(item.name for item in location_scene.objects),
            light_names=tuple(item.name for item in location_scene.lights),
            material_names=tuple(item.name for item in location_scene.materials),
        )
    request = RenderRequest(
        shot_id=shot.shot_id,
        shot_version=shot.version,
        run_id=run_id,
        degrade_seed=shot.degrade_seed,
        motion_seed=shot.motion_seed,
        motion_model_semver=shot.motion_model_semver,
        duration_sec=shot.duration_sec,
        camera_profile=camera_profile,
        scene=scene,
        shared_location=shared_location,
        asset_bindings=asset_bindings,
        event_sec=shot.event_sec,
        anomaly=shot.anomaly,
        anomaly_precomposed=anomaly_precomposed,
        mode=mode,
        engine="eevee",
    )
    validate_camera_placement(request)
    return request


def _matches_location_scene(scene: SceneSpec, location_scene: SceneSpec) -> bool:
    """A persisted shot may add figure geometry and animation to the current room."""
    scene_fields = {"objects", "lights", "materials", "camera_keyframes"}
    if scene.model_dump(exclude=scene_fields) != location_scene.model_dump(exclude=scene_fields):
        return False
    for collection, animated_fields in (
        ("objects", {"keyframes", "visibility_keyframes"}),
        ("lights", {"energy_keyframes"}),
        ("materials", set()),
    ):
        actual = {item.name: item.model_dump(exclude=animated_fields)
                  for item in getattr(scene, collection)}
        for expected in getattr(location_scene, collection):
            if actual.get(expected.name) != expected.model_dump(exclude=animated_fields):
                return False
    return True


def compose_scene_for_location(location: Location) -> SceneSpec:
    """Build the existing Location shell/placement/material SceneSpec without store access."""
    if location.base_shell != "american_living_room":
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"Location {location.location_id!r} has unsupported base shell {location.base_shell!r}",
        )
    scene = american_living_room(look=location.scene_look)
    objects = list(scene.objects)
    names = {item.name for item in objects} | {item.name for item in scene.lights}
    for index, placement in enumerate(location.placements, start=1):
        name = f"LocationPlacement{index:03d}"
        suffix = 2
        while name in names:
            name = f"LocationPlacement{index:03d}_{suffix:02d}"
            suffix += 1
        names.add(name)
        objects.append(ObjectSpec(
            name=name,
            # ObjectSpec's closed primitive vocabulary is still required by the worker
            # envelope; the worker imports the asset instead whenever asset_id is set.
            kind="box",
            asset_id=placement.asset_id,
            location=placement.position,
            dimensions=placement.dimensions,
            rotation=placement.rotation,
            fit_mode=placement.fit_mode,
        ))
    materials = list(scene.materials)
    known_materials = {material.name for material in materials}
    for material_name, texture_set in location.material_overrides.items():
        if material_name not in known_materials:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"Location {location.location_id!r} material override targets unknown room material "
                f"{material_name!r}",
            )
        materials = [
            material.model_copy(update={"textures": texture_set})
            if material.name == material_name else material
            for material in materials
        ]
    return SceneSpec.model_validate(scene.model_dump(mode="python") | {
        "objects": tuple(objects), "materials": tuple(materials),
        "environment": location.environment,
    })


def _legacy_scene_for_shot(shot: Shot) -> SceneSpec:
    """Preserve the procedural shell for shots that predate Location binding."""
    if shot.scene_look is None:
        return american_living_room()
    return american_living_room(look=cast(Literal["bright", "low_key"], shot.scene_look))


def _load_location_for_shot(store: ProjectStore, shot: Shot) -> Location:
    assert shot.location_id is not None
    try:
        return store.load(Location, shot.location_id)
    except RecordNotFound as exc:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"Shot {shot.shot_id!r} references missing Location {shot.location_id!r}",
            cause=exc,
        ) from exc


def camera_profile_for_render(store: ProjectStore, shot: Shot) -> CameraProfile:
    """Resolve the latest post-only profile values and accepted framing values."""
    try:
        current = store.load(CameraProfile, shot.camera_profile)
    except RecordNotFound as exc:
        raise RecordNotFound(
            f"Shot {shot.shot_id!r} references missing CameraProfile "
            f"{shot.camera_profile!r}"
        ) from exc
    if shot.camera_profile_version is None:
        return current
    try:
        frozen = store.load(
            CameraProfile, shot.camera_profile, version=shot.camera_profile_version,
        )
    except RecordNotFound as exc:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"Shot {shot.shot_id!r} pins missing CameraProfile "
            f"{shot.camera_profile!r} version {shot.camera_profile_version}",
            shot_id=shot.shot_id,
            cause=exc,
        ) from exc
    rebuild_fields = {
        name for name in CameraProfile.model_fields
        if field_invalidation(CameraProfile, name) is Invalidation.REBUILD
    }
    values = {name: getattr(frozen, name) for name in rebuild_fields}
    resolved = current.model_copy(update=values)
    return CameraProfile.model_validate(resolved.model_dump(mode="python"))


def _load_scene_spec(
    store: ProjectStore, shot: Shot
) -> tuple[SceneSpec, bool, dict[str, tuple[str, str]]]:
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
        #
        # Strip the camera animation before validating. The plan is a worker payload, so
        # its scene block legitimately carries the handheld keyframes `build_scene_plan`
        # derived from the camera profile -- but those are DERIVED, not part of the
        # scene's identity, and feeding them back in as input is rejected outright by the
        # gate that stops a caller supplying camera motion. Measured 2026-11-06: every
        # handheld shot failed its render with "caller-supplied camera keyframes are
        # unsupported", because build_scene persisted motion that render then read back.
        # Dropping it here also means a changed `motion_seed` actually takes effect on a
        # re-render instead of being pinned by the stored scene.
        scene_payload = plan.get("scene")
        saved_asset_bindings: dict[str, tuple[str, str]] = {}
        if isinstance(scene_payload, dict):
            object_payloads = scene_payload.get("objects", [])
            if not isinstance(object_payloads, list):
                raise TypeError("scene plan objects must be a list")
            def remember_binding(asset_id, checksum, file_path):
                if not isinstance(asset_id, str) or not isinstance(checksum, str) or not isinstance(file_path, str):
                    raise TypeError("scene plan asset reference is missing its checksum or file path")
                identity = (checksum, file_path)
                if asset_id in saved_asset_bindings and saved_asset_bindings[asset_id] != identity:
                    raise ValueError(f"scene plan has inconsistent bindings for Asset {asset_id!r}")
                saved_asset_bindings[asset_id] = identity

            for item in object_payloads:
                if isinstance(item, dict) and item.get("asset_id") is not None:
                    remember_binding(item["asset_id"], item.get("asset_checksum"), item.get("asset_file_path"))
            material_payloads = scene_payload.get("materials", [])
            if not isinstance(material_payloads, list):
                raise TypeError("scene plan materials must be a list")
            for material in material_payloads:
                if not isinstance(material, dict):
                    raise TypeError("scene plan material must be an object")
                textures = material.get("textures")
                if textures is None:
                    continue
                if not isinstance(textures, dict):
                    raise TypeError("scene plan material textures must be an object")
                maps = textures.get("maps")
                bindings = textures.get("bindings")
                if not isinstance(maps, dict) or not isinstance(bindings, dict) or set(maps) != set(bindings):
                    raise ValueError(f"material {material.get('name')!r} has missing or conflicting map bindings")
                for role, asset_id in maps.items():
                    binding = bindings.get(role)
                    if not isinstance(binding, dict) or binding.get("asset_id") != asset_id:
                        raise ValueError(f"material {material.get('name')!r} map {role!r} binding disagrees with maps")
                    remember_binding(asset_id, binding.get("asset_checksum"), binding.get("asset_file_path"))
                textures.pop("bindings")
                textures.pop("asset_path", None)
            environment_payload = scene_payload.get("environment")
            if environment_payload is not None:
                if not isinstance(environment_payload, dict):
                    raise TypeError("scene plan environment must be an object")
                binding = environment_payload.pop("binding", None)
                if not isinstance(binding, dict) or binding.get("asset_id") != environment_payload.get("asset_id"):
                    raise ValueError("scene plan environment has a missing or conflicting asset binding")
                if set(binding) != {"asset_id", "asset_checksum", "asset_file_path"}:
                    raise ValueError("scene plan environment binding has unexpected derived fields")
                if "asset_path" in environment_payload:
                    raise ValueError("persisted scene environment may not contain an authorized runtime path")
                remember_binding(binding.get("asset_id"), binding.get("asset_checksum"), binding.get("asset_file_path"))
            scene_payload = {
                key: value for key, value in scene_payload.items()
                if key != "camera_keyframes"
            }
            scene_payload["objects"] = [
                {
                    key: value for key, value in item.items()
                    if key not in {"asset_checksum", "asset_file_path", "asset_path"}
                }
                if isinstance(item, dict) else item
                for item in object_payloads
            ]
        scene = SceneSpec.model_validate(scene_payload)
        stored_anomaly = plan.get("anomaly")
        if shot.anomaly is None:
            if stored_anomaly is not None:
                raise ValueError("scene plan carries an anomaly absent from the current Shot")
            return scene, False, saved_asset_bindings
        expected_anomaly = shot.anomaly.model_dump(mode="json")
        if stored_anomaly != expected_anomaly or plan.get("event_sec") != shot.event_sec:
            raise ValueError("scene plan anomaly or event_sec does not match the current Shot")
        return scene, True, saved_asset_bindings
    except (OSError, TypeError, ValueError) as exc:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"scene output {output.relative_path!r} is not a valid persisted plan: {exc}",
            shot_id=shot.shot_id,
            cause=exc,
        ) from exc


def _asset_bindings_for_scene(
    store: ProjectStore,
    scene: SceneSpec,
    *,
    figure_asset_id: str | None = None,
) -> dict[str, AssetBinding]:
    """Resolve referenced IDs to verified, immutable project asset identities."""
    bindings: dict[str, AssetBinding] = {}
    asset_ids = {item.asset_id for item in scene.objects if item.asset_id is not None}
    map_ids = {
        asset_id
        for material in scene.materials
        if material.textures is not None
        for asset_id in material.textures.maps.values()
    }
    geometry_ids = {item.asset_id for item in scene.objects if item.asset_id is not None}
    if figure_asset_id is not None:
        geometry_ids.add(figure_asset_id)
    if geometry_ids & map_ids:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            "PBR map assets cannot also be used as geometry assets: "
            + ", ".join(sorted(geometry_ids & map_ids)),
        )
    if figure_asset_id is not None:
        asset_ids.add(figure_asset_id)
    asset_ids.update(map_ids)
    environment_id = scene.environment.asset_id if scene.environment is not None else None
    if environment_id is not None:
        if environment_id in asset_ids:
            raise JobError(JobErrorKind.INVALID_MANIFEST, "environment asset id overlaps geometry or material maps")
        asset_ids.add(environment_id)
    for asset_id in sorted(asset_ids):
        assert asset_id is not None
        try:
            usage = "environment" if asset_id == environment_id else "texture" if asset_id in map_ids else "geometry"
            asset: Asset = store.verify_asset(asset_id, usage=usage)
        except RecordNotFound as exc:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"scene references missing Asset {asset_id!r}",
                cause=exc,
            ) from exc
        if asset.file_path is None or asset.checksum is None:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"Asset {asset_id!r} has no registered file path or checksum",
            )
        if asset_id == environment_id and (asset.type != "hdri" or not asset.file_path.lower().endswith((".hdr", ".exr"))):
            raise JobError(JobErrorKind.INVALID_MANIFEST, f"environment Asset {asset_id!r} must be an HDRI .hdr or .exr")
        if asset_id in map_ids:
            extension = asset.file_path.rsplit(".", 1)[-1].lower() if "." in asset.file_path else ""
            if asset.type != "texture" or extension not in {"png", "jpg", "jpeg", "webp"}:
                raise JobError(
                    JobErrorKind.INVALID_MANIFEST,
                    f"PBR map Asset {asset_id!r} must have type='texture' and a .png/.jpg/.jpeg/.webp file",
                )
        bindings[asset_id] = AssetBinding(
            asset_id=asset.asset_id,
            file_path=asset.file_path,
            checksum=asset.checksum,
        )
    return bindings


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
    if identity.overlay is not None and identity.overlay.placement not in (
        "in_recording", "on_playback"
    ):
        raise ValueError(
            f"PostProfile {identity.name!r} uses unsupported overlay placement "
            f"{identity.overlay.placement!r}"
        )

    return PostProfileSpec(
        identity=identity,
        output_fps=identity.output_fps,
        storage_resolution=identity.storage_resolution,
        sample_aspect_ratio=identity.sample_aspect_ratio,
        artifact_schedule=tuple(identity.artifact_schedule),
        overlay=(OverlaySpec.model_validate(
            # The entity's schema_version (top level and on the nested indicator) is
            # storage metadata; the FFmpeg spec carries its own.
            identity.overlay.model_dump(
                mode="python",
                exclude={"schema_version": True, "font_relative": True,
                         "indicator": {"schema_version"}},
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
