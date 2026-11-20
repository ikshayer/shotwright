"""Immutable project-local receipts for worker-generated Blender room rigs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind

from .scene import RenderRequest, SharedLocation

_BASE_PATH = re.compile(
    r"renders/[A-Za-z0-9_-]+/v[0-9]{4,}/[A-Za-z0-9_-]+/attempt-[0-9]{4,}/base\.blend"
)
_SHA256 = re.compile(r"[a-f0-9]{64}")


def static_scene(plan: dict[str, object], location: SharedLocation) -> dict[str, object]:
    """Project a bound shot plan onto its neutral location and rig."""
    scene = dict(plan["scene"])
    scene.pop("camera_keyframes", None)
    for group, names, animation in (
        ("objects", location.object_names, {"keyframes", "visibility_keyframes"}),
        ("lights", location.light_names, {"energy_keyframes"}),
        ("materials", location.material_names, set()),
    ):
        items = {item["name"]: item for item in scene[group]}
        if not set(names) <= items.keys():
            raise JobError(JobErrorKind.INVALID_MANIFEST,
                           f"built scene is missing shared Location {group}; rebuild the scene")
        scene[group] = [
            {field: value for field, value in items[name].items() if field not in animation}
            for name in names
        ]
    return scene


@dataclass(frozen=True)
class BlendReceipt:
    blend_path: str
    sha256: str


@dataclass(frozen=True)
class SharedBase:
    key: str
    receipt_path: str
    staged_blend: str
    cached: BlendReceipt | None

    @classmethod
    def prepare(cls, root: ProjectRoot, request: RenderRequest, plan: dict[str, object],
                *, worker_sha256: str, blender_sha256: str, attempt: str) -> SharedBase:
        location = request.shared_location
        assert location is not None and request.mode == "master"
        identity = {
            "schema_version": 1,
            "episode_id": location.episode_id,
            "location_id": location.location_id,
            "location_version": location.location_version,
            "camera_id": location.camera_id,
            "profile_id": request.camera_profile.camera_id,
            "camera_mode": request.camera_profile.camera_mode,
            "camera": plan["camera"],
            "scene": static_scene(plan, location),
            "worker_sha256": worker_sha256,
            "blender_sha256": blender_sha256,
        }
        key = hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
        receipt_path = f"scenes/shared/{key}.json"
        return cls(key, receipt_path, f"{attempt}/base.blend",
                   _read_receipt(root, receipt_path, key))

    def worker_payload(self, root: ProjectRoot, scene: dict[str, object]) -> dict[str, object]:
        if self.cached is None:
            output = root.write_with(self.staged_blend, lambda path: None)
            source = None
        else:
            output = None
            try:
                source_path = _canonical_path(root, self.cached.blend_path)
            except (OSError, ValueError) as exc:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                               f"invalid shared base blend path: {exc}", cause=exc) from exc
            source = {"path": source_path.as_posix(), "sha256": self.cached.sha256}
        return {"key": self.key, "scene": scene, "input": source,
                "output": output.as_posix() if output is not None else None}

    def verify(self, root: ProjectRoot, worker_evidence: object) -> BlendReceipt:
        action = "reused" if self.cached is not None else "built"
        if worker_evidence != {"key": self.key, "action": action}:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "worker shared blend evidence mismatch")
        relative = self.cached.blend_path if self.cached is not None else self.staged_blend
        digest = _blend_sha256(root, relative)
        if self.cached is not None and self.cached.sha256 != digest:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "shared blend changed during rendering")
        return BlendReceipt(relative, digest)

    def publish(self, root: ProjectRoot, used: BlendReceipt) -> dict[str, str]:
        action = "reused" if self.cached is not None else "built"
        evidence = {"key": self.key, "action": action, "blend_path": used.blend_path,
                    "sha256": used.sha256, "receipt_path": self.receipt_path}
        if self.cached is not None:
            return evidence

        try:
            target = _canonical_path(root, self.receipt_path)
            candidate = root.write_text(
                self.staged_blend.removesuffix(".blend") + ".json",
                _json({"schema_version": 1, "key": self.key, "blend_path": used.blend_path,
                       "sha256": used.sha256}),
            )

            def link_receipt(path: Path) -> None:
                if path != target:
                    raise ValueError("shared blend receipt destination changed during publication")
                os.link(candidate, path)

            root.write_with(self.receipt_path, link_receipt)
        except FileExistsError:
            # Atomic no-clobber link publishes only a complete receipt. Keep the first
            # winner while recording the exact other base this render actually used.
            winner = _read_receipt(root, self.receipt_path, self.key)
            if winner is None:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "shared blend receipt disappeared")
            evidence.update(cache_blend_path=winner.blend_path, cache_sha256=winner.sha256)
        except ValueError as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"invalid shared blend receipt location: {exc}", cause=exc) from exc
        except OSError as exc:
            kind = JobError.from_exception(exc).kind
            raise JobError(kind, f"could not publish shared blend receipt: {exc}", cause=exc) from exc
        return evidence


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_path(root: ProjectRoot, relative: str) -> Path:
    path = root.resolve(relative)
    if path != root.path.joinpath(*relative.split("/")):
        raise ValueError(f"shared blend path {relative!r} may not resolve through an alias")
    return path


def _blend_sha256(root: ProjectRoot, relative: str) -> str:
    try:
        if not _BASE_PATH.fullmatch(relative):
            raise ValueError("not an attempt-scoped base blend path")
        path = _canonical_path(root, relative)
        with path.open("rb") as stream:
            header = stream.read(12)
            if len(header) < 12 or not header.startswith(b"BLENDER"):
                raise ValueError("missing Blender file signature")
            digest = hashlib.sha256(header)
            while block := stream.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()
    except (OSError, ValueError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"invalid shared base blend {relative!r}: {exc}", cause=exc) from exc


def _read_receipt(root: ProjectRoot, relative: str, key: str) -> BlendReceipt | None:
    try:
        _canonical_path(root, relative)
        try:
            payload = json.loads(root.read_text(relative))
        except FileNotFoundError:
            return None
        if (not isinstance(payload, dict)
                or set(payload) != {"schema_version", "key", "blend_path", "sha256"}
                or type(payload["schema_version"]) is not int
                or payload["schema_version"] != 1 or payload["key"] != key
                or not isinstance(payload["blend_path"], str)
                or not isinstance(payload["sha256"], str)
                or not _SHA256.fullmatch(payload["sha256"])):
            raise ValueError("receipt structure or identity mismatch")
        blend = BlendReceipt(payload["blend_path"], payload["sha256"])
        if _blend_sha256(root, blend.blend_path) != blend.sha256:
            raise ValueError("shared base blend checksum mismatch")
        return blend
    except (OSError, ValueError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"invalid shared base receipt {relative!r}: {exc}", cause=exc) from exc
