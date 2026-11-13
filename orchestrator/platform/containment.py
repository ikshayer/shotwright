"""Storage containment and upload validation (FR-25, AT-17).

Every filesystem write in the project goes through ``ProjectRoot``. A caller never
holds a raw ``Path`` it composed itself: it hands ``ProjectRoot`` a *relative* path and
gets back either a resolved path inside the root or a ``ContainmentError`` that says
what to fix.

Limit worth knowing: resolution and the later open are two steps, so a symlink swapped
in between could still redirect a write (TOCTOU). The orchestrator is the only writer to
the project tree, which is what makes this acceptable; do not expose the root to other
writers.
"""

from __future__ import annotations

import io
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field

_SUPERSCRIPT_DIGITS = "¹²³"  # also reserved device-name suffixes on Windows
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{n}" for n in range(10)}
    | {f"LPT{n}" for n in range(10)}
    | {f"COM{c}" for c in _SUPERSCRIPT_DIGITS}
    | {f"LPT{c}" for c in _SUPERSCRIPT_DIGITS}
)
_ILLEGAL_COMPONENT_CHARS = frozenset('<>:"|?*') | {chr(c) for c in range(32)}
_SHORT_NAME_PATTERN = re.compile(r"~\d")
ATOMIC_WRITE_DIRECTORY = ".projectroot-atomic-write"
_REPLACE_DELAYS_SECONDS = (0.01, 0.02, 0.04, 0.08)
_READ_DELAYS_SECONDS = _REPLACE_DELAYS_SECONDS


class ContainedReadError(PermissionError):
    """A contained read remained blocked after bounded retries."""

    def __init__(self, relative: str, cause: PermissionError, *, attempts: int) -> None:
        self.relative = relative
        self.attempts = attempts
        super().__init__(
            cause.errno,
            f"Contained read of {relative!r} was denied after {attempts} attempt(s): {cause}",
            getattr(cause, "filename", None),
        )
        self.__cause__ = cause
_APPEND_LOCK = threading.Lock()


class AtomicWriteError(OSError):
    """An atomic staging or replacement operation failed without an in-place fallback."""

    def __init__(
        self, relative: str, message: str, *, attempts: int, errno_code: int | None = None
    ) -> None:
        self.relative = relative
        self.attempts = attempts
        super().__init__(
            errno_code,
            f"Atomic write of {relative!r} failed after {attempts} attempt(s): {message}",
        )


class ContainmentError(ValueError):
    """A path was rejected. ``path`` is the offending input; the message says how to fix it."""

    def __init__(self, path: str, problem: str) -> None:
        self.path = path
        self.problem = problem
        super().__init__(f"Path {path!r} rejected: {problem}")


class UploadRejected(ValueError):
    """An upload was rejected. For size limits, ``limit`` and ``observed`` are populated."""

    def __init__(
        self,
        message: str,
        *,
        limit_name: str | None = None,
        limit: float | None = None,
        observed: float | None = None,
    ) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.observed = observed
        super().__init__(message)


def _component_problem(component: str) -> str | None:
    if component in ("", "."):
        return None  # harmless; dropped by the caller
    if component == "..":
        return "'..' traversal is not allowed; use a path below the project root"
    bad = _ILLEGAL_COMPONENT_CHARS.intersection(component)
    if bad:
        shown = "".join(sorted(c if c.isprintable() else f"\\x{ord(c):02x}" for c in bad))
        return (
            f"component {component!r} contains {shown!r}; ':' also covers drive-relative "
            "paths and NTFS alternate data streams"
        )
    if component != component.rstrip(" ."):
        return f"component {component!r} ends in a dot or space, which Windows silently strips"
    if component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        return f"component {component!r} is a reserved Windows device name"
    if _SHORT_NAME_PATTERN.search(component):
        return f"component {component!r} looks like a Windows 8.3 short name; use the full name"
    return None


