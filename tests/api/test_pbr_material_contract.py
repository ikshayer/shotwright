"""Location API contract for nested PBR bindings and optimistic revisions."""
from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.domain.entities import Location
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore
from tests.service.test_asset_contract import _project_store

_TOKEN = "pbr-api-contract-token"


def test_location_api_accepts_nested_material_overrides_and_rejects_stale_revision(tmp_path):
    base, _ = _project_store(tmp_path / "project")
    app = create_app(settings=Settings(project_root=base.path.parent), token=_TOKEN)
    client = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    headers = {"X-Loopback-Token": _TOKEN}
    location = {
        "location_id": "api-pbr-room",
        "material_overrides": {"WallPlaster": {
            "maps": {"base_color": "wall-albedo", "normal_gl": "wall-normal"},
            "uv_scale": [2.0, 3.0], "normal_strength": 0.6, "ao_strength": 0.0,
        }},
    }
    created = client.post("/projects/p1/locations/api-pbr-room", headers=headers, json={"location": location})
    assert created.status_code == 200, f"nested Location create failed: {created.status_code} {created.text}"
    persisted = ProjectStore(ProjectRoot(base.path)).load(Location, "api-pbr-room")
    assert persisted.material_overrides["WallPlaster"].maps["normal_gl"] == "wall-normal", (
        f"nested PBR role did not persist: {persisted.material_overrides!r}"
    )
    revised = {**location, "version": 2, "supersedes": 1, "material_overrides": {"WallPlaster": {
        "maps": {"base_color": "wall-albedo"}, "uv_scale": [1.0, 1.0],
        "normal_strength": 1.0, "ao_strength": 0.0,
    }}}
    body = {"location": revised, "expected_version": 1, "episode_ids": ["ep1"]}
    ok = client.post("/projects/p1/locations/api-pbr-room/revise", headers=headers, json=body)
    assert ok.status_code == 200, f"nested Location revision failed: {ok.status_code} {ok.text}"
    stale = client.post("/projects/p1/locations/api-pbr-room/revise", headers=headers, json=body)
    assert stale.status_code == 409, f"stale expected_version did not conflict: {stale.status_code} {stale.text}"
