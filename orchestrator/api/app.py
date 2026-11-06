"""FastAPI transport for the project-local orchestrator."""

from __future__ import annotations

import hmac
import math
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from orchestrator.domain.entities import Project, RubricScores, Script, Shot
from orchestrator.jobs.journal import read_job_records
from orchestrator.jobs.lease import GpuLease, PynvmlProbe
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine, SystemHostResources
from orchestrator.platform.containment import ProjectRoot, validate_upload
from orchestrator.platform.settings import Settings
from orchestrator.service.dependencies import revise_script, revise_shot
from orchestrator.service.runner import (
    AdapterKind,
    AdapterRegistry,
    AdapterRequest,
    JobRunner,
)
from orchestrator.store.json_store import ProjectStore, RecordNotFound

from .errors import (
    MAPPED,
    AdapterRequestInvalid,
    MissingAdapter,
    RequestMismatch,
    describe,
    error_body,
)
from .manifest import build_manifest
from .routes import MAGIC_HEADER_BYTES, OPERATIONS, Operation, UploadedFile

#: Hostnames a browser may legitimately use to reach a loopback-bound server
#: (Amendment 2). The configured port is appended to each as an accepted variant.
_LOOPBACK_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]")

#: Verbs that require the loopback token. Kept in step with Operation.mutating by a
#: startup check in create_app, because the middleware cannot consult the table.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class OperationContext:
    """Runtime dependencies and the shared dispatcher used by HTTP and CLI."""

    def __init__(
        self,
        settings: Settings,
        root: ProjectRoot | None,
        *,
        job_queues: dict[str, JobQueue] | None = None,
        job_runner: JobRunner | None = None,
    ) -> None:
        self.settings = settings
        self.root = root
        self.job_queues = job_queues or {}
        if job_runner is None and root is not None:
            runtime = _DefaultRuntime(settings, root)
            job_runner = JobRunner(
                lambda project_id: ProjectStore(runtime.project_root(project_id)), settings,
                queue_for=runtime, adapters=runtime.registry,
            )
        self.job_runner = job_runner

    def _project_root(self, project_id: str, *, create: bool = False) -> ProjectRoot:
        if self.root is None:
            raise RuntimeError("project root is not configured")
        resolved = self.root.resolve(project_id)
        if create:
            self.root.make_dir(project_id)
        elif not resolved.is_dir():
            raise RecordNotFound(f"project {project_id!r} does not exist")
        return ProjectRoot(
            resolved,
            use_windows_share_delete_reads=self.settings.use_windows_share_delete_reads,
        )

    def _store(self, project_id: str, *, create: bool = False) -> ProjectStore:
        return ProjectStore(self._project_root(project_id, create=create))

    def invoke(self, operation: str, data: dict[str, Any]) -> Any:
        if operation == "health":
            return {"status": "ok"}
        if operation == "create_project":
            project = Project.model_validate(data)
            store = self._store(project.project_id, create=True)
            store.save(project, expected_version=0)
            return project.model_dump(mode="json")
        if operation == "list_projects":
            projects = []
            for path in sorted(self.root.path.iterdir(), key=lambda item: item.name):
                try:
                    contained = self.root.resolve(path.name)
                    if not contained.is_dir():
                        continue
                    projects.append(
                        ProjectStore(
                            ProjectRoot(
                                contained,
                                use_windows_share_delete_reads=self.settings.use_windows_share_delete_reads,
                            )
                        ).load(Project).model_dump(mode="json")
                    )
                except RecordNotFound:
                    continue
            return projects
        if operation == "load_project":
            return self._store(data["project_id"]).load(Project).model_dump(mode="json")
        if operation == "revise_script":
            script = Script.model_validate(data["script"])
            _require_path_match(data, "episode_id", script.episode_id)
            changed = revise_script(self._store(data["project_id"]), script,
                                    expected_version=data["expected_version"])
            return {"script": script.model_dump(mode="json"), "invalidated": list(changed)}
        if operation == "revise_shot":
            shot = Shot.model_validate(data["shot"])
            _require_path_match(data, "shot_id", shot.shot_id)
            _require_path_match(data, "episode_id", shot.episode_id)
            revised = revise_shot(self._store(data["project_id"]), shot,
                                  expected_version=data["expected_version"])
            return revised.model_dump(mode="json")
        if operation == "score_rubric":
            scores = RubricScores.model_validate(data.get("scores", data))
            return {"scores": scores.model_dump(mode="json"), "passed": scores.passed()}
        if operation == "create_asset":
            # Transport-level validation of the uploaded bytes (FR-25, AT-17). It runs
            # before the adapter check because validating the request is this layer's
            # job, while storing the asset is the absent adapter's: a well-formed
            # upload therefore still answers 501, and a malformed one answers 400.
            # Pixel and duration caps are NOT enforced here. They need a decoder, and
            # the only alternative was to believe a dimension the caller supplied,
            # which validates nothing: a caller that wants past the cap simply omits or
            # understates it. Enforcing them belongs with the adapter that decodes the
            # file. Extension, magic bytes and the byte caps are enforced.
            for upload in data.get("uploads", ()):
                validate_upload(upload.filename, upload.head, upload.size_bytes,
                                self.settings.upload)
        if operation == "report_jobs":
            queue = self.job_queues.get(data["project_id"])
            return self._read_job_reports(data["project_id"], root=queue.root if queue else None)
        if operation in {"build_scene", "render_preview", "generate_media", "degrade_media"}:
            if self.job_runner is None:
                raise RuntimeError("default JobRunner was not configured")
            kind = {
                "build_scene": AdapterKind.SCENE,
                "render_preview": AdapterKind.RENDER,
                "generate_media": AdapterKind.GENERATE,
                "degrade_media": AdapterKind.DEGRADE,
            }[operation]
            record_id = data.get("shot_id")
            if not record_id:
                raise AdapterRequestInvalid(f"{operation} requires shot_id")
            store = self._store(data["project_id"])
            shot = store.load(Shot, record_id, episode_id=data.get("episode_id"))
            payload = data.get("request", data)
            record_version = data.get("record_version")
            if record_version is None:
                record_version = getattr(payload, "shot_version", None)
            if record_version is None and isinstance(payload, dict):
                record_version = payload.get("shot_version")
            if record_version is None:
                record_version = shot.version
            if type(record_version) is not int or record_version < 1:
                raise AdapterRequestInvalid("record_version must be a positive content version")
            resources = data.get("resources", {})
            if not isinstance(resources, dict):
                raise AdapterRequestInvalid("resources must be an object")
            required_vram = data.get("required_vram_gb", resources.get("required_vram_gb", 0.0))
            if (isinstance(required_vram, bool) or not isinstance(required_vram, (int, float))
                    or not math.isfinite(required_vram)
                    or required_vram < 0):
                raise AdapterRequestInvalid("required_vram_gb must be a finite nonnegative number")
            request = AdapterRequest(
                kind=kind, payload=payload, record_id=record_id,
                record_version=record_version, episode_id=shot.episode_id,
                required_vram_gb=required_vram,
                required_ram_gb=data.get("required_ram_gb", resources.get("required_ram_gb", 0.0)),
                required_disk_gb=data.get("required_disk_gb", resources.get("required_disk_gb", 0.0)),
            )
            job = self.job_runner.submit(data["project_id"], request)
            return {"job_id": job.job_id, "state": job.state}
        item = next((route for route in OPERATIONS if route.name == operation), None)
        if item is None:
            raise MissingAdapter(operation)
        raise MissingAdapter(item.adapter)

    def _read_job_reports(
        self, project_id: str, *, root: ProjectRoot | None = None
    ) -> list[dict[str, Any]]:
        project_root = root or self._project_root(project_id)
        jobs_path = project_root.resolve("state/jobs")
        if not jobs_path.is_dir():
            return []
        job_ids = {path.stem for path in jobs_path.glob("*.json")}
        job_ids.update(path.stem for path in jobs_path.glob("*.jsonl"))
        return read_job_records(project_root, list(job_ids))


