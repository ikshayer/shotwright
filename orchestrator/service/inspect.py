"""Read one verified project output as a bounded PNG frame."""

from __future__ import annotations

import hashlib
import io
import math
import subprocess
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from orchestrator.domain.entities import ShotOutput, ShotOutputKind
from orchestrator.platform.containment import ContainmentError
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.store.json_store import ProjectStore


class FrameUnavailable(Exception):
    """The selected output has no frame at the requested position."""


_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"})
#: The demuxer named for each accepted video extension. None of these follows
#: external references: matroska and avi have none, and the mov demuxer ignores data
#: references unless `enable_drefs` is set, which is never passed.
_VIDEO_DEMUXERS = {
    ".mp4": "mov", ".m4v": "mov", ".mov": "mov",
    ".mkv": "matroska", ".webm": "matroska", ".avi": "avi",
}
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_LONG_EDGE = 1024
#: Refuse to decode an image larger than this from its header alone, before any
#: pixel data is decoded (Pillow's own bomb limit is higher, ~89 MP).
_MAX_SOURCE_PIXELS = 50_000_000
#: No output in this pipeline runs a day; anything later is past the end.
_MAX_AT_SEC = 86_400.0
_FFMPEG_TIMEOUT_SECONDS = 60


def inspect_output(
    store: ProjectStore,
    *,
    episode_id: str,
    output_id: str,
    ffmpeg_exe: str | Path,
    at_sec: float = 0.0,
) -> bytes:
    """Find and verify an output receipt, returning a bounded PNG frame."""
    valid_at_sec = False
    if not isinstance(at_sec, bool) and isinstance(at_sec, (int, float)):
        try:
            valid_at_sec = math.isfinite(at_sec) and at_sec >= 0
        except OverflowError:
            valid_at_sec = False
    if not valid_at_sec:
        raise ValueError("at_sec must be finite and greater than or equal to zero")

    # Only the selected receipt is read and verified, so corruption elsewhere in the
    # episode cannot block this inspection, and an unknown id is a plain 404.
    try:
        output = store.find_output(episode_id, output_id)
    except JobError as exc:
        if exc.kind is JobErrorKind.INVALID_MANIFEST and (
            "artifact hash mismatch" in exc.message
            or "artifact is missing or not a file" in exc.message
        ):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, exc.message, cause=exc) from exc
        raise

    relative_path = output.relative_path
    suffix = Path(relative_path).suffix.lower()
    if suffix == ".blend" or (isinstance(output, ShotOutput) and output.kind is ShotOutputKind.SCENE):
        raise FrameUnavailable("scene outputs (.blend) have no frame; call render_preview and inspect the preview")

    is_required_image = isinstance(output, ShotOutput) and output.kind in {
        ShotOutputKind.PREVIEW,
        ShotOutputKind.STILL,
    }
    is_generated_image_path = (
        isinstance(output, ShotOutput)
        and output.kind is ShotOutputKind.GENERATED
        and suffix in _IMAGE_SUFFIXES
    )
    if is_required_image or is_generated_image_path:
        return _read_verified_image_png(store, output)

    if suffix not in _VIDEO_DEMUXERS:
        # FFmpeg is never left to guess a format: probing can pick a demuxer that
        # follows references (playlists, concat lists) to files outside the project.
        raise FrameUnavailable(
            f"output {output_id!r} has extension {suffix or '(none)'!r}, which is not an "
            f"inspectable image or video ({', '.join(sorted(_IMAGE_SUFFIXES | set(_VIDEO_DEMUXERS)))})"
        )
    # find_output has already verified this artifact's SHA-256.
    return _extract_video_frame(
        store,
        relative_path=output.relative_path,
        output_id=output_id,
        ffmpeg_exe=ffmpeg_exe,
        at_sec=float(at_sec),
    )


def _read_verified_image_png(store: ProjectStore, output: ShotOutput) -> bytes:
    data = _read_project_bytes(store, output.relative_path, output.content_sha256)
    return _bounded_png(data, output.output_id)


