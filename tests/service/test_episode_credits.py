"""Publication rights and episode credit collection conformance tests."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from orchestrator.adapters.blender.scene import (
    AssetBinding,
    ObjectSpec,
    RenderRequest,
    synthetic_interior,
)
from orchestrator.domain.entities import (
    Asset,
    CameraProfile,
    Episode,
    EpisodeOutput,
    Job,
    Project,
    Shot,
    ShotOutput,
    ShotOutputKind,
    Timeline,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import parse_settings
from orchestrator.store.json_store import ProjectStore


def _store():
    base = Path.cwd() / "_work" / "production-feature-tests" / ("credits-" + uuid.uuid4().hex)
    ProjectRoot(Path.cwd()).make_dir(base.relative_to(Path.cwd()).as_posix())
    root = ProjectRoot(base)
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="credit fixture"), expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=10, shot_ids=["shot1"]), expected_version=0)
    store.create(CameraProfile(camera_id="cam1", profile_name="Credit test camera"))
    store.create(Shot(shot_id="shot1", episode_id="ep1", camera_profile="cam1", duration_sec=4, degrade_seed=1, asset_ids=["used-by", "used-cc0"], audio_refs=[]))
    return store


def _register(store, asset_id, *, license_name, provider, attribution, flag=False):
    payload = ("licensed fixture bytes:" + asset_id).encode()
    relative = f"assets/{asset_id}.glb"
    store._root.write_bytes(relative, payload)
    asset = Asset(asset_id=asset_id, type="model", generated=False, file_path=relative,
                  checksum=hashlib.sha256(payload).hexdigest(), provider=provider,
                  license=license_name, attribution=attribution, attribution_surface_required=flag,
                  source_url=f"https://{provider}.example/models/{asset_id}")
    store.save(asset, expected_version=0)
    return asset


def _make_built_scene(store):
    """Persist an actual scene plan and exact-version SCENE receipt; no fake registry."""
    from orchestrator.adapters.blender.runner import BlenderAdapter
    root = store._root
    shot = store.load(Shot, "shot1", episode_id="ep1")
    assets = [store.load(Asset, key) for key in ("used-by", "used-cc0")]
    base = synthetic_interior()
    objects = list(base.objects)
    bindings = {}
    for index, asset in enumerate(assets):
        objects.append(ObjectSpec(name=f"licensed-{index}", kind="box", asset_id=asset.asset_id,
                                  location=(4 + index, 2, 0), dimensions=(0.5, 0.5, 0.5)))
        bindings[asset.asset_id] = AssetBinding(asset_id=asset.asset_id, file_path=asset.file_path, checksum=asset.checksum)
    scene = base.model_copy(update={"objects": tuple(objects)})
    request = RenderRequest(shot_id=shot.shot_id, shot_version=shot.version, run_id="credit-scene",
                            degrade_seed=shot.degrade_seed, duration_sec=shot.duration_sec,
                            camera_profile=CameraProfile(camera_id="cam1", profile_name="Credit camera"),
                            scene=scene, asset_bindings=bindings)
    settings = parse_settings({"project_root": str(root.path)})
    result = BlenderAdapter(root, settings).assemble_scene(request, Job(
        job_id="credit-scene-job", type="build_scene", shot_id=shot.shot_id,
        episode_id=shot.episode_id, run_id=request.run_id,
    ))
    artifact = root.read_bytes(result.scene_path)
    store.append_shot_output(ShotOutput(
        output_id="credit-scene-receipt", episode_id=shot.episode_id, shot_id=shot.shot_id,
        record_version=shot.version, kind=ShotOutputKind.SCENE, relative_path=result.scene_path,
        content_sha256=hashlib.sha256(artifact).hexdigest(),
    ), expected_version=0)


def _snapshot(store):
    return {p.relative_to(store._root.path).as_posix(): p.read_bytes() for p in store._root.path.rglob("*") if p.is_file()}



def test_episode_credits_follow_persisted_scene_bindings_and_exclude_unused_assets():
    from orchestrator.service.credits import collect_episode_credits
    store = _store()
    by = _register(store, "used-by", license_name="CC-BY-4.0", provider="sketchfab", attribution="Creator Name, Model Title; licensed CC BY 4.0")
    cc0 = _register(store, "used-cc0", license_name="CC0-1.0", provider="polyhaven", attribution="Poly Haven", flag=True)
    _register(store, "unused", license_name="CC-BY-4.0", provider="sketchfab", attribution="Unused Creator, Unused Model")
    _make_built_scene(store)
    result = collect_episode_credits(store, episode_id="ep1")
    assert isinstance(result["entries"], list)
    assert result["publication_blockers"] == []
    entries = {item["asset_id"]: item for item in result["entries"]}
    assert set(entries) == {"used-by", "used-cc0"}
    assert entries["used-by"]["publication_required"] is True
    assert entries["used-by"]["license"] == "CC-BY-4.0"
    assert entries["used-by"]["attribution"] == by.attribution
    assert entries["used-by"]["source_url"] == by.source_url
    assert entries["used-cc0"]["publication_required"] is False
    assert entries["used-cc0"]["license"] == "CC0-1.0"
    assert entries["used-cc0"]["attribution"] == cc0.attribution
    assert "unused" not in json.dumps(result)


def test_episode_credit_get_is_read_only_and_requires_exact_built_version():
    from orchestrator.service.credits import collect_episode_credits
    store = _store()
    before = _snapshot(store)
    with pytest.raises(JobError) as caught:
        collect_episode_credits(store, episode_id="ep1")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert _snapshot(store) == before


def test_cc_by_missing_creator_attribution_refuses_to_clear_publication():
    from orchestrator.service.credits import collect_episode_credits
    store = _store()
    _register(store, "used-by", license_name="CC-BY-4.0", provider="sketchfab", attribution="")
    _register(store, "used-cc0", license_name="CC0-1.0", provider="polyhaven", attribution="Poly Haven")
    _make_built_scene(store)
    with pytest.raises(JobError) as caught:
        collect_episode_credits(store, episode_id="ep1")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


def test_tampered_registered_asset_hash_is_rejected_before_credit_clearance():
    from orchestrator.service.credits import collect_episode_credits
    store = _store()
    _register(store, "used-by", license_name="CC-BY-4.0", provider="sketchfab", attribution="Creator, Title")
    _register(store, "used-cc0", license_name="CC0-1.0", provider="polyhaven", attribution="Poly Haven")
    _make_built_scene(store)
    store._root.write_bytes("assets/used-by.glb", b"tampered after scene build")
    with pytest.raises(JobError) as caught:
        collect_episode_credits(store, episode_id="ep1")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


def _episode_output_fixture(store, *, with_manifest):
    """Create the persisted timeline and cut receipt using the real project store."""
    timeline = Timeline(episode_id="ep1")
    store.save(timeline, expected_version=0)
    saved, timeline_token = store.load_with_version(Timeline, "ep1")
    media_path = "deliveries/cut.mp4"
    media = b"small retained episode cut"
    store._root.write_bytes(media_path, media)
    values = {
        "output_id": "cut-1", "episode_id": "ep1", "timeline_store_version": timeline_token,
        "timeline_sha256": saved.content_sha256, "relative_path": media_path,
        "content_sha256": hashlib.sha256(media).hexdigest(), "delivery_profile": "dvr-2005",
        "storage_resolution": (640, 480), "output_fps": 12, "sample_aspect_ratio": "1/1",
        "ffmpeg_identity": "fixture-ffmpeg-build",
    }
    if with_manifest:
        manifest_path = "deliveries/cut-credits.txt"
        manifest = b"Creator Name - Model Title\nCC BY 4.0: https://creativecommons.org/licenses/by/4.0/\n"
        store._root.write_bytes(manifest_path, manifest)
        values.update(credit_manifest_path=manifest_path,
                      credit_manifest_sha256=hashlib.sha256(manifest).hexdigest())
    receipt = EpisodeOutput(**values)
    store.append_episode_output(receipt, expected_version=0)
    return receipt


def test_episode_output_listing_verifies_paired_credit_manifest_hash():
    store = _store()
    receipt = _episode_output_fixture(store, with_manifest=True)
    assert store.list_episode_outputs("ep1") == (receipt,)
    store._root.write_bytes(receipt.credit_manifest_path, b"tampered credit manifest")
    with pytest.raises(JobError) as caught:
        store.list_episode_outputs("ep1")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


def test_episode_output_listing_rejects_missing_credit_manifest():
    store = _store()
    receipt = _episode_output_fixture(store, with_manifest=True)
    store._root.resolve(receipt.credit_manifest_path).unlink()
    with pytest.raises(JobError) as caught:
        store.list_episode_outputs("ep1")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


def test_legacy_episode_output_without_credit_manifest_remains_listable():
    store = _store()
    receipt = _episode_output_fixture(store, with_manifest=False)
    assert receipt.credit_manifest_path is None and receipt.credit_manifest_sha256 is None
    assert store.list_episode_outputs("ep1") == (receipt,)