def _require_path_match(data: dict[str, Any], field: str, body_value: str) -> None:
    """Refuse a body whose identifier contradicts the URL that addressed it.

    This is request validation, not a business rule: it decides nothing about the
    record, only that the request describes the record it claims to.
    """
    supplied = data.get(field)
    if supplied is not None and supplied != body_value:
        raise RequestMismatch(field, str(supplied), str(body_value))


#: Chunk used only when the parser did not report a size, to measure without buffering.
_MEASURE_CHUNK_BYTES = 64 * 1024


async def _describe_upload(item: UploadFile) -> UploadedFile:
    """Read a file's magic-byte header and establish its true size, nothing more.

    The size comes from the multipart parser, which counted the bytes as it wrote them.
    Where it did not report one the file is measured in chunks and the bytes discarded,
    so memory stays bounded by the chunk no matter how large the upload is.
    """
    head = await item.read(MAGIC_HEADER_BYTES)
    size = item.size
    if size is None:
        size = len(head)
        while chunk := await item.read(_MEASURE_CHUNK_BYTES):
            size += len(chunk)
    return UploadedFile(filename=item.filename or "", head=head, size_bytes=size)


def _error(status: int, code: str, message: str, **details: Any) -> JSONResponse:
    """Emit a transport-only failure in the shared error shape."""
    return JSONResponse(status_code=status, content=error_body(code, message, **details))


