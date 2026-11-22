"""Project-local JSON store: the source of truth (10.0, invariant 1).

One orchestrator process is the single writer. Concurrency is optimistic: every
mutation carries `expected_version` (create implies 0), and a mismatch raises
`VersionConflict`, which the API layer turns into HTTP 409.

Layout (10.4). Versioned records are append-only, so each version is its own file
and "the current record" is the highest version on disk. There is deliberately no
head pointer or combined manifest to keep in sync with the version files:

    project.json
    assets/registry/<asset_id>.json
    assets/models/<asset_id>/<filename>
    episodes/<episode_id>/episode.json
    episodes/<episode_id>/edits/timeline.json
    episodes/<episode_id>/scripts/<script_id>/v0001.json
    episodes/<episode_id>/shots/<shot_id>/v0001.json
    locations/<location_id>/v0001.json
    camera_profiles/<camera_id>/v0001.json
    references/style_bible/v0001.json

Every write goes through `ProjectRoot` (FR-25). Stored JSON holds only
project-relative references, so a copied folder reopens standalone (AT-09).

Unowned here (service layer): cascading staleness on upstream change, Episode
gate-C revocation, and verifying an approval_review_id names a real approving Review.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import ValidationError

from orchestrator.domain.base import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    Entity,
    FramingStatus,
    Invalidation,
    ShotStatus,
    VersionedEntity,
)
from orchestrator.domain.entities import (
    Asset,
    AudioPostProfile,
    CameraProfile,
    Episode,
    EpisodeOutput,
    Export,
    GenerationRun,
    H3Candidate,
    Location,
    PostProfile,
    Project,
    Review,
    Script,
    Shot,
    ShotOutput,
    ShotOutputKind,
    StyleBible,
    Timeline,
)
from orchestrator.domain.invalidation import field_invalidation
from orchestrator.domain.state import (
    RevisionError,
    TransitionContext,
    TransitionError,
    check_revision,
    check_transition,
)
from orchestrator.platform.containment import ContainmentError, ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind

E = TypeVar("E", bound=Entity)

_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
_VERSION_FILE = re.compile(r"^v(\d{4,})\.json$")
_LIFECYCLE_FILE = re.compile(r"^v(\d{4,})\.lifecycle\.(\d{8,})\.json$")
_SHOT_OUTPUT_FILE = re.compile(r"^o(\d{8,})-([A-Za-z0-9_-]+)\.json$")
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_S = 0.02
_LIFECYCLE_FIELDS = frozenset({
    "status", "approval_review_id", "framing_status", "approval_notes",
    # FR-29 panel refs change in the pending compare loop; profile pin and authoring
    # markers change atomically with accepted framing.
    "framing_panel_ref", "creator_authored", "camera_profile_version",
})
# Only declared filesystem references are paths; prose and identifiers are not.
_PATH_FIELDS: dict[type[Entity], tuple[str, ...]] = {
    Asset: ("file_path",),
    Location: ("blend_path",),
    Shot: ("audio_refs", "framing_panel_ref"),
    Timeline: ("audio_tracks",),
    EpisodeOutput: ("relative_path", "credit_manifest_path"),
    Export: ("output_path",),
    H3Candidate: ("source_path", "av_path", "wav_path", "report_path"),
    GenerationRun: ("input_paths", "output_paths", "log_path"),
}


class VersionConflict(Exception):
    """`expected_version` did not match the stored record (the API layer maps this to 409).

    `expected` is 0 when the caller meant to create a record that already exists.
    `current` is the stored head, so the UI can show a diff.
    """

    def __init__(self, record: str, expected: int, actual: int, current: Entity) -> None:
        super().__init__(f"{record}: expected version {expected} but stored version is {actual}")
        self.record = record
        self.expected = expected
        self.actual = actual
        self.current = current


class CommandInProgress(RuntimeError):
    """A nested service command was attempted on the same project thread."""


class RecordNotFound(LookupError):
    """No such record (the API layer maps this to 404)."""


@dataclass(frozen=True)
class _Layout:
    """Where one entity type lives. Versioned types get a directory of v<N>.json files."""

    versioned: bool
    id_field: str | None
    place: Callable[[str | None, str | None], str]  # (record_id, episode_id) -> path
    episode_scoped: bool = False


_LAYOUTS: dict[type[Entity], _Layout] = {
    Asset: _Layout(False, "asset_id", lambda i, _e: f"assets/registry/{i}.json"),
    Project: _Layout(False, None, lambda _i, _e: "project.json"),
    Episode: _Layout(False, "episode_id", lambda i, _e: f"episodes/{i}/episode.json"),
    Timeline: _Layout(False, "episode_id", lambda i, _e: f"episodes/{i}/edits/timeline.json"),
    Script: _Layout(True, "script_id", lambda i, e: f"episodes/{e}/scripts/{i}", True),
    Shot: _Layout(True, "shot_id", lambda i, e: f"episodes/{e}/shots/{i}", True),
    Location: _Layout(True, "location_id", lambda i, _e: f"locations/{i}"),
    CameraProfile: _Layout(True, "camera_id", lambda i, _e: f"camera_profiles/{i}"),
    PostProfile: _Layout(False, "name", lambda i, _e: f"post_profiles/{i}.json"),
    AudioPostProfile: _Layout(False, "name", lambda i, _e: f"audio_post_profiles/{i}.json"),
    StyleBible: _Layout(True, "project_id", lambda _i, _e: "references/style_bible"),
    # Review is the only writer of approval state (10.1), so it must be
    # persistable: without a layout here every production approval failed with
    # INTERNAL while tests passed against a patched lookup. Unversioned - a
    # review is a dated opinion about one (target, version), never revised.
    Review: _Layout(False, "review_id", lambda i, _e: f"reviews/{i}.json"),
    GenerationRun: _Layout(False, "run_id", lambda i, _e: f"generations/{i}.json"),
    H3Candidate: _Layout(False, "candidate_id", lambda i, _e: f"generated/h3/candidates/{i}.json"),
    Export: _Layout(False, "export_id", lambda i, _e: f"exports/{i}/export.json"),
}


def _layout_for(cls: type[Entity]) -> _Layout:
    try:
        return _LAYOUTS[cls]
    except KeyError:
        raise JobError(
            JobErrorKind.INTERNAL, f"{cls.__name__} has no project-folder layout in the store"
        ) from None


def _invalid(message: str, *, cause: BaseException | None = None) -> JobError:
    return JobError(JobErrorKind.INVALID_MANIFEST, message, cause=cause)


def _check_creation_approval_fields(entity: Entity) -> None:
    """Approval state can only be attached to an existing record by its service."""
    for field in ("approval_review_id", "approved_cut_version", "approved_cut_output_id"):
        if getattr(entity, field, None) is not None:
            raise _invalid(
                f"`{field}` may not be set on creation; approval is set only by the approval service"
            )


def _declared_paths(data: dict[str, Any], entity_type: type[Entity]) -> list[tuple[str, str]]:
    """(field, value) for every declared path field holding a string."""
    found: list[tuple[str, str]] = []
    for name in _PATH_FIELDS.get(entity_type, ()):
        value = data.get(name)
        if isinstance(value, str) and value:
            found.append((name, value))
        elif isinstance(value, list):
            found.extend(
                (f"{name}[{i}]", v) for i, v in enumerate(value) if isinstance(v, str) and v
            )
    # PostProfileOverlay is nested in PostProfile JSON rather than having its own
    # store layout. Keep the nested path on this same ProjectRoot validation route.
    if entity_type is PostProfile:
        overlay = data.get("overlay")
        font = overlay.get("font_relative") if isinstance(overlay, dict) else None
        if isinstance(font, str) and font:
            found.append(("overlay.font_relative", font))
    return found


def _approval_is_binding(record: VersionedEntity) -> bool:
    """True once this version is approved; its content is then immutable (10.2)."""
    if isinstance(record, Shot) and record.status in {ShotStatus.APPROVED, ShotStatus.STALE}:
        return True
    return getattr(record, "approval_review_id", None) is not None


class ProjectStore:
    """Reads and writes entity records under a project folder."""

    #: One lock per resolved project root, shared across instances so two
    #: ProjectStore objects cannot both replace the same successor file.
    _locks: ClassVar[dict[Path, Any]] = {}
    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _commands_local: ClassVar[threading.local] = threading.local()

    def __init__(self, root: ProjectRoot) -> None:
        self._root = root
        with self._locks_guard:
            self._lock = self._locks.setdefault(root.path.resolve(), threading.RLock())

    @contextmanager
    def command(self) -> Iterator[None]:
        """Serialize one complete service command for this resolved project."""
        active = getattr(self._commands_local, "roots", set())
        root = self._root.path.resolve()
        if root in active:
            raise CommandInProgress(f"a service command is already active for {root}")
        with self._lock:
            active = set(getattr(self._commands_local, "roots", set()))
            active.add(root)
            self._commands_local.roots = active
            try:
                yield
            finally:
                active.remove(root)
                self._commands_local.roots = active

    # -- reads -----------------------------------------------------------------------

    def load(
        self,
        cls: type[E],
        record_id: str | None = None,
        *,
        episode_id: str | None = None,
        version: int | None = None,
    ) -> E:
        """The record's head (highest version), or exactly `version` if given."""
        return self.load_with_version(
            cls, record_id, episode_id=episode_id, version=version,
        )[0]

    def load_with_version(
        self,
        cls: type[E],
        record_id: str | None = None,
        *,
        episode_id: str | None = None,
        version: int | None = None,
    ) -> tuple[E, int]:
        """Load an entity and the mutation token from one locked snapshot."""
        with self._lock:
            layout = self._checked_layout(cls, record_id, episode_id)
            place = layout.place(record_id, episode_id)
            if not layout.versioned:
                if version is not None:
                    raise _invalid(f"{cls.__name__} is not versioned")
                return self._read_record(cls, place)
            existing = self._versions_in(place)
            if not existing:
                raise RecordNotFound(f"{cls.__name__} {record_id!r} does not exist")
            selected = existing[-1] if version is None else version
            if selected not in existing:
                raise RecordNotFound(f"{cls.__name__} {record_id!r} version {selected} does not exist")
            return self._read_version_snapshot(cls, place, selected)

    def load_timeline_at(self, episode_id: str, store_version: int) -> Timeline:
        """Load one immutable Timeline snapshot, with a legacy-head fallback."""
        if type(store_version) is not int or store_version < 1:
            raise RecordNotFound(
                f"Timeline for Episode {episode_id!r} store version {store_version!r} does not exist"
            )
        with self._lock:
            current, current_version = self.load_with_version(Timeline, episode_id)
            # A snapshot above the head token is an orphan from a save whose head write
            # failed. It was never a committed cut and the next save replaces it, so a
            # Review must never be able to bind to it.
            if store_version > current_version:
                raise RecordNotFound(
                    f"Timeline for Episode {episode_id!r} store version {store_version} "
                    f"is not committed (head is {current_version})"
                )
            relative = f"episodes/{episode_id}/edits/timelines/{store_version:06d}.json"
            if store_version == current_version:
                if current.episode_id != episode_id:
                    raise _invalid(f"episodes/{episode_id}/edits/timeline.json Episode identity does not match its path")
                # The head's own snapshot (absent for a legacy head) must agree with it,
                # or history and head would describe different cuts at the same version.
                if self._resolve(relative).exists():
                    snapshot, found_version = self._read_record(Timeline, relative)
                    if found_version != store_version or snapshot != current:
                        raise _invalid(f"{relative} disagrees with the Timeline head at the same version")
                return current
            try:
                timeline, found_version = self._read_record(Timeline, relative)
            except RecordNotFound as exc:
                raise RecordNotFound(
                    f"Timeline for Episode {episode_id!r} store version {store_version} does not exist"
                ) from exc
            if found_version != store_version:
                raise _invalid(
                    f"{relative} _store_version does not match its filename"
                )
            if timeline.episode_id != episode_id:
                raise _invalid(f"{relative} Episode identity does not match its path")
            return timeline

    def versions(
        self, cls: type[Entity], record_id: str | None, *, episode_id: str | None = None
    ) -> list[int]:
        """Stored version numbers, ascending. Empty if the record does not exist."""
        with self._lock:
            layout = self._checked_layout(cls, record_id, episode_id)
            if not layout.versioned:
                raise _invalid(f"{cls.__name__} is not versioned")
            return self._versions_in(layout.place(record_id, episode_id))

    def ids(self, cls: type[Entity], *, episode_id: str | None = None) -> list[str]:
        """Ids of stored records of a per-id versioned type (sorted)."""
        with self._lock:
            layout = _layout_for(cls)
            if not layout.versioned:
                raise _invalid(f"{cls.__name__} cannot be enumerated")
            if layout.episode_scoped and not episode_id:
                raise _invalid(f"{cls.__name__} is scoped to an episode; pass episode_id")
            parent = self._resolve(layout.place("x", episode_id)).parent
            if not parent.is_dir():
                return []
            return sorted(p.name for p in parent.iterdir() if self._versions_in_path(p))

    def episode_ids(self) -> list[str]:
        """Sorted ids for Episodes with a project-local episode.json manifest."""
        with self._lock:
            episodes = self._resolve("episodes")
            if not episodes.is_dir():
                return []
            try:
                directories = tuple(episodes.iterdir())
            except OSError as exc:
                raise JobError.from_exception(exc) from exc
            found = []
            for directory in directories:
                episode_path = self._resolve(f"episodes/{directory.name}/episode.json")
                if episode_path.is_file():
                    found.append(directory.name)
            return sorted(found)

    def episode_directories(self) -> list[str]:
        """Sorted names of every directory under episodes/, with or without episode.json.

        Invalidation scans these rather than episode_ids(): a Shot stored in a folder
        whose episode.json is missing must still be found, so that the revision is
        refused when the plan loads that Episode, instead of the Shot being skipped.
        """
        with self._lock:
            episodes = self._resolve("episodes")
            if not episodes.is_dir():
                return []
            try:
                return sorted(entry.name for entry in episodes.iterdir() if entry.is_dir())
            except OSError as exc:
                raise JobError.from_exception(exc) from exc

    def list_reviews(self) -> list[Review]:
        """Read every project Review, including legacy unscoped opinions."""
        with self._lock:
            directory = self._resolve("reviews")
            try:
                entries = tuple(directory.iterdir())
            except FileNotFoundError:
                return []
            except NotADirectoryError as exc:
                raise _invalid("reviews path is not a directory", cause=exc) from exc
            except OSError as exc:
                raise JobError.from_exception(exc) from exc
            relatives = [
                f"reviews/{entry.name}"
                for entry in entries
                if entry.suffix == ".json"
            ]
            reviews = [self._read_record(Review, relative)[0] for relative in relatives]

            def created_utc(review: Review) -> datetime:
                # Legacy Reviews may carry naive timestamps; new ones are UTC-aware.
                # Read a naive value as UTC so mixed projects still sort.
                stamp = review.created_at
                return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp

            return sorted(reviews, key=lambda review: (created_utc(review), review.review_id))

    def find_output(self, episode_id: str, output_id: str) -> ShotOutput | EpisodeOutput:
        """Find one Shot or cut output receipt by id, verifying only that receipt.

        Ledger filenames carry the output id, so other receipts are never read: a
        damaged artifact elsewhere in the episode cannot block this lookup.
        """
        with self._lock:
            self.load(Episode, episode_id)
            matches: list[ShotOutput | EpisodeOutput] = []
            for shot_id in self.ids(Shot, episode_id=episode_id):
                ledger = self._shot_output_directory(episode_id, shot_id)
                for token, relative in self._shot_output_files(ledger):
                    match = _SHOT_OUTPUT_FILE.fullmatch(Path(relative).name)
                    if match is not None and match.group(2) == output_id:
                        output, _ = self._read_shot_output(
                            relative, token=token, episode_id=episode_id, shot_id=shot_id,
                        )
                        matches.append(output)
            directory = self._episode_output_directory(episode_id)
            for token, relative in self._episode_output_files(directory):
                match = _SHOT_OUTPUT_FILE.fullmatch(Path(relative).name)
                if match is not None and match.group(2) == output_id:
                    output, _ = self._read_episode_output(relative, token=token, episode_id=episode_id)
                    matches.append(output)
            if not matches:
                raise RecordNotFound(f"output {output_id!r} does not exist in Episode {episode_id!r}")
            if len(matches) > 1:
                raise _invalid(f"output id {output_id!r} is ambiguous in Episode {episode_id!r}")
            return matches[0]

    def list_h3_candidates(self, shot_id: str, *, episode_id: str) -> tuple[H3Candidate, ...]:
        """List immutable candidate receipts for a Shot in stable filename order."""
        with self._lock:
            directory = self._resolve("generated/h3/candidates")
            if not directory.is_dir():
                return ()
            found = []
            for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
                relative = path.relative_to(self._root.path).as_posix()
                candidate, _ = self._read_record(H3Candidate, relative)
                if candidate.episode_id == episode_id and candidate.shot_id == shot_id:
                    found.append(candidate)
            return tuple(found)

    def append_shot_output(self, output: ShotOutput, *, expected_version: int) -> int:
        """Append an immutable output receipt using its per-Shot ledger token."""
        if type(expected_version) is not int or expected_version < 0:
            raise _invalid("ShotOutput expected_version must be a non-negative ledger token")
        with self._lock:
            try:
                output = ShotOutput.model_validate(output.model_dump(mode="python"))
            except (ValidationError, AttributeError, TypeError) as exc:
                raise _invalid(f"invalid ShotOutput: {exc}", cause=exc) from exc
            self.load(Episode, output.episode_id)
            self.load(Shot, output.shot_id, episode_id=output.episode_id,
                      version=output.record_version)
            ledger = self._shot_output_directory(output.episode_id, output.shot_id)
            existing = self._shot_output_files(ledger)
            current_token = len(existing)
            if expected_version != current_token:
                current: Entity
                if existing:
                    current = self._read_shot_output(
                        existing[-1][1], token=existing[-1][0],
                        episode_id=output.episode_id, shot_id=output.shot_id,
                    )[0]
                else:
                    current = self.load(Shot, output.shot_id, episode_id=output.episode_id)
                raise VersionConflict(
                    f"shot_outputs/{output.episode_id}/{output.shot_id}",
                    expected_version, current_token, current,
                )
            # Validate the existing ledger before extending it; corruption must not be
            # hidden by a new append.
            prior = [self._read_shot_output(
                relative, token=token, episode_id=output.episode_id, shot_id=output.shot_id,
            )[0]
                     for token, relative in existing]
            if any(item.output_id == output.output_id for item in prior):
                raise _invalid(f"ShotOutput output_id {output.output_id!r} already exists")
            self._verify_shot_output_artifact(output)
            token = current_token + 1
            relative = self._shot_output_path(output.episode_id, output.shot_id,
                                              token, output.output_id)
            if self._resolve(relative).exists():
                raise _invalid(f"ShotOutput destination already exists: {relative}")
            self._write(relative, output, mutation_token=token,
                        predecessor_token=current_token)
            return token

    def list_shot_outputs(
        self, shot_id: str, *, episode_id: str, record_version: int | None = None,
        kind: ShotOutputKind | None = None,
    ) -> tuple[ShotOutput, ...]:
        """Return verified output receipts in append order."""
        with self._lock:
            if record_version is not None and (type(record_version) is not int or record_version < 1):
                raise _invalid("record_version must be a positive Shot content version")
            try:
                if kind is not None:
                    kind = ShotOutputKind(kind)
            except ValueError as exc:
                raise _invalid(f"invalid ShotOutput kind: {kind!r}", cause=exc) from exc
            ledger = self._shot_output_directory(episode_id, shot_id)
            found = self._shot_output_files(ledger)
            outputs: list[ShotOutput] = []
            for expected_token, relative in found:
                output, token = self._read_shot_output(
                    relative, token=expected_token, episode_id=episode_id, shot_id=shot_id,
                )
                if token != expected_token:
                    raise _invalid(f"{relative} mutation token does not match its filename")
                if output.episode_id != episode_id or output.shot_id != shot_id:
                    raise _invalid(f"{relative} Shot identity does not match its ledger")
                if record_version is not None and output.record_version != record_version:
                    continue
                if kind is not None and output.kind is not kind:
                    continue
                outputs.append(output)
            return tuple(outputs)

    def append_episode_output(self, output: EpisodeOutput, *, expected_version: int) -> int:
        """Append an immutable episode-cut receipt with its own ledger token."""
        if type(expected_version) is not int or expected_version < 0:
            raise _invalid("EpisodeOutput expected_version must be a non-negative ledger token")
        with self._lock:
            try:
                output = EpisodeOutput.model_validate(output.model_dump(mode="python"))
            except (ValidationError, AttributeError, TypeError) as exc:
                raise _invalid(f"invalid EpisodeOutput: {exc}", cause=exc) from exc
            self.load(Episode, output.episode_id)
            try:
                timeline, timeline_token = self.load_with_version(Timeline, output.episode_id)
            except RecordNotFound as exc:
                raise _invalid(
                    f"EpisodeOutput for {output.episode_id!r} has no persisted Timeline",
                    cause=exc,
                ) from exc
            if timeline_token != output.timeline_store_version:
                raise VersionConflict(
                    f"episodes/{output.episode_id}/edits/timeline.json",
                    output.timeline_store_version, timeline_token, timeline,
                )
            if timeline.content_sha256 != output.timeline_sha256:
                raise _invalid("EpisodeOutput timeline_sha256 does not match the persisted Timeline")
            ledger = self._episode_output_directory(output.episode_id)
            existing = self._episode_output_files(ledger)
            current_token = len(existing)
            if expected_version != current_token:
                current: Entity = (
                    self._read_episode_output(existing[-1][1], token=existing[-1][0],
                                              episode_id=output.episode_id)[0]
                    if existing else timeline
                )
                raise VersionConflict(
                    f"episode_outputs/{output.episode_id}", expected_version, current_token, current,
                )
            prior = [self._read_episode_output(relative, token=token,
                                               episode_id=output.episode_id)[0]
                     for token, relative in existing]
            if any(item.output_id == output.output_id for item in prior):
                raise _invalid(f"EpisodeOutput output_id {output.output_id!r} already exists")
            self._verify_episode_output_artifact(output)
            token = current_token + 1
            relative = self._episode_output_path(output.episode_id, token, output.output_id)
            if self._resolve(relative).exists():
                raise _invalid(f"EpisodeOutput destination already exists: {relative}")
            self._write(relative, output, mutation_token=token,
                        predecessor_token=current_token)
            return token

    def list_episode_outputs(self, episode_id: str) -> tuple[EpisodeOutput, ...]:
        """Return verified cut receipts in append order."""
        with self._lock:
            self.load(Episode, episode_id)
            directory = self._episode_output_directory(episode_id)
            found = self._episode_output_files(directory)
            outputs: list[EpisodeOutput] = []
            for expected_token, relative in found:
                output, token = self._read_episode_output(
                    relative, token=expected_token, episode_id=episode_id,
                )
                if token != expected_token:
                    raise _invalid(f"{relative} mutation token does not match its filename")
                outputs.append(output)
            return tuple(outputs)

    def verify_asset(
        self, asset_id: str, *, usage: Literal["geometry", "texture", "environment"] = "geometry"
    ) -> Asset:
        """Load an asset record and verify its bytes for an explicit consumer."""
        if usage not in {"geometry", "texture", "environment"}:
            raise ValueError(f"unsupported asset usage {usage!r}; expected geometry, texture, or environment")
        asset = self.load(Asset, asset_id)
        self._verify_asset_artifact(asset, usage=usage)
        return asset

    # -- writes ----------------------------------------------------------------------

    def save(self, entity: Entity, *, expected_version: int) -> int:
        """Save an unversioned entity with optimistic concurrency.

        Use expected_version=0 to create. The returned token increments on each
        save and is persisted as `_store_version`, separate from domain fields.
        Read it with load_with_version(). Legacy files without metadata have token 1.
        """
        layout = _layout_for(type(entity))
        if layout.versioned:
            raise _invalid(f"{type(entity).__name__} is versioned; use create/update/revise")
        relative = layout.place(self._id_of(entity, layout), None)
        if isinstance(entity, H3Candidate) and expected_version != 0:
            raise _invalid("H3Candidate receipts are immutable; only expected_version=0 creation is allowed")
        if isinstance(entity, Review) and expected_version != 0:
            raise _invalid("Review records are immutable; record a new Review")
        if isinstance(entity, AudioPostProfile) and expected_version != 0:
            raise _invalid("AudioPostProfile records are immutable; a changed chain needs a new name")
        if isinstance(entity, PostProfile) and expected_version != 0:
            raise _invalid("PostProfile records are immutable; a changed chain needs a new name")
        if isinstance(entity, Export) and expected_version != 0:
            raise _invalid("Export receipts are immutable; only expected_version=0 creation is allowed")
        with self._lock:
            creating = False
            try:
                current, actual = self._read_record(type(entity), relative)
            except RecordNotFound:
                if expected_version != 0:
                    raise
                actual = 0
                creating = True
            else:
                if expected_version != actual:
                    raise VersionConflict(relative, expected_version, actual, current)
            if creating:
                _check_creation_approval_fields(entity)
            next_version = actual + 1
            if isinstance(entity, Timeline):
                history = f"episodes/{entity.episode_id}/edits/timelines"
                if actual:
                    # A head saved before history existed has no snapshot of its own.
                    # Preserve its exact bytes before replacing it, so earlier cut
                    # receipts keep their source edit.
                    current_snapshot = f"{history}/{actual:06d}.json"
                    if not self._resolve(current_snapshot).exists():
                        self._atomic_write(
                            current_snapshot,
                            self._root.read_bytes(relative).decode("utf-8"),
                        )
                # Snapshot first, then head. Snapshots at or below the head token are
                # never written here, so they are immutable; one at next_version can only
                # be an orphan from a save whose head write failed, and is replaced.
                snapshot_text = self._serialized_text(entity, store_version=next_version)
                self._atomic_write(f"{history}/{next_version:06d}.json", snapshot_text)
                self._atomic_write(relative, snapshot_text)
            else:
                self._write(relative, entity, store_version=next_version)
            return next_version

    def create(self, entity: VersionedEntity) -> None:
        """Create content version 1 with mutation token 1."""
        if entity.version != 1 or entity.supersedes is not None:
            raise _invalid("a new record must be version 1 with no `supersedes`")
        _check_creation_approval_fields(entity)
        if isinstance(entity, Shot) and entity.status is not ShotStatus.DRAFT:
            raise _invalid("a new Shot must be draft")
        directory = self._directory_of(entity)
        with self._lock:
            existing = self._versions_in(directory)
            if existing:
                head, token = self._read_version_snapshot(type(entity), directory, existing[-1])
                raise VersionConflict(directory, 0, token, head)
            self._write(self._version_path(directory, 1), entity, mutation_token=1, predecessor_token=0)

    def update(self, entity: VersionedEntity, *, expected_version: int) -> None:
        """Append a lifecycle snapshot for the current content version."""
        directory = self._directory_of(entity)
        with self._lock:
            head, token = self._head_checked(entity, directory, expected_version)
            validated = self.validate_entity_payload(entity)
            if validated.version != head.version or validated.supersedes != head.supersedes:
                raise _invalid("update() keeps version and `supersedes`; use revise() for a new version")
            self._check_in_place_change(head, validated)
            self._write(self._lifecycle_path(directory, head.version, token + 1), validated,
                        mutation_token=token + 1, predecessor_token=token)

    def revise(self, entity: VersionedEntity, *, expected_version: int) -> None:
        """Append a content successor; mutation token advances independently."""
        directory = self._directory_of(entity)
        with self._lock:
            _, token = self._validate_revision_locked(entity, expected_version)
            target = self._version_path(directory, entity.version)
            if self._resolve(target).exists():
                raise _invalid(f"revision target {target} already exists; version files are append-only")
            self._write(self._version_path(directory, entity.version), entity,
                        mutation_token=token + 1, predecessor_token=token)

    def validate_revision(
        self, entity: VersionedEntity, *, expected_version: int,
    ) -> tuple[VersionedEntity, int]:
        """Validate CAS, payload, successor policy, and path containment without writing."""
        with self._lock:
            return self._validate_revision_locked(entity, expected_version)

    def validate_entity_payload(self, entity: Entity) -> Entity:
        """Revalidate a model instance and its declared paths without writing."""
        with self._lock:
            try:
                validated = type(entity).model_validate(entity.model_dump(mode="python"))
            except (ValidationError, TypeError, ValueError) as exc:
                raise _invalid(f"{type(entity).__name__} payload is invalid: {exc}", cause=exc) from exc
            self._check_path_fields(validated.model_dump(mode="json"), type(validated))
            return validated

    def _validate_revision_locked(
        self, entity: VersionedEntity, expected_version: int,
    ) -> tuple[VersionedEntity, int]:
        directory = self._directory_of(entity)
        head, token = self._head_checked(entity, directory, expected_version)
        validated = self.validate_entity_payload(entity)
        try:
            check_revision(head, validated)
        except RevisionError as exc:
            raise _invalid(str(exc), cause=exc) from exc
        return head, token

    def record_cascade(self, plan: dict[str, Any]) -> None:
        """Append an opaque cascade plan after validating its journal envelope."""
        with self._lock:
            self._validate_cascade_plan(plan)
            cascade_id = plan["cascade_id"]
            relative = f"_journal/cascades/{cascade_id}.json"
            if self._resolve(relative).exists():
                raise _invalid(f"cascade plan {cascade_id} already exists")
            self._atomic_write(relative, json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    def cascades(self, *, pending_only: bool = False) -> tuple[dict[str, Any], ...]:
        """Return validated immutable plans, including completed history by default."""
        with self._lock:
            directory = self._resolve("_journal/cascades")
            if not directory.exists():
                return ()
            plans: list[dict[str, Any]] = []
            completed: set[str] = set()
            for item in sorted(directory.glob("*.json")):
                relative = f"_journal/cascades/{item.name}"
                try:
                    raw = json.loads(self._read_text(relative))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _invalid(f"cannot read cascade journal entry {item.name}: {exc}", cause=exc) from exc
                if item.name.endswith(".done.json"):
                    if (not isinstance(raw, dict) or set(raw) != {"cascade_id"}
                            or not isinstance(raw["cascade_id"], str)
                            or not re.fullmatch(r"[0-9a-f]{32}", raw["cascade_id"])
                            or item.name != f"{raw['cascade_id']}.done.json"):
                        raise _invalid(f"malformed cascade completion marker {item.name}")
                    completed.add(raw["cascade_id"])
                    continue
                if not isinstance(raw, dict):
                    raise _invalid(f"malformed cascade plan {item.name}")
                self._validate_cascade_plan(raw)
                if item.stem != raw["cascade_id"]:
                    raise _invalid(f"cascade plan filename does not match its id: {item.name}")
                plans.append(raw)
            known = {plan["cascade_id"] for plan in plans}
            if completed - known:
                raise _invalid(f"completion marker references missing cascade plan {sorted(completed-known)}")
            return tuple(plan for plan in plans if not pending_only or plan["cascade_id"] not in completed)

    def complete_cascade(self, cascade_id: str) -> None:
        with self._lock:
            plans = {p["cascade_id"] for p in self.cascades()}
            if cascade_id not in plans:
                raise _invalid(f"cannot complete unknown cascade {cascade_id}")
            relative = f"_journal/cascades/{cascade_id}.done.json"
            if not self._resolve(relative).exists():
                self._atomic_write(relative, json.dumps({"cascade_id": cascade_id}, sort_keys=True) + "\n")

    @staticmethod
    def _validate_cascade_plan(plan: dict[str, Any]) -> None:
        required = {"cascade_id", "entity_type", "before", "after", "expected_version", "episode_ids", "affected"}
        if set(plan) != required or not isinstance(plan.get("cascade_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", plan["cascade_id"]):
            raise _invalid("malformed cascade plan structure")
        if not isinstance(plan.get("entity_type"), str) or not plan["entity_type"]:
            raise _invalid("cascade plan entity_type must be a nonempty string")
        if not isinstance(plan.get("before"), dict) or not isinstance(plan.get("after"), dict):
            raise _invalid("cascade before/after payloads must be objects")
        if type(plan.get("expected_version")) is not int or plan["expected_version"] < 1:
            raise _invalid("cascade expected_version must be a positive token")
        if (not isinstance(plan.get("episode_ids"), list)
                or any(not isinstance(x, str) or not x for x in plan["episode_ids"])):
            raise _invalid("cascade episode_ids must be strings")
        if plan["episode_ids"] != sorted(set(plan["episode_ids"])):
            raise _invalid("cascade episode_ids must be sorted and unique")
        if not isinstance(plan.get("affected"), list):
            raise _invalid("cascade affected must be a list")
        seen: set[tuple[str, str]] = set()
        for row in plan["affected"]:
            if (not isinstance(row, dict) or set(row) != {"episode_id", "shot_id", "version"}
                    or not isinstance(row["episode_id"], str) or not row["episode_id"]
                    or not isinstance(row["shot_id"], str) or not row["shot_id"]
                    or type(row["version"]) is not int or row["version"] < 1):
                raise _invalid("malformed affected Shot reference in cascade plan")
            key = (row["episode_id"], row["shot_id"])
            if key in seen or row["episode_id"] not in plan["episode_ids"]:
                raise _invalid("duplicate or unlisted affected Shot in cascade plan")
            seen.add(key)

    # -- guards ----------------------------------------------------------------------

    def _head_checked(
        self, entity: VersionedEntity, directory: str, expected: int
    ) -> tuple[VersionedEntity, int]:
        existing = self._versions_in(directory)
        if not existing:
            raise RecordNotFound(f"{type(entity).__name__} at {directory!r} does not exist")
        head, token = self._read_version_snapshot(type(entity), directory, existing[-1])
        if token != expected:
            raise VersionConflict(directory, expected, token, head)
        return head, token

    @staticmethod
    def _check_in_place_change(head: VersionedEntity, new: VersionedEntity) -> None:
        before, after = head.model_dump(mode="json"), new.model_dump(mode="json")
        changed = {name for name in before if before[name] != after[name]}
        if isinstance(head, Shot):
            conflicts = changed & head.creator_authored
            if conflicts:
                raise _invalid(
                    f"FR-29 creator_authored conflict: {', '.join(sorted(conflicts))}; "
                    "creator-authored fields cannot be overwritten by update()"
                )
            framing_approval = (
                head.framing_status is FramingStatus.PENDING
                and new.framing_status is FramingStatus.APPROVED
            )
            if framing_approval:
                required_authored = {
                    "camera_profile",
                    "camera_profile_version",
                    *(
                        f"camera_profile.{name}"
                        for name in CameraProfile.model_fields
                        if field_invalidation(CameraProfile, name) is Invalidation.REBUILD
                    ),
                }
                if not (
                    head.status is ShotStatus.BLOCKING
                    and new.status is ShotStatus.BUILDING
                    and new.camera_profile_version is not None
                    and required_authored.issubset(new.creator_authored)
                ):
                    raise _invalid(
                        "FR-29 framing approval must atomically move blocking to building "
                        "and freeze the accepted CameraProfile values"
                    )
            panel_changed = "framing_panel_ref" in changed
            if panel_changed and not (
                new.status is ShotStatus.BLOCKING
                and new.framing_status is FramingStatus.PENDING
                and bool(new.framing_panel_ref)
            ):
                raise _invalid(
                    "FR-29 framing_panel_ref may change only while a pending panel is "
                    "being created or replaced in blocking"
                )
            framing_binding_changed = bool(
                changed & {"creator_authored", "camera_profile_version"}
            )
            if framing_binding_changed and not (
                head.status is ShotStatus.BLOCKING
                and new.status is ShotStatus.BUILDING
                and head.framing_status is FramingStatus.PENDING
                and new.framing_status is FramingStatus.APPROVED
            ):
                raise _invalid(
                    "FR-29 camera values may only be frozen while approving pending framing; "
                    "use revise() for creator-authored content changes"
                )
        content = changed - _LIFECYCLE_FIELDS
        if content:
            raise _invalid(
                f"update() cannot change content fields: {', '.join(sorted(content))}; "
                "use revise()"
            )
        if isinstance(head, Shot) and isinstance(new, Shot) and head.status is not new.status:
            check_transition(
                head.status,
                new.status,
                TransitionContext(
                    framing_status=new.framing_status,
                    approving_review_id=new.approval_review_id,
                ),
            )
        if not _approval_is_binding(head):
            return
        if isinstance(head, Shot):
            # approved -> stale is the one in-place change the state machine allows
            after["status"] = before["status"]
        if before != after:
            raise _invalid(
                f"version {head.version} is approved and immutable; "
                "create a successor with revise()"
            )

    # -- locating and file I/O -------------------------------------------------------

    @staticmethod
    def _checked_layout(
        entity_cls: type[Entity], record_id: str | None, episode_id: str | None
    ) -> _Layout:
        layout = _layout_for(entity_cls)
        if layout.id_field is not None and not record_id:
            raise _invalid(f"{entity_cls.__name__} needs a record id")
        if layout.episode_scoped and not episode_id:
            raise _invalid(f"{entity_cls.__name__} is scoped to an episode; pass episode_id")
        return layout

    @staticmethod
    def _id_of(entity: Entity, layout: _Layout) -> str | None:
        return None if layout.id_field is None else getattr(entity, layout.id_field)

    def _directory_of(self, entity: VersionedEntity) -> str:
        layout = _layout_for(type(entity))
        if not layout.versioned:
            raise _invalid(f"{type(entity).__name__} is not versioned; use save()")
        episode = getattr(entity, "episode_id", None) if layout.episode_scoped else None
        return layout.place(self._id_of(entity, layout), episode)

    @staticmethod
    def _shot_output_directory(episode_id: str, shot_id: str) -> str:
        return f"episodes/{episode_id}/shots/{shot_id}/outputs"

    @classmethod
    def _shot_output_path(cls, episode_id: str, shot_id: str, token: int,
                          output_id: str) -> str:
        return f"{cls._shot_output_directory(episode_id, shot_id)}/o{token:08d}-{output_id}.json"

    @staticmethod
    def _episode_output_directory(episode_id: str) -> str:
        return f"episodes/{episode_id}/outputs/cut_receipts"

    @classmethod
    def _episode_output_path(cls, episode_id: str, token: int, output_id: str) -> str:
        return f"{cls._episode_output_directory(episode_id)}/o{token:08d}-{output_id}.json"

    def _shot_output_files(self, directory: str) -> list[tuple[int, str]]:
        path = self._resolve(directory)
        if not path.exists():
            return []
        if not path.is_dir():
            raise _invalid(f"ShotOutput ledger path is not a directory: {directory}")
        found: list[tuple[int, str]] = []
        for item in path.iterdir():
            match = _SHOT_OUTPUT_FILE.fullmatch(item.name)
            if match is None:
                raise _invalid(f"unexpected entry in ShotOutput ledger: {item.name!r}")
            token = int(match.group(1))
            found.append((token, f"{directory}/{item.name}"))
        found.sort()
        for expected, (token, relative) in enumerate(found, start=1):
            if token != expected:
                raise _invalid(f"{relative} does not advance the ShotOutput ledger token exactly once")
        return found

    def _episode_output_files(self, directory: str) -> list[tuple[int, str]]:
        path = self._resolve(directory)
        if not path.exists():
            return []
        if not path.is_dir():
            raise _invalid(f"EpisodeOutput ledger path is not a directory: {directory}")
        found: list[tuple[int, str]] = []
        for item in path.iterdir():
            match = _SHOT_OUTPUT_FILE.fullmatch(item.name)
            if match is None:
                raise _invalid(f"unexpected entry in EpisodeOutput ledger: {item.name!r}")
            found.append((int(match.group(1)), f"{directory}/{item.name}"))
        found.sort()
        for expected, (token, relative) in enumerate(found, start=1):
            if token != expected:
                raise _invalid(f"{relative} does not advance the EpisodeOutput ledger token exactly once")
        return found

    def _read_shot_output(self, relative: str, *, token: int, episode_id: str,
                          shot_id: str) -> tuple[ShotOutput, int]:
        match = _SHOT_OUTPUT_FILE.fullmatch(Path(relative).name)
        if match is None or int(match.group(1)) != token:
            raise _invalid(f"{relative} filename does not match its expected ledger token")
        try:
            data = json.loads(self._read_text(relative))
        except FileNotFoundError as exc:
            raise RecordNotFound(f"{relative} does not exist") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _invalid(f"cannot read ShotOutput {relative}: {exc}", cause=exc) from exc
        self._require_readable_schema(relative, data)
        if not isinstance(data, dict):
            raise _invalid(f"{relative} is not a JSON object")
        metadata = data.pop("_mutation_token", None)
        predecessor = data.pop("_predecessor_token", None)
        if (type(metadata) is not int or metadata != token
                or type(predecessor) is not int or predecessor != token - 1):
            raise _invalid(f"{relative} has inconsistent ShotOutput ledger metadata")
        try:
            output = ShotOutput.model_validate(data)
        except ValidationError as exc:
            raise _invalid(f"{relative} does not match ShotOutput: {exc}", cause=exc) from exc
        if match.group(2) != output.output_id:
            raise _invalid(f"{relative} filename output_id does not match its payload")
        if output.episode_id != episode_id or output.shot_id != shot_id:
            raise _invalid(f"{relative} Shot identity does not match its ledger")
        # Receipts may bind historical Shot versions, but the named source version
        # must actually exist in this project. Never treat content_version as a token.
        self.load(Episode, output.episode_id)
        self.load(Shot, output.shot_id, episode_id=output.episode_id,
                  version=output.record_version)
        self._verify_shot_output_artifact(output)
        return output, metadata

    def _read_episode_output(self, relative: str, *, token: int,
                             episode_id: str) -> tuple[EpisodeOutput, int]:
        match = _SHOT_OUTPUT_FILE.fullmatch(Path(relative).name)
        if match is None or int(match.group(1)) != token:
            raise _invalid(f"{relative} filename does not match its expected ledger token")
        try:
            data = json.loads(self._read_text(relative))
        except FileNotFoundError as exc:
            raise RecordNotFound(f"{relative} does not exist") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _invalid(f"cannot read EpisodeOutput {relative}: {exc}", cause=exc) from exc
        self._require_readable_schema(relative, data)
        if not isinstance(data, dict):
            raise _invalid(f"{relative} is not a JSON object")
        metadata = data.pop("_mutation_token", None)
        predecessor = data.pop("_predecessor_token", None)
        if (type(metadata) is not int or metadata != token
                or type(predecessor) is not int or predecessor != token - 1):
            raise _invalid(f"{relative} has inconsistent EpisodeOutput ledger metadata")
        try:
            output = EpisodeOutput.model_validate(data)
        except ValidationError as exc:
            raise _invalid(f"{relative} does not match EpisodeOutput: {exc}", cause=exc) from exc
        if match.group(2) != output.output_id:
            raise _invalid(f"{relative} filename output_id does not match its payload")
        if output.episode_id != episode_id:
            raise _invalid(f"{relative} Episode identity does not match its ledger")
        self.load(Episode, output.episode_id)
        self._verify_episode_output_artifact(output)
        return output, metadata

    def _verify_shot_output_artifact(self, output: ShotOutput) -> None:
        try:
            artifact = self._root.resolve(output.relative_path)
        except ContainmentError as exc:
            raise _invalid(f"ShotOutput artifact path escapes the project: {exc}", cause=exc) from exc
        if not artifact.is_file():
            raise _invalid(f"ShotOutput artifact is missing or not a file: {output.relative_path}")
        digest = hashlib.sha256()
        try:
            with artifact.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
        except OSError as exc:
            raise _invalid(f"cannot hash ShotOutput artifact {output.relative_path}: {exc}", cause=exc) from exc
        if digest.hexdigest() != output.content_sha256:
            raise _invalid(f"ShotOutput artifact hash mismatch: {output.relative_path}")

    def _verify_asset_artifact(
        self, asset: Asset, *, usage: Literal["geometry", "texture", "environment"] = "geometry"
    ) -> None:
        if not asset.file_path or not asset.checksum:
            raise _invalid(f"Asset {asset.asset_id!r} has no registered file path or checksum")
        try:
            artifact = self._root.resolve(asset.file_path)
        except ContainmentError as exc:
            raise _invalid(
                f"Asset {asset.asset_id!r} path escapes the project: {exc}", cause=exc
            ) from exc
        if not artifact.is_file():
            raise _invalid(
                f"Asset {asset.asset_id!r} file is missing or not a file: {asset.file_path}"
            )
        extension = Path(asset.file_path).suffix.lower()
        try:
            with artifact.open("rb") as stream:
                head = stream.read(12)
        except OSError as exc:
            raise _invalid(
                f"cannot read Asset {asset.asset_id!r} file {asset.file_path}: {exc}", cause=exc
            ) from exc
        if extension == ".blend" or head.startswith(b"BLENDER"):
            raise _invalid(
                f"Asset {asset.asset_id!r} is a Blender .blend file, which can execute Python "
                "on load; only .glb files can be imported as geometry"
            )
        if usage == "environment":
            if asset.type != "hdri" or extension not in {".hdr", ".exr"}:
                raise _invalid(f"Asset {asset.asset_id!r} environment usage requires type 'hdri' and a .hdr or .exr file")
            if extension == ".hdr" and not (head.startswith((b"#?RADIANCE", b"#?RGBE"))):
                raise _invalid(f"Asset {asset.asset_id!r} is not a Radiance HDR image")
            if extension == ".exr" and head[:4] != bytes((0x76, 0x2f, 0x31, 0x01)):
                raise _invalid(f"Asset {asset.asset_id!r} is not an OpenEXR image")
        elif usage == "geometry":
            if extension != ".glb":
                raise _invalid(
                    f"Asset {asset.asset_id!r} has unsupported geometry format {extension!r}; "
                    "only .glb files can be imported as geometry"
                )
        else:
            if asset.type != "texture":
                raise _invalid(
                    f"Asset {asset.asset_id!r} has type {asset.type!r}; texture usage requires type 'texture'"
                )
            if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise _invalid(
                    f"Asset {asset.asset_id!r} has unsupported texture format {extension!r}; "
                    "only .png, .jpg, .jpeg, and .webp files can be used as textures"
                )
        digest = hashlib.sha256()
        try:
            with artifact.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
        except OSError as exc:
            raise _invalid(
                f"cannot hash Asset {asset.asset_id!r} file {asset.file_path}: {exc}", cause=exc
            ) from exc
        actual = digest.hexdigest()
        if actual != asset.checksum:
            raise _invalid(
                f"Asset {asset.asset_id!r} checksum mismatch: registered {asset.checksum}, "
                f"actual {actual}"
            )

    def _verify_episode_output_artifact(self, output: EpisodeOutput) -> None:
        try:
            artifact = self._root.resolve(output.relative_path)
        except ContainmentError as exc:
            raise _invalid(f"EpisodeOutput artifact path escapes the project: {exc}", cause=exc) from exc
        if not artifact.is_file():
            raise _invalid(f"EpisodeOutput artifact is missing or not a file: {output.relative_path}")
        digest = hashlib.sha256()
        try:
            with artifact.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
        except OSError as exc:
            raise _invalid(f"cannot hash EpisodeOutput artifact {output.relative_path}: {exc}", cause=exc) from exc
        if digest.hexdigest() != output.content_sha256:
            raise _invalid(f"EpisodeOutput artifact hash mismatch: {output.relative_path}")
        if output.credit_manifest_path is not None:
            try:
                manifest = self._root.resolve(output.credit_manifest_path)
            except ContainmentError as exc:
                raise _invalid(f"EpisodeOutput credit manifest path escapes the project: {exc}", cause=exc) from exc
            if not manifest.is_file():
                raise _invalid(f"EpisodeOutput credit manifest is missing or not a file: {output.credit_manifest_path}")
            manifest_digest = hashlib.sha256()
            try:
                with manifest.open("rb") as stream:
                    while block := stream.read(1024 * 1024):
                        manifest_digest.update(block)
            except OSError as exc:
                raise _invalid(f"cannot hash EpisodeOutput credit manifest {output.credit_manifest_path}: {exc}", cause=exc) from exc
            if manifest_digest.hexdigest() != output.credit_manifest_sha256:
                raise _invalid(f"EpisodeOutput credit manifest hash mismatch: {output.credit_manifest_path}")

    @staticmethod
    def _version_path(directory: str, version: int) -> str:
        return f"{directory}/v{version:04d}.json"

    @staticmethod
    def _lifecycle_path(directory: str, version: int, token: int) -> str:
        return f"{directory}/v{version:04d}.lifecycle.{token:08d}.json"

    def _read_version_snapshot(self, cls: type[E], directory: str, version: int) -> tuple[E, int]:
        base_relative = self._version_path(directory, version)
        base, base_token = self._read_record(cls, base_relative)
        if base.version != version:
            raise _invalid(f"{base_relative} payload version does not match its filename")

        earlier = [item for item in self._versions_in(directory) if item < version]
        if earlier:
            previous_version = earlier[-1]
            if previous_version != version - 1:
                raise _invalid(f"content history before {base_relative} has a version gap")
            previous, previous_token = self._read_version_snapshot(cls, directory, previous_version)
            try:
                raw_base = json.loads(self._read_text(base_relative))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _invalid(f"cannot read content record {base_relative}: {exc}", cause=exc) from exc
            tracked_base = "_mutation_token" in raw_base
            expected_predecessor = (
                raw_base.get("_predecessor_token") if tracked_base else version - 1
            )
            if (expected_predecessor != previous_token
                    or base_token != previous_token + 1):
                raise _invalid(f"{base_relative} mutation token does not continue prior content history")
            if self._identity_of(previous) != self._identity_of(base):
                raise _invalid(f"{base_relative} changes entity identity across content versions")
            if base.supersedes != previous.version:
                raise _invalid(f"{base_relative} supersedes does not name the preceding content version")
            # Legacy stores replaced the base record with its latest lifecycle state,
            # so those records need structural chain validation without birth-only
            # revision guards (for example, a draft-state guard on a successor).
            if tracked_base:
                try:
                    check_revision(previous, base)
                except RevisionError as exc:
                    raise _invalid(f"{base_relative} has inconsistent successor metadata: {exc}", cause=exc) from exc
        token = base_token
        snapshot = base
        for candidate_token, relative in self._lifecycle_files(directory, version):
            if candidate_token != token + 1:
                raise _invalid(f"{relative} does not advance the lifecycle token exactly once")
            lifecycle, found = self._read_record(cls, relative)
            if found != candidate_token:
                raise _invalid(f"{relative} mutation token does not match its filename")
            try:
                raw = json.loads(self._read_text(relative))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _invalid(f"cannot read lifecycle snapshot {relative}: {exc}", cause=exc) from exc
            if raw.get("_predecessor_token") != token:
                raise _invalid(f"{relative} predecessor token does not continue the lifecycle chain")
            if (self._identity_of(lifecycle) != self._identity_of(base)
                    or lifecycle.version != base.version
                    or lifecycle.supersedes != base.supersedes):
                raise _invalid(f"{relative} does not describe its base entity/content version")
            try:
                self._check_in_place_change(snapshot, lifecycle)
            except (JobError, TransitionError) as exc:
                raise _invalid(
                    f"{relative} has an illegal lifecycle change: {exc}", cause=exc
                ) from exc
            snapshot = lifecycle
            token = found
        return snapshot, token

    def _lifecycle_files(self, directory: str, version: int) -> list[tuple[int, str]]:
        path = self._resolve(directory)
        if not path.is_dir():
            return []
        found = []
        for item in path.iterdir():
            match = _LIFECYCLE_FILE.match(item.name)
            if match and int(match.group(1)) == version:
                found.append((int(match.group(2)), f"{directory}/{item.name}"))
        return sorted(found)

    def _resolve(self, relative: str) -> Path:
        try:
            return self._root.resolve(relative)
        except ContainmentError as exc:
            raise _invalid(str(exc), cause=exc) from exc

    def _read_text(self, relative: str) -> str:
        try:
            return self._root.read_text(relative, encoding="utf-8")
        except ContainmentError as exc:
            raise _invalid(str(exc), cause=exc) from exc

    def _versions_in(self, directory: str) -> list[int]:
        return self._versions_in_path(self._resolve(directory))

    @staticmethod
    def _versions_in_path(path: Path) -> list[int]:
        if not path.is_dir():
            return []
        matches = (_VERSION_FILE.match(p.name) for p in path.iterdir())
        return sorted(int(m.group(1)) for m in matches if m)

    def _read(self, cls: type[E], relative: str) -> E:
        return self._read_record(cls, relative)[0]

    def _read_record(self, cls: type[E], relative: str) -> tuple[E, int]:
        try:
            raw = self._read_text(relative)
        except FileNotFoundError as exc:
            raise RecordNotFound(f"{relative} does not exist") from exc
        except OSError as exc:
            raise JobError.from_exception(exc) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _invalid(f"{relative} is not valid JSON: {exc}", cause=exc) from exc
        self._require_readable_schema(relative, data)
        # Also on load: a project folder can be hand-edited or arrive from an
        # export, so a stored path is untrusted input until containment says so.
        self._check_path_fields(data, cls)
        versioned = _layout_for(cls).versioned
        metadata_name = "_mutation_token" if versioned else "_store_version"
        legacy_default = None
        if versioned:
            match = _VERSION_FILE.match(Path(relative).name)
            legacy_default = int(match.group(1)) if match else None
        explicit_metadata = metadata_name in data
        metadata = data.pop(metadata_name, legacy_default if versioned else 1)
        predecessor = data.pop("_predecessor_token", None) if versioned else None
        if type(metadata) is not int or metadata < 1:
            raise _invalid(f"{relative} has no positive integer {metadata_name}")
        if versioned and explicit_metadata and (
            type(predecessor) is not int or predecessor < 0 or predecessor >= metadata
        ):
            raise _invalid(f"{relative} has malformed _predecessor_token")
        if versioned and not explicit_metadata and predecessor is not None:
            raise _invalid(f"{relative} has predecessor metadata without a mutation token")
        try:
            entity = cls.model_validate(data)
        except ValidationError as exc:
            raise _invalid(f"{relative} does not match {cls.__name__}: {exc}", cause=exc) from exc
        return entity, metadata

    @staticmethod
    def _require_readable_schema(relative: str, data: Any) -> None:
        stored = data.get("schema_version") if isinstance(data, dict) else None
        if isinstance(stored, bool) or not isinstance(stored, int):
            raise _invalid(f"{relative} has no integer schema_version")
        if stored > SCHEMA_VERSION:
            raise JobError(
                JobErrorKind.SCHEMA_VERSION_MISMATCH,
                f"{relative} has schema_version {stored}, newer than this build "
                f"({SCHEMA_VERSION}); refusing to load it",
            )
        if stored not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
            raise JobError(
                JobErrorKind.SCHEMA_VERSION_MISMATCH,
                f"{relative} has schema_version {stored} and no migration to "
                f"{SCHEMA_VERSION} is registered",
            )

    def _write(
        self, relative: str, entity: Entity, *, store_version: int | None = None,
        mutation_token: int | None = None, predecessor_token: int | None = None,
    ) -> None:
        self._atomic_write(
            relative,
            self._serialized_text(
                entity,
                store_version=store_version,
                mutation_token=mutation_token,
                predecessor_token=predecessor_token,
            ),
        )

    def _serialized_text(
        self, entity: Entity, *, store_version: int | None = None,
        mutation_token: int | None = None, predecessor_token: int | None = None,
    ) -> str:
        data = entity.model_dump(mode="json")
        if store_version is not None:
            data["_store_version"] = store_version
        if mutation_token is not None:
            data["_mutation_token"] = mutation_token
            data["_predecessor_token"] = predecessor_token
        self._check_path_fields(data, type(entity))
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    @staticmethod
    def _identity_of(entity: Entity) -> tuple[Any, ...]:
        layout = _layout_for(type(entity))
        identity = (getattr(entity, layout.id_field),) if layout.id_field else ()
        if layout.episode_scoped:
            identity += (entity.episode_id,)
        return (type(entity), *identity)

    def _check_path_fields(self, data: dict[str, Any], entity_type: type[Entity]) -> None:
        r"""Validate declared path fields through containment, not a local regex.

        FR-25 and CLAUDE.md invariant 4: one helper decides what a safe path is.
        A second, weaker check here was a validator differential - it matched
        only a leading drive letter, UNC prefix or "/", so it passed
        "..\..\outside.blend", drive-relative "C:foo" and reserved device
        names, all of which ProjectRoot rejects. These values are opened later,
        so the store must apply the same rule, on write and on load.
        """
        for field_name, value in _declared_paths(data, entity_type):
            try:
                self._root.resolve(value)
            except ContainmentError as error:
                raise _invalid(
                    f"{entity_type.__name__}.{field_name} is not a safe project-relative "
                    f"path ({error.problem}); the project must reopen standalone (AT-09)",
                    cause=error,
                ) from error

    def _atomic_write(self, relative: str, text: str) -> None:
        """Write a sibling temp file, fsync it, then rename over the target.

        The temp file is in the same directory (same volume, so the rename cannot
        degrade to copy+delete) and is written through `ProjectRoot` like any other
        file. Failures leave the old record intact and remove the temporary file.
        """
        temp = f"{relative}.{uuid.uuid4().hex}.tmp"
        temp_path: Path | None = None

        def write_durably(path: Path) -> None:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())

        try:
            try:
                temp_path = self._resolve(temp)
                self._root.write_with(temp, write_durably)
                self._replace(temp_path, self._resolve(relative))
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
        except ContainmentError as exc:
            raise _invalid(str(exc), cause=exc) from exc
        except OSError as exc:
            raise JobError.from_exception(exc) from exc

    @staticmethod
    def _replace(source: Path, target: Path) -> None:
        """`os.replace`, retried briefly: on Windows a scanner or reader holding the
        target open makes the rename fail with PermissionError for a few milliseconds."""
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))
