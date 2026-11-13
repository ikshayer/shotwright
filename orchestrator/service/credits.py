"""Rights and attribution snapshot for the assets actually bound to built shots."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

from orchestrator.domain.entities import Asset, Episode, Shot, ShotOutputKind
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.store.json_store import ProjectStore, RecordNotFound

_LICENSE_URLS = {
    "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
}


def collect_episode_credits(store: ProjectStore, *, episode_id: str) -> dict[str, Any]:
    """Collect credits for every current Episode Shot, requiring its exact built scene."""
    episode = store.load(Episode, episode_id)
    shots = [store.load(Shot, shot_id, episode_id=episode_id) for shot_id in episode.shot_ids]
    result = collect_credits_for_shots(store, shots)
    result["episode_id"] = episode_id
    return result


def collect_credits_for_shots(store: ProjectStore, shots: Sequence[Shot]) -> dict[str, Any]:
    """Traverse exact persisted scene plans and current Shot references, not project inventory."""
    records: dict[str, dict[str, Any]] = {}
    shot_rows: list[dict[str, Any]] = []
    publication_blockers: list[str] = []
    for shot in shots:
        outputs = store.list_shot_outputs(
            shot.shot_id, episode_id=shot.episode_id, record_version=shot.version,
            kind=ShotOutputKind.SCENE,
        )
        if not outputs:
            raise _invalid(f"Shot {shot.shot_id!r} version {shot.version} has no built scene output")
        output = outputs[-1]
        try:
            scene_bytes = store._root.read_bytes(output.relative_path)
            if hashlib.sha256(scene_bytes).hexdigest() != output.content_sha256:
                raise ValueError("scene output bytes do not match their immutable ShotOutput checksum")
            artifact = json.loads(scene_bytes.decode("utf-8"))
            plan = artifact.get("plan") if isinstance(artifact, dict) else None
            if (output.kind is not ShotOutputKind.SCENE
                    or output.shot_id != shot.shot_id
                    or output.episode_id != shot.episode_id
                    or output.record_version != shot.version):
                raise ValueError("SCENE receipt identity does not match the selected Shot content version")
            if (not isinstance(plan, dict) or plan.get("schema_version") != 1
                    or plan.get("shot_id") != shot.shot_id
                    or plan.get("shot_version") != shot.version):
                raise ValueError("plan identity does not match the selected Shot content version")
            scene = plan.get("scene")
            if not isinstance(scene, dict):
                raise TypeError("scene plan has no persisted SceneSpec")
            object_rows = scene.get("objects")
            material_rows = scene.get("materials")
            if not isinstance(object_rows, list) or not isinstance(material_rows, list):
                raise TypeError("persisted SceneSpec requires object and material lists")
            used: dict[str, tuple[str | None, str | None, str | None]] = {}

            def bind(target: dict[str, tuple[str | None, str | None, str | None]],
                     asset_id: Any, checksum: Any, file_path: Any, kind: str) -> None:
                if not isinstance(asset_id, str) or not asset_id:
                    raise ValueError(f"persisted {kind} binding has an invalid asset id")
                if not isinstance(checksum, str) or not checksum or not isinstance(file_path, str) or not file_path:
                    raise ValueError(f"persisted {kind} binding {asset_id!r} lacks checksum or file path")
                value = (checksum, file_path, kind)
                prior = target.get(asset_id)
                if prior is not None and prior != value:
                    raise ValueError(f"inconsistent persisted bindings for Asset {asset_id!r}")
                target[asset_id] = value

            shot_rows.append({
                "shot_id": shot.shot_id,
                "shot_version": shot.version,
                "scene_output_id": output.output_id,
                "scene_output_path": output.relative_path,
                "scene_output_sha256": output.content_sha256,
            })
            for obj in object_rows:
                if not isinstance(obj, dict):
                    raise TypeError("scene object row is invalid")
                if "asset_id" in obj and obj["asset_id"] is not None:
                    asset_id = obj["asset_id"]
                    if not isinstance(asset_id, str) or not asset_id:
                        raise ValueError("scene object has an invalid asset identity")
                    bind(used, asset_id, obj.get("asset_checksum"), obj.get("asset_file_path"), "model")
            for material in material_rows:
                if not isinstance(material, dict):
                    raise TypeError("scene material row is invalid")
                textures = material.get("textures")
                if textures is not None:
                    if not isinstance(textures, dict):
                        raise TypeError("scene material textures are invalid")
                    maps, bindings = textures.get("maps"), textures.get("bindings")
                    if not isinstance(maps, dict) or not isinstance(bindings, dict) or set(maps) != set(bindings):
                        raise ValueError("scene material has incomplete persisted asset bindings")
                    for role, asset_id in maps.items():
                        binding = bindings[role]
                        if not isinstance(asset_id, str) or not asset_id:
                            raise ValueError(f"scene material role {role!r} has an invalid asset identity")
                        if not isinstance(binding, dict) or binding.get("asset_id") != asset_id:
                            raise ValueError(f"scene material role {role!r} asset identity mismatch")
                        bind(used, asset_id, binding.get("asset_checksum"), binding.get("asset_file_path"), "texture")
            environment = scene.get("environment")
            if environment is not None:
                binding = environment.get("binding") if isinstance(environment, dict) else None
                if not isinstance(binding, dict) or binding.get("asset_id") != environment.get("asset_id"):
                    raise ValueError("scene environment binding is missing or mismatched")
                bind(used, binding.get("asset_id"), binding.get("asset_checksum"), binding.get("asset_file_path"), "hdri")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _invalid(f"Shot {shot.shot_id!r} scene provenance is invalid: {exc}", cause=exc) from exc

        # Shot-level still/audio references are part of the delivered shot's provenance.
        for asset_id in shot.asset_ids:
            used.setdefault(asset_id, (None, None, None))
        # audio_refs are project-relative paths; include only a registered exact path
        # match so arbitrary path text never becomes an asset identity.
        if shot.audio_refs:
            registry_dir = store._root.resolve("assets/registry")
            registry_files = sorted(registry_dir.glob("*.json")) if registry_dir.is_dir() else []
            for audio_ref in shot.audio_refs:
                matched: Asset | None = None
                try:
                    matched = store.load(Asset, audio_ref)
                except RecordNotFound:
                    pass
                if matched is None:
                    # Asset is an unversioned record and cannot use ProjectStore.ids.
                    # Read candidate metadata through ProjectRoot, then load only the
                    # exact path match so malformed unused registry rows do not block a cut.
                    for registry_file in registry_files:
                        asset_id = registry_file.stem
                        try:
                            raw = json.loads(
                                store._root.read_text(f"assets/registry/{registry_file.name}", encoding="utf-8")
                            )
                        except (OSError, ValueError, json.JSONDecodeError):
                            continue
                        if isinstance(raw, dict) and raw.get("file_path") == audio_ref:
                            matched = store.load(Asset, asset_id)
                            break
                if matched is None:
                    raise _invalid(
                        f"Shot {shot.shot_id!r} audio reference {audio_ref!r} has no registered Asset identity"
                    )
                used.setdefault(matched.asset_id, (None, None, "audio"))

        row_ids: list[str] = []
        for asset_id, (bound_hash, bound_path, expected_type) in used.items():
            asset = _load_verified_asset(store, asset_id, bound_hash, bound_path, expected_type)
            license_key = _canonical_license(asset.license)
            license_url = _LICENSE_URLS.get(license_key)
            publication_required = license_key == "CC-BY-4.0"
            attribution = (asset.attribution or "").strip()
            source_url = (asset.source_url or "").strip()
            blocker = None
            if license_key not in _LICENSE_URLS:
                blocker = f"Unrecognized or restricted license {asset.license!r} for Asset {asset_id!r}"
            elif publication_required and (not attribution or not source_url or not license_url):
                raise _invalid(f"CC-BY-4.0 Asset {asset_id!r} is missing complete attribution, source URL, or canonical license URL")
            elif asset.reuse_scope not in {"project", "commercial", "commercial_use", "public"}:
                blocker = f"Asset {asset_id!r} reuse_scope {asset.reuse_scope!r} is not cleared for publication"
            record = {
                "asset_id": asset.asset_id,
                "attribution": attribution,
                "source_url": source_url or None,
                "license": asset.license,
                "license_url": license_url,
                # This is the finished-video obligation derived from the license.
                # Poly Haven's separate API attribution flag remains provenance metadata
                # and never turns a CC0 asset into a publication-required credit.
                "publication_required": publication_required,
                "reuse_scope": asset.reuse_scope,
            }
            prior = records.get(asset_id)
            if prior is not None and prior != record:
                raise _invalid(f"Asset {asset_id!r} has inconsistent provenance across selected shots")
            records[asset_id] = record
            if blocker is not None:
                blockers_for_asset = f"{blocker}"
                if blockers_for_asset not in publication_blockers:
                    publication_blockers.append(blockers_for_asset)
            row_ids.append(asset_id)
        shot_rows[-1]["asset_ids"] = sorted(set(row_ids))

    assets = [records[key] for key in sorted(records)]
    blockers = publication_blockers
    lines: list[str] = []
    for row in assets:
        lines.append(f"Asset: {row['asset_id']}")
        if row["attribution"]:
            lines.append(row["attribution"])
        if row["source_url"]:
            lines.append(f"Source: {row['source_url']}")
        lines.append(f"License: {row['license']}")
        if row["license_url"]:
            lines.append(f"License URL: {row['license_url']}")
        lines.append(f"Publication credit required: {'yes' if row['publication_required'] else 'no'}")
        lines.append(f"Reuse scope: {row['reuse_scope']}")
        lines.append("")
    return {
        "schema_version": 1, "entries": assets, "shot_versions": shot_rows,
        "publication_blockers": blockers,
        "plain_text": "\n".join(lines).rstrip() + ("\n" if lines else ""),
    }


def _load_verified_asset(store: ProjectStore, asset_id: str, bound_hash: Any, bound_path: Any, expected_type: str | None) -> Asset:
    try:
        asset = store.load(Asset, asset_id)
    except RecordNotFound as exc:
        raise _invalid(f"scene references missing registered Asset {asset_id!r}", cause=exc) from exc
    if expected_type is not None:
        allowed_types = {
            "model": {"model", "geometry", "mesh"},
            "texture": {"texture"}, "hdri": {"hdri"}, "audio": {"audio", "voice"},
        }.get(expected_type, {expected_type})
        if asset.type not in allowed_types:
            raise _invalid(f"persisted {expected_type} binding {asset_id!r} resolves to wrong registry type {asset.type!r}")
    if not asset.file_path or not asset.checksum:
        raise _invalid(f"Asset {asset_id!r} has incomplete registered file provenance")
    if expected_type is not None and (not bound_hash or not bound_path):
        raise _invalid(f"persisted scene binding for Asset {asset_id!r} lacks checksum or path")
    if bound_hash is not None and bound_hash != asset.checksum:
        raise _invalid(f"persisted scene binding checksum for Asset {asset_id!r} is stale or tampered")
    if bound_path is not None and bound_path != asset.file_path:
        raise _invalid(f"persisted scene binding path for Asset {asset_id!r} is stale or tampered")
    try:
        data = store._root.read_bytes(asset.file_path)
    except (OSError, ValueError) as exc:
        raise _invalid(f"Asset {asset_id!r} bytes cannot be read through ProjectRoot: {exc}", cause=exc) from exc
    if hashlib.sha256(data).hexdigest() != asset.checksum:
        raise _invalid(f"Asset {asset_id!r} checksum does not match its registered identity")
    if not asset.license or not asset.reuse_scope:
        raise _invalid(f"Asset {asset_id!r} is missing rights metadata")
    return asset


def _canonical_license(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).upper().replace("CREATIVECOMMONS", "CC-")
    if normalized in {"CC-BY-4.0", "CCBY4.0", "CC-BY4.0", "CC-BY-4.0-INTERNATIONAL"}:
        return "CC-BY-4.0"
    if normalized in {"CC0-1.0", "CC0", "CC-ZERO", "PUBLICDOMAIN"}:
        return "CC0-1.0"
    return value.strip()


def _invalid(message: str, *, cause: BaseException | None = None) -> JobError:
    return JobError(JobErrorKind.INVALID_MANIFEST, message, cause=cause)
