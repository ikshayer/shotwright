"""Target validation and immutable report storage for advisory audio reviews."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.domain.entities import Asset, ShotOutput, ShotOutputKind
from orchestrator.platform.containment import ContainmentError, ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.perception import (
    PerceptionInvalid,
    _find_output,
    audio_review_demuxer,
    probe_audio_review_media,
    verify_audio_review_asset,
)
from orchestrator.store.json_store import ProjectStore, RecordNotFound

_AUDIO_ASSET_TYPES = frozenset({"audio", "voice"})
_NOTICE = (
    "Qwen responses are unverified model output. Check them against the measured metrics "
    "from measure_output or measure_asset and your own listening."
)
_LIMITATIONS = [
    "Advisory text only; Qwen responses are unverified and never grant approval.",
    "The staged input is a frozen copy; the native helper uses its own isolated runtime.",
]


def _finite_number(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _project_target(target: dict[str, Any]) -> dict[str, Any]:
    keys = ("episode_id", "output_id") if target["kind"] == "output" else ("asset_id",)
    return {
        "kind": target["kind"],
        **{key: target[key] for key in keys},
        "sha256": target["sha256"],
        "demuxer": target["demuxer"],
    }


def _valid_interval(interval: Any) -> bool:
    return (isinstance(interval, list) and len(interval) == 2
            and all(_finite_number(item) for item in interval)
            and interval[0] >= 0 and interval[1] > interval[0])


def _valid_metric_value(value: Any) -> bool:
    return value is None or type(value) is bool or _finite_number(value)


def _valid_metric_map(metrics: Any) -> bool:
    # The helper reports per-channel figures (RMS, peak, DC) as lists, with null for an
    # exactly-zero channel; every other metric is a scalar.
    return (isinstance(metrics, dict)
            and all(_valid_metric_value(value)
                    or (isinstance(value, list) and all(_valid_metric_value(item) for item in value))
                    for value in metrics.values()))


def resolve_audio_review_snapshot(
    store: ProjectStore,
    *,
    target: dict[str, Any],
    brief: str,
    mode: str,
    start_sec: float,
    duration_sec: float | None,
    max_interval_seconds: float,
    ffmpeg_exe: str | Path,
) -> dict[str, Any]:
    """Verify the target, demuxer, streams and interval before a job is submitted."""
    if not isinstance(brief, str) or "\x00" in brief or not 1 <= len(brief.strip()) <= 4000:
        raise PerceptionInvalid("brief must contain 1 through 4000 non-whitespace characters and no NUL")
    if mode not in {"audio", "joint"}:
        raise PerceptionInvalid("mode must be 'audio' or 'joint'")
    for name, value, allow_zero in (
        ("start_sec", start_sec, True), ("max_interval_seconds", max_interval_seconds, False),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PerceptionInvalid(f"{name} must be a finite number")
        if value < 0 if allow_zero else value <= 0:
            raise PerceptionInvalid(f"{name} is outside its valid range")
    if duration_sec is not None and (
        isinstance(duration_sec, bool) or not isinstance(duration_sec, (int, float))
        or not math.isfinite(duration_sec) or duration_sec <= 0
    ):
        raise PerceptionInvalid("duration_sec must be a finite positive number when supplied")

    kind = target.get("kind")
    if kind == "output":
        episode_id, output_id = target.get("episode_id"), target.get("output_id")
        if not isinstance(episode_id, str) or not isinstance(output_id, str):
            raise PerceptionInvalid("output target requires episode_id and output_id")
        output = store.find_output(episode_id, output_id)
        relative_path = output.relative_path
        if isinstance(output, ShotOutput) and output.kind in {ShotOutputKind.SCENE, ShotOutputKind.STILL}:
            raise PerceptionInvalid("scene and still outputs have no audio; review a rendered output instead")
        demuxer = audio_review_demuxer(relative_path, output=True)
        if demuxer is None:
            raise PerceptionInvalid(
                f"output extension {Path(relative_path).suffix or '(none)'!r} is not in the video demuxer allowlist"
            )
        digest = output.content_sha256
        target_identity = {
            "kind": "output", "episode_id": episode_id, "output_id": output_id,
            "sha256": digest, "demuxer": demuxer,
        }
    elif kind == "asset":
        asset_id = target.get("asset_id")
        if not isinstance(asset_id, str):
            raise PerceptionInvalid("asset target requires asset_id")
        asset = store.load(Asset, asset_id)
        if asset.type not in _AUDIO_ASSET_TYPES or not asset.file_path:
            raise PerceptionInvalid("audio review accepts only audio or voice Assets with a local file_path")
        demuxer = audio_review_demuxer(asset.file_path, output=False)
        if demuxer is None:
            raise PerceptionInvalid(
                f"audio Asset extension {Path(asset.file_path).suffix or '(none)'!r} is not supported"
            )
        verify_audio_review_asset(store, asset)
        relative_path = asset.file_path
        digest = asset.checksum
        target_identity = {
            "kind": "asset", "asset_id": asset_id, "sha256": digest, "demuxer": demuxer,
        }
    else:
        raise PerceptionInvalid("target.kind must be 'output' or 'asset'")

    probe = probe_audio_review_media(
        store, relative_path, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe,
    )
    if probe["audio"] is None:
        raise PerceptionInvalid("target has no audio stream")
    if mode == "joint" and probe["video"] is None:
        raise PerceptionInvalid("mode 'joint' requires a target with a video stream")
    media_duration = probe["duration_sec"]
    end_sec = media_duration if duration_sec is None else start_sec + duration_sec
    interval = end_sec - start_sec
    if start_sec >= media_duration or end_sec > media_duration or interval <= 0:
        raise PerceptionInvalid("requested interval must lie inside the media")
    if interval > max_interval_seconds:
        raise PerceptionInvalid(
            f"requested interval is {interval:g} seconds; qwen_review.max_interval_seconds is "
            f"{max_interval_seconds:g} seconds"
        )
    return {
        "target": target_identity,
        "source_path": relative_path,
        "brief": brief,
        "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        "brief_actor": "agent",
        "mode": mode,
        "start_sec": float(start_sec),
        "duration_sec": float(interval),
        "media_duration_seconds": float(media_duration),
    }


def with_brief_actor(snapshot: dict[str, Any], actor: str) -> dict[str, Any]:
    """Bind the author of the brief after target validation."""
    if actor not in {"agent", "creator"}:
        raise ValueError("audio review brief actor must be agent or creator")
    return {**snapshot, "brief_actor": actor}


def stage_audio_review_input(
    store: ProjectStore,
    repository_root: ProjectRoot,
    snapshot: dict[str, Any],
    *,
    audio_review_id: str,
) -> tuple[str, str]:
    """Reverify and stream a target into a contained, hash-checked repository copy."""
    target = snapshot["target"]
    try:
        if target["kind"] == "output":
            output = _find_output(store, target["episode_id"], target["output_id"])
            relative_path = output.relative_path
            current_hash = output.content_sha256
        elif target["kind"] == "asset":
            asset = store.load(Asset, target["asset_id"])
            if asset.type not in _AUDIO_ASSET_TYPES or not asset.file_path:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "audio Asset target changed since submission")
            verify_audio_review_asset(store, asset)
            relative_path = asset.file_path
            current_hash = asset.checksum
        else:
            raise JobError(JobErrorKind.INVALID_MANIFEST, "audio review target kind is invalid")
    except RecordNotFound as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "audio review target no longer exists", cause=exc) from exc
    except JobError as exc:
        if target["kind"] == "asset" and exc.kind is JobErrorKind.INVALID_MANIFEST:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"audio review Asset could not be re-verified: {exc.message}", cause=exc) from exc
        raise
    if relative_path != snapshot["source_path"] or current_hash != target["sha256"]:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       "audio review target identity or checksum changed since submission")

    source = store._root.resolve(relative_path)
    if not source.is_file():
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"audio review source is missing: {relative_path}")
    suffix = Path(relative_path).suffix.lower()
    staged_rel = f"_work/qwen_review_inputs/{audio_review_id}/source{suffix}"
    digest = hashlib.sha256()

    def copy_to(destination: Path) -> None:
        with source.open("rb") as reader, destination.open("xb") as writer:
            while block := reader.read(1024 * 1024):
                digest.update(block)
                writer.write(block)

    try:
        repository_root.write_with(staged_rel, copy_to)
    except OSError as exc:
        raise JobError(JobErrorKind.DISK_EXHAUSTED,
                       f"could not stage the audio review input: {exc}", cause=exc) from exc
    if digest.hexdigest() != target["sha256"]:
        repository_root.resolve(staged_rel).unlink(missing_ok=True)
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       "staged audio review input does not match the verified target checksum")
    return staged_rel, target["demuxer"]


def remove_staged_audio_review_input(repository_root: ProjectRoot, staged_rel: str) -> str | None:
    """Remove the staged copy and its empty directory, returning any cleanup failures."""
    failures = []
    try:
        repository_root.resolve(staged_rel).unlink(missing_ok=True)
    except (ContainmentError, OSError) as exc:
        failures.append(
            f"could not remove staged audio review input {staged_rel!r}: {type(exc).__name__}: {exc}"
        )
    directory_rel = Path(staged_rel).parent.as_posix()
    try:
        repository_root.resolve(directory_rel).rmdir()
    except FileNotFoundError:
        pass
    except (ContainmentError, OSError) as exc:
        failures.append(
            f"could not remove staged audio review input directory {directory_rel!r}: "
            f"{type(exc).__name__}: {exc}"
        )
    return "; ".join(failures) if failures else None


def persist_audio_review_report(
    store: ProjectStore,
    *,
    audio_review_id: str,
    job_id: str,
    snapshot: dict[str, Any],
    helper_report: dict[str, Any],
) -> str:
    """Write the project-visible immutable report once, through ProjectRoot."""
    root = store._root
    report_rel = f"audio_reviews/{audio_review_id}.json"
    record = dict(helper_report)
    record.update(
        audio_review_id=audio_review_id,
        job_id=job_id,
        created_at=datetime.now(UTC).isoformat(),
        target=snapshot["target"],
        brief={"text": snapshot["brief"], "sha256": snapshot["brief_sha256"]},
        brief_actor=snapshot["brief_actor"],
        mode=snapshot["mode"],
        helper_report_path=helper_report.get("report_path"),
        advisory=True,
    )
    serialized = json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2)
    root.make_dir("audio_reviews")

    def create_once(path: Path) -> None:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)

    try:
        root.write_with(report_rel, create_once)
    except FileExistsError as exc:
        raise JobError(JobErrorKind.INTERNAL,
                       f"refusing to overwrite immutable audio review {audio_review_id!r}", cause=exc) from exc
    return report_rel


def minimal_audio_review_report(
    *,
    audio_review_id: str,
    job_id: str,
    snapshot: dict[str, Any],
    status: str,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the visible terminal record when the helper did not return a report."""
    return {
        "schema_version": 1,
        "report_id": audio_review_id,
        "version": 1,
        "supersedes": None,
        "status": status,
        "request": {
            "media": snapshot["source_path"], "purpose": "audio_review",
            "brief": snapshot["brief"], "modality": "audio" if snapshot["mode"] == "audio" else "joint",
            "start_seconds": snapshot["start_sec"], "duration_seconds": snapshot["duration_sec"],
        },
        "media": {"path": snapshot["source_path"], "sha256": snapshot["target"]["sha256"]},
        "brief": {"text": snapshot["brief"], "sha256": snapshot["brief_sha256"]},
        "windows": [], "batches": [],
        "input_coverage": {
            "requested_interval": [snapshot["start_sec"], snapshot["start_sec"] + snapshot["duration_sec"]],
            "completed_intervals": [],
            "uncovered_intervals": [[snapshot["start_sec"], snapshot["start_sec"] + snapshot["duration_sec"]]],
        },
        "has_truncated_responses": False,
        "errors": errors,
        "limitations": list(_LIMITATIONS),
        "audio_review_id": audio_review_id,
        "job_id": job_id,
        "created_at": datetime.now(UTC).isoformat(),
        "target": snapshot["target"],
        "brief_actor": snapshot["brief_actor"],
        "mode": snapshot["mode"],
        "helper_report_path": None,
        "advisory": True,
    }


