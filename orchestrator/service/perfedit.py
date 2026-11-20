"""Request bodies, validation, composition and persistence for PerformanceEdit (FR-32).

An edited take sits beside the Shot like a speech take: it never changes the Shot, its
status, its approval or its outputs. Editing a voice does not change whose voice it is, so
rights are inherited from the source take unchanged.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ConfigDict, Field, StrictInt

from orchestrator.adapters.perfedit import (
    ALGORITHM_VERSION,
    PauseDetection,
    PerformanceEditRequest,
    PerformanceEditRequestError,
    PerformanceEditResult,
    PerformanceEditSpec,
)
from orchestrator.adapters.perfedit.detect import TakeAudio, detect_pauses, read_take
from orchestrator.adapters.perfedit.edit import plan_edit
from orchestrator.adapters.tts.core import sha256_file
from orchestrator.domain.entities import Asset, GenerationRun, Job, Shot
from orchestrator.platform.containment import ContainmentError
from orchestrator.service.take_files import discard_take, mark_report_discarded
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict

__all__ = [
    "PerformanceEditBody", "compose_performance_edit_request", "inspect_take_pauses",
    "persist_performance_edit_success", "persist_performance_edit_terminal",
]

_VOICE_ASSET_TYPES = frozenset({"voice", "audio"})


class PerformanceEditBody(PerformanceEditSpec):
    """The body of `apply_performance_edit`: the edit, the take it applies to, and the Shot's token."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    expected_version: StrictInt = Field(ge=1, description="The Shot's mutation token.")
    take_asset_id: str = Field(
        min_length=1,
        description="The voice take to edit: a registered 16-bit PCM mono .wav of type voice or "
                    "audio with a recorded voice_consent other than licensed_recording.",
    )


# --- the take ---------------------------------------------------------------------------


def _load_take(store: ProjectStore, asset_id: str, *, require_consent: bool) -> tuple[Asset, TakeAudio]:
    """Accept a take only if it is an intact, readable 16-bit PCM mono .wav voice.

    A licensed recording was licensed to lend its emotion, not to be heard, so it is
    refused for an edit exactly as it is refused as a speaker (FR-16).
    """
    try:
        asset = store.load(Asset, asset_id)
    except RecordNotFound as exc:
        raise PerformanceEditRequestError(f"take_asset_id {asset_id!r} is not a registered asset") from exc
    if asset.type not in _VOICE_ASSET_TYPES:
        raise PerformanceEditRequestError(
            f"take asset {asset_id!r} has type {asset.type!r}; it must be type 'voice' or 'audio'")
    if require_consent:
        if asset.voice_consent is None:
            raise PerformanceEditRequestError(
                f"take asset {asset_id!r} has no voice_consent recorded; a voice that will be heard "
                "needs consent, not merely a licence (FR-16)")
        if asset.voice_consent == "licensed_recording":
            raise PerformanceEditRequestError(
                f"take asset {asset_id!r} has voice_consent 'licensed_recording', which permits use "
                "as an emotion reference only; an edited clip is a voice that will be heard (FR-16)")
    if not asset.file_path or not asset.file_path.lower().endswith(".wav"):
        raise PerformanceEditRequestError(f"take asset {asset_id!r} must be a registered .wav file")
    if not asset.checksum:
        raise PerformanceEditRequestError(f"take asset {asset_id!r} has no registered checksum")
    try:
        path = store._root.resolve(asset.file_path)
    except ContainmentError as exc:
        raise PerformanceEditRequestError(f"take asset {asset_id!r} path is not contained: {exc}") from exc
    if not path.is_file():
        raise PerformanceEditRequestError(f"take asset {asset_id!r} file is missing: {asset.file_path}")
    data = store._root.read_bytes(asset.file_path)
    if hashlib.sha256(data).hexdigest() != asset.checksum:
        raise PerformanceEditRequestError(
            f"take asset {asset_id!r} bytes do not match its registered checksum; re-register the clip")
    return asset, read_take(data)


# --- inspection -------------------------------------------------------------------------


def inspect_take_pauses(store: ProjectStore, asset_id: str, detection: PauseDetection) -> dict[str, Any]:
    """Where a take pauses, so an agent can write an edit. Read-only and model-free."""
    _, take = _load_take(store, asset_id, require_consent=False)
    scan = detect_pauses(take, detection)
    rate = take.sample_rate

    def ms(samples: int) -> float:
        return round(samples * 1000 / rate, 1)

    return {
        "sample_rate": rate,
        "duration_ms": round(take.duration_ms, 1),
        "speech_onset_ms": None if scan.speech_onset is None else ms(scan.speech_onset),
        "pauses": [{"index": p.index, "start_ms": ms(p.start), "end_ms": ms(p.end),
                    "duration_ms": round(p.natural_ms(rate), 1)} for p in scan.pauses],
    }


# --- composition ------------------------------------------------------------------------


def compose_performance_edit_request(store: ProjectStore, *, shot_id: str, episode_id: str,
                                     body: PerformanceEditBody, run_id: str) -> PerformanceEditRequest:
    """Validate an edit body against the project and the take, and bind it to the Shot's version.

    Pause detection and the full edit plan run here, so an invalid edit creates no job.
    """
    if not isinstance(body, PerformanceEditBody):
        body = PerformanceEditBody.model_validate(body)
    spec = PerformanceEditSpec.model_validate(
        body.model_dump(mode="python", exclude={"expected_version", "take_asset_id"}))
    with store.command():
        shot, token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
        if token != body.expected_version:
            raise VersionConflict(f"episodes/{episode_id}/shots/{shot_id}",
                                  body.expected_version, token, shot)
        take, audio = _load_take(store, body.take_asset_id, require_consent=True)
        plan_edit(audio, take.asset_id, spec)
        return PerformanceEditRequest(
            shot_id=shot_id, shot_version=shot.version, run_id=run_id,
            take_asset_id=take.asset_id, take_path=take.file_path or "",
            take_sha256=take.checksum or "", edit=spec,
        )


