import os
import threading
import time
from pathlib import Path

import pytest

from orchestrator.platform.containment import ContainmentError, ProjectRoot


def test_concurrent_writers_never_expose_partial_or_empty_content(tmp_path):
    """Exercise the original truncate-then-write race with sustained, real I/O."""
    project = tmp_path / "project"
    project.mkdir()
    root = ProjectRoot(project)
    target = root.write_bytes("state/jobs/job.json", b"I" * (4 * 1024 * 1024))

    payloads = (b"A" * (4 * 1024 * 1024), b"B" * (4 * 1024 * 1024))
    iterations = 24
    start = threading.Event()
    writers_done = threading.Event()
    failures: list[str] = []
    writer_errors: list[BaseException] = []

    def writer(payload: bytes) -> None:
        try:
            if not start.wait(timeout=10):
                raise TimeoutError("writer start gate was not released")
            for _ in range(iterations):
                root.write_bytes("state/jobs/job.json", payload)
        except BaseException as error:  # noqa: BLE001 - report every worker-thread failure to pytest
            writer_errors.append(error)

    def reader() -> None:
        try:
            if not start.wait(timeout=10):
                raise TimeoutError("reader start gate was not released")
            deadline = time.monotonic() + 40
            while not writers_done.is_set() and time.monotonic() < deadline:
                observed = target.read_bytes()
                if observed not in payloads and observed != b"I" * (4 * 1024 * 1024):
                    failures.append(
                        f"reader observed {len(observed)} bytes; expected one complete "
                        "4 MiB writer payload (or the initial complete 4 MiB value)"
                    )
                    return
            if time.monotonic() >= deadline and not writers_done.is_set():
                failures.append("writers did not finish within the 40 second test deadline")
        except BaseException as error:  # noqa: BLE001 - report every reader-thread failure to pytest
            failures.append(f"reader raised {type(error).__name__}: {error}")

    threads = [threading.Thread(target=writer, args=(payload,)) for payload in payloads]
    reader_thread = threading.Thread(target=reader)
    for thread in [*threads, reader_thread]:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=40)
    if all(not thread.is_alive() for thread in threads):
        writers_done.set()
    reader_thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in [*threads, reader_thread]), (
        "concurrency test threads exceeded their bounded join time"
    )
    assert not writer_errors, f"writer thread raised: {writer_errors!r}"
    assert not failures, "atomic write regression: " + "; ".join(failures)
    assert target.read_bytes() in payloads, "final target was not one complete writer version"


