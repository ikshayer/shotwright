"""Promoting an accepted H3 candidate to a production `ShotOutput`.

Written from `orchestrator/adapters/minimax_h3/H3_PROMOTION_CONTRACT.md`, whose section 5
lists what a blind test author should attack. Each test here is one of those attacks.

The load-bearing property is R5: a promoted H3 output must never be re-ingestible by H3.
`compose_h3_request` accepts `MASTER` or `DEGRADED`, so the new `GENERATED` kind is
excluded by construction -- but "by construction" is how undriven invariants die, so it is
tested behaviourally through `compose_h3_request` rather than by asserting a set literal.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from orchestrator.adapters.minimax_h3.core import H3RequestError
from orchestrator.domain.entities import (
    Episode,
    H3Candidate,
    Review,
    Shot,
    ShotOutput,
    ShotOutputKind,
    ShotStatus,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.service.minimax_h3 import (
    H3_ENGINE_ID,
    H3GenerateBody,
    compose_h3_request,
    promote_h3_candidate,
)
from orchestrator.store.json_store import ProjectStore

AV_BYTES = b"not really an mp4, but its digest is what is checked"
REPORT = {
    "server_ready": {"system_stats": {"system": {"comfyui_version": "0.44.0"}}},
    "runtime": {"source_receipt": {"commit": "2c75dc1b379a59e0880619d029607af5169a62ef"}},
}
RUN = "h3run1"
AV_REL = f"generated/h3/s1/v0001/{RUN}/candidate.mp4"
REPORT_REL = f"generated/h3/s1/v0001/{RUN}/report.json"


def _sha(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _setup(tmp_path, *, experimental=False, report=None, av=AV_BYTES):
    """A store holding one H3 candidate whose artifacts are on disk, plus an approving Review."""
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    payload = json.dumps(REPORT if report is None else report).encode("utf-8")
    root.write_bytes(AV_REL, av)
    root.write_bytes(REPORT_REL, payload)
    candidate = H3Candidate(
        candidate_id=RUN, episode_id="ep1", shot_id="s1", shot_version=1,
        job_id="j1", run_id=RUN, source_output_id="master1",
        source_path="renders/s1/v0001/master.mkv", source_sha256="a" * 64,
        prompt_sha256="b" * 64, seed=7,
        av_path=AV_REL, av_sha256=_sha(av),
        wav_path=f"generated/h3/s1/v0001/{RUN}/audio.wav", wav_sha256="c" * 64,
        report_path=REPORT_REL, report_sha256=_sha(payload),
        frame_count=192, fps=24, audio_sample_rate=32000, audio_channels=2,
        audio_samples_per_channel=256000, experimental=experimental,
    )
    store.save(Episode(episode_id="ep1", project_id="p1", number=1,
                       duration_target_sec=60.0), expected_version=0)
    store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=8.0))
    store.save(candidate, expected_version=0)
    store.save(_review("rev1", RUN, ShotStatus.APPROVED), expected_version=0)
    return store, root, candidate


def _review(review_id, target_id, state, *, target_version=1):
    return Review(review_id=review_id, target_id=target_id, target_version=target_version,
                  target_kind="h3_candidate", episode_id="ep1", actor="creator",
                  created_at=datetime.now(UTC), approval_state=state)


# --- the promotion itself -----------------------------------------------------


def test_an_approved_candidate_is_promoted_to_a_generated_output(tmp_path):
    store, _root, candidate = _setup(tmp_path)
    output = promote_h3_candidate(store, candidate_id=RUN, review_id="rev1",
                                  expected_version=0)
    assert output.kind is ShotOutputKind.GENERATED
    assert output.relative_path == candidate.av_path
    assert output.content_sha256 == candidate.av_sha256
    assert output.record_version == candidate.shot_version


def test_engine_identity_comes_from_the_report_not_a_constant(tmp_path):
    store, _root, _candidate = _setup(tmp_path)
    output = promote_h3_candidate(store, candidate_id=RUN, review_id="rev1",
                                  expected_version=0)
    assert output.engine_id == H3_ENGINE_ID
    assert output.engine_build == (
        "comfyui 0.44.0+2c75dc1b379a59e0880619d029607af5169a62ef"
    )


def test_a_report_without_engine_provenance_is_refused(tmp_path):
    store, _root, _candidate = _setup(tmp_path, report={"runtime": {}})
    with pytest.raises(H3RequestError, match="ComfyUI version and source commit"):
        promote_h3_candidate(store, candidate_id=RUN, review_id="rev1", expected_version=0)


def test_the_candidate_record_is_unchanged_by_promotion(tmp_path):
    store, _root, candidate = _setup(tmp_path)
    before = store.load(H3Candidate, RUN).model_dump(mode="json")
    promote_h3_candidate(store, candidate_id=RUN, review_id="rev1", expected_version=0)
    assert store.load(H3Candidate, RUN).model_dump(mode="json") == before
    assert before == candidate.model_dump(mode="json")


# --- R1: approval ------------------------------------------------------------


def test_promotion_without_a_review_is_refused(tmp_path):
    store, _root, _candidate = _setup(tmp_path)
    with pytest.raises(H3RequestError, match="does not exist"):
        promote_h3_candidate(store, candidate_id=RUN, review_id="absent",
                             expected_version=0)


def test_a_review_approving_a_different_candidate_is_refused(tmp_path):
    store, _root, _candidate = _setup(tmp_path)
    store.save(_review("rev2", "some-other-run", ShotStatus.APPROVED), expected_version=0)
    with pytest.raises(H3RequestError, match="targets"):
        promote_h3_candidate(store, candidate_id=RUN, review_id="rev2", expected_version=0)


def test_a_rejecting_review_is_refused(tmp_path):
    store, _root, _candidate = _setup(tmp_path)
    store.save(_review("rev3", RUN, ShotStatus.REJECTED), expected_version=0)
    with pytest.raises(H3RequestError, match="not approval"):
        promote_h3_candidate(store, candidate_id=RUN, review_id="rev3", expected_version=0)


def test_a_review_for_another_shot_version_is_refused(tmp_path):
    store, _root, _candidate = _setup(tmp_path)
    store.save(_review("rev4", RUN, ShotStatus.APPROVED, target_version=2),
               expected_version=0)
    with pytest.raises(H3RequestError, match="version"):
        promote_h3_candidate(store, candidate_id=RUN, review_id="rev4", expected_version=0)


# --- R4: the bytes must still be the approved bytes ---------------------------


def test_a_candidate_whose_bytes_changed_after_generation_is_refused(tmp_path):
    store, root, candidate = _setup(tmp_path)
    root.write_bytes(candidate.av_path, AV_BYTES + b"tampered")
    with pytest.raises(H3RequestError, match="SHA256 differs"):
        promote_h3_candidate(store, candidate_id=RUN, review_id="rev1", expected_version=0)


def test_a_missing_candidate_artifact_is_refused(tmp_path):
    store, root, candidate = _setup(tmp_path)
    root.resolve(candidate.av_path).unlink()
    with pytest.raises(H3RequestError, match="missing or its SHA256 differs"):
        promote_h3_candidate(store, candidate_id=RUN, review_id="rev1", expected_version=0)


# --- idempotence by refusal ---------------------------------------------------


def test_double_promotion_is_refused_rather_than_replacing(tmp_path):
    store, _root, _candidate = _setup(tmp_path)
    promote_h3_candidate(store, candidate_id=RUN, review_id="rev1", expected_version=0)
    with pytest.raises(H3RequestError, match="already promoted"):
        promote_h3_candidate(store, candidate_id=RUN, review_id="rev1", expected_version=1)
    assert len(store.list_shot_outputs("s1", episode_id="ep1")) == 1


# --- D2: experimental stays a gate --------------------------------------------


def test_an_experimental_candidate_cannot_be_promoted(tmp_path):
    store, _root, _candidate = _setup(tmp_path, experimental=True)
    with pytest.raises(H3RequestError, match="experimental"):
        promote_h3_candidate(store, candidate_id=RUN, review_id="rev1", expected_version=0)


def test_an_absent_candidate_is_refused(tmp_path):
    store, _root, _candidate = _setup(tmp_path)
    with pytest.raises(H3RequestError, match="does not exist"):
        promote_h3_candidate(store, candidate_id="nope", review_id="rev1",
                             expected_version=0)


# --- R5: a promoted output must never be re-ingestible by H3 ------------------


def test_a_promoted_h3_output_cannot_be_fed_back_into_h3(tmp_path):
    """The compounding-artifact guard, exercised through the real composer."""
    store, _root, _candidate = _setup(tmp_path)
    promoted = promote_h3_candidate(store, candidate_id=RUN, review_id="rev1",
                                    expected_version=0)
    assert promoted.kind is ShotOutputKind.GENERATED

    _shot, token = store.load_with_version(Shot, "s1", episode_id="ep1")
    with pytest.raises(H3RequestError, match="MASTER or DEGRADED"):
        compose_h3_request(
            store, shot_id="s1", episode_id="ep1", run_id="h3run2",
            body=H3GenerateBody(
                expected_version=token, shot_version=1, video_path=promoted.relative_path,
                prompt="Feed the generated output straight back in.",
            ),
        )


def test_a_master_output_is_still_an_accepted_h3_source(tmp_path):
    """The R5 guard must refuse generated output without refusing legitimate sources."""
    store, root, _candidate = _setup(tmp_path)
    master_rel = "renders/s1/v0001/master.mkv"
    root.write_bytes(master_rel, b"a blender master")
    store.append_shot_output(
        ShotOutput(output_id="m1", episode_id="ep1", shot_id="s1", record_version=1,
                   kind=ShotOutputKind.MASTER, relative_path=master_rel,
                   content_sha256=_sha(b"a blender master"),
                   engine_id="BLENDER_EEVEE", engine_build="fbe6228777e7"),
        expected_version=0,
    )
    _shot, token = store.load_with_version(Shot, "s1", episode_id="ep1")
    request = compose_h3_request(
        store, shot_id="s1", episode_id="ep1", run_id="h3run3",
        body=H3GenerateBody(expected_version=token, shot_version=1,
                            video_path=master_rel, prompt="A legitimate source."),
    )
    assert request.video_path == master_rel
