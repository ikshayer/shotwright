"""FastAPI transport for the project-local orchestrator."""

from __future__ import annotations

import base64
import binascii
import hmac
import html
import math
import uuid
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from orchestrator.adapters.assets.models import (
    AssetAcquireRequest,
    AssetCategoryRequest,
    AssetPreviewRequest,
    AssetSearchRequest,
)
from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    Location,
    PostProfile,
    Project,
    RubricScores,
    Script,
    Shot,
)
from orchestrator.jobs.gpu_slot import GpuExecutionSlot
from orchestrator.jobs.journal import read_job_records
from orchestrator.jobs.lease import GpuLease, PynvmlProbe
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine, SystemHostResources
from orchestrator.platform.containment import ProjectRoot, validate_upload
from orchestrator.platform.settings import Settings
from orchestrator.service.assets import (
    AssetRegistrationInvalid,
    register_asset,
    validate_asset_source,
)
from orchestrator.service.compose import compose_render_request
from orchestrator.service.dependencies import (
    create_camera_profile,
    create_episode,
    create_location,
    create_post_profile,
    create_project,
    draft_script,
    propose_shots,
    revise_location,
    revise_script,
    revise_shot,
)
from orchestrator.service.minimax_h3 import (
    H3CancelBody,
    H3GenerateBody,
    H3RequestError,
    H3SetupError,
    compose_h3_request,
    effective_h3_settings,
)
from orchestrator.service.runner import (
    AdapterKind,
    AdapterRegistry,
    AdapterRequest,
    JobRunner,
)
from orchestrator.service.sourcing import acquire_asset as source_acquire_asset
from orchestrator.service.sourcing import list_asset_categories as source_list_asset_categories
from orchestrator.service.sourcing import preview_asset as source_preview_asset
from orchestrator.service.sourcing import search_assets as source_search_assets
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
from .routes import (
    ASYNC_ADAPTER_OPERATIONS,
    MAGIC_HEADER_BYTES,
    OPERATIONS,
    AssetRegistration,
    Operation,
    UploadedFile,
)

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
        self._runtime: _DefaultRuntime | None = None
        if job_runner is None and root is not None:
            runtime = _DefaultRuntime(settings, root)
            self._runtime = runtime
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
            created = create_project(store, project)
            return created.model_dump(mode="json")
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
        if operation == "create_camera_profile":
            profile = CameraProfile.model_validate(data["camera_profile"])
            _require_path_match(data, "camera_id", profile.camera_id)
            created = create_camera_profile(self._store(data["project_id"]), profile)
            return created.model_dump(mode="json")
        if operation == "create_post_profile":
            profile = PostProfile.model_validate(data["post_profile"])
            _require_path_match(data, "profile_name", profile.name)
            created = create_post_profile(self._store(data["project_id"]), profile)
            return created.model_dump(mode="json")
        if operation == "create_episode":
            episode = Episode.model_validate(data["episode"])
            _require_path_match(data, "project_id", episode.project_id)
            created = create_episode(self._store(data["project_id"]), episode)
            return created.model_dump(mode="json")
        if operation == "create_location":
            location = Location.model_validate(data["location"])
            _require_path_match(data, "location_id", location.location_id)
            created = create_location(self._store(data["project_id"]), location)
            return created.model_dump(mode="json")
        if operation == "revise_location":
            location = Location.model_validate(data["location"])
            _require_path_match(data, "location_id", location.location_id)
            changed = revise_location(
                self._store(data["project_id"]),
                location,
                expected_version=data["expected_version"],
                episode_ids=data["episode_ids"],
            )
            return {"location": location.model_dump(mode="json"), "invalidated": list(changed)}
        if operation == "draft_script":
            script = Script.model_validate(data["script"])
            _require_path_match(data, "episode_id", script.episode_id)
            created = draft_script(self._store(data["project_id"]), script)
            return created.model_dump(mode="json")
        if operation == "propose_shots":
            shots = [Shot.model_validate(item) for item in data["shots"]]
            for shot in shots:
                _require_path_match(data, "episode_id", shot.episode_id)
            created = propose_shots(self._store(data["project_id"]), shots)
            return [shot.model_dump(mode="json") for shot in created]
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
        if operation == "derive_script_scene_plan":
            from orchestrator.service.script_authoring import derive_script_scene_plan
            return derive_script_scene_plan(
                self._store(data["project_id"]),
                episode_id=data["episode_id"],
                script_id=data["script_id"],
                expected_version=data["expected_version"],
                expected_episode_version=data["expected_episode_version"],
                expected_shot_versions=data["expected_shot_versions"],
                expected_location_versions=data["expected_location_versions"],
            )
        if operation == "list_episode_credits":
            from orchestrator.service.credits import collect_episode_credits
            return collect_episode_credits(
                self._store(data["project_id"]), episode_id=data["episode_id"]
            )
        if operation == "score_rubric":
            scores = RubricScores.model_validate(data.get("scores", data))
            return {"scores": scores.model_dump(mode="json"), "passed": scores.passed()}
        if operation == "create_asset":
            uploads = data.get("uploads", ())
            if len(uploads) != 1:
                raise AssetRegistrationInvalid("create_asset requires exactly one permitted upload file")
            registration = AssetRegistration.model_validate({
                field: data[field]
                for field in AssetRegistration.model_fields
                if field in data
            })
            upload: UploadedFile = uploads[0]
            source = upload.source_path or upload.stream
            if source is None:
                raise AssetRegistrationInvalid(
                    "the uploaded asset has no readable source file or stream"
                )
            # Registration accepts every upload kind supported by the shared closed
            # extension/magic allowlist. Geometry restrictions are enforced on use.
            validate_upload(
                upload.filename,
                upload.head,
                upload.size_bytes,
                self.settings.upload,
            )
            validate_asset_source(
                source,
                filename=upload.filename,
                license=registration.license,
                source_url=registration.source_url,
                generated=registration.generated,
            )
            asset = register_asset(
                self._store(data["project_id"]), source, filename=upload.filename,
                asset_id=registration.asset_id, asset_type=registration.type,
                generated=registration.generated, source_url=registration.source_url,
                provider=registration.provider, license=registration.license,
                model_or_voice_license=registration.model_or_voice_license,
                attribution=registration.attribution,
                attribution_surface_required=registration.attribution_surface_required,
                reuse_scope=registration.reuse_scope,
                expected_version=registration.expected_version,
            )
            return asset.model_dump(mode="json")
        if operation == "list_asset_categories":
            request = AssetCategoryRequest.model_validate({
                key: value for key, value in data.items()
                if key in AssetCategoryRequest.model_fields
            })
            return source_list_asset_categories(
                self._store(data["project_id"]), self.settings, request
            )
        if operation == "asset_preview":
            request = AssetPreviewRequest.model_validate({
                key: value for key, value in data.items()
                if key in AssetPreviewRequest.model_fields
            })
            return source_preview_asset(
                self._store(data["project_id"]), self.settings, request
            ).model_dump(mode="json")
        if operation in {"search_assets", "asset_browser"}:
            q = data.get("q")
            saved = []
            store = self._store(data["project_id"])
            if operation == "asset_browser":
                assets_dir = self._project_root(data["project_id"]).resolve("assets/registry")
                if assets_dir.is_dir():
                    from orchestrator.domain.entities import Asset
                    for record in sorted(assets_dir.glob("*.json")):
                        saved.append(store.load(Asset, record.stem).model_dump(mode="json"))
            if operation == "asset_browser" and not q:
                return {"project_id": data["project_id"], "provider": data.get("provider", "polyhaven"), "q": "", "asset_type": data.get("asset_type", "model"), "categories": [], "selected_categories": [], "search": None, "assets": saved}
            fields = ("provider", "q", "asset_type", "limit", "page", "categories")
            query = AssetSearchRequest.model_validate({key: data[key] for key in fields if key in data})
            result = source_search_assets(store, self.settings, query)
            if operation == "asset_browser":
                category_request = AssetCategoryRequest(provider=query.provider, asset_type=query.asset_type)
                category_result = source_list_asset_categories(store, self.settings, category_request)
                categories = category_result.get("categories", [])
                return {"project_id": data["project_id"], "provider": query.provider, "q": query.q, "asset_type": query.asset_type, "categories": categories, "selected_categories": query.categories, "search": result.model_dump(mode="json"), "assets": saved}
            return result.model_dump(mode="json")
        if operation == "acquire_asset":
            request = AssetAcquireRequest.model_validate({key: value for key, value in data.items() if key != "project_id"})
            asset = source_acquire_asset(self._store(data["project_id"]), self.settings, request)
            return asset.model_dump(mode="json")
        if operation == "inspect_h3":
            import json
            from pathlib import Path
            policy = self.settings.minimax_h3
            repository = ProjectRoot(Path(__file__).resolve().parents[2])
            base = "spikes/22_minimax_h3_workflows"
            runtime_rel = base + "/_work/local_runtime"
            catalog_rel = base + "/LOCAL_MODEL_CATALOG.json"
            required = [
                base + "/direct_comfy.py",
                base + "/local_h3.py",
                runtime_rel + "/env/Scripts/python.exe",
                runtime_rel + "/source/ComfyUI/main.py",
                runtime_rel + "/source/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-Easy/__init__.py",
            ]
            try:
                catalog = json.loads(repository.read_text(catalog_rel, encoding="utf-8"))
                model_root = catalog["model_root"]
                required.extend(
                    model_root + "/" + row["model_relative_path"]
                    for row in catalog["components"]
                )
                runtime_present = (
                    repository.resolve(runtime_rel).is_dir()
                    and all(repository.resolve(relative).is_file() for relative in required)
                )
            except (OSError, KeyError, TypeError, ValueError):
                runtime_present = False
            return {"enabled": policy.enabled, "configured_runtime_present": runtime_present,
                    "tested_defaults": {"duration_seconds": 8, "width": 384, "height": 288, "fps": 24, "steps": 20},
                    "limitations": ["video and prompt only", "experimental settings require explicit opt-in",
                                    "reservations are admission policy, not measured model minima"]}
        if operation == "list_h3_candidates":
            store = self._store(data["project_id"])
            shot = store.load(Shot, data["shot_id"], episode_id=data["episode_id"])
            return [item.model_dump(mode="json") | {
                "stale": item.shot_version != shot.version or shot.status is ShotStatus.STALE,
            } for item in store.list_h3_candidates(data["shot_id"], episode_id=data["episode_id"])]
        if operation == "promote_h3_candidate":
            from orchestrator.domain.entities import H3Candidate
            from orchestrator.service.minimax_h3 import H3PromoteBody, promote_h3_candidate
            store = self._store(data["project_id"])
            body = H3PromoteBody.model_validate({key: value for key, value in data.items()
                                                 if key in H3PromoteBody.model_fields})
            # The candidate is addressed by id, so the Episode and Shot in the path would
            # otherwise be decorative: without this, any shot's URL could promote any
            # candidate. `cancel_h3_candidate` binds its job the same way.
            try:
                candidate = store.load(H3Candidate, data["candidate_id"])
            except RecordNotFound as exc:
                raise H3RequestError(
                    f"H3 candidate {data['candidate_id']!r} does not exist"
                ) from exc
            if (candidate.shot_id != data["shot_id"]
                    or candidate.episode_id != data["episode_id"]):
                raise H3RequestError("H3 candidate is not bound to this Shot and Episode")
            output = promote_h3_candidate(
                store, candidate_id=data["candidate_id"], review_id=body.review_id,
                expected_version=body.expected_version,
            )
            return output.model_dump(mode="json")
        if operation == "cancel_h3_candidate":
            if self.job_runner is None:
                raise RuntimeError("default JobRunner was not configured")
            store = self._store(data["project_id"])
            body = H3CancelBody.model_validate({key: value for key, value in data.items()
                                                if key in H3CancelBody.model_fields})
            with store.command():
                shot, token = store.load_with_version(Shot, data["shot_id"], episode_id=data["episode_id"])
                if token != body.expected_version:
                    from orchestrator.store.json_store import VersionConflict
                    raise VersionConflict(f"episodes/{data['episode_id']}/shots/{data['shot_id']}",
                                          body.expected_version, token, shot)
                from orchestrator.jobs.journal import read_job
                record = read_job(store._root, data["job_id"])
                if (record is None or record.get("type") != AdapterKind.H3_CANDIDATE.value
                        or record.get("shot_id") != data["shot_id"]
                        or record.get("episode_id") != data["episode_id"]):
                    raise H3RequestError("job is not bound to this H3 candidate, Shot, and Episode")
                job = self.job_runner.cancel(data["project_id"], data["job_id"])
            return {"job_id": job.job_id, "state": job.state, "cancellation_requested": True}
        if operation == "generate_h3_candidate":
            if self.job_runner is None:
                raise RuntimeError("default JobRunner was not configured")
            effective_h3_settings(self.settings)
            if self._runtime is not None:
                adapter = self._runtime.adapter_for(
                    self._project_root(data["project_id"]), AdapterKind.H3_CANDIDATE,
                )
                if not adapter.inspect().get("runtime_present", False):
                    raise H3SetupError(
                        "MiniMax H3 runtime is not present; restore the pinned local runtime before submitting"
                    )
            body = H3GenerateBody.model_validate({key: value for key, value in data.items()
                                                  if key in H3GenerateBody.model_fields})
            store = self._store(data["project_id"])
            request_model = compose_h3_request(store, shot_id=data["shot_id"],
                episode_id=data["episode_id"], body=body, run_id=uuid.uuid4().hex)
            policy = self.settings.minimax_h3
            request = AdapterRequest(kind=AdapterKind.H3_CANDIDATE,
                payload={"request": request_model, "expected_version": body.expected_version},
                record_id=data["shot_id"], record_version=request_model.shot_version,
                episode_id=data["episode_id"], required_vram_gb=policy.required_vram_gb,
                required_ram_gb=policy.required_ram_gb, required_disk_gb=policy.required_disk_gb)
            job = self.job_runner.submit(data["project_id"], request)
            return {"job_id": job.job_id, "state": job.state, "run_id": request_model.run_id}
        if operation == "report_jobs":
            queue = self.job_queues.get(data["project_id"])
            return self._read_job_reports(data["project_id"], root=queue.root if queue else None)
        if operation in ASYNC_ADAPTER_OPERATIONS:
            if self.job_runner is None:
                raise RuntimeError("default JobRunner was not configured")
            kind = {
                "build_scene": AdapterKind.SCENE,
                "render_preview": AdapterKind.RENDER,
                "render_master": AdapterKind.RENDER,
                "generate_media": AdapterKind.GENERATE,
                "degrade_media": AdapterKind.DEGRADE,
            }[operation]
            record_id = data.get("shot_id")
            if not record_id:
                raise AdapterRequestInvalid(f"{operation} requires shot_id")
            store = self._store(data["project_id"])
            if operation in {"build_scene", "render_preview", "render_master"}:
                # Only build_scene composes its own scene; both renders consume the one
                # it persisted, so neither silently re-derives geometry of its own.
                payload = compose_render_request(
                    store,
                    shot_id=record_id,
                    episode_id=data.get("episode_id"),
                    run_id=uuid.uuid4().hex,
                    mode="preview" if operation == "render_preview" else "master",
                    require_built_scene=operation != "build_scene",
                )
                record_version = payload.shot_version
                episode_id = data.get("episode_id")
            else:
                shot = store.load(Shot, record_id, episode_id=data.get("episode_id"))
                payload = data.get("request", data)
                record_version = data.get("record_version")
                if record_version is None:
                    record_version = getattr(payload, "shot_version", None)
                if record_version is None and isinstance(payload, dict):
                    record_version = payload.get("shot_version")
                if record_version is None:
                    record_version = shot.version
                episode_id = shot.episode_id
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
                record_version=record_version, episode_id=episode_id,
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
    await item.seek(0)
    return UploadedFile(
        filename=item.filename or "", head=head, size_bytes=size, stream=item.file
    )


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