async def _mapped_exception(_request: Request, exc: Exception) -> JSONResponse:
    """Answer any exception the shared table maps; let anything else stay a 500."""
    mapped = describe(exc)
    if mapped is None:
        raise exc
    status, body = mapped
    return JSONResponse(status_code=status, content=body)


def create_app(
    settings: Settings,
    token: str,
    *,
    job_queues: dict[str, JobQueue] | None = None,
    job_runner: JobRunner | None = None,
) -> FastAPI:
    """Create an app; invalid/non-loopback settings fail immediately at startup."""
    configured = settings
    if not token:
        raise ValueError("a loopback token must be provided")
    root = ProjectRoot(
        configured.project_root,
        use_windows_share_delete_reads=configured.use_windows_share_delete_reads,
    )
    if job_runner is None:
        runtime = _DefaultRuntime(configured, root)
        job_runner = JobRunner(
            lambda project_id: ProjectStore(runtime.project_root(project_id)), configured,
            queue_for=runtime, adapters=runtime.registry,
        )
    runner = job_runner
    context = OperationContext(configured, root, job_queues=job_queues, job_runner=runner)
    app = FastAPI(title="CCTV Production Pipeline API", version="0.1.0")
    app.state.settings = configured
    app.state.loopback_token = token
    app.state.context = context
    app.state.operations = OPERATIONS

    # All three checks live in one middleware so their order is explicit in the code
    # rather than implied by registration order, which Starlette inverts.
    allowed_hosts = frozenset(
        {name.lower() for name in _LOOPBACK_HOSTNAMES}
        | {"::1"}
        | {f"{name.lower()}:{configured.server.port}" for name in _LOOPBACK_HOSTNAMES}
    )
    # The token is checked in middleware, which runs before routing and so cannot read
    # the operation table; it keys on the verb instead. That the two agree is therefore
    # an invariant, checked here at startup so they cannot drift apart unnoticed.
    for declared in OPERATIONS:
        if declared.mutating != (declared.method in _MUTATING_METHODS):
            raise ValueError(
                f"operation {declared.name!r} is marked mutating={declared.mutating} but "
                f"uses {declared.method}; the loopback-token check keys on the verb"
            )

    @app.middleware("http")
    async def enforce_loopback_policy(request: Request, call_next: Any) -> Any:
        # Amendment 2 (DNS rebinding). A cross-site simple GET carries no Origin and
        # reads need no token, so an Origin-only check leaves every read exposed: a
        # page whose DNS re-points to 127.0.0.1 is same-origin to the browser, so CORS
        # never engages, and binding to loopback does not help because the attacker's
        # own browser is on loopback. Validating Host is what makes a missing Origin
        # safe, so it runs on every request, reads included, before routing.
        #
        # Starlette's TrustedHostMiddleware cannot serve here, though the contract
        # offers it: it matches on headers["host"].split(":")[0], which reduces both
        # "[::1]" and "[::1]:8765" to "[", so an IPv6 loopback literal can never
        # match, and it answers 400 where this contract requires 403.
        # Exactly one Host is required. HTTP/1.1 permits only one, and accepting the
        # first of several lets an attacker append a header a proxy or the application
        # may disagree about which to honour.
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or hosts[0].strip().lower() not in allowed_hosts:
            return _error(403, "host_denied",
                          "the Host header is not a configured loopback address")
        origins = request.headers.getlist("origin")
        if len(origins) > 1:
            return _error(403, "origin_denied", "a request carries more than one Origin")
        if origins:
            target = request.url
            try:
                # urlsplit itself raises on a malformed authority such as "http://[",
                # so it has to be inside the guard; it previously sat outside and made
                # a crafted Origin a 500.
                source = urlsplit(origins[0])
                source_port = source.port or (443 if source.scheme == "https" else 80)
                target_port = target.port or (443 if target.scheme == "https" else 80)
            except ValueError:
                return _error(403, "origin_denied", "cross-origin requests are not allowed")
            if (source.scheme.lower(), (source.hostname or "").lower(), source_port) != (
                target.scheme.lower(), (target.hostname or "").lower(), target_port
            ):
                return _error(403, "origin_denied", "cross-origin requests are not allowed")
        # Authentication belongs here, ahead of routing. Inside a route handler it runs
        # after the framework has already read and parsed the body, so an unauthenticated
        # request with a malformed body answered 422 instead of 401, and an
        # unauthenticated multipart request got its parts spooled to disk first.
        if request.method in _MUTATING_METHODS:
            supplied = request.headers.get("X-Loopback-Token", "")
            # Compared as bytes: compare_digest rejects a str containing non-ASCII,
            # which a client controls, and that would be a 500 instead of a 401.
            if not hmac.compare_digest(supplied.encode("utf-8"),
                                       str(app.state.loopback_token).encode("utf-8")):
                return _error(401, "unauthorized", "a valid loopback token is required")
        return await call_next(request)

    # Every exception the contract's table covers is mapped by the shared module, so
    # the CLI cannot answer the same failure differently. Registered per concrete type
    # rather than on Exception, so an unmapped error stays a 500 and a visible bug.
    for mapped_type in MAPPED:
        app.add_exception_handler(mapped_type, _mapped_exception)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Framework-raised failures — 404, 405, a missing multipart boundary — otherwise
        # answer Starlette's own {"detail": ...}, so a client would have to parse two
        # error formats from one API. An unmapped *exception* is still a 500 and a bug;
        # this only covers failures the framework itself has already classified.
        return _error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, "validation_error", str(exc), field="payload")

    for operation in OPERATIONS:
        _register_operation(app, operation, context)
    # The manifest is deliberately NOT built or cached here. It is derived from the
    # route table on demand by build_manifest(), and storing a copy on app.state would
    # be the duplicated source of truth this contract forbids.
    return app


