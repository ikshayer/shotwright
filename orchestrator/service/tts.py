"""Request bodies, validation, consent and persistence for the speech adapters.

A take is an input the agent may later attach to a Shot (through `revise_shot`); like an H3
candidate it never changes the Shot, its status, its approval or its outputs.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator, model_validator

from orchestrator.adapters.tts.core import (
    INDEXTTS2_CONFIG,
    INDEXTTS2_MODEL_LICENSE,
    INDEXTTS2_PYTHON,
    PIPER_PYTHON,
    TAKE_LICENSE_PENDING,
    Emotion,
    IndexTts2Request,
    PiperRequest,
    TtsRequestError,
    TtsResult,
    TtsSetupError,
    load_piper_catalog,
    repository_root,
    sha256_file,
    validate_speech_text,
)
from orchestrator.domain.entities import Asset, GenerationRun, Job, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict

__all__ = [
    "IndexTts2SpeechBody", "PiperSpeechBody", "compose_indextts2_request",
    "compose_piper_request", "effective_indextts2_settings", "inspect_tts", "persist_tts_success",
    "persist_tts_terminal", "require_piper_enabled",
]

_VOICE_ASSET_TYPES = frozenset({"voice", "audio"})


class _SpeechBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: StrictInt = Field(ge=1)
    text: str | None = None
    text_file: str | None = Field(None, min_length=1)

    @model_validator(mode="after")
    def _exactly_one_text_source(self) -> _SpeechBody:
        if (self.text is None) == (self.text_file is None):
            raise ValueError("provide exactly one of text or text_file")
        if self.text is not None:
            validate_speech_text(self.text)
        return self


class PiperSpeechBody(_SpeechBody):
    voice_id: str = Field(min_length=1)
    length_scale: float | None = Field(None, ge=0.5, le=2.0, allow_inf_nan=False)
    sentence_silence_seconds: float | None = Field(None, ge=0.0, le=2.0, allow_inf_nan=False)

    @field_validator("length_scale", "sentence_silence_seconds", mode="before")
    @classmethod
    def _number_not_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        return value


class IndexTts2SpeechBody(_SpeechBody):
    speaker_asset_id: str = Field(min_length=1)
    emotion: Emotion


# --- inspection -------------------------------------------------------------------------


def inspect_tts(settings: Settings, repository: ProjectRoot | None = None) -> dict[str, Any]:
    """Model-free: file presence only. No process, no torch/onnxruntime import, no hashing."""
    repo = repository or repository_root()
    return {
        "piper": {
            "enabled": settings.piper.enabled,
            "runtime_present": repo.resolve(PIPER_PYTHON).is_file(),
            "voices": [{
                "voice_id": voice.voice_id, "gender": voice.gender, "license": voice.license,
                "sample_rate": voice.sample_rate,
                "present": (repo.resolve(voice.onnx_relative).is_file()
                            and repo.resolve(voice.config_relative).is_file()),
            } for voice in load_piper_catalog().values()],
        },
        "indextts2": {
            "enabled": settings.indextts2.enabled,
            "runtime_present": repo.resolve(INDEXTTS2_PYTHON).is_file(),
            "checkpoints_present": repo.resolve(INDEXTTS2_CONFIG).is_file(),
        },
        "limitations": [
            "synthesis only: no mixing, loudness normalisation, resampling or PerformanceEdit",
            "Piper is for processed-source voices; IndexTTS-2 is for diegetic voices and needs a "
            "speaker reference with recorded voice consent",
            "IndexTTS-2 sampling is not deterministic and has no seed",
            "IndexTTS-2 reservations are operator admission policy, not measured minima",
            "a take is registered as an Asset; attach it to a Shot with revise_shot (audio_refs)",
            "take licence classification is a pending creator decision and reports as a credits blocker",
        ],
    }


# --- settings ---------------------------------------------------------------------------


def require_piper_enabled(settings: Settings) -> None:
    if not settings.piper.enabled:
        raise TtsSetupError("Piper is disabled; set enabled = true under [piper] in settings.toml")


def effective_indextts2_settings(settings: Settings) -> Settings:
    """Copy general settings and apply only the IndexTTS-2 operational policy.

    Retries are zero (no automatic retries). The hard wall clock covers the model load
    plus generation; the adapter enforces the two allowances separately, so the
    throughput watchdog, which measures progress rather than time, is switched off.
    """
    policy = settings.indextts2
    if not policy.enabled:
        raise TtsSetupError(
            "IndexTTS-2 is disabled; configure [indextts2] with explicit admission reservations"
        )
    if any(value is None or value <= 0 for value in (
        policy.required_vram_gb, policy.required_ram_gb, policy.required_disk_gb,
    )):
        raise TtsSetupError("IndexTTS-2 requires explicit positive resource reservations")
    budget = settings.budget.model_copy(update={
        "retries_per_shot": 0,
        "per_shot_wall_clock_seconds": policy.startup_allowance_seconds + policy.execution_timeout_seconds,
    })
    resources = settings.resources.model_copy(update={
        "startup_allowance_seconds": policy.startup_allowance_seconds,
        "throughput_floor_per_second": 0.0,
    })
    return settings.model_copy(update={"budget": budget, "resources": resources})


# --- composition ------------------------------------------------------------------------


def _resolve_text(root: ProjectRoot, body: _SpeechBody) -> str:
    if body.text is not None:
        return body.text
    assert body.text_file is not None
    try:
        text = root.read_bytes(body.text_file).decode("utf-8", errors="strict")
    except FileNotFoundError as exc:
        raise TtsRequestError(f"text_file {body.text_file!r} does not exist") from exc
    except UnicodeDecodeError as exc:
        raise TtsRequestError(f"text_file {body.text_file!r} is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise TtsRequestError(f"text_file {body.text_file!r} cannot be read: {exc}") from exc
    return validate_speech_text(text)


def _current_shot(store: ProjectStore, shot_id: str, episode_id: str, expected_version: int) -> Shot:
    shot, token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
    if token != expected_version:
        raise VersionConflict(f"episodes/{episode_id}/shots/{shot_id}", expected_version, token, shot)
    return shot


def compose_piper_request(store: ProjectStore, *, shot_id: str, episode_id: str,
                          body: PiperSpeechBody, run_id: str) -> PiperRequest:
    """Validate a Piper body against the project and bind it to the Shot's current version."""
    if not isinstance(body, PiperSpeechBody):
        body = PiperSpeechBody.model_validate(body)
    if body.voice_id not in load_piper_catalog():
        raise TtsRequestError(
            f"voice_id {body.voice_id!r} is not in the Piper voice catalog; "
            f"allowed: {sorted(load_piper_catalog())}"
        )
    with store.command():
        shot = _current_shot(store, shot_id, episode_id, body.expected_version)
        text = _resolve_text(store._root, body)
        return _build(PiperRequest, shot_id=shot_id, shot_version=shot.version, run_id=run_id,
                      text=text, voice_id=body.voice_id, length_scale=body.length_scale,
                      sentence_silence_seconds=body.sentence_silence_seconds)


