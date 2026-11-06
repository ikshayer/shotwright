"""Contract tests for ProjectRoot's contained read surface.

These tests intentionally target methods that are absent from the atomic-combined
baseline. Red AttributeErrors against that baseline are expected until the parallel
implementation lands.
"""

import os
import threading
import time
from pathlib import Path

import pytest

from orchestrator.platform.containment import AtomicWriteError, ContainmentError, ProjectRoot


def _project(tmp_path: Path) -> ProjectRoot:
    directory = tmp_path / "project"
    directory.mkdir()
    return ProjectRoot(directory)


def test_read_bytes_round_trip_is_byte_exact(tmp_path):
    root = _project(tmp_path)
    payload = bytes(range(256)) * 257

    root.write_bytes("nested/data.bin", payload)

    assert root.read_bytes("nested/data.bin") == payload


def test_read_text_round_trip_preserves_non_ascii_and_lf(tmp_path):
    root = _project(tmp_path)
    text = "caf\u00e9 \u96ea \U0001f47b\nsecond line\n"

    root.write_text("nested/notes.txt", text)

    assert (root.path / "nested" / "notes.txt").read_bytes() == text.encode("utf-8")
    assert root.read_text("nested/notes.txt") == text


def test_read_text_normalizes_preexisting_crlf_like_path_read_text(tmp_path):
    """Legacy CRLF is translated to LF to retain Path.read_text compatibility."""
    root = _project(tmp_path)
    legacy = b"first line\r\nsecond line\r\n"
    path = root.path / "legacy.txt"
    path.write_bytes(legacy)

    expected_current_path_behavior = path.read_text(encoding="utf-8")

    assert expected_current_path_behavior == "first line\nsecond line\n"
    assert root.read_text("legacy.txt") == expected_current_path_behavior


@pytest.mark.parametrize("method", ["read_bytes", "read_text"])
def test_missing_file_keeps_path_read_exception_type(tmp_path, method):
    root = _project(tmp_path)
    path = root.resolve("missing.txt")
    reader = getattr(root, method)
    baseline_reader = path.read_bytes if method == "read_bytes" else path.read_text

    with pytest.raises(FileNotFoundError) as baseline:
        baseline_reader()
    with pytest.raises(type(baseline.value)):
        reader("missing.txt")


@pytest.mark.parametrize("method", ["read_bytes", "read_text"])
def test_directory_keeps_path_read_exception_type(tmp_path, method):
    root = _project(tmp_path)
    directory = root.path / "directory"
    directory.mkdir()
    reader = getattr(root, method)
    baseline_reader = directory.read_bytes if method == "read_bytes" else directory.read_text

    with pytest.raises(OSError) as baseline:
        baseline_reader()
    with pytest.raises(type(baseline.value)):
        reader("directory")


def test_invalid_utf8_keeps_path_decode_error(tmp_path):
    root = _project(tmp_path)
    path = root.path / "invalid.txt"
    path.write_bytes(b"valid prefix \xff invalid suffix")

    with pytest.raises(UnicodeDecodeError) as baseline:
        path.read_text(encoding="utf-8")
    with pytest.raises(type(baseline.value)):
        root.read_text("invalid.txt")


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.txt",
        "a/../../escape.txt",
        "a\\..\\..\\escape.txt",
        "/etc/passwd",
        "\\Windows\\system32",
        "\\\\server\\share\\x",
        "//server/share/x",
        "C:\\Windows\\x",
        "C:x",
        "C:",
        "CON",
        "nul.txt",
        "dir/COM1",
        "dir/lpt3.log",
        "aux.tar.gz",
    ],
)
def test_both_read_apis_keep_rejecting_escaping_or_hostile_paths(tmp_path, bad):
    root = _project(tmp_path)

    with pytest.raises(ContainmentError):
        root.read_bytes(bad)
    with pytest.raises(ContainmentError):
        root.read_text(bad)


