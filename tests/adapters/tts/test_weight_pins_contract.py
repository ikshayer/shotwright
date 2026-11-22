"""IndexTTS-2 weight pins, written from orchestrator/adapters/tts/WEIGHT_PINS_CONTRACT.md.

Never hashes the real checkpoint tree: every behavioural test builds a tiny checkpoint tree
under tmp_path and injects a pin table for it into a real IndexTTS2Adapter that drives a
fake worker process.

Assumed production seams (the contract names none of them):
  * ``orchestrator.adapters.tts.core.INDEXTTS2_WEIGHT_PINS``: the pin table, a sequence (or
    mapping of path -> pin) of objects with ``path``, ``size``, ``sha256``, ``source``.
    Behavioural tests replace it in ``core`` and in ``indextts2`` (wherever it was imported),
    so the adapter must read the table at verification time, not freeze it at import.
  * ``IndexTTS2Adapter(root, settings, popen=..., repository=...)`` is unchanged.
  * Verification failures surface on the job as ``error_kind`` / ``error_message``.
"""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.domain.entities import Asset, Episode, GenerationRun, Project, Shot
from orchestrator.platform.errors import JobErrorKind
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from tests.adapters.tts.test_indextts2_contract import (
    INDEX_PATH,
    INDEX_RESERVATIONS,
    FakeIndexProcess,
    _wav_bytes,
)
from tests.api.conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER
from tests.service.conftest import Environment

PINS_NAME = "INDEXTTS2_WEIGHT_PINS"
MISSING = JobErrorKind.MISSING_MODEL_ARTIFACT.value

# --------------------------------------------------------------------------------------
# R1 literals, copied from the contract's table.
# --------------------------------------------------------------------------------------

IT2 = "IndexTeam/IndexTTS-2@740dcaff396282ffb241903d150ac011cd4b1ede"
PUBLISHED = (
    ("gpt.pth", 3484663079, "baaaeb8b56328da81731dc540a85a7dee32eca9da28f174b05757cb651c602a4", IT2),
    ("s2mel.pth", 1202198223, "aae1bb12017cbb47e7a5ce537fc82f40b6b1deb71acdb9b8f25686f32714b636", IT2),
    ("bpe.model", 475997, "b2a5ce8090d32da3642cc4f81fdc996376bc6dd3f4cd5e3d165f71120d9f2bc8", IT2),
    ("feat1.pt", 57170, "f219cb447d80216ba615666da2ff8d63ac544eee26657f3a7b278692bf7a67c4", IT2),
    ("feat2.pt", 374866, "9c4292e96dee535aea9a6206e9a0c856dd578dde9212acdb16dd3ada4d12bf80", IT2),
    ("wav2vec2bert_stats.pt", 9343,
     "c9c176c2b8850ab2e3ba828bbfa969deaf4566ce55db5f2687b8430b87526ad2", IT2),
    ("qwen0.6bemo4-merge/model.safetensors", 1192135096,
     "11293257a8df593c154a8ecd5fc039f3076de35411e35f06d41b471e136f6641", IT2),
    ("qwen0.6bemo4-merge/tokenizer.json", 11422654,
     "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4", IT2),
    ("hf_cache/w2v-bert-2.0/model.safetensors", 2322063736,
     "eb890c9660ed6e3414b6812e27257b8ce5454365d5490d3ad581ea60b93be043",
     "facebook/w2v-bert-2.0@da985ba0987f70aaeb84a80f2851cfac8c697a7b"),
    ("hf_cache/semantic_codec_model.safetensors", 177183712,
     "ec947271175d8cad75ec37e83aa487e27c97a0f72a303393772da5ffa84bddf2",
     "amphion/MaskGCT@265c6cef07625665d0c28d2faafb1415562379dc:semantic_codec/model.safetensors"),
    ("hf_cache/campplus_cn_common.bin", 28036335,
     "3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8",
     "funasr/campplus@e4b6ede7ce16997aff4ae69fbca1f0175e2afede"),
    ("hf_cache/bigvgan/bigvgan_generator.pt", 449228171,
     "e95ba25972d3de0628d99cd156e9315a9c018899bf739988959ebe3544080ced",
     "nvidia/bigvgan_v2_22khz_80band_256x@633ff708ed5b74903e86ff1298cf4a98e921c513"),
)


def _field(pin: Any, name: str) -> Any:
    return pin[name] if isinstance(pin, Mapping) else getattr(pin, name)