def persist_minimal_audio_review(
    store: ProjectStore,
    *,
    audio_review_id: str,
    job_id: str,
    snapshot: dict[str, Any],
    status: str,
    errors: list[dict[str, Any]],
) -> str:
    record = minimal_audio_review_report(
        audio_review_id=audio_review_id, job_id=job_id, snapshot=snapshot,
        status=status, errors=errors,
    )
    # The record is already in the final envelope shape; persistence still refuses replacement.
    root = store._root
    relative = f"audio_reviews/{audio_review_id}.json"
    serialized = json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2)
    root.make_dir("audio_reviews")

    def create_once(path: Path) -> None:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)

    try:
        root.write_with(relative, create_once)
    except FileExistsError as exc:
        raise JobError(JobErrorKind.INTERNAL,
                       f"refusing to overwrite immutable audio review {audio_review_id!r}", cause=exc) from exc
    return relative


def persist_minimal_audio_review_if_missing(
    store: ProjectStore,
    *,
    audio_review_id: str,
    job_id: str,
    snapshot: dict[str, Any],
    status: str,
    errors: list[dict[str, Any]],
) -> str:
    relative = f"audio_reviews/{audio_review_id}.json"
    if store._root.resolve(relative).exists():
        return relative
    try:
        return persist_minimal_audio_review(
            store, audio_review_id=audio_review_id, job_id=job_id,
            snapshot=snapshot, status=status, errors=errors,
        )
    except JobError as exc:
        if store._root.resolve(relative).exists():
            return relative
        raise exc