def test_temp_artifact_is_contained_and_invisible_to_enumerators(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    root = ProjectRoot(project)
    target = root.path / "jobs" / "job.json"
    target.parent.mkdir()
    original_replace = os.replace
    seen_sources: list[Path] = []
    enumeration_observations: list[str] = []

    def inspect_then_replace(source, destination):
        source_path = Path(source).resolve()
        destination_path = Path(destination).resolve()
        seen_sources.append(source_path)
        assert source_path.is_file(), "atomic temp artifact did not exist at replace time"
        assert os.path.commonpath([str(root.path), str(source_path)]) == str(root.path), (
            f"atomic temp artifact escaped the project root: {source_path}"
        )
        assert destination_path == target.resolve()

        # Exercise each repository enumeration family while the temporary source exists.
        directories = [root.path, *(path for path in root.path.rglob("*") if path.is_dir())]
        for directory in directories:
            if source_path.parent == directory:
                if source_path in directory.glob("*"):
                    enumeration_observations.append(f"glob('*') in {directory} saw {source_path.name}")
                if source_path in directory.glob("*.json"):
                    enumeration_observations.append(f"glob('*.json') in {directory} saw {source_path.name}")
                if source_path in directory.iterdir():
                    enumeration_observations.append(f"iterdir() in {directory} saw {source_path.name}")
        if source_path in root.path.rglob("*"):
            enumeration_observations.append(f"root.rglob('*') saw {source_path.relative_to(root.path)}")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", inspect_then_replace)
    root.write_text("jobs/job.json", '{"state":"complete"}')

    assert seen_sources, "write_text did not stage content and atomically replace the target"
    assert not enumeration_observations, "temp artifact visible to enumeration: " + "; ".join(
        enumeration_observations
    )


def test_failed_atomic_replace_preserves_target_and_leaves_no_debris(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    root = ProjectRoot(project)
    target = root.write_bytes("state/jobs/job.json", b"previous complete record")
    before = target.read_bytes()

    def fail_replace(source, destination):
        raise PermissionError("simulated Windows sharing violation")

    monkeypatch.setattr(os, "replace", fail_replace)
    # The contract requires a loud classified failure but does not name its exception type.
    with pytest.raises(Exception):  # noqa: B017
        root.write_bytes("state/jobs/job.json", b"new complete record")

    assert target.read_bytes() == before, "failed replace changed the previous target contents"
    debris = [path for path in root.path.rglob("*") if path.is_file() and path != target]
    assert debris == [], f"failed write left temp debris: {debris!r}"


def test_text_write_uses_utf8_and_lf_bytes(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    root = ProjectRoot(project)

    written = root.write_text("notes.txt", "caf\u00e9\nsecond line\n")

    assert written.read_bytes() == "caf\u00e9\nsecond line\n".encode("utf-8")


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
        "",
        "   ",
        ".",
        "./",
        "a\x00b",
        "CON",
        "nul.txt",
        "dir/COM1",
        "dir/lpt3.log",
        "aux.tar.gz",
        "file.txt:stream",
        "trailing.",
        "trailing ",
        "PROGRA~1/x",
        'bad"name',
        "bad|name",
    ],
)
def test_both_atomic_write_apis_keep_rejecting_escaping_or_hostile_paths(tmp_path, bad):
    project = tmp_path / "project"
    project.mkdir()
    root = ProjectRoot(project)

    with pytest.raises(ContainmentError):
        root.write_bytes(bad, b"must not be written")
    with pytest.raises(ContainmentError):
        root.write_text(bad, "must not be written")
    assert list(project.rglob("*")) == [], f"rejected write created a path for {bad!r}"


def test_write_with_mkdir_receives_real_target_path(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    root = ProjectRoot(project)
    received: list[Path] = []

    root.write_with("renders/shot", lambda path: (received.append(path), path.mkdir(exist_ok=False)))

    assert received == [root.path / "renders" / "shot"]
    assert received[0].is_dir()


def test_write_with_reserve_then_external_write_receives_real_target(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    root = ProjectRoot(project)
    target = root.path / "renders" / "master.blend"
    received: list[Path] = []

    def reserve(path: Path) -> None:
        received.append(path)
        assert path == target
        assert not path.exists(), "reserve-then-write callback must receive the unused real target"

    root.write_with("renders/master.blend", reserve)
    received[0].write_bytes(b"external tool output")

    assert received == [target]
    assert target.read_bytes() == b"external tool output"


def test_write_with_open_xb_receives_real_target_and_exclusive_create_works(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    root = ProjectRoot(project)
    received: list[Path] = []

    root.write_with("post/grade.mp4", lambda path: (received.append(path), path.open("xb").close()))

    assert received == [root.path / "post" / "grade.mp4"]
    assert received[0].is_file()
    with pytest.raises(FileExistsError):
        received[0].open("xb")


def test_write_with_is_non_atomic_by_design(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    root = ProjectRoot(project)

    def write_part_then_fail(path: Path) -> None:
        assert path == root.path / "result.bin"
        path.write_bytes(b"partial external output")
        raise RuntimeError("external writer failed")

    with pytest.raises(RuntimeError, match="external writer failed"):
        root.write_with("result.bin", write_part_then_fail)

    assert (root.path / "result.bin").read_bytes() == b"partial external output"