def _pin_list(table: Any) -> list[Any]:
    return list(table.values()) if isinstance(table, Mapping) else list(table)


def _core():
    import orchestrator.adapters.tts.core as core

    assert hasattr(core, PINS_NAME), f"core.{PINS_NAME} (the pin table) is missing"
    return core


def _digest_variants(pins: list[Any]) -> set[str]:
    """SHA-256 of the sorted ``path:sha256`` lines, computed here, independently.

    The contract does not say whether the final line ends in a newline: accept both.
    """
    lines = sorted(f"{_field(p, 'path')}:{_field(p, 'sha256')}" for p in pins)
    joined = "\n".join(lines)
    return {
        hashlib.sha256(joined.encode()).hexdigest(),
        hashlib.sha256((joined + "\n").encode()).hexdigest(),
    }


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------------------
# R1: the real table.
# --------------------------------------------------------------------------------------


def test_r1_real_table_has_exactly_the_published_rows() -> None:
    core = _core()
    pins = {str(_field(p, "path")): p for p in _pin_list(getattr(core, PINS_NAME))}
    for path, size, sha256, source in PUBLISHED:
        assert path in pins, f"{path} is not pinned"
        pin = pins[path]
        assert _field(pin, "size") == size, path
        assert _field(pin, "sha256") == sha256, path
        assert source in _field(pin, "source"), path
    assert len(pins) == len(_pin_list(getattr(core, PINS_NAME))), "duplicate pin paths"


def test_r1_real_table_pins_config_and_excludes_unloaded_files() -> None:
    core = _core()
    pins = {str(_field(p, "path")): p for p in _pin_list(getattr(core, PINS_NAME))}
    assert "config.yaml" in pins
    assert _field(pins["config.yaml"], "sha256") == core.INDEXTTS2_CONFIG_SHA256
    assert not any("conformer_shaw" in path for path in pins)
    assert "hf_cache/w2v-bert-2.0/conformer_shaw.pt" not in pins


def test_r1_every_entry_is_well_formed_and_has_a_source() -> None:
    for pin in _pin_list(getattr(_core(), PINS_NAME)):
        path = _field(pin, "path")
        assert isinstance(path, str) and path and not path.startswith(("/", "checkpoints"))
        assert "\\" not in path and ".." not in Path(path).parts
        assert isinstance(_field(pin, "size"), int) and _field(pin, "size") > 0
        sha = _field(pin, "sha256")
        assert isinstance(sha, str) and len(sha) == 64 and sha == sha.lower()
        int(sha, 16)
        source = _field(pin, "source")
        assert isinstance(source, str) and source.strip(), f"{path} has no source"


# --------------------------------------------------------------------------------------
# Rig: a real adapter over a temporary checkpoint tree and a fake worker.
# --------------------------------------------------------------------------------------

CHECKPOINTS = "spikes/05_voice/_work/index-tts/checkpoints"
PYTHON = "spikes/05_voice/_work/index-tts/.venv/Scripts/python.exe"
BIG = 5 * 1024 * 1024

BASE_FILES: dict[str, bytes] = {
    "config.yaml": b"gpt: {}\n",
    "small.bin": b"small weights " * 40,
    "sub/dir/model.safetensors": b"\x01\x02\x03 model " * 300,
    "hf_cache/extra.bin": b"aux " * 500,
}
TARGETS = ("sub/dir/model.safetensors", "hf_cache/extra.bin")


