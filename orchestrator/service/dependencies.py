"""Serialized, journaled dependency revisions and roll-forward recovery."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from orchestrator.domain.base import LEGACY_SCHEMA_VERSION, ShotStatus, VersionedEntity
from orchestrator.domain.entities import (
    ArtifactScheduleEntry,
    CameraProfile,
    Episode,
    Location,
    PostProfile,
    PostProfileEncode,
    PostProfileOverlay,
    PostProfileOverlayIndicator,
    PostStage,
    Project,
    Script,
    Shot,
)
from orchestrator.domain.invalidation import revision_impact
from orchestrator.domain.state import TransitionContext, check_revision, check_transition
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.store.json_store import ProjectStore, VersionConflict

_ENTITY_TYPES = {"Script": Script, "Location": Location, "CameraProfile": CameraProfile, "Shot": Shot}


class ShotProposalWriteFailure(RuntimeError):
    """A multi-Shot append failed after reporting the durable partial result."""

    def __init__(self, written: list[str], not_written: list[str], cause: BaseException) -> None:
        self.written = tuple(written)
        self.not_written = tuple(not_written)
        self.cause = cause
        super().__init__(
            f"Shot proposal failed after writing {list(self.written)}; "
            f"not written: {list(self.not_written)}; cause: {cause}"
        )


def validated_cascades(store: ProjectStore) -> tuple[dict[str, Any], ...]:
    """Validate every journal payload before history is used as evidence."""
    plans = store.cascades()
    for plan in plans:
        entity_type = plan["entity_type"]
        cls = _ENTITY_TYPES.get(entity_type)
        if cls is None:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"cascade {plan['cascade_id']} has unknown entity type {entity_type!r}",
            )
        try:
            before = store.validate_entity_payload(cls.model_validate(plan["before"]))
            after = store.validate_entity_payload(cls.model_validate(plan["after"]))
            check_revision(before, after)
            record_id = _record_id(before)
            episode_id = getattr(before, "episode_id", None)
            stored_before, stored_token = store.load_with_version(
                cls, record_id, episode_id=episode_id, version=before.version,
            )
            if (not _same(stored_before, plan["before"])
                    or stored_token != plan["expected_version"]):
                raise ValueError("planned before payload or mutation token does not match retained history")
            # Exercise domain impact validation as well as successor validation;
            # this keeps journal payloads subject to the same accepted entity set.
            revision_impact(before, after, ())
        except Exception as exc:
            cause = exc.__cause__ if isinstance(exc, JobError) and exc.__cause__ is not None else exc
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"cascade {plan['cascade_id']} has invalid entity payload; cause: {cause}",
                cause=cause,
            ) from exc
    return plans


def _record_id(entity: VersionedEntity) -> str:
    """Select the storage key from the entity's concrete layout."""
    if type(entity) is Script:
        return entity.script_id
    if type(entity) is Location:
        return entity.location_id
    if type(entity) is CameraProfile:
        return entity.camera_id
    if type(entity) is Shot:
        return entity.shot_id
    raise JobError(JobErrorKind.INVALID_MANIFEST,
                   f"unsupported revision entity type {type(entity).__name__}")


def _pending_error(store: ProjectStore) -> None:
    plans = validated_cascades(store)
    pending_ids = {plan["cascade_id"] for plan in store.cascades(pending_only=True)}
    pending = tuple(plan for plan in plans if plan["cascade_id"] in pending_ids)
    if pending:
        ids = [plan["cascade_id"] for plan in pending]
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"pending cascade plan(s) {ids}; call resume_invalidations(store) before another approval or revision",
        )


def _shots(store: ProjectStore, episode_ids: Iterable[str]) -> list[Shot]:
    result: list[Shot] = []
    for episode_id in sorted(set(episode_ids)):
        result.extend(store.load(Shot, shot_id, episode_id=episode_id)
                      for shot_id in store.ids(Shot, episode_id=episode_id))
    return result


