"""Register project-local assets and verify geometry assets before import."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from orchestrator.domain.entities import Asset
from orchestrator.platform.containment import (
    ProjectRoot,
    UploadLimits,
    UploadRejected,
    sanitize_filename,
    validate_upload,
)
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict


class AssetRegistrationInvalid(ValueError):
    """A local asset registration request lacks valid format or provenance."""


_ASSET_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_GLB_MAGIC = b"glTF"
_GLB_JSON_CHUNK = 0x4E4F534A


def register_asset(
    store: ProjectStore,
    source: Path | BinaryIO,
    *,
    filename: str,
    asset_id: str | None = None,
    asset_type: str = "model",
    generated: bool = False,
    source_url: str | None = None,
    provider: str | None = None,
    license: str | None = None,
    model_or_voice_license: str | None = None,
    attribution: str | None = None,
    attribution_surface_required: bool = False,
    reuse_scope: str = "project",
    expected_version: int = 0,
) -> Asset:
    """Copy one permitted upload into the project and append its provenance record.

    ``source`` is either a local path (CLI) or a seekable, already-spooled upload
    stream (HTTP). The asset bytes and JSON record are both written through the
    project's single containment helper.
    """
    if type(expected_version) is not int or expected_version != 0:
        raise AssetRegistrationInvalid("a new asset requires expected_version=0")
    if not asset_type.strip():
        raise AssetRegistrationInvalid("asset type must not be empty")
    if not reuse_scope.strip():
        raise AssetRegistrationInvalid("reuse_scope must not be empty")

    validate_asset_source(
        source,
        filename=filename,
        license=license,
        source_url=source_url,
        generated=generated,
    )
    clean_license = license.strip() if isinstance(license, str) else ""
    clean_source_url = source_url.strip() if isinstance(source_url, str) else ""
    selected_id = asset_id or uuid.uuid4().hex
    if not _ASSET_ID.fullmatch(selected_id):
        raise AssetRegistrationInvalid("asset_id must match [A-Za-z0-9_-]{1,64}")
    clean_filename = sanitize_filename(filename)
    relative_path = f"assets/models/{selected_id}/{clean_filename}"
    root = store._root

    with _open_source(source) as stream, store.command():
        try:
            current, token = store.load_with_version(Asset, selected_id)
        except RecordNotFound:
            pass
        else:
            raise VersionConflict(
                f"assets/registry/{selected_id}", expected_version, token, current
            )
        if root.resolve(relative_path).exists():
            raise AssetRegistrationInvalid(
                f"asset destination already exists without a registry record: {relative_path}"
            )
        stream.seek(0)
        root.write_stream(relative_path, stream)
        checksum = _sha256(root, relative_path)
        record = Asset(
            asset_id=selected_id,
            type=asset_type.strip(),
            generated=generated,
            source_url=clean_source_url or None,
            provider=provider.strip() if isinstance(provider, str) and provider.strip() else None,
            license=clean_license,
            model_or_voice_license=(
                model_or_voice_license.strip()
                if isinstance(model_or_voice_license, str) and model_or_voice_license.strip()
                else None
            ),
            file_path=relative_path,
            checksum=checksum,
            attribution=attribution.strip() if isinstance(attribution, str) and attribution.strip() else None,
            attribution_surface_required=attribution_surface_required,
            reuse_scope=reuse_scope.strip(),
        )
        store.save(record, expected_version=expected_version)
        return record


def validate_asset_source(
    source: Path | BinaryIO,
    *,
    filename: str,
    license: str | None,
    source_url: str | None,
    generated: bool,
) -> None:
    """Validate format and provenance before looking up or writing project records."""
    clean_filename = sanitize_filename(filename)
    with _open_source(source) as stream:
        _validate_format(stream, filename=clean_filename, size=_stream_size(stream))
    clean_license = license.strip() if isinstance(license, str) else ""
    clean_source_url = source_url.strip() if isinstance(source_url, str) else ""
    if not clean_license:
        raise AssetRegistrationInvalid("license is required to record the asset's reuse right")
    if not generated and not clean_source_url:
        raise AssetRegistrationInvalid(
            "source_url is required for externally sourced assets; set generated=true "
            "for a locally generated asset"
        )


def _validate_format(
    stream: BinaryIO, *, filename: str, size: int
) -> None:
    if size <= 0:
        raise UploadRejected(f"Asset {filename!r} is empty")

    stream.seek(0)
    head = stream.read(64)
    stream.seek(0)
    limits = UploadLimits(
        max_image_bytes=size,
        max_video_bytes=size,
        max_model_bytes=size,
    )
    upload = validate_upload(filename, head, size, limits)
    if upload.kind != "model":
        return

    try:
        header = stream.read(12)
        if len(header) != 12:
            raise ValueError("GLB header is truncated")
        magic, version, total_length = struct.unpack("<4sII", header)
        if magic != _GLB_MAGIC:
            raise ValueError("GLB magic bytes are missing")
        if version != 2:
            raise ValueError(f"GLB version {version} is unsupported; expected 2")
        if total_length != size:
            raise ValueError("GLB declared length does not match the file size")
        chunk_header = stream.read(8)
        if len(chunk_header) != 8:
            raise ValueError("GLB JSON chunk header is truncated")
        chunk_length, chunk_type = struct.unpack("<II", chunk_header)
        if chunk_type != _GLB_JSON_CHUNK or chunk_length > size - 20:
            raise ValueError("GLB JSON chunk is missing or malformed")
        raw_json = stream.read(chunk_length)
        if len(raw_json) != chunk_length:
            raise ValueError("GLB JSON chunk is truncated")
        document = json.loads(raw_json.decode("utf-8").rstrip("\x00 \t\r\n"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, struct.error, ValueError) as exc:
        raise UploadRejected(f"Asset {filename!r} is not a valid glTF 2.0 file: {exc}") from exc
    finally:
        stream.seek(0)

    if not isinstance(document, dict) or not isinstance(document.get("asset"), dict):
        raise UploadRejected(f"Asset {filename!r} has no glTF asset declaration")
    if document["asset"].get("version") != "2.0":
        raise UploadRejected(f"Asset {filename!r} must declare glTF version 2.0")
    for category in ("buffers", "images"):
        resources = document.get(category, [])
        if not isinstance(resources, list):
            raise UploadRejected(f"Asset {filename!r} has malformed {category}")
        for resource in resources:
            uri = resource.get("uri") if isinstance(resource, dict) else None
            if uri is not None and (not isinstance(uri, str) or not uri.lower().startswith("data:")):
                raise UploadRejected(
                    f"Asset {filename!r} references an external {category} URI; "
                    "register a self-contained glTF or GLB"
                )


@contextmanager
def _open_source(source: Path | BinaryIO) -> Iterator[BinaryIO]:
    if isinstance(source, Path):
        with source.open("rb") as stream:
            yield stream
        return
    if not callable(getattr(source, "seek", None)) or not callable(getattr(source, "read", None)):
        raise AssetRegistrationInvalid("asset source must be a readable, seekable binary file")
    try:
        source.seek(0)
    except (OSError, ValueError) as exc:
        raise AssetRegistrationInvalid("asset source must be seekable") from exc
    yield source


def _stream_size(stream: BinaryIO) -> int:
    try:
        original = stream.tell()
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(original)
    except (OSError, ValueError) as exc:
        raise AssetRegistrationInvalid("asset source size could not be measured") from exc
    return size


def _sha256(root: ProjectRoot, relative_path: str) -> str:
    digest = hashlib.sha256()
    path = root.resolve(relative_path)
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()
