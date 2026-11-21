"""FR-15 location dressing, composition, and invalidation seam tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator.adapters.blender.scene import (
    SceneSpec,
    american_living_room,
    build_scene_plan,
)
from orchestrator.domain import entities
from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import CameraProfile, Location, Review, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.service.approval import approve_shot
from orchestrator.service.compose import compose_render_request
from orchestrator.service.dependencies import revise_location
from orchestrator.store.json_store import ProjectStore
from tests.service.test_asset_contract import (
    _asset,
    _asset_fit_function,
    _canonical_plan_bytes,
    _project_store,
    _register_asset,
    _tiny_glb,
)

_ROOM_ID = "shared-living-room"
_PROP_ID = "location-prop-1"
_AUTHORED_BOUNDS = (0.5, 1.0, 2.0)
_DECLARED_DIMENSIONS = (0.4, 0.7, 1.8)
_PROP_POSITION = (4.0, 4.0, 0.0)
_PROP_ROTATION = (0.0, 0.0, 0.25)


def _canonical_scene_bytes(scene: SceneSpec | dict[str, object]) -> bytes:
    payload = scene.model_dump(mode="json") if isinstance(scene, SceneSpec) else scene
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _placement(
    asset_id: str = _PROP_ID,
    *,
    position: tuple[float, float, float] = _PROP_POSITION,
    dimensions: tuple[float, float, float] = _DECLARED_DIMENSIONS,
    rotation: tuple[float, float, float] = _PROP_ROTATION,
) -> Any:
    """Build the contract's placement shape and fail locally if it is absent."""
    placement_type = getattr(entities, "AssetPlacement", None)
    assert placement_type is not None, (
        "orchestrator.domain.entities did not expose the contract's AssetPlacement; "
        f"observed {placement_type!r}"
    )
    return placement_type(
        asset_id=asset_id,
        position=position,
        dimensions=dimensions,
        rotation=rotation,
    )


def _location(
    *,
    scene_look: str = "bright",
    base_shell: str = "american_living_room",
    placements: list[Any] | None = None,
    asset_ids: list[str] | None = None,
    location_id: str = _ROOM_ID,
) -> Location:
    """Build a Location and identify any missing contract fields clearly."""
    required_fields = {
        "location_id",
        "base_shell",
        "scene_look",
        "placements",
        "asset_ids",
    }
    for field in sorted(required_fields):
        assert field in Location.model_fields, (
            f"Location did not expose the required {field!r} field; "
            f"observed Location fields {sorted(Location.model_fields)!r}"
        )
    values: dict[str, object] = {
        "location_id": location_id,
        "base_shell": base_shell,
        "scene_look": scene_look,
    }
    if placements is not None:
        values["placements"] = placements
    if asset_ids is not None:
        values["asset_ids"] = asset_ids
    return Location.model_validate(values)


def _put_location_and_link_shot(
    store: ProjectStore,
    location: Location,
    *,
    shot_id: str = "shot-1",
    episode_id: str = "ep1",
) -> None:
    store.create(location)
    current = store.load(Shot, shot_id, episode_id=episode_id)
    successor = current.model_copy(update={
        **current.next_version(),
        "location_id": location.location_id,
    })
    store.revise(successor, expected_version=current.version)


def _compose(
    store: ProjectStore,
    *,
    shot_id: str = "shot-1",
    episode_id: str = "ep1",
    run_id: str = "run-location-contract",
):
    return compose_render_request(
        store,
        shot_id=shot_id,
        episode_id=episode_id,
        run_id=run_id,
        mode="master",
    )


def _location_asset_plan(
    path: Path,
    *,
    authored_bounds: tuple[float, float, float] = _AUTHORED_BOUNDS,
) -> tuple[dict[str, object], str]:
    root, store = _project_store(path / "project")
    source_relative = "incoming/location-prop.glb"
    root.write_bytes(source_relative, _tiny_glb(authored_bounds))
    registered = _register_asset(
        root,
        _asset(asset_id=_PROP_ID),
        root.resolve(source_relative),
    )
    location = _location(placements=[_placement()])
    _put_location_and_link_shot(store, location)
    plan = build_scene_plan(_compose(store))
    assert registered.checksum is not None, (
        "registered location asset had no checksum; "
        f"observed Asset {registered!r}"
    )
    return plan, registered.checksum