def compose_indextts2_request(store: ProjectStore, *, shot_id: str, episode_id: str,
                              body: IndexTts2SpeechBody, run_id: str) -> IndexTts2Request:
    """Validate an IndexTTS-2 body, including the speaker reference's consent (FR-16)."""
    if not isinstance(body, IndexTts2SpeechBody):
        body = IndexTts2SpeechBody.model_validate(body)
    with store.command():
        shot = _current_shot(store, shot_id, episode_id, body.expected_version)
        text = _resolve_text(store._root, body)
        speaker = _verified_speaker(store, body.speaker_asset_id)
        return _build(IndexTts2Request, shot_id=shot_id, shot_version=shot.version, run_id=run_id,
                      text=text, speaker_asset_id=speaker.asset_id,
                      speaker_path=speaker.file_path, speaker_sha256=speaker.checksum,
                      speaker_consent=speaker.voice_consent,
                      speaker_consent_basis=speaker.voice_consent_basis, emotion=body.emotion)


def _build(model: type, **fields: Any) -> Any:
    try:
        return model(**fields)
    except ValidationError as exc:
        raise TtsRequestError(str(exc)) from exc


def _verified_speaker(store: ProjectStore, asset_id: str) -> Asset:
    """Accept a speaker reference only if consent is recorded and its bytes are intact."""
    try:
        asset = store.load(Asset, asset_id)
    except RecordNotFound as exc:
        raise TtsRequestError(f"speaker_asset_id {asset_id!r} is not a registered asset") from exc
    if asset.type not in _VOICE_ASSET_TYPES:
        raise TtsRequestError(
            f"speaker asset {asset_id!r} has type {asset.type!r}; a speaker reference must be "
            "type 'voice' or 'audio'"
        )
    if asset.voice_consent is None:
        raise TtsRequestError(
            f"speaker asset {asset_id!r} has no voice_consent recorded; voice identity needs "
            "consent, not merely a licence (FR-16). Register the clip with voice_consent "
            "'synthetic', 'creator_own_voice' or 'licensed_for_cloning'"
        )
    if not asset.file_path or not asset.file_path.lower().endswith(".wav"):
        raise TtsRequestError(f"speaker asset {asset_id!r} must be a registered .wav file")
    if not asset.checksum:
        raise TtsRequestError(f"speaker asset {asset_id!r} has no registered checksum")
    try:
        path = store._root.resolve(asset.file_path)
    except ValueError as exc:  # ContainmentError
        raise TtsRequestError(f"speaker asset {asset_id!r} path is not contained: {exc}") from exc
    if not path.is_file():
        raise TtsRequestError(f"speaker asset {asset_id!r} file is missing: {asset.file_path}")
    if sha256_file(path) != asset.checksum:
        raise TtsRequestError(
            f"speaker asset {asset_id!r} bytes do not match its registered checksum; "
            "re-register the clip"
        )
    return asset