class _Counting:
    """Proxy for a pinned file's read handle that counts the bytes read through it."""

    def __init__(self, stream: Any, rig: _Rig) -> None:
        self._stream, self._rig = stream, rig

    def read(self, *args: Any) -> bytes:
        data = self._stream.read(*args)
        self._rig.bytes_read += len(data)
        return data

    def read1(self, *args: Any) -> bytes:
        data = self._stream.read1(*args)
        self._rig.bytes_read += len(data)
        return data

    def readinto(self, buffer: Any) -> int | None:
        count = self._stream.readinto(buffer)
        self._rig.bytes_read += count or 0
        return count

    def __enter__(self) -> _Counting:
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stream.close()

    def __iter__(self):
        return iter(self._stream)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _Rig:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.bytes_read = 0
        self.cancel_after_first_read = False
        self.checkpoints: Path
        self.files: dict[str, bytes]
        self.pins: list[Any]
        self.client: TestClient
        self.env: Environment
        self.calls: list[FakeIndexProcess] = []

    # -- observation ---------------------------------------------------------------------
    def opens(self, *, since: int = 0) -> list[str]:
        return [name for kind, name in self.events[since:] if kind == "open"]

    def launches(self) -> int:
        return len(self.calls)

    # -- jobs ----------------------------------------------------------------------------
    def run_job(self) -> Any:
        _shot, version = self.env.store.load_with_version(Shot, "s1", episode_id="ep1")
        response = self.client.post(INDEX_PATH, json={
            "expected_version": version, "text": "Weights must be the pinned ones.",
            "speaker_asset_id": "speaker", "emotion": {"mode": "none"},
        }, headers={TOKEN_HEADER: TOKEN})
        assert response.status_code == 202, response.text
        return self.env.runner.wait("p1", response.json()["job_id"])

    def tool_version(self, job: Any) -> str:
        return self.env.store.load(GenerationRun, job.run_id).tool_version

    def report(self, job: Any) -> dict[str, Any]:
        return json.loads(self.env.root.read_text(
            f"generated/tts/s1/v0001/{job.run_id}/report.json", encoding="utf-8"))

    # -- tampering -----------------------------------------------------------------------
    def path(self, rel: str) -> Path:
        return self.checkpoints / rel

    def pin_for(self, rel: str) -> Any:
        return next(p for p in self.pins if _field(p, "path") == rel)


def _write_tree(checkpoints: Path, files: Mapping[str, bytes]) -> None:
    for rel, data in files.items():
        target = checkpoints / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


