"""Click CLI generated from the shared HTTP operation registry."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click

from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings

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
    return UploadedFile(filename=path.name, head=head, size_bytes=path.stat().st_size)


def _error_payload(exc: Exception) -> dict[str, Any]:
    """Describe a failure exactly as the HTTP surface would.

    The mapping is not restated here. An unmapped exception is still reported, with a
    nonzero exit status, rather than being swallowed or dressed up as a known failure.
    """
    mapped = describe(exc)
    if mapped is not None:
        return mapped[1]
    return error_body(type(exc).__name__, str(exc))


def _command_callback(operation_name: str):
    def run(data: str, json_output: bool, **values: Any) -> None:
        context = click.get_current_context()
        configured: Settings = (context.obj or {}).get("settings") or Settings()
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
            context = OperationContext(configured, root)
            result = context.invoke(operation_name, payload)
            if operation_name in ASYNC_ADAPTER_OPERATIONS and wait_for_job:
                job = context.job_runner.wait(payload["project_id"], result["job_id"])
                context.job_runner.close()
                if job.state != "completed":
                    kind = JobErrorKind(job.error_kind or JobErrorKind.INTERNAL.value)
                    raise JobError(kind, f"job {job.job_id} ended in state {job.state}",
                                   job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
                result = {"job_id": job.job_id, "state": job.state}
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
def _configure_cli(context: click.Context, project_root: Path | None) -> None:
    existing = context.obj if isinstance(context.obj, dict) else {}
    if existing.get("settings") is None:
        existing["settings"] = Settings(project_root=project_root or Settings().project_root)
    context.obj = existing


app = click.Group(
    name="cctv-pipeline",
    help="CCTV production pipeline",
    callback=_configure_cli,
    params=[click.Option(["--project-root"], type=click.Path(path_type=Path),
                         help="Existing project root from Settings.")],
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
