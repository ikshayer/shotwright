"""Bounded, deterministic glTF 2.0 packing and safe ZIP member reading."""
from __future__ import annotations

import base64
import binascii
import json
import re
import struct
import zipfile
import zlib
from pathlib import PurePosixPath
from urllib.parse import unquote


class GltfError(ValueError):
    pass


GLTF_PACKER_VERSION = "1.0.0"


def validate_glb(raw: bytes, *, max_bytes: int) -> None:
    """Validate GLB 2 framing and the self-contained buffers/images this pipeline accepts."""
    if type(raw) is not bytes or type(max_bytes) is not int or max_bytes <= 0:
        raise GltfError("GLB input or size limit is invalid")
    if len(raw) > max_bytes or len(raw) < 20:
        raise GltfError("GLB is empty, truncated, or exceeds configured size limit")
    try:
        magic, version, declared_total = struct.unpack_from("<4sII", raw, 0)
    except struct.error as exc:
        raise GltfError("GLB header is truncated") from exc
    if magic != b"glTF" or version != 2 or declared_total != len(raw):
        raise GltfError("GLB header magic, version, or declared length is invalid")
    offset = 12
    chunks: list[tuple[int, bytes]] = []
    while offset < len(raw):
        if len(raw) - offset < 8:
            raise GltfError("GLB chunk header is truncated")
        length, kind = struct.unpack_from("<II", raw, offset)
        offset += 8
        if length % 4 or length > len(raw) - offset:
            raise GltfError("GLB chunk length is invalid")
        if kind not in {0x4E4F534A, 0x004E4942}:
            raise GltfError("GLB contains an unsupported chunk type")
        chunks.append((kind, raw[offset:offset + length]))
        offset += length
    if not chunks or chunks[0][0] != 0x4E4F534A or sum(kind == 0x4E4F534A for kind, _ in chunks) != 1:
        raise GltfError("GLB must contain exactly one leading JSON chunk")
    if sum(kind == 0x004E4942 for kind, _ in chunks) > 1:
        raise GltfError("GLB contains multiple BIN chunks")
    json_bytes = chunks[0][1].rstrip(b" ")
    try:
        document = json.loads(
            json_bytes.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON number")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise GltfError("GLB JSON chunk is malformed") from exc
    if not isinstance(document, dict) or not isinstance(document.get("asset"), dict) or document["asset"].get("version") != "2.0":
        raise GltfError("GLB JSON must declare asset.version 2.0")
    bins = [data for kind, data in chunks if kind == 0x004E4942]
    buffers = document.get("buffers", [])
    if not isinstance(buffers, list) or len(buffers) > 1:
        raise GltfError("GLB must use at most one buffer")
    declared_buffer_length = 0
    if buffers:
        buffer = buffers[0]
        if not isinstance(buffer, dict) or "uri" in buffer:
            raise GltfError("GLB buffer must be self-contained and must not declare a URI")
        declared_buffer_length = buffer.get("byteLength")
        if type(declared_buffer_length) is not int or declared_buffer_length < 0 or len(bins) != 1:
            raise GltfError("GLB buffer declaration does not match its BIN chunk")
        if not declared_buffer_length <= len(bins[0]) <= declared_buffer_length + 3:
            raise GltfError("GLB BIN chunk length does not match the declared buffer length")
        if any(bins[0][declared_buffer_length:]):
            raise GltfError("GLB BIN padding must contain zero bytes")
    elif bins:
        raise GltfError("GLB BIN chunk has no declared buffer")
    views = document.get("bufferViews", [])
    if not isinstance(views, list):
        raise GltfError("GLB bufferViews must be an array")
    if views and not buffers:
        raise GltfError("GLB bufferViews require a declared BIN buffer")
    for view in views:
        if not isinstance(view, dict):
            raise GltfError("GLB bufferView entry must be an object")
        index = view.get("buffer", 0)
        start = view.get("byteOffset", 0)
        length = view.get("byteLength")
        if type(index) is not int or index != 0 or type(start) is not int or type(length) is not int or start < 0 or length < 0 or start + length > declared_buffer_length:
            raise GltfError("GLB bufferView references an invalid buffer range")
    images = document.get("images", [])
    if not isinstance(images, list):
        raise GltfError("GLB images must be an array")
    for image in images:
        if not isinstance(image, dict) or "uri" in image:
            raise GltfError("GLB images must be embedded without URI references")
        index = image.get("bufferView")
        mime = image.get("mimeType")
        if type(index) is not int or not 0 <= index < len(views) or not isinstance(mime, str) or mime not in {"image/jpeg", "image/png", "image/ktx2", "image/webp"}:
            raise GltfError("GLB image must reference a bufferView and supported mimeType")
        view = views[index]
        start = view.get("byteOffset", 0)
        end = start + view["byteLength"]
        if not bins or end > declared_buffer_length or not _image_matches_mime(bins[0][start:min(end, start + 12)], mime):
            raise GltfError("GLB image bufferView range or MIME type is invalid")


def pack_gltf(document: dict, resources: dict[str, bytes], *, max_bytes: int) -> bytes:
    """Embed every buffer and image, then serialize a self-contained GLB 2.0."""
    if not isinstance(document, dict) or not isinstance(resources, dict) or not isinstance(document.get("asset"), dict) or document["asset"].get("version") != "2.0":
        raise GltfError("glTF document must declare asset.version 2.0")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    safe_resources: dict[str, bytes] = {}
    folded_resources: set[str] = set()
    for name, data in resources.items():
        key = _safe_resource(name)
        if key.casefold() in folded_resources or type(data) is not bytes:
            raise GltfError("duplicate or invalid resource")
        folded_resources.add(key.casefold())
        safe_resources[key] = data
    if sum(map(len, safe_resources.values())) > max_bytes:
        raise GltfError("source resources exceed configured size limit")
    doc = _clone_json_bounded(document, max_bytes)
    for category in ("buffers", "bufferViews", "images"):
        if category in doc and not isinstance(doc[category], list):
            raise GltfError(f"{category} must be an array")
    extensions_required = doc.get("extensionsRequired", [])
    if not isinstance(extensions_required, list) or any(not isinstance(item, str) for item in extensions_required):
        raise GltfError("extensionsRequired must be an array of names")
    if any(
        isinstance(view, dict)
        and isinstance(view.get("extensions"), dict)
        and "EXT_meshopt_compression" in view["extensions"]
        for view in doc.get("bufferViews", [])
    ) or "EXT_meshopt_compression" in extensions_required:
        raise GltfError("EXT_meshopt_compression cannot be safely repacked")
    for view in doc.get("bufferViews", []):
        if not isinstance(view, dict):
            raise GltfError("malformed bufferView")
        extensions = view.get("extensions", {})
        if not isinstance(extensions, dict) or any(not isinstance(name, str) or not isinstance(value, dict) for name, value in extensions.items()):
            raise GltfError("bufferView extensions must be an object")
        if "target" in view and (type(view["target"]) is not int or view["target"] not in {34962, 34963}):
            raise GltfError("bufferView target is invalid")
    bin_payload = bytearray()
    buffers = doc.setdefault("buffers", [])
    if not isinstance(buffers, list):
        raise GltfError("buffers must be an array")
    offsets: list[int] = []
    original_lengths: list[int] = []
    for buffer in buffers:
        if not isinstance(buffer, dict):
            raise GltfError("malformed buffer")
        data = _resource_bytes(buffer.get("uri"), safe_resources, max_bytes - len(bin_payload))
        byte_length = buffer.get("byteLength")
        if type(byte_length) is not int or byte_length < 0 or len(data) != byte_length:
            raise GltfError("buffer byteLength does not match resource")
        padding = (-len(bin_payload)) % 4
        if len(bin_payload) + padding + len(data) > max_bytes:
            raise GltfError("packed binary payload exceeds configured size limit")
        _align(bin_payload)
        offsets.append(len(bin_payload))
        original_lengths.append(len(data))
        bin_payload.extend(data)
        buffer.pop("uri", None)
    views = doc.setdefault("bufferViews", [])
    if not isinstance(views, list):
        raise GltfError("bufferViews must be an array")
    for view in views:
        if not isinstance(view, dict):
            raise GltfError("malformed bufferView")
        index = view.get("buffer", 0)
        if type(index) is not int or index < 0 or index >= len(buffers):
            raise GltfError("bufferView references a missing buffer")
        offset = view.get("byteOffset", 0)
        length = view.get("byteLength")
        if type(offset) is not int or type(length) is not int or offset < 0 or length < 0 or offset + length > original_lengths[index]:
            raise GltfError("bufferView exceeds its source buffer")
        if "byteStride" in view and (type(view["byteStride"]) is not int or not 4 <= view["byteStride"] <= 252 or view["byteStride"] % 4):
            raise GltfError("bufferView has invalid byteStride")
        view["buffer"] = 0
        view["byteOffset"] = offset + offsets[index]
    for image in doc.get("images", []):
        if not isinstance(image, dict):
            raise GltfError("malformed image")
        image_view = image.get("bufferView")
        if image_view is not None:
            if type(image_view) is not int or image_view < 0 or image_view >= len(views):
                raise GltfError("image references a missing bufferView")
            if image.get("uri") is not None:
                raise GltfError("image cannot declare both uri and bufferView")
            mime = image.get("mimeType")
            if not isinstance(mime, str) or mime not in {"image/jpeg", "image/png", "image/ktx2", "image/webp"}:
                raise GltfError("image bufferView has unsupported MIME type")
            view = views[image_view]
            buffer_index = view.get("buffer", 0)
            start = view.get("byteOffset", 0)
            end = start + view["byteLength"]
            if buffer_index != 0 or start < 0 or end > len(bin_payload):
                raise GltfError("image bufferView range is outside the packed binary payload")
            signature = bytes(bin_payload[start:min(end, start + 12)])
            if not _image_matches_mime(signature, mime):
                raise GltfError("image bufferView bytes do not match mimeType")
            continue
        data = _resource_bytes(image.get("uri"), safe_resources, max_bytes - len(bin_payload))
        mime = image.get("mimeType") or _mime_from_uri(str(image.get("uri", "")))
        if str(image.get("uri", "")).startswith("data:"):
            data_mime = _data_uri_mime(str(image["uri"]))
            if image.get("mimeType") and data_mime != mime:
                raise GltfError("data URI MIME type disagrees with image mimeType")
            mime = data_mime
        if not isinstance(mime, str) or mime not in {"image/jpeg", "image/png", "image/ktx2", "image/webp"}:
            raise GltfError("unsupported embedded image MIME type")
        if not _image_matches_mime(data, mime):
            raise GltfError("embedded image bytes do not match mimeType")
        padding = (-len(bin_payload)) % 4
        if len(bin_payload) + padding + len(data) > max_bytes:
            raise GltfError("packed binary payload exceeds configured size limit")
        _align(bin_payload)
        offset = len(bin_payload)
        bin_payload.extend(data)
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data)})
        image.pop("uri", None)
        image["bufferView"] = len(views) - 1
        image["mimeType"] = mime
    if buffers:
        # glTF byteLength excludes up to three BIN chunk padding bytes.
        doc["buffers"] = [{"byteLength": len(bin_payload)}]
    elif bin_payload:
        doc["buffers"] = [{"byteLength": len(bin_payload)}]
    if bin_payload and not doc.get("buffers"):
        raise GltfError("binary payload has no buffer declaration")
    _align(bin_payload)
    json_chunk = _encode_json_bounded(doc, max_bytes)
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    total = 12 + 8 + len(json_chunk) + (8 + len(bin_payload) if bin_payload else 0)
    if total > max_bytes:
        raise GltfError("packed GLB exceeds configured size limit")
    out = bytearray(struct.pack("<4sII", b"glTF", 2, total))
    out.extend(struct.pack("<II", len(json_chunk), 0x4E4F534A)); out.extend(json_chunk)
    if bin_payload:
        out.extend(struct.pack("<II", len(bin_payload), 0x004E4942)); out.extend(bin_payload)
    return bytes(out)