def _register_operation(app: FastAPI, operation: Operation, context: OperationContext) -> None:
    from inspect import Parameter, Signature

    async def endpoint(request: Request, **values: Any) -> Any:
        # No authentication here: the middleware has already enforced the token before
        # the body was read.
        payload = values.pop("payload", None)
        files: list[UploadFile] = values.pop("files", None) or []
        data: dict[str, Any] = payload.model_dump(mode="python") if payload is not None else {}
        # Path values are always present; optional form fields are dropped when unset so
        # the operation can tell "not supplied" from a real value.
        data.update({key: value for key, value in values.items() if value is not None})
        if files:
            data["uploads"] = [await _describe_upload(item) for item in files]
        result = operation.invoke(context, data)
        if operation.name in {"build_scene", "render_preview", "generate_media", "degrade_media"}:
            return JSONResponse(status_code=202, content=result)
        return result

    path_names = [part[1:-1] for part in operation.path.split("/") if part.startswith("{")]
    params = [Parameter(name, Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
              for name in path_names]
    params.append(Parameter("request", Parameter.POSITIONAL_OR_KEYWORD, annotation=Request))
    if operation.upload:
        # A multipart surface, so uploaded bytes can be validated by extension, magic
        # bytes and the section 6.1 caps (FR-25). The file list is optional: a caller
        # that sends no file still reaches the operation, and an adapter-absent
        # operation still answers 501 rather than a validation error.
        params.append(Parameter(
            "files", Parameter.POSITIONAL_OR_KEYWORD, default=[],
            annotation=Annotated[list[UploadFile], File()],
        ))
    elif operation.request_model is not None:
        # A mutating body is required, so its schema is the model itself rather than
        # "model or null". An optional body publishes anyOf[model, null], which hides
        # the model's own fields from every consumer that reads the route's schema.
        if operation.mutating:
            params.append(Parameter(
                "payload", Parameter.POSITIONAL_OR_KEYWORD,
                annotation=Annotated[operation.request_model, Body()],
            ))
        else:
            params.append(Parameter(
                "payload", Parameter.POSITIONAL_OR_KEYWORD, default=None,
                annotation=Annotated[operation.request_model | None, Body()],
            ))
    endpoint.__signature__ = Signature(params, return_annotation=Any)  # type: ignore[attr-defined]
    endpoint.__name__ = operation.name
    endpoint.__doc__ = operation.summary
    app.add_api_route(operation.path, endpoint, methods=[operation.method],
                      name=operation.name, operation_id=operation.name,
                      summary=operation.summary, response_model=None, include_in_schema=True,
                      status_code=202 if operation.name in {
                          "build_scene", "render_preview", "generate_media", "degrade_media",
                      } else 200,
                      openapi_extra={"parameters": [{
                          "name": "X-Loopback-Token",
                          "in": "header",
                          "required": True,
                          "schema": {"type": "string"},
                          "description": "Loopback token required for mutations.",
                      }]} if operation.mutating else None)


class _CpuWorker:
    """Worker identity for CPU-only PostProfile work; it never acquires a GPU lease."""

    def unload(self) -> None:
        return None

    def terminate(self) -> bool:
        return True

    def respawn(self) -> int:
        return 0

    def pid(self) -> int | None:
        return None


class _RuntimeAdapter:
    def __init__(self, runtime: _DefaultRuntime, kind: AdapterKind) -> None:
        self.runtime, self.kind = runtime, kind
        self._cancel_check: Any = None

    def set_cancel_check(self, check: Any) -> None:
        self._cancel_check = check

    def invoke(self, request: Any, job: Any, *, queue: JobQueue) -> Any:
        adapter = self.runtime.adapter_for(queue.root, self.kind)
        from orchestrator.service.runner import _AdapterShim

        shim = _AdapterShim(self.kind, adapter, self.runtime.settings)
        shim.cancel_check = self._cancel_check
        try:
            return shim.invoke(request, job, queue=queue)
        finally:
            self._cancel_check = None

    def close(self) -> None:
        self.runtime.close()


class _DefaultRuntime:
    """Lazily build project/kind adapter queues after HTTP/CLI submission returns."""

    def __init__(self, settings: Settings, root: ProjectRoot) -> None:
        self.settings, self.root = settings, root
        self._lock = __import__("threading").RLock()
        self._queues: dict[tuple[str, AdapterKind], JobQueue] = {}
        self._adapters: dict[tuple[str, AdapterKind], Any] = {}
        self._nvml: Any = None
        self.registry = AdapterRegistry({kind: _RuntimeAdapter(self, kind) for kind in AdapterKind})

    def project_root(self, project_id: str) -> ProjectRoot:
        path = self.root.resolve(project_id)
        if not path.is_dir():
            raise RecordNotFound(f"project {project_id!r} does not exist")
        return ProjectRoot(
            path,
            use_windows_share_delete_reads=self.settings.use_windows_share_delete_reads,
        )

    def adapter_for(self, project_root: ProjectRoot, kind: AdapterKind) -> Any:
        key = (str(project_root.path), kind)
        with self._lock:
            if key not in self._adapters:
                if kind in {AdapterKind.SCENE, AdapterKind.RENDER}:
                    from orchestrator.adapters.blender.runner import BlenderAdapter
                    adapter = BlenderAdapter(project_root, self.settings)
                elif kind is AdapterKind.GENERATE:
                    from orchestrator.adapters.comfyui.runner import ComfyAdapter
                    adapter = ComfyAdapter(project_root, self.settings)
                else:
                    from orchestrator.adapters.postprofile.runner import PostProfileAdapter
                    adapter = PostProfileAdapter(project_root, self.settings)
                self._adapters[key] = adapter
            return self._adapters[key]

    def queue_for(self, project_id: str) -> JobQueue:
        return self.for_kind(project_id, AdapterKind.RENDER)

    def __call__(self, project_id: str) -> JobQueue:
        return self.queue_for(project_id)

    def for_kind(self, project_id: str, kind: AdapterKind) -> JobQueue:
        root = self.project_root(project_id)
        key = (str(root.path), kind)
        with self._lock:
            if key in self._queues:
                return self._queues[key]
            adapter = self.adapter_for(root, kind)
            if self._nvml is None:
                self._nvml = PynvmlProbe()
            worker = getattr(adapter, "worker", _CpuWorker())
            lease = GpuLease(root, self.settings, nvml=self._nvml, worker=worker)
            queue = JobQueue(root, self.settings, lease, ProjectStore(root), nvml=self._nvml,
                             host=SystemHostResources(), quarantine=ProjectQuarantine(root))
            self._queues[key] = queue
            return queue

    def close(self) -> None:
        with self._lock:
            adapters = tuple(self._adapters.values())
            self._adapters.clear()
            self._queues.clear()
        first: Exception | None = None
        for adapter in adapters:
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001 - close every adapter, then report first failure.
                    first = first or exc
        if first is not None:
            raise first


def get_tool_manifest(app: FastAPI) -> dict[str, Any]:
    """Return the generated agent tool manifest."""
    return build_manifest(app)


def serve(settings: Settings, token: str, *, job_queues: dict[str, JobQueue] | None = None) -> None:
    """Run the local server at the validated bind address and port from Settings."""
    import uvicorn

    uvicorn.run(
        create_app(settings, token, job_queues=job_queues),
        host=settings.server.host,
        port=settings.server.port,
    )
