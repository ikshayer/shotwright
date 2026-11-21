"""Derive versioned production records from structured Script directions."""
from __future__ import annotations

import json
import math
import uuid
from typing import Any

from pydantic import ValidationError

from orchestrator.adapters.blender.scene import (
    RenderRequest,
    build_scene_plan,
    validate_camera_placement,
)
from orchestrator.domain.base import ProductionMethod, ShotStatus
from orchestrator.domain.entities import (
    Asset,
    Beat,
    CameraProfile,
    Episode,
    Location,
    Project,
    Script,
    Shot,
)
from orchestrator.domain.invalidation import agent_overwrites_creator_fields
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.compose import (
    _asset_bindings_for_scene,
    camera_profile_for_render,
    compose_scene_for_location,
)
from orchestrator.service.dependencies import (
    _pending_error,
    create_location,
    propose_shots,
    revise_location,
    revise_shot,
    validated_cascades,
)
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict


def derive_script_scene_plan(
    store: ProjectStore, *, episode_id: str, script_id: str,
    expected_version: int, expected_episode_version: int,
    expected_shot_versions: dict[str, int], expected_location_versions: dict[str, int],
) -> dict[str, Any]:
    """Validate a complete structured Script derivation, then append its versions.

    The store's shared RLock spans prevalidation and each established service command.
    Commands are intentionally not nested: each existing command wrapper owns its own
    command boundary and invalidation cascade.
    """
    journal_relative: str | None = None
    committed: list[str] = []
    with store._lock:
        script, script_token = store.load_with_version(Script, script_id, episode_id=episode_id)
        episode, episode_token = store.load_with_version(Episode, episode_id)
        if episode_token != expected_episode_version:
            raise VersionConflict(f"episodes/{episode_id}/episode.json", expected_episode_version, episode_token, episode)
        if script_token != expected_version:
            raise VersionConflict(f"episodes/{episode_id}/scripts/{script_id}", expected_version, script_token, script)
        beats = {beat.beat_id: beat for beat in script.beats}
        if not beats or set(script.beat_to_shot) != set(beats):
            raise _invalid("every Script beat must have exactly one mapped Shot")
        shot_ids = list(script.beat_to_shot.values())
        if len(shot_ids) != len(set(shot_ids)):
            raise _invalid("Script beat_to_shot may not map multiple beats to one Shot")
        if any(getattr(beat, "scene_direction", None) is None for beat in script.beats):
            raise _invalid("every Script beat requires structured scene_direction")
        scenes = list(getattr(script, "scenes", []))
        locations = {location.location_id: location for location in scenes}
        if len(locations) != len(scenes):
            raise _invalid("Script.scenes contains duplicate location ids")
        directions = {beat.beat_id: beat.scene_direction for beat in script.beats}
        if any(direction.location_id not in locations for direction in directions.values()):
            raise _invalid("every scene_direction must reference an embedded Script location")
        for beat in script.beats:
            direction = directions[beat.beat_id]
            if direction.event_sec is not None and direction.event_sec > beat.target_sec:
                raise _invalid(f"Beat {beat.beat_id!r} scene_direction.event_sec exceeds Beat.target_sec")
        location_ids = sorted(locations)
        if set(expected_shot_versions) != set(shot_ids):
            raise _invalid("expected_shot_versions keys must exactly cover mapped Shot ids")
        if set(expected_location_versions) != set(location_ids):
            raise _invalid("expected_location_versions keys must exactly cover embedded Location ids")
        if any(type(value) is not int or value < 0 for value in expected_shot_versions.values()):
            raise _invalid("expected Shot mutation tokens must be non-negative integers")
        if any(type(value) is not int or value < 0 for value in expected_location_versions.values()):
            raise _invalid("expected Location mutation tokens must be non-negative integers")

        # Validate project identity and revision readiness before the first command writes.
        store.load(Project)
        _pending_error(store)
        # Parse every Episode manifest now: revise_location scans all episodes, and a
        # corrupt manifest must fail here rather than after the first Location commit.
        _project_episode_ids(store)
        fenced_shot_versions = {
            (row["episode_id"], row["shot_id"], row["version"])
            for plan in validated_cascades(store) for row in plan["affected"]
        }

        # Validate registered cameras, every embedded asset and all concurrency tokens.
        for direction in directions.values():
            try:
                store.load(CameraProfile, direction.camera_profile)
            except RecordNotFound as exc:
                raise _invalid(f"scene direction references unregistered CameraProfile {direction.camera_profile!r}", cause=exc) from exc
            figure_asset_id = getattr(direction.anomaly, "figure_asset_id", None)
            if figure_asset_id:
                figure_asset = store.verify_asset(figure_asset_id, usage="geometry")
                if figure_asset.type not in {"model", "geometry", "mesh"}:
                    raise _invalid(f"anomaly figure Asset {figure_asset_id!r} has wrong type {figure_asset.type!r}")
        for location in scenes:
            _validate_location_assets(store, location)
            from orchestrator.adapters.blender.scene import american_living_room
            room_materials = {material.name for material in american_living_room(look=location.scene_look).materials}
            unknown_targets = set(location.material_overrides) - room_materials
            if unknown_targets:
                raise _invalid(f"Location {location.location_id!r} material overrides target unknown room material(s): {sorted(unknown_targets)}")

        current_locations: dict[str, tuple[Location | None, int]] = {}
        for location_id in location_ids:
            token_expected = expected_location_versions[location_id]
            try:
                current, token = store.load_with_version(Location, location_id)
            except RecordNotFound:
                current, token = None, 0
            if token != token_expected:
                raise VersionConflict(f"locations/{location_id}", token_expected, token, current)  # type: ignore[arg-type]
            current_locations[location_id] = (current, token)
            if current is not None:
                _check_location_ownership(store, script, episode_id, current, location)

        changed_location_ids = {
            location_id for location_id, (current, _token) in current_locations.items()
            if current is not None and _location_content(current) != _location_content(locations[location_id])
        }
        current_shots: dict[str, tuple[Shot | None, int]] = {}
        proposed_shots: list[Shot] = []
        revised_shots: list[tuple[Shot, int]] = []
        conflicts: dict[str, list[str]] = {}
        for beat in script.beats:
            shot_id = script.beat_to_shot[beat.beat_id]
            token_expected = expected_shot_versions[shot_id]
            try:
                current, token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
            except RecordNotFound:
                current, token = None, 0
            if token != token_expected:
                raise VersionConflict(f"episodes/{episode_id}/shots/{shot_id}", token_expected, token, current)  # type: ignore[arg-type]
            current_shots[shot_id] = (current, token)
            if current is not None and (
                current.episode_id != episode_id or current.beat_id not in (None, beat.beat_id)
            ):
                raise _invalid(f"Shot {shot_id!r} is not owned by mapped beat {beat.beat_id!r}")
            direction = directions[beat.beat_id]
            proposed = _shot_direction_values(beat, direction, locations[direction.location_id])
            if current is None or current.camera_profile != direction.camera_profile:
                proposed["camera_profile_version"] = None
            validation_values = {
                **proposed,
                "production_method": ProductionMethod.BLENDER,
                "fallback_method": ProductionMethod.BLENDER,
            }
            validation_shot = (
                Shot(shot_id=shot_id, episode_id=episode_id, **validation_values)
                if current is None
                else Shot.model_validate(current.model_copy(update=validation_values).model_dump(mode="python"))
            )
            _validate_actual_scene_plan(store, validation_shot, locations[direction.location_id])
            if current is not None:
                overwrite = agent_overwrites_creator_fields(current, proposed)
                if overwrite:
                    conflicts[shot_id] = list(overwrite)
                    continue
                changed_fields = [name for name, value in proposed.items() if getattr(current, name) != value]
                needs_fresh_successor = (
                    current.status in {ShotStatus.STALE, ShotStatus.REJECTED}
                    or proposed["location_id"] in changed_location_ids
                    or (episode_id, shot_id, current.version) in fenced_shot_versions
                )
                if changed_fields or needs_fresh_successor:
                    payload = current.model_copy(update={
                        **proposed, "status": type(current.status).DRAFT,
                        "approval_review_id": None, "approval_notes": "",
                    })
                    payload = Shot.model_validate(payload.model_dump(mode="python"))
                    successor = payload.model_copy(update={"version": current.version + 1, "supersedes": current.version})
                    store.validate_revision(successor, expected_version=token)
                    revised_shots.append((successor, token))
            else:
                proposed = {**proposed, "production_method": ProductionMethod.BLENDER, "fallback_method": ProductionMethod.BLENDER}
                payload = Shot(shot_id=shot_id, episode_id=episode_id, **proposed)
                store.validate_entity_payload(payload)
                proposed_shots.append(payload)
        if conflicts:
            raise _invalid("agent derivation would overwrite creator-authored fields: " + json.dumps(conflicts, sort_keys=True))

        revised_locations: list[tuple[Location, int]] = []
        new_locations: list[Location] = []
        for location_id in location_ids:
            blueprint = locations[location_id]
            current, token = current_locations[location_id]
            if current is None:
                candidate = blueprint.model_copy(update={"version": 1, "supersedes": None, "approval_review_id": None})
                store.validate_entity_payload(candidate)
                new_locations.append(candidate)
            elif _location_content(current) != _location_content(blueprint):
                candidate = current.model_copy(update={
                    **_location_content(blueprint), "version": current.version + 1,
                    "supersedes": current.version, "approval_review_id": None,
                })
                store.validate_revision(candidate, expected_version=token)
                revised_locations.append((candidate, token))

        changed = bool(new_locations or revised_locations or proposed_shots or revised_shots)
        if not changed:
            result = _result(script, script_token, episode_token, [], current_shots, current_locations, None)
            result["timing_summary"] = _timing_summary(script, episode)
            return result

        journal_relative = f"episodes/{episode_id}/scripts/{script_id}/derivations/{uuid.uuid4().hex}.jsonl"
        journal = {
            "event": "started", "script_id": script_id, "script_version": script.version,
            "script_token": script_token, "episode_id": episode_id,
            "planned_locations": [x.location_id for x in new_locations] + [x.location_id for x, _ in revised_locations],
            "planned_shots": [x.shot_id for x in proposed_shots] + [x.shot_id for x, _ in revised_shots],
        }
        store._root.write_text(journal_relative, json.dumps(journal, sort_keys=True) + "\n")
        try:
            for location in new_locations:
                create_location(store, location)
                committed.append(f"location:{location.location_id}:v{location.version}")
                _journal_event(store, journal_relative, "committed", committed)
            for location, token in revised_locations:
                revise_location(store, location, expected_version=token)
                committed.append(f"location:{location.location_id}:v{location.version}")
                _journal_event(store, journal_relative, "committed", committed)
            for shot, initially_checked_token in revised_shots:
                current, refreshed_token = store.load_with_version(
                    Shot, shot.shot_id, episode_id=episode_id,
                )
                if current.version != shot.supersedes:
                    raise _invalid(f"Shot {shot.shot_id!r} content head changed during derivation")
                # Location cascade may append a lifecycle token after the caller token
                # was checked. The shared RLock proves this is our own cascade's advance.
                if refreshed_token < initially_checked_token:
                    raise _invalid(f"Shot {shot.shot_id!r} mutation token moved backwards")
                revise_shot(store, shot, expected_version=refreshed_token)
                committed.append(f"shot:{shot.shot_id}:v{shot.version}")
                _journal_event(store, journal_relative, "committed", committed)
            if proposed_shots:
                propose_shots(store, proposed_shots)
                committed.extend(f"shot:{shot.shot_id}:v{shot.version}" for shot in proposed_shots)
                # propose_shots performs the one Episode manifest append itself.
                _journal_event(store, journal_relative, "committed", committed)
            store._root.append_line(journal_relative, json.dumps({"event": "complete", "committed": committed}, sort_keys=True))
        except Exception as exc:
            recovery_note = ""
            try:
                _recover_committed(store, episode_id, new_locations, revised_locations,
                                   proposed_shots, revised_shots, current_locations,
                                   current_shots, committed)
            except (OSError, ValueError, JobError) as recovery_exc:
                recovery_note = f"; committed-head recovery also failed: {recovery_exc}"
            journal_note = f"{type(exc).__name__}: {exc}{recovery_note}"
            try:
                _journal_event(store, journal_relative, "partial_failure", committed, error=journal_note)
                journal_status = f"retained journal {journal_relative}"
            except (OSError, ValueError, JobError) as journal_exc:
                journal_status = f"journal {journal_relative} could not be extended ({journal_exc}); initial journal remains"
            raise JobError(JobErrorKind.INTERNAL,
                           f"Script derivation partially failed; committed {committed!r}; {journal_status}",
                           cause=exc) from exc

        shot_heads = {shot_id: store.load_with_version(Shot, shot_id, episode_id=episode_id) for shot_id in shot_ids}
        location_heads = {location_id: store.load_with_version(Location, location_id) for location_id in location_ids}
        episode_token = store.load_with_version(Episode, episode_id)[1]
        result = _result(script, script_token, episode_token, committed, shot_heads, location_heads, journal_relative)
        result["timing_summary"] = _timing_summary(script, episode)
        return result


