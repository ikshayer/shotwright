"""Manifest composition and receipt persistence for FR-09 episode cuts."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from PIL import ImageFont

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
from orchestrator.service.credits import collect_credits_for_shots
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict


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
        card_text_files: dict[int, tuple[str, int]] | None = None,
        narrative_duration_sec: float | None = None,
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
        _require_assembly_status(shot)
        delivered = _delivered_output(store, shot)
        if delivered.frame_count is None or delivered.output_fps is None:
            raise _invalid(
                f"Shot {shot.shot_id!r} degraded output has no delivered frame timing; "
                "run degrade_media again"
            )
        duration_sec = delivered.frame_count / delivered.output_fps
        entries.append(TimelineEntry(
            type="shot", ref=shot_id, start_sec=cursor, duration_sec=duration_sec,
        ))
        cursor += duration_sec
    return Timeline(episode_id=episode_id, entries=entries)


def assemble_episode(
    store: ProjectStore,
    encoder: TimelineEncoder,
    *,
    episode_id: str,
    timeline: Timeline | None = None,
    run_id: str | None = None,
    budget_seconds: float | None = None,
    credit_card_duration_sec: float = 5.0,
    output_id: str | None = None,
) -> EpisodeOutput:
    """Persist the narrative cut and rights snapshot, encode appended credit cards, then receipt."""
    episode, episode_token = store.load_with_version(Episode, episode_id)
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
    # Five seconds is a creator-facing presentation default, not a measured runtime limit.
    if not math.isfinite(credit_card_duration_sec) or credit_card_duration_sec <= 0:
        raise _invalid("credit_card_duration_sec must be finite and positive")

    # Keep the persisted Timeline narrative-only. Credit cards are an adapter render
    # extension over the exact selected Shot versions and never mutate that source.
    selected_ids = tuple(dict.fromkeys(
        entry.ref for entry in cut.entries if entry.type == "shot" and entry.ref
    ))
    selected_shots: list[Shot] = []
    for shot_id in selected_ids:
        delivered = outputs.get(shot_id)
        if delivered is None or delivered.kind is not ShotOutputKind.DEGRADED:
            raise _invalid(f"Timeline Shot {shot_id!r} has no pinned degraded receipt")
        selected_shots.append(store.load(
            Shot, shot_id, episode_id=episode_id, version=delivered.record_version,
        ))
    credits = collect_credits_for_shots(store, selected_shots)
    if credits["publication_blockers"]:
        raise _invalid("episode has publication-blocking rights metadata: " + "; ".join(credits["publication_blockers"]))

    # A queued job passes its own id so its caller can find exactly this cut.
    output_id = output_id or uuid.uuid4().hex
    run_id = run_id or uuid.uuid4().hex
    output_relative = f"episodes/{episode_id}/outputs/cuts/{output_id}.mp4"
    log_relative = f"episodes/{episode_id}/outputs/cuts/{output_id}-{run_id}.ffmpeg.log"
    render_cut = cut
    card_text_files: dict[int, tuple[str, int]] = {}
    credit_manifest_path: str | None = None
    credit_manifest_sha256: str | None = None
    generated_cards: list[dict[str, object]] = []
    credit_duration = 0.0
    required_entries = [row for row in credits["entries"] if row["publication_required"]]
    if required_entries:
        if not font_relative:
            raise _invalid("CC-BY-4.0 credit cards require a project-local delivery font")
        font_path = store._root.resolve(font_relative)
        cards = _credit_cards(required_entries, font_path, profile.storage_resolution)
        cursor = cut.total_duration_sec
        entries = list(cut.entries)
        for ordinal, (card_text, asset_ids, fontsize) in enumerate(cards, start=1):
            index = len(entries)
            relative_text_path = (
                f"episodes/{episode_id}/outputs/cuts/{output_id}.credit-card-{ordinal:03d}.txt"
            )
            store._root.write_text(relative_text_path, card_text)
            entries.append(TimelineEntry(
                type="card", ref=card_text, start_sec=cursor,
                duration_sec=credit_card_duration_sec,
            ))
            card_text_files[index] = (relative_text_path, fontsize)
            generated_cards.append({
                "index": index, "text_path": relative_text_path,
                "text_sha256": hashlib.sha256(card_text.encode("utf-8")).hexdigest(),
                "asset_ids": asset_ids, "duration_sec": credit_card_duration_sec,
                "fontsize": fontsize,
            })
            cursor += credit_card_duration_sec
        credit_duration = len(cards) * credit_card_duration_sec
        render_cut = Timeline(episode_id=episode_id, entries=entries)

    if credits["entries"] or credits["publication_blockers"]:
        credit_manifest_path = f"episodes/{episode_id}/outputs/cuts/{output_id}.credits.json"
        plain_text_path = f"episodes/{episode_id}/outputs/cuts/{output_id}.credits.txt"
        snapshot = {
            **credits,
            "narrative_duration_sec": cut.total_duration_sec,
            "credit_duration_sec": credit_duration,
            "total_duration_sec": cut.total_duration_sec + credit_duration,
            "generated_cards": generated_cards,
            "plain_text_path": plain_text_path,
        }
        snapshot_bytes = (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        # Snapshot and companion full attribution text are contained writes and precede
        # output receipt commitment. If later encoding fails, these retained artifacts
        # describe that attempted cut and no earlier receipt is replaced.
        store._root.write_bytes(credit_manifest_path, snapshot_bytes)
        store._root.write_text(plain_text_path, credits["plain_text"])
        credit_manifest_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()

    with store.command():
        current_episode, current_episode_token = store.load_with_version(Episode, episode_id)
        if current_episode_token != episode_token or current_episode != episode:
            raise VersionConflict(
                f"episodes/{episode_id}/episode.json", episode_token,
                current_episode_token, current_episode,
            )
        try:
            persisted, timeline_token = store.load_with_version(Timeline, episode_id)
        except RecordNotFound:
            persisted, timeline_token = None, 0
        timeline_changed = (
            persisted is None
            or persisted.model_dump(mode="json") != cut.model_dump(mode="json")
        )
        if timeline_changed:
            timeline_token = store.save(cut, expected_version=timeline_token)
        approval_is_stale = (
            current_episode.approved_cut_version is not None
            or current_episode.approved_cut_output_id is not None
            or current_episode.approval_review_id is not None
        ) and current_episode.approved_cut_version != timeline_token
        if timeline_changed or approval_is_stale:
            store.save(
                current_episode.model_copy(update={
                    "approved_cut_version": None,
                    "approved_cut_output_id": None,
                    "approval_review_id": None,
                }),
                expected_version=current_episode_token,
            )

    encode_kwargs = {
        "timeline": render_cut,
        "shot_outputs": outputs,
        "profile": profile,
        "output_relative": output_relative,
        "log_relative": log_relative,
        "font_relative": font_relative,
        "budget_seconds": budget_seconds,
    }
    if card_text_files:
        encode_kwargs["card_text_files"] = card_text_files
        encode_kwargs["narrative_duration_sec"] = cut.total_duration_sec
    result = encoder.run(**encode_kwargs)
    if result.relative_path != output_relative:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"timeline encoder wrote {result.relative_path!r}; expected {output_relative!r}",
        )
    receipt_values = {
        "output_id": output_id,
        "episode_id": episode_id,
        "timeline_store_version": timeline_token,
        "timeline_sha256": cut.content_sha256,
        "relative_path": result.relative_path,
        "content_sha256": result.content_sha256,
        "delivery_profile": profile.identity.name,
        "storage_resolution": profile.storage_resolution,
        "output_fps": profile.output_fps,
        "sample_aspect_ratio": profile.sample_aspect_ratio,
        "ffmpeg_identity": normalize_ffmpeg_identity(result.ffmpeg_identity),
    }
    if "credit_manifest_path" in EpisodeOutput.model_fields:
        receipt_values["credit_manifest_path"] = credit_manifest_path
        receipt_values["credit_manifest_sha256"] = credit_manifest_sha256
    receipt = EpisodeOutput(**receipt_values)
    with store.command():
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
            _require_assembly_status(shot)
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
                raise _invalid("Narrative Timeline card text must be a single printable line")
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


def _require_assembly_status(shot: Shot) -> None:
    if shot.status in {ShotStatus.REVIEW, ShotStatus.APPROVED}:
        return
    next_steps = {
        ShotStatus.DRAFT: "run build_scene, render_master, and degrade_media",
        ShotStatus.PLANNED: "finish build_scene, render_master, and degrade_media",
        ShotStatus.BLOCKING: "approve framing if required, then run render_master and degrade_media",
        ShotStatus.BUILDING: "finish render_master, then run degrade_media",
        ShotStatus.STALE: "revise the Shot, then run build_scene, render_master, and degrade_media",
        ShotStatus.REJECTED: "revise the Shot, then run build_scene, render_master, and degrade_media",
        ShotStatus.FAILED: "retry the failed stage, then run degrade_media",
    }
    remediation = next_steps.get(shot.status, "run render_master and degrade_media")
    raise _invalid(
        f"Shot {shot.shot_id!r} is {shot.status.value}; {remediation} to make it eligible "
        "for cut assembly (review or approved with a current degraded output)"
    )


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


def _credit_cards(
    entries: list[dict[str, object]],
    font_path: Path,
    resolution: tuple[int, int],
) -> list[tuple[str, list[str], int]]:
    """Wrap complete attribution blocks using measured delivery-font bounds."""
    width, height = resolution
    if width <= 32 or height <= 32:
        raise _invalid("delivery resolution is too small for a visible credit card")
    fontsize = max(12, min(16, width // 24))
    font = ImageFont.truetype(str(font_path), fontsize)
    max_width = width - 32
    ascent, descent = font.getmetrics()
    line_height = max(ascent + descent, fontsize) + 3
    max_lines = (height - 32) // line_height
    if max_lines < 1:
        raise _invalid("delivery font does not fit vertically inside the credit-card frame")

    def measured_width(value: str) -> int:
        left, _top, right, _bottom = font.getbbox(value)
        return right - left

    def wrap_line(value: str) -> list[str]:
        if not value:
            return [""]
        words = value.split()
        output: list[str] = []
        current = ""
        for word in words:
            chunks: list[str] = []
            piece = ""
            for char in word:
                candidate = piece + char
                if measured_width(candidate) > max_width and piece:
                    chunks.append(piece)
                    piece = char
                else:
                    piece = candidate
            if piece:
                if measured_width(piece) > max_width:
                    raise _invalid("a credit-card glyph is wider than the delivery frame")
                chunks.append(piece)
            for chunk in chunks:
                candidate = f"{current} {chunk}".strip()
                if current and measured_width(candidate) > max_width:
                    output.append(current)
                    current = chunk
                else:
                    current = candidate
        if current:
            output.append(current)
        return output

    cards: list[tuple[str, list[str], int]] = []
    for entry in entries:
        block = [
            str(entry["attribution"]),
            f"Source: {entry['source_url']}",
            f"License: {entry['license']}",
            f"{entry['license_url']}",
        ]
        lines = [wrapped for row in block for wrapped in wrap_line(row)]
        pages = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        for page in pages:
            cards.append(("\n".join(page), [str(entry["asset_id"])], fontsize))
    return cards
