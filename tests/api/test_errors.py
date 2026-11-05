"""The error-mapping table in CONTRACT.md, one table-driven suite.

Each exception is raised from a test-only route mounted on the real app, so the
app-level mapping is exercised independent of any particular business route.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.domain.entities import RubricScores
from orchestrator.domain.base import ShotStatus
from orchestrator.domain.state import ReviewApprovalError, TransitionError
from orchestrator.platform.containment import ContainmentError, UploadRejected
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.store.json_store import RecordNotFound, VersionConflict

from .conftest import LOOPBACK_BASE, TEST_ROUTE_PREFIX, TOKEN, add_raising_routes, find_key

_HEAD = RubricScores(
    mount_inferable=True,
    plausible_place=True,
    no_unmotivated_movement=True,
    stable_light_and_geometry=True,
    integrated_anomaly=False,
)
_STATUSES = list(ShotStatus)
_REFUSAL = "shot has no approved render for this exact version"

CASES: dict[str, tuple[Callable[[], BaseException], int]] = {
    "version_conflict": (lambda: VersionConflict("shots/s1", 3, 5, _HEAD), 409),
    "not_found": (lambda: RecordNotFound("shots/nope"), 404),
    "containment": (lambda: ContainmentError("../../etc/passwd", "escapes the project root"), 400),
    "upload_rejected": (lambda: UploadRejected("magic bytes do not match .png"), 400),
    "upload_oversize": (
        lambda: UploadRejected(
            "Upload exceeds max_image_bytes", limit_name="max_image_bytes", limit=1000, observed=2500
        ),
        400,
    ),
    "transition": (lambda: TransitionError(_STATUSES[0], _STATUSES[-1], _REFUSAL), 422),
    "review_approval": (lambda: ReviewApprovalError(_REFUSAL), 422),
    **{
        f"job_{k.value}": ((lambda k=k: JobError(k, f"boom {k.value}", job_id="j1")), 409 if k.retryable else 422)
        for k in JobErrorKind
    },
}


@pytest.fixture
def raising_app(app: FastAPI) -> FastAPI:
    add_raising_routes(app, {name: factory for name, (factory, _) in CASES.items()})
    return app


@pytest.fixture
def raising_client(raising_app: FastAPI) -> TestClient:
    return TestClient(raising_app, base_url=LOOPBACK_BASE, raise_server_exceptions=False)


def _get(c: TestClient, name: str):
    return c.get(f"{TEST_ROUTE_PREFIX}/raise/{name}")


@pytest.mark.parametrize("name", list(CASES))
def test_exception_maps_to_contract_status(raising_client: TestClient, name: str) -> None:
    resp = _get(raising_client, name)
    assert resp.status_code == CASES[name][1]
    assert resp.headers["content-type"].startswith("application/json")
    assert TOKEN not in resp.text


def test_error_bodies_share_one_shape(raising_client: TestClient) -> None:
    shapes = {name: frozenset(_get(raising_client, name).json()) for name in CASES}
    assert len(set(shapes.values())) == 1, shapes


def test_version_conflict_carries_expected_actual_and_head(raising_client: TestClient) -> None:
    body: Any = _get(raising_client, "version_conflict").json()
    assert 3 in find_key(body, lambda k: "expected" in k.lower())
    assert 5 in find_key(body, lambda k: "actual" in k.lower())
    heads = find_key(body, lambda k: "current" in k.lower() or "head" in k.lower())
    assert any(isinstance(h, dict) and h.get("integrated_anomaly") is False for h in heads), body


def test_record_not_found_names_the_record(raising_client: TestClient) -> None:
    assert "shots/nope" in _get(raising_client, "not_found").text


def test_containment_error_names_the_offending_path(raising_client: TestClient) -> None:
    assert "../../etc/passwd" in _get(raising_client, "containment").text


def test_transition_refusal_reason_is_verbatim(raising_client: TestClient) -> None:
    assert _REFUSAL in _get(raising_client, "transition").text
    assert _REFUSAL in _get(raising_client, "review_approval").text


@pytest.mark.parametrize("kind", list(JobErrorKind), ids=lambda k: k.value)
def test_job_error_remediation_survives_to_the_body(raising_client: TestClient, kind: JobErrorKind) -> None:
    body = _get(raising_client, f"job_{kind.value}").json()
    assert kind.value in find_key(body, lambda k: k == "kind"), body
    assert kind.retryable in find_key(body, lambda k: k == "retryable"), body
    hints = find_key(body, lambda k: "remediation" in k.lower())
    assert kind.remediation in hints, f"remediation hint flattened for {kind.value}: {body}"


def test_oversize_upload_reports_limit_and_observed(raising_client: TestClient) -> None:
    body = _get(raising_client, "upload_oversize").json()
    assert 1000 in find_key(body, lambda k: "limit" in k.lower() and "name" not in k.lower()), body
    assert 2500 in find_key(body, lambda k: "observed" in k.lower()), body
    assert "max_image_bytes" in _get(raising_client, "upload_oversize").text


def test_magic_byte_rejection_names_the_file_problem(raising_client: TestClient) -> None:
    assert "magic bytes" in _get(raising_client, "upload_rejected").text