def _project_episode_ids(store: ProjectStore) -> list[str]:
    """Enumerate Episode manifests in the one project folder through its contained root."""
    root = store._root.resolve("episodes")
    project_id = store.load(Project).project_id
    ids: list[str] = []
    if not root.exists():
        return ids
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "episode.json").is_file():
            # revise_location scans every episode folder, so a folder holding Shots
            # without its manifest would refuse the revision after earlier Location
            # commits. Refuse here, before the derivation writes anything.
            if store.ids(Shot, episode_id=child.name):
                raise _invalid(
                    f"episodes/{child.name} holds Shots but has no episode.json; "
                    "restore the Episode manifest before deriving"
                )
            continue
        episode, _token = store.load_with_version(Episode, child.name)
        if episode.project_id == project_id:
            ids.append(episode.episode_id)
    return ids


def _validate_actual_scene_plan(store: ProjectStore, shot: Shot, location: Location) -> None:
    """Run the production composer and pure Blender plan builder before any writes."""
    try:
        scene = compose_scene_for_location(location)
        camera_profile = camera_profile_for_render(store, shot)
        bindings = _asset_bindings_for_scene(
            store, scene, figure_asset_id=getattr(shot.anomaly, "figure_asset_id", None),
        )
        request = RenderRequest(
            shot_id=shot.shot_id, shot_version=shot.version, run_id="derive-validation",
            degrade_seed=shot.degrade_seed, motion_seed=shot.motion_seed,
            motion_model_semver=shot.motion_model_semver, duration_sec=shot.duration_sec,
            camera_profile=camera_profile, scene=scene, asset_bindings=bindings,
            event_sec=shot.event_sec, anomaly=shot.anomaly, mode="master", engine="eevee",
        )
        validate_camera_placement(request)
        # This is the real pure plan builder; it validates anomaly object/light names,
        # figure bounds, camera geometry and the asset-binding envelope without rendering.
        build_scene_plan(request)
    except JobError:
        raise
    except (ValueError, TypeError, ValidationError) as exc:
        raise _invalid(
            f"Shot {shot.shot_id!r} (Beat {shot.beat_id!r}) cannot build a valid master scene plan: {exc}",
            cause=exc,
        ) from exc


