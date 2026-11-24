"""Select and validate the portable files in one project-local JSON snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from orchestrator.adapters.blender.shared_base import _read_receipt
from orchestrator.domain.base import Entity, VersionedEntity
from orchestrator.domain.entities import (
    Asset,
    AudioPostProfile,
    CameraProfile,
    Episode,
    EpisodeOutput,
    GenerationRun,
    H3Candidate,
    Location,
    PostProfile,
    Project,
    Review,
    Script,
    Shot,
    ShotOutput,
    ShotOutputKind,
    StyleBible,
    Timeline,
)
from orchestrator.platform.containment import ContainmentError
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.dependencies import (
    DEFAULT_OVERLAY_FONT_PROJECT_LICENSE,
    DEFAULT_OVERLAY_FONT_LICENSE_PATH,
    DEFAULT_OVERLAY_FONT_SHA256,
    validated_cascades,
)
from orchestrator.store.json_store import ProjectStore

E = TypeVar("E", bound=Entity)
V = TypeVar("V", bound=VersionedEntity)

_VERSIONED = re.compile(r"v\d{4,}(?:\.lifecycle\.\d{8,})?\.json")
_LEDGER = re.compile(r"o\d{8,}-[A-Za-z0-9_-]+\.json")
_NATIVE_RENDER = re.compile(
    r"(?P<attempt>renders/[A-Za-z0-9_-]+/v\d{4,}/[A-Za-z0-9_-]+/attempt-\d{4,})/"
    r"(?:master\.(?:mkv|mp4)|scene\.blend|frames/frame_\d+\.png)"
)
_NATIVE_POST = re.compile(
    r"(post/[A-Za-z0-9_-]+/v\d{4,}/[A-Za-z0-9_-]+)/degraded\.mp4"
)
_SHARED_BLEND = re.compile(
    r"renders/[A-Za-z0-9_-]+/v\d{4,}/[A-Za-z0-9_-]+/attempt-\d{4,}/base\.blend"
)
_SHARED_RECEIPT = re.compile(r"scenes/shared/([a-f0-9]{64})\.json")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_DATABASE = re.compile(r".*\.(?:sqlite[23]?|db)(?:[-.].*)?")
_ADMIN_DIRS = frozenset({
    ".projectroot-atomic-write", "_journal", "_work", "cache", "caches", "exports",
    "index", "jobs", "quarantine", "runtime", "state", "temp", "tmp",
})


@dataclass(frozen=True)
class SnapshotFile:
    relative_path: str
    expected_sha256: str | None = None
    embedded_content: bytes | None = None


def plan_snapshot(
    store: ProjectStore, *, extra_files: Iterable[SnapshotFile] = (),
) -> tuple[SnapshotFile, ...]:
    """Read the native project closure and additional bound files under one fence."""
    snapshot = _Snapshot(store)
    try:
        snapshot.cascades()
        snapshot.collect()
        for item in extra_files:
            if item.embedded_content is None:
                snapshot.add(item.relative_path, item.expected_sha256)
            else:
                snapshot.add_content(item.relative_path, item.embedded_content)
    except (ContainmentError, OSError, ValueError) as exc:
        raise _invalid(f"cannot read portable project snapshot: {exc}", cause=exc) from exc
    return tuple(snapshot.files[path] for path in sorted(snapshot.files))


def _invalid(message: str, *, cause: BaseException | None = None) -> JobError:
    return JobError(JobErrorKind.INVALID_MANIFEST, message, cause=cause)


class _Snapshot:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store
        self.root = store._root
        self.files: dict[str, SnapshotFile] = {}
        self.casefolded: dict[str, str] = {}
        self.private: set[str] = set()
        self.framing: dict[str, set[tuple[str, str, int]]] = {}
        self.assets: dict[str, Asset] = {}

    def cascades(self) -> None:
        pending = self.store.cascades(pending_only=True)
        if pending:
            ids = ", ".join(plan["cascade_id"] for plan in pending)
            raise _invalid(
                f"cannot export with pending dependency cascades: {ids}; resume invalidations first"
            )
        for plan in validated_cascades(self.store):
            path = f"_journal/cascades/{plan['cascade_id']}"
            self.capture_json(f"{path}.json")
            self.capture_json(f"{path}.done.json")

    def collect(self) -> None:
        self.capture_json("project.json")
        project = self.store.load(Project)
        assets = self.records(Asset, "assets/registry")
        self.assets = {asset.asset_id: asset for asset in assets}
        self.private.update(
            asset.file_path.replace("\\", "/").casefold()
            for asset in assets if asset.type.casefold() == "reference" and asset.file_path
        )
        shots = []
        episode_ids = []
        stems: list[str] = []
        for directory in self.directories("episodes"):
            episode_path = f"{directory}/episode.json"
            if not self.resolve(episode_path)[1].is_file():
                continue
            episode_id = directory.rsplit("/", 1)[-1]
            self.capture_json(episode_path)
            episode = self.store.load(Episode, episode_id)
            episode_ids.append(episode_id)
            if episode.episode_id != episode_id or episode.project_id != project.project_id:
                raise _invalid(f"Episode {episode_id!r} does not match its project and folder")
            timeline_path = f"{directory}/edits/timeline.json"
            head_token = 0
            if self.resolve(timeline_path)[1].exists():
                timeline, head_token = self.store.load_with_version(Timeline, episode_id)
                if timeline.episode_id != episode_id:
                    raise _invalid(f"Timeline {timeline_path!r} does not match its Episode")
                self.capture_json(timeline_path)
                stems.extend(timeline.audio_tracks)
            for snapshot_path in self.json_paths(f"{directory}/edits/timelines"):
                name = snapshot_path.rsplit("/", 1)[-1]
                if not re.fullmatch(r"\d{6}\.json", name):
                    raise _invalid(f"unexpected file {snapshot_path!r} in Timeline history")
                token = int(name[:6])
                if not head_token:
                    raise _invalid(f"Timeline history {snapshot_path!r} exists without a Timeline head")
                if token > head_token:
                    # An orphan from a save whose head write failed: never a committed
                    # cut, so it is not part of the project's history.
                    continue
                historical = self.store.load_timeline_at(episode_id, token)
                self.capture_json(snapshot_path)
                # A historical cut's audio belongs to the package even when the current
                # Timeline no longer references it.
                stems.extend(historical.audio_tracks)
            for script in self.directories(f"{directory}/scripts"):
                self.history(Script, script, script.rsplit("/", 1)[-1], episode_id=episode_id)
            for path in self.directories(f"{directory}/shots"):
                shot_id = path.rsplit("/", 1)[-1]
                shots.extend(self.history(Shot, path, shot_id, episode_id=episode_id))

        # The current framing workflow stores generated preview paths in the same
        # field as private framing panels. Only a native preview bound to the exact
        # shot and version can establish that its source is generated here.
        shot_ids = sorted({(shot.episode_id, shot.shot_id) for shot in shots})
        self.protect_framing(shot_ids)
        invalid = sorted(path for path in self.files if path.casefold() in self.private)
        if invalid:
            raise _invalid(f"required project manifests are also private references: {invalid}")
        for asset in assets:
            if asset.type.casefold() != "reference" and asset.file_path is not None:
                self.add(asset.file_path, asset.checksum)
        for shot in shots:
            stems.extend(shot.audio_refs)
        for directory in self.directories("locations"):
            for location in self.history(Location, directory, directory.rsplit("/", 1)[-1]):
                if location.blend_path is not None:
                    self.add(location.blend_path)
        for directory in self.directories("camera_profiles"):
            self.history(CameraProfile, directory, directory.rsplit("/", 1)[-1])
        self.history(StyleBible, "references/style_bible", project.project_id)
        self.records(Review, "reviews")
        for profile in self.records(PostProfile, "post_profiles"):
            if profile.overlay is not None and profile.overlay.font_relative is not None:
                font_relative = profile.overlay.font_relative
                font_sha256 = hashlib.sha256(self.root.read_bytes(font_relative)).hexdigest()
                self.add(font_relative, font_sha256)
                if font_sha256 == DEFAULT_OVERLAY_FONT_SHA256:
                    license_bytes = DEFAULT_OVERLAY_FONT_LICENSE_PATH.read_bytes()
                    license_path = self.root.resolve(DEFAULT_OVERLAY_FONT_PROJECT_LICENSE)
                    if license_path.is_file():
                        existing_license = self.root.read_bytes(DEFAULT_OVERLAY_FONT_PROJECT_LICENSE)
                        if existing_license != license_bytes:
                            raise _invalid(
                                f"project font licence {DEFAULT_OVERLAY_FONT_PROJECT_LICENSE!r} "
                                "conflicts with the bundled DejaVu Sans Mono licence"
                            )
                        self.add(
                            DEFAULT_OVERLAY_FONT_PROJECT_LICENSE,
                            hashlib.sha256(license_bytes).hexdigest(),
                        )
                    else:
                        self.add_content(DEFAULT_OVERLAY_FONT_PROJECT_LICENSE, license_bytes)
        self.records(AudioPostProfile, "audio_post_profiles")
        for stem in stems:
            self.audio(stem)
        for episode_id, shot_id in shot_ids:
            self.ledger(self.store._shot_output_directory(episode_id, shot_id))
            for output in self.store.list_shot_outputs(shot_id, episode_id=episode_id):
                self.shot_output(output)
        for episode_id in episode_ids:
            self.ledger(self.store._episode_output_directory(episode_id))
            for cut in self.store.list_episode_outputs(episode_id):
                self.cut_output(cut)
        for run in self.records(GenerationRun, "generations"):
            self.generation(run)
        for h3 in self.records(H3Candidate, "generated/h3/candidates"):
            self.add(h3.source_path, h3.source_sha256)
            self.add(h3.av_path, h3.av_sha256)
            self.add(h3.wav_path, h3.wav_sha256)
            self.add(h3.report_path, h3.report_sha256)

    def resolve(self, relative: str) -> tuple[str, Path]:
        normalized = relative.replace("\\", "/")
        parts = normalized.split("/")
        resolved = self.root.resolve(normalized)
        if any(part in {"", "."} for part in parts) or resolved != self.root.path.joinpath(*parts):
            raise _invalid(f"source package path {relative!r} is an alias")
        return normalized, resolved

    def directories(self, relative: str) -> tuple[str, ...]:
        _, directory = self.resolve(relative)
        if not directory.exists():
            return ()
        if not directory.is_dir():
            raise _invalid(f"expected source package record directory: {relative}")
        return tuple(
            self.resolve(f"{relative}/{item.name}")[0]
            for item in sorted(directory.iterdir()) if item.is_dir()
        )

    def json_paths(self, relative: str, pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
        _, directory = self.resolve(relative)
        if not directory.exists():
            return ()
        if not directory.is_dir():
            raise _invalid(f"expected source package manifest directory: {relative}")
        return tuple(
            f"{relative}/{item.name}" for item in sorted(directory.iterdir())
            if item.suffix == ".json" and (pattern is None or pattern.fullmatch(item.name))
        )

    def records(self, cls: type[E], directory: str) -> list[E]:
        result = []
        for relative in self.json_paths(directory):
            self.capture_json(relative)
            record_id = Path(relative).stem
            record = self.store.load(cls, record_id)
            id_field = self.store._checked_layout(cls, record_id, None).id_field
            if id_field is None or getattr(record, id_field) != record_id:
                raise _invalid(f"project manifest identity does not match {relative!r}")
            result.append(record)
        return result

    def history(
        self, cls: type[V], directory: str, record_id: str, *, episode_id: str | None = None,
    ) -> list[V]:
        paths = self.json_paths(directory, _VERSIONED)
        versions = self.store.versions(cls, record_id, episode_id=episode_id)
        for relative in paths:
            raw = self.capture_json(relative)
            if cls is Shot:
                assert episode_id is not None
                framing = raw.get("framing_panel_ref")
                if isinstance(framing, str) and framing:
                    key = framing.replace("\\", "/").casefold()
                    version = int(Path(relative).stem.split(".")[0][1:])
                    self.framing.setdefault(key, set()).add((episode_id, record_id, version))
        orphaned = [path for path in paths if int(Path(path).stem.split(".")[0][1:]) not in versions]
        if orphaned:
            raise _invalid(f"lifecycle snapshots lack their base content file: {orphaned}")
        return [
            self.store.load(cls, record_id, episode_id=episode_id, version=version)
            for version in versions
        ]

    def protect_framing(self, shot_ids: list[tuple[str, str]]) -> None:
        if not self.framing:
            return
        for episode_id, shot_id in shot_ids:
            directory = self.store._shot_output_directory(episode_id, shot_id)
            for relative in self.json_paths(directory, _LEDGER):
                row = self.capture_json(relative)
                path, version = row.get("relative_path"), row.get("record_version")
                if (row.get("kind") != ShotOutputKind.PREVIEW or not isinstance(path, str)
                        or type(version) is not int or row.get("episode_id") != episode_id
                        or row.get("shot_id") != shot_id):
                    continue
                normalized = path.replace("\\", "/")
                key = normalized.casefold()
                if (_NATIVE_RENDER.fullmatch(normalized) and normalized.endswith(".png")
                        and normalized.startswith(f"renders/{shot_id}/v{version:04d}/")
                        and self.framing.get(key) == {(episode_id, shot_id, version)}):
                    del self.framing[key]
        self.private.update(self.framing)

    def ledger(self, directory: str) -> None:
        for relative in self.json_paths(directory, _LEDGER):
            row = self.capture_json(relative)
            # Refuse excluded paths before store ledger validation reads their bytes.
            for field in ("relative_path", "credit_manifest_path"):
                artifact = row.get(field)
                if isinstance(artifact, str):
                    self.add(artifact)

    def capture_json(self, relative: str, *, cache_receipt: bool = False) -> dict:
        normalized = self.add(relative, cache_receipt=cache_receipt)
        assert normalized is not None
        data = self.root.read_bytes(normalized)
        self.add(normalized, hashlib.sha256(data).hexdigest(), cache_receipt=cache_receipt)
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise _invalid(f"expected a JSON object in {relative!r}")
        return payload

    def reason_excluded(self, relative: str) -> str | None:
        folded = relative.casefold()
        parts = folded.split("/")
        name = parts[-1]
        if folded in self.private:
            return "private reference"
        if "references" in parts and not (
            len(parts) == 3 and parts[:2] == ["references", "style_bible"]
            and _VERSIONED.fullmatch(name)
        ):
            return "private reference"
        if any(part in {"authorized_samples", "reference_frames", "retrieved_frames"} for part in parts):
            return "private reference"
        if parts[:2] in (["generated", "qwen"], ["generated", "reference"]):
            return "private reference"
        if (len(parts) == 3 and parts[:2] == ["_journal", "cascades"]
                and re.fullmatch(r"[a-f0-9]{32}(?:\.done)?\.json", name)):
            return None
        if (parts[0] in _ADMIN_DIRS or _DATABASE.fullmatch(name)
                or any(part.startswith(".") for part in parts)
                or any(part in {"credentials", "secrets", "__pycache__"} for part in parts)
                or name.startswith(("settings.", "secrets.", "credentials."))
                or Path(name).suffix in {".pem", ".pfx", ".key"}):
            return "administrative data"
        return None

    def add(
        self, relative: str, digest: str | None = None, *, optional: bool = False,
        skip_private: bool = False, cache_receipt: bool = False,
    ) -> str | None:
        normalized, path = self.resolve(relative)
        reason = self.reason_excluded(normalized)
        if reason is not None and (optional or (reason == "private reference" and skip_private)):
            return None
        if reason is not None:
            raise _invalid(f"required source package file {relative!r} is excluded as {reason}")
        if not path.is_file():
            if optional and not path.exists():
                return None
            raise _invalid(f"required source package file is missing or not a file: {relative}")
        if path.stat().st_nlink > 1 and not cache_receipt:
            raise _invalid(f"source package file {relative!r} is a hard-link alias")
        if digest is not None and (not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
            raise _invalid(f"invalid SHA-256 for source package file {relative!r}")
        key = normalized.casefold()
        prior_name = self.casefolded.setdefault(key, normalized)
        if prior_name != normalized:
            raise _invalid(f"source package paths {prior_name!r} and {normalized!r} collide")
        prior = self.files.get(normalized)
        existing_hash = prior.expected_sha256 if prior is not None else None
        if digest is not None and existing_hash is not None and digest != existing_hash:
            raise _invalid(f"conflicting SHA-256 receipts for source package file {relative!r}")
        self.files[normalized] = SnapshotFile(normalized, digest or existing_hash)
        return normalized

    def add_content(self, relative: str, content: bytes) -> str:
        """Add package-only bytes without writing them into the source project."""
        normalized, _path = self.resolve(relative)
        reason = self.reason_excluded(normalized)
        if reason is not None:
            raise _invalid(f"required package file {relative!r} is excluded as {reason}")
        digest = hashlib.sha256(content).hexdigest()
        key = normalized.casefold()
        prior_name = self.casefolded.setdefault(key, normalized)
        if prior_name != normalized:
            raise _invalid(f"source package paths {prior_name!r} and {normalized!r} collide")
        self.files[normalized] = SnapshotFile(normalized, digest, content)
        return normalized

    def audio(self, stem: str) -> None:
        asset = self.assets.get(stem)
        if asset is None:
            self.add(stem)
            return
        if asset.file_path is None:
            raise _invalid(f"audio Asset {stem!r} has no local file")
        self.add(asset.file_path, asset.checksum)

    def shot_output(self, output: ShotOutput) -> None:
        relative = self.add(output.relative_path, output.content_sha256)
        assert relative is not None
        if output.kind is ShotOutputKind.SCENE:
            self.scene_bindings(relative)
        if output.kind is ShotOutputKind.DEGRADED:
            post = _NATIVE_POST.fullmatch(relative)
            if post is not None:
                self.add(f"{post[1]}/ffmpeg.log", optional=True)
                metadata = self.add(f"{post[1]}/metadata.json", optional=True)
                if metadata is not None:
                    self.capture_json(metadata)
        if output.kind not in {ShotOutputKind.MASTER, ShotOutputKind.PREVIEW}:
            return
        match = _NATIVE_RENDER.fullmatch(relative)
        if match is None:
            return
        attempt = match["attempt"]
        blend = f"{attempt}/scene.blend"
        self.add(blend)
        with self.resolve(blend)[1].open("rb") as stream:
            if stream.read(7) != b"BLENDER":
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"invalid native Blender source: {blend}")
        worker = f"{attempt}/worker.py"
        self.add(worker)
        for filename in ("blender.log", "ffmpeg-assembly.log"):
            self.add(f"{attempt}/{filename}", optional=True)
        meta = self.add(f"{attempt}/metadata.json", optional=True)
        if meta is not None:
            metadata = self.capture_json(meta)
            self.add(worker, metadata.get("script_sha256"))
            shared = metadata.get("shared_base")
            if shared is not None:
                self.shared_base(shared)

    def scene_bindings(self, relative: str) -> None:
        artifact = self.capture_json(relative)
        if "plan" not in artifact:
            return  # Older JSON scene records may have no bound asset payloads.
        plan = artifact["plan"]
        scene = plan.get("scene") if isinstance(plan, dict) else None
        if not isinstance(scene, dict):
            raise _invalid(f"scene manifest {relative!r} has no scene plan")
        objects, materials = scene.get("objects", []), scene.get("materials", [])
        if not isinstance(objects, list) or not isinstance(materials, list):
            raise _invalid(f"scene manifest {relative!r} has invalid object or material lists")
        for obj in objects:
            if not isinstance(obj, dict):
                raise _invalid(f"scene manifest {relative!r} has an invalid object")
            if obj.get("asset_id") is not None:
                self.scene_binding(obj, relative)
        for material in materials:
            if not isinstance(material, dict):
                raise _invalid(f"scene manifest {relative!r} has an invalid material")
            textures = material.get("textures")
            if textures is None:
                continue
            bindings = textures.get("bindings") if isinstance(textures, dict) else None
            maps = textures.get("maps") if isinstance(textures, dict) else None
            if not isinstance(bindings, dict) or not isinstance(maps, dict) or bindings.keys() != maps.keys():
                raise _invalid(f"scene manifest {relative!r} has invalid material texture bindings")
            for role, binding in bindings.items():
                self.scene_binding(binding, relative, asset_id=maps[role])
        environment = scene.get("environment")
        if environment is not None:
            if not isinstance(environment, dict) or not isinstance(environment.get("asset_id"), str):
                raise _invalid(f"scene manifest {relative!r} has an invalid environment")
            self.scene_binding(environment.get("binding"), relative, asset_id=environment["asset_id"])

    def scene_binding(self, binding: object, relative: str, *, asset_id: str | None = None) -> None:
        if not isinstance(binding, dict):
            raise _invalid(f"scene manifest {relative!r} has an invalid asset binding")
        bound_id = binding.get("asset_id")
        path = binding.get("asset_file_path")
        digest = binding.get("asset_checksum")
        if (not isinstance(bound_id, str) or not isinstance(path, str) or not isinstance(digest, str)
                or (asset_id is not None and bound_id != asset_id)):
            raise _invalid(f"scene manifest {relative!r} has an incomplete or inconsistent asset binding")
        asset = self.assets.get(bound_id)
        if asset is not None and asset.type.casefold() == "reference":
            raise _invalid(f"scene manifest {relative!r} uses private reference Asset {bound_id!r}")
        self.add(path, digest)

    def shared_base(self, evidence: object) -> None:
        if not isinstance(evidence, dict):
            raise _invalid("Blender shared base evidence is not an object")
        relative = evidence.get("receipt_path")
        path = evidence.get("blend_path")
        digest = evidence.get("sha256")
        if not isinstance(relative, str) or not isinstance(path, str) or not isinstance(digest, str):
            raise _invalid("Blender shared base evidence is missing a path or SHA-256")
        match = _SHARED_RECEIPT.fullmatch(relative)
        if match is None or evidence.get("key") != match[1] or not _SHARED_BLEND.fullmatch(path):
            raise _invalid(f"invalid Blender shared base receipt {relative!r}")
        self.add(path, digest)
        self.cache_receipt(relative, match[1])

    def cache_receipt(self, relative: str, key: str, *, digest: str | None = None) -> None:
        self.resolve(relative)
        receipt = _read_receipt(self.root, relative, key)
        if receipt is None:
            raise _invalid(f"missing Blender shared base receipt {relative!r}")
        self.capture_json(relative, cache_receipt=True)
        self.add(relative, digest, cache_receipt=True)
        self.add(receipt.blend_path, receipt.sha256)

    def cut_output(self, cut: EpisodeOutput) -> None:
        self.add(cut.relative_path, cut.content_sha256)
        if cut.credit_manifest_path is None:
            return
        self.add(cut.credit_manifest_path, cut.credit_manifest_sha256)
        credits = self.capture_json(cut.credit_manifest_path)
        plain_text = credits.get("plain_text_path")
        if plain_text is not None:
            if not isinstance(plain_text, str):
                raise _invalid("credit manifest plain_text_path is not a project-relative string")
            self.add(plain_text)
        cards = credits.get("generated_cards", [])
        if not isinstance(cards, list):
            raise _invalid("credit manifest generated_cards is not a list")
        for card in cards:
            if not isinstance(card, dict) or not isinstance(card.get("text_path"), str):
                raise _invalid("credit manifest card lacks a project-relative text_path")
            self.add(card["text_path"], card.get("text_sha256"))

    def generation(self, run: GenerationRun) -> None:
        for path in run.input_paths:
            self.run_file(path, skip_private=True)
        for path in run.output_paths:
            self.run_file(path, run.output_hashes.get(path))
        if run.log_path is not None:
            self.run_file(run.log_path, skip_private=True, optional=True)

    def run_file(
        self, path: str, digest: str | None = None, *,
        skip_private: bool = False, optional: bool = False,
    ) -> None:
        relative, _ = self.resolve(path)
        cache = _SHARED_RECEIPT.fullmatch(relative)
        if cache is not None:
            self.cache_receipt(relative, cache[1], digest=digest)
        else:
            self.add(relative, digest, skip_private=skip_private, optional=optional)