# --- persistence ------------------------------------------------------------------------


def _engine_of(request: PiperRequest | IndexTts2Request) -> str:
    return "piper" if isinstance(request, PiperRequest) else "indextts2"


def persist_tts_success(store: ProjectStore, job: Job, result: TtsResult,
                        request: PiperRequest | IndexTts2Request) -> Asset:
    """Save the GenerationRun and register the take, once its files are verified."""
    root = store._root
    if result.run_id != request.run_id or job.run_id != request.run_id:
        raise TtsRequestError("speech result run_id must match the submitted request and job")
    hashes: dict[str, str] = {}
    for relative, expected in ((result.take_path, result.take_sha256),
                               (result.text_path, result.text_sha256),
                               (result.report_path, result.report_sha256)):
        artifact = root.resolve(relative)
        if not artifact.is_file() or sha256_file(artifact) != expected:
            raise TtsRequestError(f"speech artifact is missing or its SHA256 differs: {relative}")
        hashes[relative] = expected
    report = json.loads(root.read_text(result.report_path, encoding="utf-8"))
    log = root.resolve(result.log_path)
    if log.is_file():
        hashes[result.log_path] = sha256_file(log)
    metrics = {key: float(value) for key, value in job.resource_metrics.items()}
    metrics["wall_seconds"] = float(report["timings"]["wall_seconds"])
    if isinstance(request, PiperRequest):
        voice = load_piper_catalog()[request.voice_id]
        model_license, consent, basis = voice.license, "synthetic", None
        inputs: list[str] = []
    else:
        model_license = INDEXTTS2_MODEL_LICENSE
        consent, basis = request.speaker_consent, request.speaker_consent_basis
        inputs = [request.speaker_path]
    generation = GenerationRun(
        run_id=request.run_id, tool=_engine_of(request), tool_version=result.tool_version,
        invocation=request.text, seed=None, settings=request.settings_record,
        input_paths=inputs, output_paths=list(hashes), output_hashes=hashes,
        log_path=result.log_path if result.log_path in hashes else None,
        status="completed", resource_metrics=metrics,
    )
    asset = Asset(
        asset_id=f"tts-{request.run_id}", type="voice", generated=True,
        provider=_engine_of(request), license=TAKE_LICENSE_PENDING,
        model_or_voice_license=model_license, file_path=result.take_path,
        checksum=result.take_sha256, reuse_scope="project",
        voice_consent=consent, voice_consent_basis=basis,
    )
    with store.command():
        store.save(generation, expected_version=0)
        store.save(asset, expected_version=0)
    return asset