def _plan(store: ProjectStore, before: VersionedEntity, after: VersionedEntity,
          expected_version: int, shots: Iterable[Shot], episode_ids: Iterable[str]) -> dict[str, Any]:
    affected = sorted(
        ({"episode_id": shot.episode_id, "shot_id": shot.shot_id, "version": shot.version}
         for shot in revision_impact(before, after, shots)),
        key=lambda row: (row["episode_id"], row["shot_id"], row["version"]),
    )
    ids = sorted({row["episode_id"] for row in affected})
    if isinstance(after, Shot):
        ids = sorted(set(ids) | {after.episode_id})
    # Validate every gate record before the durable plan or any mutation.
    for episode_id in ids:
        store.load(Episode, episode_id)
    return {
        "cascade_id": uuid.uuid4().hex,
        "entity_type": type(before).__name__,
        "before": before.model_dump(mode="json"),
        "after": after.model_dump(mode="json"),
        "expected_version": expected_version,
        "episode_ids": ids,
        "affected": affected,
    }


def _same(entity: VersionedEntity, payload: dict[str, Any]) -> bool:
    # Compare the full stored value in the journal's schema without changing its bytes.
    stored = (entity.schema_v1_payload() if payload.get("schema_version") == LEGACY_SCHEMA_VERSION
              else entity.model_dump(mode="json"))
    return stored == payload


def _revoke_gates(store: ProjectStore, plan: dict[str, Any], revoked: list[str]) -> None:
    for episode_id in plan["episode_ids"]:
        episode, token = store.load_with_version(Episode, episode_id)
        if episode.approved_cut_version is None and episode.approval_review_id is None:
            continue
        store.save(episode.model_copy(update={"approved_cut_version": None, "approval_review_id": None}),
                   expected_version=token)
        revoked.append(episode_id)


def _commit_upstream(store: ProjectStore, plan: dict[str, Any]) -> bool:
    cls = _ENTITY_TYPES[plan["entity_type"]]
    after = cls.model_validate(plan["after"])
    before = cls.model_validate(plan["before"])
    record_id = _record_id(after)
    episode_id = getattr(after, "episode_id", None)
    current, token = store.load_with_version(cls, record_id, episode_id=episode_id)
    if _same(current, plan["after"]):
        return False
    if current.version != before.version or not _same(current, plan["before"]):
        raise RuntimeError(f"upstream {record_id} diverged from both planned before and after payloads")
    # A changed lifecycle token on the same content is safe only when it is still the planned CAS token.
    if token != plan["expected_version"]:
        raise VersionConflict(f"cascade/{plan['cascade_id']}/{record_id}", plan["expected_version"], token, current)
    store.revise(after, expected_version=token)
    return True


def _stale_affected(store: ProjectStore, plan: dict[str, Any], changed: list[str]) -> None:
    if plan["entity_type"] == "Shot":
        return  # the planned successor is itself the fresh draft for this Shot
    for row in plan["affected"]:
        current, token = store.load_with_version(Shot, row["shot_id"], episode_id=row["episode_id"])
        if current.version != row["version"]:
            raise RuntimeError(
                f"affected Shot {row['episode_id']}/{row['shot_id']} diverged from planned version {row['version']}"
            )
        if current.status is ShotStatus.STALE:
            continue
        if current.status is ShotStatus.APPROVED:
            check_transition(current.status, ShotStatus.STALE, TransitionContext())
            store.update(current.model_copy(update={"status": ShotStatus.STALE}), expected_version=token)
            changed.append(current.shot_id)
        # Review and draft records retain their lifecycle state; the journal fences them.


