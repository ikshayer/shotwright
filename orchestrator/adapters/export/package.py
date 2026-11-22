"""Publish an immutable, independently reopenable project package."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from orchestrator.domain.entities import Episode, EpisodeOutput, Export, Project, Review, Timeline
from orchestrator.domain.state import ReviewApprovalError, check_review_approval
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict

from .package_media import DeliveryEvidence, encode_delivery
from .snapshot import SnapshotFile, plan_snapshot

_DELIVERY = "delivery.mp4"


@dataclass(frozen=True)
class BoundCut:
    episode: Episode
    timeline: Timeline
    timeline_version: int
    output: EpisodeOutput
    review: Review


def export_project(
    store: ProjectStore, settings: Settings, *, episode_id: str, cut_output_id: str,
    expected_version: int,
) -> dict[str, str]:
    """Stage a source snapshot and delivery, then publish after a fresh CAS check."""
    if type(expected_version) is not int or expected_version <= 0:
        raise _invalid("export expected_version must be a positive Episode mutation token")
    root = store._root
    with store.command():
        project = store.load(Project)
        selected = _bind_cut(
            store, episode_id, cut_output_id, expected_version,
            agent_may_approve=settings.approvals.agent_may_approve,
        )
        if project.project_id != selected.episode.project_id:
            raise _invalid("the selected Episode belongs to a different Project")
        files = plan_snapshot(store)
    _validate_snapshot_plan(files, selected.output)

    export_id = uuid.uuid4().hex
    package = f"exports/{export_id}"
    stage = f"exports/.{export_id}"
    _canonical_path(root, stage)
    if _canonical_path(root, package).exists():
        raise _invalid(f"export already exists: {package}")
    try:
        root.write_with(stage, lambda path: path.mkdir(exist_ok=False))
    except OSError as exc:
        raise JobError.from_exception(exc) from exc
    try:
        staged_root = ProjectRoot(root.make_dir(f"{stage}/project"))
        copied = [_copy_file(root, staged_root, item) for item in files]
        _verify_reopen(
            staged_root, project, selected, expected_version,
            agent_may_approve=settings.approvals.agent_may_approve,
        )

        temporary = root.make_dir(f"{stage}/.media-tmp")
        output_path = staged_root.write_with(_DELIVERY, lambda path: None)
        evidence = encode_delivery(
            settings.binaries.ffmpeg_exe,
            source=_canonical_path(staged_root, selected.output.relative_path),
            output=output_path,
            temporary_directory=temporary,
            expected_resolution=selected.output.storage_resolution,
            expected_frame_rate=selected.output.output_fps,
            expected_sar=selected.output.sample_aspect_ratio,
            timeline_has_audio=bool(selected.timeline.audio_tracks),
            budget_seconds=settings.budget.per_shot_wall_clock_seconds,
        )
        _remove_stage_directory(root, f"{stage}/.media-tmp")
        copied.append(_file_evidence(staged_root, _DELIVERY))
        validation = {
            "project_reopened": True,
            "timeline_receipt_verified": True,
            "approval_verified": True,
            "file_checksums_verified": True,
            "ffprobe_verified": True,
        }
        manifest = _manifest(
            export_id, project.project_id, expected_version, selected, copied,
            evidence, validation,
        )
        manifest_bytes = _json_bytes(manifest)
        root.write_bytes(f"{stage}/manifest.json", manifest_bytes)
        receipt = Export(
            export_id=export_id,
            output_path=f"{package}/project/{_DELIVERY}",
            codec="h264/aac",
            resolution=evidence.media.resolution,
            frame_rate=float(evidence.media.frame_rate),
            # EpisodeOutput binds a profile name but does not record its semver.
            post_profile_version="unrecorded",
            ffmpeg_version=evidence.ffmpeg_identity,
            filter_graph_hash=evidence.filter_graph_hash,
            manifest_checksum=hashlib.sha256(manifest_bytes).hexdigest(),
            validation_results=validation,
            silent_audio_added=evidence.silent_audio_added,
        )
        store.validate_entity_payload(receipt)
        record = receipt.model_dump(mode="json") | {"_store_version": 1}
        root.write_bytes(f"{stage}/export.json", _json_bytes(record))

        with store.command():
            current = _bind_cut(
                store, episode_id, cut_output_id, expected_version,
                agent_may_approve=settings.approvals.agent_may_approve,
            )
            if current != selected:
                raise _invalid("the Episode, Timeline, cut receipt, or approval changed during export")
            _canonical_path(root, package)

            def publish(target: Path) -> None:
                if target.exists():
                    raise FileExistsError(f"export already exists: {package}")
                os.rename(_canonical_path(root, stage), target)

            root.write_with(package, publish)
    except BaseException as exc:
        try:
            _remove_stage_directory(root, stage)
        except OSError as cleanup:
            raise JobError.from_exception(cleanup) from exc
        if isinstance(exc, OSError):
            raise JobError.from_exception(exc) from exc
        raise
    return {
        "export_id": export_id,
        "package_path": f"{package}/project",
        "output_path": receipt.output_path,
        "manifest_path": f"{package}/manifest.json",
        "receipt_path": f"{package}/export.json",
    }


def _bind_cut(
    store: ProjectStore, episode_id: str, cut_output_id: str, expected_version: int,
    *, agent_may_approve: bool,
) -> BoundCut:
    episode, token = store.load_with_version(Episode, episode_id)
    if token != expected_version:
        raise VersionConflict(
            f"episodes/{episode_id}/episode.json", expected_version, token, episode,
        )
    timeline, timeline_version = store.load_with_version(Timeline, episode_id)
    if episode.episode_id != episode_id or timeline.episode_id != episode_id:
        raise _invalid("the persisted Episode or Timeline identity disagrees with the requested Episode")
    cuts = store.list_episode_outputs(episode_id)
    selected = next((row for row in cuts if row.output_id == cut_output_id), None)
    if selected is None:
        raise RecordNotFound(f"Episode {episode_id!r} has no cut output {cut_output_id!r}")
    if (selected.timeline_store_version != timeline_version
            or selected.timeline_sha256 != timeline.content_sha256):
        raise _invalid("the selected cut does not match the current persisted Timeline version and SHA-256")
    if episode.approved_cut_output_id is None:
        raise _invalid(
            "this Episode has no cut-specific approval; re-approve the cut with approve_cut"
        )
    if episode.approved_cut_version != timeline_version or episode.approval_review_id is None:
        raise _invalid("the selected Timeline version needs gate-C approval before export")
    approved = next(
        (row for row in cuts if row.output_id == episode.approved_cut_output_id),
        None,
    )
    if approved is None:
        raise _invalid(
            f"approved cut receipt {episode.approved_cut_output_id!r} is missing"
        )
    if (approved.timeline_store_version != timeline_version
            or approved.timeline_sha256 != timeline.content_sha256):
        raise _invalid(
            f"approved cut {approved.output_id!r} does not match the current Timeline"
        )
    if (selected.output_id != approved.output_id
            and selected.content_sha256 != approved.content_sha256):
        raise _invalid(
            f"selected cut {selected.output_id!r} is not the approved cut "
            f"{approved.output_id!r} and is not byte-identical"
        )
    try:
        review = store.load(Review, episode.approval_review_id)
    except RecordNotFound as exc:
        raise _invalid(f"gate-C approval Review {episode.approval_review_id!r} is missing") from exc
    if review.review_id != episode.approval_review_id:
        raise _invalid("gate-C approval Review identity disagrees with the Episode")
    try:
        check_review_approval(
            review,
            target_kind="episode_cut",
            target_id=episode_id,
            target_version=timeline_version,
            episode_id=episode_id,
            agent_may_approve=agent_may_approve,
        )
    except ReviewApprovalError as exc:
        raise _invalid(f"gate-C approval Review does not match the selected cut: {exc}") from exc
    return BoundCut(episode, timeline, timeline_version, selected, review)


def _validate_snapshot_plan(files: tuple[SnapshotFile, ...], source: EpisodeOutput) -> None:
    planned = {item.relative_path.casefold(): item for item in files}
    if len(planned) != len(files) or _DELIVERY.casefold() in planned:
        raise _invalid("snapshot contains duplicate paths or occupies the reserved delivery.mp4 path")
    project = planned.get("project.json")
    cut = planned.get(source.relative_path.replace("\\", "/").casefold())
    if project is None or not project.expected_sha256:
        raise _invalid("snapshot is missing the project manifest or its SHA-256")
    if cut is None or cut.expected_sha256 != source.content_sha256:
        raise _invalid("snapshot did not bind the selected cut and its SHA-256")


def _canonical_path(root: ProjectRoot, relative: str) -> Path:
    normal = relative.replace("\\", "/")
    path = root.resolve(normal)
    if path != root.path.joinpath(*normal.split("/")):
        raise _invalid(f"package path {relative!r} resolves through an alias")
    return path


def _copy_file(source_root: ProjectRoot, destination_root: ProjectRoot, item: SnapshotFile) -> dict[str, object]:
    source = _canonical_path(source_root, item.relative_path)
    _canonical_path(destination_root, item.relative_path)
    digest = hashlib.sha256()
    count = 0

    def copy(target: Path) -> None:
        nonlocal count
        with source.open("rb") as stream, target.open("xb") as staged:
            before = os.fstat(stream.fileno())
            while chunk := stream.read(1024 * 1024):
                staged.write(chunk)
                digest.update(chunk)
                count += len(chunk)
            after = os.fstat(stream.fileno())
            if before.st_size != count or before.st_mtime_ns != after.st_mtime_ns or after.st_size != count:
                raise _invalid(f"source artifact {item.relative_path!r} changed while being copied")

    if not source.is_file():
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"package source file is missing: {item.relative_path}")
    destination_root.write_with(item.relative_path, copy)
    sha256 = digest.hexdigest()
    if item.expected_sha256 is not None and sha256 != item.expected_sha256:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"package source SHA-256 mismatch: {item.relative_path}")
    return {"path": item.relative_path, "sha256": sha256, "size_bytes": count}


def _file_evidence(root: ProjectRoot, relative: str) -> dict[str, object]:
    with _canonical_path(root, relative).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        size = os.fstat(stream.fileno()).st_size
    return {"path": relative, "sha256": digest, "size_bytes": size}


def _verify_reopen(
    destination: ProjectRoot,
    expected: Project,
    cut: BoundCut,
    expected_version: int,
    *,
    agent_may_approve: bool,
) -> None:
    reopened = ProjectStore(destination)
    if reopened.load(Project) != expected:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       "the packaged project does not reopen as the snapshotted Project")
    rebound = _bind_cut(
        reopened, cut.episode.episode_id, cut.output.output_id, expected_version,
        agent_may_approve=agent_may_approve,
    )
    if rebound != cut:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       "the packaged Episode, Timeline, cut receipt, or approval is inconsistent")


def _manifest(
    export_id: str, project_id: str, expected_version: int, cut: BoundCut,
    files: list[dict[str, object]], evidence: DeliveryEvidence, validation: dict[str, bool],
) -> dict[str, object]:
    project_file = next(item for item in files if item["path"] == "project.json")
    return {
        "schema_version": 1,
        "export_id": export_id,
        "project_path": "project",
        "source": {
            "project_id": project_id,
            "project_sha256": project_file["sha256"],
            "episode_id": cut.episode.episode_id,
            "episode_store_version": expected_version,
            "timeline_store_version": cut.timeline_version,
            "timeline_sha256": cut.timeline.content_sha256,
            "gate_c_review_id": cut.review.review_id,
            "cut_output_id": cut.output.output_id,
            "cut_path": cut.output.relative_path,
            "cut_sha256": cut.output.content_sha256,
            "delivery_profile": cut.output.delivery_profile,
            "cut_ffmpeg_identity": cut.output.ffmpeg_identity,
        },
        "delivery": {
            "path": _DELIVERY,
            "ffmpeg_identity": evidence.ffmpeg_identity,
            "filter_graph_hash": evidence.filter_graph_hash,
            "silent_audio_added": evidence.silent_audio_added,
            **evidence.media.evidence(),
        },
        "validation_results": validation,
        "files": sorted(files, key=lambda file: str(file["path"])),
    }


def _json_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _remove_stage_directory(root: ProjectRoot, relative: str) -> None:
    directory = _canonical_path(root, relative)
    if directory.exists():
        root.write_with(relative, shutil.rmtree)


def _invalid(message: str) -> JobError:
    return JobError(JobErrorKind.INVALID_MANIFEST, message)