def _split_relative(relative: str) -> list[str]:
    """Validate a caller-supplied relative path string and return its components."""
    if not isinstance(relative, str):
        raise ContainmentError(repr(relative), "path must be a string relative to the project root")
    if not relative.strip():
        raise ContainmentError(relative, "path is empty")
    if "\x00" in relative:
        raise ContainmentError(relative, "path contains a NUL byte")
    # Both separators count on every OS so behaviour does not depend on the platform.
    components = relative.replace("\\", "/").split("/")
    if components[0] == "":
        raise ContainmentError(
            relative, "absolute and UNC paths are not allowed; give a path relative to the project root"
        )
    if re.fullmatch(r"[A-Za-z]:", components[0]):
        raise ContainmentError(
            relative, "drive-qualified paths are not allowed; give a path relative to the project root"
        )
    for component in components:
        problem = _component_problem(component)
        if problem:
            raise ContainmentError(relative, problem)
    kept = [c for c in components if c not in ("", ".")]
    if not kept:
        raise ContainmentError(relative, "path names the project root itself, not something inside it")
    return kept


def _is_within(root: Path, candidate: Path) -> bool:
    """Case-insensitive on Windows (via normcase), exact elsewhere; both already resolved."""
    root_norm = os.path.normcase(str(root))
    candidate_norm = os.path.normcase(str(candidate))
    try:
        return os.path.commonpath([root_norm, candidate_norm]) == root_norm
    except ValueError:  # different drives
        return False