def _roll_forward(store: ProjectStore, plan: dict[str, Any]) -> tuple[str, ...]:
    phase = "gate revocation"
    revoked: list[str] = []
    upstream_committed = False
    stale_ids: list[str] = []
    try:
        _revoke_gates(store, plan, revoked)
        phase = "upstream commit"
        upstream_committed = _commit_upstream(store, plan)
        phase = "Shot staleness"
        _stale_affected(store, plan, stale_ids)
        phase = "completion marker"
        store.complete_cascade(plan["cascade_id"])
        return tuple(stale_ids)
    except VersionConflict as exc:
        progress = (
            f"phase={phase}, gates revoked={list(revoked)}, "
            f"upstream newly committed={upstream_committed}, shots newly stale={list(stale_ids)}"
        )
        conflict_message = (
            f"cascade {plan['cascade_id']} remains pending ({progress}); "
            f"resolve conflict and resume_invalidations(store); cause: {exc}"
        )
        exc.args = (conflict_message,)
        raise
    except Exception as exc:
        progress = (
            f"phase={phase}, gates revoked={list(revoked)}, "
            f"upstream newly committed={upstream_committed}, shots newly stale={list(stale_ids)}"
        )
        raise JobError(
            JobErrorKind.INTERNAL,
            f"cascade {plan['cascade_id']} remains pending ({progress}); "
            f"retry resume_invalidations(store); cause: {exc}", cause=exc,
        ) from exc


def resume_invalidations(store: ProjectStore) -> tuple[str, ...]:
    """Roll all pending cascade plans forward under the shared service command lock."""
    with store.command():
        validated_cascades(store)
        plans = store.cascades(pending_only=True)
        for plan in plans:
            for episode_id in plan["episode_ids"]:
                try:
                    store.load(Episode, episode_id)
                except Exception as exc:
                    raise JobError(JobErrorKind.INVALID_MANIFEST,
                                   f"cascade {plan['cascade_id']} references invalid Episode {episode_id}; cause: {exc}",
                                   cause=exc) from exc
            _roll_forward(store, plan)
        return tuple(plan["cascade_id"] for plan in plans)


def _revise(store: ProjectStore, revised: VersionedEntity, expected_version: int,
            episode_ids: Iterable[str] = ()) -> tuple[str, ...]:
    _pending_error(store)
    before, token = store.validate_revision(revised, expected_version=expected_version)
    shots = _shots(store, set(episode_ids) | ({revised.episode_id} if isinstance(revised, Script) else set()))
    ids = [revised.episode_id] if isinstance(revised, Shot) else []
    plan = _plan(store, before, revised, token, shots, set(episode_ids) | set(ids))
    try:
        store.record_cascade(plan)
    except Exception as exc:
        cause = exc.__cause__ or exc
        mapped = JobError.from_exception(cause)
        mapped.message = (
            f"cascade {plan['cascade_id']} plan persistence failed; if the plan exists, "
            f"call resume_invalidations(store); cause: {cause}"
        )
        mapped.args = (mapped.message,)
        mapped.__cause__ = cause
        raise mapped from exc
    return _roll_forward(store, plan)


def revise_script(store: ProjectStore, revised: Script, *, expected_version: int) -> tuple[str, ...]:
    with store.command():
        return _revise(store, revised, expected_version)


def revise_location(store: ProjectStore, revised: Location, *, expected_version: int) -> tuple[str, ...]:
    with store.command():
        return _revise(store, revised, expected_version, store.episode_directories())


def revise_camera_profile(
    store: ProjectStore, revised: CameraProfile, *, expected_version: int,
) -> tuple[str, ...]:
    with store.command():
        return _revise(store, revised, expected_version, store.episode_directories())


def revise_shot(store: ProjectStore, revised: Shot, *, expected_version: int) -> Shot:
    with store.command():
        _revise(store, revised, expected_version, [revised.episode_id])
        return revised