@pytest.mark.parametrize("scene_look", ["bright", "low_key"])
def test_empty_location_composes_the_direct_procedural_room_byte_for_byte(
    tmp_path: Path,
    scene_look: str,
) -> None:
    _root, store = _project_store(tmp_path / scene_look)
    location = _location(scene_look=scene_look)
    _put_location_and_link_shot(store, location)

    observed = _canonical_scene_bytes(_compose(store).scene)
    expected = _canonical_scene_bytes(american_living_room(look=scene_look))

    assert observed == expected, (
        f"empty Location scene bytes differed from "
        f"american_living_room(look={scene_look!r}); "
        f"observed SHA-256 {hashlib.sha256(observed).hexdigest()}, "
        f"expected {hashlib.sha256(expected).hexdigest()}"
    )


def test_shared_location_composes_identical_room_scene_for_two_shots(
    tmp_path: Path,
) -> None:
    root, store = _project_store(tmp_path / "shared-location")
    source_relative = "incoming/location-prop.glb"
    root.write_bytes(source_relative, _tiny_glb(_AUTHORED_BOUNDS))
    _register_asset(root, _asset(asset_id=_PROP_ID), root.resolve(source_relative))
    location = _location(placements=[_placement()])
    _put_location_and_link_shot(store, location)

    store.create(CameraProfile(
        camera_id="cam-2",
        profile_name="second location camera",
        field_of_view_mm=14.0,
    ))
    store.create(Shot(
        shot_id="shot-2",
        episode_id="ep1",
        location_id=_ROOM_ID,
        camera_profile="cam-2",
        duration_sec=5.0,
        narrative_beat="a different beat in the same room",
    ))

    first_request = _compose(store, shot_id="shot-1", run_id="run-shot-1")
    second_request = _compose(store, shot_id="shot-2", run_id="run-shot-2")
    first_plan = build_scene_plan(first_request)
    second_plan = build_scene_plan(second_request)
    first_scene = first_plan.get("scene")
    second_scene = second_plan.get("scene")
    assert isinstance(first_scene, dict) and isinstance(second_scene, dict), (
        "composed worker plans did not both carry scene dictionaries; "
        f"observed first={type(first_scene).__name__}, second={type(second_scene).__name__}"
    )
    first_bytes = _canonical_scene_bytes(first_scene)
    second_bytes = _canonical_scene_bytes(second_scene)

    assert first_request.camera_profile.camera_id != second_request.camera_profile.camera_id, (
        "two-shot fixture did not use different cameras; "
        f"observed {first_request.camera_profile.camera_id!r} and "
        f"{second_request.camera_profile.camera_id!r}"
    )
    assert first_bytes == second_bytes, (
        "two shots referencing one location composed different room scene bytes; "
        f"observed SHA-256 {hashlib.sha256(first_bytes).hexdigest()} and "
        f"{hashlib.sha256(second_bytes).hexdigest()}"
    )