class ProjectRoot:
    """The only object allowed to turn a relative name into a writable location."""

    def __init__(self, root: Path | str, *, use_windows_share_delete_reads: bool = False) -> None:
        resolved = Path(root).resolve(strict=False)
        if not resolved.is_dir():
            raise ContainmentError(str(root), "project root does not exist or is not a directory; create it first")
        self._root = resolved
        self._use_windows_share_delete_reads = use_windows_share_delete_reads

    @property
    def path(self) -> Path:
        return self._root

    def resolve(self, relative: str) -> Path:
        """Return the absolute, symlink-resolved location of ``relative``; raise if it leaves the root."""
        components = _split_relative(relative)
        resolved = self._root.joinpath(*components).resolve(strict=False)
        if not _is_within(self._root, resolved) or resolved == self._root:
            raise ContainmentError(
                relative,
                f"resolves to {str(resolved)!r}, outside the project root {str(self._root)!r} "
                "(a symlink or junction points out of the root)",
            )
        return resolved

    def read_bytes(self, relative: str) -> bytes:
        """Read contained bytes with bounded PermissionError retry.

        ``use_windows_share_delete_reads`` opts into a slower Windows ``CreateFileW``
        open requesting ``FILE_SHARE_DELETE``. The measured Amendment 2 replacement
        experiment disproved its original rationale: it did not permit replacement
        while a reader was open on this machine. This option is retained for evaluation;
        it is not known to fix anything and is not a recommendation.
        """
        target = self.resolve(relative)
        for attempt in range(len(_READ_DELAYS_SECONDS) + 1):
            try:
                with self._open_read(target) as stream:
                    return stream.read()
            except PermissionError as exc:
                if target.is_dir():
                    raise
                if attempt == len(_READ_DELAYS_SECONDS):
                    raise ContainedReadError(relative, exc, attempts=attempt + 1) from exc
                time.sleep(_READ_DELAYS_SECONDS[attempt])
        raise AssertionError("unreachable")

    def read_text(self, relative: str, *, encoding: str = "utf-8") -> str:
        """Read text using Path.read_text's strict decoding and universal newlines.

        The optional Windows share-delete handle is slower and the Amendment 2
        measurement disproved its original justification. It is retained only as an
        option; it is not known to fix anything and is not a recommendation.
        """
        target = self.resolve(relative)
        for attempt in range(len(_READ_DELAYS_SECONDS) + 1):
            try:
                with (
                    self._open_read(target) as binary,
                    io.TextIOWrapper(binary, encoding=encoding, newline=None) as stream,
                ):
                    return stream.read()
            except PermissionError as exc:
                if target.is_dir():
                    raise
                if attempt == len(_READ_DELAYS_SECONDS):
                    raise ContainedReadError(relative, exc, attempts=attempt + 1) from exc
                time.sleep(_READ_DELAYS_SECONDS[attempt])
        raise AssertionError("unreachable")

    def _open_read(self, target: Path):
        if self._use_windows_share_delete_reads and os.name == "nt":
            return _open_shared_read(target)
        return target.open("rb")

    def make_dir(self, relative: str) -> Path:
        target = self.resolve(relative)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def write_bytes(self, relative: str, data: bytes) -> Path:
        target = self._prepare_file(relative)

        def write_temporary(temporary: Path) -> None:
            with temporary.open("xb") as stream:
                stream.write(data)

        self._write_atomically(relative, target, write_temporary)
        return target

    def write_stream(
        self, relative: str, source: BinaryIO, *, chunk_size: int = 1024 * 1024
    ) -> Path:
        """Atomically copy a binary stream to a contained project-relative file."""
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        target = self._prepare_file(relative)

        def write_temporary(temporary: Path) -> None:
            with temporary.open("xb") as destination:
                while chunk := source.read(chunk_size):
                    if not isinstance(chunk, bytes):
                        raise TypeError("binary stream read() must return bytes")
                    destination.write(chunk)

        self._write_atomically(relative, target, write_temporary)
        return target

    def write_text(self, relative: str, text: str, *, encoding: str = "utf-8") -> Path:
        target = self._prepare_file(relative)

        def write_temporary(temporary: Path) -> None:
            with temporary.open("x", encoding=encoding, newline="\n") as stream:
                stream.write(text)

        self._write_atomically(relative, target, write_temporary)
        return target

    def append_line(self, relative: str, text: str) -> Path:
        """Append one UTF-8 line to a contained file and close it before returning."""
        if not isinstance(text, str):
            raise TypeError("line text must be a string")
        if "\n" in text or "\r" in text:
            raise ValueError("line text must not contain embedded line breaks")
        target = self._prepare_file(relative)
        with _APPEND_LOCK, target.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() > 0:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    stream.seek(0, os.SEEK_END)
                    stream.write(b"\n")
            stream.seek(0, os.SEEK_END)
            stream.write((text + "\n").encode("utf-8"))
            stream.flush()
        return target

    def write_with(self, relative: str, writer: Callable[[Path], None]) -> Path:
        """For external tools (FFmpeg, Blender) that must be handed an output path.

        The writer receives only an already-contained path. This is non-atomic by design:
        callers that need atomic content writes must use ``write_bytes`` or ``write_text``.
        """
        target = self._prepare_file(relative)
        writer(target)
        return target

    def _write_atomically(
        self, relative: str, target: Path, writer: Callable[[Path], None]
    ) -> None:
        """Stage bytes below the project root and atomically replace the contained target."""
        staging_directory = self.make_dir(ATOMIC_WRITE_DIRECTORY)
        expected_staging_directory = self._root / ATOMIC_WRITE_DIRECTORY
        if staging_directory != expected_staging_directory:
            raise AtomicWriteError(
                relative,
                "reserved staging directory must not be a symlink or junction",
                attempts=0,
            )
        temporary_relative = f"{ATOMIC_WRITE_DIRECTORY}/write-{uuid.uuid4().hex}.tmp"
        temporary = self._prepare_file(temporary_relative)
        try:
            if os.stat(temporary.parent).st_dev != os.stat(target.parent).st_dev:
                raise AtomicWriteError(
                    relative,
                    "staging directory and target parent are on different filesystems",
                    attempts=0,
                )
            writer(temporary)
            self._replace_atomically(temporary, target, relative)
        except BaseException:
            self._remove_temporary(temporary, relative)
            raise

    @staticmethod
    def _remove_temporary(temporary: Path, relative: str) -> None:
        for attempt in range(len(_REPLACE_DELAYS_SECONDS) + 1):
            try:
                temporary.unlink(missing_ok=True)
                return
            except PermissionError as exc:
                if attempt == len(_REPLACE_DELAYS_SECONDS):
                    raise AtomicWriteError(
                        relative,
                        f"temporary-file cleanup was denied: {exc}",
                        attempts=attempt + 1,
                        errno_code=exc.errno,
                    ) from exc
                time.sleep(_REPLACE_DELAYS_SECONDS[attempt])
            except OSError as exc:
                raise AtomicWriteError(
                    relative,
                    f"temporary-file cleanup failed: {exc}",
                    attempts=attempt + 1,
                    errno_code=exc.errno,
                ) from exc

    @staticmethod
    def _replace_atomically(temporary: Path, target: Path, relative: str) -> None:
        for attempt in range(len(_REPLACE_DELAYS_SECONDS) + 1):
            try:
                os.replace(temporary, target)
                return
            except PermissionError as exc:
                if attempt == len(_REPLACE_DELAYS_SECONDS):
                    raise AtomicWriteError(
                        relative,
                        f"replace was denied: {exc}",
                        attempts=attempt + 1,
                        errno_code=exc.errno,
                    ) from exc
                time.sleep(_REPLACE_DELAYS_SECONDS[attempt])
            except OSError as exc:
                raise AtomicWriteError(
                    relative,
                    f"replace failed: {exc}",
                    attempts=attempt + 1,
                    errno_code=exc.errno,
                ) from exc

    def _prepare_file(self, relative: str) -> Path:
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