@pytest.fixture
def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stack = contextlib.ExitStack()
    counter = [0]

    def build(files: Mapping[str, bytes] | None = None, *,
              pin_files: Mapping[str, bytes] | None = None) -> _Rig:
        """Build a rig. `files` is written to disk; `pin_files` (default: same) is what is pinned."""
        counter[0] += 1
        base = tmp_path / f"rig{counter[0]}"
        files = dict(BASE_FILES if files is None else files)
        pinned = dict(files if pin_files is None else pin_files)
        core = _core()
        index_module = pytest.importorskip("orchestrator.adapters.tts.indextts2")

        real_dir = Path(__file__).resolve().parents[3] / "spikes/05_voice/_work/index-tts"
        if not (real_dir / ".venv/Scripts/python.exe").is_file() or not (
                real_dir / "checkpoints/config.yaml").is_file():
            pytest.skip("the pinned IndexTTS-2 interpreter is not installed; no model will be started")

        rig = _Rig()
        repo = base / "repo"
        rig.checkpoints = repo / CHECKPOINTS
        rig.files = files
        (repo / PYTHON).parent.mkdir(parents=True)
        (repo / PYTHON).write_bytes(b"")
        _write_tree(rig.checkpoints, files)

        proto = _pin_list(getattr(core, PINS_NAME))[0]
        make_pin: Callable[..., Any] = dict if isinstance(proto, Mapping) else type(proto)
        rig.pins = [
            make_pin(path=rel, size=len(data), sha256=_sha(data), source=f"test/fixture@0:{rel}")
            for rel, data in pinned.items()
        ]
        table: Any = ({_field(p, "path"): p for p in rig.pins}
                      if isinstance(getattr(core, PINS_NAME), Mapping) else tuple(rig.pins))
        for module in (core, index_module):
            monkeypatch.setattr(module, PINS_NAME, table, raising=False)
        # The pre-existing runtime checks are not under test: satisfy them for the tree.
        config = pinned.get("config.yaml", files.get("config.yaml", b""))
        monkeypatch.setattr(index_module, "INDEXTTS2_CONFIG_SHA256", _sha(config), raising=False)
        monkeypatch.setattr(index_module, "INDEXTTS2_AUXILIARY_FILES", (), raising=False)
        monkeypatch.setattr(index_module, "_git_head", lambda _repo: core.INDEXTTS2_COMMIT)

        # Observe every read-open of a pinned file, and count the bytes read through it.
        checkpoints_abs = os.path.abspath(rig.checkpoints)
        pinned_paths = set(pinned) | set(files)

        def pinned_rel(candidate: Any, mode: str) -> str | None:
            if not isinstance(candidate, (str, os.PathLike)) or "r" not in mode or any(
                    flag in mode for flag in "wax+"):
                return None
            rel = os.path.relpath(os.path.abspath(candidate), checkpoints_abs).replace("\\", "/")
            return rel if rel in pinned_paths else None

        real_path_open, real_open = Path.open, builtins.open

        def spy_path_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any):
            stream = real_path_open(self, mode, *args, **kwargs)
            rel = pinned_rel(self, mode)
            if rel is None:
                return stream
            rig.events.append(("open", rel))
            return _Counting(stream, rig)

        def spy_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
            stream = real_open(file, mode, *args, **kwargs)
            rel = pinned_rel(file, mode)
            if rel is None:
                return stream
            rig.events.append(("open", rel))
            return _Counting(stream, rig)

        monkeypatch.setattr(Path, "open", spy_path_open)
        monkeypatch.setattr(builtins, "open", spy_open)

        # Project, shot and consented speaker.
        parent = base / "projects"
        project_path = parent / "p1"
        project_path.mkdir(parents=True)
        env = Environment(project_path)
        env.settings.indextts2.enabled = True
        for key, value in INDEX_RESERVATIONS.items():
            setattr(env.settings.indextts2, key, value)
        env.store.save(Project(project_id="p1", title="weight pins"), expected_version=0)
        env.store.save(Episode(episode_id="ep1", project_id="p1", number=1,
                               duration_target_sec=30), expected_version=0)
        env.store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))
        wav = _wav_bytes()
        env.root.write_bytes("assets/audio/speaker/source.wav", wav)
        env.store.save(Asset.model_validate({
            "asset_id": "speaker", "type": "voice", "generated": False,
            "license": "creator supplied reference",
            "file_path": "assets/audio/speaker/source.wav", "checksum": _sha(wav),
            "voice_consent": "creator_own_voice",
            "voice_consent_basis": "Creator recorded and approved this reference.",
        }), expected_version=0)

        def popen(argv: Any, **kwargs: Any) -> FakeIndexProcess:
            rig.events.append(("launch", ""))
            process = FakeIndexProcess(argv, project_path=project_path, **kwargs)
            rig.calls.append(process)
            return process

        backend = index_module.IndexTTS2Adapter(
            env.root, env.settings, popen=popen, repository=ProjectRoot(repo))
        runner_cancel: list[Callable[[], bool] | None] = [None]

        def combined_cancel() -> bool:
            if runner_cancel[0] is not None and runner_cancel[0]():
                return True
            return rig.cancel_after_first_read and rig.bytes_read > 0

        env.adapter.set_cancel_check = lambda check: runner_cancel.__setitem__(0, check)
        env.adapter.invoke = lambda request, job, *, queue: backend.invoke(
            request, job, queue=queue, cancel=combined_cancel)
        app = create_app(
            settings=Settings.model_validate({
                "project_root": parent, "indextts2": {"enabled": True, **INDEX_RESERVATIONS}}),
            token=TOKEN, job_runner=env.runner)
        rig.client = stack.enter_context(
            TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False))
        stack.callback(env.runner.close)
        rig.env = env
        return rig

    yield build
    stack.close()


def _first_launch_index(rig: _Rig) -> int:
    return next(i for i, (kind, _name) in enumerate(rig.events) if kind == "launch")


# --------------------------------------------------------------------------------------
# R2: verification.
# --------------------------------------------------------------------------------------


def test_r2_valid_tree_passes_and_hashes_every_pinned_file_before_launch(make_rig) -> None:
    rig = make_rig()
    job = rig.run_job()

    assert job.state == "completed", job.error_message
    assert rig.launches() == 1
    launch = _first_launch_index(rig)
    opened_before ={name for kind, name in rig.events[:launch] if kind == "open"}
    assert set(BASE_FILES) - {"config.yaml"} <= opened_before, (
        "every pinned file must be read (hashed) before the worker is launched")
    assert not any(kind == "open" for kind, _ in rig.events[launch:]), (
        "no pinned file may be read after the worker launches")


def _tamper_missing(rig: _Rig, rel: str) -> None:
    rig.path(rel).unlink()


def _tamper_directory(rig: _Rig, rel: str) -> None:
    rig.path(rel).unlink()
    rig.path(rel).mkdir()


def _tamper_wrong_size(rig: _Rig, rel: str) -> None:
    rig.path(rel).write_bytes(rig.files[rel] + b"extra")


def _tamper_wrong_bytes(rig: _Rig, rel: str) -> None:
    data = bytearray(rig.files[rel])
    data[0] ^= 0xFF
    rig.path(rel).write_bytes(bytes(data))