def _measured_dvr_2005_defaults() -> tuple[CameraProfile, PostProfile]:
    """Materialize the spike's measured DVR profile without rebuilding its values."""
    repository = Path(__file__).resolve().parents[2]
    profile_path = repository / "spikes" / "01_postprofile_determinism" / "post_profile.json"
    results_path = repository / "spikes" / "01_postprofile_determinism" / "results_dvr-2005.json"
    profile_bytes = profile_path.read_bytes()
    results_bytes = results_path.read_bytes()
    measured = json.loads(profile_bytes)
    results = json.loads(results_bytes)
    if measured.get("profile_id") != "dvr-2005":
        raise ValueError(f"measured default profile id is not dvr-2005: {measured.get('profile_id')!r}")

    schedule = [ArtifactScheduleEntry.model_validate(item)
                for item in measured["artifact_schedule"]]
    stages: list[PostStage] = []
    overlay: PostProfileOverlay | None = None
    for source in measured["stages"]:
        params = dict(source.get("params", {}))
        if "from_schedule" in source:
            params["from_schedule"] = source["from_schedule"]
        is_overlay = source["filter"] == "drawtext"
        if is_overlay:
            overlay = PostProfileOverlay.model_validate({
                **params,
                "placement": "in_recording",
                "font_relative": "assets/fonts/overlay.ttf",
            })
        stages.append(PostStage(
            name=source["name"],
            filter=source["filter"],
            params=params,
            seed_source="shot_degrade_seed" if source.get("seeded") is True else None,
            composite_stage="in_recording" if is_overlay else None,
        ))

    encode = PostProfileEncode.model_validate(measured["encode"])
    profile_digest = hashlib.sha256(profile_bytes).hexdigest()
    results_digest = hashlib.sha256(results_bytes).hexdigest()
    provenance = (
        "Measured 2026-11-03 in spikes/01_postprofile_determinism: "
        f"post_profile.json sha256={profile_digest}; "
        f"results_dvr-2005.json sha256={results_digest}. The project-local dvr-2005 "
        "CameraProfile mirrors the measured post-only codec, dimensions, display ratio, "
        "frame rate and schedule. Its rebuild-triggering camera optics, placement and "
        "lighting are not measured by spike 01 and retain CameraProfile's declared "
        "domain defaults."
    )
    post_profile = PostProfile(
        name=measured["profile_id"],
        semver=measured["profile_version"],
        stages=stages,
        pinned_binaries={"ffmpeg": normalize_ffmpeg_identity(results["ffmpeg_full_identity"])},
        filter_graph_hash=results["filter_graph_hash"],
        output_fps=measured["output_fps"],
        storage_resolution=tuple(measured["storage_resolution"]),
        sample_aspect_ratio=measured["sample_aspect_ratio"],
        artifact_schedule=schedule,
        overlay=overlay,
        encode=encode,
        provenance=provenance,
    )
    camera_profile = CameraProfile(
        camera_id=measured["profile_id"],
        profile_name=measured["profile_id"],
        camera_mode="cctv",
        codec=encode.codec,
        storage_resolution=tuple(measured["storage_resolution"]),
        display_aspect_ratio=measured["display_aspect_ratio"],
        fps=measured["output_fps"],
        frame_drop_schedule=schedule,
    )
    return camera_profile, post_profile


