"""Manifest composition and receipt persistence for FR-09 episode cuts."""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from typing import Protocol

from orchestrator.adapters.postprofile.profile import PostProfileSpec
from orchestrator.adapters.timeline.runner import TimelineAssemblyResult
from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import (
    Episode,
    EpisodeOutput,
    Script,
    Shot,
    ShotOutput,
    ShotOutputKind,
    Timeline,
    TimelineEntry,
)
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.service.compose import compose_post_profile_spec
from orchestrator.store.json_store import ProjectStore, RecordNotFound


class TimelineEncoder(Protocol):
    def run(
        self,
        *,
        timeline: Timeline,
        shot_outputs: dict[str, ShotOutput],
        profile: PostProfileSpec,
        output_relative: str,
        log_relative: str,
        font_relative: str | None = None,
        budget_seconds: float | None = None,
    ) -> TimelineAssemblyResult: ...


def compose_timeline(store: ProjectStore, *, episode_id: str) -> Timeline:
    """Build an ordered shot-only cut from the current Episode and Script manifests."""
    episode = store.load(Episode, episode_id)
    shot_ids = tuple(episode.shot_ids)
    if not shot_ids:
        raise _invalid(f"Episode {episode_id!r} has no shots to assemble")
    if len(set(shot_ids)) != len(shot_ids):
        raise _invalid(f"Episode {episode_id!r} contains duplicate shot ids")

    ordered_ids = _ordered_shots(store, episode, shot_ids)
    entries: list[TimelineEntry] = []
    cursor = 0.0
    for shot_id in ordered_ids:
        shot = store.load(Shot, shot_id, episode_id=episode_id)
        if shot.status is not ShotStatus.APPROVED:
            raise _invalid(
                f"Shot {shot_id!r} is {shot.status.value}; FR-09 only assembles approved shots"
            )
        _delivered_output(store, shot)
        entries.append(TimelineEntry(
            type="shot", ref=shot_id, start_sec=cursor, duration_sec=shot.duration_sec,
        ))
        cursor += shot.duration_sec
    return Timeline(episode_id=episode_id, entries=entries)


def assemble_episode(
    store: ProjectStore,
    encoder: TimelineEncoder,
    *,
    episode_id: str,
    timeline: Timeline | None = None,
    run_id: str | None = None,
    budget_seconds: float | None = None,
) -> EpisodeOutput:
    """Persist the cut manifest, encode it, then append a verified episode receipt."""
    episode = store.load(Episode, episode_id)
    cut = timeline or compose_timeline(store, episode_id=episode_id)
    cut = Timeline.model_validate(cut.model_dump(mode="python"))
    if cut.episode_id != episode_id:
        raise _invalid(
            f"Timeline episode_id {cut.episode_id!r} does not match requested episode {episode_id!r}"
        )
    _validate_timeline_shape(cut)
    outputs = _outputs_for_timeline(store, episode, cut)
    profile = compose_post_profile_spec(store)
    font_relative = (
        profile.identity.overlay.font_relative
        if profile.identity.overlay is not None else None
    )

    try:
        persisted, timeline_token = store.load_with_version(Timeline, episode_id)
    except RecordNotFound:
        persisted, timeline_token = None, 0
    if persisted is None or persisted.model_dump(mode="json") != cut.model_dump(mode="json"):
        timeline_token = store.save(cut, expected_version=timeline_token)

    output_id = uuid.uuid4().hex
    run_id = run_id or uuid.uuid4().hex
    output_relative = f"episodes/{episode_id}/outputs/cuts/{output_id}.mp4"
    log_relative = f"episodes/{episode_id}/outputs/cuts/{output_id}-{run_id}.ffmpeg.log"
    result = encoder.run(
        timeline=cut,
        shot_outputs=outputs,
        profile=profile,
        output_relative=output_relative,
        log_relative=log_relative,
        font_relative=font_relative,
        budget_seconds=budget_seconds,
    )
    if result.relative_path != output_relative:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"timeline encoder wrote {result.relative_path!r}; expected {output_relative!r}",
        )
    receipt = EpisodeOutput(
        output_id=output_id,
        episode_id=episode_id,
        timeline_store_version=timeline_token,
        timeline_sha256=cut.content_sha256,
        relative_path=result.relative_path,
        content_sha256=result.content_sha256,
        delivery_profile=profile.identity.name,
        storage_resolution=profile.storage_resolution,
        output_fps=profile.output_fps,
        sample_aspect_ratio=profile.sample_aspect_ratio,
        ffmpeg_identity=normalize_ffmpeg_identity(result.ffmpeg_identity),
    )
    existing = store.list_episode_outputs(episode_id)
    store.append_episode_output(receipt, expected_version=len(existing))
    return receipt