def _read_project_bytes(store: ProjectStore, relative_path: str, content_sha256: str) -> bytes:
    try:
        data = store._root.read_bytes(relative_path)
    except (ContainmentError, OSError) as exc:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"cannot read output artifact {relative_path!r}: {exc}",
            cause=exc,
        ) from exc
    if hashlib.sha256(data).hexdigest() != content_sha256:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"output artifact hash mismatch: {relative_path}",
        )
    return data


def _bounded_png(data: bytes, output_id: str) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as image:
            # Image.open reads only the header; refuse oversized images before any
            # pixel data is decoded, so working memory stays bounded.
            width, height = image.size
            if width * height > _MAX_SOURCE_PIXELS:
                raise JobError(
                    JobErrorKind.CORRUPT_ARTIFACT,
                    f"image output {output_id!r} is {width}x{height}; inspection decodes at most "
                    f"{_MAX_SOURCE_PIXELS} pixels",
                )
            image.load()
            return _bounded_png_from_image(image, output_id)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"image output {output_id!r} cannot be decoded: {exc}",
            cause=exc,
        ) from exc


def _bounded_png_from_image(image: Image.Image, output_id: str) -> bytes:
    try:
        frame = image.copy()
        frame.thumbnail((_MAX_LONG_EDGE, _MAX_LONG_EDGE), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG")
        result = buffer.getvalue()
    except (OSError, ValueError) as exc:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"image output {output_id!r} cannot be encoded as PNG: {exc}",
            cause=exc,
        ) from exc
    if len(result) > _MAX_IMAGE_BYTES:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"inspected image output {output_id!r} is {len(result)} bytes; maximum is {_MAX_IMAGE_BYTES}",
        )
    return result


def _extract_video_frame(
    store: ProjectStore,
    *,
    relative_path: str,
    output_id: str,
    ffmpeg_exe: str | Path,
    at_sec: float,
) -> bytes:
    try:
        video_path = store._root.resolve(relative_path)
    except ContainmentError as exc:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"output artifact path {relative_path!r} is not contained in the project: {exc}",
            cause=exc,
        ) from exc
    if at_sec > _MAX_AT_SEC:
        raise FrameUnavailable(f"at_sec {at_sec:g} is past the end of the output")
    demuxer = _VIDEO_DEMUXERS[Path(relative_path).suffix.lower()]
    command = [
        str(ffmpeg_exe),
        "-hide_banner", "-nostdin", "-loglevel", "error",
        # Local files only, and a named demuxer: nothing the file refers to elsewhere
        # can be opened (see _VIDEO_DEMUXERS).
        "-protocol_whitelist", "file",
        "-f", demuxer,
        # Fixed-point: FFmpeg rejects Python's exponent form (1e-07) as a duration.
        "-ss", f"{at_sec:.6f}",
        "-i", str(video_path),
        "-frames:v", "1",
        # Downscale in FFmpeg so the piped frame is already within the size cap.
        "-vf", (
            f"scale=w='min(iw,{_MAX_LONG_EDGE})':h='min(ih,{_MAX_LONG_EDGE})'"
            ":force_original_aspect_ratio=decrease"
        ),
        "-f", "image2pipe",
        "-vcodec", "png",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"FFmpeg timed out after {_FFMPEG_TIMEOUT_SECONDS} seconds while inspecting output {output_id!r}",
            cause=exc,
        ) from exc
    except OSError as exc:
        raise JobError(
            JobErrorKind.INTERNAL,
            f"could not run FFmpeg to inspect output {output_id!r}: {exc}",
            cause=exc,
        ) from exc

    if not result.stdout and result.returncode == 0:
        raise FrameUnavailable(f"at_sec {at_sec:g} is past the end of the output")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"FFmpeg could not decode output {output_id!r}: {detail or f'exit status {result.returncode}' }",
        )
    return _bounded_png(result.stdout, output_id)
