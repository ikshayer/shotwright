"""4.4 post-only vs rebuild-triggering, and 10.2 upstream invalidation."""

import pytest

from orchestrator.domain.base import Invalidation, ShotStatus
from orchestrator.domain.entities import CameraProfile, Shot
from orchestrator.domain.invalidation import (
    ProfileDiff,
    agent_overwrites_creator_fields,
    diff_camera_profile,
    field_invalidation,
    shots_invalidated_by,
    shots_invalidated_by_profile,
)

#: The 4.4 lists, one changed value per field.
POST_ONLY_CHANGES = {
    "codec": "mpeg4",
    "bitrate_kbps": 400,
    "storage_resolution": (320, 240),
    "display_aspect_ratio": "16:9",
    "fps": 8.0,
    "decimation_pattern": "3:2",
    "noise_model": {"luma": 4.0},
    "chroma_smearing": 0.5,
    "block_artifacts": 0.3,
    "sharpening": 0.2,
    "grade_white_balance": {"r": 1.1},
    "overlay_model": {"font": "mono"},
    "analog_artifacts": {"tracking": 0.1},
    "frame_drop_schedule": [{"type": "drop", "start_frame": 3, "count": 2}],
}
REBUILD_CHANGES = {
    "field_of_view_mm": 24.0,
    "barrel_distortion": 0.2,
    "exposure_ev": -1.0,
    "practical_light_color_k": 5600,
    "practical_light_intensity": 2.0,
    "monochrome_night": True,
    "camera_position": (1.0, 2.0, 3.0),
    "camera_mount": "ceiling-corner",
    "depth_of_field": 0.4,
}


def profile(**kw) -> CameraProfile:
    return CameraProfile(camera_id="CAM-1", profile_name="dvr-2005", **kw)


def shot(shot_id, status=ShotStatus.APPROVED, deps=(), **kw) -> Shot:
    if status is ShotStatus.APPROVED:
        kw.setdefault("approval_review_id", "rev-1")
    kw.setdefault("camera_id", "CAM-1")
    return Shot(
        shot_id=shot_id, episode_id="EP1", duration_sec=5.0,
        status=status, dependencies=list(deps), **kw,
    )


def test_tables_cover_every_camera_profile_field():
    # Guards the tables: a new CameraProfile field must be classified here too.
    own = set(CameraProfile.model_fields) - {
        "camera_id", "profile_name", "version", "supersedes", "schema_version",
    }
    assert set(POST_ONLY_CHANGES) | set(REBUILD_CHANGES) == own
    assert not set(POST_ONLY_CHANGES) & set(REBUILD_CHANGES)


@pytest.mark.parametrize("name,value", POST_ONLY_CHANGES.items())
def test_post_only_field_change_does_not_require_rebuild(name, value):
    d = diff_camera_profile(profile(), profile(**{name: value}))
    assert d.changed == (name,)
    assert d.post_only == (name,)
    assert not d.requires_rebuild
    assert field_invalidation(CameraProfile, name) is Invalidation.POST_ONLY


@pytest.mark.parametrize("name,value", REBUILD_CHANGES.items())
def test_rebuild_field_change_requires_rebuild(name, value):
    d = diff_camera_profile(profile(), profile(**{name: value}))
    assert d.rebuild == (name,)
    assert d.requires_rebuild
    assert field_invalidation(CameraProfile, name) is Invalidation.REBUILD


def test_mixed_change_requires_rebuild():
    d = diff_camera_profile(profile(), profile(codec="mpeg4", exposure_ev=1.0))
    assert d.post_only == ("codec",)
    assert d.rebuild == ("exposure_ev",)
    assert d.requires_rebuild


def test_identical_profiles_diff_to_nothing():
    d = diff_camera_profile(profile(), profile())
    assert d.changed == ()
    assert not d.requires_rebuild


def test_version_bump_alone_is_not_a_change():
    after = profile(**profile().next_version())
    assert diff_camera_profile(profile(), after).changed == ()


def test_diffing_different_cameras_is_refused():
    other = CameraProfile(camera_id="CAM-2", profile_name="dvr-2005")
    with pytest.raises(ValueError):
        diff_camera_profile(profile(), other)


def test_untagged_field_counts_as_rebuild_failsafe():
    d = ProfileDiff("CAM-1", untagged=("mystery",))
    assert d.requires_rebuild
    assert d.changed == ("mystery",)