def test_concurrent_read_loop_never_observes_a_partial_version(tmp_path):
    """Large reads overlap replacement; every observation must be a whole version."""
    root = _project(tmp_path)
    relative = "state/record.bin"
    size = 4 * 1024 * 1024
    initial = b"I" * size
    payloads = (b"A" * size, b"B" * size)
    allowed = {initial, *payloads}
    root.write_bytes(relative, initial)

    iterations = 16
    start = threading.Event()
    reader_ready = threading.Event()
    writers_done = threading.Event()
    observations: list[bytes] = []
    reader_errors: list[str] = []
    writes_completed: list[bytes] = []
    writes_refused: list[bytes] = []
    writer_errors: list[str] = []

    def writer(payload: bytes) -> None:
        try:
            if not start.wait(timeout=10):
                raise TimeoutError("writer start gate was not released")
            if not reader_ready.wait(timeout=10):
                raise TimeoutError("reader did not enter its read loop")
            for _ in range(iterations):
                try:
                    root.write_bytes(relative, payload)
                    writes_completed.append(payload[:1])
                except AtomicWriteError:
                    # Sanctioned. Amendment 2 of ATOMIC_WRITE_CONTRACT.md measured that
                    # os.replace over an open file is refused on this platform whatever
                    # share mode the reader used, so this cannot be engineered away from
                    # the read side. The guarantee under test is the READER's: it never
                    # observes a partial version. A refused write is loud, not silent.
                    writes_refused.append(payload[:1])
        except BaseException as error:  # noqa: BLE001 - preserve worker failures for the assertion
            writer_errors.append(f"{type(error).__name__}: {error}")

    def reader() -> None:
        try:
            if not start.wait(timeout=10):
                raise TimeoutError("reader start gate was not released")
            deadline = time.monotonic() + 50
            while not writers_done.is_set() and time.monotonic() < deadline:
                reader_ready.set()
                observed = root.read_bytes(relative)
                observations.append(observed)
                if observed not in allowed:
                    reader_errors.append(
                        f"reader observed {len(observed)} bytes (prefix={observed[:16]!r}); "
                        f"expected exactly one of the complete {size // (1024 * 1024)} MiB "
                        "initial/A/B payloads"
                    )
                    return
            if time.monotonic() >= deadline and not writers_done.is_set():
                reader_errors.append("writer threads did not finish within the 50 second deadline")
        except BaseException as error:  # noqa: BLE001 - capture exceptions raised in reader thread
            reader_errors.append(f"reader raised {type(error).__name__}: {error}")

    threads = [
        threading.Thread(target=writer, args=(payload,), name=f"writer-{payload[:1].decode()}")
        for payload in payloads
    ]
    reader_thread = threading.Thread(target=reader, name="contained-reader")
    all_threads = [*threads, reader_thread]
    for thread in all_threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=50)
    if all(not thread.is_alive() for thread in threads):
        writers_done.set()
    reader_thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in all_threads), (
        "concurrent read/replace threads exceeded their bounded join time"
    )
    assert not writer_errors, (
        f"writer raised something other than a sanctioned refusal: {writer_errors}"
    )
    # A run in which every write was refused proves nothing about what the reader saw.
    assert writes_completed, (
        f"no write completed ({len(writes_refused)} refused); the reader never observed "
        "a replacement, so this run cannot speak to partial reads"
    )
    assert not reader_errors, "read-during-replace failure: " + "; ".join(reader_errors)
    assert observations, "reader completed no reads while the writer was replacing the target"
    final = root.read_bytes(relative)
    assert final in payloads, f"final target was {len(final)} bytes, not one complete writer payload"


@pytest.mark.parametrize("method", ["read_bytes", "read_text"])
def test_permanent_open_refusal_is_loud_and_has_no_plain_read_fallback(tmp_path, method):
    """A denied read must raise; it must not silently switch to an ordinary Path read."""
    root = _project(tmp_path)
    target = root.write_bytes("locked.bin", b"contents")
    reader = getattr(root, method)
    baseline_reader = target.read_bytes if method == "read_bytes" else target.read_text
    baseline_error: OSError | None = None

    if os.name == "nt":
        # An exclusive Win32 handle makes the ordinary Path read fail with the actual
        # platform permission error, without depending on ACL administration rights.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        invalid = wintypes.HANDLE(-1).value
        handle = create_file(str(target), 0x80000000, 0, None, 3, 0x80, None)
        assert handle != invalid, f"could not create exclusive read-denial handle: {ctypes.get_last_error()}"
        try:
            with pytest.raises(OSError) as baseline:
                baseline_reader()
            baseline_error = baseline.value
            with pytest.raises(Exception):  # noqa: B017 - classified error type is deliberately unspecified
                reader("locked.bin")
        finally:
            assert close_handle(handle), f"could not release exclusive handle: {ctypes.get_last_error()}"
    else:
        pytest.skip("a portable permission-denied open cannot be established on this platform")

    assert baseline_error is not None