def _unmeasured_low_light_bw_defaults(*, ffmpeg_identity: str) -> tuple[CameraProfile, PostProfile]:
    """Build the §4.2 Low-light B&W pair with every unmeasured choice called out."""
    profile_id = "low-light-bw"
    camera_id = "CAM 03"
    clock_epoch = 1131984000
    output_fps = 10
    storage_resolution = (352, 288)
    sample_aspect_ratio = "12/11"

    overlay = PostProfileOverlay(
        camera_id=camera_id,
        clock_epoch=clock_epoch,
        fontsize=16,
        font_relative="assets/fonts/overlay.ttf",
        x=8,
        y="h-24",
        placement="in_recording",
        indicator=PostProfileOverlayIndicator(
            kind="battery_rec",
            fontsize=12,
            x=8,
            y=8,
            placement="in_recording",
        ),
    )
    stages = [
        PostStage(name="resample", filter="scale",
                  params={"w": storage_resolution[0], "h": storage_resolution[1], "flags": "bicubic"}),
        PostStage(name="decimate", filter="fps", params={"fps": output_fps}),
        PostStage(name="restructure", filter="select", params={"from_schedule": "drop+freeze"}),
        PostStage(name="clock_timebase", filter="setpts", params={"from_schedule": "clock"}),
        PostStage(
            name="overlay_clock",
            filter="drawtext",
            params={
                "element": "clock",
                "camera_id": camera_id,
                "clock_epoch": clock_epoch,
                "fontsize": overlay.fontsize,
                "x": overlay.x,
                "y": overlay.y,
            },
            composite_stage="in_recording",
        ),
        PostStage(
            name="overlay_battery_rec",
            filter="drawtext",
            params={"element": "battery_rec"},
            composite_stage="in_recording",
        ),
        PostStage(name="output_timebase", filter="setpts",
                  params={"timebase": "1/output_fps", "pts": "N"}),
        PostStage(name="shadow_noise", filter="noise", params={"alls": 24, "allf": "t+u"},
                  seed_source="shot_degrade_seed"),
        PostStage(name="set_sar", filter="setsar", params={"sar": sample_aspect_ratio}),
    ]
    encode = PostProfileEncode(
        codec="libx264", preset="medium", crf=30, pix_fmt="yuv420p", threads=1,
    )
    post_profile = PostProfile(
        name=profile_id,
        semver="0.1.0",
        stages=stages,
        pinned_binaries={"ffmpeg": ffmpeg_identity},
        output_fps=output_fps,
        storage_resolution=storage_resolution,
        sample_aspect_ratio=sample_aspect_ratio,
        artifact_schedule=[],
        overlay=overlay,
        encode=encode,
        provenance=(
            "Built from PRD §4.2: 352x288 storage, 4:3 display, 8-12 fps, monochrome night, "
            "strong shadow noise, and clipped infrared-like highlights only if justified. "
            "UNMEASURED PLACEHOLDER values for this profile: fps=10; noise alls=24; "
            "scale flags=bicubic and noise allf=t+u (inherited from the measured DVR chain "
            "but unmeasured for this profile); "
            "profile semver=0.1.0; encode codec=libx264, preset=medium, crf=30, "
            "pix_fmt=yuv420p, threads=1; overlay camera_id=CAM 03, clock_epoch=1131984000, "
            "clock fontsize=16 at (8,h-24), and battery/REC fontsize=12 at (8,8), with "
            "display text 'REC [||||]'. The overlay font path is the existing project-local "
            "assets/fonts/overlay.ttf convention. Drawtext styling fontcolor=white, box=1, "
            "boxcolor=black@0.4 is inherited and unmeasured for this profile. The font file "
            "must be supplied project-locally. Rebuild-triggering optics and practical "
            "lighting inherit CameraProfile domain defaults and are UNMEASURED PLACEHOLDERS: "
            "field_of_view_mm=12, barrel_distortion=0, exposure_ev=0, "
            "practical_light_color_k=3200, practical_light_intensity=1.0; the unused "
            "CameraProfile bitrate_kbps=800 and its zero/empty post-only defaults are also "
            "UNMEASURED PLACEHOLDERS and do not override the PostProfile chain. "
            "No highlight clipping is enabled by default. The FFmpeg executable identity "
            "is the machine measurement recorded by spike 01; no profile-specific visual "
            "or encoding result has been measured."
        ),
    )
    camera_profile = CameraProfile(
        camera_id=profile_id,
        profile_name=profile_id,
        camera_mode="cctv",
        monochrome_night=True,
        codec=encode.codec,
        storage_resolution=storage_resolution,
        display_aspect_ratio="4:3",
        fps=float(output_fps),
    )
    return camera_profile, post_profile