def _timing_summary(script: Script, episode: Episode) -> dict[str, Any]:
    derived_duration = sum(beat.target_sec for beat in script.beats)
    return {
        "derived_duration_sec": derived_duration,
        "episode_target_sec": episode.duration_target_sec,
        "delta_sec": derived_duration - episode.duration_target_sec,
        "matches_target": math.isclose(
            derived_duration, episode.duration_target_sec, rel_tol=0.0, abs_tol=1e-6,
        ),
    }


def _shot_direction_values(beat: Beat, direction: Any, location: Location) -> dict[str, Any]:
    narrative = beat.narration_text or beat.caption_text or ""
    values: dict[str, Any] = {
        "beat_id": beat.beat_id, "narrative_beat": narrative,
        "duration_sec": beat.target_sec, "camera_profile": direction.camera_profile,
        "framing": direction.framing, "event_sec": direction.event_sec,
        "anomaly": direction.anomaly, "location_id": location.location_id,
        "dependencies": list(dict.fromkeys([location.location_id, *location.asset_ids,
            *(p.asset_id for p in location.placements),
            *(asset_id for textures in location.material_overrides.values() for asset_id in textures.maps.values()),
            *([location.environment.asset_id] if location.environment is not None else [])])),
    }
    return values


def _validate_location_assets(store: ProjectStore, location: Location) -> None:
    for placement in location.placements:
        asset = store.verify_asset(placement.asset_id, usage="geometry")
        if asset.type not in {"model", "geometry", "mesh"}:
            raise _invalid(f"Location geometry binding {placement.asset_id!r} has wrong type {asset.type!r}")
    for textures in location.material_overrides.values():
        for asset_id in textures.maps.values():
            asset = store.verify_asset(asset_id, usage="texture")
            if asset.type != "texture":
                raise _invalid(f"Location map binding {asset_id!r} has wrong type {asset.type!r}")
    if location.environment is not None:
        asset = store.verify_asset(location.environment.asset_id, usage="environment")
        if asset.type != "hdri":
            raise _invalid(f"Location environment {asset.asset_id!r} has wrong type {asset.type!r}")
    for asset_id in location.asset_ids:
        asset = store.load(Asset, asset_id)
        if asset.type == "texture":
            asset = store.verify_asset(asset_id, usage="texture")
        elif asset.type == "hdri":
            asset = store.verify_asset(asset_id, usage="environment")
        elif asset.type in {"model", "geometry", "mesh"}:
            asset = store.verify_asset(asset_id, usage="geometry")
        elif asset.type in {"audio", "image"}:
            _verify_nonrender_asset(store, asset)
        else:
            raise _invalid(f"Location asset {asset_id!r} has unsupported type {asset.type!r}")


