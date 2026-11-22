"""AT-09 through the public export API and CLI, with a relocatable mixed-schema fixture."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.cli import main as cli_main
from orchestrator.domain.base import SCHEMA_VERSION, ShotStatus
from orchestrator.domain.entities import (
    Asset,
    CameraProfile,
    Episode,
    EpisodeOutput,
    Export,
    Location,
    Project,
    Review,
    Script,
    Shot,
    ShotOutput,
    ShotOutputKind,
    StyleBible,
    Timeline,
    TimelineEntry,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.service.approval import approve_episode
from orchestrator.store.json_store import ProjectStore
from tests.service.test_timeline_assembly_contract import _approve_fixture_shot

_PROJECT = "portable"
_EPISODE = "ep1"
_TOKEN = "at09-loopback-token"
_SHOTS = ("A", "B")
_CUT = f"episodes/{_EPISODE}/outputs/cuts/approved-cut.mp4"
_CUT_LEDGER = f"episodes/{_EPISODE}/outputs/cut_receipts/o00000001-approved-cut.json"
_TIMELINE = f"episodes/{_EPISODE}/edits/timeline.json"
_SCRIPT = f"episodes/{_EPISODE}/scripts/script/v0001.json"
_STEM = "assets/audio/voice.wav"
_REFERENCE_ASSET = "assets/models/private-upload.mp4"
_SECRET_FILES = {
    _REFERENCE_ASSET: b"private upload in registered asset storage",
    "references/authorized_samples/creator-frame.png": b"private reference under references",
    "runtime/cache/other.json": b"disposable runtime cache",
    "assets/models/unregistered.glb": b"unregistered geometry",
    "settings.toml": b"credential placeholder not for export",
    "index.sqlite3": b"SQLite format 3\x00 disposable index",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_files(root: ProjectRoot) -> dict[str, bytes]:
    relative_paths = (
        path.relative_to(root.path).as_posix() for path in root.path.rglob("*") if path.is_file()
    )
    return {path: root.read_bytes(path) for path in relative_paths}


def _legacy_json(root: ProjectRoot, relative: str, payload: dict) -> None:
    root.write_text(relative, json.dumps({**payload, "schema_version": 1}, indent=4) + "\n")


def _offline_media(root: ProjectRoot, relative: str, *, audio: bool) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or shutil.which("ffprobe") is None:
        pytest.skip("AT-09 media integration needs FFmpeg and FFprobe")
    argv = [
        ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=c=0x56869a:size=64x48:rate=12:duration=1",
    ]
    if audio:
        argv += ["-f", "lavfi", "-i", "sine=frequency=600:sample_rate=48000:duration=1"]
    argv += ["-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p"]
    if audio:
        argv += ["-c:a", "aac"]

    def encode(path: Path) -> None:
        subprocess.run([*argv, str(path)], check=True, capture_output=True, timeout=30)

    root.write_with(relative, encode)
    return root.read_bytes(relative)


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00" * 96_000)
    return buffer.getvalue()


@dataclass
class _Fixture:
    root: ProjectRoot
    settings: Settings
    store: ProjectStore
    cut: EpisodeOutput
    episode_token: int

    @property
    def body(self) -> dict[str, object]:
        return {
            "episode_id": _EPISODE,
            "cut_output_id": self.cut.output_id,
            "expected_version": self.episode_token,
        }

    def export(self, **edits: object):
        app = create_app(settings=self.settings, token=_TOKEN)
        try:
            with TestClient(app, base_url="http://127.0.0.1:8765") as client:
                return client.post(
                    f"/projects/{_PROJECT}/export",
                    headers={"X-Loopback-Token": _TOKEN},
                    json=self.body | edits,
                )
        finally:
            app.state.context.job_runner.close()


def _setup(tmp_path: Path, cut_bytes: bytes, *, intended_audio: bool = True) -> _Fixture:
    tmp = ProjectRoot(tmp_path)
    root = ProjectRoot(tmp.make_dir(_PROJECT))
    store = ProjectStore(root)
    store.save(Project(project_id=_PROJECT, title="AT-09 portable episode"), expected_version=0)
    _legacy_json(root, "project.json", json.loads(root.read_text("project.json")))
    store.save(Episode(
        episode_id=_EPISODE, project_id=_PROJECT, number=1, duration_target_sec=1,
        shot_ids=list(_SHOTS),
    ), expected_version=0)
    store.create(CameraProfile(camera_id="room-rig", profile_name="Room rig"))
    location = Location(location_id="room")
    store.create(location)
    store.revise(location.model_copy(update={**location.next_version(), "scene_look": "low_key"}),
                 expected_version=1)
    store.create(StyleBible(project_id=_PROJECT))
    store.create(Script(script_id="script", episode_id=_EPISODE, logline="Two room angles"))

    for name, data in _SECRET_FILES.items():
        root.write_bytes(name, data)
    store.save(Asset(
        asset_id="private-upload", type="reference", generated=False,
        license="private creator reference only", file_path=_REFERENCE_ASSET,
        checksum=_digest(_SECRET_FILES[_REFERENCE_ASSET]),
    ), expected_version=0)

    if intended_audio:
        stem = _wav()
        root.write_bytes(_STEM, stem)
        store.save(Asset(
            asset_id="fixture-voice", type="voice", generated=True, license="fixture-original",
            file_path=_STEM, checksum=_digest(stem), voice_consent="synthetic",
            reuse_scope="project",
        ), expected_version=0)

    for index, shot_id in enumerate(_SHOTS):
        shot = Shot(
            shot_id=shot_id, episode_id=_EPISODE, location_id="room", camera_id="room-rig",
            camera_profile="room-rig", duration_sec=0.5,
            audio_refs=[_STEM] if index == 0 and intended_audio else [],
        )
        store.create(shot)
        _approve_fixture_shot(store, shot)
        version_path = f"episodes/{_EPISODE}/shots/{shot_id}/v0001.json"
        _legacy_json(root, version_path, json.loads(root.read_text(version_path)))

        # One legacy worker run per Shot, with no GenerationRun available to name its sources.
        producer = f"renders/{shot_id}/v0001/run-legacy/attempt-0001"
        master = f"{producer}/master.mkv"
        root.write_bytes(master, cut_bytes)
        root.write_bytes(f"{producer}/scene.blend", f"BLENDER-v300 scene for {shot_id}".encode())
        root.write_text(f"{producer}/worker.py", f"# Original worker source for {shot_id}\n")
        store.append_shot_output(ShotOutput(
            output_id=f"master-{shot_id}", episode_id=_EPISODE, shot_id=shot_id,
            record_version=1, kind=ShotOutputKind.MASTER, relative_path=master,
            content_sha256=_digest(cut_bytes), engine_id="BLENDER_EEVEE",
            engine_build="fixture-build", camera_profile_id="room-rig", camera_profile_version=1,
        ), expected_version=0)

    timeline = Timeline(
        episode_id=_EPISODE,
        entries=[TimelineEntry(type="shot", ref=shot_id, start_sec=0.5 * index, duration_sec=0.5)
                 for index, shot_id in enumerate(_SHOTS)],
        audio_tracks=[_STEM] if intended_audio else [],
    )
    legacy = timeline.model_dump(mode="json")
    legacy["schema_version"] = 1
    for entry in legacy["entries"]:
        entry["schema_version"] = 1
    legacy_digest = _digest(json.dumps(
        legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode())
    timeline_token = 3
    _legacy_json(root, _TIMELINE, {**legacy, "_store_version": timeline_token})
    assert store.load(Timeline, _EPISODE).content_sha256 == legacy_digest

    root.write_bytes(_CUT, cut_bytes)
    cut = EpisodeOutput(
        output_id="approved-cut", episode_id=_EPISODE, relative_path=_CUT,
        timeline_store_version=timeline_token, timeline_sha256=legacy_digest,
        content_sha256=_digest(cut_bytes), delivery_profile="dvr-2005", output_fps=12,
        storage_resolution=(64, 48), sample_aspect_ratio="1/1", ffmpeg_identity="fixture",
    )
    store.append_episode_output(cut, expected_version=0)
    _legacy_json(root, _CUT_LEDGER, json.loads(root.read_text(_CUT_LEDGER)))
    store.save(Review(
        review_id="gate-c", target_id=_EPISODE, target_version=timeline_token,
        target_kind="episode_cut", episode_id=_EPISODE, actor="creator",
        created_at=datetime(2026, 11, 16, tzinfo=UTC), approval_state=ShotStatus.APPROVED,
    ), expected_version=0)
    approve_episode(store, _EPISODE, cut_output_id=cut.output_id,
                    review_id="gate-c", expected_version=1)
    _, token = store.load_with_version(Episode, _EPISODE)
    return _Fixture(root, Settings(project_root=tmp_path), store, cut, token)


def _probe_delivery(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, timeout=30,
    )
    media = json.loads(result.stdout)
    streams = {stream["codec_type"]: stream for stream in media["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert streams["audio"]["codec_name"] == "aac"
    assert (streams["video"]["width"], streams["video"]["height"]) == (64, 48)
    assert Fraction(streams["video"]["avg_frame_rate"]) == 12
    assert float(media["format"]["duration"]) == pytest.approx(1.0, abs=0.1)
    return streams


def _assert_manifest_covers_package(manifest: bytes, files: dict[str, bytes]) -> None:
    def entries(node, name: str | None = None):
        if isinstance(node, dict):
            yield name, node
            for key, value in node.items():
                yield from entries(value, key)
        elif isinstance(node, list):
            for value in node:
                yield from entries(value)

    records = tuple(entries(json.loads(manifest)))
    for relative, data in files.items():
        digest = _digest(data)
        paths = (relative, f"project/{relative}")
        assert any(
            (name in paths or any(path in record.values() for path in paths))
            and digest in record.values() and len(data) in record.values()
            for name, record in records
        ), f"manifest lacks a path, SHA-256, and size record for {relative}"


def test_api_exports_a_project_that_reopens_after_relocation(tmp_path: Path) -> None:
    tmp = ProjectRoot(tmp_path)
    cut = _offline_media(tmp, "mixed-cut.mp4", audio=True)
    fixture = _setup(tmp_path, cut)
    source_files = _read_files(fixture.root)

    response = fixture.export()
    assert response.status_code == 200, response.text
    payload = response.json()
    bundle = f"exports/{payload['export_id']}"
    assert payload == {
        "export_id": payload["export_id"],
        "package_path": f"{bundle}/project",
        "output_path": f"{bundle}/project/delivery.mp4",
        "manifest_path": f"{bundle}/manifest.json",
        "receipt_path": f"{bundle}/export.json",
    }
    receipt = fixture.store.load(Export, payload["export_id"])
    manifest = fixture.root.read_bytes(payload["manifest_path"])
    assert receipt.output_path == payload["output_path"]
    assert receipt.manifest_checksum == _digest(manifest)
    assert json.loads(fixture.root.read_text(payload["receipt_path"]))["schema_version"] == 2
    assert all(fixture.root.read_bytes(name) == data for name, data in source_files.items())

    # Rename both directories to break dependence on source checkout path and on its index.
    exported = fixture.root.resolve(bundle)
    tmp.write_with("relocated-bundle", lambda destination: exported.rename(destination))
    tmp.write_with("offline-original", lambda destination: fixture.root.path.rename(destination))
    assert not fixture.root.path.exists()
    moved = ProjectRoot(tmp.resolve("relocated-bundle/project"))
    reopened = ProjectStore(moved)
    package_files = _read_files(moved)

    assert package_files["project.json"] == source_files["project.json"]
    assert json.loads(package_files["project.json"])["schema_version"] == 1
    assert reopened.load(Project).schema_version == 2
    assert package_files[_TIMELINE] == source_files[_TIMELINE]
    assert package_files[_CUT_LEDGER] == source_files[_CUT_LEDGER]
    migrated, token = reopened.load_with_version(Timeline, _EPISODE)
    assert migrated.schema_version == 2
    assert [entry.ref for entry in migrated.entries] == list(_SHOTS)
    assert token == fixture.cut.timeline_store_version
    assert migrated.content_sha256 == fixture.cut.timeline_sha256
    retained_cut, = reopened.list_episode_outputs(_EPISODE)
    assert retained_cut.schema_version == 2
    assert retained_cut.timeline_sha256 == migrated.content_sha256
    assert retained_cut.content_sha256 == _digest(package_files[_CUT])
    episode, episode_token = reopened.load_with_version(Episode, _EPISODE)
    assert episode_token == fixture.episode_token
    assert (episode.approval_review_id == "gate-c"
            and episode.approved_cut_version == token
            and episode.approved_cut_output_id == fixture.cut.output_id)
    assert reopened.load(Review, "gate-c").target_version == token
    assert reopened.versions(Location, "room") == [1, 2]
    assert reopened.load(Script, "script", episode_id=_EPISODE).logline == "Two room angles"
    assert package_files[_SCRIPT] == source_files[_SCRIPT]

    for shot_id in _SHOTS:
        approved = reopened.load(Shot, shot_id, episode_id=_EPISODE, version=1)
        assert approved.status == ShotStatus.APPROVED
        assert approved.approval_review_id == f"review-{shot_id}"
        master, = reopened.list_shot_outputs(shot_id, episode_id=_EPISODE)
        assert master.content_sha256 == _digest(package_files[master.relative_path])
        attempt = Path(master.relative_path).parent.as_posix()
        for name in ("scene.blend", "worker.py"):
            relative = f"{attempt}/{name}"
            assert package_files[relative] == source_files[relative]

    stem = reopened.load(Asset, "fixture-voice")
    assert stem.voice_consent == "synthetic" and stem.license == "fixture-original"
    assert stem.checksum == _digest(package_files[_STEM])
    assert reopened.load(Asset, "private-upload").file_path == _REFERENCE_ASSET
    assert set(package_files).isdisjoint(_SECRET_FILES)
    assert not set(package_files.values()).intersection(_SECRET_FILES.values())
    assert package_files["references/style_bible/v0001.json"] == source_files[
        "references/style_bible/v0001.json"
    ]
    _assert_manifest_covers_package(manifest, package_files)
    _probe_delivery(moved.resolve("delivery.mp4"))


@pytest.mark.parametrize("failure", ["token", "cut-id", "cut-bytes", "timeline-hash"])
def test_export_rejects_inconsistent_requests_without_a_bundle(tmp_path: Path, failure: str) -> None:
    fixture = _setup(tmp_path, b"registered local cut fixture")
    edits: dict[str, object] = {}
    if failure == "token":
        edits["expected_version"] = fixture.episode_token - 1
    elif failure == "cut-id":
        edits["cut_output_id"] = "missing-cut"
    elif failure == "cut-bytes":
        fixture.root.write_bytes(_CUT, b"cut bytes changed after approval")
    elif failure == "timeline-hash":
        _legacy_json(fixture.root, _CUT_LEDGER, {
            **json.loads(fixture.root.read_text(_CUT_LEDGER)),
            "timeline_sha256": "a" * 64,
        })
    before = _read_files(fixture.root)
    response = fixture.export(**edits)
    if failure == "token":
        assert response.status_code == 409, response.text
    else:
        assert response.status_code in {400, 404, 409, 422}, response.text
        assert "missing_adapter" not in response.text
    assert _read_files(fixture.root) == before


def test_video_only_cut_cannot_drop_declared_timeline_audio(tmp_path: Path) -> None:
    cut = _offline_media(ProjectRoot(tmp_path), "video-only.mp4", audio=False)
    fixture = _setup(tmp_path, cut, intended_audio=True)
    before = _read_files(fixture.root)
    response = fixture.export()
    assert response.status_code in {400, 409, 422}, response.text
    assert "audio" in response.text.lower()
    assert _read_files(fixture.root) == before


def test_cli_exports_a_silent_cut_with_an_explicit_aac_track(tmp_path: Path, capsys) -> None:
    cut = _offline_media(ProjectRoot(tmp_path), "video-only.mp4", audio=False)
    fixture = _setup(tmp_path, cut, intended_audio=False)
    exit_code = cli_main(
        ["export", "--project-id", _PROJECT, "--data", json.dumps(fixture.body), "--json"],
        settings=fixture.settings,
    )
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    payload = json.loads(captured.out)
    assert _probe_delivery(fixture.root.resolve(payload["output_path"]))["audio"]["duration"]
    receipt = fixture.root.read_text(payload["receipt_path"])
    assert "silent" in receipt.lower() or "silence" in receipt.lower()


@pytest.mark.parametrize("unknown", [0, 3])
def test_store_refuses_schema_versions_without_a_migration(tmp_path: Path, unknown: int) -> None:
    root = ProjectRoot(tmp_path)
    root.write_text("project.json", json.dumps({
        "schema_version": unknown, "project_id": _PROJECT, "title": "unsupported",
    }))
    with pytest.raises(JobError) as raised:
        ProjectStore(root).load(Project)
    assert SCHEMA_VERSION == 2
    assert raised.value.kind == JobErrorKind.SCHEMA_VERSION_MISMATCH
