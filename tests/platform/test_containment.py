import os
import subprocess
import sys

import pytest

from orchestrator.platform.containment import (
    ContainmentError,
    ProjectRoot,
    UploadLimits,
    UploadRejected,
    sanitize_filename,
    validate_upload,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64
WEBM = b"\x1a\x45\xdf\xa3" + b"\x42\x82\x84webm" + b"\x00" * 64
MKV = b"\x1a\x45\xdf\xa3" + b"\x42\x82\x88matroska" + b"\x00" * 64


@pytest.fixture
def root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return ProjectRoot(project)


def test_plain_nested_write_lands_inside_root(root):
    written = root.write_text("shots/s01/notes.txt", "hi")
    assert written.read_text() == "hi"
    assert os.path.commonpath([root.path, written]) == str(root.path)


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
def test_rejects_hostile_paths(root, bad):
    with pytest.raises(ContainmentError):
        root.resolve(bad)


def test_rejected_path_writes_nothing(root, tmp_path):
    with pytest.raises(ContainmentError):
        root.write_bytes("../outside.bin", b"x")
    assert not (tmp_path / "outside.bin").exists()


def test_non_string_rejected(root):
    with pytest.raises(ContainmentError):
        root.resolve(None)  # type: ignore[arg-type]


def test_error_names_path_and_fix(root):
    with pytest.raises(ContainmentError) as caught:
        root.resolve("../x")
    assert caught.value.path == "../x"
    assert "traversal" in str(caught.value)


def _symlink_or_skip(link, target):
    """Symlink, or an NTFS junction (needs no privilege) when symlinks are denied."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as error:
        if sys.platform != "win32":
            pytest.skip(f"symlinks unavailable here: {error}")
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False)
    if result.returncode != 0:
        pytest.skip("neither symlink nor junction could be created")


def test_symlink_escaping_root_is_rejected(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(root.path / "link", outside)
    with pytest.raises(ContainmentError, match="outside the project root"):
        root.write_bytes("link/evil.bin", b"x")
    assert not (outside / "evil.bin").exists()


def test_symlink_staying_inside_root_is_allowed(root):
    (root.path / "real").mkdir()
    _symlink_or_skip(root.path / "alias", root.path / "real")
    written = root.write_bytes("alias/ok.bin", b"x")
    assert written.parent == (root.path / "real")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows case-insensitivity")
def test_case_variant_of_root_is_still_inside_on_windows(tmp_path):
    project = tmp_path / "Project"
    project.mkdir()
    root = ProjectRoot(str(project).swapcase())
    assert root.resolve("a.txt").name == "a.txt"


def test_root_must_exist(tmp_path):
    with pytest.raises(ContainmentError, match="create it first"):
        ProjectRoot(tmp_path / "missing")


def test_write_with_hands_writer_a_contained_path(root):
    seen = []
    root.write_with("out/clip.mp4", lambda p: seen.append(p) or p.write_bytes(b"x"))
    assert seen[0] == root.path / "out" / "clip.mp4"


def test_make_dir(root):
    assert root.make_dir("a/b").is_dir()
    with pytest.raises(ContainmentError):
        root.make_dir("../c")


# --- filenames -----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../evil.png", "evil.png"),
        ("C:\\temp\\pic.png", "pic.png"),
        ("we?ird*name.png", "we_ird_name.png"),
        ("pic.png.", "pic.png"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", ["", "..", "///", "CON.png", "???", "nul"])
def test_sanitize_filename_rejects_unusable(raw):
    with pytest.raises(UploadRejected):
        sanitize_filename(raw)


# --- uploads -------------------------------------------------------------------------

LIMITS = UploadLimits()


@pytest.mark.parametrize(
    ("name", "head", "kind"),
    [
        ("a.png", PNG, "image"),
        ("a.JPG", JPEG, "image"),
        ("a.jpeg", JPEG, "image"),
        ("a.webp", WEBP, "image"),
        ("a.mp4", MP4, "video"),
        ("a.mov", MOV, "video"),
        ("a.webm", WEBM, "video"),
    ],
)
def test_valid_uploads(name, head, kind):
    assert validate_upload(name, head, 1000, LIMITS).kind == kind


@pytest.mark.parametrize(
    ("name", "head"),
    [
        ("a.png", JPEG),
        ("a.jpg", PNG),
        ("a.png", b"MZ\x90\x00" + b"\x00" * 60),
        ("a.mp4", PNG),
        ("a.mp4", MOV),
        ("a.webm", MKV),
        ("a.webp", PNG),
        ("a.png", b""),
    ],
)
def test_magic_bytes_must_agree_with_extension(name, head):
    with pytest.raises(UploadRejected, match="must agree"):
        validate_upload(name, head, 1000, LIMITS)


@pytest.mark.parametrize("name", ["a.exe", "a.svg", "a", "a.png.exe"])
def test_unlisted_extension_rejected(name):
    with pytest.raises(UploadRejected, match="not accepted"):
        validate_upload(name, PNG, 1000, LIMITS)


def test_oversize_image_reports_limit_and_observed():
    with pytest.raises(UploadRejected) as caught:
        validate_upload("a.png", PNG, LIMITS.max_image_bytes + 1, LIMITS)
    assert caught.value.limit_name == "max_image_bytes"
    assert caught.value.limit == LIMITS.max_image_bytes
    assert caught.value.observed == LIMITS.max_image_bytes + 1


def test_oversize_video_and_boundary_accepted():
    validate_upload("a.mp4", MP4, LIMITS.max_video_bytes, LIMITS)
    with pytest.raises(UploadRejected) as caught:
        validate_upload("a.mp4", MP4, LIMITS.max_video_bytes + 1, LIMITS)
    assert caught.value.limit_name == "max_video_bytes"


def test_pixel_and_duration_caps_when_observed():
    with pytest.raises(UploadRejected) as pixels:
        validate_upload("a.png", PNG, 10, LIMITS, image_long_edge_px=9000)
    assert (pixels.value.limit, pixels.value.observed) == (8192, 9000)
    with pytest.raises(UploadRejected) as seconds:
        validate_upload("a.mp4", MP4, 10, LIMITS, duration_seconds=121.5)
    assert (seconds.value.limit, seconds.value.observed) == (120.0, 121.5)


def test_custom_limits_apply():
    tight = UploadLimits(max_image_bytes=10)
    with pytest.raises(UploadRejected):
        validate_upload("a.png", PNG, 11, tight)


def test_upload_filename_is_sanitized():
    assert validate_upload("../../x.png", PNG, 1, LIMITS).safe_filename == "x.png"