def _discard_take(root: ProjectRoot, directory: str) -> bool:
    """A failed or cancelled run never leaves a take.wav (contract 5.1). True if one existed."""
    found = False
    for name in ("take.wav", "take.partial.wav"):
        path = root.resolve(f"{directory}/{name}")
        if path.is_file():
            path.unlink()
            found = found or name == "take.wav"
    return found


def persist_tts_terminal(store: ProjectStore, job: Job, request: PiperRequest | IndexTts2Request,
                         *, status: str) -> GenerationRun:
    """Record a failed or cancelled run: a GenerationRun, no take, no Asset.

    Created only once (`expected_version=0`); a run already recorded is returned unchanged.
    """
    if status not in {"failed", "cancelled"}:
        raise ValueError("terminal speech generation status must be failed or cancelled")
    root = store._root
    directory = request.run_directory
    report_rel = f"{directory}/report.json"
    try:
        existing, token = store.load_with_version(GenerationRun, request.run_id)
    except RecordNotFound:
        existing = None
    if existing is not None:
        try:
            store.load(Asset, f"tts-{request.run_id}")
        except RecordNotFound:
            pass
        else:
            return existing  # Fully registered; nothing of this run is left to undo.
        # The run was saved but its Asset was not (the two are separate writes): undo the
        # take so the receipt cannot describe a take that has no registered identity.
        discarded = _discard_take(root, directory)
        if discarded and root.resolve(report_rel).is_file():
            report = json.loads(root.read_text(report_rel, encoding="utf-8"))
            report.update(status=status, take_discarded=True)
            root.write_text(report_rel, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        kept = [path for path in existing.output_paths if root.resolve(path).is_file()]
        repaired = existing.model_copy(update={
            "status": status, "output_paths": kept,
            "output_hashes": {path: sha256_file(root.resolve(path)) for path in kept
                              if path in existing.output_hashes},
        })
        store.save(GenerationRun.model_validate(repaired.model_dump()), expected_version=token)
        return repaired
    discarded = _discard_take(root, directory)
    if discarded and root.resolve(report_rel).is_file():
        # A report written by a run that finished before it was cancelled would otherwise
        # still claim a completed take that no longer exists.
        report = json.loads(root.read_text(report_rel, encoding="utf-8"))
        report.update(status=status, take_discarded=True)
        root.write_text(report_rel, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    outputs = [f"{directory}/{name}" for name in ("text.txt", "report.json", "logs/stderr.txt")
               if root.resolve(f"{directory}/{name}").is_file()]
    version = "unknown"
    if report_rel in outputs:
        report = json.loads(root.read_text(report_rel, encoding="utf-8"))
        version = str(report.get("engine_version", version))
    log_rel = f"{directory}/logs/stderr.txt"
    run = GenerationRun(
        run_id=request.run_id, tool=_engine_of(request), tool_version=version,
        invocation=request.text, settings=request.settings_record,
        input_paths=[request.speaker_path] if isinstance(request, IndexTts2Request) else [],
        output_paths=outputs,
        output_hashes={path: sha256_file(root.resolve(path)) for path in outputs
                       if path == report_rel},
        log_path=log_rel if log_rel in outputs else None, status=status,
        resource_metrics={key: float(value) for key, value in job.resource_metrics.items()},
    )
    store.save(run, expected_version=0)
    return run
