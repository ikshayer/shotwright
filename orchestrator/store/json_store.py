"""Project-local JSON store: the source of truth (10.0, invariant 1).

One orchestrator process is the single writer. Concurrency is optimistic: every
mutation carries `expected_version` (create implies 0), and a mismatch raises
`VersionConflict`, which the API layer turns into HTTP 409.

Layout (10.4). Versioned records are append-only, so each version is its own file
and "the current record" is the highest version on disk. There is deliberately no
head pointer or combined manifest to keep in sync with the version files:

    project.json
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

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from pydantic import ValidationError

from orchestrator.domain.base import SCHEMA_VERSION, Entity, ShotStatus, VersionedEntity
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    Location,
    Project,
    Script,
    Shot,
    StyleBible,
    Timeline,
)
from orchestrator.domain.state import TransitionContext, check_transition
from orchestrator.platform.containment import ContainmentError, ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind

E = TypeVar("E", bound=Entity)

_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
_VERSION_FILE = re.compile(r"^v(\d{4,})\.json$")
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_S = 0.02
_LIFECYCLE_FIELDS = frozenset({
    "status", "approval_review_id", "framing_status", "approval_notes",
})
# Only declared filesystem references are paths; prose and identifiers are not.
_PATH_FIELDS: dict[type[Entity], tuple[str, ...]] = {
    Location: ("blend_path",),
    Shot: ("audio_refs", "framing_panel_ref"),
    Timeline: ("audio_tracks",),
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
    Project: _Layout(False, None, lambda _i, _e: "project.json"),
    Episode: _Layout(False, "episode_id", lambda i, _e: f"episodes/{i}/episode.json"),
    Timeline: _Layout(False, "episode_id", lambda i, _e: f"episodes/{i}/edits/timeline.json"),
    Script: _Layout(True, "script_id", lambda i, e: f"episodes/{e}/scripts/{i}", True),
    Shot: _Layout(True, "shot_id", lambda i, e: f"episodes/{e}/shots/{i}", True),
    Location: _Layout(True, "location_id", lambda i, _e: f"locations/{i}"),
    CameraProfile: _Layout(True, "camera_id", lambda i, _e: f"camera_profiles/{i}"),
    StyleBible: _Layout(True, "project_id", lambda _i, _e: "references/style_bible"),
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

    def __init__(self, root: ProjectRoot) -> None:
        self._root = root
        with self._locks_guard:
            self._lock = self._locks.setdefault(root.path.resolve(), threading.RLock())

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
        """Load a record and its expected_version token from the same snapshot."""
        layout = self._checked_layout(cls, record_id, episode_id)
        place = layout.place(record_id, episode_id)
        if not layout.versioned:
            if version is not None:
                raise _invalid(f"{cls.__name__} is not versioned")
            return self._read_record(cls, place)
        if version is None:
            existing = self._versions_in(place)
            if not existing:
                raise RecordNotFound(f"{cls.__name__} {record_id!r} does not exist")
            version = existing[-1]
        return self._read_record(cls, self._version_path(place, version))

    def versions(
        self, cls: type[Entity], record_id: str | None, *, episode_id: str | None = None
    ) -> list[int]:
        """Stored version numbers, ascending. Empty if the record does not exist."""
        layout = self._checked_layout(cls, record_id, episode_id)
        if not layout.versioned:
            raise _invalid(f"{cls.__name__} is not versioned")
        return self._versions_in(layout.place(record_id, episode_id))

    def ids(self, cls: type[Entity], *, episode_id: str | None = None) -> list[str]:
        """Ids of stored records of a per-id versioned type (sorted)."""
        layout = _layout_for(cls)
        if not layout.versioned:
            raise _invalid(f"{cls.__name__} cannot be enumerated")
        if layout.episode_scoped and not episode_id:
            raise _invalid(f"{cls.__name__} is scoped to an episode; pass episode_id")
        parent = self._resolve(layout.place("x", episode_id)).parent
        if not parent.is_dir():
            return []
        return sorted(p.name for p in parent.iterdir() if self._versions_in_path(p))

    # -- writes ----------------------------------------------------------------------

    def save(self, entity: Entity, *, expected_version: int) -> int:
        """Save Project/Episode/Timeline with optimistic concurrency.

        Use expected_version=0 to create. The returned token increments on each
        save and is persisted as `_store_version`, separate from domain fields.
        Read it with load_with_version(). Legacy files without metadata have token 1.
        """
        layout = _layout_for(type(entity))
        if layout.versioned:
            raise _invalid(f"{type(entity).__name__} is versioned; use create/update/revise")
        relative = layout.place(self._id_of(entity, layout), None)
        with self._lock:
            try:
                current, actual = self._read_record(type(entity), relative)
            except RecordNotFound:
                if expected_version != 0:
                    raise
                actual = 0
            else:
                if expected_version != actual:
                    raise VersionConflict(relative, expected_version, actual, current)
            self._write(relative, entity, store_version=actual + 1)
            return actual + 1

    def create(self, entity: VersionedEntity) -> None:
        """Write version 1 of a new record."""
        if entity.version != 1 or entity.supersedes is not None:
            raise _invalid("a new record must be version 1 with no `supersedes`")
        if isinstance(entity, Shot) and entity.status is not ShotStatus.DRAFT:
            raise _invalid("a new Shot must be draft")
        directory = self._directory_of(entity)
        with self._lock:
            existing = self._versions_in(directory)
            if existing:
                head = self._read(type(entity), self._version_path(directory, existing[-1]))
                raise VersionConflict(directory, 0, head.version, head)
            self._write(self._version_path(directory, 1), entity)

    def update(self, entity: VersionedEntity, *, expected_version: int) -> None:
        """Update lifecycle fields on the head; content changes require revise().

        An approved version is immutable, except a Shot's approved -> stale move, which
        the state machine permits and which retains every artifact.
        """
        directory = self._directory_of(entity)
        with self._lock:
            head = self._head_checked(entity, directory, expected_version)
            if entity.version != head.version or entity.supersedes != head.supersedes:
                raise _invalid("update() keeps version and `supersedes`; use revise() for a new version")
            self._check_in_place_change(head, entity)
            self._write(self._version_path(directory, head.version), entity)

    def revise(self, entity: VersionedEntity, *, expected_version: int) -> None:
        """Append the successor of the head version; the superseded record is never touched."""
        directory = self._directory_of(entity)
        with self._lock:
            head = self._head_checked(entity, directory, expected_version)
            if entity.version != head.version + 1 or entity.supersedes != head.version:
                raise _invalid(
                    f"a revision of version {head.version} must be version {head.version + 1} "
                    f"with supersedes={head.version}"
                )
            if getattr(entity, "approval_review_id", None) is not None:
                raise _invalid("a revision never inherits approval; it must pass review again")
            if isinstance(entity, Shot) and entity.status is not ShotStatus.DRAFT:
                raise _invalid("a revised Shot must be draft")
            self._write(self._version_path(directory, entity.version), entity)

    # -- guards ----------------------------------------------------------------------

    def _head_checked(
        self, entity: VersionedEntity, directory: str, expected: int
    ) -> VersionedEntity:
        existing = self._versions_in(directory)
        if not existing:
            raise RecordNotFound(f"{type(entity).__name__} at {directory!r} does not exist")
        head = self._read(type(entity), self._version_path(directory, existing[-1]))
        if head.version != expected:
            raise VersionConflict(directory, expected, head.version, head)
        return head

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
    def _version_path(directory: str, version: int) -> str:
        return f"{directory}/v{version:04d}.json"

    def _resolve(self, relative: str) -> Path:
        try:
            return self._root.resolve(relative)
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
            raw = self._resolve(relative).read_text(encoding="utf-8")
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
        store_version = 1
        if not _layout_for(cls).versioned:
            store_version = data.pop("_store_version", 1)
            if type(store_version) is not int or store_version < 1:
                raise _invalid(f"{relative} has no positive integer _store_version")
        try:
            entity = cls.model_validate(data)
        except ValidationError as exc:
            raise _invalid(f"{relative} does not match {cls.__name__}: {exc}", cause=exc) from exc
        return entity, entity.version if isinstance(entity, VersionedEntity) else store_version

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
        if stored < SCHEMA_VERSION:
            raise JobError(
                JobErrorKind.SCHEMA_VERSION_MISMATCH,
                f"{relative} has schema_version {stored} and no migration to "
                f"{SCHEMA_VERSION} is registered",
            )

    def _write(self, relative: str, entity: Entity, *, store_version: int | None = None) -> None:
        data = entity.model_dump(mode="json")
        if store_version is not None:
            data["_store_version"] = store_version
        self._check_path_fields(data, type(entity))
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        self._atomic_write(relative, text)

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
