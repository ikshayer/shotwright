# ShotOutput entity and persistence contract

This contract implements the domain gap identified in Amendment 2 of
`service/ADAPTER_INTEGRATION_CONTRACT.md`. It does not wire adapters, jobs, API routes,
or approvals.

## Domain entity

Add `ShotOutputKind(StrEnum)` and `ShotOutput(Entity)` in `orchestrator/domain/entities.py`.
The entity fields and types are:

```python
class ShotOutputKind(StrEnum):
    SCENE = "scene"
    MASTER = "master"
    PREVIEW = "preview"
    STILL = "still"
    DEGRADED = "degraded"

class ShotOutput(Entity):
    output_id: str                 # unique attempt/output id; [A-Za-z0-9_-]+
    episode_id: str                # Shot IDs are episode scoped
    shot_id: str
    record_version: int = Field(ge=1, strict=True)  # content version; never a mutation token
    kind: ShotOutputKind
    relative_path: str             # project-relative artifact path
    content_sha256: str            # lowercase SHA-256 of artifact bytes
    engine_id: str | None          # Blender applied_settings.engine
    engine_build: str | None       # Blender build_hash
    profile_semver: str | None     # PostProfile identity semver
    filter_graph_hash: str | None  # SHA-256 emitted by PostProfile
    pinned_binaries: dict[str, str] | None  # PostProfile identity pins
```

`scene`, `master`, and `preview` require `engine_id` and `engine_build`. `degraded`
requires `profile_semver`, `filter_graph_hash`, and `pinned_binaries`. Other provenance
fields must be `None`; `still` has no additional provenance fields in this entity.
`content_sha256` is required for every kind. Hashes are 64 lowercase hexadecimal
characters. `record_version` is a strict positive integer (booleans are invalid). Paths must be non-empty project-relative paths and may not be absolute,
drive-qualified, contain NUL, or escape the project root. `ShotOutput` has no `stale`
field. Staleness is derived by comparing `record_version` with the current Shot head.

The record identity is `(episode_id, shot_id, record_version, kind, output_id)`. The
first three required key components from Amendment 2 are preserved; `output_id` allows
multiple attempts of the same kind/version without collision. `episode_id` disambiguates
episode-scoped Shot IDs. `output_id` must match `[A-Za-z0-9_-]+` so it is one safe filename
component in the ledger path.

## Store API

Add these methods to `ProjectStore` in `orchestrator/store/json_store.py`:

```python
def append_shot_output(self, output: ShotOutput, *, expected_version: int) -> int: ...
def list_shot_outputs(
    self, shot_id: str, *, episode_id: str, record_version: int | None = None,
    kind: ShotOutputKind | None = None,
) -> tuple[ShotOutput, ...]: ...
```

`expected_version` is the advancing mutation token for the output ledger of this
`(episode_id, shot_id)`, starting at `0`; successful append returns the incremented
token. It is independent of both `ShotOutput.record_version` and the Shot's own
mutation token. A mismatch raises the existing `VersionConflict` with expected/actual
ledger tokens. Missing Episode, Shot, or historical content version raises the existing
`RecordNotFound`. Invalid or corrupt output records, artifacts, and ledger structure raise
`JobError(INVALID_MANIFEST)`. Append checks the referenced Shot content version but does
not require it to be current and does not mutate the Shot or run invalidation.

Append and read hold the store's shared project lock. Append validates the entity,
artifact containment, artifact existence, and SHA-256 before writing. Both append and
listing validate that every receipt's episode/shot identity and historical `record_version`
match an existing Shot snapshot in that ledger. Ledger predecessor metadata must be an
integer (not a boolean) and advance exactly once. Listing rechecks each referenced
artifact's existence and hash. Missing source versions, changed, malformed, or escaping
artifacts fail loudly; no record is skipped. Results are ordered by output-ledger mutation
token.

## On-disk layout

Project-local JSON remains authoritative. Store each immutable record at:

```text
episodes/<episode_id>/shots/<shot_id>/outputs/o<token:08d>-<output_id>.json
```

Each file contains the entity JSON plus `_mutation_token` and `_predecessor_token`,
following the existing store metadata convention. The token is contiguous per Shot output
ledger. The filename and token metadata must agree. Each append uses the existing
contained atomic writer (`ProjectRoot.write_with`) and refuses an occupied destination;
it never replaces an output. The output artifact path points to a separately retained
project-local file, typically in the adapter's attempt directory. SQLite is not involved.
An absent ledger directory means no outputs; an existing non-directory at that path is
corrupt and fails with `JobError(INVALID_MANIFEST)`.

## Required negative cases

- Reject invalid kind, missing required per-kind provenance, extra provenance on the
  wrong kind, malformed SHA-256, unsafe relative path, and non-positive content version.
- Reject append for a missing episode, missing Shot, or unavailable historical Shot
  content version; do not compare `record_version` to `expected_version`.
- Reject stale ledger `expected_version` with `VersionConflict`; a failed append must not
  consume a token or replace an existing record.
- Reject duplicate `output_id` and any occupied destination without overwriting.
- On listing, fail on malformed JSON/entity, filename/token mismatch, token gap,
  missing artifact, artifact hash mismatch, and containment escape.
- Preserve existing schema-1 legacy entity reads without rewriting their bytes.
- Retain and return multiple outputs for the same `(episode_id, shot_id,
  record_version, kind)` in deterministic append order.

## Provenance evidence and scope

The Blender runner exposes the applied engine in worker metadata and its build hash;
`GenerationRun.tool_version` also combines the version and build. The PostProfile runner
emits profile semver, filter graph SHA-256, and FFmpeg identity, while its profile has
`identity.pinned_binaries`. This entity records only the named fields above. The existing
ComfyUI runner's generation metadata is not added as a new ShotOutput field by this
contract. No adapter code is changed here.
