"""A pre-ntsc-rs camera journal can be replayed without rewriting its evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from orchestrator.domain.entities import CameraProfile
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.dependencies import (
    resume_invalidations,
    revise_camera_profile,
    validated_cascades,
)
from orchestrator.store.json_store import ProjectStore


def _write_json(root: ProjectRoot, path: str, payload: dict[str, Any]) -> None:
    root.write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _files(root: ProjectRoot) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.path.rglob("*.json")}


def _legacy_camera_payload(
    schema_version: int, legacy_analog: dict[str, float],
) -> dict[str, Any]:
    payload = CameraProfile(
        camera_id="hall", profile_name="CCTV", camera_position=(1.0, 2.0, 3.0),
        storage_resolution=(352, 288),
    ).model_dump(mode="json")
    payload.update(
        schema_version=schema_version,
        analog_artifacts=legacy_analog,
        overlay_model=dict.fromkeys((
            "camera_id_text", "clock_epoch", "clock_format", "fontsize", "x", "y", "placement",
        )),
    )
    return payload


def _legacy_pending(
    root: ProjectRoot, *, successor_written: bool, legacy_analog: dict[str, float],
    schema_version: int = 3,
) -> dict[str, Any]:
    before = _legacy_camera_payload(schema_version, legacy_analog)
    after = {**before, "version": 2, "supersedes": 1, "fps": 15.0}
    directory = "camera_profiles/hall"
    _write_json(root, f"{directory}/v0001.json", {
        **before, "_mutation_token": 1, "_predecessor_token": 0,
    })
    _write_json(root, f"{directory}/v0001.lifecycle.00000002.json", {
        **before, "_mutation_token": 2, "_predecessor_token": 1,
    })
    if successor_written:
        _write_json(root, f"{directory}/v0002.json", {
            **after, "_mutation_token": 3, "_predecessor_token": 2,
        })
    plan = {
        "cascade_id": uuid4().hex, "entity_type": "CameraProfile",
        "before": before, "after": after, "expected_version": 2,
        "episode_ids": [], "affected": [],
    }
    _write_json(root, f"_journal/cascades/{plan['cascade_id']}.json", plan)
    return plan


@pytest.mark.parametrize(("successor_written", "legacy_analog", "schema_version"), [
    pytest.param(False, {}, 3, id="crash-before-commit"),
    pytest.param(True, {}, 3, id="crash-after-commit"),
    pytest.param(False, {"roll": 0.1}, 3, id="legacy-flat-analog"),
    pytest.param(True, {"roll": 0.1}, 1, id="schema-v1-payload"),
])
def test_pending_legacy_camera_revision_resumes_and_accepts_a_new_revision(
    tmp_path: Path, successor_written: bool, legacy_analog: dict[str, float], schema_version: int,
) -> None:
    root = ProjectRoot(tmp_path)
    plan = _legacy_pending(
        root, successor_written=successor_written, legacy_analog=legacy_analog,
        schema_version=schema_version,
    )
    original = _files(root)

    store = ProjectStore(root)
    assert store.cascades(pending_only=True) == (plan,)
    assert validated_cascades(store) == (plan,)
    assert _files(root) == original
    assert resume_invalidations(store) == (plan["cascade_id"],)

    reopened = ProjectStore(root)
    assert resume_invalidations(reopened) == ()
    assert validated_cascades(reopened) == (plan,)
    before, before_token = reopened.load_with_version(CameraProfile, "hall", version=1)
    assert (before.version, before_token, before.fps) == (1, 2, plan["before"]["fps"])
    assert before.analog_artifacts.legacy_flat_values == (legacy_analog or None)
    head, token = reopened.load_with_version(CameraProfile, "hall")
    assert (head.version, head.supersedes, token, head.fps) == (2, 1, 3, 15.0)
    assert head.analog_artifacts == before.analog_artifacts
    assert head.storage_resolution == (352, 288)
    assert head.camera_position == (1.0, 2.0, 3.0)
    recovered = _files(root)
    assert all(recovered[path] == contents for path, contents in original.items())

    successor = head.model_copy(update={**head.next_version(), "fps": 20.0})
    assert revise_camera_profile(reopened, successor, expected_version=token) == ()
    final = ProjectStore(root)
    assert final.load_with_version(CameraProfile, "hall") == (successor, 4)
    assert final.versions(CameraProfile, "hall") == [1, 2, 3]
    assert len(validated_cascades(final)) == 2
    assert final.cascades(pending_only=True) == ()
    assert all(path.read_bytes() == contents for path, contents in recovered.items())


@pytest.mark.parametrize("schema_version", [1, 2])
def test_pre_v3_flat_analog_records_remain_readable_but_are_not_accepted_as_new_requests(
    tmp_path: Path, schema_version: int,
) -> None:
    root = ProjectRoot(tmp_path)
    legacy_analog = {"roll": 0.1}
    payload = _legacy_camera_payload(schema_version, legacy_analog)
    _write_json(root, "camera_profiles/hall/v0001.json", payload)
    original = _files(root)

    store = ProjectStore(root)
    head, token = store.load_with_version(CameraProfile, "hall")
    assert (head.version, token) == (1, 1)
    assert head.analog_artifacts.legacy_flat_values == legacy_analog
    assert _files(root) == original
    successor = head.model_copy(update={**head.next_version(), "fps": 20.0})
    assert revise_camera_profile(store, successor, expected_version=token) == ()
    reopened = ProjectStore(root)
    assert reopened.load_with_version(CameraProfile, "hall") == (successor, 2)
    assert len(validated_cascades(reopened)) == 1
    assert all(path.read_bytes() == contents for path, contents in original.items())

    with pytest.raises(ValidationError, match="analog_artifacts"):
        CameraProfile.model_validate(payload)


@pytest.mark.parametrize("damage", ["before-payload", "mutation-token"])
def test_legacy_camera_recovery_still_rejects_mismatched_history(tmp_path: Path, damage: str) -> None:
    root = ProjectRoot(tmp_path)
    plan = _legacy_pending(root, successor_written=False, legacy_analog={})
    if damage == "before-payload":
        plan["before"]["camera_position"] = [1.0, 2.0, 4.0]
    else:
        plan["expected_version"] = 1  # The content version, no longer the head mutation token.
    _write_json(root, f"_journal/cascades/{plan['cascade_id']}.json", plan)
    original = _files(root)

    store = ProjectStore(root)
    assert store.cascades(pending_only=True) == (plan,)
    with pytest.raises(JobError) as caught:
        resume_invalidations(store)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "planned before payload or mutation token" in str(caught.value)
    assert _files(root) == original