def _open_shared_read(path: Path):
    """Open with Windows share-delete flags; used only by the explicit opt-in path."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # FILE_SHARE_READ | WRITE | DELETE
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        windows_error = ctypes.WinError(ctypes.get_last_error())
        raise OSError(windows_error.errno, windows_error.strerror, str(path)) from windows_error

    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


# --- filenames and uploads -------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Reduce an untrusted filename to a safe single path component, or raise.

    Directory parts are discarded and unsafe characters become '_'. Names that are
    empty or reserved are rejected rather than silently renamed.
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join("_" if (c in _ILLEGAL_COMPONENT_CHARS or not c.isprintable()) else c for c in base)
    cleaned = cleaned.strip().rstrip(" .")
    cleaned = _SHORT_NAME_PATTERN.sub("_", cleaned)
    if cleaned in ("", ".", "..") or not cleaned.strip("_"):
        raise UploadRejected(f"Filename {name!r} has no usable characters; rename the file and retry")
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise UploadRejected(f"Filename {name!r} is a reserved Windows device name; rename the file and retry")
    return cleaned[:128]


class UploadLimits(BaseModel):
    """Section 6.1 default limits; overridable through settings (FR-23)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_image_bytes: int = Field(25 * 1024 * 1024, gt=0, description="Maximum image upload size in bytes.")
    max_image_long_edge_px: int = Field(8192, gt=0, description="Maximum image long edge in pixels.")
    max_video_bytes: int = Field(500 * 1024 * 1024, gt=0, description="Maximum video upload size in bytes.")
    max_video_seconds: float = Field(120.0, gt=0, description="Maximum video duration in seconds.")
    max_model_bytes: int = Field(500 * 1024 * 1024, gt=0, description="Maximum GLB model upload size in bytes.")


MediaKind = Literal["image", "video", "model"]

_EXTENSION_KIND: dict[str, MediaKind] = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image",
    ".hdr": "image", ".exr": "image",
    ".mp4": "video", ".mov": "video", ".webm": "video",
    ".glb": "model",
}


