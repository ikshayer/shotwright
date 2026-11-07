"""FR-20 figure asset binding, planning, and Shot persistence contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orchestrator.adapters.blender.scene import (
    RenderRequest,
    build_scene_plan,
    synthetic_interior,
)
from orchestrator.domain.anomaly import StaticSilhouette
from orchestrator.domain.entities import CameraProfile, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.service.compose import compose_render_request
from orchestrator.store.json_store import ProjectStore
from tests.service.test_asset_contract import (
    _asset,
    _asset_fit_function,
    _canonical_plan_bytes,
    _project_store,
    _register_asset,
    _tiny_glb,
)

_FIGURE_ASSET_ID = "figure-only-on-anomaly"
_AUTHORED_BOUNDS = (0.5, 1.0, 2.0)
_DECLARED_DIMENSIONS = (0.4, 0.7, 1.8)  # width, depth, height
_FIGURE_NAMES = (
    "FR20AnomalyFigure",
    "FR20AnomalyFigureHead",
    "FR20AnomalyFigureLeftArm",
    "FR20AnomalyFigureRightArm",
    "FR20AnomalyFigureLeftLeg",
    "FR20AnomalyFigureRightLeg",
    "FR20AnomalyFigureLeftFoot",
    "FR20AnomalyFigureRightFoot",
)
_FIGURE_CHILD_NAMES = _FIGURE_NAMES[1:]


def _figure_anomaly(
    *,
    figure_asset_id: str | None = None,
    minimum_distance_m: float = 1.0,
) -> StaticSilhouette:
    values: dict[str, object] = {
        "kind": "static_silhouette",
        "position": (0.0, 1.0, 0.0),
        "height_m": _DECLARED_DIMENSIONS[2],
        "width_m": _DECLARED_DIMENSIONS[0],
        "depth_m": _DECLARED_DIMENSIONS[1],
        "minimum_distance_m": minimum_distance_m,
        "maximum_on_frame_height_px": 960,
    }
    if figure_asset_id is not None:
        values["figure_asset_id"] = figure_asset_id
    return StaticSilhouette(**values)


def _figure_objects(plan: dict[str, object]) -> list[dict[str, object]]:
    scene = plan.get("scene")
    assert isinstance(scene, dict), (
        f"worker plan did not contain a scene object; observed {scene!r}"
    )
    objects = scene.get("objects")
    assert isinstance(objects, list), (
        f"worker scene did not contain an object list; observed {objects!r}"
    )
    return [
        item for item in objects
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"].startswith("FR20AnomalyFigure")
    ]


def _register_figure_asset(path: Path):
    root, store = _project_store(path)
    source_relative = "incoming/figure.glb"
    root.write_bytes(source_relative, _tiny_glb(_AUTHORED_BOUNDS))
    registered = _register_asset(
        root,
        _asset(asset_id=_FIGURE_ASSET_ID),
        root.resolve(source_relative),
    )
    return root, store, registered


def _revise_shot_with_anomaly(store: ProjectStore, anomaly: StaticSilhouette) -> Shot:
    current = store.load(Shot, "shot-1", episode_id="ep1")
    revised = current.model_copy(update={"event_sec": 0.5, "anomaly": anomaly})
    successor = revised.model_copy(update=revised.next_version())
    store.revise(successor, expected_version=current.version)
    return successor


def _compose_request(store: ProjectStore, monkeypatch: pytest.MonkeyPatch):
    from orchestrator.service import compose as compose_service

    scene = synthetic_interior()
    monkeypatch.setattr(compose_service, "american_living_room", lambda **_kwargs: scene)
    return compose_render_request(
        store,
        shot_id="shot-1",
        episode_id="ep1",
        run_id="run-figure-asset",
        mode="master",
    )


def _geometry_refusal(store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> str:
    request = _compose_request(store, monkeypatch)
    try:
        build_scene_plan(request)
    except Exception as error:  # noqa: BLE001 - the contract pins refusal, not exception class
        return str(error)
    pytest.fail("figure with minimum_distance_m=100.0 was accepted by scene planning")


def test_figure_without_asset_keeps_the_eight_primitive_objects() -> None:
    request = RenderRequest(
        shot_id="shot-1",
        shot_version=2,
        run_id="run-figure-primitives",
        degrade_seed=9,
        duration_sec=4.0,
        camera_profile=CameraProfile(camera_id="cam-1", profile_name="test"),
        scene=synthetic_interior(),
        event_sec=0.5,
        anomaly=_figure_anomaly(),
    )
    anomaly_payload = request.anomaly.model_dump(mode="json")
    assert "figure_asset_id" not in anomaly_payload, (
        "None figure_asset_id appeared in the serialized no-asset anomaly; "
        f"observed anomaly keys {sorted(anomaly_payload)!r}"
    )
    plan = build_scene_plan(request)
    plan_anomaly = plan.get("anomaly")
    assert isinstance(plan_anomaly, dict), (
        f"worker plan omitted the typed anomaly payload; observed {plan_anomaly!r}"
    )
    assert "figure_asset_id" not in plan_anomaly, (
        "None figure_asset_id appeared in the serialized worker anomaly; "
        f"observed anomaly keys {sorted(plan_anomaly)!r}"
    )
    observed_names = tuple(sorted(item["name"] for item in _figure_objects(plan)))

    assert observed_names == tuple(sorted(_FIGURE_NAMES)), (
        "figure anomaly without figure_asset_id did not preserve today's eight primitives; "
        f"observed figure names {observed_names!r}"
    )


def test_anomaly_only_asset_is_bound_checksummed_fitted_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, registered = _register_figure_asset(tmp_path / "anomaly-only")
    anomaly = _figure_anomaly(figure_asset_id=_FIGURE_ASSET_ID)
    _revise_shot_with_anomaly(store, anomaly)
    stored_shot = store.load(Shot, "shot-1", episode_id="ep1")

    assert stored_shot.asset_ids == [], (
        "fixture Shot unexpectedly named the figure asset outside its anomaly; "
        f"observed Shot.asset_ids {stored_shot.asset_ids!r}"
    )
    assert getattr(stored_shot.anomaly, "figure_asset_id", None) == _FIGURE_ASSET_ID, (
        "figure asset id was not retained on the stored anomaly; "
        f"observed anomaly {stored_shot.anomaly!r}"
    )

    request = _compose_request(store, monkeypatch)
    scene_asset_ids = [item.asset_id for item in request.scene.objects if item.asset_id is not None]
    assert scene_asset_ids == [], (
        "fixture scene unexpectedly supplied the asset binding; "
        f"observed scene asset ids {scene_asset_ids!r}"
    )
    assert set(request.asset_bindings) == {_FIGURE_ASSET_ID}, (
        "compose_render_request did not bind exactly the asset named only by the anomaly; "
        f"observed binding ids {sorted(request.asset_bindings)!r}"
    )
    binding = request.asset_bindings.get(_FIGURE_ASSET_ID)
    assert binding is not None, (
        f"anomaly-only asset {_FIGURE_ASSET_ID!r} had no request binding; "
        f"observed bindings {request.asset_bindings!r}"
    )
    assert binding.checksum == registered.checksum, (
        "anomaly-only binding did not use the registered checksum; "
        f"observed {binding.checksum!r}, registered {registered.checksum!r}"
    )

    plan = build_scene_plan(request)
    figure_objects = _figure_objects(plan)
    assert len(figure_objects) == 1, (
        "asset-backed anomaly did not emit exactly one FR20AnomalyFigure object; "
        f"observed names {[item.get('name') for item in figure_objects]!r}"
    )
    figure = figure_objects[0]
    assert figure.get("name") == "FR20AnomalyFigure", (
        f"asset-backed figure had the wrong object name; observed {figure.get('name')!r}"
    )
    assert figure.get("asset_id") == _FIGURE_ASSET_ID, (
        f"worker figure object did not identify its asset; observed {figure.get('asset_id')!r}"
    )
    assert figure.get("asset_checksum") == registered.checksum, (
        "registered checksum did not reach the anomaly-only figure in the worker payload; "
        f"observed {figure.get('asset_checksum')!r}, expected {registered.checksum!r}"
    )
    observed_payload = _canonical_plan_bytes(plan)
    assert registered.checksum.encode("ascii") in observed_payload, (
        "canonical worker payload omitted the registered checksum for the anomaly-only asset; "
        f"observed plan SHA-256 {hashlib.sha256(observed_payload).hexdigest()}"
    )

    object_names = [
        item.get("name") for item in plan["scene"]["objects"]
        if isinstance(item, dict)
    ]
    observed_children = sorted(set(object_names) & set(_FIGURE_CHILD_NAMES))
    assert observed_children == [], (
        "asset-backed figure still emitted primitive head, arm, leg, or foot objects; "
        f"observed children {observed_children!r} in scene objects {object_names!r}"
    )
    assert figure.get("dimensions") == list(_DECLARED_DIMENSIONS), (
        "asset-backed figure dimensions differed from the declared width/depth/height; "
        f"observed {figure.get('dimensions')!r}, expected {list(_DECLARED_DIMENSIONS)!r}"
    )
    assert figure.get("material") != "FR20AnomalySilhouette", (
        "asset-backed figure received the primitive silhouette material; "
        f"observed material {figure.get('material')!r}"
    )

    fit = _asset_fit_function()
    source_min = tuple(-extent / 2 for extent in _AUTHORED_BOUNDS)
    source_max = tuple(extent / 2 for extent in _AUTHORED_BOUNDS)
    factors = fit(source_min, source_max, tuple(figure["dimensions"]))
    assert factors == pytest.approx((0.8, 0.7, 0.9)), (
        "worker asset_fit_factors did not map fixture bounds to declared dimensions; "
        f"observed factors {factors!r} for bounds {_AUTHORED_BOUNDS!r}"
    )
    mapped_envelope = tuple(
        (source_max[index] - source_min[index]) * factors[index]
        for index in range(3)
    )
    assert mapped_envelope == pytest.approx(_DECLARED_DIMENSIONS), (
        "scaled GLB bounds did not match the declared figure dimensions; "
        f"observed envelope {mapped_envelope!r}, expected {_DECLARED_DIMENSIONS!r}"
    )

    second_payload = _canonical_plan_bytes(build_scene_plan(request))
    assert observed_payload == second_payload, (
        "two builds of one anomaly-only asset request were not byte-identical; "
        f"observed SHA-256 {hashlib.sha256(observed_payload).hexdigest()} and "
        f"{hashlib.sha256(second_payload).hexdigest()}"
    )
    assert root.resolve("assets/models/figure-only-on-anomaly").is_dir(), (
        "API registration did not place the fixture under the project asset tree; "
        f"observed asset root {root.resolve('assets/models/figure-only-on-anomaly')}"
    )


def test_unregistered_anomaly_asset_is_refused_and_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, store = _project_store(tmp_path / "unregistered")
    _revise_shot_with_anomaly(store, _figure_anomaly(figure_asset_id=_FIGURE_ASSET_ID))

    with pytest.raises(Exception) as caught:
        request = _compose_request(store, monkeypatch)
        build_scene_plan(request)
    observed = str(caught.value)

    assert _FIGURE_ASSET_ID in observed, (
        "unregistered figure asset refusal did not identify the requested asset; "
        f"observed {observed!r}"
    )


def test_anomaly_only_asset_is_checksum_verified_before_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, registered = _register_figure_asset(tmp_path / "tampered-anomaly-only")
    assert registered.file_path is not None, (
        f"API-registered figure had no project-local path; observed {registered!r}"
    )
    root.write_bytes(registered.file_path, b"glTF bytes changed after registration")
    _revise_shot_with_anomaly(store, _figure_anomaly(figure_asset_id=_FIGURE_ASSET_ID))

    with pytest.raises(Exception) as caught:
        _compose_request(store, monkeypatch)
    observed = str(caught.value)

    assert _FIGURE_ASSET_ID in observed and "checksum mismatch" in observed.lower(), (
        "anomaly-only asset was not rejected after its registered bytes changed; "
        f"observed {observed!r}"
    )


def test_figure_asset_does_not_change_geometry_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, store, _registered = _register_figure_asset(tmp_path / "geometry")
    _revise_shot_with_anomaly(
        store,
        _figure_anomaly(minimum_distance_m=100.0),
    )
    plain_refusal = _geometry_refusal(store, monkeypatch)
    _revise_shot_with_anomaly(
        store,
        _figure_anomaly(
            figure_asset_id=_FIGURE_ASSET_ID,
            minimum_distance_m=100.0,
        ),
    )
    asset_refusal = _geometry_refusal(store, monkeypatch)

    assert plain_refusal == asset_refusal, (
        "asset-backed figure changed the refusal for the same declared geometry; "
        f"observed primitive path {plain_refusal!r} and asset path {asset_refusal!r}"
    )
    assert "minimum_distance_m" in asset_refusal, (
        "asset-backed figure bypassed the declared minimum-distance refusal; "
        f"observed {asset_refusal!r}"
    )


def test_figure_asset_id_round_trips_and_none_is_omitted_from_store_json(
    tmp_path: Path,
) -> None:
    root, store = _project_store(tmp_path / "store-round-trip")
    _revise_shot_with_anomaly(store, _figure_anomaly())
    none_path = root.resolve("episodes/ep1/shots/shot-1/v0002.json")
    none_record = json.loads(none_path.read_text(encoding="utf-8"))
    none_anomaly = none_record.get("anomaly")
    assert isinstance(none_anomaly, dict), (
        f"stored no-asset Shot omitted its anomaly object; observed {none_anomaly!r}"
    )
    assert "figure_asset_id" not in none_anomaly, (
        "None figure_asset_id changed the stored anomaly shape; "
        f"observed serialized anomaly keys {sorted(none_anomaly)!r}"
    )

    _revise_shot_with_anomaly(
        store,
        _figure_anomaly(figure_asset_id=_FIGURE_ASSET_ID),
    )
    reopened_store = ProjectStore(ProjectRoot(root.path))
    restored = reopened_store.load(Shot, "shot-1", episode_id="ep1")
    restored_asset_id = getattr(restored.anomaly, "figure_asset_id", None)
    assert restored_asset_id == _FIGURE_ASSET_ID, (
        "figure_asset_id did not survive a Shot store close/reopen round trip; "
        f"observed restored anomaly {restored.anomaly!r}"
    )
    stored_asset_record = json.loads(
        root.resolve("episodes/ep1/shots/shot-1/v0003.json").read_text(encoding="utf-8")
    )
    asset_anomaly = stored_asset_record.get("anomaly")
    assert isinstance(asset_anomaly, dict), (
        f"stored asset-backed Shot omitted its anomaly object; observed {asset_anomaly!r}"
    )
    assert asset_anomaly.get("figure_asset_id") == _FIGURE_ASSET_ID, (
        "stored anomaly JSON did not retain figure_asset_id; "
        f"observed anomaly payload {asset_anomaly!r}"
    )