def _read_record(store: ProjectStore, audio_review_id: str) -> dict[str, Any]:
    relative = f"audio_reviews/{audio_review_id}.json"
    try:
        path = store._root.resolve(relative)
        if not path.is_file():
            raise RecordNotFound(f"audio review {audio_review_id!r} does not exist")
        value = json.loads(store._root.read_text(relative, encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("record must be a JSON object")
        if value.get("audio_review_id") != audio_review_id:
            raise ValueError("record id does not match its filename")
        if value.get("status") not in {"planned", "completed", "failed", "cancelled"}:
            raise ValueError("record has an invalid status")
        target = value.get("target")
        if not isinstance(target, dict) or target.get("kind") not in {"output", "asset"}:
            raise ValueError("record target is missing or invalid")
        required_target_ids = ("episode_id", "output_id") if target["kind"] == "output" else ("asset_id",)
        if any(not isinstance(target.get(name), str) or not target[name] for name in required_target_ids):
            raise ValueError("record target identity is invalid")
        if (not isinstance(target.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", target["sha256"])
                or not isinstance(target.get("demuxer"), str) or not target["demuxer"]):
            raise ValueError("record target checksum or demuxer is invalid")
        brief = value.get("brief")
        if (not isinstance(brief, dict) or not isinstance(brief.get("text"), str)
                or not isinstance(brief.get("sha256"), str)
                or hashlib.sha256(brief["text"].encode("utf-8")).hexdigest() != brief["sha256"]
                or value.get("brief_actor") not in {"agent", "creator"}):
            raise ValueError("record brief or brief author is invalid")
        if value.get("mode") not in {"audio", "joint"}:
            raise ValueError("record mode is invalid")
        if (not isinstance(value.get("windows"), list) or not isinstance(value.get("errors"), list)
                or not isinstance(value.get("limitations"), list)
                or any(not isinstance(item, str) for item in value["limitations"])
                or not isinstance(value.get("has_truncated_responses"), bool)
                or value.get("advisory") is not True
                or (value.get("helper_report_path") is not None
                    and not isinstance(value.get("helper_report_path"), str))):
            raise ValueError("record report fields are invalid")
        coverage = value.get("input_coverage")
        if not isinstance(coverage, dict):
            raise ValueError("record input coverage is invalid")
        for field_name in ("requested_interval", "completed_intervals", "uncovered_intervals"):
            intervals = coverage.get(field_name)
            valid = (_valid_interval(intervals) if field_name == "requested_interval" else
                     isinstance(intervals, list) and all(_valid_interval(interval) for interval in intervals))
            if not valid:
                raise ValueError(f"record {field_name} coverage is invalid")
        for row in value["windows"]:
            if (not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] < 1
                    or row.get("status") not in {"planned", "completed", "truncated", "failed", "not_run"}
                    or any(isinstance(row.get(name), bool) or not isinstance(row.get(name), (int, float))
                           or not math.isfinite(row[name]) for name in (
                               "input_start_seconds", "input_end_seconds", "source_start_seconds", "source_end_seconds",
                           ))
                    or any(row[name] < 0 for name in (
                        "input_start_seconds", "input_end_seconds", "source_start_seconds", "source_end_seconds",
                    ))
                    or row["input_end_seconds"] <= row["input_start_seconds"]
                    or row["source_end_seconds"] <= row["source_start_seconds"]
                    or (row.get("raw_response") is not None and not isinstance(row["raw_response"], str))
                    or (row.get("termination_reason") is not None
                        and not isinstance(row["termination_reason"], str))
                    or not isinstance(row.get("token_cap_reached"), bool)
                    or (row.get("audio_metrics") is not None
                        and not _valid_metric_map(row["audio_metrics"]))
                    or ("audio_interpretation_note" in row
                        and not isinstance(row["audio_interpretation_note"], str))):
                raise ValueError("record window is invalid")
        for error in value["errors"]:
            if (not isinstance(error, dict)
                    or any(not isinstance(error.get(name), str) for name in ("family", "kind", "message"))):
                raise ValueError("record error is invalid")
        created_at = datetime.fromisoformat(value["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value
    except RecordNotFound:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"audio review {audio_review_id!r} is unreadable or invalid: {exc}", cause=exc) from exc


def get_audio_review(store: ProjectStore, audio_review_id: str) -> dict[str, Any]:
    """Return the bounded human/agent projection, excluding native telemetry."""
    record = _read_record(store, audio_review_id)
    brief = record["brief"]
    if not isinstance(brief.get("text"), str) or not isinstance(record.get("brief_actor"), str):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "audio review brief projection is invalid")
    projected_windows = []
    for row in record["windows"]:
        if not isinstance(row, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "audio review window projection is invalid")
        projected = {
            "index": row.get("index"),
            "input_start_seconds": row.get("input_start_seconds"),
            "input_end_seconds": row.get("input_end_seconds"),
            "source_start_seconds": row.get("source_start_seconds"),
            "source_end_seconds": row.get("source_end_seconds"),
            "status": row.get("status"),
            "raw_response": row.get("raw_response"),
            "termination_reason": row.get("termination_reason"),
            "token_cap_reached": row.get("token_cap_reached"),
            "audio_metrics": row.get("audio_metrics"),
        }
        if "audio_interpretation_note" in row:
            projected["audio_interpretation_note"] = row["audio_interpretation_note"]
        projected_windows.append(projected)
    projected_errors = []
    for error in record["errors"]:
        if not isinstance(error, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "audio review error projection is invalid")
        projected_errors.append({key: error.get(key) for key in ("family", "kind", "message")})
    return {
        "audio_review_id": audio_review_id,
        "status": record["status"],
        "target": _project_target(record["target"]),
        "brief": {"text": brief["text"], "sha256": brief.get("sha256"), "actor": record["brief_actor"]},
        "mode": record.get("mode"),
        "input_coverage": {
            key: record["input_coverage"][key]
            for key in ("requested_interval", "completed_intervals", "uncovered_intervals")
        },
        "has_truncated_responses": record.get("has_truncated_responses", False),
        "windows": projected_windows,
        "errors": projected_errors,
        "limitations": record.get("limitations", []),
        "advisory": True,
        "notice": _NOTICE,
    }


def list_audio_reviews(
    store: ProjectStore,
    *,
    target_kind: str | None = None,
    target_id: str | None = None,
) -> list[dict[str, Any]]:
    """List project-visible audio reviews, newest first."""
    if target_id is not None and target_kind is None:
        raise PerceptionInvalid("target_id requires target_kind")
    if target_kind is not None and target_kind not in {"output", "asset"}:
        raise PerceptionInvalid("target_kind must be 'output' or 'asset'")
    directory = store._root.resolve("audio_reviews")
    if not directory.is_dir():
        return []
    records = []
    for path in directory.glob("*.json"):
        audio_review_id = path.stem
        record = _read_record(store, audio_review_id)
        target = _project_target(record["target"])
        if target_kind is not None and target.get("kind") != target_kind:
            continue
        id_field = "output_id" if target_kind == "output" else "asset_id"
        if target_id is not None and target.get(id_field) != target_id:
            continue
        records.append({
            "audio_review_id": audio_review_id,
            "status": record["status"],
            "target": target,
            "created_at": record["created_at"],
            "_sort": datetime.fromisoformat(record["created_at"]).astimezone(UTC),
        })
    records.sort(key=lambda row: (row["_sort"], row["audio_review_id"]), reverse=True)
    for record in records:
        record.pop("_sort")
    return records


def settings_for_audio_review(settings: Any) -> Any:
    """Apply one-attempt and Qwen-specific watchdog budgets to the queue settings."""
    policy = settings.qwen_review
    budget = settings.budget.model_copy(update={
        "retries_per_shot": 0,
        "per_shot_wall_clock_seconds": policy.wall_clock_seconds,
    })
    resources = settings.resources.model_copy(update={
        "startup_allowance_seconds": policy.startup_allowance_seconds,
        "throughput_floor_per_second": 0.0,
    })
    return settings.model_copy(update={"budget": budget, "resources": resources})
