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

from orchestrator.domain.entities import Asset, VoiceConsent
from orchestrator.platform.containment import (
    MediaKind,
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
_ASSET_TYPES_BY_MEDIA_KIND: dict[MediaKind, frozenset[str]] = {
    "model": frozenset({"model"}),
    "image": frozenset({"texture", "hdri", "reference", "image"}),
    "video": frozenset({"reference", "video"}),
    "audio": frozenset({"voice", "audio"}),
}
#: Where each media kind is stored. Only audio has its own directory; the rest predate
#: this table and keep their original location.
_STORAGE_DIRECTORY: dict[MediaKind, str] = {"audio": "assets/audio"}
_DEFAULT_STORAGE_DIRECTORY = "assets/models"


def register_asset(
    store: ProjectStore,
    source: Path | BinaryIO,
    *,
    filename: str,
    asset_id: str | None = None,
    asset_type: str | None = None,
    generated: bool = False,
    source_url: str | None = None,
    provider: str | None = None,
    license: str | None = None,
    model_or_voice_license: str | None = None,
    attribution: str | None = None,
    attribution_surface_required: bool = False,
    reuse_scope: str = "project",
    expected_version: int = 0,
    voice_consent: VoiceConsent | None = None,
    voice_consent_basis: str | None = None,
) -> Asset:
    """Copy one permitted upload into the project and append its provenance record.

    ``source`` is either a local path (CLI) or a seekable, already-spooled upload
    stream (HTTP). The asset bytes and JSON record are both written through the
    project's single containment helper.
    """
    if type(expected_version) is not int or expected_version != 0:
        raise AssetRegistrationInvalid("a new asset requires expected_version=0")
    if asset_type is not None and not asset_type.strip():
        raise AssetRegistrationInvalid("asset type must not be empty")
    if not reuse_scope.strip():
        raise AssetRegistrationInvalid("reuse_scope must not be empty")

    media_kind = validate_asset_source(
        source,
        filename=filename,
        license=license,
        source_url=source_url,
        generated=generated,
        voice_consent=voice_consent,
    )
    # An omitted type is derived from the bytes rather than guessed: every media
    # kind's own name is a member of its permitted set, so derivation never
    # produces a type the check below would reject.
    declared_type = asset_type.strip() if asset_type is not None else media_kind
    if declared_type not in _ASSET_TYPES_BY_MEDIA_KIND[media_kind]:
        raise AssetRegistrationInvalid(
            f"declared asset type {declared_type!r} is incompatible with detected media kind "
            f"{media_kind!r}"
        )
    clean_basis = _validate_voice_consent(
        declared_type, voice_consent, voice_consent_basis
    )
    clean_license = license.strip() if isinstance(license, str) else ""
    clean_source_url = source_url.strip() if isinstance(source_url, str) else ""
    selected_id = asset_id or uuid.uuid4().hex
    if not _ASSET_ID.fullmatch(selected_id):
        raise AssetRegistrationInvalid("asset_id must match [A-Za-z0-9_-]{1,64}")
    clean_filename = sanitize_filename(filename)
    storage = _STORAGE_DIRECTORY.get(media_kind, _DEFAULT_STORAGE_DIRECTORY)
    relative_path = f"{storage}/{selected_id}/{clean_filename}"
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
            type=declared_type,
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
            voice_consent=voice_consent,
            voice_consent_basis=clean_basis,
        )
        store.save(record, expected_version=expected_version)
        return record


def _validate_voice_consent(
    declared_type: str, voice_consent: VoiceConsent | None, basis: str | None
) -> str | None:
    """FR-16: a voice needs recorded consent, kept apart from its licence.

    Returns the stripped basis. Checked before any byte is written so a refusal leaves
    the project untouched.
    """
    clean_basis = basis.strip() if isinstance(basis, str) and basis.strip() else None
    if declared_type == "voice" and voice_consent is None:
        raise AssetRegistrationInvalid(
            "a voice asset requires voice_consent ('synthetic', 'creator_own_voice', "
            "'licensed_for_cloning' or 'licensed_recording'); a licence alone does not record "
            "consent (FR-16)"
        )
    if voice_consent is None:
        if clean_basis is not None:
            raise AssetRegistrationInvalid("voice_consent_basis requires voice_consent")
        return None
    if declared_type not in {"voice", "audio"}:
        raise AssetRegistrationInvalid("voice_consent applies only to voice or audio assets")
    if voice_consent != "synthetic" and clean_basis is None:
        raise AssetRegistrationInvalid(
            f"voice_consent {voice_consent!r} requires voice_consent_basis: who consented and "
            "how, or the licence that permits cloning"
        )
    return clean_basis


def validate_asset_source(
    source: Path | BinaryIO,
    *,
    filename: str,
    license: str | None,
    source_url: str | None,
    generated: bool,
    voice_consent: VoiceConsent | None = None,
) -> MediaKind:
    """Validate format and provenance before looking up or writing project records.

    A creator's own voice recording has no source URL; its ``voice_consent_basis``
    carries the provenance instead, so the URL is not required for it.
    """
    clean_filename = sanitize_filename(filename)
    with _open_source(source) as stream:
        media_kind = _validate_format(stream, filename=clean_filename, size=_stream_size(stream))
    clean_license = license.strip() if isinstance(license, str) else ""
    clean_source_url = source_url.strip() if isinstance(source_url, str) else ""
    if not clean_license:
        raise AssetRegistrationInvalid("license is required to record the asset's reuse right")
    if not generated and not clean_source_url and voice_consent != "creator_own_voice":
        raise AssetRegistrationInvalid(
            "source_url is required for externally sourced assets; set generated=true "
            "for a locally generated asset"
        )
    return media_kind


def _validate_format(
    stream: BinaryIO, *, filename: str, size: int
) -> MediaKind:
    if size <= 0:
        raise UploadRejected(f"Asset {filename!r} is empty")

    stream.seek(0)
    head = stream.read(64)
    stream.seek(0)
    limits = UploadLimits(
        max_image_bytes=size,
        max_video_bytes=size,
        max_model_bytes=size,
        max_audio_bytes=size,
    )
    upload = validate_upload(filename, head, size, limits)
    if upload.kind != "model":
        return upload.kind

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
    return upload.kind


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