# --- persistence ------------------------------------------------------------------------


def _asset_id(run_id: str) -> str:
    return f"perfedit-{run_id}"


def _settings_record(request: PerformanceEditRequest) -> dict[str, str | bool | int | float]:
    return {"algorithm_version": ALGORITHM_VERSION, "take_asset_id": request.take_asset_id,
            "take_sha256": request.take_sha256}


def _invocation(request: PerformanceEditRequest) -> str:
    return json.dumps(request.edit.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def persist_performance_edit_success(store: ProjectStore, job: Job, result: PerformanceEditResult,
                                     request: PerformanceEditRequest) -> Asset:
    """Save the GenerationRun and register the edited take, once its files are verified."""
    root = store._root
    if job.run_id != request.run_id:
        raise PerformanceEditRequestError("performance edit job run_id must match the submitted request")
    hashes: dict[str, str] = {}
    for relative, expected in ((result.take_path, result.take_sha256),
                               (result.report_path, result.report_sha256)):
        artifact = root.resolve(relative)
        if not artifact.is_file() or sha256_file(artifact) != expected:
            raise PerformanceEditRequestError(
                f"performance edit artifact is missing or its SHA256 differs: {relative}")
        hashes[relative] = expected
    log = root.resolve(result.log_path)
    if log.is_file():
        hashes[result.log_path] = sha256_file(log)
    metrics = {key: float(value) for key, value in job.resource_metrics.items()}
    metrics["wall_seconds"] = float(result.wall_seconds)
    generation = GenerationRun(
        run_id=request.run_id, tool="performance_edit", tool_version=result.tool_version,
        invocation=_invocation(request), seed=request.edit.seed, settings=_settings_record(request),
        input_paths=[request.take_path], output_paths=list(hashes), output_hashes=hashes,
        log_path=result.log_path if result.log_path in hashes else None,
        status="completed", resource_metrics=metrics,
    )
    with store.command():
        # An edit changes no rights: copied from the source as it is now, so a
        # restriction added while the job ran still reaches the new take.
        try:
            source = store.load(Asset, request.take_asset_id)
        except RecordNotFound as exc:
            raise PerformanceEditRequestError(
                f"source take {request.take_asset_id!r} no longer exists; cannot copy its rights") from exc
        asset = Asset(
            asset_id=_asset_id(request.run_id), type="voice", generated=True,
            provider=f"performance_edit@{ALGORITHM_VERSION}",
            license=source.license, model_or_voice_license=source.model_or_voice_license,
            file_path=result.take_path, checksum=result.take_sha256, reuse_scope=source.reuse_scope,
            voice_consent=source.voice_consent, voice_consent_basis=source.voice_consent_basis,
        )
        store.save(generation, expected_version=0)
        store.save(asset, expected_version=0)
    return asset


def persist_performance_edit_terminal(store: ProjectStore, job: Job, request: PerformanceEditRequest,
                                      *, status: str) -> GenerationRun:
    """Record a failed or cancelled run: a GenerationRun, no take, no Asset.

    Created only once (`expected_version=0`); a run already recorded is returned unchanged,
    unless its take was saved without its Asset, in which case the take is undone.
    """
    if status not in {"failed", "cancelled"}:
        raise ValueError("terminal performance edit generation status must be failed or cancelled")
    root = store._root
    directory = request.run_directory
    report_rel = f"{directory}/report.json"
    try:
        existing, token = store.load_with_version(GenerationRun, request.run_id)
    except RecordNotFound:
        existing = None
    if existing is not None:
        try:
            store.load(Asset, _asset_id(request.run_id))
        except RecordNotFound:
            pass
        else:
            return existing  # Fully registered; nothing of this run is left to undo.
        if discard_take(root, directory):
            mark_report_discarded(root, report_rel, status)
        kept = [path for path in existing.output_paths if root.resolve(path).is_file()]
        repaired = existing.model_copy(update={
            "status": status, "output_paths": kept,
            "output_hashes": {path: sha256_file(root.resolve(path)) for path in kept
                              if path in existing.output_hashes},
        })
        store.save(GenerationRun.model_validate(repaired.model_dump()), expected_version=token)
        return repaired
    if discard_take(root, directory):
        mark_report_discarded(root, report_rel, status)
    log_rel = f"{directory}/logs/ffmpeg.log"
    outputs = [path for path in (report_rel, log_rel) if root.resolve(path).is_file()]
    run = GenerationRun(
        run_id=request.run_id, tool="performance_edit", tool_version=ALGORITHM_VERSION,
        invocation=_invocation(request), seed=request.edit.seed, settings=_settings_record(request),
        input_paths=[request.take_path], output_paths=outputs,
        output_hashes={path: sha256_file(root.resolve(path)) for path in outputs},
        log_path=log_rel if log_rel in outputs else None, status=status,
        resource_metrics={key: float(value) for key, value in job.resource_metrics.items()},
    )
    store.save(run, expected_version=0)
    return run