#: Formats refused with their own reason instead of the generic allowlist
#: message. The allowlist above is still the gate and still default-deny; this
#: only means a caller who sends one of these is told why this project will not
#: take it, which Decision 2 of the asset contract requires.
_REFUSED_REASONS: dict[str, str] = {
    ".blend": (
        "Extension '.blend' is not accepted: a .blend can carry executable "
        "Python that runs on load, and this project never enables script "
        "auto-run. Convert the asset to .glb."
    ),
    ".gltf": (
        "Extension '.gltf' is not accepted: glTF JSON references external "
        "buffer and texture files, so it is neither self-contained nor "
        "verifiable by a single checksum. Use .glb, which embeds them."
    ),
}


def _extensions_matching_magic(head: bytes) -> frozenset[str] | None:
    """Extensions consistent with the leading bytes; None if unrecognised."""
    if head.startswith(b"glTF"):
        return frozenset({".glb"})
    if head.startswith(b"\xff\xd8\xff"):
        return frozenset({".jpg", ".jpeg"})
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return frozenset({".png"})
    if head.startswith((b"#?RADIANCE", b"#?RGBE")):
        return frozenset({".hdr"})
    if head.startswith(b"\x76\x2f\x31\x01"):
        return frozenset({".exr"})
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return frozenset({".webp"})
    if head[4:8] == b"ftyp":
        # QuickTime brand is .mov only; every other ISO-BMFF brand may be mp4 or mov.
        return frozenset({".mov"}) if head[8:12] == b"qt  " else frozenset({".mp4", ".mov"})
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        # EBML header shared by Matroska and WebM; WebM declares its DocType early on.
        return frozenset({".webm"}) if b"webm" in head[:64] else frozenset()
    return None


@dataclass(frozen=True)
class ValidatedUpload:
    kind: MediaKind
    extension: str
    safe_filename: str


def validate_upload(
    filename: str,
    head: bytes,
    size_bytes: int,
    limits: UploadLimits,
    *,
    image_long_edge_px: int | None = None,
    duration_seconds: float | None = None,
) -> ValidatedUpload:
    """Check extension, magic bytes and size caps; raise ``UploadRejected`` otherwise.

    ``head`` is the first bytes of the file (64 or more recommended). Pixel and duration
    caps are checked only when the caller supplies the observed value, because measuring
    them needs a decoder (ffprobe), which this pure module does not use.
    """
    safe_name = sanitize_filename(filename)
    extension = os.path.splitext(safe_name)[1].lower()
    kind = _EXTENSION_KIND.get(extension)
    if kind is None:
        reason = _REFUSED_REASONS.get(extension)
        if reason is not None:
            raise UploadRejected(reason)
        allowed = ", ".join(sorted(_EXTENSION_KIND))
        raise UploadRejected(f"Extension {extension or '(none)'!r} is not accepted; allowed: {allowed}")

    matching = _extensions_matching_magic(head)
    if matching is None or extension not in matching:
        detected = "unrecognised content" if not matching else f"content of type {sorted(matching)}"
        raise UploadRejected(
            f"File {filename!r} has extension {extension} but its bytes show {detected}; "
            "the extension and the file contents must agree"
        )

    if kind == "image":
        _enforce("max_image_bytes", limits.max_image_bytes, size_bytes)
        if image_long_edge_px is not None:
            _enforce("max_image_long_edge_px", limits.max_image_long_edge_px, image_long_edge_px)
    elif kind == "video":
        _enforce("max_video_bytes", limits.max_video_bytes, size_bytes)
        if duration_seconds is not None:
            _enforce("max_video_seconds", limits.max_video_seconds, duration_seconds)
    else:
        _enforce("max_model_bytes", limits.max_model_bytes, size_bytes)
    return ValidatedUpload(kind=kind, extension=extension, safe_filename=safe_name)


def _enforce(limit_name: str, limit: float, observed: float) -> None:
    if observed > limit:
        raise UploadRejected(
            f"Upload exceeds {limit_name}: limit {limit}, observed {observed}. "
            f"Reduce the file or raise {limit_name} in settings.toml.",
            limit_name=limit_name,
            limit=limit,
            observed=observed,
        )