def _tamper_symlink(rig: _Rig, rel: str) -> None:
    elsewhere = rig.checkpoints.parent / "elsewhere.bin"
    elsewhere.write_bytes(rig.files[rel])  # identical bytes: only the link itself is wrong
    rig.path(rel).unlink()
    try:
        os.symlink(elsewhere, rig.path(rel))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink on this platform: {exc}")


@pytest.mark.parametrize("rel", TARGETS)
@pytest.mark.parametrize("tamper", [
    _tamper_missing, _tamper_directory, _tamper_symlink,
], ids=["missing", "directory", "symlink"])
def test_r2_structural_failures_refuse_before_launch_and_name_the_file(
    make_rig, tamper, rel: str,
) -> None:
    rig = make_rig()
    tamper(rig, rel)
    job = rig.run_job()

    assert job.state == "failed"
    assert job.error_kind == MISSING
    assert rel in (job.error_message or "").replace("\\", "/")
    assert rig.launches() == 0


@pytest.mark.parametrize("rel", TARGETS)
def test_r2_wrong_size_fails_with_both_sizes_and_never_reads_the_file(make_rig, rel: str) -> None:
    rig = make_rig()
    _tamper_wrong_size(rig, rel)
    job = rig.run_job()

    message = (job.error_message or "").replace("\\", "/")
    assert job.state == "failed" and job.error_kind == MISSING
    assert rel in message
    assert str(len(rig.files[rel])) in message, "expected size missing from the message"
    assert str(len(rig.files[rel]) + 5) in message, "observed size missing from the message"
    assert rig.launches() == 0
    assert rel not in rig.opens(), "a wrong-size file must be rejected without reading its contents"


@pytest.mark.parametrize("rel", TARGETS)
def test_r2_right_size_wrong_bytes_fails_with_both_hashes(make_rig, rel: str) -> None:
    rig = make_rig()
    _tamper_wrong_bytes(rig, rel)
    observed = _sha(rig.path(rel).read_bytes())
    job = rig.run_job()

    message = (job.error_message or "").replace("\\", "/")
    assert job.state == "failed" and job.error_kind == MISSING
    assert rel in message
    assert _field(rig.pin_for(rel), "sha256") in message, "expected hash missing from the message"
    assert observed in message, "observed hash missing from the message"
    assert rig.launches() == 0


def test_r2_cancel_during_hashing_stops_promptly_without_launching(make_rig) -> None:
    files = {**BASE_FILES, "big.pth": b"B" * BIG}
    rig = make_rig(files)
    rig.cancel_after_first_read = True
    job = rig.run_job()

    assert job.state in {"cancelled", "failed"}
    assert job.error_kind == JobErrorKind.CANCELLED.value, (job.error_kind, job.error_message)
    assert rig.launches() == 0
    assert 0 < rig.bytes_read < sum(len(data) for data in files.values()), (
        f"hashing must stop at a chunk boundary after cancel; read {rig.bytes_read} bytes")


def test_r2_cancel_is_not_cached_as_success(make_rig) -> None:
    """A cancelled verification must not make a later job skip hashing."""
    files = {**BASE_FILES, "big.pth": b"B" * BIG}
    rig = make_rig(files)
    rig.cancel_after_first_read = True
    assert rig.run_job().error_kind == JobErrorKind.CANCELLED.value
    rig.cancel_after_first_read = False
    mark = len(rig.events)
    job = rig.run_job()

    assert job.state == "completed", job.error_message
    assert "big.pth" in rig.opens(since=mark)


def test_r2_second_job_in_the_same_process_does_not_rehash_unchanged_files(make_rig) -> None:
    rig = make_rig()
    assert rig.run_job().state == "completed"
    mark = len(rig.events)
    second = rig.run_job()

    assert second.state == "completed", second.error_message
    reread = set(rig.opens(since=mark)) - {"config.yaml"}  # config.yaml has a legacy check too
    assert reread == set(), f"unchanged files were re-hashed: {sorted(reread)}"
    assert rig.launches() == 2