def test_unknown_or_untagged_field_has_no_class():
    assert field_invalidation(CameraProfile, "no_such_field") is None
    assert field_invalidation(CameraProfile, "camera_id") is None
    assert field_invalidation(Shot, "duration_sec") is None


def test_untagged_diff_invalidates_approved_shots():
    r = shots_invalidated_by_profile(ProfileDiff("CAM-1", untagged=("mystery",)), [shot("A")])
    assert r.shot_ids == ("A",)


def test_post_only_profile_change_invalidates_nothing():
    d = diff_camera_profile(profile(), profile(codec="mpeg4"))
    r = shots_invalidated_by_profile(d, [shot("A"), shot("B")])
    assert r.shot_ids == ()
    assert not r.revokes_cut_approval


def test_rebuild_profile_change_invalidates_only_approved_shots():
    d = diff_camera_profile(profile(), profile(field_of_view_mm=30.0))
    shots = [
        shot("A"), shot("B", ShotStatus.REVIEW), shot("C", ShotStatus.DRAFT),
        shot("D", ShotStatus.STALE), shot("E"),
    ]
    r = shots_invalidated_by_profile(d, shots)
    assert r.shot_ids == ("A", "E")
    assert r.revokes_cut_approval
    assert "field_of_view_mm" in r.reason


def test_profile_change_only_invalidates_shots_on_that_camera():
    d = diff_camera_profile(profile(), profile(field_of_view_mm=30.0))
    shots = [shot("A"), shot("B", camera_id="CAM-2")]
    assert shots_invalidated_by_profile(d, shots).shot_ids == ("A",)


def test_no_profile_change_invalidates_nothing():
    r = shots_invalidated_by_profile(ProfileDiff("CAM-1"), [shot("A")])
    assert r.shot_ids == () and not r.revokes_cut_approval


def test_upstream_change_invalidates_only_approved_dependents():
    shots = [
        shot("A", deps=["loc-1"]),
        shot("B", ShotStatus.REVIEW, deps=["loc-1"]),
        shot("C", deps=["loc-2"]),
        shot("D", ShotStatus.STALE, deps=["loc-1"]),
        shot("E", ShotStatus.DRAFT, deps=["loc-1"]),
    ]
    r = shots_invalidated_by("loc-1", shots, "location edited")
    assert r.shot_ids == ("A",)
    assert r.reason == "location edited"


def test_shot_leaving_approved_revokes_cut_approval():
    r = shots_invalidated_by("loc-1", [shot("A", deps=["loc-1"])], "x")
    assert r.revokes_cut_approval


def test_nothing_invalidated_means_cut_approval_kept():
    r = shots_invalidated_by("loc-1", [shot("A", deps=["loc-2"])], "x")
    assert r.shot_ids == ()
    assert not r.revokes_cut_approval


def test_invalidation_retains_rather_than_deletes():
    shots = [shot("A", deps=["loc-1"])]
    r = shots_invalidated_by("loc-1", shots, "x")
    # A pure decision: inputs are untouched and the result only names ids.
    assert shots[0].status is ShotStatus.APPROVED
    assert shots[0].approval_review_id == "rev-1"
    assert all(isinstance(i, str) for i in r.shot_ids)


def test_creator_authored_conflict_is_reported_not_applied():
    s = shot("A", ShotStatus.DRAFT, framing="wide", creator_authored={"framing"})
    out = agent_overwrites_creator_fields(s, {"framing": "close", "narrative_beat": "x"})
    assert out == ("framing",)
    assert s.framing == "wide"


def test_agent_proposing_unchanged_creator_value_is_not_a_conflict():
    s = shot("A", ShotStatus.DRAFT, framing="wide", creator_authored={"framing"})
    assert agent_overwrites_creator_fields(s, {"framing": "wide"}) == ()


def test_agent_may_change_non_creator_fields():
    s = shot("A", ShotStatus.DRAFT, framing="wide")
    assert agent_overwrites_creator_fields(s, {"framing": "close"}) == ()


def test_conflicts_are_sorted_and_complete():
    s = shot("A", ShotStatus.DRAFT, framing="w", narrative_beat="n",
             creator_authored={"narrative_beat", "framing"})
    out = agent_overwrites_creator_fields(s, {"narrative_beat": "z", "framing": "c"})
    assert out == ("framing", "narrative_beat")