def _verify_nonrender_asset(store: ProjectStore, asset: Asset) -> None:
    import hashlib
    if not asset.file_path or not asset.checksum:
        raise _invalid(f"Location asset {asset.asset_id!r} has incomplete file/checksum identity")
    try:
        data = store._root.read_bytes(asset.file_path)
    except (OSError, ValueError) as exc:
        raise _invalid(f"Location asset {asset.asset_id!r} cannot be read through ProjectRoot", cause=exc) from exc
    if hashlib.sha256(data).hexdigest() != asset.checksum:
        raise _invalid(f"Location asset {asset.asset_id!r} checksum does not match registered bytes")


def _check_location_ownership(store: ProjectStore, script: Script, episode_id: str, current: Location, blueprint: Location) -> None:
    mapped = set(script.beat_to_shot.values())
    referenced = False
    for shot_id in mapped:
        try:
            shot = store.load(Shot, shot_id, episode_id=episode_id)
        except RecordNotFound:
            continue
        if shot.location_id == current.location_id:
            referenced = True
            break
    if not referenced and (current.approval_review_id is not None or _location_content(current) != _location_content(blueprint)):
        raise _invalid(f"Location {current.location_id!r} is unrelated and does not match an unapproved embedded blueprint")


def _location_content(location: Location) -> dict[str, Any]:
    result = location.model_dump(mode="python")
    for key in ("version", "supersedes", "schema_version", "approval_review_id"):
        result.pop(key, None)
    return result


