"""Click CLI generated from the shared HTTP operation registry."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click

from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings, load_settings
from orchestrator.store.json_store import ProjectStore

from .app import OperationContext
from .errors import describe, error_body
from .routes import (
    ASYNC_ADAPTER_OPERATIONS,
    MAGIC_HEADER_BYTES,
    OPERATIONS,
    UploadedFile,
    path_parameters,
)

#: Request model per operation, from the one registry, so the CLI validates exactly what
#: the HTTP route validates.
_OPERATION_MODELS = {
    operation.name: operation.request_model
    for operation in OPERATIONS
    if operation.request_model is not None
}


def command_names() -> tuple[str, ...]:
    """Return CLI command names from the same registry as the HTTP routes."""
    return tuple(operation.name for operation in OPERATIONS)


def _describe_file(path: Path) -> UploadedFile:
    """Describe a local file for upload validation without reading all of it."""
    with path.open("rb") as handle:
        head = handle.read(MAGIC_HEADER_BYTES)
    return UploadedFile(
        filename=path.name,
        head=head,
        size_bytes=path.stat().st_size,
        source_path=path,
    )


def _error_payload(exc: Exception) -> dict[str, Any]:
    """Describe a failure exactly as the HTTP surface would.

    The mapping is not restated here. An unmapped exception is still reported, with a
    nonzero exit status, rather than being swallowed or dressed up as a known failure.
    """
    mapped = describe(exc)
    if mapped is not None:
        return mapped[1]
    return error_body(type(exc).__name__, str(exc))


#: Jobs whose GPU/CPU process is owned by this CLI process: they cannot outlive it, so
#: `--no-wait` is refused and Ctrl+C requests cancellation and waits for cleanup.
_SPEECH_OPERATIONS = frozenset({
    "generate_piper_speech", "generate_indextts2_speech", "apply_audio_post_profile",
    "apply_performance_edit",
})
_LOCALLY_OWNED_JOBS = {
    "assemble_cut": "Cut assembly",
    "generate_h3_candidate": "H3",
    "generate_piper_speech": "Piper",
    "generate_indextts2_speech": "IndexTTS-2",
    "apply_audio_post_profile": "Audio post",
    "apply_performance_edit": "Performance edit",
    "review_audio": "Audio review",
}


def _command_callback(operation_name: str):
    def run(data: str, json_output: bool, **values: Any) -> None:
        context = click.get_current_context()
        configured: Settings = (context.obj or {}).get("settings") or Settings()
        operation_context = None
        submitted = None
        payload = {}
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise TypeError("--data must decode to a JSON object")
            # --file is read here so an upload operation is reachable from the CLI with
            # the same bytes the HTTP surface would receive, and so it is validated by
            # the same operation rather than by a second code path.
            files: tuple[Path, ...] = values.pop("file", ()) or ()
            # Validated against the operation's own model, as the HTTP surface does
            # before a handler sees the body. Without this a missing field surfaced as a
            # bare KeyError from the dispatcher instead of a typed validation failure,
            # so the two surfaces reported the same bad request differently.
            model = _OPERATION_MODELS.get(operation_name)
            if model is not None:
                payload = model.model_validate(payload).model_dump(mode="python")
            wait_for_job = values.pop("wait", True)
            if operation_name in _LOCALLY_OWNED_JOBS and not wait_for_job:
                raise ValueError(
                    f"{_LOCALLY_OWNED_JOBS[operation_name]} CLI jobs cannot use --no-wait because "
                    "the local process owns cleanup"
                )
            payload.update({key: value for key, value in values.items() if value is not None})
            if files:
                payload["uploads"] = [_describe_file(path) for path in files]
            root = (
                ProjectRoot(
                    configured.project_root,
                    use_windows_share_delete_reads=configured.use_windows_share_delete_reads,
                )
                if configured.project_root.is_dir()
                else None
            )
            operation_context = OperationContext(configured, root, actor="creator")
            result = operation_context.invoke(operation_name, payload)
            if operation_name in {"inspect_output", "contact_sheet"}:
                if not isinstance(result, bytes):
                    raise RuntimeError(f"{operation_name} service returned non-binary image data")
                result = {
                    "media_type": "image/png",
                    "data_base64": base64.b64encode(result).decode("ascii"),
                }
            if operation_name in ASYNC_ADAPTER_OPERATIONS and wait_for_job:
                submitted = result
                job = operation_context.job_runner.wait(payload["project_id"], result["job_id"])
                operation_context.job_runner.close()
                if job.state != "completed":
                    kind = JobErrorKind(job.error_kind or JobErrorKind.INTERNAL.value)
                    raise JobError(kind, f"job {job.job_id} ended in state {job.state}",
                                   job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
                result = {"job_id": job.job_id, "state": job.state}
                if operation_name in _SPEECH_OPERATIONS:
                    result.update({"run_id": submitted["run_id"], "asset_id": submitted["asset_id"]})
                if operation_name == "generate_h3_candidate":
                    if root is None:
                        raise RuntimeError("project root is required to read the H3 candidate receipt")
                    store = ProjectStore(ProjectRoot(root.resolve(payload["project_id"])))
                    candidates = store.list_h3_candidates(payload["shot_id"], episode_id=payload["episode_id"])
                    candidate = next((item for item in candidates if item.job_id == job.job_id), None)
                    if candidate is None:
                        raise RuntimeError("completed H3 job has no immutable candidate receipt")
                    result.update({"run_id": candidate.run_id, "av_path": candidate.av_path,
                                   "wav_path": candidate.wav_path, "report_path": candidate.report_path})
                if operation_name == "assemble_cut":
                    if operation_context is None:
                        raise RuntimeError("cut assembly operation context is unavailable")
                    # The job names its cut after its own run id, so this is exactly this
                    # job's cut even if another assembly appended one meanwhile.
                    cuts = operation_context._store(payload["project_id"]).list_episode_outputs(
                        payload["episode_id"]
                    )
                    mine = [cut for cut in cuts if cut.output_id == job.run_id]
                    if len(mine) != 1:
                        raise RuntimeError(
                            f"completed cut assembly job {job.job_id} has no EpisodeOutput named {job.run_id!r}"
                        )
                    result["output_id"] = mine[0].output_id
                if operation_name == "review_audio":
                    result["audio_review_id"] = job.run_id
        except KeyboardInterrupt:
            cleanup_error = None
            if operation_name in _LOCALLY_OWNED_JOBS and submitted and operation_context is not None and operation_context.job_runner is not None:
                try:
                    operation_context.job_runner.cancel(payload["project_id"], submitted["job_id"])
                    operation_context.job_runner.wait(payload["project_id"], submitted["job_id"])
                    operation_context.job_runner.close()
                except Exception as exc:  # noqa: BLE001 - report a cleanup failure alongside the interrupt, never instead of it.
                    cleanup_error = f"{type(exc).__name__}: {exc}"
            click.echo(json.dumps(error_body(
                "cancelled", ("H3 candidate generation interrupted" if operation_name == "generate_h3_candidate"
                 else f"{_LOCALLY_OWNED_JOBS.get(operation_name, 'job')} interrupted"),
                cleanup_error=cleanup_error,
            )), err=True)
            raise click.exceptions.Exit(130)
        except Exception as exc:  # CLI failures are reported to stderr with a nonzero status.
            click.echo(json.dumps(_error_payload(exc), ensure_ascii=False), err=True)
            raise click.exceptions.Exit(1) from exc
        click.echo(json.dumps(result, ensure_ascii=False, default=_json_default,
                              indent=None if json_output else 2))

    return run


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _build_command(operation: Any, name: str) -> click.Command:
    params: list[click.Parameter] = [
        click.Option(["--data"], default="{}", show_default=True,
                     help="JSON object passed to the operation."),
        click.Option(["--json", "json_output"], is_flag=True,
                     help="Print compact machine-readable JSON."),
    ]
    for path_name in path_parameters(operation.path):
        params.append(click.Option([f"--{path_name.replace('_', '-')}"]),)
    if operation.name in ASYNC_ADAPTER_OPERATIONS:
        params.append(click.Option(
            ["--wait/--no-wait"], default=True, show_default=True,
            help="Wait for this job to finish (default), or return after submission.",
        ))
    if operation.upload:
        params.append(click.Option(
            ["--file"], multiple=True,
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            help="File to upload; repeat for several. Validated by extension, magic "
                 "bytes and the configured size caps.",
        ))
    return click.Command(
        name=name,
        callback=_command_callback(operation.name),
        params=params,
        help=operation.summary,
    )


@click.pass_context
def _configure_cli(context: click.Context, project_root: Path | None, settings_file: Path | None) -> None:
    existing = context.obj if isinstance(context.obj, dict) else {}
    if existing.get("settings") is None:
        configured = load_settings(settings_file) if settings_file is not None else Settings()
        if project_root is not None:
            configured = configured.model_copy(update={"project_root": project_root})
        existing["settings"] = configured
    context.obj = existing


app = click.Group(
    name="cctv-pipeline",
    help="CCTV production pipeline",
    callback=_configure_cli,
    params=[click.Option(["--project-root"], type=click.Path(path_type=Path),
                         help="Existing project root from Settings."),
            click.Option(["--settings", "settings_file"], type=click.Path(exists=True, dir_okay=False, path_type=Path),
                         help="Load validated TOML settings.")],
)
for _operation in OPERATIONS:
    app.add_command(_build_command(_operation, _operation.name))
    hyphenated = _operation.name.replace("_", "-")
    if hyphenated != _operation.name:
        app.add_command(_build_command(_operation, hyphenated), name=hyphenated)


def build_parser() -> click.Group:
    """Compatibility name for callers that need the CLI application object."""
    return app


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    """Run the Click application and return its process-style exit code.

    Click is invoked directly, with ``standalone_mode=False`` so it neither calls
    ``sys.exit`` nor swallows the exception. It is deliberately NOT run through
    ``click.testing.CliRunner``: that is a test harness, and in Click 8.2+ it captures
    stderr separately from ``result.output``, so routing that output by exit code — as
    this function previously did — discarded every error message and reported a bare
    nonzero status. Writing straight to the real streams keeps stdout machine-readable
    and errors on stderr, which is what the contract asks of the CLI.
    """
    try:
        # With standalone_mode=False Click RETURNS the status of a click.exceptions.Exit
        # rather than raising it, so the status has to be read off the return value; a
        # command callback itself returns None. Reading it is what makes a failed
        # operation exit nonzero, which the contract requires of the CLI.
        outcome = app.main(args=list(argv) if argv is not None else None,
                           obj={"settings": settings}, standalone_mode=False)
    except click.ClickException as exc:
        exc.show()  # Click writes usage and parameter errors to stderr.
        return exc.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1
    return outcome if isinstance(outcome, int) else 0