def _unmeasured_consumer_2002_defaults(*, ffmpeg_identity: str) -> tuple[CameraProfile, PostProfile]:
    """Build the §4.2 Consumer-2002 pair with every unmeasured choice identified."""
    profile_id = "consumer-2002"
    output_fps = 30
    storage_resolution = (640, 480)
    sample_aspect_ratio = "1/1"
    noise_strength = 24

    stages = [
        PostStage(name="resample", filter="scale",
                  params={"w": storage_resolution[0], "h": storage_resolution[1], "flags": "bicubic"}),
        PostStage(name="decimate", filter="fps", params={"fps": output_fps}),
        PostStage(name="restructure", filter="select", params={"from_schedule": "drop+freeze"}),
        PostStage(name="clock_timebase", filter="setpts", params={"from_schedule": "clock"}),
        PostStage(name="output_timebase", filter="setpts",
                  params={"timebase": "1/output_fps", "pts": "N"}),
        PostStage(name="sensor_noise", filter="noise",
                  params={"alls": noise_strength, "allf": "t+u"},
                  seed_source="shot_degrade_seed"),
    ]
    encode = PostProfileEncode(
        codec="libx264", preset="medium", crf=30, pix_fmt="yuv420p", threads=1,
    )
    post_profile = PostProfile(
        name=profile_id,
        semver="0.1.0",
        stages=stages,
        pinned_binaries={"ffmpeg": ffmpeg_identity},
        output_fps=output_fps,
        storage_resolution=storage_resolution,
        sample_aspect_ratio=sample_aspect_ratio,
        artifact_schedule=[],
        overlay=None,
        encode=encode,
        provenance=(
            "Built from PRD §4.2: Non-CCTV poor handheld camera, 640x480 storage, "
            "4:3 display, 15-30 fps, autofocus hunting, rolling movement, and sensor noise; "
            "this profile is rebuild-triggering. The spec supplies the resolution, display "
            "ratio, frame-rate range, intended handheld mode, and no default timestamp. "
            "UNMEASURED PLACEHOLDER values: output_fps=30 (integer selected from the "
            "specified range); sensor_noise alls=24 and allf=t+u; scale flags=bicubic; "
            "semver=0.1.0; encode codec=libx264, preset=medium, crf=30, "
            "pix_fmt=yuv420p, threads=1. Storage sample aspect ratio 1/1 follows the "
            "spec's square-pixel 640x480 rule. The profile has no burned-in overlay: "
            "overlay=None and no drawtext stage. analog_artifacts remains empty/off. "
            "The FFmpeg identity is the machine pin measured by spike 01, not a "
            "Consumer-2002 behavior measurement. CameraProfile's rebuild-triggering "
            "optics and lighting retain unmeasured domain defaults: field_of_view_mm=12, "
            "barrel_distortion=0, exposure_ev=0, practical_light_color_k=3200, "
            "practical_light_intensity=1.0, camera_position=None, depth_of_field=None, "
            "and monochrome_night=False. Its post-only bitrate_kbps=800, "
            "noise_model={}, chroma_smearing=0, block_artifacts=0, sharpening=0, "
            "grade_white_balance={}, overlay_model={}, decimation_pattern=None, and "
            "frame_drop_schedule=[] are inherited/empty defaults and are also "
            "UNMEASURED PLACEHOLDERS. camera_mode='non_cctv' enables deterministic, "
            "profile-derived camera sway; camera_mount='handheld' remains descriptive and "
            "does not drive motion. Motion bounds: maximum positional drift=0.025 m, "
            "yaw=±0.35 degrees, pitch=±0.25 degrees, keyframe interval=2.0-4.0 seconds. "
            "These deliberately small bounds represent a standing operator and have not "
            "been measured against reference footage. "
            "No profile-specific image, motion, noise, or encoding result has been measured."
        ),
    )
    camera_profile = CameraProfile(
        camera_id=profile_id,
        profile_name=profile_id,
        camera_mode="non_cctv",
        camera_mount="handheld",
        codec=encode.codec,
        storage_resolution=storage_resolution,
        display_aspect_ratio="4:3",
        fps=float(output_fps),
    )
    return camera_profile, post_profile


def create_project(store: ProjectStore, project: Project) -> Project:
    """Create a project with the measured DVR and unmeasured Low-light/Consumer pairs."""
    camera_profile, post_profile = _measured_dvr_2005_defaults()
    low_light_camera, low_light_post = _unmeasured_low_light_bw_defaults(
        ffmpeg_identity=post_profile.pinned_binaries["ffmpeg"],
    )
    consumer_camera, consumer_post = _unmeasured_consumer_2002_defaults(
        ffmpeg_identity=post_profile.pinned_binaries["ffmpeg"],
    )
    with store.command():
        project = store.validate_entity_payload(project)
        store.save(project, expected_version=0)
        store.create(camera_profile)
        store.save(post_profile, expected_version=0)
        store.create(low_light_camera)
        store.save(low_light_post, expected_version=0)
        store.create(consumer_camera)
        store.save(consumer_post, expected_version=0)
        store._root.make_dir("assets/fonts")
        return project