def _shot_content(shot: Shot) -> dict[str, Any]:
    result = shot.model_dump(mode="python")
    for key in ("version", "supersedes", "schema_version"):
        result.pop(key, None)
    return result


def _result(script: Script, script_token: int, episode_token: int, changed: list[str], shots: dict[str, Any], locations: dict[str, Any], journal: str | None) -> dict[str, Any]:
    shot_versions = {key: value[0].version for key, value in shots.items() if value[0] is not None}
    shot_tokens = {key: value[1] for key, value in shots.items() if value[0] is not None}
    location_versions = {key: value[0].version for key, value in locations.items() if value[0] is not None}
    location_tokens = {key: value[1] for key, value in locations.items() if value[0] is not None}
    return {
        "script_version": script.version, "script_token": script_token,
        "episode_token": episode_token,
        "changed": list(dict.fromkeys(item.split(":", 2)[1] for item in changed)), "conflicts": [],
        "shots": shot_versions, "shot_tokens": shot_tokens,
        "locations": location_versions, "location_tokens": location_tokens,
        "journal_path": journal,
    }


def _recover_committed(store: ProjectStore, episode_id: str, new_locations, revised_locations,
                       proposed_shots, revised_shots, old_locations, old_shots,
                       committed: list[str]) -> None:
    """Recover precise content heads after an existing command reports a partial write."""
    seen = set(committed)
    for record, prior in [*((item, None) for item in new_locations),
                          *((item, old_locations[item.location_id][0]) for item, _ in revised_locations)]:
        try:
            current = store.load(Location, record.location_id)
        except RecordNotFound:
            continue
        if prior is None or current.version > prior.version:
            label = f"location:{record.location_id}:v{current.version}"
            if label not in seen:
                committed.append(label)
                seen.add(label)
    for record, prior in [*((item, None) for item in proposed_shots),
                          *((item, old_shots[item.shot_id][0]) for item, _ in revised_shots)]:
        try:
            current = store.load(Shot, record.shot_id, episode_id=episode_id)
        except RecordNotFound:
            continue
        if prior is None or current.version > prior.version:
            label = f"shot:{record.shot_id}:v{current.version}"
            if label not in seen:
                committed.append(label)
                seen.add(label)


def _journal_event(store: ProjectStore, path: str, event: str, committed: list[str], error: str | None = None) -> None:
    row: dict[str, Any] = {"event": event, "committed": list(committed)}
    if error is not None:
        row["error"] = error
    store._root.append_line(path, json.dumps(row, sort_keys=True, ensure_ascii=False))


def _invalid(message: str, *, cause: BaseException | None = None) -> JobError:
    return JobError(JobErrorKind.INVALID_MANIFEST, message, cause=cause)
