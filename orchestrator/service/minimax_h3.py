"""Composition and immutable receipt handling for opt-in MiniMax H3 candidates."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from orchestrator.adapters.minimax_h3 import H3Request, H3Result
from orchestrator.adapters.minimax_h3.core import H3RequestError, H3SetupError
from orchestrator.domain.entities import (
    Episode,
    GenerationRun,
    H3Candidate,
    Job,
    Review,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.domain.state import ReviewApprovalError, check_review_approval
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict


class _H3ApprovalJobError(JobError, H3RequestError):
    """Review binding failure as INVALID_MANIFEST and the existing H3 request error."""

    def __init__(self, cause: ReviewApprovalError) -> None:
        super().__init__(JobErrorKind.INVALID_MANIFEST, str(cause), cause=cause)


class H3GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: StrictInt = Field(ge=1)
    shot_version: StrictInt = Field(ge=1)
    video_path: str = Field(min_length=1)
    prompt: str | None = None
    prompt_file: str | None = None
    degrade_seed: StrictInt = Field(0, ge=0)
    start_seconds: float = Field(0, ge=0)
    duration_seconds: float = Field(8, gt=0)
    width: StrictInt = Field(384, ge=1)
    height: StrictInt = Field(288, ge=1)
    fps: StrictInt = Field(24, ge=1)
    steps: StrictInt = Field(20, ge=1)
    experimental: bool = False

    @model_validator(mode="after")
    def _one_prompt_source(self) -> H3GenerateBody:
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError("provide exactly one of prompt or prompt_file")
        return self


class H3CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: StrictInt = Field(ge=1)


class H3PromoteBody(BaseModel):
    """The approval and the output-list version a promotion is bound to.

    `expected_version` is the number of ShotOutputs already recorded for the shot, not a
    Shot version: `promote_h3_candidate` appends through `append_shot_output`, so this is
    what makes a concurrent second promotion conflict instead of silently appending a
    duplicate. It is therefore `ge=0` -- a shot whose first output is the promotion is a
    legitimate case -- where the cancel body's Shot version is `ge=1`.
    """

    model_config = ConfigDict(extra="forbid")
    review_id: str = Field(min_length=1)
    expected_version: StrictInt = Field(ge=0)


def effective_h3_settings(settings: Settings) -> Settings:
    """Copy general settings and apply only the H3-specific operational policy."""
    policy = settings.minimax_h3
    if not policy.enabled:
        raise H3SetupError("MiniMax H3 is disabled; configure [minimax_h3] with explicit admission reservations")
    if any(value is None or value <= 0 for value in (
        policy.required_vram_gb, policy.required_ram_gb, policy.required_disk_gb,
        policy.throughput_floor_per_second,
    )):
        raise H3SetupError("MiniMax H3 requires explicit positive resource reservations and throughput floor")
    budget = settings.budget.model_copy(update={"retries_per_shot": 0,
                                                "per_shot_wall_clock_seconds": policy.wall_clock_seconds})
    resources = settings.resources.model_copy(update={
        "startup_allowance_seconds": policy.startup_allowance_seconds,
        "throughput_floor_per_second": policy.throughput_floor_per_second,
    })
    return settings.model_copy(update={"budget": budget, "resources": resources})


def compose_h3_request(
    store: ProjectStore, *, shot_id: str, episode_id: str, body: object, run_id: str,
) -> H3Request:
    """Bind an H3 request to a historical Shot and a hash-verified production output."""
    if not isinstance(body, H3GenerateBody):
        body = H3GenerateBody.model_validate(body)
    with store.command():
        shot, token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
        if token != body.expected_version:
            raise VersionConflict(f"episodes/{episode_id}/shots/{shot_id}", body.expected_version, token, shot)
        if body.shot_version > shot.version:
            raise H3RequestError("shot_version does not exist")
        historical = store.load(Shot, shot_id, episode_id=episode_id, version=body.shot_version)
        store.load(Episode, episode_id)
        output = next((candidate for candidate in store.list_shot_outputs(
            shot_id, episode_id=episode_id, record_version=body.shot_version
        ) if candidate.relative_path == body.video_path and candidate.kind in {
            ShotOutputKind.MASTER, ShotOutputKind.DEGRADED,
        }), None)
        if output is None:
            raise H3RequestError("video_path must name a MASTER or DEGRADED receipt for this Shot version")
        normalized = body.video_path.replace("\\", "/")
        if normalized.startswith("references/") or "/references/" in normalized:
            raise H3RequestError("reference media cannot be used as an H3 candidate source")
        target = store._root.resolve(output.relative_path)
        if not target.is_file():
            raise H3RequestError("bound source output is missing")
        digest = _sha256(target)
        if digest != output.content_sha256:
            raise H3RequestError("bound source output hash does not match its immutable receipt")
        prompt = body.prompt
        if body.prompt_file is not None:
            prompt = store._root.read_bytes(body.prompt_file).decode("utf-8", errors="strict")
        try:
            return H3Request(
                shot_id=shot_id, shot_version=historical.version, run_id=run_id,
                video_path=output.relative_path, source_sha256=digest,
                source_output_id=output.output_id, prompt=prompt,
                degrade_seed=body.degrade_seed, start_seconds=body.start_seconds,
                duration_seconds=body.duration_seconds, width=body.width, height=body.height,
                fps=body.fps, steps=body.steps, experimental=body.experimental,
            )
        except (TypeError, ValueError) as exc:
            raise H3RequestError(str(exc)) from exc


def persist_h3_candidate(store: ProjectStore, job: Job, result: H3Result,
                         request: H3Request) -> H3Candidate:
    """Save the generation receipt and candidate only after adapter validation/cleanup."""
    root = store._root
    if result.run_id != request.run_id or job.run_id != request.run_id:
        raise H3RequestError("H3 result run_id must match the submitted request and job")
    for relative, expected in ((result.av_path, result.av_sha256),
                               (result.wav_path, result.wav_sha256),
                               (result.report_path, result.report_sha256)):
        artifact = root.resolve(relative)
        if not artifact.is_file() or _sha256(artifact) != expected:
            raise H3RequestError(f"H3 result artifact is missing or its SHA256 differs: {relative}")
    prompt_hash = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
    candidate = H3Candidate(
        candidate_id=request.run_id, episode_id=job.episode_id or "",
        shot_id=request.shot_id, shot_version=request.shot_version,
        job_id=job.job_id, run_id=request.run_id,
        source_output_id=request.source_output_id, source_path=request.video_path,
        source_sha256=request.source_sha256, start_seconds=request.start_seconds,
        prompt_sha256=prompt_hash,
        seed=result.seed, av_path=result.av_path, av_sha256=result.av_sha256,
        wav_path=result.wav_path, wav_sha256=result.wav_sha256,
        report_path=result.report_path, report_sha256=result.report_sha256,
        frame_count=result.frame_count, fps=result.fps,
        audio_sample_rate=result.audio_sample_rate, audio_channels=result.audio_channels,
        audio_samples_per_channel=result.audio_samples_per_channel,
        experimental=result.experimental,
    )
    # The report contains complete provenance; retain its exact text in invocation.
    report = json.loads(root.read_text(result.report_path, encoding="utf-8"))
    generation = GenerationRun(
        run_id=request.run_id, tool="minimax_h3", tool_version=str(report.get("runtime", {}).get("version", "pinned")),
        invocation=request.prompt, seed=result.seed,
        settings={"duration_seconds": request.duration_seconds, "width": request.width,
                  "height": request.height, "fps": request.fps, "steps": request.steps,
                  "start_seconds": request.start_seconds,
                  "experimental": request.experimental},
        input_paths=[request.video_path], output_paths=[result.av_path, result.wav_path, result.report_path],
        log_path=report.get("log_path"), status="completed", resource_metrics=job.resource_metrics,
    )
    with store.command():
        store.save(generation, expected_version=0)
        try:
            store.save(candidate, expected_version=0)
        except Exception as primary:
            sidecar_rel = (
                f"generated/h3/{request.shot_id}/v{request.shot_version:04d}/"
                f"{request.run_id}/metadata_failure.json"
            )
            diagnostic = {
                "status": "candidate_metadata_failed",
                "run_id": request.run_id,
                "generation_run_path": f"generations/{request.run_id}.json",
                "candidate_path": f"generated/h3/candidates/{candidate.candidate_id}.json",
                "error": {"type": type(primary).__name__, "message": str(primary)},
            }
            try:
                if root.resolve(sidecar_rel).exists():
                    raise FileExistsError(f"refusing to overwrite H3 metadata diagnostic {sidecar_rel}")
                root.write_text(sidecar_rel, json.dumps(diagnostic, ensure_ascii=False,
                                                        allow_nan=False, indent=2) + "\n")
            except Exception as diagnostic_error:  # noqa: BLE001 - a diagnostic write must not mask the primary failure.
                primary.add_note(
                    f"H3 metadata-failure sidecar write also failed: "
                    f"{type(diagnostic_error).__name__}: {diagnostic_error}"
                )
            raise
    return candidate


def persist_h3_terminal(store: ProjectStore, job: Job, request: H3Request, *, status: str) -> GenerationRun | None:
    """Retain a native failure/cancellation report as a GenerationRun when work began."""
    if status not in {"failed", "cancelled"}:
        raise ValueError("terminal H3 generation status must be failed or cancelled")
    root = store._root
    run_rel = f"generated/h3/{request.shot_id}/v{request.shot_version:04d}/{request.run_id}"
    report_rel = run_rel + "/report.json"
    report_path = root.resolve(report_rel)
    try:
        existing = store.load(GenerationRun, request.run_id)
    except RecordNotFound:
        existing = None
    if existing is not None:
        return existing
    if not report_path.is_file():
        return None
    report = json.loads(root.read_text(report_rel, encoding="utf-8"))
    # A2.2, resolved: hash the report before building the record, so a failed or
    # cancelled run carries the same kind of integrity receipt the success path puts on
    # H3Candidate. Hashed from the bytes on disk rather than re-serialising `report`,
    # because a receipt for a re-encoding of the parsed object would not detect the file
    # itself being altered -- which is the only thing it exists to detect.
    report_hash = _sha256(report_path)
    run = GenerationRun(
        run_id=request.run_id, tool="minimax_h3",
        tool_version=str(report.get("runtime", {}).get("version", "pinned")),
        invocation=request.prompt, seed=report.get("seed"),
        settings={"duration_seconds": request.duration_seconds, "width": request.width,
                  "height": request.height, "fps": request.fps, "steps": request.steps,
                  "start_seconds": request.start_seconds,
                  "experimental": request.experimental},
        input_paths=[request.video_path], output_paths=[report_rel],
        output_hashes={report_rel: report_hash},
        log_path=report.get("log_path"), status=status,
        resource_metrics=job.resource_metrics,
    )
    # expected_version=0 keeps this create-only: an already-written record for this run
    # id raises rather than being revised, which a retry must never do.
    store.save(run, expected_version=0)
    return run


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


#: `ShotOutput.engine_id` for a promoted H3 output. H3's identity is not one binary but a
#: set, so the id names the generator and `engine_build` carries the ComfyUI version and
#: source commit. The four component digests (diffusion, text encoder, video VAE, audio
#: VAE) stay reachable through the candidate's report rather than stuffed into a string.
H3_ENGINE_ID = "MINIMAX_H3"


def _h3_engine_build(report: dict) -> str:
    """Derive `engine_build` from the run's own verified report, never from a constant.

    A hardcoded version would keep claiming 0.44.0 after the runtime moved. Both values are
    required: a promoted output with unknown provenance is worse than a refused promotion.
    """
    try:
        version = report["server_ready"]["system_stats"]["system"]["comfyui_version"]
        commit = report["runtime"]["source_receipt"]["commit"]
    except (KeyError, TypeError) as exc:
        raise H3RequestError(
            "H3 report does not record the ComfyUI version and source commit needed for "
            "engine provenance"
        ) from exc
    if not isinstance(version, str) or not isinstance(commit, str) or not version or not commit:
        raise H3RequestError("H3 report engine provenance must be non-empty strings")
    return f"comfyui {version}+{commit}"


def promote_h3_candidate(
    store: ProjectStore,
    *,
    candidate_id: str,
    review_id: str,
    expected_version: int,
    agent_may_approve: bool = False,
) -> ShotOutput:
    """Append a GENERATED ShotOutput for an approved, hash-verified H3 candidate.

    The candidate is never mutated: promotion creates a new record that references it.
    Each step refuses rather than guesses, because a promoted record whose bytes drifted
    would poison every later read through `_verify_shot_output_artifact`.
    """
    root = store._root
    with store.command():
        try:
            candidate = store.load(H3Candidate, candidate_id)
        except RecordNotFound as exc:
            raise H3RequestError(f"H3 candidate {candidate_id!r} does not exist") from exc

        try:
            review = store.load(Review, review_id)
        except RecordNotFound as exc:
            raise H3RequestError(f"promotion Review {review_id!r} does not exist") from exc
        try:
            check_review_approval(
                review,
                target_kind="h3_candidate",
                target_id=candidate_id,
                target_version=candidate.shot_version,
                episode_id=candidate.episode_id,
                agent_may_approve=agent_may_approve,
            )
        except ReviewApprovalError as exc:
            raise _H3ApprovalJobError(exc) from exc

        # D2 widened `_BASELINE_SHAPES` so a production shape needs no flag. A candidate
        # still carrying one is therefore an off-spec probe, and `experimental` keeps
        # working as a gate only if promotion refuses it.
        if candidate.experimental:
            raise H3RequestError(
                "an experimental H3 candidate cannot be promoted; regenerate the shot with "
                "experimental=False, which a baseline production shape no longer requires"
            )

        for relative, expected in ((candidate.av_path, candidate.av_sha256),
                                   (candidate.report_path, candidate.report_sha256)):
            artifact = root.resolve(relative)
            if not artifact.is_file() or _sha256(artifact) != expected:
                raise H3RequestError(
                    f"H3 candidate artifact is missing or its SHA256 differs: {relative}"
                )

        existing = store.list_shot_outputs(candidate.shot_id, episode_id=candidate.episode_id)
        if any(output.kind is ShotOutputKind.GENERATED
               and output.relative_path == candidate.av_path for output in existing):
            raise H3RequestError(
                f"H3 candidate {candidate_id!r} is already promoted; promotion is idempotent "
                "by refusal, never by replacement"
            )

        report = json.loads(root.read_text(candidate.report_path, encoding="utf-8"))
        output = ShotOutput(
            output_id=uuid.uuid4().hex, episode_id=candidate.episode_id,
            shot_id=candidate.shot_id, record_version=candidate.shot_version,
            kind=ShotOutputKind.GENERATED, relative_path=candidate.av_path,
            content_sha256=candidate.av_sha256, engine_id=H3_ENGINE_ID,
            engine_build=_h3_engine_build(report),
        )
        store.append_shot_output(output, expected_version=expected_version)
        return output