def _asset_browser_html(data: dict[str, Any]) -> str:
    """Render escaped search cards, local previews, categories and visible credits."""
    search = data.get("search")
    rows: list[str] = []
    project_id = quote(str(data.get("project_id", "")), safe="")
    if isinstance(search, dict):
        credit_url = search.get("credit_url", "")
        if _safe_asset_href(credit_url):
            rows.append(f'<p class="provider-credit"><a rel="noopener" href="{html.escape(credit_url, quote=True)}">{html.escape(str(search.get("credit", "")))}</a></p>')
        for item in search.get("results", []):
            provider = str(item.get("provider", data.get("provider", "polyhaven")))
            asset_id = str(item.get("source_asset_id", ""))
            title = html.escape(str(item.get("name", "")))
            source = item.get("source_url", "")
            if _safe_asset_href(source):
                title = f'<a rel="noopener" href="{html.escape(source, quote=True)}">{title}</a>'
            preview = ""
            if provider in {"polyhaven", "sketchfab"} and asset_id:
                local_url = (f'/projects/{project_id}/assets/preview?provider={quote(provider, safe="")}'
                             f'&amp;source_asset_id={quote(asset_id, safe="")}')
                preview = f'<img src="{local_url}" alt="Preview of {html.escape(str(item.get("name", "asset")), quote=True)}" loading="lazy"> '
            rows.append(f'<li>{preview}{title} <span class="license">{html.escape(str(item.get("license", "")))}</span> <span class="creator">{html.escape(str(item.get("attribution", "")))}</span></li>')
    for item in data.get("assets", []):
        item_provider = item.get("provider")
        provider_credit = ('<a class="provider-credit" href="https://polyhaven.com" rel="noopener">Assets from Poly Haven</a> '
                           if item_provider == "polyhaven" else "")
        title = html.escape(str(item.get("asset_id", "")))
        source = item.get("source_url")
        if _safe_asset_href(source):
            title = f'<a rel="noopener" href="{html.escape(source, quote=True)}">{title}</a>'
        license_name = str(item.get("license", ""))
        license_url = {"CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
                       "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/"}.get(license_name)
        shown_license = html.escape(license_name)
        if license_url and _safe_asset_href(license_url):
            shown_license = f'<a rel="noopener" href="{license_url}">{shown_license}</a>'
        rows.append(f'<li class="saved">{provider_credit}{title} <span class="license">{shown_license}</span> <span class="creator">{html.escape(str(item.get("attribution", "")))}</span></li>')
    provider = html.escape(str(data.get("provider", "polyhaven")), quote=True)
    selected = str(data.get("asset_type", "model"))
    types = "".join(f'<option value="{name}"{" selected" if selected == name else ""}>{label}</option>' for name, label in (("model", "Model"), ("texture", "Texture"), ("hdri", "HDRI")))
    provs = "".join(f'<option value="{name}"{" selected" if provider == name else ""}>{label}</option>' for name, label in (("polyhaven", "Poly Haven"), ("sketchfab", "Sketchfab")))
    category_controls = ""
    if data.get("q"):
        selected_categories = set(data.get("selected_categories", []))
        options = []
        for category in data.get("categories", []):
            slug = str(category.get("slug", ""))
            label = html.escape(str(category.get("name", slug)))
            checked = " checked" if slug in selected_categories else ""
            count = category.get("count")
            count_text = f' ({int(count)})' if isinstance(count, int) and not isinstance(count, bool) else ""
            options.append(f'<label><input type="checkbox" name="categories" value="{html.escape(slug, quote=True)}"{checked}>{label}{count_text}</label>')
        category_controls = '<fieldset><legend>Categories</legend>' + "".join(options) + "</fieldset>"
    return ('<!doctype html><html><head><meta charset=\"utf-8\"><title>Asset browser</title></head><body>'
            f'<form method="get"><label>Provider <select name="provider">{provs}</select></label><label>Search <input name="q" value="{html.escape(str(data.get("q", "")), quote=True)}"></label><label>Type <select name="asset_type">{types}</select></label>{category_controls}<button type="submit">Search</button></form><ul>{"".join(rows)}</ul></body></html>')


