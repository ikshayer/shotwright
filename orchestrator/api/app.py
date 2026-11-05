"""FastAPI transport for the project-local orchestrator."""

from __future__ import annotations

import hmac
import json
import os
import secrets
from typing import Annotated, Any

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from orchestrator.domain.entities import Project, RubricScores, Script, Shot
from orchestrator.domain.state import ReviewApprovalError, TransitionError
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.containment import ContainmentError, ProjectRoot, UploadRejected
from orchestrator.platform.errors import JobError
from orchestrator.platform.settings import Settings
from orchestrator.service.dependencies import revise_script, revise_shot
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict

from .routes import OPERATIONS, Operation, tool_manifest


class TokenConflict(Exception):
    """A stale API mutation token."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"stale mutation token: expected {expected}, actual {actual}")
        self.expected, self.actual = expected, actual


class MissingAdapter(Exception):
    def __init__(self, adapter: str) -> None:
        super().__init__(f"missing adapter: {adapter}")
        self.adapter = adapter


class OperationContext:
    """Runtime dependencies and the shared dispatcher used by HTTP and CLI."""

    def __init__(
        self,
        settings: Settings,
        root: ProjectRoot,
        *,
        job_queues: dict[str, JobQueue] | None = None,
    ) -> None:
        self.settings = settings
        self.root = root
        self.job_queues = job_queues or {}

    def _project_root(self, project_id: str, *, create: bool = False) -> ProjectRoot:
        resolved = self.root.resolve(project_id)
        if create:
            self.root.make_dir(project_id)
        elif not resolved.is_dir():
            raise RecordNotFound(f"project {project_id!r} does not exist")
        return ProjectRoot(resolved)

    def _store(self, project_id: str, *, create: bool = False) -> ProjectStore:
        return ProjectStore(self._project_root(project_id, create=create))

    def invoke(self, operation: str, data: dict[str, Any]) -> Any:
        if operation == "create_project":
            project = Project.model_validate(data)
            store = self._store(project.project_id, create=True)
            store.save(project, expected_version=0)
            return project.model_dump(mode="json")
        if operation == "list_projects":
            projects = []
            for path in sorted(self.root.path.iterdir(), key=lambda item: item.name):
                if not path.is_dir():
                    continue
                try:
                    projects.append(ProjectStore(ProjectRoot(path)).load(Project).model_dump(mode="json"))
                except RecordNotFound:
                    continue
            return projects
        if operation == "load_project":
            return self._store(data["project_id"]).load(Project).model_dump(mode="json")
        if operation == "revise_script":
            script = Script.model_validate(data["script"])
            changed = revise_script(self._store(data["project_id"]), script,
                                    expected_version=data["expected_version"])
            return {"script": script.model_dump(mode="json"), "invalidated": list(changed)}
        if operation == "revise_shot":
            shot = Shot.model_validate(data["shot"])
            revised = revise_shot(self._store(data["project_id"]), shot,
                                  expected_version=data["expected_version"])
            return revised.model_dump(mode="json")
        if operation == "score_rubric":
            scores = RubricScores.model_validate(data.get("scores", data))
            return {"scores": scores.model_dump(mode="json"), "passed": scores.passed()}
        if operation == "report_jobs":
            queue = self.job_queues.get(data["project_id"])
            if queue is None:
                return self._read_job_reports(data["project_id"])
            return self._read_job_reports(data["project_id"])
        item = next((route for route in OPERATIONS if route.name == operation), None)
        if item is None:
            raise MissingAdapter(operation)
        raise MissingAdapter(item.adapter)

    def _read_job_reports(self, project_id: str) -> list[dict[str, Any]]:
        project_root = self._project_root(project_id)
        jobs_path = project_root.resolve("state/jobs")
        if not jobs_path.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for path in sorted(jobs_path.glob("*.jsonl")):
            # Resolve every discovered name through the one containment authority.
            contained = project_root.resolve(path.relative_to(project_root.path).as_posix())
            for line in contained.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    result.append(json.loads(line))
        return result


def _error(status: int, code: str, message: str, **details: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": code,
        "message": message,
        "details": details,
    })


def create_app(
    settings: Settings | None = None,
    *,
    token: str | None = None,
    job_queues: dict[str, JobQueue] | None = None,
) -> FastAPI:
    """Create an app; invalid/non-loopback settings fail immediately at startup."""
    configured = settings or Settings()
    # Pydantic's Settings validator rejects non-loopback hosts before the app is served.
    active_token = token if token is not None else os.environ.get("CCTV_LOOPBACK_TOKEN")
    if not active_token:
        active_token = secrets.token_urlsafe(32)
    root = ProjectRoot(configured.project_root)
    context = OperationContext(configured, root, job_queues=job_queues)
    app = FastAPI(title="CCTV Production Pipeline API", version="0.1.0")
    app.state.settings = configured
    app.state.loopback_token = active_token
    app.state.context = context
    app.state.tool_manifest = tool_manifest

    @app.middleware("http")
    async def deny_non_loopback_origins(request: Request, call_next: Any) -> Any:
        origin = request.headers.get("origin")
        if origin:
            import ipaddress
            from urllib.parse import urlsplit

            hostname = urlsplit(origin).hostname
            allowed = hostname == "localhost"
            if hostname:
                try:
                    allowed = allowed or ipaddress.ip_address(hostname).is_loopback
                except ValueError:
                    pass
            if not allowed:
                return _error(403, "origin_denied", "cross-origin requests are not allowed")
        return await call_next(request)

    async def authenticate(request: Request) -> None:
        supplied = request.headers.get("X-Loopback-Token", "")
        if not hmac.compare_digest(supplied, app.state.loopback_token):
            raise PermissionError

    @app.exception_handler(PermissionError)
    async def unauthorized(_request: Request, _exc: PermissionError) -> JSONResponse:
        return _error(401, "unauthorized", "a valid loopback token is required")

    @app.exception_handler(VersionConflict)
    async def version_conflict(_request: Request, exc: VersionConflict) -> JSONResponse:
        current = exc.current.model_dump(mode="json") if hasattr(exc.current, "model_dump") else exc.current
        return _error(409, "version_conflict", str(exc), expected=exc.expected,
                      actual=exc.actual, current=current, record=exc.record)

    @app.exception_handler(TokenConflict)
    async def token_conflict(_request: Request, exc: TokenConflict) -> JSONResponse:
        return _error(409, "token_conflict", str(exc), expected=exc.expected, actual=exc.actual)

    @app.exception_handler(RecordNotFound)
    async def not_found(_request: Request, exc: RecordNotFound) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(ContainmentError)
    async def containment(_request: Request, exc: ContainmentError) -> JSONResponse:
        return _error(400, "containment_error", str(exc), field="path", value=exc.path)

    @app.exception_handler(UploadRejected)
    async def upload_rejected(_request: Request, exc: UploadRejected) -> JSONResponse:
        details = {"field": "file"}
        if exc.limit_name is not None:
            details.update(limit_name=exc.limit_name, limit=exc.limit, observed=exc.observed)
        return _error(400, "upload_rejected", str(exc), **details)

    @app.exception_handler(TransitionError)
    async def transition_error(_request: Request, exc: TransitionError) -> JSONResponse:
        return _error(422, "transition_refused", str(exc), reason=str(exc))

    @app.exception_handler(ReviewApprovalError)
    async def review_error(_request: Request, exc: ReviewApprovalError) -> JSONResponse:
        return _error(422, "review_approval_refused", str(exc), reason=str(exc))

    @app.exception_handler(JobError)
    async def job_error(_request: Request, exc: JobError) -> JSONResponse:
        status = 409 if exc.retryable else 422
        return _error(status, "job_error", exc.message, kind=exc.kind.value,
                      retryable=exc.retryable, remediation=exc.remediation,
                      job_id=exc.job_id, shot_id=exc.shot_id, run_id=exc.run_id)

    @app.exception_handler(MissingAdapter)
    async def missing_adapter(_request: Request, exc: MissingAdapter) -> JSONResponse:
        return _error(501, "missing_adapter", str(exc), adapter=exc.adapter)

    @app.exception_handler(ValidationError)
    async def validation_error(_request: Request, exc: ValidationError) -> JSONResponse:
        return _error(422, "validation_error", str(exc), field="payload")

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, "validation_error", str(exc), field="payload")

    @app.get("/tool-manifest", name="tool_manifest", summary="Get generated agent tool definitions")
    async def get_tool_manifest() -> list[dict[str, Any]]:
        return tool_manifest()

    for operation in OPERATIONS:
        _register_operation(app, operation, context, authenticate)
    return app


def _register_operation(app: FastAPI, operation: Operation, context: OperationContext, authenticate: Any) -> None:
    from inspect import Parameter, Signature

    async def endpoint(request: Request, payload: dict[str, Any] | None = None,
                       **path_values: str) -> Any:
        if operation.mutating:
            await authenticate(request)
        data = dict(payload or {})
        data.update(path_values)
        return operation.invoke(context, data)

    path_names = [part[1:-1] for part in operation.path.split("/") if part.startswith("{")]
    params = [Parameter(name, Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
              for name in path_names]
    params.extend((
        Parameter("request", Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
        Parameter("payload", Parameter.POSITIONAL_OR_KEYWORD, default=None,
                  annotation=Annotated[dict[str, Any] | None, Body()]),
    ))
    endpoint.__signature__ = Signature(params, return_annotation=Any)  # type: ignore[attr-defined]
    endpoint.__name__ = operation.name
    endpoint.__doc__ = operation.summary
    app.add_api_route(operation.path, endpoint, methods=[operation.method],
                      name=operation.name, operation_id=operation.name,
                      summary=operation.summary, response_model=None)


def get_tool_manifest() -> list[dict[str, Any]]:
    """Return the generated agent tool manifest."""
    return tool_manifest()