def test_location_placement_reaches_worker_with_checksum_and_declared_envelope(
    tmp_path: Path,
) -> None:
    plan, checksum = _location_asset_plan(tmp_path / "placed-asset")
    scene = plan.get("scene")
    assert isinstance(scene, dict), (
        f"location plan did not contain a worker scene; observed {scene!r}"
    )
    objects = scene.get("objects")
    assert isinstance(objects, list), (
        f"location worker scene did not contain objects; observed {objects!r}"
    )
    placed = [item for item in objects if isinstance(item, dict) and item.get("asset_id") == _PROP_ID]

    assert len(placed) == 1, (
        f"one location placement did not produce exactly one worker asset object; "
        f"observed {[item.get('asset_id') for item in objects if isinstance(item, dict)]!r}"
    )
    item = placed[0]
    assert item.get("asset_checksum") == checksum, (
        "location placement worker object did not carry the registered asset checksum; "
        f"observed {item.get('asset_checksum')!r}, expected {checksum!r}"
    )
    assert checksum.encode("ascii") in _canonical_plan_bytes(plan), (
        "canonical worker payload omitted the placement asset checksum; "
        f"observed plan SHA-256 {hashlib.sha256(_canonical_plan_bytes(plan)).hexdigest()}"
    )
    assert item.get("location") == list(_PROP_POSITION), (
        "placement position did not reach the worker object's location; "
        f"observed {item.get('location')!r}, expected {list(_PROP_POSITION)!r}"
    )
    assert item.get("dimensions") == list(_DECLARED_DIMENSIONS), (
        "declared placement dimensions did not reach the worker object; "
        f"observed {item.get('dimensions')!r}, expected {list(_DECLARED_DIMENSIONS)!r}"
    )
    assert item.get("rotation") == list(_PROP_ROTATION), (
        "placement rotation did not reach the worker object; "
        f"observed {item.get('rotation')!r}, expected {list(_PROP_ROTATION)!r}"
    )

    fit = _asset_fit_function()
    source_min = tuple(-extent / 2 for extent in _AUTHORED_BOUNDS)
    source_max = tuple(extent / 2 for extent in _AUTHORED_BOUNDS)
    factors = fit(source_min, source_max, tuple(item["dimensions"]))
    mapped_envelope = tuple(
        (source_max[index] - source_min[index]) * factors[index]
        for index in range(3)
    )
    assert mapped_envelope == pytest.approx(_DECLARED_DIMENSIONS), (
        "worker asset fit did not map the registered GLB bounds onto the placement's "
        f"declared envelope; observed {mapped_envelope!r}, expected {_DECLARED_DIMENSIONS!r}"
    )


def test_changed_placement_asset_bytes_change_the_worker_plan(tmp_path: Path) -> None:
    first_plan, first_checksum = _location_asset_plan(
        tmp_path / "first", authored_bounds=(0.5, 1.0, 2.0),
    )
    second_plan, second_checksum = _location_asset_plan(
        tmp_path / "second", authored_bounds=(0.75, 1.0, 2.0),
    )
    first_bytes = _canonical_plan_bytes(first_plan)
    second_bytes = _canonical_plan_bytes(second_plan)

    assert first_checksum != second_checksum, (
        "different placement asset bytes unexpectedly produced one checksum; "
        f"observed {first_checksum!r} and {second_checksum!r}"
    )
    assert first_checksum.encode("ascii") in first_bytes and second_checksum.encode("ascii") in second_bytes, (
        "one or both placement plans omitted their own registered checksum; "
        f"observed {first_checksum in first_bytes.decode('utf-8')}, "
        f"{second_checksum in second_bytes.decode('utf-8')}"
    )
    assert first_bytes != second_bytes, (
        "changing bytes behind the same placed asset id did not change worker plan bytes; "
        f"observed identical SHA-256 {hashlib.sha256(first_bytes).hexdigest()}"
    )


def test_unregistered_location_placement_is_refused_and_names_asset(
    tmp_path: Path,
) -> None:
    _root, store = _project_store(tmp_path / "unregistered")
    missing_id = "missing-location-prop"
    _put_location_and_link_shot(
        store,
        _location(placements=[_placement(asset_id=missing_id)]),
    )

    with pytest.raises(Exception) as caught:
        build_scene_plan(_compose(store))
    observed = str(caught.value)
    assert missing_id in observed, (
        "unregistered location placement refusal did not name its asset id; "
        f"observed {observed!r}"
    )


