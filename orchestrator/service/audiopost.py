"""Request bodies, validation, composition and persistence for AudioPostProfile (FR-32).

An applied profile produces a take beside the Shot: like a speech take it never changes the
Shot, its status, its approval or its outputs. A post-processed clip is still a voice that
will be heard, so rights are inherited from the source take unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from orchestrator.adapters.audiopost.profile import LOUDNORM_MEASURE, build_filter_graph
from orchestrator.adapters.audiopost.runner import (
    AudioPostRequest,
    AudioPostRequestError,
    AudioPostResult,
    capture_ffmpeg_identity,
)
from orchestrator.adapters.tts.core import sha256_file
from orchestrator.domain.entities import Asset, AudioPostProfile, GenerationRun, Job, Project, Shot
from orchestrator.platform.containment import ContainmentError
from orchestrator.service.take_files import discard_take, mark_report_discarded
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict

__all__ = [
    "AudioPostApplyBody", "AudioPostProfileCreation", "compose_audio_post_request",
    "create_audio_post_profile", "persist_audio_post_success", "persist_audio_post_terminal",
]

_VOICE_ASSET_TYPES = frozenset({"voice", "audio"})
_PROFILE_NAME = r"^[a-z0-9][a-z0-9-]{0,63}$"


class AudioPostProfileCreation(BaseModel):
    """The body of `create_audio_post_profile`: a profile without its measured pins."""

    audio_post_profile: AudioPostProfile

    @field_validator("audio_post_profile")
    @classmethod
    def _pins_are_measured_not_asserted(cls, profile: AudioPostProfile) -> AudioPostProfile:
        # Refused whenever supplied, even empty: the identity is measured, never asserted.
        # (A re-validated dump carries the default None, which is not a supplied value.)
        if profile.pinned_binaries is not None:
            raise ValueError(
                "pinned_binaries is measured when the profile is created, never supplied; "
                "remove it from the body"
            )
        return profile


class AudioPostApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: StrictInt = Field(ge=1)
    take_asset_id: str = Field(
        min_length=1,
        description="The voice take to degrade: a registered .wav of type voice or audio with a "
                    "recorded voice_consent other than licensed_recording.",
    )
    profile_name: str = Field(pattern=_PROFILE_NAME, description="An existing AudioPostProfile.")


# --- profiles ---------------------------------------------------------------------------


def create_audio_post_profile(store: ProjectStore, profile: AudioPostProfile, *, ffmpeg_exe: Path,
                              popen: Callable[..., Any] | None = None) -> AudioPostProfile:
    """Pin the installed FFmpeg to a new profile and save it once (`expected_version=0`).

    Stages were validated when the entity was built and are validated again by the store;
    the identity is measured here, so a profile can only pin a binary that actually ran.
    """
    if profile.pinned_binaries is not None:
        raise AudioPostRequestError("pinned_binaries is measured, never supplied")
    identity = capture_ffmpeg_identity(ffmpeg_exe, popen)
    with store.command():
        pinned = store.validate_entity_payload(
            profile.model_copy(update={"pinned_binaries": {"ffmpeg": identity}}))
        store.load(Project)
        store.save(pinned, expected_version=0)
        return pinned


# --- composition ------------------------------------------------------------------------


def compose_audio_post_request(store: ProjectStore, *, shot_id: str, episode_id: str,
                               body: AudioPostApplyBody, run_id: str) -> AudioPostRequest:
    """Validate an apply body against the project and bind it to the Shot's current version."""
    if not isinstance(body, AudioPostApplyBody):
        body = AudioPostApplyBody.model_validate(body)
    with store.command():
        shot, token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
        if token != body.expected_version:
            raise VersionConflict(f"episodes/{episode_id}/shots/{shot_id}",
                                  body.expected_version, token, shot)
        try:
            profile = store.load(AudioPostProfile, body.profile_name)
        except RecordNotFound as exc:
            raise AudioPostRequestError(
                f"profile_name {body.profile_name!r} is not a created AudioPostProfile") from exc
        take = _verified_take(store, body.take_asset_id)
        return AudioPostRequest(
            shot_id=shot_id, shot_version=shot.version, run_id=run_id,
            take_asset_id=take.asset_id, take_path=take.file_path or "",
            take_sha256=take.checksum or "", profile=profile,
        )