def _outputs_for_timeline(
    store: ProjectStore,
    episode: Episode,
    timeline: Timeline,
) -> dict[str, ShotOutput]:
    episode_shots = set(episode.shot_ids)
    outputs: dict[str, ShotOutput] = {}
    for entry in timeline.entries:
        if entry.type == "shot":
            if not entry.ref:
                raise _invalid("Timeline shot entry is missing its shot reference")
            if entry.ref not in episode_shots:
                raise _invalid(f"Timeline refers to Shot {entry.ref!r}, which is not in the Episode")
            shot = store.load(Shot, entry.ref, episode_id=episode.episode_id)
            if shot.status is not ShotStatus.APPROVED:
                raise _invalid(
                    f"Shot {shot.shot_id!r} is {shot.status.value}; FR-09 only assembles approved shots"
                )
            outputs[shot.shot_id] = _delivered_output(store, shot)
        elif entry.type not in {"card", "pause"}:
            raise _invalid(f"Timeline entry type {entry.type!r} is unsupported")
    if not timeline.entries:
        raise _invalid(f"Timeline for episode {episode.episode_id!r} has no entries")
    return outputs


def _validate_timeline_shape(timeline: Timeline) -> None:
    if not timeline.entries:
        raise _invalid(f"Timeline for episode {timeline.episode_id!r} has no entries")
    if timeline.audio_tracks:
        raise _invalid("Timeline.audio_tracks is out of scope; episode audio is planned for Phase 2")
    cursor = 0.0
    for entry in timeline.entries:
        if not math.isfinite(entry.start_sec) or not math.isfinite(entry.duration_sec):
            raise _invalid("Timeline entry start and duration must be finite numbers")
        if entry.start_sec < cursor:
            raise _invalid("Timeline entries overlap or are out of order")
        if entry.type == "shot" and not entry.ref:
            raise _invalid("Timeline shot entry is missing its shot reference")
        if entry.type == "card":
            if not entry.ref:
                raise _invalid("Timeline card entry must store its text in ref")
            if any(ord(char) < 32 or ord(char) == 127 for char in entry.ref):
                raise _invalid("Timeline card text must be a single printable line")
        if entry.type == "pause" and entry.ref is not None:
            raise _invalid("Timeline pause entry must not carry a reference")
        if entry.type not in {"shot", "card", "pause"}:
            raise _invalid(f"Timeline entry type {entry.type!r} is unsupported")
        cursor = entry.start_sec + entry.duration_sec


def _delivered_output(store: ProjectStore, shot: Shot) -> ShotOutput:
    delivered = store.list_shot_outputs(
        shot.shot_id,
        episode_id=shot.episode_id,
        record_version=shot.version,
        kind=ShotOutputKind.DEGRADED,
    )
    if not delivered:
        raise _invalid(
            f"Shot {shot.shot_id!r} has no degraded output; run degrade_media first"
        )
    return delivered[-1]


def _ordered_shots(store: ProjectStore, episode: Episode, shot_ids: Sequence[str]) -> tuple[str, ...]:
    scripts = [
        store.load(Script, script_id, episode_id=episode.episode_id)
        for script_id in store.ids(Script, episode_id=episode.episode_id)
    ]
    candidates: list[tuple[Script, tuple[str, ...]]] = []
    known = set(shot_ids)
    for script in scripts:
        if not script.beat_to_shot:
            continue
        beat_ids = {beat.beat_id for beat in script.beats}
        unknown_beats = set(script.beat_to_shot) - beat_ids
        if unknown_beats:
            raise _invalid(
                f"Script {script.script_id!r} maps unknown beat(s): {', '.join(sorted(unknown_beats))}"
            )
        mapped = tuple(
            script.beat_to_shot[beat.beat_id]
            for beat in script.beats
            if beat.beat_id in script.beat_to_shot
        )
        outside = set(mapped) - known
        if outside:
            raise _invalid(
                f"Script {script.script_id!r} maps to Shot(s) outside Episode {episode.episode_id!r}: "
                + ", ".join(sorted(outside))
            )
        if mapped:
            # Keep each episode shot once: a beat can refer back to the same shot.
            unique = tuple(dict.fromkeys(mapped))
            candidates.append((script, unique + tuple(s for s in shot_ids if s not in unique)))
    if not candidates:
        return tuple(shot_ids)

    approved = [row for row in candidates if row[0].approval_review_id is not None]
    chosen = approved if approved else candidates
    orderings = {ordering for _script, ordering in chosen}
    if len(orderings) > 1:
        names = ", ".join(sorted(script.script_id for script, _ in chosen))
        raise _invalid(f"Episode {episode.episode_id!r} has ambiguous Script beat ordering: {names}")
    return next(iter(orderings))


def _invalid(message: str) -> JobError:
    return JobError(JobErrorKind.INVALID_MANIFEST, message)