def test_placement_revision_stales_only_approved_shots_using_that_location(
    tmp_path: Path,
) -> None:
    _root, store = _project_store(tmp_path / "invalidation")
    before = _location(placements=[_placement()])
    store.create(before)

    def create_approved_shot(
        shot_id: str,
        *,
        location_id: str,
        dependencies: list[str],
        review_id: str,
    ) -> None:
        shot = Shot(
            shot_id=shot_id,
            episode_id="ep1",
            location_id=location_id,
            dependencies=dependencies,
            duration_sec=4.0,
        )
        store.create(shot)
        for status in (
            ShotStatus.PLANNED,
            ShotStatus.BLOCKING,
            ShotStatus.BUILDING,
            ShotStatus.REVIEW,
        ):
            changes: dict[str, object] = {"status": status}
            if status is ShotStatus.BUILDING:
                changes["framing_status"] = FramingStatus.APPROVED
            shot = shot.model_copy(update=changes)
            store.update(
                shot,
                expected_version=store.load_with_version(
                    Shot, shot_id, episode_id="ep1"
                )[1],
            )
        review = Review(
            review_id=review_id,
            target_id=shot_id,
            target_version=shot.version,
            target_kind="shot",
            episode_id="ep1",
            actor="creator",
            created_at=datetime.now(UTC),
            approval_state=ShotStatus.APPROVED,
        )
        store.save(review, expected_version=0)
        approve_shot(
            store,
            shot_id,
            episode_id="ep1",
            review_id=review_id,
            expected_version=store.load_with_version(
                Shot, shot_id, episode_id="ep1"
            )[1],
        )

    create_approved_shot(
        "approved-dependent",
        location_id=_ROOM_ID,
        dependencies=[_ROOM_ID],
        review_id="review-dependent",
    )
    create_approved_shot(
        "approved-unrelated",
        location_id="different-room",
        dependencies=[],
        review_id="review-unrelated",
    )
    current, token = store.load_with_version(Location, _ROOM_ID)
    moved = _placement(position=(4.5, 4.0, 0.0))
    revised = current.model_copy(update={
        **current.next_version(),
        "placements": [moved],
    })

    observed_stale = revise_location(
        store,
        revised,
        expected_version=token,
    )
    dependent = store.load(Shot, "approved-dependent", episode_id="ep1")
    unrelated = store.load(Shot, "approved-unrelated", episode_id="ep1")

    assert observed_stale == ("approved-dependent",), (
        "placement edit did not report exactly the render using the changed location; "
        f"observed stale ids {observed_stale!r}"
    )
    assert dependent.status is ShotStatus.STALE, (
        "approved shot using the revised location was not made stale for re-render; "
        f"observed status {dependent.status!r}"
    )
    assert dependent.approval_review_id == "review-dependent", (
        "location invalidation discarded the prior approval evidence; "
        f"observed approval_review_id {dependent.approval_review_id!r}"
    )
    assert unrelated.status is ShotStatus.APPROVED, (
        "placement edit invalidated an unrelated approved shot; "
        f"observed status {unrelated.status!r}"
    )


def test_asset_ids_and_placements_cannot_both_claim_the_same_geometry() -> None:
    asset_id = "duplicate-source-of-truth-prop"
    with pytest.raises(ValidationError) as caught:
        _location(
            location_id="duplicate-source-of-truth",
            asset_ids=[asset_id],
            placements=[_placement(asset_id=asset_id)],
        )
    observed = str(caught.value)
    assert any(term in observed.lower() for term in (asset_id, "asset_ids", "placement")), (
        "Location validator rejected the duplicate without identifying the conflicting "
        f"asset/field; observed {observed!r}"
    )


def test_one_location_request_builds_byte_identical_worker_plans(tmp_path: Path) -> None:
    _plan, _checksum = _location_asset_plan(tmp_path / "determinism")
    root = ProjectRoot(tmp_path / "determinism" / "project" / "p1")
    store = ProjectStore(root)

    first = _canonical_plan_bytes(build_scene_plan(_compose(store)))
    second = _canonical_plan_bytes(build_scene_plan(_compose(store)))

    assert first == second, (
        "two builds of the same location request were not byte-identical; "
        f"observed SHA-256 {hashlib.sha256(first).hexdigest()} and "
        f"{hashlib.sha256(second).hexdigest()}"
    )


def test_location_creation_or_revision_is_exposed_as_a_mutating_operation() -> None:
    from orchestrator.api.routes import OPERATIONS

    matching = [
        operation
        for operation in OPERATIONS
        if operation.mutating
        and ("location" in operation.name.casefold() or "location" in operation.path.casefold())
    ]
    assert matching, (
        "OPERATIONS did not expose a mutating location operation; "
        f"observed {[operation.name for operation in OPERATIONS]!r}"
    )
    assert any(operation.request_model is not None and operation.adapter for operation in matching), (
        "location operation had no typed request or service adapter; "
        f"observed {[(item.name, item.request_model, item.adapter) for item in matching]!r}"
    )