def unpack_gltf_zip(raw: bytes, *, max_files: int, max_expanded_bytes: int) -> tuple[dict, dict[str, bytes]]:
    """Read a glTF ZIP in memory with path, type, count and expansion limits."""
    if type(raw) is not bytes or type(max_files) is not int or max_files < 1 or type(max_expanded_bytes) is not int or max_expanded_bytes < 1:
        raise GltfError("ZIP input and limits are invalid")
    try:
        archive = zipfile.ZipFile(__import__("io").BytesIO(raw))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, struct.error, ValueError) as exc:
        raise GltfError("download is not a valid ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > max_files:
            raise GltfError("ZIP member count exceeds configured limit")
        names: dict[str, tuple[str, zipfile.ZipInfo]] = {}
        seen_paths: set[str] = set()
        expanded = 0
        for info in infos:
            raw_name = info.filename[:-1] if info.is_dir() and info.filename.endswith("/") else info.filename
            name = _safe_resource(raw_name)
            folded = name.casefold()
            mode = (info.external_attr >> 16) & 0o170000
            if (folded in seen_paths or info.flag_bits & 1 or mode == 0o120000
                    or (mode not in {0, 0o100000, 0o040000} )):
                raise GltfError("ZIP contains duplicate, encrypted or symbolic-link entries")
            seen_paths.add(folded)
            if info.is_dir():
                if mode not in {0, 0o040000}:
                    raise GltfError("ZIP directory has an invalid entry type")
                if info.file_size != 0 or info.compress_size != 0:
                    raise GltfError("ZIP directory entry must not contain data")
                continue
            if mode not in {0, 0o100000}:
                raise GltfError("ZIP contains a non-regular file")
            if PurePosixPath(name).suffix.lower() not in {".gltf", ".glb", ".bin", ".png", ".jpg", ".jpeg", ".webp"}:
                raise GltfError("ZIP contains an unsupported file type")
            expanded += info.file_size
            if expanded > max_expanded_bytes:
                raise GltfError("ZIP expanded size exceeds configured limit")
            names[folded] = (name, info)
        mains = [name for name, _info in names.values() if name.endswith(".gltf")]
        if not mains:
            raise GltfError("ZIP has no glTF scene")
        member_data: dict[str, bytes] = {}
        actual_expanded = 0
        for folded_name in sorted(names):
            info = names[folded_name][1]
            try:
                content = _read_zip_member(archive, info, max_expanded_bytes - actual_expanded)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError, zlib.error, struct.error):
                raise GltfError("ZIP member is corrupt or uses an unsupported compression method") from None
            actual_expanded += len(content)
            if actual_expanded > max_expanded_bytes:
                raise GltfError("ZIP actual expanded size exceeds configured limit")
            member_data[folded_name] = content
        scene_mains = [name for name in mains if PurePosixPath(name).name.casefold() == "scene.gltf"]
        if len(scene_mains) > 1:
            raise GltfError("ZIP contains more than one scene.gltf")
        main = scene_mains[0] if scene_mains else None
        if main is None:
            if len(mains) != 1:
                raise GltfError("ZIP must contain scene.gltf or exactly one glTF document")
            main = mains[0]
        resource_dir = str(PurePosixPath(main).parent)
        resource_dir = "" if resource_dir == "." else resource_dir + "/"
        doc_raw = member_data[main.casefold()]
        try:
            document = json.loads(doc_raw.decode("utf-8"), parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise GltfError("glTF JSON is malformed") from exc
        if not isinstance(document, dict):
            raise GltfError("glTF root must be an object")
        for category in ("buffers", "images"):
            if category in document and not isinstance(document[category], list):
                raise GltfError(f"glTF {category} must be an array")
        resources: dict[str, bytes] = {}
        refs = []
        for category in ("buffers", "images"):
            for item in document.get(category, []):
                if not isinstance(item, dict):
                    raise GltfError(f"glTF {category} entries must be objects")
                uri = item.get("uri")
                if uri:
                    if not isinstance(uri, str):
                        raise GltfError("glTF resource URI must be a string")
                    if uri.startswith("data:"):
                        continue
                    key = _safe_resource(resource_dir + uri)
                    if key.casefold() not in names:
                        raise GltfError("glTF references a missing resource")
                    actual_key = names[key.casefold()][0]
                    refs.append(actual_key)
                    item["uri"] = actual_key
        for key in sorted(set(refs)):
            resources[key] = member_data[key.casefold()]
        return document, resources


def _safe_resource(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise GltfError("unsafe resource path")
    try:
        decoded = unquote(name, errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise GltfError("unsafe encoded resource path") from exc
    if re.search(r"%[0-9a-f]{2}", decoded, re.IGNORECASE) or "\\" in decoded:
        raise GltfError("unsafe encoded resource path")
    name = decoded
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", name) or "?" in name or "#" in name:
        raise GltfError("unsafe resource path")
    return path.as_posix()


def _resource_bytes(uri: object, resources: dict[str, bytes], max_bytes: int) -> bytes:
    if not isinstance(uri, str):
        raise GltfError("glTF resource URI is missing")
    if type(max_bytes) is not int or max_bytes < 0:
        raise GltfError("glTF resource exceeds configured size limit")
    if uri.startswith("data:"):
        if len(uri) > ((max_bytes + 2) // 3) * 4 + 128:
            raise GltfError("glTF data URI exceeds configured size limit")
        try:
            header, payload = uri.split(",", 1)
            if not header.startswith("data:") or not header.endswith(";base64"):
                raise ValueError
            if len(payload) > ((max_bytes + 2) // 3) * 4:
                raise GltfError("glTF data URI exceeds configured size limit")
            return base64.b64decode(payload, validate=True)
        except GltfError:
            raise
        except (ValueError, binascii.Error) as exc:
            raise GltfError("invalid glTF base64 data URI") from exc
    key = _safe_resource(uri)
    try:
        data = resources[key]
        if len(data) > max_bytes:
            raise GltfError("glTF resource exceeds configured size limit")
        return data
    except KeyError as exc:
        raise GltfError("glTF references a missing or external resource") from exc


def _data_uri_mime(uri: str) -> str:
    try:
        header, _payload = uri.split(",", 1)
        if not header.startswith("data:") or not header.endswith(";base64"):
            raise ValueError
        mime = header[5:-7]
        if not mime or "/" not in mime or any(char in mime for char in " ;\r\n\t"):
            raise ValueError
        return mime.lower()
    except ValueError as exc:
        raise GltfError("invalid glTF base64 data URI header") from exc


def _image_matches_mime(data: bytes, mime: str) -> bool:
    return (
        (mime == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
        or (mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (mime == "image/webp" and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or (mime == "image/ktx2" and data.startswith(b"\xabKTX 20\xbb\r\n\x1a\n"))
    )


def _clone_json_bounded(document: dict, max_bytes: int) -> dict:
    """Make a JSON-compatible copy without materializing unbounded JSON text."""
    pieces: list[bytes] = []
    total = 0
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    try:
        for piece in encoder.iterencode(document):
            step = max(1, min(64 * 1024, max_bytes // 4))
            for start in range(0, len(piece), step):
                encoded = piece[start:start + step].encode("utf-8")
                total += len(encoded)
                if total > max_bytes:
                    raise GltfError("glTF JSON exceeds configured size limit")
                pieces.append(encoded)
        cloned = json.loads(b"".join(pieces))
    except GltfError:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise GltfError("glTF document contains malformed JSON values") from exc
    if not isinstance(cloned, dict):
        raise GltfError("glTF root must be an object")
    return cloned


def _encode_json_bounded(document: dict, max_bytes: int) -> bytes:
    """Encode a JSON object in bounded chunks for the final GLB JSON chunk."""
    chunks: list[bytes] = []
    total = 0
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    try:
        for piece in encoder.iterencode(document):
            step = max(1, min(64 * 1024, max_bytes // 4))
            for start in range(0, len(piece), step):
                encoded = piece[start:start + step].encode("utf-8")
                total += len(encoded)
                if total > max_bytes:
                    raise GltfError("packed glTF JSON exceeds configured size limit")
                chunks.append(encoded)
    except GltfError:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise GltfError("packed glTF JSON contains malformed values") from exc
    return b"".join(chunks)


def _align(data: bytearray) -> None:
    data.extend(b"\x00" * ((-len(data)) % 4))


def _mime_from_uri(uri: str) -> str:
    ext = uri.split("?", 1)[0].lower().rsplit(".", 1)[-1]
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "ktx2": "image/ktx2", "webp": "image/webp"}.get(ext, "")


def _read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    chunks = bytearray()
    with archive.open(info, "r") as stream:
        while chunk := stream.read(min(1024 * 1024, limit + 1 - len(chunks))):
            chunks.extend(chunk)
            if len(chunks) > limit:
                raise GltfError("ZIP member exceeds configured limit")
    if len(chunks) != info.file_size:
        raise GltfError("ZIP member is truncated")
    return bytes(chunks)
