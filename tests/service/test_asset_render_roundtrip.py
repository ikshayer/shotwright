"""Regression for imported-material preservation through a persisted scene round trip."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.adapters.blender.runner import BlenderAdapter
from orchestrator.adapters.blender.scene import ObjectSpec, RenderRequest
from orchestrator.domain.entities import Job, Location, Shot, ShotOutput, ShotOutputKind
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import parse_settings
from orchestrator.service.compose import compose_render_request
from orchestrator.store.json_store import ProjectStore
from tests.service.test_figure_asset_contract import (
    _figure_anomaly,
    _register_figure_asset,
)


def _minimal_object(**overrides: object) -> dict[str, object]:
    return {
        "name": "ImportedFigure",
        "kind": "box",
        "location": (0.0, 0.0, 0.0),
        "dimensions": (1.0, 1.0, 1.0),
        **overrides,
    }


@pytest.mark.parametrize("include_material", [False, True], ids=["omitted", "explicit-none"])
def test_optional_material_round_trips_python_and_json(include_material: bool) -> None:
    payload = _minimal_object()
    if include_material:
        payload["material"] = None

    original = ObjectSpec.model_validate(payload)
    python_round_trip = ObjectSpec.model_validate(original.model_dump(mode="python"))
    json_round_trip = ObjectSpec.model_validate(json.loads(original.model_dump_json()))

    assert original.material is None, f"asset material should remain None; observed {original.material!r}"
    assert python_round_trip.model_dump(mode="json") == original.model_dump(mode="json"), (
        "ObjectSpec changed across model_dump(mode='python') revalidation; "
        f"before={original.model_dump(mode='json')!r}, after={python_round_trip.model_dump(mode='json')!r}"
    )
    assert json_round_trip.model_dump(mode="json") == original.model_dump(mode="json"), (
        "ObjectSpec changed across JSON dump/revalidation; "
        f"before={original.model_dump(mode='json')!r}, after={json_round_trip.model_dump(mode='json')!r}"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", ""), ("name", "   "), ("material", ""), ("material", "   ")],
)
def test_object_name_and_non_null_material_must_be_nonblank(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ObjectSpec.model_validate(_minimal_object(**{field: value}))
    assert any(error["loc"] == (field,) for error in caught.value.errors()), (
        f"blank {field} was rejected for an unrelated field; observed {caught.value.errors()!r}"
    )


def test_registered_asset_scene_persists_recomposes_and_revalidates_without_gpu(
    tmp_path: Path,
) -> None:
    root, store, asset = _register_figure_asset(tmp_path / "persisted-asset-roundtrip")
    store.create(Location(location_id="loc-low-key", scene_look="low_key"))

    current = store.load(Shot, "shot-1", episode_id="ep1")
    successor = current.model_copy(update={
        "location_id": "loc-low-key",
        "event_sec": 0.5,
        "anomaly": _figure_anomaly(figure_asset_id=asset.asset_id),
    })
    successor = successor.model_copy(update=successor.next_version())
    store.revise(successor, expected_version=current.version)
    shot = store.load(Shot, "shot-1", episode_id="ep1")

    request = compose_render_request(
        store,
        shot_id=shot.shot_id,
        episode_id=shot.episode_id,
        run_id="asset-scene-build",
        mode="master",
    )
    assert request.asset_bindings[asset.asset_id].checksum == asset.checksum, (
        "initial composition did not bind the registered figure bytes; "
        f"observed {request.asset_bindings!r}, registered checksum={asset.checksum!r}"
    )

    settings = parse_settings({"project_root": str(root.path)})
    adapter = BlenderAdapter(root, settings)
    job = Job(
        job_id="asset-scene-build-job",
        type="build_scene",
        shot_id=shot.shot_id,
        episode_id=shot.episode_id,
        run_id=request.run_id,
    )
    # assemble_scene is the real persistence path and performs no Blender launch/GPU work.
    assembled = adapter.assemble_scene(request, job)
    artifact_path = root.resolve(assembled.scene_path)
    artifact = json.loads(root.read_text(assembled.scene_path, encoding="utf-8"))
    plan = artifact["plan"]
    scene_objects = plan["scene"]["objects"]
    figure_objects = [item for item in scene_objects if item.get("name") == "FR20AnomalyFigure"]
    assert len(figure_objects) == 1, (
        "persisted asset-backed scene did not contain exactly one registered figure; "
        f"observed {[item.get('name') for item in scene_objects]!r}"
    )
    figure = figure_objects[0]
    assert figure.get("asset_id") == asset.asset_id, (
        f"persisted figure references wrong registered asset; observed {figure.get('asset_id')!r}"
    )
    assert figure.get("asset_checksum") == asset.checksum, (
        "persisted figure checksum did not match the registered asset; "
        f"observed {figure.get('asset_checksum')!r}, expected {asset.checksum!r}"
    )
    assert figure.get("material") is None, (
        f"persisted asset-backed figure overrode its imported material; observed {figure.get('material')!r}"
    )

    store.append_shot_output(
        ShotOutput(
            output_id="asset-scene-roundtrip",
            episode_id=shot.episode_id,
            shot_id=shot.shot_id,
            record_version=shot.version,
            kind=ShotOutputKind.SCENE,
            relative_path=assembled.scene_path,
            content_sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        ),
        expected_version=0,
    )

    reopened_request = compose_render_request(
        ProjectStore(ProjectRoot(root.path)),
        shot_id=shot.shot_id,
        episode_id=shot.episode_id,
        run_id="asset-scene-render",
        mode="master",
        require_built_scene=True,
    )
    recomposed_figure = next(
        item for item in reopened_request.scene.objects if item.name == "FR20AnomalyFigure"
    )
    assert recomposed_figure.material is None, (
        "built-scene recomposition did not preserve the imported-material None value; "
        f"observed {recomposed_figure.material!r}"
    )
    python_payload = reopened_request.model_dump(mode="python")
    json_payload = json.loads(reopened_request.model_dump_json())
    python_reopened = RenderRequest.model_validate(python_payload)
    json_reopened = RenderRequest.model_validate(json_payload)

    for label, candidate in (("python", python_reopened), ("json", json_reopened)):
        imported = next(item for item in candidate.scene.objects if item.name == "FR20AnomalyFigure")
        assert imported.material is None, (
            f"full RenderRequest {label} revalidation lost material=None; "
            f"observed {imported.material!r}"
        )
        assert imported.asset_id == asset.asset_id, (
            f"full RenderRequest {label} revalidation lost registered asset id; "
            f"observed {imported.asset_id!r}"
        )
        assert candidate.asset_bindings[asset.asset_id].checksum == asset.checksum, (
            f"full RenderRequest {label} revalidation lost asset binding checksum; "
            f"observed {candidate.asset_bindings!r}"
        )