def _safe_asset_href(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and parsed.port in {None, 443} and parsed.hostname and parsed.hostname.casefold() in {
            "polyhaven.com", "www.polyhaven.com", "sketchfab.com", "www.sketchfab.com",
            "creativecommons.org", "www.creativecommons.org",
        } and not parsed.username and not parsed.password and not parsed.fragment
    except ValueError:
        return False


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
    configured.register_runtime_secret(token)
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
        if operation.name == "asset_browser":
            return HTMLResponse(content=_asset_browser_html(result), media_type="text/html")
        if operation.name == "asset_preview":
            try:
                image_bytes = base64.b64decode(result["data_base64"], validate=True)
            except (KeyError, TypeError, binascii.Error) as exc:
                raise RuntimeError("asset preview service returned invalid encoded image data") from exc
            media_type = result.get("media_type")
            if media_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise RuntimeError("asset preview service returned an unsupported media type")
            return Response(content=image_bytes, media_type=media_type,
                            headers={"X-Content-Type-Options": "nosniff"})
        if operation.name in ASYNC_ADAPTER_OPERATIONS:
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
        if operation.request_model is not None:
            for name, model_field in operation.request_model.model_fields.items():
                default = (
                    Parameter.empty if model_field.is_required()
                    else model_field.default_factory() if model_field.default_factory is not None
                    else model_field.default
                )
                params.append(Parameter(
                    name,
                    Parameter.POSITIONAL_OR_KEYWORD,
                    default=default,
                    annotation=Annotated[model_field.annotation, Form()],
                ))
    elif operation.name in {"search_assets", "asset_browser", "list_asset_categories", "asset_preview"}:
        for name, model_field in operation.request_model.model_fields.items():
            default = (
                Parameter.empty if model_field.is_required()
                else model_field.default_factory() if model_field.default_factory is not None
                else model_field.default
            )
            params.append(Parameter(name, Parameter.POSITIONAL_OR_KEYWORD, default=default,
                                    annotation=Annotated[model_field.annotation, Query()]))
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
    # A parameter without a default may not follow one that has a default. The
    # upload surface appends a defaulted `files` before the request model's own
    # fields, so a single required model field previously raised
    # "non-default argument follows default argument" and broke construction of
    # every route at once, not just this one. A stable partition keeps each
    # group's declared order while making the signature legal.
    params = (
        [p for p in params if p.default is Parameter.empty]
        + [p for p in params if p.default is not Parameter.empty]
    )
    endpoint.__signature__ = Signature(params, return_annotation=Any)  # type: ignore[attr-defined]
    endpoint.__name__ = operation.name
    endpoint.__doc__ = operation.summary
    app.add_api_route(operation.path, endpoint, methods=[operation.method],
                      name=operation.name, operation_id=operation.name,
                      summary=operation.summary, response_model=None, include_in_schema=True,
                      status_code=202 if operation.name in ASYNC_ADAPTER_OPERATIONS else 200,
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
        #: One canonical ProjectRoot per path. `MinimaxH3Adapter.generate_managed`
        #: compares `queue.root is self.root`, so an adapter and the queue that drives it
        #: must hold the SAME instance, not merely equal paths. Both this class and
        #: `OperationContext` previously minted a fresh ProjectRoot on every call while
        #: `adapter_for` cached by path string and stored whichever instance reached it
        #: first -- so the runtime-presence pre-check in `generate_h3_candidate` cached an
        #: adapter against the context's instance, `for_kind` then built the queue around
        #: a different one, and every H3 job failed its identity guard before generating.
        self._roots: dict[str, ProjectRoot] = {}
        self._nvml: Any = None
        self.registry = AdapterRegistry({kind: _RuntimeAdapter(self, kind) for kind in AdapterKind})

    def _canonical_root(self, path: Any) -> ProjectRoot:
        """The single ProjectRoot instance this runtime uses for `path`."""
        key = str(path)
        with self._lock:
            root = self._roots.get(key)
            if root is None:
                root = ProjectRoot(
                    path,
                    use_windows_share_delete_reads=self.settings.use_windows_share_delete_reads,
                )
                self._roots[key] = root
            return root

    def project_root(self, project_id: str) -> ProjectRoot:
        path = self.root.resolve(project_id)
        if not path.is_dir():
            raise RecordNotFound(f"project {project_id!r} does not exist")
        return self._canonical_root(path)

    def adapter_for(self, project_root: ProjectRoot, kind: AdapterKind) -> Any:
        # Canonicalised so a caller that built its own ProjectRoot for the same path --
        # `OperationContext._project_root` does -- still yields an adapter whose `root`
        # is the instance `for_kind` will put in the queue.
        project_root = self._canonical_root(project_root.path)
        key = (str(project_root.path), kind)
        with self._lock:
            if key not in self._adapters:
                if kind in {AdapterKind.SCENE, AdapterKind.RENDER}:
                    from orchestrator.adapters.blender.runner import BlenderAdapter
                    adapter = BlenderAdapter(project_root, self.settings)
                elif kind is AdapterKind.GENERATE:
                    from orchestrator.adapters.comfyui.runner import ComfyAdapter
                    adapter = ComfyAdapter(project_root, self.settings)
                elif kind is AdapterKind.H3_CANDIDATE:
                    from orchestrator.adapters.minimax_h3 import MinimaxH3Adapter
                    adapter = MinimaxH3Adapter(project_root, effective_h3_settings(self.settings))
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
            queue_settings = adapter.settings if kind is AdapterKind.H3_CANDIDATE else self.settings
            if self._nvml is None:
                self._nvml = PynvmlProbe()
            worker = getattr(adapter, "worker", _CpuWorker())
            repo_root = ProjectRoot(__import__("pathlib").Path(__file__).resolve().parents[2])
            lease = GpuLease(root, queue_settings, nvml=self._nvml, worker=worker,
                             execution_slot=GpuExecutionSlot(repo_root))
            queue = JobQueue(root, queue_settings, lease, ProjectStore(root), nvml=self._nvml,
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