def create_camera_profile(store: ProjectStore, profile: CameraProfile) -> CameraProfile:
    """Persist a new agent-authored CameraProfile without downstream invalidation."""
    with store.command():
        profile = store.validate_entity_payload(profile)
        store.load(Project)
        store.create(profile)
        return profile


def create_location(store: ProjectStore, location: Location) -> Location:
    """Create a project-local Location record with placements at content version 1."""
    with store.command():
        location = store.validate_entity_payload(location)
        store.load(Project)
        store.create(location)
        return location


def create_post_profile(store: ProjectStore, profile: PostProfile) -> PostProfile:
    """Persist a new agent-authored PostProfile without overwriting an existing one."""
    with store.command():
        profile = store.validate_entity_payload(profile)
        store.load(Project)
        store.save(profile, expected_version=0)
        return profile


def create_episode(store: ProjectStore, episode: Episode) -> Episode:
    """Persist a new Episode under the service command boundary."""
    with store.command():
        episode = store.validate_entity_payload(episode)
        store.load(Project, episode.project_id)
        store.save(episode, expected_version=0)
        return episode


def draft_script(store: ProjectStore, script: Script) -> Script:
    """Persist an agent-authored Script at version 1 without invalidating anything."""
    with store.command():
        script = store.validate_entity_payload(script)
        store.load(Episode, script.episode_id)
        store.create(script)
        return script


def propose_shots(store: ProjectStore, shots: list[Shot]) -> tuple[Shot, ...]:
    """Append a set of new draft Shots; report any writes if a set fails partway."""
    with store.command():
        validated = tuple(store.validate_entity_payload(shot) for shot in shots)
        episode_ids = {shot.episode_id for shot in validated}
        episodes: dict[str, tuple[Episode, int]] = {}
        for episode_id in sorted(episode_ids):
            episodes[episode_id] = store.load_with_version(Episode, episode_id)
        # Resolve all ordinary create conflicts before the first append. A pre-existing
        # Shot therefore keeps the normal VersionConflict/409 mapping and cannot leave
        # earlier proposals from the same request written just before the conflict.
        for shot in validated:
            if store.versions(Shot, shot.shot_id, episode_id=shot.episode_id):
                current, token = store.load_with_version(
                    Shot, shot.shot_id, episode_id=shot.episode_id,
                )
                raise VersionConflict(
                    f"episodes/{shot.episode_id}/shots/{shot.shot_id}", 0, token, current,
                )

        def append_to_episode_manifest(proposed: Iterable[Shot]) -> None:
            additions: dict[str, list[str]] = {}
            for shot in proposed:
                additions.setdefault(shot.episode_id, []).append(shot.shot_id)
            for episode_id, shot_ids_to_add in additions.items():
                episode, token = episodes[episode_id]
                shot_ids = list(episode.shot_ids)
                known_ids = set(shot_ids)
                for shot_id in shot_ids_to_add:
                    if shot_id not in known_ids:
                        shot_ids.append(shot_id)
                        known_ids.add(shot_id)
                if shot_ids != episode.shot_ids:
                    updated = episode.model_copy(update={
                        "shot_ids": shot_ids,
                        "approved_cut_version": None,
                        "approval_review_id": None,
                    })
                    store.save(updated, expected_version=token)

        written: list[Shot] = []
        for index, shot in enumerate(validated):
            try:
                store.create(shot)
            except Exception as exc:
                pending = [f"{item.episode_id}/{item.shot_id}" for item in validated[index:]]
                completed = [f"{item.episode_id}/{item.shot_id}" for item in written]
                append_to_episode_manifest(written)
                raise ShotProposalWriteFailure(completed, pending, exc) from exc
            written.append(shot)

        append_to_episode_manifest(written)
        return tuple(written)
