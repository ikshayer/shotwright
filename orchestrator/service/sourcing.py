"""Project-bound provider search and immutable asset acquisition."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import uuid

from orchestrator.adapters.assets import gltf, providers
from orchestrator.adapters.assets.http import AssetHTTP
from orchestrator.adapters.assets.models import (
    AssetAcquireRequest,
    AssetCategoryRequest,
    AssetPreviewRequest,
    AssetPreviewResult,
    AssetSearchRequest,
    AssetSearchResult,
    ProviderConfigurationError,
)
from orchestrator.domain.entities import Asset, Project
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict

from .assets import AssetRegistrationInvalid, register_asset

GLTF_PACKER_VERSION = gltf.GLTF_PACKER_VERSION


def search_assets(
    store: ProjectStore,
    settings: Settings,
    request: AssetSearchRequest,
    *,
    http: AssetHTTP | None = None,
) -> AssetSearchResult:
    request = AssetSearchRequest.model_validate(request.model_dump() if isinstance(request, AssetSearchRequest) else request)
    _require_project(store)
    if request.page > settings.asset_providers.max_search_pages:
        raise ProviderConfigurationError(request.provider, "Requested page exceeds configured search bound")
    client = http or AssetHTTP(settings)
    if request.provider == "polyhaven":
        return providers.search_polyhaven(client, request.q, request.asset_type, request.limit, request.page, request.categories)
    return providers.search_sketchfab(client, request.q, request.asset_type, request.limit, request.page, settings, request.categories)


def list_asset_categories(
    store: ProjectStore,
    settings: Settings,
    request: AssetCategoryRequest,
    *,
    http: AssetHTTP | None = None,
) -> dict:
    request = AssetCategoryRequest.model_validate(request.model_dump() if isinstance(request, AssetCategoryRequest) else request)
    _require_project(store)
    client = http or AssetHTTP(settings)
    categories = (providers.list_polyhaven_categories(client, request.asset_type)
                  if request.provider == "polyhaven" else providers.list_sketchfab_categories(client))
    return {"provider": request.provider, "asset_type": request.asset_type, "categories": categories,
            "credit": "Assets from Poly Haven" if request.provider == "polyhaven" else "Models from Sketchfab",
            "credit_url": "https://polyhaven.com" if request.provider == "polyhaven" else "https://sketchfab.com"}


def preview_asset(
    store: ProjectStore,
    settings: Settings,
    request: AssetPreviewRequest,
    *,
    http: AssetHTTP | None = None,
) -> AssetPreviewResult:
    request = AssetPreviewRequest.model_validate(request.model_dump() if isinstance(request, AssetPreviewRequest) else request)
    _require_project(store)
    client = http or AssetHTTP(settings)
    if request.provider == "polyhaven":
        info = client.get_json(f"https://api.polyhaven.com/info/{request.source_asset_id}")
        if not isinstance(info, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven asset metadata schema is invalid")
        candidate = info.get("thumbnail_url")
        if candidate is None:
            raise JobError(JobErrorKind.PROVIDER_HTTP_4XX, "Poly Haven asset has no public thumbnail")
        url = providers._safe_thumbnail(candidate, {"cdn.polyhaven.com"})
        credit, credit_url = "Assets from Poly Haven", "https://polyhaven.com"
    else:
        info = client.get_json(f"https://api.sketchfab.com/v3/models/{request.source_asset_id}")
        if not isinstance(info, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Sketchfab model metadata schema is invalid")
        thumbnails = info.get("thumbnails")
        images = thumbnails.get("images") if isinstance(thumbnails, dict) else None
        candidates = [item.get("url") for item in images if isinstance(item, dict) and isinstance(item.get("url"), str) and item.get("url").strip()] if isinstance(images, list) else []
        if not candidates:
            raise JobError(JobErrorKind.PROVIDER_HTTP_4XX, "Sketchfab model has no public thumbnail")
        url = providers._sketchfab_thumbnail(thumbnails)
        credit, credit_url = "Models from Sketchfab", "https://sketchfab.com"
    if not url:
        raise JobError(JobErrorKind.INVALID_MANIFEST, "Provider supplied a thumbnail URL outside the public HTTPS allowlist")
    raw = client.get_bytes(url, max_bytes=2 * 1024 * 1024, authenticated=False)
    if not isinstance(raw, bytes) or len(raw) > 2 * 1024 * 1024:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider thumbnail exceeds the preview byte limit")
    try:
        from PIL import Image, UnidentifiedImageError
        with Image.open(io.BytesIO(raw)) as image:
            image_format = (image.format or "").upper()
            formats = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
            width, height = image.size
            if image_format not in formats or width < 1 or height < 1 or max(width, height) > 2048:
                raise ValueError("unsupported thumbnail format or dimensions")
            image.load()
    except ImportError:
        raise ProviderConfigurationError("image", "Pillow is required to decode provider thumbnails") from None
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider thumbnail failed image decoding or safety limits") from None
    return AssetPreviewResult(provider=request.provider, source_asset_id=request.source_asset_id,
        media_type=formats[image_format], width=width, height=height,
        data_base64=base64.b64encode(raw).decode("ascii"), credit=credit, credit_url=credit_url)


def acquire_asset(
    store: ProjectStore,
    settings: Settings,
    request: AssetAcquireRequest,
    *,
    http: AssetHTTP | None = None,
) -> Asset:
    request = AssetAcquireRequest.model_validate(request.model_dump() if isinstance(request, AssetAcquireRequest) else request)
    _require_project(store)
    if request.expected_version != 0:
        raise AssetRegistrationInvalid("new assets require expected_version=0")
    if request.asset_id is None:
        request = request.model_copy(update={"asset_id": uuid.uuid4().hex})
    _check_duplicate(store, request.asset_id)
    client = http or AssetHTTP(settings)
    if request.provider == "polyhaven":
        result = _acquire_polyhaven(store, settings, client, request)
    else:
        result = _acquire_sketchfab(store, settings, client, request)
    raw, filename, asset_type, source_url, license_name, attribution, needs_credit, provenance = result
    if asset_type == "model":
        try:
            gltf.validate_glb(raw, max_bytes=settings.upload.max_model_bytes)
        except gltf.GltfError:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider model is not a valid self-contained GLB") from None
    _check_space(store, len(raw), settings)
    root = store._root
    digest = hashlib.sha256(raw).hexdigest()
    cache_rel = f"assets/provider_cache/{digest}"
    if root.resolve(cache_rel).exists():
        cached = _bounded_contained_read(store, cache_rel, len(raw))
        if hashlib.sha256(cached).hexdigest() != digest or cached != raw:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Content-addressed provider cache failed integrity verification")
    else:
        _contained_write(store, cache_rel, raw)
    stage_id = uuid.uuid4().hex
    stage_rel = f"assets/provider_cache/staging/{stage_id}/{filename}"
    _contained_write(store, stage_rel, raw)
    if asset_type in {"texture", "hdri"}:
        _validate_image_dimensions(settings, root.resolve(stage_rel), filename)
    asset_relative = f"assets/models/{request.asset_id}/{filename}"
    manifest = {**provenance, "asset_id": request.asset_id,
                "conversion_version": gltf.GLTF_PACKER_VERSION, "normalized_size": len(raw),
                "normalized_asset_path": asset_relative, "normalized_sha256": digest}
    manifest_path = f"assets/provider_cache/manifests/{request.asset_id}-{uuid.uuid4().hex}.json"
    with store._lock:
        _check_duplicate(store, request.asset_id)
        _contained_write(store, manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        try:
            asset = register_asset(store, root.resolve(stage_rel), filename=filename, asset_id=request.asset_id,
                asset_type=asset_type, generated=False, source_url=source_url, provider=request.provider,
                license=license_name, attribution=attribution, attribution_surface_required=needs_credit,
                expected_version=request.expected_version)
        except OSError:
            raise JobError(JobErrorKind.DISK_EXHAUSTED, "Project disk failed while registering provider asset") from None
    return asset


def _acquire_polyhaven(store, settings, http, request):
    slug = request.source_asset_id
    info = http.get_json(f"https://api.polyhaven.com/info/{slug}")
    kind = {0: "hdri", 1: "texture", 2: "model"}.get(info.get("type"))
    if kind != request.asset_type:
        raise JobError(JobErrorKind.PROVIDER_HTTP_4XX, "Poly Haven asset type does not match request")
    files = http.get_json(f"https://api.polyhaven.com/files/{slug}")
    name = info.get("name") if isinstance(info.get("name"), str) else slug
    authors = providers._ph_authors(info.get("authors"))
    attribution = name + (f" - {authors}" if authors else "")
    source_url = f"https://polyhaven.com/a/{slug}"
    if kind == "model":
        entry = _pick(files.get("gltf", {}).get(request.resolution, {}).get("gltf"))
        _check_declared_space(store, entry, settings, settings.upload.max_model_bytes)
        extras = entry.get("include")
        if not isinstance(extras, dict) or not extras:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven model resource manifest is missing")
        aggregate = _declared_size(entry) + sum(_declared_size(value) for value in extras.values())
        if aggregate > settings.upload.max_model_bytes:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven model resources exceed aggregate size limit")
        _check_space(store, aggregate + settings.upload.max_model_bytes * 3, settings)
        raw_json = _download_checked(http, entry, settings.upload.max_model_bytes,
                                     store=store, cache_key=f"polyhaven/{slug}/gltf/{request.resolution}")
        resources = {}
        source_files = [_source_file_record(store, "gltf", raw_json, entry)]
        for rel, desc in sorted(extras.items()):
            if not isinstance(rel, str) or not isinstance(desc, dict):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven include manifest is invalid")
            gltf._safe_resource(rel)
            _check_declared_space(store, desc, settings, settings.upload.max_model_bytes)
            resources[rel] = _download_checked(http, desc, settings.upload.max_model_bytes,
                                                store=store, cache_key=f"polyhaven/{slug}/{request.resolution}/{rel}")
            source_files.append(_source_file_record(store, rel, resources[rel], desc))
        try:
            doc = json.loads(raw_json)
            raw = gltf.pack_gltf(doc, resources, max_bytes=settings.upload.max_model_bytes)
        except (json.JSONDecodeError, gltf.GltfError):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven glTF could not be normalized") from None
        provenance = _manifest_base("polyhaven", slug, name, attribution, source_url, "CC0-1.0", "https://creativecommons.org/publicdomain/zero/1.0/", request, creator=authors or "Unknown creator")
        provenance["source_files"] = source_files
        return raw, f"{slug}.glb", kind, source_url, "CC0-1.0", attribution, True, provenance
    group = files.get("hdri") if kind == "hdri" else files
    variants = group.get(request.resolution, {}) if kind == "hdri" and isinstance(group, dict) else {}
    if kind == "texture" and isinstance(group, dict):
        variants = group.get(request.map, {}).get(request.resolution, {})
    if kind == "texture":
        candidates = [request.format] if request.format != "auto" else ["jpg", "png"]
        selected = next(((key, variants.get(key)) for key in candidates if isinstance(variants.get(key), dict)), None)
    else:
        candidates = [request.format] if request.format != "auto" else ["hdr", "exr"]
        selected = next(((key, variants.get(key)) for key in candidates if isinstance(variants.get(key), dict)), None)
    if selected is None:
        raise JobError(JobErrorKind.PROVIDER_HTTP_4XX, f"Requested {kind} variant is unavailable at {request.resolution}")
    fmt, desc = selected
    _check_declared_space(store, desc, settings, settings.upload.max_image_bytes)
    raw = _download_checked(http, desc, settings.upload.max_image_bytes,
                            store=store, cache_key=f"polyhaven/{slug}/{kind}/{request.resolution}/{request.map}/{fmt}")
    provenance = _manifest_base("polyhaven", slug, name, attribution, source_url, "CC0-1.0", "https://creativecommons.org/publicdomain/zero/1.0/", request, creator=authors or "Unknown creator")
    provenance["source_files"] = [_source_file_record(store, f"{request.map}.{fmt}" if kind == "texture" else fmt, raw, desc)]
    return raw, f"{slug}.{fmt}", kind, source_url, "CC0-1.0", attribution, True, provenance


def _acquire_sketchfab(store, settings, http, request):
    license_catalog = providers.sketchfab_licenses(http, authenticated=True)
    item = http.get_json(f"https://api.sketchfab.com/v3/models/{request.source_asset_id}", authenticated=True)
    if item.get("isDownloadable") is not True:
        raise JobError(JobErrorKind.PROVIDER_HTTP_4XX, "Sketchfab model is not downloadable")
    rights = providers.classify_sketchfab_license(item.get("license"), license_catalog)
    if rights is None:
        raise JobError(JobErrorKind.UNRESOLVED_LICENSE, "Sketchfab model licence is not approved for acquisition")
    license_name, license_url, needs_credit = rights
    download = http.get_json(f"https://api.sketchfab.com/v3/models/{request.source_asset_id}/download", authenticated=True)
    direct = download.get("glb")
    if isinstance(direct, dict) and isinstance(direct.get("url"), str):
        _check_declared_space(store, direct, settings, settings.upload.max_model_bytes)
        raw = http.get_bytes(direct["url"], max_bytes=settings.upload.max_model_bytes)
        source_blob = raw
    else:
        packed = download.get("gltf")
        if not isinstance(packed, dict) or not isinstance(packed.get("url"), str):
            raise JobError(JobErrorKind.PROVIDER_HTTP_4XX, "Sketchfab provides no supported GLB or glTF download")
        _check_declared_space(store, packed, settings, settings.upload.max_model_bytes)
        archive = http.get_bytes(packed["url"], max_bytes=settings.upload.max_model_bytes)
        source_blob = archive
        try:
            doc, resources = gltf.unpack_gltf_zip(archive, max_files=settings.asset_providers.max_archive_files,
                max_expanded_bytes=settings.upload.max_model_bytes)
            raw = gltf.pack_gltf(doc, resources, max_bytes=settings.upload.max_model_bytes)
        except gltf.GltfError:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Sketchfab glTF archive could not be normalized") from None
    title = item.get("name") if isinstance(item.get("name"), str) else request.source_asset_id
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    creator = user.get("displayName") if isinstance(user.get("displayName"), str) else "Unknown creator"
    profile = providers._optional_http(user.get("profileUrl"), "sketchfab.com") or "https://sketchfab.com"
    source_url = f"https://sketchfab.com/3d-models/{request.source_asset_id}"
    attribution = f"{title} by {creator} ({profile}) - {license_name} {license_url}"
    provenance = _manifest_base("sketchfab", request.source_asset_id, title, attribution, source_url, license_name, license_url, request, creator=creator)
    provenance["source_files"] = [_source_file_record(store, "download", source_blob, direct if isinstance(direct, dict) else packed)]
    return raw, f"{request.source_asset_id}.glb", "model", source_url, license_name, attribution, needs_credit, provenance


def _download_checked(http, descriptor, cap, *, store=None, cache_key=None):
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("url"), str):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider download descriptor is invalid")
    size = descriptor.get("size")
    md5 = descriptor.get("md5")
    if type(size) is not int or size < 1 or size > cap:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider file exceeds configured size limit")
    if not isinstance(md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", md5):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven omitted a valid file checksum")
    index_rel = f"assets/provider_cache/index/{hashlib.sha256(cache_key.encode()).hexdigest()}.json" if store is not None and cache_key else None
    if index_rel and store._root.resolve(index_rel).exists():
        try:
            raw_index = _bounded_contained_read(store, index_rel, 4096)
            index = json.loads(raw_index.decode("utf-8"))
            if not isinstance(index, dict) or type(index.get("size")) is not int or not isinstance(index.get("md5"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(index.get("sha256", ""))):
                raise ValueError
            relative = index["relative_path"]
            digest = index["sha256"]
            if index.get("md5", "").lower() == md5.lower() and index.get("size") == size:
                if relative != f"assets/provider_cache/{digest}" or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError
                cached = _bounded_contained_read(store, relative, cap)
                if len(cached) != size or hashlib.sha256(cached).hexdigest() != digest or hashlib.md5(cached).hexdigest().lower() != md5.lower():
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven cached file failed checksum verification")
                return cached
        except JobError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven cache index is invalid") from None
    data = http.get_bytes(descriptor["url"], max_bytes=cap)
    if size != len(data):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider file size does not match descriptor")
    if hashlib.md5(data).hexdigest().lower() != md5.lower():
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider file checksum does not match descriptor")
    if index_rel:
        digest = hashlib.sha256(data).hexdigest()
        relative = f"assets/provider_cache/{digest}"
        root = store._root
        if root.resolve(relative).exists():
            cached = _bounded_contained_read(store, relative, cap)
            if cached != data or hashlib.sha256(cached).hexdigest() != digest:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven content cache failed integrity verification")
        else:
            _contained_write(store, relative, data)
        index = {"relative_path": relative, "sha256": digest, "size": size, "md5": md5.lower()}
        _contained_write(store, index_rel, json.dumps(index, sort_keys=True, separators=(",", ":")).encode())
    return data


def _pick(value):
    if not isinstance(value, dict) or not isinstance(value.get("url"), str):
        raise JobError(JobErrorKind.PROVIDER_HTTP_4XX, "Requested Poly Haven model variant is unavailable")
    return value


def _manifest_base(provider, source_id, title, attribution, source, license_name, license_url, req, **extra):
    return {"provider": provider, "id": source_id, "title": str(title)[:500], "creator": extra.get("creator", attribution),
            "source_page": source, "license": license_name, "license_url": license_url,
            "attribution": attribution, "resolution": req.resolution, "map": req.map, "format": req.format}


def _require_project(store):
    store.load(Project)


def _check_duplicate(store, asset_id):
    if asset_id is None:
        return
    try:
        current, version = store.load_with_version(Asset, asset_id)
    except RecordNotFound:
        return
    raise VersionConflict(f"assets/registry/{asset_id}", 0, version, current)


def _check_space(store, incoming, settings):
    try:
        free = shutil.disk_usage(store._root.path).free
    except OSError:
        raise JobError(JobErrorKind.DISK_EXHAUSTED, "Could not determine available project disk space") from None
    minimum = int(settings.budget.min_free_disk_gb * 1024**3)
    if free - incoming < minimum:
        raise JobError(JobErrorKind.DISK_EXHAUSTED, "Provider acquisition would cross configured minimum free disk space")
    ceiling = int(settings.budget.disk_ceiling_gb * 1024**3)
    current = 0
    for directory, _, names in os.walk(store._root.path):
        for name in names:
            try:
                current += os.stat(os.path.join(directory, name), follow_symlinks=False).st_size
            except OSError:
                raise JobError(JobErrorKind.DISK_EXHAUSTED, "Could not determine current project disk usage") from None
    if current + incoming > ceiling:
        raise JobError(JobErrorKind.DISK_EXHAUSTED, "Provider acquisition exceeds configured project disk ceiling")


def _check_declared_space(store, descriptor, settings, cap):
    declared = descriptor.get("size") if isinstance(descriptor, dict) else None
    if isinstance(declared, int) and declared > 0:
        _check_space(store, declared + cap * 3, settings)
    else:
        _check_space(store, cap * 4, settings)


def _declared_size(descriptor):
    if not isinstance(descriptor, dict):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider file descriptor is malformed")
    size = descriptor.get("size")
    if type(size) is not int or size < 0:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider omitted or invalidated declared file size")
    return size


def _bounded_contained_read(store, relative, cap):
    target = store._root.resolve(relative)
    with target.open("rb") as stream:
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Contained provider cache file exceeds its limit")
    return data


def _contained_write(store, relative, data):
    try:
        store._root.write_bytes(relative, data)
    except OSError as exc:
        if getattr(exc, "errno", None) == 28:
            raise JobError(JobErrorKind.DISK_EXHAUSTED, "Project disk is full while writing provider cache") from None
        raise JobError(JobErrorKind.DISK_EXHAUSTED, "Provider cache could not be written to project storage") from None


def _source_file_record(store, name, data, descriptor):
    digest = hashlib.sha256(data).hexdigest()
    relative = f"assets/provider_cache/{digest}"
    root = store._root
    if root.resolve(relative).exists():
        cached = _bounded_contained_read(store, relative, len(data))
        if hashlib.sha256(cached).hexdigest() != digest or cached != data:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider cache content failed integrity verification")
    else:
        _contained_write(store, relative, data)
    return {"name": name, "relative_path": relative, "size": len(data), "sha256": digest,
            "md5": descriptor.get("md5") if isinstance(descriptor, dict) and isinstance(descriptor.get("md5"), str) else None}


def _validate_image_dimensions(settings, path, filename):
    suffix = filename.rsplit(".", 1)[-1].lower()
    if suffix in {"jpg", "jpeg", "png", "webp"}:
        try:
            from PIL import Image, UnidentifiedImageError
            with Image.open(path) as image:
                width, height = image.size
                if width < 1 or height < 1 or max(width, height) > settings.upload.max_image_long_edge_px:
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider image exceeds configured pixel dimensions")
                image.verify()
        except JobError:
            raise
        except ImportError:
            raise ProviderConfigurationError("image", "Pillow is required to validate provider image dimensions") from None
        except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider image failed decoder validation") from None
        return
    ffmpeg = settings.binaries.ffmpeg_exe
    resolved = str(ffmpeg) if ffmpeg.is_file() else shutil.which(str(ffmpeg))
    if not resolved:
        raise ProviderConfigurationError("image", "Configured FFmpeg executable could not be resolved")
    probe = os.path.join(os.path.dirname(resolved), "ffprobe.exe" if os.name == "nt" else "ffprobe")
    if not os.path.isfile(probe):
        raise ProviderConfigurationError("image", "ffprobe beside configured FFmpeg is unavailable")
    try:
        result = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(path)], capture_output=True, timeout=30, check=False)
        if result.returncode != 0:
            raise ValueError
        stream = json.loads(result.stdout)["streams"][0]
        if max(int(stream["width"]), int(stream["height"])) > settings.upload.max_image_long_edge_px:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider image exceeds configured pixel dimensions")
    except JobError:
        raise
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError, json.JSONDecodeError):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Provider HDR/EXR image failed decoder validation") from None