def test_r2_mtime_change_alone_triggers_exactly_that_files_rehash(make_rig) -> None:
    rig = make_rig()
    assert rig.run_job().state == "completed"
    rel = "sub/dir/model.safetensors"
    stat = rig.path(rel).stat()
    os.utime(rig.path(rel), ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    mark = len(rig.events)
    job = rig.run_job()

    assert job.state == "completed", job.error_message
    assert set(rig.opens(since=mark)) - {"config.yaml"} == {rel}


def test_r2_changed_bytes_with_new_mtime_are_caught_after_a_cached_success(make_rig) -> None:
    rig = make_rig()
    assert rig.run_job().state == "completed"
    rel = "hf_cache/extra.bin"
    stat = rig.path(rel).stat()
    _tamper_wrong_bytes(rig, rel)
    os.utime(rig.path(rel), ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    mark = len(rig.events)
    job = rig.run_job()

    assert job.state == "failed" and job.error_kind == MISSING
    assert rel in rig.opens(since=mark), "the changed file must be re-hashed"
    assert rig.launches() == 1, "no second launch after the tampering"


def test_r2_size_change_after_a_cached_success_fails_on_size(make_rig) -> None:
    rig = make_rig()
    assert rig.run_job().state == "completed"
    rel = "hf_cache/extra.bin"
    _tamper_wrong_size(rig, rel)
    mark = len(rig.events)
    job = rig.run_job()

    assert job.state == "failed" and job.error_kind == MISSING
    assert rel not in rig.opens(since=mark)
    assert rig.launches() == 1


def test_r2_cache_is_memory_only_and_writes_nothing_to_disk(make_rig) -> None:
    rig = make_rig()
    repo = rig.checkpoints.parents[4]

    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            str(path.relative_to(repo)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in repo.rglob("*") if path.is_file()
        }

    before = snapshot()
    assert rig.run_job().state == "completed"
    assert rig.run_job().state == "completed"

    assert snapshot() == before, "verification must not write (cache or otherwise) into the runtime tree"


# --------------------------------------------------------------------------------------
# R3: identity.
# --------------------------------------------------------------------------------------


def test_r3_tool_version_and_report_carry_the_independently_computed_digest(make_rig) -> None:
    rig = make_rig()
    job = rig.run_job()
    assert job.state == "completed", job.error_message
    expected = _digest_variants(rig.pins)

    tool_version = rig.tool_version(job)
    assert any(digest in tool_version for digest in expected), tool_version
    report = rig.report(job)
    assert any(digest in str(report["engine_version"]) for digest in expected)
    assert any(digest in json.dumps(report["runtime"]) for digest in expected)


def test_r3_changing_one_pins_sha_changes_the_digest(make_rig) -> None:
    first = make_rig()
    job_a = first.run_job()
    changed = dict(BASE_FILES)
    changed["hf_cache/extra.bin"] = b"different " * 400
    second = make_rig(changed)
    job_b = second.run_job()
    assert job_a.state == "completed" and job_b.state == "completed"

    digest_a, digest_b = _digest_variants(first.pins), _digest_variants(second.pins)
    assert digest_a.isdisjoint(digest_b), "test setup: the two tables must differ"
    tool_a, tool_b = first.tool_version(job_a), second.tool_version(job_b)
    assert any(d in tool_a for d in digest_a) and not any(d in tool_a for d in digest_b)
    assert any(d in tool_b for d in digest_b) and not any(d in tool_b for d in digest_a)
    assert tool_a != tool_b


def test_r3_digest_ignores_table_order(make_rig) -> None:
    """Sorted lines: the same pins in a different row order give the same identity."""
    rig = make_rig()
    job = rig.run_job()
    assert job.state == "completed", job.error_message
    shuffled = list(reversed(rig.pins))
    assert _digest_variants(shuffled) == _digest_variants(rig.pins)
    assert any(digest in rig.tool_version(job) for digest in _digest_variants(shuffled))


def test_r3_persisted_run_with_the_old_identity_loads_unchanged(tmp_path: Path) -> None:
    old_identity = (
        "indextts2 d9e41aac89fd00b3d71497fddb287b7f24613712 config.yaml sha256 "
        "ea9c2815ecc3874577c7ac158b97248c027250e5b05972fbbb1216b6d6539081"
    )
    env = Environment(tmp_path / "p1")
    env.store.save(Project(project_id="p1", title="legacy"), expected_version=0)
    run = GenerationRun(run_id="old-run", tool="indextts2", tool_version=old_identity,
                        invocation="legacy take", status="completed")
    env.store.save(run, expected_version=0)

    loaded = env.store.load(GenerationRun, "old-run")

    assert loaded.tool_version == old_identity
    assert loaded.model_dump(mode="json") == run.model_dump(mode="json")
    env.runner.close()