def _verified_take(store: ProjectStore, asset_id: str) -> Asset:
    """Accept a take only if it is a consented, intact .wav voice (FR-16).

    A licensed recording was licensed to lend its emotion, not to be heard, so it is
    refused here exactly as it is refused as a speaker.
    """
    try:
        asset = store.load(Asset, asset_id)
    except RecordNotFound as exc:
        raise AudioPostRequestError(f"take_asset_id {asset_id!r} is not a registered asset") from exc
    if asset.type not in _VOICE_ASSET_TYPES:
        raise AudioPostRequestError(
            f"take asset {asset_id!r} has type {asset.type!r}; it must be type 'voice' or 'audio'")
    if asset.voice_consent is None:
        raise AudioPostRequestError(
            f"take asset {asset_id!r} has no voice_consent recorded; a voice that will be heard "
            "needs consent, not merely a licence (FR-16)")
    if asset.voice_consent == "licensed_recording":
        raise AudioPostRequestError(
            f"take asset {asset_id!r} has voice_consent 'licensed_recording', which permits use "
            "as an emotion reference only; a post-processed clip is a voice that will be heard "
            "(FR-16)")
    if not asset.file_path or not asset.file_path.lower().endswith(".wav"):
        raise AudioPostRequestError(f"take asset {asset_id!r} must be a registered .wav file")
    if not asset.checksum:
        raise AudioPostRequestError(f"take asset {asset_id!r} has no registered checksum")
    try:
        path = store._root.resolve(asset.file_path)
    except ContainmentError as exc:
        raise AudioPostRequestError(f"take asset {asset_id!r} path is not contained: {exc}") from exc
    if not path.is_file():
        raise AudioPostRequestError(f"take asset {asset_id!r} file is missing: {asset.file_path}")
    if sha256_file(path) != asset.checksum:
        raise AudioPostRequestError(
            f"take asset {asset_id!r} bytes do not match its registered checksum; re-register the clip")
    return asset


# --- persistence ------------------------------------------------------------------------


def _asset_id(run_id: str) -> str:
    return f"audiopost-{run_id}"


def _settings_record(request: AudioPostRequest) -> dict[str, str | bool | int | float]:
    return {"profile": request.profile.name, "semver": request.profile.semver,
            "take_asset_id": request.take_asset_id, "take_sha256": request.take_sha256}


def persist_audio_post_success(store: ProjectStore, job: Job, result: AudioPostResult,
                               request: AudioPostRequest) -> Asset:
    """Save the GenerationRun and register the degraded take, once its files are verified."""
    root = store._root
    if job.run_id != request.run_id:
        raise AudioPostRequestError("audio post job run_id must match the submitted request")
    hashes: dict[str, str] = {}
    for relative, expected in ((result.take_path, result.take_sha256),
                               (result.report_path, result.report_sha256)):
        artifact = root.resolve(relative)
        if not artifact.is_file() or sha256_file(artifact) != expected:
            raise AudioPostRequestError(f"audio post artifact is missing or its SHA256 differs: {relative}")
        hashes[relative] = expected
    log = root.resolve(result.log_path)
    if log.is_file():
        hashes[result.log_path] = sha256_file(log)
    metrics = {key: float(value) for key, value in job.resource_metrics.items()}
    metrics["wall_seconds"] = float(result.wall_seconds)
    graph = json.loads(root.read_text(result.report_path, encoding="utf-8"))["filter_graph"]
    generation = GenerationRun(
        run_id=request.run_id, tool="ffmpeg", tool_version=result.tool_version,
        invocation=graph, seed=result.noise_seed, settings=_settings_record(request),
        input_paths=[request.take_path], output_paths=list(hashes), output_hashes=hashes,
        log_path=result.log_path if result.log_path in hashes else None,
        status="completed", resource_metrics=metrics,
    )
    with store.command():
        # Degradation changes no rights: copied from the source as it is now, so a
        # restriction added while the job ran still reaches the new take.
        try:
            source = store.load(Asset, request.take_asset_id)
        except RecordNotFound as exc:
            raise AudioPostRequestError(
                f"source take {request.take_asset_id!r} no longer exists; cannot copy its rights") from exc
        asset = Asset(
            asset_id=_asset_id(request.run_id), type="voice", generated=True,
            provider=f"audio_post_profile:{request.profile.name}@{request.profile.semver}",
            license=source.license, model_or_voice_license=source.model_or_voice_license,
            file_path=result.take_path, checksum=result.take_sha256, reuse_scope=source.reuse_scope,
            voice_consent=source.voice_consent, voice_consent_basis=source.voice_consent_basis,
        )
        store.save(generation, expected_version=0)
        store.save(asset, expected_version=0)
    return asset


def persist_audio_post_terminal(store: ProjectStore, job: Job, request: AudioPostRequest,
                                *, status: str) -> GenerationRun:
    """Record a failed or cancelled run: a GenerationRun, no take, no Asset.

    Created only once (`expected_version=0`); a run already recorded is returned unchanged,
    unless its take was saved without its Asset, in which case the take is undone.
    """
    if status not in {"failed", "cancelled"}:
        raise ValueError("terminal audio post generation status must be failed or cancelled")
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
    version = "unknown"
    if report_rel in outputs:
        identity = json.loads(root.read_text(report_rel, encoding="utf-8")).get("ffmpeg_identity")
        if isinstance(identity, str) and identity:
            version = identity.splitlines()[0]
    run = GenerationRun(
        run_id=request.run_id, tool="ffmpeg", tool_version=version,
        invocation=build_filter_graph(request.profile, seed=request.noise_seed, loudnorm=LOUDNORM_MEASURE),
        seed=request.noise_seed, settings=_settings_record(request),
        input_paths=[request.take_path], output_paths=outputs,
        output_hashes={path: sha256_file(root.resolve(path)) for path in outputs},
        log_path=log_rel if log_rel in outputs else None, status=status,
        resource_metrics={key: float(value) for key, value in job.resource_metrics.items()},
    )
    store.save(run, expected_version=0)
    return run
